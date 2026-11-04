# 管理层 Agent 工作流

> 对应代码：`services/extraction_agent.py`、`services/extractor.py`、`services/library.py`、
> `services/shelf.py`、`services/merge.py`、`services/cleaner.py`、`services/chat_memory.py`、`services/agent_log.py`
>
> 设计意图见 `docs/agent-architecture.md` §7。

---

## 1. 先弄清"管理层 Agent"现在到底是什么

| | 管理 Agent | 角色 Agent |
|---|---|---|
| 作用域 | **一个书架** | 一个角色 |
| 记忆 | 书架级（设计上）／目前只有审计事件 | 角色记忆条目 + 对话蒸馏 |
| 形态 | **事件驱动的批处理**，不是常驻会话 | 常驻对象，每轮对话被唤醒 |
| 谁在跑 | 后台 asyncio task（上传后触发） | `agent_manager` LRU 缓存 |
| 是否用 LLM | **只有"审核名单"这一环**用 | 每轮都用 |

关键结论：**"管理 Agent" 是 1 个 LLM 自主循环（ReAct，≤8 步）+ 4 组确定性事务函数**。
其余职责（书架同步、合并、清洗）都是写死顺序的普通函数，不是 Agent 的自主决策。

---

## 2. 总览：4 条链

```
① 上传小说  POST /api/books
     └─ A 抽取链（后台任务，唯一用 LLM 自主循环）

② 书架加书  POST   /api/shelves/{sid}/books/{bid}
③ 书架减书  DELETE /api/shelves/{sid}/books/{bid}
     └─ B 书架事务链（同步成员 / 补强或回收记忆 / 合并 / 重算摘要）

④ 手动合并  POST /api/shelves/{sid}/merge
     └─ C 合并链（并查集分组 → 选本体 → 别名声望 → 记忆迁移）

⑤ 每轮聊天后
     └─ D 维护链（蒸馏阈值检查 → 建索引；手动接口：清洗记忆、重建索引）
```

---

## 3. 链 A：上传 → 抽取（重点）

### 3.1 阶段表

| 阶段 | 位置 | 做什么 | 用 LLM | 失败处理 | 进度 |
|---|---|---|---|---|---|
| A0 接收入口 | `api/routes.py` | 收文件、大小/字数校验 | — | 400/413 | — |
| A1 取文本+清洗 | `readers/document.py` → `cleaner.clean_text` | 解 pdf/txt/md、编码探测、去广告水印 | — | 400 | — |
| A2 落盘分块 | `library.create_book` | 原文 + chunks 写库，状态 `extracting` | — | — | 0 |
| A3 调度 | `library.schedule_extraction` | `asyncio.create_task`，**按 book_id 去重** | — | — | 2 |
| A4 算法挖掘 | `character_miner.mine_characters`（`to_thread`） | 对话归因、称呼提取、性格/说话风格统计 | — | — | **60** |
| A5 管理员 Agent 审核 | `extraction_agent.ExtractionAgent.run` | ReAct 循环修正名单（仅 `EXTRACTION_MODE=agent`） | ✅ 最多 8 次 | 单步失败→回退算法名单 | **68** |
| A6 模型校验补全 | `extractor.enrich_characters` | 分批补全档案、补漏（hybrid/agent） | ✅ ceil(N/10) 次 | **单批失败保留算法画像** | **72** |
| A7 统一校验 | `miner.validate_characters` | 名字必须逐字出现在原文；剔除职位/组织/泛称 | — | — | — |
| A8 收尾 | `library._run_extraction` | 挑原文片段当初始记忆 → 写角色 → 存头像候选 → `ready` | — | 记 error 给前端 | **95→100** |

> 进度百分比目前是**硬编码**的（2/60/68/72/95/100），散在 `library.py` 里。

### 3.2 A5 的 ReAct 循环（真正的"Agent 部分"）

```
初始化：先跑一遍算法（_run_miner），把候选表 + 丢弃样本塞进首条 user 消息
        ↓
  ┌─→ LLM 输出一个 JSON：{"thought": "...", "action": ..., "args": {...}}
  │      ├─ finish  → 确定性收口 → 结束
  │      └─ 工具    → 执行 → **结构化裁剪到 900 字（保证合法 JSON）** → 下一轮
  └──┘
约束：
· 最多 8 步（AGENT_MAX_STEPS）
· 最后一步**强制**注入"立刻 finish"
· 任何一步 LLM 抛错 / 8 步没 finish → 回退算法名单（fallback=True）
· 每次输出上限 400 token（动作只是个小 JSON，给 800 是浪费）
```

**6 个工具**（头像已移出决策循环，见 §3.4）：

| 工具 | 作用 | 外部成本 |
|---|---|---|
| `miner_report` | 算法统计：丢了多少、为什么丢（不再重复贴候选表） | 无 |
| `name_scan` | 用 NLP 库独立扫全文，给出「库认出但算法没挖到」的差集 | 无 |
| `name_check` | 批量判名字：中国人名/外国译名/称呼/不像人名（≤30 个） | 无 |
| `text_evidence` | 查某名字在原文的证据（提示词要求 ≤2 次） | 无 |
| `web_roster` | 联网查这本书的真实角色表（`AGENT_ENABLE_WEB`） | **HTTP + 抓网页** |
| `tune_miner` | 调算法参数后重跑 | 无（CPU，但重算全文） |
| `finish` | 交最终名单：`keep` / `drop` / `rename` | — |

**`_finalise()` 的四道闸**（模型说了不算）：

1. `keep` 里的名字必须**逐字出现在原文**，否则丢弃
2. `rename` 的目标名原文里没有 → 不合并
3. `drop` 里的名字进报告
4. 若模型给不出任何可用记录 → **整体回退算法名单**（`fallback=True`）

### 3.3 上下文是怎么省下来的

每一轮的消息由 `_compose()` **重新拼装**，而不是在上一步的数组上追加：

| 规则 | 效果 |
|---|---|
| 只有**最后一次**工具结果是全文，更早的折成一行摘要 | 6 步时省掉约七成上下文 |
| **不回显 `thought`**：模型的理由只在它那一轮用，写回历史只是重复计费 | 每轮省 20~40 字 |
| 观察先**结构化裁剪**（截数组、截字符串、加省略标记）再序列化 | 输出始终是**合法 JSON**，不会让下一步解析失败 |
| `max_tokens` 800 → 400 | 输出侧延迟减半 |
| 姓名库判定带缓存 | 候选表重建从 3ms 降到 0.1ms |

实测（同样的 5 步轨迹）：**累计输入字符 16,881 → 11,707，降 31%**；
单步观察 `name_scan` 1642→725 字、`name_check` 2610→741 字，且都是合法 JSON。

### 3.4 头像为什么移出了决策循环

原提示词让模型"对 1~2 个主要角色调用 `avatar_candidates`"——这等于**用头像搜索反向校验人名**，
既不可靠，又要多花一次模型调用。

现在：头像在 `finish` **之后**由 `_collect_avatars()` 按出场次数自动抓前 2 个（并发、8s 超时、失败即放弃）。
少一次 LLM 往返，头像覆盖不变；`avatar_candidates` 动作仍然保留（老脚本/测试可调），只是不再宣传。

---

## 4. 链 B：书架加/减书

### 4.1 加书 `shelf.add_book`

```
① 已在目标书架？ → 只补跑一次合并，返回
② db.set_book_shelf            改写归属
③ sync_members(原书架)          原书架的角色退群（有的话）
④ sync_members(新书架)          新角色进群，默认 online
⑤ supplement_memory(新书架)     用新书正文给**已有角色**补初始记忆
                                只补"在新书里真的出现过"的角色，每角色有上限
⑥ merge.merge_shelf             同系列重复角色合并成一个 Agent
```

顺序不能换：先同步成员再补记忆（补记忆要按成员找角色），最后合并（合并会改 id 归属）。

### 4.2 减书 `shelf.remove_book`

```
① db.release_merged_children(book_id)   把寄居在这本书角色身上的其它书角色解放出来
② library.delete_book(book_id)          删书 + 回收记忆条目/情绪/成员关系
③ merge.refresh_summaries(shelf_id)     重算剩余角色的记忆摘要
④ db.ensure_shelf_members(shelf_id)     成员列表对账
```

① 必须在 ② 之前：否则被合并的子角色会指向一个即将消失的本体。

---

## 5. 链 C：合并去重 `merge.merge_shelf`
### 5.1 触发时机（这里曾经是个断链）

**抽取完成 = 这本书真正进了书架**。`library._sync_shelf_after_extraction()` 会补跑书架事务：
同步成员 → 给已有角色补记忆 → 合并。

以前只有「手动把书放进书架」的接口会跑这一套，而**上传时直接带 shelf_id** 的路径从不经过它，
所以新书的同名角色一直没被合并——用户的 42 个成员里躺着 7 组重名。

### 5.2 三个阶段（可信度从高到低）

| 阶段 | 依据 | 处理 |
|---|---|---|
| ① 名字 / 别名 | 主名或别名相同 | **直接合并** |
| ② 管理 Agent 记忆 | 以前确认过的身份关系 | **直接合并**，不再判断 |
| ③a 关系网 + 性格高度相似 | `similarity()` **> 0.7** | **直接合并**（三个信号里至少两个高度吻合） |
| ③b 有点相似 | `0.5 ≤ similarity ≤ 0.7` | 只当**候选**：有判定器就联网问模型，判 true 才合并；否则等人工确认 |

相似度 = 关系网 0.30 + 性格 0.45 + 身份描述 0.25 的加权；至少要两个信号才算数，
**同一本书里的两个人永远不参与**（抽取阶段已经去过重）。

### 5.3 判定与记忆

- 判定器 `merge.llm_verifier()`：把两个角色的档案（别名 / 身份 / 性格 / 关系网 / 出处）
  发给模型，要求只回 `{"same": bool, "confidence": 0~1, "reason": "..."}`；
  **置信度 < 0.7 一律当"不确定"**，不猜。
- 判定结果沉淀进 `manager_memories`：

| kind | 含义 | 下次行为 |
|---|---|---|
| `same_person` | 确认是同一人 | 直接合并，不再问 |
| `different_person` | 确认不是同一人 | 直接跳过，不再问 |

- 没有判定器时，候选进 `pending`，等用户手动合并——**绝不自动猜**。
- 手动合并（`merge_pair`）同样会写一条 `same_person` 经验。
- 经验可查可删：`GET /api/shelves/{id}/memories`、`DELETE /api/memories/{id}`。

### 5.4 合并动作

```
1. transfer_memory_items   子角色的记忆条目转到本体
2. set_canonical           子角色打上 canonical_id（不再作为独立成员）
3. _merge_aliases          把子角色的名字并进本体别名
4. agent_memory.summarize  重算本体摘要
5. 写 character_merged 事件（带 source: name / similarity / manual）
```

撤销：`unmerge(child)` 把角色还原，并按原始归属取回属于它自己那本书的记忆。

---

## 6. 链 D：日常维护

| 触发 | 动作 | 位置 |
|---|---|---|
| 每轮群聊/单聊后 | `chat_memory.maybe_distill`：未蒸馏条数 > 阈值就压一半 | `routes.py:1414`、`group_chat` |
| 蒸馏后 | `rag.index_character` 重建该角色的向量 | `chat_memory.py` |
| 启动 | 后台线程预热 RAG 模型 + 情绪模型 | `main.py` |
| 手动 | `POST /api/maintenance/clean-memories`（清洗存量记忆） | `routes.py:159` |
| 手动 | `POST /api/rag/rebuild`（重建全部索引） | `routes.py` |

---

## 7. 审计：管理层说过的话都在哪

`agent_events` 表（`agent_log`），全部带 `trace_id` 可串成一条链：

```
extraction_started → decision×6 → extraction_finished / extraction_failed / extraction_cancelled
shelf_book_added / shelf_book_removed
character_merged / character_merged_manual
chat_distilled
```

`decision()` 目前**只在 `library.py` 用了 6 处**；`shelf.py`、`merge.py` 用的是普通 `agent_log.log`，
所以"为什么这几个人被合并了"在界面上只能看到事件、看不到理由。

---

## 8. 成本模型（一本书，`EXTRACTION_MODE=agent`）

| 环节 | 调用次数 | 说明 |
|---|---|---|
| 算法挖掘 | 0 | 纯 CPU |
| Agent 审核 | 1 ~ 8 | 通常 2~4；看模型是否早 finish |
| 补全 batches | ⌈N / 10⌉ | N = 角色数，上限 `max_characters=20` → 最多 2 |
| **合计** | **约 3 ~ 10 次** | 另加可选的 web 查询、头像抓取 |

`hybrid` 模式省掉 Agent 那 1~8 次；`algo` 完全不花钱（但档案粗糙）。

---

## 9. 与设计文档 §7 的差距

| 设计里的职责 | 现状 | 差距 |
|---|---|---|
| 书架同步（最高优先级） | ✅ `shelf.add_book` / `remove_book` | 是写死顺序的函数，**不是 Agent 决策**——够用，但设计描述超前了 |
| 抽取与校验 | ✅ ReAct + `validate_characters` | 已实现 |
| 头像设计（风格画像 → 打分取舍） | ⚠️ 只到"抓候选" | 没有风格画像、没有打分取舍；且 `avatar_candidates` 工具默认禁用 |
| **经验记忆（书架级）** | ✅ **已实现“身份关系”这一种** | `manager_memories` 表 + `GET /api/shelves/{id}/memories`；同人/不同人的判定会沉淀，下次直接复用。抽取套路类经验仍未做 |
| 全量日志 | ✅ | 事件全，但 `decision` 覆盖不全（见 §7） |

**经验记忆现在补上了「身份关系」这一层**（谁和谁是同一个人）。
抽取套路类（比如“这本书该用高阈值”）仍未沉淀。

---

## 10. 优化清单（不改功能，只改质量）

> **P0 三条已完成（P12）**：①启动回收卡死书 ②补全/挑片段失败兜底 ③重抽保留角色身份。
> 顺带修了「两个 warmup 线程并发导入 transformers 导致 RAG 静默降级」——见 §12。

### P0 · 可靠性（真 bug）

**① 重启后抽取任务永久卡死**

`library._tasks` 是**进程内 dict**。服务在抽取中途重启 → dict 清空，但 DB 里
`books.status` 还停在 `extracting`，前端会一直轮询一个永远不动的进度条。

- 没有启动回收（`main.lifespan` 只清了上传临时目录）
- 修复：启动时把 `status='extracting'` 的书改成 `failed`（理由"服务重启"，可点重新抽取）
- 影响：一次性 20 行，消除一类"卡住必须删书"的用户投诉

**② 收尾段异常会让已挖到的角色全丢**

`library.py:194-220`（补全段）没有 try。补全内部**每批**都兜住了，但
`miner.pick_reference_excerpts` / `db.update_book` 这类非 LLM 异常仍会冒到
`_run_extraction` 的 except → 整本 `failed`，而算法名单其实可用。

- 修复：补全段整体包 try，失败就跳过补全（`used_llm=False`）

**③ 重抽会重建角色 id，用户改动丢失**

`db.replace_novel_characters` 全量替换 → 头像选择、手工编辑、群内状态都没了。
这也是"别重抽真实书"的根因。

- 修复：按名字匹配，已有角色**保留 id**，只更新抽取出来的字段
- 收益：重抽从"破坏性操作"变成"刷新操作"

### P1 · 成本

**④ Agent 步数固定 8，允许空转**

系统提示已经要求"看完候选表就 finish"，但没有任何硬约束。候选干净时完全可以直接跳过
Agent 那 1~8 次调用。

- 做法：`miner` 诊断干净（dropped 少、台词归属率高）+ 候选数 < 阈值 → 跳过 A5
- 收益：常见小说省 2~4 次调用

**⑤ `web_roster` 默认开**

一次联网查询要 HTTP + 抓正文，慢且可能超时；而它只影响"可能漏了谁"。
建议默认关，让用户在需要时手动开。

### P2 · 可观测与结构

**⑥ `decision` 覆盖不全** —— `shelf`/`merge` 补上理由字段，前端"为什么合并"才能讲清。

**⑦ 进度百分比硬编码** —— 收敛成常量表，避免改流程时忘了改进度。

**⑧ 管理层没有统一入口** —— 能力散在 6 个文件，`routes.py` 直接调 `shelf` / `merge` /
`cleaner` / `chat_memory`。建议加一层薄 facade（`services/manager.py`）收口，
路由只 import 一个模块。**纯结构调整，不改行为。**

---

## 11. 一页速查

```
上传 → 清洗 → 分块 → 算法挖人（免费）→ [Agent 审核 8 步] → [模型补全 2 批]
     → 逐字校验 → 挑原文片段 → 写角色 → ready

加书 → 改归属 → 原书架退群 → 新书架进群 → 给已有角色补记忆 → 合并重复角色
减书 → 解放被合并的 → 删书（连带回收）→ 重算摘要 → 成员对账

合并 → 并查集分组 → 出场最多者当本体 → 别名并入 → 记忆迁移
维护 → 蒸馏检查 → 建索引
```

---

## 12. P0 修复记录（P12）

### 12.1 重启后抽取任务卡死

**症状**：抽到一半重启服务，进度条永远停在 40%，点「重新抽取」也没用，只能删书重传。

**原因**：抽取任务跑在进程内（`library._tasks` 是 dict），进程没了任务就没了，
但 `books.status` 还停在 `extracting`。前端每 1.5s 轮询一次，永远等不到变化。

**修复**：`db.recover_interrupted_extractions()`，启动时把这些书标成 `failed` 并写清原因
（记一条 `extraction_failed` 审计事件），用户点一下就重抽。

### 12.2 补全失败会让整本白抽

**症状**：算法已经挖出 18 个角色，模型补全环节一报错，整本书直接 `failed`。

**原因**：`extractor.enrich_characters` 内部对每批都兜住了，但外层没有——
`pick_reference_excerpts`、`db.update_book` 这类非 LLM 异常会一直冒到 `_run_extraction`。

**修复**：补全段整体包 try，失败就保留算法画像继续；`attach_excerpts` 同样处理。
诊断里记 `enrich_error` / `excerpt_error`，出问题能查。

### 12.3 重抽不再是破坏性操作

**症状**：点一次「重新抽取」，角色的头像被打回 🎭、群成员关系没了、情绪归零、
从对话里学到的东西全忘——因为旧实现是"先全删再重建"，角色 id 全变。

**修复**：`replace_novel_characters` 改成**按名字原地更新**：

| 情况 | 行为 |
|---|---|
| 同名角色 | 更新抽取字段，**id 不变**；头像 / 化身 / prompt_override 保留 |
| 新名字 | 插入 |
| 消失的角色 | 删除，并回收成员关系、情绪、记忆、向量 |
| 每个角色的本书原文片段 | 重算（`kind='excerpt'` 且 `source_book_id` 是本本）；**对话记忆与别处片段保留** |

重抽后还会重建这些角色的 RAG 索引，避免向量指向已经被换掉的片段。

### 12.4 顺带修掉：RAG 会静默降级成词法检索

**症状**：`/api/health` 里 `rag.backend = lexical`、错误信息是
`cannot import name 'AutoModel' from 'transformers'`，但日志里明明写着"向量后端就绪"。

**原因**：`transformers` 5.x 用模块级 `__getattr__`（惰性模块）。当两个后台线程
（RAG 预热、情绪模型预热）**同时首次导入**它时，后一个会拿到半初始化模块。
更糟的是 `_load_embedder` 把失败永久缓存，整个进程就一直是词法检索，只在日志里留一行 warning。

**修复**：

1. `core/hf_import.py`：全局锁串行化导入 + 失败时丢掉半成品重试一次；
2. `main._warm_models()` 改成**串行**预热（先 RAG 后情绪），不再并发；
3. `rag.retry_embedder()`：预热失败时显式重试一次，不让"悄悄降级"挂一整个进程。

验证：干净实例上连续 3 次 `/api/health` 都是 `rag=vector / emotion=onnx`。
