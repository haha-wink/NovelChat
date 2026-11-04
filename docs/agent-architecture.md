# Services 层双 Agent 架构设计（v2）

> **v1 → v2 变更**：引入**书架**作为系统边界、新增**群聊引擎**、情绪改为**规则+本地小模型**三层漏斗、好感度**不可见但外显**、贴纸用 **Twemoji**、新增**渠道抽象**为 QQ/微信预留。
>
> 本文只做设计，不含实现。

---

## 0. 五个决策 → 设计影响

| 决策 | 结论 | 对设计的影响 |
|---|---|---|
| 好感度可见性 | **不给用户看数值**，也不显示区间；由角色言行外显 + 详情面板给"人话" | 无进度条；新增「情绪/态度→人话」映射（第 5.3 节）+ 角色详情面板 |
| 贴纸来源 | **先用开源 Twemoji**，后期管理 Agent 可扩展 | 表情与表情包统一为「emoji 白名单 + Twemoji 资源」，第 8 节 |
| 多角色 | **做群聊**：书架内所有角色一个群，用户是群主 | 新增群聊引擎、发言意愿、@ 唤醒、渠道抽象，第 3/9 节 |
| 情绪信号 | **本地小模型 + 规则约束** | 三层漏斗，不引入 torch，可降级，第 4 节 |
| 管理 Agent 经验 | **按书架共享**；新增书架结构，一本书架=一套系统 | 数据边界上移；初始记忆必须可增量增删，第 2/6/7 节 |

⚠️ 第 3、5 项是破坏性变更：**现有 `characters.excerpts` 是单个 JSON 数组，无法支持「删书后重建初始记忆」**，必须改成带来源的记忆条目表（第 2.4 节）。

---

## 1. 总体分层

```
┌────────────────────────────────────────────────────────────────┐
│  api/routes.py                 HTTP / SSE                       │
│  services/channels/            Web(现) · QQ · 微信(预留)          │
└──────────────┬───────────────────────────────┬─────────────────┘
               │                               │
   ┌───────────▼──────────────┐   ┌────────────▼─────────────────┐
   │ 管理 Agent                │   │ 角色 Agent（单个角色）         │
   │ manager_agent.py          │   │ character_agent.py           │
   │ · 书架同步（增/删书）      │   │ · 单条发言（文字/图/贴纸）     │
   │ · 抽取 / 人名校验          │   │ · 情绪 + 外显                 │
   │ · 头像设计                │   │ · 好感度（对用户/对角色）      │
   │ · 书架级经验记忆           │   │ · 书架级记忆                  │
   └───────────┬──────────────┘   └────────────┬─────────────────┘
               │                               │
   ┌───────────▼───────────────────────────────▼─────────────────┐
   │ 群聊引擎 group_chat.py                                        │
   │ · 发言意愿打分 → 选角 → 串行生成 → @ 传播 → 静默/唤醒状态机      │
   └───────────┬──────────────────────────────────────────────────┘
               │
   ┌───────────▼──────────────────────────────────────────────────┐
   │ 共享基座                                                      │
   │ agent_log · agent_memory · affect(+emotion_model) · expression │
   └──────────────────────────────────────────────────────────────┘
```

**三条硬约束**（继承并扩展 v1）：

1. **模型只做决策，不提供事实**（沿用 `extraction_agent.py` 原则）。
2. **数值不出现在对话里**——好感度、情绪强度既不给用户看，也不进模型可见输出。
3. **一本书架 = 一套隔离系统**——记忆、情绪、群聊、经验全部按 `shelf_id` 分区，跨书架不串味。

---

## 2. 书架（Shelf）：新的系统边界

### 2.1 模型

```
library（书库，最大结构：用户的书库，可含多个书架）
 └─ shelf（书架，一套"系统"：同系列的书为一个整体）
     └─ books（同系列的书，可增删）
         └─ characters（角色 agent，属于某本书）
              └─ 加入 shelf 级群聊
```

- **书库**是组织单位（一个用户可以有多个书库）；**书架**才是隔离边界。
- `books.shelf_id` 为外键；老数据迁移时**自动创建「我的书库 / 默认书架」**并把现有书全部归入。
- 用户在界面上按书库 → 书架组织，**记忆、情绪、群聊、管理 Agent 经验、日志全部以书架为作用域**。

### 2.2 隔离矩阵

| 资源 | 作用域 | 说明 |
|---|---|---|
| 短期记忆（7 天） | **书架** | 群聊里 A 说的话，B 的短期记忆里也有 |
| 长期摘要 | 书架 × 角色 × 用户 | 摘要记录"在这个书架里发生的事" |
| 初始记忆 | 角色 | 但**来源标注到书**，删书可精确回收 |
| 对用户好感度 | 角色 × 用户 × 书架 | 换书架等于换世界观，好感度重置 |
| 角色间好感度 | 角色 × 角色（限同书架） | 跨书架不建立关系 |
| 群聊 | 书架 × 用户 | 一个书架一个群 |
| 管理 Agent 经验 | **书架** | 同系列书的抽取套路共享 |

> 注意：用户对话历史本身不分书架强弱，但**情绪与好感度按书架独立**——否则"在 A 书架和某人热络，去 B 书架还热络"会崩人设。

### 2.3 书架内的增删书（两个关键事务）

**加书**（用户把一个新书放进书架）：

```
1. 落盘 + 分块（复用 library.py）
2. 管理 Agent 抽取该书角色 → 为每个新角色生成角色档案 + 初始记忆
3. 书架级增量同步（重点）：
   a. 新角色加入群聊（shelf_members 插入，状态=在场）
   b. 对书架内**已有角色**做"初始记忆补强"：
      在新书全文里检索旧角色名/别名 → 命中则抽取新片段
      → 写入 character_memory_items(kind=excerpt, source_book_id=新书)
   c. 为新角色 × 旧角色两两建立种子好感度（同系列书常有既定关系）
   d. 更新书架级经验记忆
4. 群聊提示"XX 加入了群聊"（系统消息，不算角色发言）
```

**合并同名角色**（P5 补）：同系列两本书常会抽出同一个人（两本都有「路明非」）。
不合并的话，群里有两个分身、记忆还各记一半。合并规则：

* 认定：**必须有一方的主名参与**——主名相同，或一方主名出现在另一方别名里。
  只看"别名相交"是不够的：真实数据里赵孟华的别名是「老大」、恺撒·加图索的别名也是
  「老大」，靠别名相交会把两个完全不同的人并成一个（已踩过，已加回归测试）。
* 通用称呼（老大/师父/老板…）一律不当身份键。
* 本体：出场次数最多的那个。
* 记忆：其它角色的条目整体迁到本体，**`source_book_id` 原样保留**；
  再把跨书记忆压成一段摘要（`kind='summary'`），摘要只是派生视图，原始条目一条不删。
* 被合并的角色写 `canonical_id` 指向本体，不再作为独立成员出现。

**手动合并**：自动规则只认主名，`诺诺`/`陈墨瞳`、`麻衣`/`酒德麻衣` 这种同一人但别名
没互相指向的情况会漏。所以详情面板提供「合并到…」手动指定，并支持撤销。

**撤销靠 `origin_character_id`，不能靠 `source_book_id`。** 同一个角色和它的重复项可能在
同一本书里（手动合并常见），按"来源书"回迁会把对方的记忆一起搬走——这条是被测试当场
抓出来的。

**删书**：

```
1. 该书角色退出群聊（shelf_members.status='left'）——不删角色数据，只是不在群里
2. 回收初始记忆：删除 character_memory_items WHERE source_book_id=该书
3. 重建受影响旧角色的初始记忆（若有片段来自该书则重算 top-k）
4. 删除跨该书的关系种子；删除仅存在于该书的群聊上下文
5. 更新书架级经验记忆
```

**关键设计**：初始记忆必须**按条存储且带来源**，才能做「精确回收 + 重建」。这正是要改 `excerpts` 的原因（见 2.4）。

### 2.4 初始记忆的改造：从 JSON 数组 → 可回溯条目表

| 现状 | 问题 | 改为 |
|---|---|---|
| `characters.excerpts` JSON 数组 | 无法知道哪段来自哪本书；删书无法回收；无法增量追加 | `character_memory_items` 表，每条带 `source_book_id`、`kind`、`importance`、`added_reason` |

向后兼容：**`excerpts` 降级为派生缓存**——由条目表同步生成（`_sync_excerpts_from_items`），
提示词（`services/prompts.py`）、前端、接口全部零改动。迁移时把每条拆成
`source_book_id=该角色所属书` 的记忆条目，幂等（已有条目的角色不会重复灌入）。

---

## 3. 群聊引擎（核心新增）

### 3.1 回合模型

**一次"回合"= 用户说一句话之后，群里的连锁反应**：

```
用户发言（可 @角色 / @全体）
   ↓
第 1 轮选角：算出所有在场角色的发言意愿 → 挑 top-k（默认 k=2，上限 4）
   ↓
串行生成（必须串行：后一个角色要看到前一个说了什么）
   ↓
角色 A 发言 → 若 A 在话里 @了 B → B 进入"必答"队列（跳过本轮打分，直接排下一轮）
   ↓
第 2 轮（若有必答或仍有人过阈值）→ 继续
   ↓
达到 max_rounds（默认 2）或无人愿意说话 → 回合结束
```

**为什么串行**：并发会让 A、B 互相看不到对方刚说的话，出现"各说各话"。代价是延迟叠加，所以必须严格控制发言人数。

### 3.2 发言意愿（主动性来自性格）

每个角色在**抽取阶段**由管理 Agent 推出 `SpeechProfile`：

| 字段 | 含义 | 来源 |
|---|---|---|
| `extraversion` 0~1 | 外向度/话痨度 | 从 `personality` 推断（"热情健谈"→0.8，"沉默寡言"→0.15） |
| `assertiveness` 0~1 | 是否爱抢话/打断 | 从 `role`/`personality` |
| `initiative_floor` | 无人提问时是否主动开口 | 多数角色 < 0.2 |

每轮打分：

```
score = 0.30 * extraversion      # 性格推出（话痨 0.85 / 寡言 0.25 / 默认 0.5）
      + 0.25 * relevance         # 点名 0.9 / 别名 0.7 / 提到关系人 0.4
      + 0.15 * arousal           # 当前情绪唤醒度（激动时更想说话）
      + 0.10 * noise             # ±0.1，避免群聊变成固定顺序的复读
      - 0.20 * cooldown          # 最近 T 秒内发过言
      - 0.15 * 刚说过话

发言阈值: score >= 0.55 才有资格；取 top-k（k = group_max_speakers）
```

**实现注记（P4 落地）**：被 `@全体成员` 点到时额外 **+0.15**（`ALL_CALL_BOOST`）。
没有这个加成，`0.30 × 外向度` 的基础分本来就只有 0.15~0.26，够不着降到 0.35 的阈值，
`@全体` 等于白叫——实测只会有一个人开口。加成后最话痨的 2 人（古德里安 0.51、芬格尔 0.41）
会响应，仍受 `max_speakers` 限制。

**@ 是一等公民**：

| 来源 | 行为 |
|---|---|
| 用户 `@全体成员` | 所有在场角色都进入候选，阈值降到 0.35（但 k 仍受限，避免刷屏） |
| 用户 `@某角色` | 该角色 score 直接置 1.0，跳冷却，**必答** |
| 角色 `@某角色` | 被 @ 者进入"必答"队列（一次），可形成对话链 |
| 常驻静默 | `shelf_members.status='muted'` 的角色不参与打分，仅被 @ 时唤醒 |

### 3.3 角色状态机

```
idle(沉默/潜水) ──@或被高相关话题唤醒──> listening(在场关注)
listening ──score 过阈值 / 被 @──> speaking(本轮发言)
speaking ──发言结束──> cooldown(冷却) ──超时──> idle
```

- `muted`（用户手动静默）是 `idle` 的特例，`@` 可强制唤醒一次。
- 状态落 `group_member_state`，重启不丢。

### 3.4 一轮的完整时序（含情绪与记忆）

```
1. 用户消息进入 → 渠道适配器归一化（text/images/mentions）
2. 写入 chat_turns（role=user, shelf_id）
3. 本轮候选角色的情绪先按"群聊上下文"做一次回落
4. 选角 → 对每个当选角色：
   a. 组装 system：档案 + 初始记忆 + 长期摘要 + 当前情绪块 + 群聊规则
   b. 注入"最近 N 条群聊（含谁说了什么）"作为上下文
   c. 流式生成 → 解析出文本 + 贴纸占位符 + 可能的 @
   d. 写 chat_turns(role=assistant, character_id)
   e. 其他在场角色：情绪微调（听到 A 的话）+ 好感度微调（对 A，小步）
5. SSE 逐条推送（每条带 speaker + 增量文本）
6. 回合结束：异步跑情绪信号抽取（三层漏斗）+ 记忆摘要（若达阈值）
```

**重要**：第 4.e 步是"角色间情绪"的主引擎——**群聊让 A↔B 自然演化**，比 v1 的"用户转述"通道强得多。

**单角色已落地（P3）**：第 4a 步的提示词分两段组装——

| 段 | 内容 | 载体 | 频率 |
|---|---|---|---|
| 稳定 | 角色档案 + 初始记忆 top-k（`excerpt_chunks`） | `Agent.instructions` | 构建时一次 |
| 易变 | `【此刻状态】`（情绪 + 态度 + 表达规则） | `Agent.additional_context` | 每轮 |

Agno 在**每轮组装系统消息时**才拼接 `additional_context`（`agno/agent/_messages.py:1086`），
所以既不用重建 Agent（不破坏 LRU 缓存），状态也不会停在第一轮。并发上每轮操作 Agent 的
**副本**（`copy.copy`），避免共享缓存实例把 A 的情绪串给 B。

### 3.5 成本与节流（群聊最大风险）

| 风险 | 对策 |
|---|---|
| N 个角色 × M 轮 = 模型调用爆炸 | 硬上限：`max_speakers=2`、`max_rounds=2`，即一轮最多 4 次调用 |
| 用户连发短消息 | 500ms 内合并成一条 |
| 话痨角色垄断 | 冷却 + k 限制 + 每人每回合最多 1 次 |
| 群聊上下文过长 | 群聊上下文只取最近 20 条 + 摘要，不塞全史 |
| 空转 | 无人过阈值就直接结束，不要"为了热闹"强行让角色说话 |

---

## 4. 情绪系统

### 4.1 三层漏斗（规则 → 本地小模型 → 兜底）

```
输入：用户这一句（+ 近期上下文）
 ① 规则层（永远执行，零成本）
    词表/句式 → 信号：礼貌/夸奖/侮辱/威胁/触碰忌讳/承诺/失约
    产出：确定性的 Δvalence / Δarousal / Δfavorability
 ② 本地小模型（仅在"不确定"时调用）
    触发条件：规则信号冲突、或出现反讽/含蓄表达、或消息 > 80 字
    产出：情绪倾向（正向/中性/负向 + 强度）+ 置信度
    用法：只做**加权修正**，不改规则已经确定的部分
 ③ 兜底：两者都没有明确结论 → 只做向 baseline 回落，好感度不动
```

### 4.2 为什么不是每轮 LLM

每轮 LLM 抽信号 = 每轮多 200~400ms + 费用，且群里一轮可能多个角色 = 成本乘倍数。本地小模型 + 规则能做到毫秒级、零网络、零费用，只有在"真的读不懂"时才升级。

### 4.3 模型选型（**不引入 torch**）

这是本设计最关键的技术约束：项目当初为了启动速度**主动移除了 torch / sentence-transformers**（见 `requirements.txt` 第 55~62 行注释）。所以：

| 方案 | 可行性 | 结论 |
|---|---|---|
| torch + BERT 情感分类 | 与项目既有决策冲突（多 GB、启动慢） | ❌ 不用 |
| **ONNX Runtime + 中文情感小模型** | 运行时约几十 MB，纯 CPU，启动快 | ✅ **首选** |
| 纯词典 + 规则 | 零依赖，但反讽/含蓄读不懂 | ✅ 永远作为兜底 |

设计成与 `name_engine.py` **完全同构**的可插拔后端：

```
EMOTION_MODEL = auto | onnx | rules      # 默认 auto
```

- `auto`：能 import `onnxruntime` 且模型文件存在 → 用 onnx，否则 rules。
- `/api/health` 暴露 `emotion_model` 字段（照抄 `name_engine` 的做法）。
- 模型文件放 `backend/data/models/emotion/`，**不进仓库**，缺失即降级，不报错。
- 具体模型待定（需要挑一个已导出 ONNX 的中文情感分类，见待定项）。

### 4.4 好感度不可见 → 必须"外显"

既然用户看不到数值，角色就必须**用言行让用户感觉到**。外显映射（写进 prompt 的输出要求）：

| 内部状态 | 外显手段（禁止直接说数值） |
|---|---|
| 情绪=喜悦 | 语气轻快、句尾语气词（呀/啦）、😆✨ 类 emoji/Twemoji |
| 情绪=恼怒 | 短句、句号变多、反问、"哼"、🥱😤 类贴纸 |
| 情绪=失落/伤心 | **"我太伤心了，呜呜"**、省略号、缩句、🥺💧 类贴纸 |
| 情绪=警惕 | 追问动机、减少自称、缩短回复 |
| 好感度低 | 敬称/疏离措辞、不接话、拒绝亲昵称呼 |
| 好感度高 | 主动关心、用昵称、替对方说话、示弱 |

**硬规则**：
- 情绪低落时**允许**出现"呜呜""唉"等情绪词与贴纸——这正是用户要的可见性。
- 但**禁止**输出"我的好感度是 62""我对你情绪 -0.3"这类元信息。
- 情绪与好感度矛盾时（高好感+恼怒）→ "生气地关心"，不许崩回冷淡。

---

## 5. 好感度

### 5.1 两个对象，两套数值

| 对象 | 表 | 作用 |
|---|---|---|
| 对用户 | `character_affect`（角色×用户×**书架**） | 决定对用户的语气策略 |
| 对其他角色 | `character_relations`（角色×角色，限同书架） | 决定群聊里对某人的态度、是否主动搭话/怼人 |

### 5.2 种子来自小说原文（沿用 v1）

关系词先验（师徒 +55 / 恋人 +75 / 挚友 +65 / 同门 +35 / 竞争 −15 / 仇敌 −60）+ 共现段落语气 ±10 + 性格修正，每条带 `evidence` 引用原句。

**书架场景增强**：同系列书加入时，为"新角色 × 旧角色"两两建种子（同系列常见既定关系，如"A 的父亲"）。

### 5.3 不显示数值，但可感知

**不出现任何数值或档位条**。用户通过两条通道感知：

1. **对话里的言行**——措辞、句长、语气词、emoji/贴纸、主动@、是否接话；
2. **角色详情面板**（`GET /api/characters/{id}/status`）——展示「心情：警惕 · 对你有些戒备，先观察」
   「对你的态度：保持距离，礼貌但不会过分热络」这类**人话**，以及互动轮次、记忆条数等。

面板刻意**不返回** `favorability` / `valence` / `arousal`：数值只留在库里和 `affect_change`
日志中，供调参使用。前端也据此不渲染任何进度条或区间刻度。

**迭代靠日志验证**——`agent_events` 里的 `affect_change`（含 before/after/信号/证据）是唯一的调参依据。

---

## 6. 记忆分层（书架作用域）

### 6.1 短期记忆（书架内近 7 天）

- 数据源 `chat_turns`，键：`shelf_id + user_id`，**包含群聊里所有人的发言**。
- 取用：`created_at >= now-7d`，按 token 预算截断（默认 3000），无条件保留最近 N 条。
- 角色看短期记忆时会带上"谁说的"（speaker），否则群聊上下文会混乱。

### 6.2 初始记忆（可增删、可回溯）

- 存储：`character_memory_items`，每条 `kind=excerpt`、带 `source_book_id`。
- 只读语义：**来自小说的原文片段，角色不可改写**，只能由管理 Agent 增删。
- 加书 → 追加条目；删书 → 精确回收并重建 top-k。

### 6.3 长期摘要（精炼提取）

- 对短期记忆滚动蒸馏：`事实 / 关系变化 / 承诺约定` 三类要点。
- 存 `memory_summaries`，带 `importance`、`period`、`source_turn_ids`、`superseded_by`。
- 触发：每 20 轮 或 会话空闲 30 分钟，异步单飞。

### 6.4 检索（无向量库）

`初始记忆 top-3` + `摘要 top-M`，排序 = `importance × 时间衰减 × 关键词重合`。
项目已移除向量依赖，保持零新依赖；若将来同系列书很多，再考虑本地 embedding。

---

## 7. 管理 Agent

### 7.1 职责（书架同步升为首要职责）

| 职责 | 说明 |
|---|---|
| **书架同步** | 加书/删书后的一系列事务（第 2.3 节），是它的最高优先级工作 |
| 抽取与校验 | 角色抽取、人名识别、`validate_characters` 逐字校验 |
| 头像设计 | 风格画像 → 多查询 → 打分取舍（v1 第 2.3 节） |
| 经验记忆 | 按**书架**沉淀抽取套路，供下次增量抽取使用 |
| 全量日志 | 所有工具调用/决策/错误落 `agent_events` |

### 7.2 工具集（在 v1 基础上新增）

- 新增 `sync_shelf(shelf_id, change)`：执行加/删书后的增量同步（补强初始记忆、建关系种子、更新群成员）
- 新增 `rebuild_memory(character_id)`：回收某来源的片段后重建 top-k
- 新增 `recall_experience(shelf_id)`：取书架级经验
- 改名 `avatar_candidates` → `avatar_design`
- 新增 `add_sticker_pack`（后期：让管理 Agent 扩展贴纸库）

### 7.3 经验记忆按书架

`agent_memories.owner_type='manager'` + `shelf_id`：同系列书的抽取套路共享，跨书架不串。
这正好回答了你原来的顾虑——**"跨书"被限制在书架内，偏见不会漫到别的系列**。

---

## 8. 贴纸与表情（Twemoji）

| 概念 | 实现 |
|---|---|
| 表情（emoji） | Twemoji 资源，按 Unicode 码点命名 |
| 表情包（贴纸） | 复用同一套资源，做成"情绪→候选集"的语义包 |

- 资源放 `backend/data/stickers/twemoji/*.svg`（**不进仓库**，由脚本下载或首次运行拉取）。
- `expression.py` 维护 `情绪标签 → 允许的 emoji 子集`。
- Agent 输出**占位符** `[sticker:😭]` 或直接输出 emoji；服务端校验在白名单内 → 转 `/api/sticker/{codepoint}.svg`；不在白名单 → 原样保留成文本（不打断回复）。
- 前端渲染；`/api/sticker/{name}` 只读登记表内文件（防目录穿越）。
- 后期：管理 Agent 可下载新包并登记（`add_sticker_pack` 工具）。

**为什么用占位符**：模型不能凭空编造 URL，也不把二进制塞进 SSE。

---

## 9. 渠道抽象（为 QQ / 微信预留）

群聊一旦要接外部平台，**核心引擎必须与协议解耦**。设计：

```
services/channels/
├─ base.py          # 协议与规范消息
├─ web.py           # 现在的 SSE（默认）
├─ qq.py            # 预留：OneBot / 官方机器人
└─ wechat.py        # 预留：公众号 / 企业微信
```

**规范消息**（与平台无关）：

```python
InboundMessage(
    channel: str,                # web | qq | wechat
    external_user_id: str,       # 平台用户 id
    external_group_id: str|None, # 平台群 id（私聊为 None）
    text: str,
    images: list[bytes],
    mentions: list[str],         # 被 @ 的角色名 / "all"
    reply_to: str|None,
    ts: datetime,
)

OutboundMessage(
    text: str,
    stickers: list[str],         # 冒号占位符或 emoji
    mentions: list[str],
    images: list[str],
    reply_to: str|None,
)
```

- `ChannelAdapter` 协议：`receive(event) -> InboundMessage`、`send(OutboundMessage) -> None`、`capabilities()`。
- **身份映射表** `channel_identities(channel, external_id, user_id, shelf_id)`：平台 id ↔ 内部 user_id。
- 群聊引擎只吃 `InboundMessage`、只吐 `OutboundMessage`，**不知道**底层是 SSE 还是 QQ。
- 平台差异（是否支持 @、是否支持图片、消息长度上限）由 `capabilities()` 声明，引擎据此降级。

这样后期接 QQ/微信只是新增一个 adapter，**不改群聊与情绪逻辑**。

**落地实现（P7）**：

```
backend/channels/
  base.py     ChannelCapabilities / InboundMessage / OutboundMessage / Channel
              + event_to_outbound()  <- 引擎事件到出站消息的唯一翻译点
  web.py      WebChannel（enabled）：render -> SSE 事件字典，to_sse -> data: 帧
  qq.py       QQChannel（enabled=False）：文件头写着接入清单（验签、切段、不支持流式）
  wechat.py   WeChatChannel（enabled=False）：说明公众号/企业微信/个人号三条路的差别
  registry.py 按名字取渠道 + describe_all()
```

群聊 SSE 现在走一圈：`引擎事件 → OutboundMessage → WebChannel.render → to_sse`。
看着绕，但换来的是接新平台时**引擎和路由一行都不用改**——测试里专门验证了这一圈不丢字段
（`forced` / `mentions` / `round` / `sticker` 都在 `meta` 里原样带回）。

平台硬约束（已写进各 adapter 的 `capabilities()`）：

| 渠道 | 流式 | @ 唤醒 | 单条上限 | 备注 |
|---|---|---|---|---|
| web | ✅ | ✅ | 20000 | 当前唯一启用 |
| QQ 官方机器人 | ❌ | ✅ | ~1000 | 长回复必须切段 |
| 微信（公众号/企业微信） | ❌ | ❌ | ~600 | 拿不到全群消息，"谁能接话"要退化 |

最后一行是设计上的硬约束：**微信个人号拿不到群消息流，做不到真正的多角色群聊**。

---

## 10. 数据表

在 v1 基础上新增/修改：

```sql
-- 书库（最大结构，仅作组织单位；隔离边界仍在书架）
CREATE TABLE IF NOT EXISTS libraries (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 书架
CREATE TABLE IF NOT EXISTS shelves (
    id TEXT PRIMARY KEY, library_id TEXT NOT NULL, title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE CASCADE
);
ALTER TABLE books ADD COLUMN shelf_id TEXT;         -- 老库迁移：建默认书库+书架并回填

-- 群成员（书架 × 角色）
CREATE TABLE IF NOT EXISTS shelf_members (
    shelf_id TEXT NOT NULL, character_id TEXT NOT NULL, source_book_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle',      -- idle|muted|left
    joined_at TEXT NOT NULL, left_at TEXT,
    PRIMARY KEY (shelf_id, character_id)
);

-- 群成员运行态（状态机 + 冷却 + 性格参数）
CREATE TABLE IF NOT EXISTS group_member_state (
    shelf_id TEXT NOT NULL, character_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'idle',       -- idle|listening|speaking|cooldown|muted
    last_spoke_at TEXT, extraversion REAL NOT NULL DEFAULT 0.5,
    assertiveness REAL NOT NULL DEFAULT 0.5, initiative_floor REAL NOT NULL DEFAULT 0.2,
    PRIMARY KEY (shelf_id, character_id)
);

-- 初始记忆条目（可回溯，替代 excerpts JSON）
CREATE TABLE IF NOT EXISTS character_memory_items (
    id TEXT PRIMARY KEY, character_id TEXT NOT NULL,
    source_book_id TEXT,                      -- 删书时按此精确回收
    kind TEXT NOT NULL,                       -- excerpt | summary | fact
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    added_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 群聊发言（chat_turns 扩展）
ALTER TABLE chat_turns ADD COLUMN shelf_id TEXT;
ALTER TABLE chat_turns ADD COLUMN speaker_character_id TEXT;   -- 群聊中是谁说的
ALTER TABLE chat_turns ADD COLUMN mentions TEXT NOT NULL DEFAULT '[]';
ALTER TABLE chat_turns ADD COLUMN round_no INTEGER NOT NULL DEFAULT 0;

-- 其余沿用 v1：agent_events / agent_memories / character_affect / character_relations
-- 全部加 shelf_id 维度；character_affect 主键改为 (character_id, user_id, shelf_id)

-- 渠道身份映射
CREATE TABLE IF NOT EXISTS channel_identities (
    channel TEXT NOT NULL, external_id TEXT NOT NULL,
    user_id TEXT NOT NULL, shelf_id TEXT,
    PRIMARY KEY (channel, external_id)
);
```

---

## 11. 文件规划

```
services/
├─ manager_agent.py       【新】管理 Agent（书架同步 + 抽取 + 头像 + 经验）
├─ character_agent.py     【新】角色 Agent（单条发言 + 情绪 + 记忆）
├─ group_chat.py          【新】群聊引擎（选角/@/状态机/串行生成）
├─ agent_log.py           【新】agent_events 写入与时间线
├─ agent_memory.py        【新】记忆条目增删、摘要蒸馏、检索
├─ affect.py              【新】情绪/好感度状态机 + 规则引擎
├─ emotion_model.py       【新】本地小模型后端（onnx/rules，仿 name_engine）
├─ expression.py          【新】Twemoji 白名单 + 贴纸占位符解析
├─ shelf.py               【新】书架：增删书事务、成员维护、迁移
├─ channels/
│   ├─ base.py            【新】ChannelAdapter + 规范消息
│   ├─ web.py             【新】SSE 适配（现有逻辑迁入）
│   ├─ qq.py              【预留】
│   └─ wechat.py          【预留】
├─ prompts/
│   ├─ roleplay.py        【迁】角色扮演（含情绪外显规则）
│   ├─ group.py           【新】群聊规则（谁在、谁刚说过话、@ 规范）
│   ├─ extraction.py      【迁】抽取
│   └─ manager.py         【新】管理 Agent 工具说明 + 经验注入
│
├─ extraction_agent.py    → 保留为 manager_agent 的实现细节
├─ agent_manager.py       → 保留为 character_agent 的 Agno 工厂
├─ library.py             → 保留文件落盘；调度迁入 manager_agent
└─ character_miner / name_engine / name_library / extractor
   web_tools / avatar_crawler / media / prompts（渐进拆分，保留兼容别名）
```

**兼容策略**：不重命名既有模块（`smoke_test.py` 有 225 项断言），只新增 + 内部改调用。

---

## 12. API 变更

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/shelves` | 书架列表 / 新建 |
| GET | `/api/shelves/{id}/members` | 群成员（含状态、是否静默） |
| PATCH | `/api/shelves/{id}/members/{cid}` | 静默/取消静默/移出群 |
| POST | `/api/shelves/{id}/books/{book_id}` | **加书**：角色入群 + 给原有角色补初始记忆 |
| DELETE | `/api/shelves/{id}/books/{book_id}` | **删书**：角色退群 + 记忆精确回收 |
| POST | `/api/shelves/{id}/merge` | 重新跑一遍同名角色自动合并 |
| POST | `/api/characters/{id}/merge` | **手动合并**到指定角色（补自动规则漏掉的） |
| POST | `/api/characters/{id}/unmerge` | 撤销合并，取回自己那本书的记忆 |
| POST | `/api/shelves/{id}/chat` | **群聊**：SSE 推送多条（每条带 speaker） |
| GET | `/api/shelves/{id}/logs` | 管理 Agent 行为时间线 |
| GET | `/api/stickers` / `/api/sticker/{name}` | Twemoji 清单与文件（**素材内置**，离线可用） |
| GET | `/api/channels` | 渠道能力清单（web 启用；QQ/微信预留） |
| GET | `/api/rag/status` | RAG 后端状态（vector / lexical）与延迟统计 |
| POST | `/api/rag/index` | 重建记忆向量索引（`?force=true` 全量重算） |
| GET | `/api/characters/{id}/recall?q=` | **检索预览**：这句话会捞到哪些记忆 |
| POST | `/api/characters/{id}/distill` | 把对话流水蒸馏成一条长期记忆 |
| GET | `/api/health` | 新增 `emotion_model`、`speech_profile` 可用性 |

`/api/chat`（单角色）保留，内部走同一个 `character_agent`。

**SSE 事件结构（群聊）**：

```json
{"type":"system","text":"《龙族II》的角色加入了群聊"}
{"type":"speaker_start","character_id":"c_1","name":"路明非","avatar_url":"..."}
{"type":"delta","character_id":"c_1","text":"……"}
{"type":"speaker_end","character_id":"c_1","stickers":["😭"],"mentions":["恺撒"]}
{"type":"done","rounds":2,"speakers":["c_1","c_3"]}
```

---

## 13. 成本模型（群聊是最大变量）

| 场景 | 调用次数 | 说明 |
|---|---|---|
| 单角色聊天 | 1 / 轮 | 与现状一致 |
| 群聊（默认） | **≤ 4 / 轮** | k=2 发言人 × round≤2 |
| 情绪信号 | 0（规则）或 1 次本地模型推理 | 本地，无网络 |
| 长期摘要 | 异步，每 20 轮 1 次 | 可批量合并 |

**必须守住的红线**：群聊一轮超过 4 次模型调用就是设计事故。所有上限都要可配且有硬顶。

---

## 14. 落地顺序

| 阶段 | 内容 | 依赖 | 风险 |
|---|---|---|---|
| **P0** ✅ | `libraries`/`shelves` + `books.shelf_id` + 老数据迁移；`agent_events` + `agent_log.py`；抽取流水线接入日志 | — | 已完成（自检 255 项） |
| **P1** ✅ | `character_memory_items` 替代 excerpts；`agent_memory.py` | P0 | 已完成（35 角色 / 105 条目迁移） |
| **P2** ✅ | `affect.py` + `emotion_model.py`（rules 后端）+ `character_affect` | P0 | 已完成（9 情绪标签 / 5 语气档 / 单轮上限） |
| **P3** ✅ | `character_agent.py` 单角色闭环（两段式提示词 + 状态注入 + 书架记忆） | P2 | 已完成（system message 实测含状态块） |
| **P4** ✅ | `group_chat.py` 群聊引擎（选角 + @ 唤醒 + 串行生成 + 状态机）；`shelf_members`/`group_member_state`/`chat_turns`；**前端群聊界面** | P3 | 已完成（引擎离线可测 + SSE 契约测试） |
| **P5** ✅ | 书架增删书事务 + `shelf.py`（角色进出群 + 记忆补强/回收）；**前端去掉角色列表，并入群成员** | P1,P4 | 已完成（13 项事务测试） |
| **P5.5** ✅ | 同名角色合并（`merge.py`）+ 跨书记忆摘要 + 删书回档 | P1,P5 | 已完成（35 → 26 成员，含防误合并回归） |
| **P6a** ✅ | `expression.py` 表达层 + Twemoji 贴纸（**18 个 SVG 内置，离线可用**）+ `/api/sticker` | P4 | 已完成（贴纸概率由 arousal 驱动） |
| **P6b** ✅ | `chat_turns` 7 天窗口 + 对话蒸馏进长期记忆；**RAG 记忆库**（块级向量 + BM25 + 场景加权） | P6a | 已完成（单次检索 11~13ms） |
| **P7** ✅ | `channels/`：`base.py`（Inbound/OutboundMessage + capabilities）、`web.py`（已启用）、`qq.py`/`wechat.py`（预留，含接入清单） | P4 | 已完成（24 项渠道测试） |
| **P9** ✅ | 正文清洗 `cleaner.py`：入库清洗 + 存量记忆原地清洗（不重抽） | P6b | 已完成（19 项清洗测试） |
| **P10** ✅ | 表达收敛（`reply.py` 去动作旁白 + 提示词改短）、**在线/离线在场模型**、贴纸规则化 | P6a,P4 | 已完成（28 项新测试） |
| **P8** ✅ | 情绪小模型（Erlangshen-Roberta-110M，自导 ONNX + int8 量化，98MB / 9ms） | P2 | 已完成（模型退化时已换型并加一致性校验） |

**建议**：P0 先做且**先备份 `backend/data/workspace/app.db`**——`shelf_id` 回填与 `excerpts` 迁移都是单向的。

---

## 15. 风险与待定

| # | 事项 | 说明 |
|---|---|---|
| 1 | **群聊一致性** | 串行生成保证"看到前一句"，但延迟随发言人数线性增长；需要前端逐条流式展示缓解体感 |
| 2 | **情绪本地模型选型** | 需挑一个已导出 ONNX、许可证清晰、中文情感分类效果可接受的小模型；未定前用 rules 后端 |
| 3 | **删书事务的原子性** | 退群/回收记忆/删关系种子必须一起成功，否则出现"人退了记忆还在"；建议单事务 + 失败重试 |
| 4 | **群聊"刷屏"** | k 与 round 的上限是产品体验问题，需实测定 2/2 还是 3/1 |
| 5 | **Twemoji 资源获取** | 首次运行需下载（约几十 MB）；离线环境要能优雅降级为纯 emoji 文本 |
| 6 | **QQ/微信合规** | 个人号自动回复有平台风险；预留接口时要把"人工确认后发送"也作为能力位 |
| 7 | **书架迁移的不可逆** | `app.db` 现有 2 本书的真实数据，迁移前必须备份 |

---

## 附：与 v1 的对应关系

| v1 内容 | v2 变化 |
|---|---|
| 双层情绪（情绪+好感度） | 保留；新增本地小模型层与外显映射 |
| 好感度 seed 来自原文 | 保留；增加"书架内新书×旧角色"两两建种 |
| 记忆分层（7天/长期） | 短期改为**书架作用域**；初始记忆改为**可回溯条目** |
| 表情包占位符 | 保留；资源确定为 Twemoji |
| `agent_events` 审计 | 保留；增加 `shelf_id` 维度 |
| 管理 Agent 经验 | 保留；作用域从"书"上移到"书架" |

---

## 13. RAG 记忆库（P6b）

角色记忆本身存在 `character_memory_items`，RAG 在它上面加三列 + 一张块表：

| 位置 | 作用 |
|---|---|
| `character_memory_items.scene` | 场景标签（战斗/雨夜/校园/对话…） |
| `character_memory_items.tokens` | BM25 用的切词（`core/text.py`，中文二元组） |
| `character_memory_items.vector` | 整段向量（块均值的兜底） |
| `memory_chunks` | **块级向量**：一条长记忆切成 ~220 字的块，每块一个 512 维向量 |

### 为什么必须按块

原文片段普遍 800~1200 字，整段编码会被 256 token 截断——只能看到开头，中段信息全丢。
切成 220 字的块之后，129 条记忆产生 856 个块，"按中段内容检索"才成立。

### 混合打分

```
score = 0.65 × 块级余弦 + 0.35 × BM25
        × 1.30（同场景）/ × 0.85（不同场景）
        × 1.05（摘要类）
```

### 后端与降级

* 默认 `bge-small-zh-v1.5`（91 MB，`backend/data/models/`），torch CPU 推理。
* 模型缺失 / torch 没装 / 任何异常 → 自动退化成纯 BM25，**聊天不会因为 RAG 挂掉**。
* 启动时后台线程预热（加载 6~7s）并给历史记忆补索引，第一个用户不会撞上冷启动。

### 实测（本机 CPU）

| 指标 | 数值 |
|---|---|
| 单次查询编码 | **11~13 ms**（与聊天响应同量级，可接受） |
| 批量建索引 | 2.1 ms / 条（129 条 ≈ 32 s，一次性） |
| 检索打分 | 856 块 × 512 维点积，< 5 ms |
| 模型加载 | 6~7 s（仅启动时一次） |

### 已知限制

原文片段里混着盗版站广告（"格格书海…"），这类文本会和任何查询都产生中等相似度。
真正的解法在抽取阶段做清洗，不在检索阶段硬压。

---

## 14. 正文清洗（P9）

盗版站的广告/水印会同时污染**抽取、提示词和 RAG 检索**——它们跟任何查询都有中等
相似度，会把真正相关的片段挤出结果。

`services/cleaner.py` 做两层：

| 时机 | 入口 | 影响面 |
|---|---|---|
| 入库 | `library.create_book()` | source.txt / chunks / 抽取 / 后续记忆，全新受益 |
| 存量 | `POST /api/maintenance/clean-memories` | 已有记忆原地改写，**不重抽、角色 id 不变** |

第二层是必须的：用户的角色可能已经改过资料、换过头像，重抽会让所有 id 变新。

### 规则

* 行级：整行是广告 / 纯分隔线（`----`、`====`）→ 删；
* **按句删**：广告常和正文粘在同一行，整行删会连正文一起删（第一版就是这么错的）；
* 行内残留：`（本章完）`、`字数：1234` 这类只摘片段；
* 重复页眉：短行出现 ≥5 次判为页眉页脚，整本去重；
* 归一化：BOM、零宽字符、全角空格、`\r\n`。

### 改内容必须作废向量

`db.update_memory_content()` 会同步重算 token、**把 vector 置空、删掉块向量**，
并刷新 `characters.excerpts` 缓存。否则检索还在用旧内容的向量——清洗看起来"生效了"，
实际检索结果一点没变。

### 实测

存量 129 条记忆只命中 2 条（真实数据本来就以正文为主），去掉 171 字；
清洗后全库 where content like '%格格书海%' 归零，重建索引 7.3 秒。
真正的大头在**入库清洗**——以后每本书都不再带这些垃圾进来。
---

## 16. 表达收敛与在场模型（P10）

### 16.1 回复只说话，不写动作

第一版提示词写的是"可以用 *星号* 包裹动作和神情"，结果每条回复都带一段旁白：

`
从桌上抬起头来，揉了揉眼睛，把嘴角的纸巾扯下来
在、在的——刚才谁喊我？
`

改成两层处理：

1. **提示词**：直接禁止动作/神态/心理描写，要求 1~2 句；
2. **`services/reply.py` 兜底**：删掉整行的 `*……*` / `（……）`、摘掉行内 `*动作*`。
   单聊用 `final` 事件覆盖流式已显示的文本；群聊在 `speaker_end` 直接给干净文本。

兜底只做"整行/整段是旁白"的情况，不碰正文里的括号，也不误删 Markdown 的 `**加粗**`。

### 16.2 在场模型：在线 / 离线

`shelf_members.status` 从 `active/muted` 改为 **`online` / `offline`**（老数据自动迁移）：

| 状态 | 能收消息 | 会发言 |
|---|---|---|
| online | ✅ | ✅（按选角规则） |
| offline | ❌ | ❌ |
| left | — | —（书被移出书架） |

* **`@全体成员` = 对每个人单独 @ 了一遍**：先把所有人设为在线，再让**所有在线成员逐一回复**，
  不受 `max_speakers` 限制，也不看阈值。
* **单独 `@某人`** 只把他叫上线，不惊动其他人。
* 成本提醒：@全体 的调用次数 = 在线人数。想省钱就先把人设为离线。

心情不再是独立的一栏，而是并入状态描述：面板显示「状态：在线 · 情绪平稳，看不出什么起伏」。

### 16.3 贴纸由规则触发

`expression.maybe_sticker()` 改成规则优先：

| 触发条件 | 行为 |
|---|---|
| 用户问起状态/心情（「你现在心情怎么样」「在吗」「你是不是生气了」） | **一定发** |
| 这一轮心情变了（`affect` 返回的 `previous_emotion` 与当前不同） | **一定发** |
| 其余 | 按 arousal 掷骰子（上限 30%） |

`affect.observe_user_message` 现在把 `previous_emotion` 一起返回，省掉一次额外查询；
群聊里每个发言人也会走一次情绪推进（规则引擎，不发模型请求）。
---

## 17. 情绪小模型（P8）

### 17.1 它在这个系统里的位置

情绪判断是**三层漏斗**的中间一层，不是主路径：

`
① affect 的言语行为规则（道谢/威胁/失约…）  → 命中就定，0.1ms，不联网
② 情绪极性小模型（本层）                    → 只在 ① 没把握时兜底
③ 都没有结论                                → 只回落到基线情绪
`

`affect._needs_model()` 决定要不要问模型：通常是无命中但句子较长、或正负信号打架时。
正常闲聊根本走不到这一层，所以它的延迟不影响常规响应。

### 17.2 模型选型踩的坑

第一次选的是现成的 ONNX 仓库 `lxyuan/...-sentiments-student`，下载 516MB，
结果**输出退化**：任何句子都判成同一类。fp32 和 int8 都一样，所以不是量化的问题，
是这个仓库导出的图本身有问题。

改成自己导出：

`
IDEA-CCNL/Erlangshen-Roberta-110M-Sentiment  (中文情感二分类)
  → 下载 PyTorch 权重 390MB
  → torch.onnx.export（旧导出器，不需要 onnxscript）
  → 与 PyTorch 输出做一致性校验（最大误差 2e-06，通过才算数）
  → onnxruntime 动态量化 int8 → 98MB
  → 删掉中间产物
`

`scripts/fetch_emotion_model.py` 就是这条流水线，新机器跑一次即可。

### 17.3 实测

| 指标 | 数值 |
|---|---|
| 模型体积 | 98 MB（int8） |
| 加载 | ~4.6 s（启动时后台线程预热，不挡启动） |
| 单次推理 | **9~22 ms**（含分词） |
| 判断准确度 | 正/负样本全对；中性句会偏向某一侧（二分类模型的固有局限） |

| 输入 | 极性 |
|---|---|
| 我今天真的很开心，谢谢你一直陪着我 | +0.92 |
| 我撑不住了，什么都不想做，太难受了 | −0.97 |
| 你真的很让我失望 | −0.88 |

### 17.4 配置与降级

| 配置 | 说明 |
|---|---|
| `EMOTION_MODEL` | `auto`（有模型就用）/ `onnx` / `rules` |
| `EMOTION_ONNX_DIR` | 模型目录，默认 `backend/data/models/erlangshen-sentiment` |

模型缺失、onnxruntime 没装、推理抛异常 → 全部自动回退规则后端，聊天不受影响。
`/api/health` 的 `emotion_model` 字段会显示当前后端、模型路径、标签和平均延迟。

> **注意**：`main.py` 不在 `dev.ps1` 的热重载监听范围内（只监听 api/core/services）。
> 改了启动逻辑要手动重启后端才生效。
---

## 18. 书架管理层与聊天历史（P11）

### 18.1 界面结构

`
书架页（书库 → 书架 → 书）
 ├─ 上传卡片（可选目标书架） + 新建书架
 └─ 书架卡片 × N
      ├─ 标题 / 书数 / 角色数
      ├─ 群聊 · 改名 · 删除
      └─ 书列表（只读 + 删除）

群聊页
 ├─ ← 书架   标题/成员数   信息
 └─ 消息流 …（**不再有成员条**：成员统一在信息界面看）

群聊信息页（独立视图，不是弹窗）
 ├─ ← 群聊   群聊信息（N）   刷新
 ├─ 群聊名称（可改，= 书架名）
 ├─ 成员网格（5 列 × 3 行，超出显示「更多」）
 └─ 添加角色 · AI审核 · 清空对话
`

「选择角色」按钮已移除：一个书架就是一个群，入口在书架卡片上。

应用启动时**默认直接进群聊**（`localStorage` 记住上次的书架，没有记录就进第一个），
书架页只通过群聊左上角的「← 书架」进入。

### 18.2 历史为什么要改蒸馏逻辑

原来 `distill()` 蒸馏完就 `DELETE FROM chat_turns`，理由是"原文不删会无限增长"。
代价是**重进群聊一片空白**——聊天流水同时是历史记录和蒸馏原料，两个用途被混在一起了。

现在加一列 `distilled`：

| 用途 | 过滤条件 |
|---|---|
| 蒸馏原料 / 阈值计数 | `distilled = 0` |
| 历史回看 | 全部 |

蒸馏只打标记（`mark_turns_distilled`），`chat_turns` 因此只增不删。
代价是表会长大——但一行只有几百字节，1 万轮对话约 3MB，可以接受。

### 18.3 历史接口

| 接口 | 说明 |
|---|---|
| `GET /api/shelves/{id}/history?token=&limit=` | 群聊历史（当前用户） |
| `GET /api/characters/{id}/history?token=&limit=` | 单聊历史（当前用户 + 该角色） |

另有 `DELETE` 同路径两个接口用于**清空**：只删 `chat_turns` 流水，
**不动蒸馏出来的长期记忆**（否则角色会失忆）。

两个 GET 都要登录态（401 否则），都按 `user_id` 隔离——换个人看到的是另一套历史。
前端进入群聊/单聊时各自拉一次，最多 80 条。

### 18.4 书架管理接口

| 接口 | 说明 |
|---|---|
| `PATCH /api/shelves/{id}` | 改名（群聊名同步）· 空名 400 |
| `DELETE /api/shelves/{id}?cascade=true` | 连书一起删；`cascade=false` 时有书就 400 |

级联删除放在**路由层**（`core` 不该反向依赖 `services`）：
先逐本 `library.delete_book()`（回收角色/记忆/情绪/群成员），再 `db.delete_shelf()`。