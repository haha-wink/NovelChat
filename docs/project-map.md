# 项目地图：每个文件是干什么的

> 一句话概括：**上传小说 → 抽出角色 → 角色进入书架群 → 和它们群聊或单聊**。
> 角色有自己的**记忆**（原文片段 + 对话蒸馏）、**情绪与好感度**、**在线状态**。
>
> 细节设计见 `docs/agent-architecture.md`，人工验收步骤见 `docs/manual-test.md`。

---

## 0. 分层

```
浏览器（Vue 3 单文件组件，SSE 流式渲染）
        │  REST + SSE
        ▼
api/routes.py            ← HTTP 层：参数校验、SSE 封装、错误码
        │  只调用服务层
        ▼
services/*              ← 业务规则（不碰 HTTP，可离线单测）
        │
        ▼
core/db.py（SQLite + JSON 列）   +   backend/data/models/*（本地小模型）
```

一条铁律：**`services/` 不 import `fastapi`，`core/` 不 import `services/`**（个别地方用函数内延迟导入，避免循环依赖）。

---

## 1. 根目录

| 文件 | 作用 |
|---|---|
| `README.md` | 项目说明、安装与启动步骤 |
| `requirements.txt` | 依赖清单（按「已验证版本」锁定，带注释说明每个包的用途） |
| `requirements.lock.txt` | `pip freeze` 的完整快照，用于精确重建 |
| `.gitignore` | 排除 venv / 密钥 / 运行时数据 / 模型缓存；**Twemoji 贴纸是例外，要入库** |
| `out.json` | ⚠️ `scripts/analyze_novel.py` 的输出残留，可删 |
| `jieba.cache` | jieba 运行时缓存（9MB），可删，删了会自动重建 |

---

## 2. backend/

### 2.1 入口与基础设施

| 文件 | 行数 | 作用 |
|---|---|---|
| `main.py` | 149 | FastAPI 应用入口。`lifespan` 里建目录、`init_db()`、清理残留图片目录，并在**后台线程**预热 RAG 嵌入模型和情绪小模型（不挡启动） |
| `core/config.py` | 235 | 集中配置（pydantic-settings，读 `backend/.env`）：路径、DeepSeek Key、抽取参数、群聊阈值、RAG、情绪模型开关 |
| `core/security.py` | 147 | 会话签名与校验：`issue_session` / `verify_session`，token 里带 user_id，HMAC 签名防伪造 |
| `core/db.py` | 1971 | **数据层核心**。建表 + 幂等迁移 + 所有读写函数。SQLite + JSON 列；WAL 模式。见下方「表结构」 |
| `core/text.py` | 77 | 极小的中文分词：字二元组 + 拉丁词。给 RAG 的 BM25 用，不依赖 jieba/torch |
| `core/hf_import.py` | 49 | 安全导入 transformers：加锁串行 + 半初始化失败重试（两个线程并发首次导入会让 RAG 静默降级成词法） |

**`core/db.py` 里的表**（全部按 `shelf_id` 隔离）：

| 表 | 存什么 |
|---|---|
| `libraries` / `shelves` / `books` | 书库 → 书架 → 书（三层） |
| `characters` | 角色档案（名字、性格、说话风格、背景、头像） |
| `character_memory_items` | 角色记忆条目（原文片段 / 对话蒸馏出来的事实） |
| `memory_chunks` | 记忆条目的向量块（RAG 检索用） |
| `character_affect` | 情绪状态（valence/arousal/emotion）+ 好感度 |
| `shelf_members` | 角色在某书架的**在线状态**（online/offline/left） |
| `group_member_state` | 群内运行态：idle/speaking/cooldown/muted + 性格权重 |
| `chat_turns` | 发言流水（**同时是聊天历史**；`distilled` 标记是否已蒸馏） |
| `agent_events` | Agent 行为审计日志 |

### 2.2 `api/routes.py`（1459 行）

唯一的 HTTP 路由文件，按域分区：

| 分区 | 主要接口 |
|---|---|
| 基础 | `/health`（含 RAG、情绪模型状态）、`/session` |
| 作品 | `/books` 上传/列表/删除、`/books/{id}/retry` |
| 书库 / 书架 | `/libraries`、`/shelves`、`PATCH /shelves/{id}`（改名）、`DELETE /shelves/{id}?cascade=` |
| 群成员 | `/shelves/{id}/members`、`PATCH .../members/{cid}`（在线/离线） |
| 聊天 | `POST /chat`（单聊 SSE）、`POST /shelves/{id}/chat`（群聊 SSE） |
| 历史 | `GET/DELETE /shelves/{id}/history`、`GET/DELETE /characters/{id}/history` |
| 角色 | 增删改查、`/characters/{id}/status`（此刻状态）、`/merge`、`/unmerge` |
| 合并 | `/shelves/{id}/merge`（跑一遍）、`/merge/candidates`（只读候选）、`/merge/resolve`（用户拍板）、`/memories`、`/memories/{id}` |
| 头像 | `/characters/{id}/avatars`（爬取候选）、`/avatar?url=`（代理图片，防盗链） |
| 贴纸 | `/sticker/{codepoint}.svg`、`/stickers` |
| RAG | `/rag/status`、`/rag/rebuild`、`/rag/search`、`/rag/distill` |
| 维护 | `/maintenance/clean-memories` |
| 渠道 | `/channels` |

### 2.3 `services/`（22 个文件，按用途分组）

#### ① 角色抽取流水线（这是最重的一块）

| 文件 | 行数 | 作用 |
|---|---|---|
| `extractor.py` | 515 | 抽取流水线骨架（纯逻辑，不碰 DB/文件）：分块 → 挖候选 → 合并 → 定档 |
| `character_miner.py` | 931 | **纯算法**角色挖掘（标准库实现，零 API 成本）：对话归因、称呼提取、性格推断、说话风格统计 |
| `name_engine.py` | 352 | 人名识别引擎：优先 jieba/HanLP，装不上回退内置规则（可插拔） |
| `name_library.py` | 473 | 人名库：判断"这串字是不是人名、中文名还是外国译名"，过滤地名/招式名 |
| `extraction_agent.py` | 606 | 「角色档案管理员 Agent」：用大模型把算法结果补全/纠错（`EXTRACTION_MODE=agent` 时启用） |
| `web_tools.py` | 187 | 联网检索校验：搜书名 → 抓正文 → 只在正文里逐字出现的名字才算数（防幻觉） |
| `avatar_crawler.py` | 443 | 角色头像爬取：多数据源尝试 + 缓存 |
| `cleaner.py` | 261 | 正文清洗：盗版站广告、水印、重复页眉页脚、分隔线（入库前 + 存量记忆原地清洗） |

#### ② 作品与书架

| 文件 | 行数 | 作用 |
|---|---|---|
| `library.py` | 473 | 作品库：原文落盘、分块、调度抽取、角色增删改；`delete_book` 会连带回收记忆/情绪/成员 |
| `shelf.py` | 189 | 书架事务：书加进来 / 拿出去时，群成员和角色记忆要跟着动 |
| `merge.py` | 505 | 同书架「同一个人」合并：名字/别名 → 管理记忆 → 关系性格相似度+模型判定；可撤销 |

#### ③ 记忆

| 文件 | 行数 | 作用 |
|---|---|---|
| `agent_memory.py` | 243 | 角色记忆条目的增删查 + 按场景检索 |
| `chat_memory.py` | 215 | 短期对话 → 长期记忆：超阈值自动蒸馏；**只打 `distilled` 标记不删原文**（原文同时是聊天历史） |
| `rag.py` | 485 | RAG 记忆库：块级向量索引 + BM25 + 场景加权；缺模型时自动降级为纯词法 |
| `scene.py` | 60 | 场景识别：把一段话/一条记忆归到某个场景标签（对话/战斗/日常…） |
| `summarizer.py` | 57 | 把角色初始记忆压成摘要（有 Key 走模型，没 Key 本地压缩） |

#### ④ 情绪与表达

| 文件 | 行数 | 作用 |
|---|---|---|
| `affect.py` | 492 | **双层状态机**：情绪（valence/arousal/emotion）+ 好感度（0~100，不展示数字）。识别言语行为（道谢/威胁/失约），读不懂时交给情绪模型兜底；每次变化写审计事件 |
| `emotion_model.py` | 423 | 情绪极性模型：规则后端（词表+否定+程度）与 ONNX 小模型（Erlangshen，int8 98MB，约 9ms）可插拔 |
| `expression.py` | 134 | 表达层：情绪 → 具体表达（贴纸选择）。**问心情 / 心情变化 → 必发贴纸** |
| `reply.py` | 74 | 回复后处理：删掉 `*动作*` / `（动作）` 这类旁白（提示词已禁止，这里是兜底） |

#### ⑤ 对话引擎

| 文件 | 行数 | 作用 |
|---|---|---|
| `prompts.py` | 251 | 提示词模板：角色扮演（稳定段进 `instructions`，易变段进 `additional_context`）+ 抽取提示词 |
| `agent_manager.py` | 156 | 按角色构建 Agno Agent，LRU 缓存；会话按 `user_id + session_id` 隔离 |
| `character_agent.py` | 113 | 单角色对话闭环：拼状态 → 调 Agent → 解析 |
| `group_chat.py` | 524 | **群聊引擎**：选角打分、@ 唤醒、@全体成员（全员上线并逐一回复）、串行生成、状态机、冷却 |

#### ⑥ 其它

| 文件 | 行数 | 作用 |
|---|---|---|
| `agent_log.py` | 182 | 审计日志：谁在什么时候因为什么做了什么事（可按书架/角色回放） |
| `media.py` | 108 | 上传图片校验：魔数判断真伪、大小/数量限制 |

### 2.4 `readers/document.py`（81 行）

小说文档读取：txt / md / pdf → 纯文本 + 类型。编码探测（utf-8/gbk）在这里做。

### 2.5 `channels/`（渠道抽象层）

| 文件 | 行数 | 作用 |
|---|---|---|
| `base.py` | 171 | 定义 `InboundMessage` / `OutboundMessage` / `capabilities`，把「引擎事件」与「平台怎么发」解耦 |
| `web.py` | 80 | Web + SSE，**当前唯一真正启用的渠道** |
| `qq.py` | 55 | QQ 官方机器人（预留，含接入清单：不支持流式、要整段发） |
| `wechat.py` | 57 | 微信（预留，含长度上限要切段的处理点） |
| `registry.py` | 39 | 按名字取渠道 |
| `__init__.py` | 49 | 导出 + `KIND_USER` 等常量 |

### 2.6 `tests/smoke_test.py`（3579 行）

**不依赖 pytest**，直接 `python backend/tests/smoke_test.py` 跑。覆盖：DB 迁移、抽取、情绪、记忆、RAG、群聊选角、SSE 契约、HTTP 接口、清洗、历史、书架管理……当前 **628 项**。

---

## 3. frontend/

### 3.1 根文件

| 文件 | 作用 |
|---|---|
| `index.html` | Vite 入口 HTML |
| `vite.config.js` | 开发服务器配置；`/api` 代理到 `127.0.0.1:8000` |
| `package.json` | `npm run dev` / `build` / `preview` |

### 3.2 `src/`

| 文件 | 行数 | 作用 |
|---|---|---|
| `main.js` | 10 | 挂载 Vue 应用 |
| `style.css` | — | 全局基础样式（滚动条、字体、reset） |
| `utils/markdown.js` | 160 | 极简 Markdown 渲染（加粗/斜体/代码/换行），避免引大依赖 |
| `App.vue` | 2275 | **整个前端**，单文件组件 |

**`App.vue` 内部结构**（已经按块注释分好）：

```
template
 ├─ 书架            （书库 → 书架 → 书；新建/改名/删除/群聊）
 ├─ 群聊            （头部 + 消息流 + 输入框；不再有成员条）
 ├─ 群聊信息        （独立界面：改名、成员网格 5×3、添加角色/审核/清空）
 ├─ 聊天            （单聊）
 ├─ 手动合并弹窗
 ├─ 角色编辑弹窗
 └─ 角色详情 / 状态

script（按注释分段）
 ├─ 基础状态        view / books / shelves / group / conversations …
 ├─ 请求封装        api()
 ├─ 上传与抽取进度   handleUpload / schedulePoll
 ├─ 书架管理        createShelf / renameShelf / removeShelf / openShelfChat
 ├─ 作品 / 角色
 ├─ 角色编辑
 ├─ 角色详情 / 状态  openMemberStatus / toggleMemberStatus
 ├─ 名单 AI 审核
 ├─ 会话 token      ensureToken / ensureGroupToken（localStorage 持久化）
 ├─ 聊天            单聊 SSE 流解析
 ├─ 群聊            群聊 SSE 流解析（speaker_start / delta / speaker_end / sticker）
 ├─ 群聊历史 / 信息  loadGroupHistory / visibleMembers / saveGroupTitle
 └─ 生命周期        onMounted（默认直接进群聊）

style（scoped）      全部样式，含书架卡片、成员网格、消息气泡、贴纸
```

---

## 4. scripts/

| 文件 | 作用 |
|---|---|
| `setup.ps1` | 一键装环境（建 venv、装依赖、取模型） |
| `dev.ps1` | 一键启动：后端 uvicorn `--reload` + 前端 `npm run dev` |
| `pip.ps1` | 用项目 venv 的 pip 装包（避免装到系统 Python） |
| `fetch_model.py` / `.ps1` | 下载 RAG 嵌入模型 bge-small-zh（91MB） |
| `fetch_emotion_model.py` | 情绪模型一条龙：下载权重 → 导出 ONNX → 一致性校验 → int8 量化（98MB） |
| `check-frontend.cjs` | 前端静态自检（68 项：模板/脚本标识符、关键交互是否还在） |
| `analyze_novel.py` | 命令行诊断：看看算法在一本小说里挖到了谁、丢了谁 |
| `check_names.py` | 人名库自检 |
| `check_nlp.py` | 人名识别引擎自检（jieba 装没装、回退到哪一档） |
| `check_avatar.py` | 头像爬虫诊断（不开服务器直接跑） |
| `sitecustomize.py` | 沙箱环境兼容：低完整性进程写不了 `%TEMP%` 时改用项目内临时目录（pip / jieba 缓存失败的解药） |

---

## 5. docs/

| 文件 | 内容 |
|---|---|
| `agent-architecture.md` | 完整设计文档：分层、数据模型、提示词策略、RAG、群聊引擎、渠道抽象、各阶段（P0–P11）实现说明 |
| `manual-test.md` | 人工验收手册：逐项点检（启动、上传、群聊、书架、历史、清空…） |
| `nlp-engine.md` | 人名识别引擎的取舍与降级链 |

---

## 6. backend/data/（运行时数据，**不入库**）

| 目录 | 内容 |
|---|---|
| `workspace/app.db` | 唯一的 SQLite 数据库（书架、角色、记忆、聊天记录） |
| `workspace/library/` | 小说原文分块落盘 |
| `models/bge-small-zh-v1.5/` | RAG 嵌入模型（91MB） |
| `models/erlangshen-sentiment/` | 情绪模型 + 分词器（98MB，int8） |
| `stickers/` | Twemoji SVG（18 个，17KB，**入库**，CC-BY 4.0） |
| `uploads/` | 聊天图片临时目录（启动时清理残留） |
| `backups/` | 数据库备份 |

---

## 7. 三条主数据流

### ① 上传 → 抽取

```
POST /api/books
  → readers/document.extract_text        取纯文本
  → cleaner.clean_text                    去广告/水印
  → services/library.create_book          原文分块落盘
  → 后台任务：extractor.extract()         算法挖候选（character_miner + name_engine）
        └→ extraction_agent（可选）        大模型补全纠错（要 API Key）
  → shelf.ensure_members                  角色进入书架群
  → agent_memory + rag.index_character    建记忆与向量
```

### ② 单聊

```
POST /api/chat（SSE）
  → affect.load_state            取当前情绪/好感度
  → rag.retrieve                 取相关记忆
  → prompts 拼 instructions/additional_context
  → agent_manager.get_agent      取（或建）该角色的 Agent
  → Agno 流式生成 → reply.strip_stage_directions 去旁白
  → affect.observe 更新情绪 → expression 决定贴纸
  → chat_turns 记流水（= 历史记录）
```

### ③ 群聊

```
POST /api/shelves/{id}/chat（SSE）
  → group_chat.parse_mentions       @ 谁 / @全体成员
  → @全体：set_all_members_online + 全员逐一回复
  → select_speakers                按性格/兴奋度/冷却/在线状态打分，取前 N
  → 串行生成（每人一次模型调用），事件流：
       speaker_start → delta… → speaker_end（带贴纸）
  → 每轮写 chat_turns
```

**成本控制点**：不是每条消息都有人回（阈值 0.55）；`@全体成员` = 在线人数次调用。

---

## 8. 维护提示

- 改提示词 → `agent/prompts.py`；改"说话短不短/有没有动作描写"→ 还要看 `services/reply.py`
- 改群聊选角 → `agent/group.py`；改在线/离线语义 → `core/db.py` 的 `set_member_status`
- 加接口 → `api/routes.py`（只做参数校验，逻辑放 services）
- 前端所有逻辑在 `App.vue` 一个文件里，改动后跑 `node scripts/check-frontend.cjs` + `npm run build`
- 改完后端跑 `python backend/tests/smoke_test.py`（628 项）
- **动数据库前先备份** `backend/data/workspace/app.db`；迁移都是幂等的，但删列不会还原