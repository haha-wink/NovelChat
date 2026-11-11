# LangGraph 在项目里的作用层

> 代码：`backend/app/agent/graph.py`（482 行）、`agent/group_graph.py`（414 行）
> 框架版本：langgraph 1.2.20 ｜ 扫描方式：AST 解析源码结构 + 人工核对调用点

---

## 1. 一句话定位

**LangGraph 只占一层：`agent/` 的编排层。它不碰业务逻辑，也不碰模型调用。**

```
┌──────────────────────────────────────────────────────────┐
│ api/          HTTP / SSE 出入口                          │
├──────────────────────────────────────────────────────────┤
│ agent/  ┌──────────────────────────────────────────────┐ │
│         │  ← LangGraph 在这里，且只在这里              │ │
│         │  graph.py        单聊状态图                   │ │
│         │  group_graph.py  群聊状态图                   │ │
│         └──────────────────────────────────────────────┘ │
│         manager.py  Agno Agent 构建 + 缓存                │
│         group.py    run_turn 壳（转发图的事件）           │
├──────────────────────────────────────────────────────────┤
│ services/     业务：对话判断 / 记忆 / 情绪 / 提示词        │
├──────────────────────────────────────────────────────────┤
│ core/         SQLite / 配置 / 签名                        │
└──────────────────────────────────────────────────────────┘
```

**边界很清楚**：把 `agent/graph.py` 和 `group_graph.py` 删掉，`services/` 和 `core/` 一行都不用改——
业务逻辑从来没依赖过框架。

---

## 2. 框架在项目里具体做了什么

| LangGraph 能力 | 用在哪 | 换手写难不难 |
|---|---|---|
| `StateGraph` 定义节点 | 两张图 | 容易（就是函数调用链） |
| `add_edge` 顺序边 | 单聊 7 条 | 容易 |
| `add_conditional_edges` 分支 | 群聊 3 个决策点 | 中等 |
| `Send` 并行扇出 | 群聊多人同时生成 | **难**（并发 + 合并 + 批结束判定） |
| `Annotated` reducer 状态合并 | 群聊 4 个字段 | **难**（并行写入的合并语义） |
| `checkpointer` 状态快照 | 单聊断点续跑 | **难**（每步快照 + 恢复） |
| `get_stream_writer` 自定义流 | 两张图的 delta | 中等（要自己维护队列） |
| `astream(stream_mode=[...])` | 端点多流消费 | 中等 |
| `recursion_limit` 循环保护 | 群聊回边 | 容易 |

**结论：真正离不开框架的是 `Send` + reducer + checkpointer 这三样**，其余都能手写。

---

## 3. 单聊图

```
                        ┌─────────────────────────────┐
POST /api/chat ────────►│  api/v1/endpoints/chat.py    │
                        │  · 校验 token / 角色 / 图片   │
                        │  · 图片落盘 → image_urls     │
                        │  · 组装 initial TurnState    │
                        │  · 注入 llm=run_agent        │
                        └──────────────┬──────────────┘
                                       │ astream(initial, config)
        ┌──────────────────────────────▼──────────────────────────────┐
        │                      agent/graph.py                         │
        │                                                             │
        │   START                                                     │
        │     │                                                       │
        │     ▼                                                       │
        │   ┌────────────┐  dialogue.preprocess                       │
        │   │ preprocess │                                             │
        │   └─────┬──────┘                                             │
        │         ▼                                                    │
        │   ┌────────────┐  dialogue.classify_with_judge              │
        │   │ classify   │  └─ 歧义 → LLM 判官（config 里的 key）      │
        │   └─────┬──────┘                                             │
        │         ▼                                                    │
        │   ┌────────────┐  affect.observe_user_message → SQLite      │
        │   │ observe    │  （生成前推进情绪）                         │
        │   └─────┬──────┘                                             │
        │         ▼                                                    │
        │   ┌────────────┐  rag.recall_block                          │
        │   │ recall     │                                             │
        │   └─────┬──────┘                                             │
        │         ▼                                                    │
        │   ┌────────────┐  dialogue.plan + affect.prompt_block       │
        │   │ plan       │  → context_block（SOP 在这拼）              │
        │   └─────┬──────┘                                             │
        │         ▼                                                    │
        │   ┌────────────┐  llm(state)  ← config 注入，逐段 yield      │
        │   │ generate   │  writer({type:'delta'})                     │
        │   └─────┬──────┘                                             │
        │         ▼                                                    │
        │   ┌────────────┐  reply.strip_stage_directions              │
        │   │ verify     │  + dialogue.verify（安全/去说教/危机补充）   │
        │   └─────┬──────┘  writer({type:'final'}) 若有改动            │
        │         ▼                                                    │
        │   ┌────────────┐  add_chat_turn ×2 + maybe_distill          │
        │   │ persist    │  → chat_turns → 蒸馏 → RAG                 │
        │   └─────┬──────┘                                             │
        │         ▼                                                    │
        │        END                                                   │
        └──────────────────────────────┬──────────────────────────────┘
                                       │
                    stream_mode=['custom','values']
                                       │
        ┌──────────────────────────────▼──────────────────────────────┐
        │ custom → 直接转 SSE（delta / final）                         │
        │ values → 最后一份快照（signals / affect_state / notes）      │
        │ 收尾：贴纸决策 + done 事件                                   │
        └─────────────────────────────────────────────────────────────┘
```

**状态契约**：`TurnState` 21 个字段，分三块——

| 块 | 字段 | 谁写 |
|---|---|---|
| 输入 | `user_id` `character` `raw_text` `image_urls` `api_key` `session_id` … | 端点 |
| 中间 | `text` `signals` `plan` `affect_state` `context_block` `reply` `notes` | 节点 |
| 出口 | `reply` `notes` | 最后两个节点 |

**注意**：单聊图**没有条件边**，是纯线性。它用到的框架特性只有「节点 + 顺序边 + 流 + checkpointer」。

---

## 4. 群聊图

```
   START
     │
     ▼
 ┌─────────┐  成员加载 / @解析 / @全体唤醒 / 用户那句落库
 │ prepare │  emit: user
 └────┬────┘
      │ 条件边 route_after_prepare
      ├──────────────(空书架)──────────────┐
      ▼                                     │
 ┌─────────┐  每轮选人（group.select_speakers）│
 │  pick   │  emit: round_start            │
 └────┬────┘                                │
      │ 条件边 fan_out                      │
      │                                     │
      ▼  Send × N（并行！）                  │
 ┌─────────┐ ┌─────────┐ ┌─────────┐        │
 │  speak  │ │  speak  │ │  speak  │  ...   │
 │ 沈砚    │ │ 苏晚    │ │ 老唐    │        │
 └────┬────┘ └────┬────┘ └────┬────┘        │
      │           │           │             │
      └───────────┴───────────┘             │
                  │ 所有分支合并               │
                  ▼                          │
             ┌─────────┐                    │
             │ decide  │  等这一批跑完        │
             └────┬────┘                    │
                  │ 条件边 route_after_speak  │
      ┌───────────┼───────────┐             │
      ▼           ▼           ▼             │
   (有人@别人)  (已说完)    (到轮次上限)       │
      │           │           │             │
      └─► pick    └─► finish ◄┴─────────────┘
                        │
                        ▼
                       END
```

**并行那一层的细节**（这是框架真正干活的地方）：

```
                pick
                  │
     fan_out(state) → [Send('speak', {...current: 沈砚}),
                       Send('speak', {...current: 苏晚})]
                  │
        ┌─────────┴─────────┐
        │  同一个 superstep  │
        │  ┌───────┐ ┌───────┐
        │  │speak  │ │speak  │   ← 两个 asyncio 任务同时跑
        │  └───┬───┘ └───┬───┘
        │      │ 合并     │
        │      ▼          ▼
        │  transcript ← operator.add   （两条都追加）
        │  spoken     ← operator.add
        │  pending    ← operator.add
        │  last_speaker ← 取最后非空
        └─────────┬─────────┘
                  ▼
               decide
```

| 状态字段 | reducer | 为什么 |
|---|---|---|
| `transcript` | `operator.add` | 每个人追加自己那条 |
| `spoken` | `operator.add` | 同上 |
| `pending_forced` | `operator.add` | 各自 @ 的人合并 |
| `last_speaker_id` | `_keep_last` | 并行时“谁最后说”无确定答案 |

---

## 5. checkpointer：断点续跑怎么走

```
┌─────────────────────────────────────────────────────────────────┐
│ main.py lifespan                                                │
│   await init_checkpointer()                                     │
│     → AsyncSqliteSaver.from_conn_string(                        │
│         data/workspace/graph_checkpoints.db)                    │
│     → build_turn_graph(saver)                                   │
│     → prune_checkpoints(7天)                                    │
│   ……yield……                                                      │
│   await close_checkpointer()                                    │
└─────────────────────────────────────────────────────────────────┘

一次请求：
   thread_id = req.turn_id or uuid4
   SSE 首帧: {"type":"turn","thread_id":...}   ← 前端记下来
        │
        ▼
   ┌────────────────────────────────────────┐
   │ 每个节点跑完 → checkpointer 存一份快照   │
   └────────────────┬───────────────────────┘
                    │
        ┌───────────┴────────────┐
        │ 正常跑完                │ 客户端断开 → CancelledError
        ▼                        ▼
      next = ()             next = ('generate',)    ← 停在生成节点
   快照没人用了                 │
                               │ 客户端带同一 turn_id + resume=true
                               ▼
                    payload = None（不是 initial）
                               │
                               ▼
                    astream(None, config) → 从 generate 重跑
```

**关键约束**：`llm` / `summarizer` 是函数，**不能进 state**（checkpointer 只序列化 state）。
所以它们走 `config["configurable"]` —— 顺带好处是续跑时能换一个生成函数。

---

## 6. 一轮对话里，框架 vs 自己的分工

```
┌──────────────────────────┬──────────────────────────────────────┐
│ LangGraph 负责            │ 项目自己负责                          │
├──────────────────────────┼──────────────────────────────────────┤
│ 节点调度顺序              │ 每个节点内部做什么                    │
│ 并行扇出与结果合并         │ 谁该说话（select_speakers）           │
│ 状态快照与恢复            │ 状态里存什么                          │
│ 自定义流的分发            │ 事件长什么样（delta/final/sticker）   │
│ 循环与递归上限            │ 循环的终止条件（轮次/阈值）            │
│ —                        │ 模型调用（Agno）                      │
│ —                        │ 提示词、记忆、情绪、SOP、校验          │
└──────────────────────────┴──────────────────────────────────────┘
```

**比例大致是 2 : 8** —— 框架管“怎么串”，业务管“串什么”。

---

## 7. 框架没有用到的部分

| 能力 | 为什么没用 |
|---|---|
| `interrupt` / `Command` | 没有需要人工确认的环节（危机处理要即时，不能卡住） |
| `store` 跨会话记忆 | 已有自建的 `agent_memory` + `memory_chunks` + RAG |
| 子图（`add_node(compiled_graph)`） | 群聊每个发言人走的是同一套函数，没必要套子图 |
| `checkpoint_ns` 命名空间 | 单线程图，用不上 |
| `langgraph.prebuilt`（ToolNode 等） | 没有工具调用需求 |

---

## 8. 一句话总结

> **LangGraph 在项目里是一层薄编排：两张图、12 个节点、5 个条件边。
> 它带来的是「并行合并」和「状态快照恢复」两件手写麻烦的事；
> 其余部分，业务代码与框架零耦合——删掉图，`services/` 和 `core/` 一行不用改。**