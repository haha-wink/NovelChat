# 记忆层梳理（2026-11-23 版）

> 一句话结论：记忆分 **6 层**；真正走 RAG 的只有「角色长期记忆」一层；
> 工作记忆按对话隔离、只在工作模式写；上下文和记忆都有硬预算，避免注意力被稀释。

## 1. 记忆怎么分层

```
┌──────────────────────────────────────────────────────────────────┐
│ ① 工作记忆  working_memory            work 模式 · 按对话隔离     │
│    “这次任务的约束 / 进度 / 试过的方案”，草稿纸；不进 RAG        │
│    清空对话时并入长期记忆，然后清空                              │
├──────────────────────────────────────────────────────────────────┤
│ ② 短期流水  chat_turns                7 天窗口 · 原文保留        │
│    用户重进群聊看到的聊天记录；清空对话时蒸馏                     │
├──────────────────────────────────────────────────────────────────┤
│ ③ 长期记忆  character_memory_items    RAG 检索层（跨对话）       │
│    kind=excerpt 原著片段 / summary 跨书摘要 / fact 对话蒸馏      │
│    + memory_chunks 220 字块级向量（bge-small-zh，512 维）        │
├──────────────────────────────────────────────────────────────────┤
│ ④ 会话记忆  agent_memory.db           Agno MemoryManager        │
│    按 user_id / session_id 的“最近几轮说过什么”（非 RAG）        │
├──────────────────────────────────────────────────────────────────┤
│ ⑤ 管理经验  manager_memories          书架级：谁和谁是同一个人   │
├──────────────────────────────────────────────────────────────────┤
│ ⑥ 派生状态  character_affect / group_member_state                │
│    情绪、好感度、在线状态——不是记忆，但决定“怎么说话”            │
└──────────────────────────────────────────────────────────────────┘
```

### ① 工作记忆（2026-11-23 新增）

- 表：`working_memory`（`conversation_id` / `shelf_id` / `user_id` / `mode` / `kind` / `content`）
- 写入：群聊 `agent/group_graph.py` 的 `prepare_node`，**只在 mode=work 时**写：
  用户每句话记 `kind=user`；技能起任务记 `kind=job`（任务 id + 需求）。
- 隔离：按 `conversation_id`。`conversations.mode` 区分聊天/工作窗口，
  切模式时前端切到该模式自己的对话，消息、任务栏、工作记忆都不串。
- **不进 RAG**：任务笔记（720p / 副歌 / 赛博朋克）不该在下次闲聊时被当往事捞出来。
- 收拾：`chat_memory.clear_conversation` 蒸馏时把工作记忆拼进摘要，然后清空；
  单对话超过 60 条自动丢最老的（`services/memory/working_memory.py: MAX_ITEMS`）。

### ② 短期流水 `chat_turns`

- 字段：`conversation_id` / `role` / `content` / `intent` / `emotion` / `distilled`。
- 保留：默认 7 天（`CHAT_RETENTION_DAYS`）。蒸馏只打 `distilled=1`，**不删原文**——
  同一张表也是用户看到的聊天记录，删了重进群聊会一片空白。
- 触发：默认**不再按条数自动蒸馏**（`chat_distill_threshold=0`），
  改为用户「清空对话」时蒸馏（`services/memory/chat_memory.py: clear_conversation`）。

### ③ 长期记忆（RAG 层）

- `character_memory_items`
  - `kind=excerpt`：小说原文片段（带 `source_book_id`，删书能精确回收）
  - `kind=summary`：跨书摘要（≤1200 字，派生视图，原文不删）
  - `kind=fact`：对话蒸馏出的用户画像 / 事实（`scene=对话`）
- `memory_chunks`：一条长记忆按 220 字（40 字重叠）切块，每块 512 维向量，
  写入时算好；检索时只编码用户这一句（实测 10~25ms）。
- 检索：`services/memory/rag.py`，混合分：
  `0.65 × 余弦 + 0.35 × BM25`，场景一致 ×1.30 / 不一致 ×0.85，摘要 ×1.05，
  低于 0.18 丢弃，默认 top_k=4；向量后端不可用自动降级纯 BM25。

### ④ Agno 会话记忆

- `agent/manager.py`：每个角色一个 Agent，`SqliteDb(agent_memory.db)` + `MemoryManager`，
  `add_history_to_context=True`、`num_history_runs=8`、`enable_agentic_memory=True`。
- 是「最近几轮」的短期上下文，按 user/session 隔离；和 ③ 的区别：
  ③ 是“很久以前的事”，④ 是“刚才说的”。

### ⑤ 管理经验 `manager_memories`

- 书架级：`same_person`（确认同一人，下次直接合并）/ `different_person`（不再问）。
- 只服务合并与书架 UI，不进对话提示词。

### ⑥ 派生状态

- `character_affect`：情绪 + 好感度（快/慢变量）；
- `group_member_state`：谁在线、谁刚说过话。
- 这两张表不参与“回忆”，但每轮进提示词（决定语气和发言资格）。

## 2. 文件结构树（记忆层在哪）

```
E:\NovelChat\
├─ backend\
│  ├─ app\
│  │  ├─ core\
│  │  │  ├─ database.py          # 全部表结构 + 老库迁移 + 工作记忆/对话 CRUD
│  │  │  ├─ config.py            # RAG_TOP_K / EXCERPT_CHUNKS / CHAT_RETENTION_DAYS…
│  │  │  └─ paths.py             # DATA_DIR = backend/data
│  │  ├─ services\memory\
│  │  │  ├─ agent_memory.py      # 长期记忆条目：写入/回收/摘要
│  │  │  ├─ chat_memory.py       # 流水 → 长期记忆（蒸馏 / 清空）
│  │  │  ├─ rag.py               # 向量 + BM25 混合检索（RAG 本体）
│  │  │  ├─ summarizer.py        # 两个摘要提示词（用户画像 / 角色简介）
│  │  │  └─ working_memory.py    # 工作记忆（work 模式、按对话）
│  │  ├─ agent\
│  │  │  ├─ manager.py           # Agno Agent + agent_memory.db（会话记忆）
│  │  │  ├─ graph.py             # 单聊：recall 节点 / 持久化
│  │  │  ├─ group_graph.py       # 群聊：工作记忆写入 / 技能任务
│  │  │  └─ character.py         # 把相关记忆挂到 Agent.additional_context
│  │  └─ api\v1\endpoints\
│  │     ├─ knowledge.py         # RAG 状态/重建索引/检索预览/蒸馏/清洗
│  │     ├─ characters.py        # 角色记忆列表
│  │     └─ shelves.py           # 清空对话（distill/purge）、对话 mode
│  └─ data\
│     ├─ workspace\
│     │  ├─ app.db               # 业务库：记忆条目/块向量/流水/对话/工作记忆
│     │  ├─ agent_memory.db      # Agno 会话记忆
│     │  └─ graph_checkpoints.db # LangGraph 断点
│     ├─ models\
│     │  ├─ bge-small-zh-v1.5\   # RAG 向量模型（512 维）
│     │  └─ erlangshen-sentiment\
│     └─ animation\              # 动画任务（按对话归属）
└─ frontend\src\stores\app.js    # 对话 mode / 任务栏按对话
```

## 3. 写路径（5 条）

### ① 加书进书架 → 原著片段

```
services/catalog/shelf.py  agent_memory.remember(...)
        ↓
services/memory/agent_memory.py: remember(kind="excerpt", scene=...)
        ↓
rag.index_character(...)  →  memory_chunks（220 字 / 512 维）
```

### ② 清空对话 → 对话记忆（fact）

见第 5 节。要点：一次摘要、写给所有说过话的角色、立即建索引、删流水。

### ③ 角色合并 / 重建摘要 → summary

合并逻辑调 `agent_memory.summarize(character_id)`，
用 `SUMMARY_SYSTEM_PROMPT`（角色简介）压成一段，`kind="summary"`。
**原文条目一条不删**：摘要只是派生视图，删书时按来源回收再重建。

### ④ 同人识别 → manager_memories

确认「是/不是同一个人」写进书架级经验，下次不再重复问。

### ⑤ Agno 自带记忆

`enable_agentic_memory` 需要模型主动调工具写，实际很少发生（历史快照全库仅 5 行）。
它和自建记忆并行、互不同步，属于已知冗余。

## 4. 读路径

### ④ 静态注入（每轮相同）

`agent/manager.py: build_instructions`：
`agent_memory.top(character_id, limit=excerpt_chunks=3)` + `agent_memory.summary()`，
进 system prompt。注意：这 3 条 + 1 条摘要和用户当前这句话无关。

### ⑤ 按需检索（每轮不同）→ 这是唯一的 RAG

`agent/graph.py` recall 节点：`rag.recall_block(character_id, text, top_k=rag_top_k=4)`；
意图判官认为“在唤起记忆”时 top_k 翻倍（8）。命中块格式：
`【与你这句话相关的记忆】- （场景）片段`。

### ②③⑥ 其余读取

- 群聊历史：`chat_turns` 按 `conversation_id` + 时间倒序（不是 RAG）；
- 工作记忆：当前对话直接读（不检索）；
- 会话记忆：Agno 最近 8 轮 history（不是 RAG）；
- 管理经验：按 `shelf_id + kind` 精确查，只给 UI/合并用。

所以：**只有长期角色记忆走 RAG**，其余都是结构化精确读取。

## 5. 清空对话的两条路径

```
用户点「清空对话」
   ├─ mode=distill（默认）
   │    流水 + 工作记忆 → 摘要（1 次 LLM）→ 写给每个发言角色的 fact
   │      → 重建向量索引 → 删流水 → 清工作记忆
   │    结果：界面干净，角色记得你
   │
   └─ 「清空对话和记忆」 mode=purge
        流水 → 直接删
             → 删这些角色身上这段对话的 fact
             → 重建索引 → 清工作记忆
        结果：彻底重来，角色忘记这段时间
```

- **为什么只调一次模型**：群聊可能 4 个人说话，每人一次摘要就是 4 倍成本；
  现在一段摘要写给 N 个角色。
- **为什么 purge 只删 fact**：`excerpt` 和 `summary` 不是这段对话产生的，
  删掉角色连原著设定都忘了。

## 6. 两个摘要提示词，别用混

| 提示词 | 用途 | 输出长什么样 |
|---|---|---|
| `SUMMARY_SYSTEM_PROMPT` | 角色简介（加书/合并时） | 「陈雯雯是路明非高中时暗恋的女孩…」 |
| `MEMORY_SYSTEM_PROMPT` | 对话蒸馏（清空对话时） | 「· 用户最近分手，情绪低落」 |

**踩过的坑**：对话蒸馏复用角色简介 prompt，会写成
「陈雯雯在对方分手难受时劝其先吃东西…」——把对话复述一遍，对“了解用户”没用。
换用户画像 prompt 后输出：`· 用户最近分手，情绪低落 / · 用户最怕晚上一个人待着`。

## 7. 数据快照（2026-11-14，仅作历史参考）

| 项 | 数值 | 说明 |
|---|---|---|
| `character_memory_items` | 260 | excerpt 234 / summary 25 / **fact 1** |
| `memory_chunks` | 1733 | 61 个角色有向量，人均 28 片 |
| `chat_turns` | 68 | 已蒸馏 0（当时阈值 40 从未触发） |
| `manager_memories` | 732 | 全部 `same_person` |
| `agno_memories` | 5 | 对比 88 会话 / 165 运行 |

场景分布极偏：战斗 221 / 260。抽取提示词偏爱戏剧性片段，是已知偏差。

## 8. 已知问题（截至 2026-11-23）

1. `fact` 类记忆仍然少：蒸馏只在用户主动清空对话时发生；
2. 原著片段场景 85% 是「战斗」，`SCENE_BOOST` 在闲聊时区分度低；
3. Agno agentic memory 几乎不写入，和自建层并行冗余；
4. 没有“事实冲突检测”：模型蒸馏错一次，这条就错着进 RAG；
5. `chat_memory.cleanup_window` 定义了但没有调用点（死代码）；
6. 长期记忆条数没有全局上限，cold memory 可能永远排不进 top_k。

## 9. RAG、幻觉与注意力

### 走不走 RAG

| 记忆 | 调取方式 | 走 RAG？ |
|---|---|---|
| 工作记忆 | 当前对话直接读 | ❌ |
| 聊天历史 | conversation_id + 时间倒序 | ❌ |
| 长期记忆 | 向量 0.65 + BM25 0.35 + 场景加权 | ✅ |
| 会话记忆 | Agno 最近 8 轮 | ❌ |
| 管理经验 | shelf_id + kind 精确查 | ❌ |

### 记忆太长 / 上下文过长，会不会幻觉

**会，但被几道闸门压住：**

1. **检索进错记忆**：top_k=4、min_score=0.18、场景加权、块级向量；
   宁可不带记忆，也不塞不相关片段。
2. **摘要滚雪球**：摘要只做派生视图，原文保留；删书按来源回收后重建；
   摘要封顶 1200 字。
3. **长上下文稀释**：提示词有硬预算——excerpt 3 条、说话样本 700 字、
   摘要 1200 字、RAG 4 条、Agno 8 轮、蒸馏输入 6000 字。
4. **跨角色串味**：记忆按 `character_id` 存；合并时并过去；删书精确回收。
5. **模型编造**：RAG 块是原文/蒸馏原文，提示词要求“自然融入，不要照念”；
   回复再过 `verify`（安全兜底/去说教）。

**仍存在的风险**：`fact` 是模型蒸馏的，错一次就错着进库；无冲突检测；
记忆无全局上限；极长上下文仍会 lost-in-the-middle。

### 注意力机制怎么工作（和记忆的关系）

```
Attention(Q, K, V) = softmax(Q·Kᵀ / √d) · V
```

- 当前 token 生成 query，与上下文每个 token 的 key 点积、softmax 归一化，
  再对 value 加权求和——**每个上下文 token 都在竞争注意力预算**。
- softmax 归一化后，token 越多单个平均权重越小；位置靠后、字面重复、
  语义相近的内容更容易拿高权重（近因/重复偏置）。
- 所以“记忆太长”不是存不下，而是**有效注意力被摊薄**：关键约束淹没在
  无关片段里，模型开始忽略中间部分（lost-in-the-middle）。
- 工程对策就是第 9 节的预算：先检索、再裁剪、只放少量高信号文本；
  长文档走“分块 + 摘要 + 按需检索”，不整段塞进上下文。

> 一句话：记忆层负责“把对的少量信息找出来”，注意力负责“在这些信息里分配权重”；
> 前者做不好，后者再强也会被稀释。

---

相关文档：[`docs/project-status.md`](project-status.md)、[`docs/langgraph-workflow.md`](langgraph-workflow.md)、[`docs/tool-layer.md`](tool-layer.md)