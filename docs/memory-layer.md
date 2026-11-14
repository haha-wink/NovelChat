# 记忆层工作流

> 本文基于真实数据（2026-11-14）和代码定位写成，每个数字都能复现。
> 一句话结论：**记忆层有四个存储、五条写路径，但真正"记住你和角色之间发生过什么"的那条路几乎没跑起来。**

## 1. 全景

```
                       ┌──────────────────────────┐
   写（5 条）           │      四个存储             │      读（3 条）
                       │                          │
① 加书进书架            │  character_memory_items  │   ──► ③ 静态注入（每轮相同）
   shelf.py:93         │   260 条                 │       excerpt ×3 + summary ×1
   └─ remember()       │   excerpt 234            │       agent/manager.py:49
        │              │   summary  25            │
        ▼              │   fact      1  ← 关键   │
② 对话蒸馏              │                          │
   chat_memory:167     │  memory_chunks           │   ──► ④ 按需检索（每轮不同）
   └─ remember()       │   1733 片 / 61 角色      │       top_k=4（唤起记忆时 8）
        │              │  （512 维向量 + BM25）   │       agent/graph.py:183
        ▼              │                          │
③ 角色合并/摘要         │  manager_memories        │   ──► ⑤ 书架级（不进提示词，
   merge.py:369 等     │   732 条（同人/别名）    │       仅 UI 与合并去重用）
   └─ summarize()      │                          │
                       │  agno_memories           │   ──► ⑥ Agno 自带记忆注入
④ 向量索引             │   5 行                   │       agent/manager.py:106
   agent_memory:96     │  （88 会话 / 165 运行）  │
   └─ index_character()│                          │
                       └──────────────────────────┘
```

## 2. 写路径

### ① 加书进书架 → 原著片段（excerpt）

```
services/shelf.py:93  agent_memory.remember(...)
        ↓
services/agent_memory.py:78   db.add_memory_items(kind="excerpt", scene=...)
services/agent_memory.py:96   rag.index_character(character_id)
        ↓
memory_chunks  ← 220 字一片、40 字重叠（rag.py:49-50），512 维
```

这是**记忆层的主体**：234 条 excerpt，全部来自小说原文。

### ② 清空对话 → 事实记忆（fact）

**触发方式变了**：不再按固定条数（原阈值 40）自动蒸馏，改为用户点「清空对话」时蒸馏。

```
DELETE /api/shelves/{id}/history?mode=distill      （群聊）
DELETE /api/characters/{id}/history?mode=distill   （单聊）
        ↓
services/chat_memory.py  clear_conversation(mode="distill")
        ↓
① 全文压成一段摘要（LLM 调用一次，失败回退本地压缩）
② 写给**所有说过话的角色**（一次摘要，N 次写入，不再调模型）
   agent_memory.remember(kind="fact", scene="对话", importance=0.8)
③ rag.index_character(...)   ← 立刻可被检索
④ db.delete_chat_turns(...)  ← 删流水
```

`mode=purge` 则不同：**不蒸馏**，直接删流水 + 删这段对话留下的 `fact` 记忆。

`maybe_distill`（旧的自动触发）保留但默认关闭：`chat_distill_threshold = 0`。

### ③ 角色合并 / 重建摘要 → summary

```
services/merge.py:369 / 602 / 743 / 794   agent_memory.summarize(character_id)
        ↓
summarizer.py  →  LLM 压成一段角色简介
services/agent_memory.py:239  kind="summary"
```

25 条 summary，内容是"夏弥是卡塞尔学院学生，真实身份是龙王…"——**角色简介，不是共同经历**。

### ④ 同人识别 → manager_memories

```
services/merge.py:260   db.list_manager_memories(shelf_id, limit=500)
core/database.py:1199   INSERT INTO manager_memories
```

732 条，形如「诺诺 与 陈墨瞳 是同一人」。**只在书架页展示和合并去重时用，不进任何提示词。**

### ⑤ Agno 自带记忆

```
agent/manager.py:86  MemoryManager(db=SqliteDb(agent_memory.db))
agent/manager.py:104 enable_agentic_memory=True
agent/manager.py:105 update_memory_on_run=True
agent/manager.py:106 add_memories_to_context=True
```

`backend/data/workspace/agent_memory.db`：88 个会话、165 次运行，但 `agno_memories` **只有 5 行**。
原因是"agentic memory"要模型自己决定调工具写记忆，实际几乎不发生。

## 3. 读路径

### ③ 静态注入（每轮一样）

```python
# agent/manager.py:49
memory = agent_memory.top(character_id, limit=settings.excerpt_chunks)  # 默认 3
if memory:
    profile["excerpts"] = memory          # 进 system prompt
profile["summary"] = agent_memory.summary(character_id)   # manager.py:57
```

这 3 条 excerpt + 1 条 summary 是**固定**的，和用户这句话无关。

### ④ 按需检索（每轮不同）

```python
# agent/graph.py:183
block = rag.recall_block(character_id, text, top_k=settings.rag_top_k)   # 4
# 意图分析器判定 needs_memory 时翻倍 -> 8（graph.py:180 附近）
```

检索打分（`rag.py:445-484`）：

```
score = 0.65 × 向量余弦 + 0.35 × BM25
score ×= 1.30（场景命中）/ 0.85（场景不符）
score ×= 1.05（如果是 summary）
score < 0.18 丢弃          ← MIN_SCORE
取前 top_k 条
```

### ⑤⑥ 其余两条不参与对话

- `manager_memories`：`api/v1/endpoints/shelves.py:119,233` 只读给 UI。
- `agno_memories`：5 行，注入量可以忽略。

## 4. 体检数据（2026-11-14）

| 项 | 数值 | 说明 |
|---|---|---|
| `character_memory_items` | 260 | excerpt 234 / summary 25 / **fact 1** |
| `memory_chunks` | 1733 | 61 个角色有向量，人均 28 片（最多 60） |
| 有记忆条目的角色 | 48 | 全书 55 个角色，7 个没有任何记忆 |
| `chat_turns` | 68 | 其中书架 `shelf_c228…` 65 条 |
| **已蒸馏** | **0** | 65 > 阈值 40，但一条都没蒸馏 |
| `manager_memories` | 732 | 全部是 `same_person` 类 |
| `agno_memories` | 5 | 对比 88 个会话、165 次运行 |

场景分布（`character_memory_items.scene`）：

```
战斗 221 ████████████████████████████████
告白   9 █
校园   8 █
离别   7 █
雨夜   6 █
争执   6 █
酒馆   1
日常   1
```

**221/260 是「战斗」**，因为抽取提示词偏爱戏剧性片段。

## 5. 问题清单

### ~~P0-1 群聊永不蒸馏~~（已改设计）

原实现里 `maybe_distill` 只在单聊 persist 节点调用，群聊图一次都没有，
65 条流水超过阈值 40 也从不蒸馏。

**现在的做法**：取消固定阈值（`chat_distill_threshold = 0`），
蒸馏改由用户显式触发：

| 按钮 | 行为 |
|---|---|
| 清空对话 | 先蒸馏成长期记忆（写给所有说过话的角色），再删流水 |
| 清空对话和记忆 | 不蒸馏，直接删流水 + 删这段对话的 `fact` 记忆 |

群聊和单聊都走同一个 `clear_conversation()`，所以群聊终于也会积累对话记忆了。

### P0-2 「对话记忆」只有 1 条

`fact` 类型全库仅 1 条。也就是说：**用户和角色聊了几十轮，能被称为"记住"的只有一条**。

### P1-1 记忆主体是原著，不是关系

234 条 excerpt 全部来自小说原文，且 85% 是「战斗」场景。
用户真正想要的是"记得我上次说分手了"这类**关系记忆**——这类只可能来自蒸馏，而蒸馏没跑。

### P1-2 静态注入与对话无关

`excerpt_chunks=3` 的摘录和 `summary` 每轮都塞进 system prompt，
不管用户在说什么。既占 token，又可能和当前话题冲突。

### P2-1 场景加权会误伤

`SCENE_BOOST=1.30` 依赖两侧场景标签，但标签 85% 是「战斗」，
导致日常对话检索时几乎所有片段都落在 `SCENE_PENALTY=0.85` 一侧，加权失去区分度。

### P2-2 Agno 记忆形同虚设

`enable_agentic_memory=True` 需要模型主动调工具才能写入，实际 5 行。
这套机制和自建的四层记忆**并行存在、互不同步**，维护成本白付。

### P3 死代码与只读存储

- `chat_memory.cleanup_window` 定义了但**没有任何调用点**。
- `manager_memories` 732 条只被 UI 和合并逻辑读取，**不参与回复**。

## 6. 建议（按性价比）

| 优先级 | 动作 | 改动量 |
|---|---|---|
| P0 | 群聊 `decide_node` 后接 `maybe_distill`（每轮检查，超阈值才跑） | 小 |
| P0 | 蒸馏阈值从 40 降到 12~16，让对话记忆真的长出来 | 一行配置 |
| P1 | 蒸馏产物加 `kind="fact"`、`scene="对话"` 的**关系型**摘要（"用户说他分手了"） | 中 |
| P1 | 静态 excerpt 从 3 条降到 1~2 条，把预算让给 `recall_block` | 一行配置 |
| P2 | 场景标签改成"战斗/日常/情感/离别"四类均衡抽样，或让 `SCENE_BOOST` 只在小样本时生效 | 中 |
| P3 | 删 `cleanup_window` 或接进 housekeeping；`manager_memories` 决定"注入还是删表" | 小 |

---

相关文档：[`docs/langgraph-workflow.md`](langgraph-workflow.md)、[`docs/tool-layer.md`](tool-layer.md)、[`docs/project-layers.md`](project-layers.md)

---

## 7. 清空对话的两条路径（2026-11 改）

```
用户点「清空对话」
   ├─ mode=distill（默认）
   │    对话 → 摘要（1 次 LLM）→ 写进每个发言角色的 fact 记忆
   │         → 重建向量索引 → 删流水
   │    结果：界面干净，角色记得你
   │
   └─ 「清空对话和记忆」 mode=purge
        对话 → 直接删流水
             → 删这些角色身上这段对话蒸馏出的 fact
             → 重建向量索引
        结果：彻底重来，角色忘记这段时间
```

**为什么蒸馏只调一次模型**：群聊可能 4 个人说话，如果每人调一次摘要就是 4 倍成本。
现在是一段摘要、写给 N 个角色（`agent_memory.remember` 只写库，不调模型）。

**为什么 purge 只删 fact**：`excerpt`（原著片段）和 `summary`（角色简介）
不是这段对话产生的，删掉会让角色连原著设定都不记得了。
