# 记忆层：怎么分、放在哪、什么时候会糊涂

一句话：**只有「长期记忆」走向量检索（RAG）；工作记忆和短期流水都是按对话直读的。**
记忆不是一坨，而是按「谁在用、用在什么场景」分层的。

## 0. 三种场景

| 场景 | 成员 | 记忆层数 | 有哪些层 |
|---|---|---|---|
| 书架级 | 书架 / 管理员这个层级本身 | 1 层 | 管理经验（`manager_memories`） |
| 聊天模式 | 所有角色 Agent | 3 层 | 初始记忆 + 长期记忆 + 短期记忆（含派生状态） |
| 工作模式 | 所有角色 Agent + 管理员 Agent | 4 层 | 初始记忆 + 长期记忆 + 短期记忆（只有在线状态）+ 工作记忆 |

> 「派生状态」指情绪、好感度这类从对话里算出来、不是原文的记忆。聊天模式有，
> 工作模式只有「在线 / 离线」——干活的人不需要心情参与任务判断。

## 1. 分层总表

| 层 | 存哪 | 谁用 | 活多久 | 走 RAG？ |
|---|---|---|---|---|
| 初始记忆 | `character_memory_items`（`kind=excerpt/summary`，导入书时蒸馏） | 角色 | 角色在，记忆在 | ✅（和长期记忆同一套检索） |
| 长期记忆 | `character_memory_items` + `memory_chunks` | 角色 | 跨对话、跨清空 | ✅ |
| 短期流水 | `chat_turns`（原文，7 天后蒸馏） | 角色 / 管理员 | 7 天（`chat_retention_days`） | ❌ |
| Agno 会话记忆 | `agent_memory.db`（最近 20 轮） | 角色 | 跟随会话 | ❌ |
| 派生状态 | `character_states`（情绪/好感）+ 在线字段 | 角色（仅聊天模式） | 实时，逐轮回落 | ❌ |
| 工作记忆 | `working_memory`（按 `conversation_id` + `agent_id`） | 工作模式全体 | 当前对话 | ❌ |
| 管理经验 | `manager_memories`（按 `shelf_id`） | 管理员 | 书架在就在 | ❌ |

## 2. 三个场景细说

### 2.1 书架级：只有管理经验

书架这一层没有「人」的记忆，只有管理员攒下来的经验：谁和谁是同一个人（`same_person`）、
这个书架做过什么任务（`admin`）。它按 `shelf_id` 存，不参与角色 RAG。

**为什么单独一层**：管理经验是「书架的事实」，不是「角色的回忆」。混进角色记忆会被
角色当成自己的往事说出来。

### 2.2 聊天模式：3 层

```
初始记忆  角色从小说里蒸馏出来的身份/关系/关键剧情（导入书时写入）
长期记忆  fact / summary / dialogue，RAG 按场景+语义检索 top 4
短期记忆  ┌ 流水：chat_turns 原文，7 天后蒸馏成长期记忆
          ├ Agno：最近 20 轮对话（跨进程存在 agent_memory.db）
          └ 派生状态：情绪 valence/arousal、好感度、在线
```

读取顺序：角色档案（`build_character_prompt`，含初始记忆 top-k）→ RAG 召回（长期）
→ Agno 20 轮 → 当轮情绪块（`attach_state`）。工作记忆**不读**。

### 2.3 工作模式：4 层

```
初始记忆  同上（角色还是那个角色）
长期记忆  同上（RAG 照走，干活前先回忆相关经历）
短期记忆  流水 + Agno 20 轮 + 只保留在线状态（无情绪/好感）
工作记忆  当前对话的任务约束、管理员计划、派发、角色汇报
```

关键差异：

* **管理员是唯一对用户说话的人**：管理员接了这一轮就不再走 pick/speak，角色 Agent 的产出只写进工作记忆、由管理员汇总；避免"管理员总结 + 角色再总结一遍"的双套输出。

* **不做派生状态**：群聊里跳过 `affect.observe_user_message`；单聊/群聊的
  `attach_state(include_affect=False)` 只挂 RAG，不挂情绪/好感。
* **工作记忆按 agent 分栏**：`agent_id=''` 是用户要求和任务、`admin` 是管理员、
  `character:<id>` 是某个角色。同一对话里**管理员能读所有人的**（决策、校验、防冲突），
  角色只读自己那栏 + 管理员计划。

### 2.4 管理员 Agent 的记忆（工作模式专属）

管理员不是书架成员（不写 `characters`/`shelf_members`），所以**没有初始记忆**：

| 层 | 内容 | 位置 |
|---|---|---|
| 长期记忆 | 以前在这个书架做完的任务摘要（`kind='admin'`，单条 ≤400 字） | `manager_memories` |
| 短期上下文 | 本次 ReAct 循环里各角色的汇报 + 用户这句话 | 请求内 + `working_memory` |
| 工作记忆 | 目标 / 计划 / 派发 / 观察（`kind=plan/dispatch/report`） | `working_memory`，`agent_id='admin'` |

每一轮流程：

```
读：自己的计划 + 共享工作记忆（所有 agent）+ 书架管理经验
做：规划 → 派发 → 收汇报 → 评估 → 再规划（最多 3 轮）
写：plan/dispatch/report 进工作记忆；结束时整段摘要进 manager_memories
```

汇总只在 `prepare_node` 发一次（同一条 2 分钟内不重复落库/推送），防止断线重连时出现两条一模一样的管理员气泡。

## 3. 文件结构树（记忆层代码在哪）

```
backend/
  app/
    core/
      database.py                 # 所有表结构 + 迁移 + CRUD
      config.py                   # 窗口/预算：history_runs=20、rag_top_k=4、excerpt_chunks=3
    services/memory/
      agent_memory.py             # 长期记忆条目（写完调 chunk 重建向量）
      chat_memory.py              # 流水 → 长期记忆（7 天/清空时蒸馏）
      rag.py                      # 混合检索（0.65 向量 + 0.35 BM25，top4，min 0.18）
      summarizer.py               # 两个蒸馏 prompt
      working_memory.py           # 工作记忆（分栏：admin / character:<id> / 公共）
    agent/
      admin_agent.py              # 管理员：ReAct 规划 → 派发 → 观察 → 循环
      manager.py                  # 角色 Agent 构建 + Agno 会话记忆
      character.py                # attach_state：RAG + 派生状态（工作模式只挂 RAG）
      graph.py                    # 单聊工作流
      group_graph.py              # 群聊工作流；工作模式先跑管理员
    api/v1/endpoints/
      knowledge.py                # RAG 状态 / 重建 / 检索预览 / 蒸馏
      shelves.py                  # 对话 mode、清空对话（带工作记忆蒸馏）
      chat.py                     # 群聊/单聊入口，传 api_key 给管理员
  data/workspace/
    app.db                        # 业务库：working_memory / chat_turns / character_memory_items /
                                  #          manager_memories / conversations(mode) / character_states
    agent_memory.db               # Agno 会话记忆（最近 20 轮）
    graph_checkpoints.db          # LangGraph 断点
  data/models/bge-small-zh-v1.5/  # RAG 向量模型（512 维，本地）
```

## 4. 记忆怎么调取

| 场景 | 调什么 | 怎么调 |
|---|---|---|
| 聊天回复 | 初始 + 长期 | 角色档案里 `agent_memory.top()`（3 条）+ `rag.recall_block`（top 4） |
| 聊天上下文 | 短期 | Agno `add_history_to_context`（20 轮）+ `chat_turns` 给摘录用 |
| 工作模式 | 再加工作记忆 | `working_memory_text(conversation_id)`，管理员读全部、角色读自己 |
| 管理员 | 管理经验 | `list_manager_memories(shelf_id, kind='admin')`，取最近 6 条 |
| 清空/蒸馏 | 短期 → 长期 | 流水 + 工作记忆摘要拼成 transcript，压成 `fact` 再删流水 |

## 5. RAG 的边界

* **只有长期记忆进 RAG**（初始记忆也以长期记忆条目存放，所以一起被检索）。
* 混合分：`0.65 × 余弦 + 0.35 × BM25`；场景一致 ×1.30、不一致 ×0.85；摘要 ×1.05；
  低于 0.18 丢弃；默认 `top_k=4`。
* 工作记忆、管理经验、Agno 会话、派生状态**都不进 RAG**。任务笔记不会被当成
  「角色往事」在下次聊天里捞出来。
* 模型不可用时降级成纯词法（BM25），不会因为向量服务挂了就完全失忆。

## 6. 两种模式怎么隔离

* `conversations.mode = chat | work`，列表接口按 mode 过滤；前端切模式**换窗口**。
* 工作记忆只在 work 写；聊天模式写会直接被 `working_memory.remember` 丢掉。
* 两种模式的**蒸馏都会进长期记忆**：清空对话不区分模式，`working_memory.digest()`
  和流水一起压成一条 fact（工作记忆会带「任务/计划/派发」标签）。
* 工作模式不更新情绪/好感；切回聊天模式，情绪还是上次聊天时的值。

## 7. 上下文太长会不会幻觉？

会，但有闸门。当前预算：

| 来源 | 上限 |
|---|---|
| 初始/长期记忆 | 3 条摘录 + RAG 4 条（每条 ≤ ~220 字块） |
| 流水蒸馏输入 | 6000 字 |
| 摘要记忆 | 1200 字 |
| Agno 会话 | 20 轮 |
| 工作记忆 | 单条 2000 字 / 单对话 60 条；管理员读 40 条、拼 1800 字 |
| 管理经验 | 6 条 × 160 字 |

**风险点**：

1. `fact` 是模型蒸馏的——蒸馏错一次就错着进库，之后一直被 RAG 捞出来；
2. `top_k=4` 只保证「相关」，不保证「不矛盾」：两条记忆冲突时没有事实校验；
3. 记忆没有全局上限，书架用久了 RAG 候选会变多（目前靠 min_score 和 top_k 挡）；
4. 极长上下文仍有 lost-in-the-middle：关键约束放在中间段容易被忽略。

缓解：检索先于拼接、只放高信号文本、工作记忆单独成层不混入往事、管理经验与角色记忆
物理隔离、清空对话时把工作记忆显式蒸馏而不是让它消失。

## 8. 注意力机制为什么「越长越容易丢」

每层 self-attention：

```
Attention(Q, K, V) = softmax(QKᵀ / √d) · V
```

当前 token 的 Q 和上下文每个 token 的 K 做点积，softmax 把权重归一化——**预算是固定的**。
上下文从 1k 涨到 32k，平均每个 token 分到的注意力下降，模型更依赖位置、显式标记
（标题/编号/「注意」）和语义相似度：

* 开头（system prompt、角色档案）通常权重高；
* 结尾（用户刚说的话）权重高；
* 中间大段材料容易被稀释（lost-in-the-middle）；
* 语义相近的内容互相「抢」权重，重复信息反而挤掉关键细节。

所以工程上不是「把所有记忆塞进去」，而是：**先检索（RAG）→ 再裁剪（预算表）→ 只放
少量高信号文本**。记忆层分得越细，塞进上下文的噪声越少。

## 9. 已知边界（还没做）

1. 管理员的 ReAct 只有「动画」一个工具；角色 Agent 的 ReAct 目前是「想 → 调动画 / 汇报」
   两步，没有通用工具注册表（搜索、文档解析等以后接）。
2. 管理员是虚拟成员（不进 `characters`/`shelf_members`），所以成员列表里不占一个真实卡片。
3. 工作记忆的修剪是「超过 60 条删最老」，没有按任务分段/摘要。
4. 管理经验（`manager_memories`）没有向量检索，按 `kind` 取最近 N 条；书架很大时可能漏。
5. 没有事实冲突检测：长期记忆里两条矛盾的说法都会留着。