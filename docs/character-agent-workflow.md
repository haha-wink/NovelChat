# 角色 Agent 工作流

> 基于源码逐行核对（`agent/manager.py`、`agent/character.py`、`api/v1/endpoints/chat.py`、
> `services/{prompts,affect,rag,agent_memory,chat_memory,expression,reply}.py`）。
> 行号对应本文写作时的版本。

---

## 0. 一句话概括

**一个角色 = 一个 Agno Agent，被 LRU 缓存复用**；每轮对话由「稳定提示词（构建时固定）+
易变状态（每轮临时挂载）」两段拼成，流式生成后更新情绪与记忆。

```
                         ┌──────────────┐
  POST /api/chat ──────► │ chat_endpoint│
                         └──────┬───────┘
                                │
        ① 取 Agent（缓存）  ────► get_agent ──┐ 未命中 → build_agent
                                │             │   ├ DeepSeek 模型
        ② 挂本轮状态（副本）───► attach_state  │   ├ SqliteDb(memory_db_path)
                                │             │   ├ MemoryManager（agentic memory）
        ③ 流式生成          ────► agent.arun   │   └ instructions = 角色档案 + 记忆
                                │
        ④ 收尾（4 件事）    ────► 见 §2.4
```

---

## 1. 一次单聊的完整时序

```
POST /api/chat  { token, character_id, prompt, images_base64 }
│
├─ deps.resolve_session(token)           HMAC 校验 → Session(user_id, session_id, display_name)
├─ db.get_character / db.get_book         取角色档案与书名
│
├─ get_agent(api_key, character, book_title, user_name)          ← ① 见 §2.1
│     cache_key = sha256(api_key | character_id | updated_at | user_name)[:24]
│     命中 → 直接返回（move_to_end）；未命中 → build_agent 并塞进 LRU（容量 8）
│
├─ character_agent.attach_state(agent, character, user_id, user_text)   ← ② 见 §2.2
│     state  = affect.load_state(character, user_id)
│     context = affect.prompt_block(state)          # 心情 / 对你的态度 / 表达禁忌
│     recalled = rag.recall_block(character_id, user_text, top_k=4)     # 相关记忆
│     per_run = copy.copy(agent); per_run.additional_context = context + recalled
│
├─ agent_log.log("chat_turn_started")     记 mood / turns / has_images
│
├─ async for chunk in per_run.arun(...)   ← ③ 见 §2.3
│     SSE: {"type":"delta","text":...}  × N
│
├─ reply.strip_stage_directions(accumulated)     去掉"（他抬起眼）"这类旁白
│    若和流式文本不同 → SSE: {"type":"final","text":...}
│
├─ affect.observe_user_message(...)        ← ④ 推进情绪/好感度，永不抛异常
├─ db.add_chat_turn(...) × 2              user + assistant 两条流水（含图片 URL）
├─ chat_memory.maybe_distill(...)         攒够阈值就压成一条长期记忆
├─ expression.maybe_sticker(...)          规则优先，否则按 arousal 掷骰子
└─ SSE: {"type":"done"}
```

---

## 2. 四个阶段

### 2.1 构建 Agent（每个角色一次）

`agent/manager.py:build_agent`

| 组成 | 取值 | 说明 |
|---|---|---|
| 模型 | `DeepSeek(id=deepseek_model, api_key)` | 默认 `deepseek-chat` |
| 会话库 | `SqliteDb(memory_db_path)` | `data/workspace/agent_memory.db`，Agno 自己的 |
| 记忆管理器 | `MemoryManager(model, db, memory_capture_instructions)` | 抽取"该记什么"的指令 |
| system 指令 | `build_instructions()` | 见下 |
| 历史 | `add_history_to_context=True`, `num_history_runs=8` | 最近 8 轮对话进上下文 |
| 原生记忆 | `enable_agentic_memory=True`、`update_memory_on_run=True`、`add_memories_to_context=True` | **每轮会额外调一次模型做记忆抽取** |
| 输出 | `markdown=True` | 前端 `renderMarkdown` 渲染 |

`build_instructions` 只放**稳定内容**：

```
角色档案（DB 里的 excerpts 会被换成"记忆条目 top-3"）
  ├ 身份 / 性格 / 说话风格 / 背景 / 人际关系
  ├ 【语言习惯】 ← services/style.py 统计自 quotes（平均字数、标点、口头禅）
  ├ 【说话样本】 ← quotes 原文（PROMPT_QUOTE_LIMIT=12 / PROMPT_QUOTE_CHARS=700）
  ├ 【原著片段记忆】← agent_memory.top(limit=excerpt_chunks=3)
  ├ 【记忆摘要】   ← agent_memory.summary(character_id)
  └ 【扮演规则】9 条（第一人称、不说破 AI、不写动作、默认 1~2 句、自伤兜底）
```

**为什么不能把情绪写进 instructions**：Agent 被缓存，`instructions` 只在构建时读一次；
写进去要么每轮重建（缓存失效），要么永远停在第一轮。

### 2.2 每轮注入（易变部分）

`agent/character.py:attach_state`

```python
state  = affect.load_state(character, user_id)   # 没有记录就按角色基线初始化
per_run = copy.copy(agent)                        # ← 关键
per_run.additional_context = prompt_block(state) + rag.recall_block(...)
```

`prompt_block` 产出的是**人话，不含数字**：

```
【此刻状态】
心情：失落（语气偏低，句子短）
你对对方的态度：有点在意（会留意对方的反应，但不主动示好）
【表达规则】
- 用言行体现情绪，不要说破「情绪值」「好感度」这类内部信息。
- 情绪低落时可以直接表达，不要忍着。
- （按情绪/好感度档位追加禁忌，如"不要突然热情"）
```

**两个设计点**：

1. **`copy.copy`**：缓存里的 Agent 被并发请求共享，直接改它的 `additional_context`
   会把 A 的情绪串给 B。每轮操作副本，缓存对象始终保持 `additional_context is None`。
2. **RAG 按"用户这句话"检索**：`recall_block(character_id, user_text, top_k=4)`，
   命中后拼成【与你这句话相关的记忆】，随易变部分注入。检索失败只记日志，不影响回复。

### 2.3 生成

```python
session_id = conversation_session(session, character_id)
           = hmac_sha256(user_id : session_id : character_id)[:32]
```

确定性哈希的三重好处：换角色不串记忆、刷新页面能接上同一段剧情、同一用户多端一致。

- **历史**：Agno 自己按 `session_id` 从 `agent_memory.db` 取最近 8 轮
- **图片**：落盘 `uploads/chat/<hex>/`，以 `AgnoImage(filepath=...)` 传入；
  无文字时兜底 `"请看着这张截图，说说你的想法。"`
- **流式差分**：Agno 的 chunk 是"累积文本"，代码里做前缀差分算出真正的增量：

```python
if content.startswith(accumulated):   delta = content[len(accumulated):]
elif accumulated.endswith(content):   delta = ""
else:                                delta = content; accumulated += content
```

### 2.4 收尾的四件事

| 步骤 | 代码 | 失败影响 |
|---|---|---|
| 去动作旁白 | `reply.strip_stage_directions` → `final` 事件覆盖 | 无（只影响观感） |
| 推进情绪 | `affect.observe_user_message(text, image_count)` | 内部 try/except，**永不抛** |
| 记流水 | `db.add_chat_turn` × 2（user / assistant，带 images） | 记日志忽略 |
| 蒸馏记忆 | `chat_memory.maybe_distill`（未蒸馏条数 > 40 时压一半） | 记日志忽略 |
| 贴纸 | `expression.maybe_sticker(state, mood_changed, asked_about_mood)` | 记日志忽略 |

贴纸是**规则优先于概率**：问起心情 / 这轮心情变了 → 必发；否则按 `arousal` 掷骰子。

---

## 2.5 对话工作流九段落地（NovelChat）

> **定位**：NovelChat 是**情感聊天**项目——用户借小说角色的性格与语气，聊的是自己的事。
> 不是角色扮演：不演剧情、不推进故事线，角色要"接得住人"。

因此提示词的主干从「扮演规则」改成了**优先级**：

```
1. 对方的安全
2. 对方的情绪被接住
3. 你的性格、身份与说话方式     ← 冲突时前面的赢
```

并且明确写了「平时 1~2 句，但对方在情绪里时可以多说两句——别为了像原著那样惜字如金
而把人晾在那儿」。这是从"像不像"到"陪不陪得住"的转向。

| 阶段 | 实现 | 说明 |
|---|---|---|
| ① 输入预处理 | `services/dialogue.py:preprocess` | 去零宽字符/压空白/限长；**不动标点与 emoji**（它们本身就是情绪） |
| ② 情绪 & 意图识别 | `dialogue.classify` + 可选 `classify_with_judge` | 规则打底（情绪/强度/意图，危机优先，带否定词处理）；`DIALOGUE_JUDGE_MODE` 开启后由 LLM 精判 |
| ③ 记忆检索 | `rag.recall_block`（原有） | 按用户这句话检索，top_k=4 |
| ④ 状态调度 | `affect.observe_user_message` **挪到生成前** | 回复用的是"听完这句话之后"的心情，而不是上一轮的 |
| ⑤ 规划层 SOP | `dialogue.plan` → `additional_context` | 危机→安全流程；倾诉→共情少建议；求助→先接情绪再给 1~2 条办法 |
| ⑥ 工具/素材 | `Plan.materials` | 规则模板（放松引导、一句接住情绪的话、一条小步骤），不额外调模型 |
| ⑦ 生成回复 | `agent.arun` | 模型看到：档案 + 语言习惯 + 说话样本 + 此刻状态 + 相关记忆 + SOP |
| ⑧ 后置校验 | `dialogue.verify` | 不安全词整段替换；「你应该」→「你可以」；危机缺求助提示时补一句 |
| ⑨ 记忆写入 | `add_chat_turn(intent/emotion)` → `maybe_distill` | 蒸馏摘要带上「情绪：疲惫×2；意图：倾诉×2」，一起向量化 |

### ② 的两层：规则打底 + 可选判官

规则层（零调用、可复现、可测）：

| 能力 | 实现 |
|---|---|
| 情绪词 + 否定 | 「我不开心」按**难过**算（不会因为跳过「开心」而误判成平静）；「不难过」不算负向 |
| 强度 | 词命中 ×0.25 + 负向 +0.15 + 连续感叹号/问号 + 长度 |
| 意图 | 求助词 / 倾诉词 / 默认闲聊；**危机优先**（硬词直接判，软词要求"我"作主语，「想死你了」是撒娇） |
| 歧义标记 | 求助+倾诉都命中、长句但没情绪词、问句读不出情绪 → `ambiguous=True` |

判官层（`DIALOGUE_JUDGE_MODE`）：

| 档位 | 行为 | 成本 |
|---|---|---|
| `off`（默认） | 只跑规则 | 0 |
| `auto` | **只在 `ambiguous=True` 时**问一次模型 | 偶发 1 次 |
| `always` | 每轮都问 | 每轮 +1 次 |

判官拿到的是「反讽、隐喻、话里有话」这类规则读不懂的情况；返回严格 JSON
（emotion / intensity / intent / crisis / reason），解析时按白名单校验。
结果按文本 SHA1 缓存 5 分钟（同一条消息重发不会重复花钱），
模型超时或返回不合法时**静默回退规则结论**，不影响聊天。

安全合并规则：**任一方判危机就是危机**（宁可多判，最多多一句关心）。

**校验为什么放在最后而不是提示词里**：模型偶尔会滑回说教腔或短到冷漠，
提示词只能"要求"，`verify` 能"保证"。它只做低风险改写（替换固定搭配、补一句话），
改动痕迹全部写进 `agent_events` 的 `reply_verified`。

**九段里没做的**：语音情感（没有语音输入）；LLM 二次精判意图（会多一次调用，收益只在反讽等边角）；
工具里的"推荐文案"没接外部服务（现在是本地模板）。

## 3. 三套记忆并存（最容易看混的地方）

| 记忆 | 存储 | 谁写 | 谁读 | 作用 |
|---|---|---|---|---|
| **A. Agno 原生记忆** | `data/workspace/agent_memory.db` | Agno `MemoryManager`（`update_memory_on_run`，每轮一次模型调用） | Agno 自动注入 `memories` | 记"用户是谁、聊过什么约定" |
| **B. 项目记忆条目** | `character_memory_items` + `memory_chunks` | 抽取阶段（原著片段）、`chat_memory.distill`（对话事实）、手动 | ① `agent_memory.top(3)` → instructions ② `rag.recall_block(4)` → additional_context | 角色"记得书里的情节"和"聊天里聊出来的事实" |
| **C. 对话流水** | `chat_turns` | `add_chat_turn`（单聊/群聊每轮） | 历史接口（用户重进看到的记录）、蒸馏的源 | 用户可见的聊天记录 |

三者的关系：

```
chat_turns ──(超过 40 条，蒸馏)──► character_memory_items(kind=fact)
                                          │
                                          └─► rag.index_character → memory_chunks(向量)
Agno memory ── 独立体系，不参与上面这条链 ──► 只进 Agno 自己的上下文
```

**这是一个可以讨论的设计**：A 和 B 功能重叠（都在记"用户偏好/约定"），
A 多花一次模型调用且不可控、B 可检索可控。如果哪天觉得单聊太贵，
第一个能砍的就是 `update_memory_on_run`（改成关掉，只靠 B）。

---

## 4. 情绪闭环

```
第 N 轮：
  load_state ──► prompt_block ──► 注入 ──► 生成回复
                                        │
  observe_user_message ◄────────────────┘   （看用户这句话，推进 valence/arousal/favorability）
        │
        └─► 落库 character_affect ──► 第 N+1 轮 load_state 读到新状态
```

- 状态维度：`valence`（正负）、`arousal`（强度）、`favorability`（好感度）、`emotion`（9 标签）
- **用户永远看不到数字**，只体现在语气和 `prompt_block` 的措辞里
- 基线（`baseline_for`）随角色编辑更新——改档案会重塑角色的"默认心情"

---

## 5. 缓存与失效

```
cache_key = sha256(api_key | character_id | updated_at | user_name)
```

| 操作 | 是否重建 Agent |
|---|---|
| 编辑角色档案（改名字/性格/说话风格） | ✅ `updated_at` 变 → 新键 |
| 改用户名 | ✅ |
| 换 API Key | ✅ |
| 只是聊天 | ❌ 命中缓存 |
| **改配置（PROMPT_QUOTE_*、excerpt_chunks 等）** | ❌ **不生效，要重启进程** |
| 容量超过 8 | 最久未用的被淘汰 |

> 最后一条是实战里最容易踩的坑：改完提示词相关配置以为立刻生效，
> 其实旧 Agent 还在缓存里。重启后端最稳。

---

## 6. 关键参数

| 参数 | 默认 | 影响 |
|---|---|---|
| `AGENT_CACHE_SIZE` | 8 | 同时缓存的角色数 |
| `HISTORY_RUNS` | 8 | 进上下文的最近对话轮数 |
| `EXCERPT_CHUNKS` | 3 | 进 instructions 的初始记忆条数 |
| `RAG_TOP_K` | 4 | 每轮检索的相关记忆条数 |
| `PROMPT_QUOTE_LIMIT` / `_CHARS` | 12 / 700 | 说话样本条数与字符预算 |
| `CHAT_DISTILL_THRESHOLD` | 40 | 未蒸馏流水超过多少条触发压缩 |
| `MAX_PROMPT_CHARS` | 8000 | 单次输入上限 |

**成本结构**（已核对 Agno 源码）：

| 调用 | 触发条件 | 源码 |
|---|---|---|
| 回复生成 | 每轮 1 次 | `agent.arun` |
| 自动记忆抽取 | `update_memory_on_run=True` → 每轮拿**用户这句原话**再调一次模型 | `agno/agent/_managers.py:38-50` |
| 记忆工具 | `enable_agentic_memory=True` → 模型可主动调用 `update_user_memory` | `agno/agent/_default_tools.py` |
| 对话蒸馏 | 未蒸馏流水 > 40 条时 | `services/chat_memory.py:177` |

也就是说：**一次单聊最少 2 次模型调用**（回复 + 自动抽取），模型主动存记忆时还会再多。
群聊按发言人数倍增。带图另算视觉 token。

如果觉得单聊太贵，第一个能关的是 `update_memory_on_run`（保底还有项目自己的
`chat_turns` 蒸馏链），第二个是 `enable_agentic_memory`——但那样 Agno 原生的
"用户偏好记忆"能力会退化，需要评估。

---

## 7. 排查手册

| 症状 | 先查 |
|---|---|
| 角色像失忆 | `agent_memory.top()` 有没有内容；`agent_memory.summary()`；`chat_distill_threshold` 是否触发过 |
| 记忆检索不灵 | `GET /api/rag/status` 的 backend 是不是 `vector`；`memory_chunks` 条数 |
| 情绪不推进 | `character_affect` 有没有新行；`agent_events` 里的 `affect_change` |
| 两个角色串味 | `session_id` 是不是按角色算的；`attach_state` 有没有 `copy.copy` |
| 角色不像本人 | `quotes` 有几条；提示词里有没有【说话样本】【语言习惯】 |
| 群聊没人说话 | `GROUP_ALWAYS_REPLY`、`GROUP_SPEAK_THRESHOLD` |
| 改了提示词没变化 | **重启后端**（Agent 缓存） |

---

## 8. 与最近改动的关系

- **说话样本 / 语言习惯**（P12）：补在 `build_instructions` 的稳定部分，
  成本是一次性的（缓存复用），所以每轮不多花钱。
- **聊天图片**（P13）：落盘 + `chat_turns.images` 记 URL，
  Agent 侧只多传一个 `AgnoImage`，工作流本身没变。
- **情绪注入**（P4）：走 `additional_context` 而不是 `instructions`，
  这是"缓存 + 每轮变化"这对矛盾的解法。