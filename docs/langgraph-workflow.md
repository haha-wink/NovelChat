# 对话工作流：LangGraph 实现

> 代码：`backend/app/agent/graph.py`；入口：`api/v1/endpoints/chat.py:chat_endpoint`
> 骨架：LangGraph 1.2.20 ｜ 生成仍走 Agno 3.1.3

---

## 1. 为什么改成图

改造前，一轮对话的编排是 `chat_endpoint` 里一条 **150 行的直线代码**：
预处理 → 判断情绪意图 → 推进状态 → 拼上下文 → 调模型 → 校验 → 落库 → 发贴纸。

能跑，但三件事难受：

| 问题 | 具体表现 |
|---|---|
| 不好测 | 想验证"规划层写对了没"，得把整个 HTTP 端点跑起来 |
| 不好改 | 想加/跳一步（比如"危机时跳过 RAG"），只能在直线里插 `if` |
| 看不见 | 一轮到底走了哪几步、每步产出什么，只能靠日志拼 |

改成图之后，一步一个节点，节点之间传一个 `TurnState`。

---

## 2. 图长什么样

```
START
  │
  ├─ preprocess   清洗输入（去零宽、压空白、限长）
  ├─ classify     情绪 + 强度 + 意图（规则打底，可选 LLM 判官）
  ├─ observe      按用户这句话推进角色情绪（生成前）
  ├─ recall       RAG：按这句话捞相关往事
  ├─ plan         规划层：算 SOP，拼 additional_context
  ├─ generate     调模型，逐字写进自定义流
  ├─ verify       去旁白 / 安全兜底 / 去说教 / 危机补充
  └─ persist      两条流水 + 必要时蒸馏
  │
 END
```

线性图，但**每个节点都能单独拿出来测**，加分支只改边。

---

## 3. 状态契约

`TurnState`（`TypedDict`）分三块：

| 块 | 字段 | 谁填 |
|---|---|---|
| 输入 | `user_id` `user_name` `character` `book_title` `shelf_id` `raw_text` `image_urls` `api_key` `model` `extra_context` `persist` `session_id` `summarizer` `llm` | 调用方 |
| 中间 | `text` `signals` `plan` `affect_state` `memory_block` `state_block` `context_block` `reply` `notes` | 节点 |
| 出口 | `reply` `notes` | 最后两个节点 |

**`llm` 是注入的**：`generate` 节点不关心模型是谁，只要给一个
`async generator`，逐段吐字就行。这样：

- 测试里传假的（不连网络、不花钱）
- 将来换成 LangChain 的 `ChatModel`，只改这一个函数

---

## 4. 流式怎么出去

`generate` 和 `verify` 节点里用 LangGraph 的 `get_stream_writer()` 直接往外写事件：

```python
writer = get_stream_writer()
writer({"type": "delta", "text": piece})     # 生成中
writer({"type": "final", "text": corrected}) # 校验改写后
```

端点上：

```python
async for mode, chunk in graph.astream(state, stream_mode=["custom", "values"]):
    if mode == "custom":
        yield sse(chunk)          # 直接转成 SSE
    elif chunk.get("reply"):
        final = chunk             # 收尾要用（贴纸、日志）
```

- `custom` → 节点自己写的事件（delta / final）
- `values` → 每步之后的状态快照（最后一份带 `reply` / `signals` / `affect_state`）

---

## 5. 哪些还在 Agno

| 能力 | 在谁那儿 |
|---|---|
| 编排、状态传递、流式出口 | **LangGraph** |
| 提示词组装、记忆检索、情绪推进、SOP、校验 | **项目自己的 services**（`dialogue` / `affect` / `rag`） |
| 模型调用、会话历史、Agno 原生记忆 | **Agno**（`manager.get_agent`） |

**为什么没把 Agno 换掉**：它负责会话历史（`num_history_runs`）与流式 chunk 解析，
换掉属于另一个量级的改动，而且会把"记忆"从 Agno 的 `agent_memory.db` 迁走。
现在把它收敛成一个可注入的 callable，需要时再换。

> 顺带：Agno 的 `update_memory_on_run` 每轮会额外调一次模型做记忆抽取。
> 如果哪天觉得单聊太贵，这是第一个能关的（关掉后还有项目自己的
> `chat_turns → 蒸馏 → RAG` 这条链兜底）。

---

## 6. 怎么加一步

**加节点**（比如"危机时跳过 RAG"）：

```python
graph.add_node("safety", safety_node)
graph.add_edge("classify", "safety")
graph.add_edge("safety", "recall")
```
或者用条件边：

```python
graph.add_conditional_edges(
    "classify",
    lambda s: "persist" if s["signals"].crisis and s.get("panic_mode") else "observe",
)
```
节点顺序变了，`graph.describe()`（`/api/health` 的 `workflow` 字段）会跟着变，
不需要改端点。

**跳过某步**：`persist=False` 时 `persist` 节点直接返回空——群聊就是这么复用的。

---

## 7. 验证

`/api/health`：

```json
"workflow": {
  "engine": "langgraph",
  "nodes": ["preprocess","classify","observe","recall","plan","generate","verify","persist"],
  "compiled": true
}
```

自检里的 `langgraph_checks()`（10 项，不连网络）：

```
LangGraph：图已编译                      ✓
LangGraph：八个节点齐全                  ✓
LangGraph：节点顺序固定                  ✓
LangGraph：generate 节点吐出了 delta     ✓
LangGraph：文本拼起来了                  ✓
LangGraph：verify 改写了说教腔           ✓
LangGraph：state 里带上了本轮判断         ✓
LangGraph：plan 产出了 SOP 块            ✓
LangGraph：persist 写了 user + assistant ✓
LangGraph：persist=False 不写库          ✓
```

加端到端：`HTTP：本轮意图/情绪写进流水`、`单聊图片写进历史` 也都在跑真实端点。

---

## 8. 群聊也迁过来了

`agent/group_graph.py`，四个节点：

```
START → prepare ─┬─(有人)──────────→ pick ─┬─(有人)→ speak ─┬─(本批还有)→ speak
                 │                         └─(没人)→ finish  ├─(被@了)→ pick
                 └─(空书架)→ finish ←───────────────────────┴─(结束)→ END
```

| 节点 | 职责 |
|---|---|
| `prepare` | 成员加载、@ 解析、**@全体唤醒**、用户那句落库 |
| `pick` | 每轮选人（规则仍由 `group.select_speakers` 决定） |
| `speak` | 一个人：生成 → 去旁白 → 校验 → 落库 → 贴纸 → @ 链式 |
| `finish` | 汇总（`done` 事件带 rounds / speakers） |

`run_turn` 的签名和事件流**没变**（`user` / `round_start` / `speaker_start` /
`delta` / `speaker_end` / `done`），所以前端和既有测试一行没改——
变的是内部：现在它只是"填 state → 跑图 → 转事件"。

顺带修了一个静默 bug：`GROUP_ALWAYS_REPLY` 以前只进了 options 但从没传给
`select_speakers`，等于关不掉兜底；现在接上了。

**循环怎么不走爆**：图里有回边（speak → speak / speak → pick），
调用时用 `config={"recursion_limit": 100}`，够 3 轮 × 3 人跑完。

验证（`group_graph_checks`，11 项）：

```
群聊图：已编译                        ✓
群聊图：四个节点                      ✓
群聊图：先回 user 事件                ✓
群聊图：有 round_start                ✓
群聊图：speaker_start/end 成对        ✓
群聊图：最后是 done                   ✓
群聊图：speaker_end 带最终文本         ✓
群聊图：被 @ 的人下一轮接上            ✓
群聊图：done 汇总了发言人与轮数        ✓
群聊图：每条发言都落了库               ✓
群聊图：落库带轮次                    ✓
群聊图：空书架给提示并收尾             ✓
```

---

## 9. 两张图的分工

| | 单聊 `agent/graph.py` | 群聊 `agent/group_graph.py` |
|---|---|---|
| 规模 | 8 节点线性 | 4 节点带循环 |
| 一轮 | 一个角色说一次 | 多个角色 × 多轮 |
| 状态 | `TurnState` | `GroupState` |
| 生成函数 | `llm`（注入） | `generate(member, transcript)`（注入） |
| 落库 | `persist` 节点 | `prepare`（用户）+ `speak`（每条发言） |

两张图的节点函数是**同一个风格**：阻塞操作走 `asyncio.to_thread`，
流式输出走 `get_stream_writer()`，校验都调 `dialogue.verify`。
