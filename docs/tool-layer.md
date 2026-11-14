# 工具层：三层结构

> 代码：
> ① 协议 `core/tool_protocol.py`
> ② 框架 `agent/tools/langgraph_tools.py`
> ③ 业务 `services/skills.py`

---

## 1. 为什么分三层

同一批能力，在不同场合需要不同形态：

| 场合 | 需要什么 | 对应层 |
|---|---|---|
| 模型自己决定调工具 | JSON Schema（function calling） | ① 协议 |
| LangGraph 编排（ToolNode） | `StructuredTool` / `ToolNode` | ② 框架 |
| 业务说"安慰一个人要做什么" | 一个 `Skill` 对象 | ③ 业务 |

如果只有一层，三种用法会互相污染：业务代码里出现 LangChain 类型，
或者协议层被迫处理 LangGraph 的 state。

```
③ Skill    services/skills.py            安慰 / 稳住 / 给建议
                 │ 调用
                 ▼
② Tool     agent/tools/langgraph_tools.py StructuredTool · ToolNode
                 │ 包装
                 ▼
① Protocol  core/tool_protocol.py         ToolSpec · 注册表 · execute
```

**依赖方向是从上往下**，且 `protocol` 放在 `core`——因为 `services` 不能 import `agent`
（分层铁律），技能层要用协议层，协议层就必须待在更底层。它也只依赖标准库。

---

## 2. ① 协议层：`core/tool_protocol.py`

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str                    # 工具名
    description: str             # 给模型看的说明
    parameters: dict[str, dict]  # JSON Schema 的 properties
    handler: Callable[[dict], dict]
    required: tuple[str, ...]    # 必填参数
    domain: str = ""             # 归属域
```

四个 API：

| 函数 | 作用 |
|---|---|
| `register(spec)` / `unregister(name)` | 注册 / 注销 |
| `get(name)` / `names()` / `specs()` | 查询 |
| `openai_schema(names)` | 产出 function calling 声明 |
| `execute(name, args)` | 校验必填 + 执行 + **吞掉异常** |

**`execute` 永不抛异常**——工具是锦上添花，挂了也不能把整轮对话搞崩：

```python
>>> execute("comfort_phrase", {})
{'ok': False, 'name': 'comfort_phrase', 'error': '缺少参数：emotion'}
>>> execute("no_such_tool", {})
{'ok': False, 'name': 'no_such_tool', 'error': '没有这个工具'}
>>> execute("_boom", {})          # 工具内部 raise
{'ok': False, 'name': '_boom', 'error': '炸了'}
```

---

## 3. ② 框架层：`agent/tools/langgraph_tools.py`

```python
def to_structured_tool(spec) -> StructuredTool   # 协议层 -> LangChain 工具
def to_langgraph_tools(names) -> list[StructuredTool]
def build_tool_node(names) -> ToolNode           # LangGraph 预置节点
def invoke_tool_calls(tool_calls) -> list[dict]  # 按 AIMessage.tool_calls 形状执行
```

**当前用法**：`ToolNode` 是**能力预留**——本项目不让模型自由调工具
（角色说话不该有工具调用延迟），真正跑的是 `skill` 节点里同步调 `protocol.execute`。

这样既没把 LangChain 类型漏进业务层，又保留了升级路径：
将来要接"模型自主调用"，把 `build_tool_node()` 挂进图即可。

---

## 4. ③ 业务层：`services/skills.py`

一个 Skill = 「什么场合用」+「调哪些工具」+「怎么拼成给模型的话」。

| 技能 | 触发条件 | 调用工具 | 产出 |
|---|---|---|---|
| `comfort` | 意图 = 倾诉 | `comfort_phrase` + `breathing_guide` | 一句接住情绪的话 + 呼吸引导 |
| `steady` | 危机 = true | `crisis_helpline` | 现实求助渠道（热线/身边人） |
| `suggest` | 意图 = 求助 | `small_step` | 一条今天能做的动作 |
| （独立工具） | 群聊每轮 | `greeting_check` / `local_time` | 时间问候 / 劳累问候 |

```python
result = skills.run_for({"intent": "vent", "emotion": "疲惫", "crisis": False})
# SkillResult(skill='comfort', materials=['累了就是累了，不用硬撑给我看。',
#                                        '先慢慢吸气 4 秒……'])
result.as_block()
# 【可用素材（自然融入，不要照念）】
# - 累了就是累了，不用硬撑给我看。
# - 先慢慢吸气 4 秒……
```

**闲聊不调用任何技能**（返回 `None`）——不乱塞素材。

---

## 5. 接进图：`skill` 节点

单聊图多了一个节点，位置在「规划层」之后、「生成」之前：

```
preprocess → classify → observe → recall → plan → skill → generate → verify → persist
                                                     ↑
                                    按 signals 挑技能、跑工具、拼素材
```

`skill` 节点做的事：

```python
signals = state["signals"]                    # 情绪/强度/意图/危机
result  = skills.run_for({...})               # ③ 业务层
block   = result.as_block()                   # 素材文本
context = state["context_block"] + "\n\n" + block
```

于是模型最终看到的是：

```
【本轮判断（内部参考，不要说破）】
用户情绪：疲惫（强度 0.4）｜意图：倾诉
【应对策略】
- 以共情为主：先承认对方的感受，别急着给方案
- 不要列清单、不要 1/2/3、不要说「你应该」
【可用素材（自然融入，不要照念）】
- 累了就是累了，不用硬撑给我看。
- 先慢慢吸气 4 秒，停两秒，再慢慢呼出来；这样来 3 轮。
```

**零额外模型调用**——工具全是同步纯函数。

---

## 6. 验证

`/api/health`：

```json
"tools": {
  "protocol": {
    "count": 4,
    "tools": ["breathing_guide", "comfort_phrase", "crisis_helpline", "small_step"]
  },
  "langgraph_tools": ["breathing_guide", "comfort_phrase", "crisis_helpline", "small_step"],
  "tool_node_ready": true
},
"skills": {
  "skills": [
    {"name": "comfort", "tools": ["comfort_phrase", "breathing_guide"]},
    {"name": "steady",  "tools": ["crisis_helpline"]},
    {"name": "suggest", "tools": ["small_step"]}
  ]
}
```

自检（`skill_checks`，20 项）：

```
工具①：注册了四个工具                   ✓
工具①：能产出 function calling 声明      ✓
工具①：正常执行返回 ok                  ✓
工具①：缺参数返回 ok=False 而不是抛异常   ✓
工具①：未知工具返回 ok=False            ✓
工具①：工具内部报错也不抛出来            ✓
工具②：转成 LangChain StructuredTool    ✓
工具②：能构造 LangGraph 的 ToolNode     ✓
工具②：按 tool_calls 形状执行           ✓
工具③：倾诉走 comfort 技能              ✓
工具③：危机走 steady 技能               ✓
工具③：危机素材含现实求助                ✓
工具③：求助走 suggest 技能              ✓
工具③：闲聊不调用任何技能                ✓
工具③：素材块格式正确                    ✓
```

---

## 7. 加一个新工具需要动哪里

```
① core/tool_protocol.py  —— 不用动（只提供注册机制）
② agent/tools/...        —— 不用动（自动从注册表转换）
③ services/skills.py     —— 写 handler + register + 决定哪个 Skill 用它
```

**一个文件，三十行。** 这就是分层的收益。

---

## 8. 时间工具（独立于技能，直接挂群里）

两个工具，注册在同一套协议层：

| 工具 | 输入 | 输出 |
|---|---|---|
| `local_time` | 无 | 本地时间、时段（早上/上午/中午/下午/傍晚/晚上/深夜）、建议问候语 |
| `greeting_check` | `shelf_id` | 要不要发时间问候 / 劳累问候 + 现成的话 |

**判断规则**：

* **时间问候**：看"这个时段是不是第一次说话"。上一条用户消息落在**不同**时段
  （或今天还没说话，或同一时段隔了 3 小时以上）才打招呼。
  用定时器（每天 8 点发早安）在聊天里很怪——用户下午才来会收到"早上好"。
* **劳累问候**：深夜（≥23:00 或 <05:00），**或**最近一小时 ≥20 条且本轮已聊 ≥30 分钟。
  频率从 `chat_turns` 现算，不额外建表。

**问候由谁说**：随机挑一个本轮要发言的角色，把问候语拼在他回复的最前面——
不额外发一条系统消息，也不指定固定人选。

```
事件流：user → round_start → dispatch → greeting → speaker_start → delta → speaker_end → done
                                        ↑
                        {speaker: "加图索", text: "傍晚了。", reason: "跨时段"}
```

实测（真实书架）：

```
加图索: 傍晚了。我是加图索。
```
