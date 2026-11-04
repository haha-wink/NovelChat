# 模块与文件分析

> 生成时间：结构调整后（commit `5dfd4fa` 之后）。
> 数据来自对源码的静态扫描（AST 提取函数/类、import 图、SQLite schema），不是凭印象写的。
> 行数为 `splitlines()` 计数，包含空行与注释。

---

## 0. 全局：仓库布局与分层规则

```
backend/app/          后端（导入根，uvicorn --app-dir backend/app）
  core/               基础设施：配置、SQLite、鉴权、路径、分词
  readers/            文档解析（txt/md/pdf）
  channels/           渠道抽象（web 已启用，qq/wechat 预留）
  services/           业务逻辑（不碰 HTTP、不直接调 Agent）
  agent/              要调用大模型的逻辑（提示词、单聊、群聊、抽取、工具）
  api/v1/             HTTP 层（deps / schemas / endpoints×6 / router）
frontend/src/         前端（Vue 3 + Vite）
  pages/ components/ stores/ api/ utils/ styles/
backend/data/         运行时数据（不入库）：app.db、原文、模型、贴纸
backend/tests/        自检脚本（不依赖 pytest）
scripts/              安装、启动、取模型、诊断
docs/                 设计与验收文档
```

**分层规则**：`core/readers/channels` → `services` → `agent` → `api`。
上层可以依赖下层，反过来不行（当前有 2 处违反，见 §7）。

**规模**：后端 40 个 Python 文件约 11k 行；前端 15 个文件约 2.5k 行；自检脚本约 4k 行 / 685 项检查。

---

## 1. 三条主链路（先看这个，再看模块）

### 1.1 上传小说 → 抽出角色 → 进书架

```
POST /api/books                    api/v1/endpoints/books.py:upload_book
  └─ library.create_book           落盘 data/library/<book_id>/source.txt，建 books 行（status=extracting）
  └─ library.schedule_extraction   起后台任务
       └─ library._run_extraction
            ├─ cleaner.clean_text              去盗版站广告/水印/页眉页脚
            ├─ extractor.chunk_text            按字数切块（chunk_chars/overlap）
            ├─ agent/extraction.ExtractionAgent.run   mode=agent 时的 ReAct 循环（调工具：联网查名单、查头像）
            │     └─ extractor.call_llm_json   一次一块，要求回 JSON
            ├─ extractor.merge_characters      跨块合并同名/别名
            ├─ extractor.enrich_characters     补性格/关系/名场面（hybrid/agent 模式）
            ├─ 落库 characters + agent_memory 初始条目 + rag.index_character
            └─ library._sync_shelf_after_extraction   ← 书架事务（P0 修复的关键）
                  ├─ shelf.sync_members            这书的角色进群（shelf_members）
                  ├─ shelf.supplement_memory       给已有角色补本书片段
                  └─ merge.merge_shelf             合并「同一个人」
                        ├─ ① 名字/别名相同 → 直接合
                        ├─ ② manager_memories 里有结论 → 直接合
                        ├─ ③ similarity > 0.7 → 直接合
                        ├─ ④ 0.5~0.7 → llm_verifier 判定 / 进 pending
                        └─ 结论写回 manager_memories
```

状态推进写在 `books.status/progress/stage`，前端 `ShelfPage.vue` 轮询 `GET /api/books`。

### 1.2 群聊（一个书架 = 一个群）

```
POST /api/shelves/{id}/chat        api/v1/endpoints/chat.py:shelf_chat
  ├─ deps.resolve_session          token → Session（HMAC 签名）
  ├─ agent/group.run_turn          谁说话、说几轮
  │    ├─ score_member             成员分 = 性格外向度 + 情绪基线 + 相关度 + 冷却
  │    ├─ select_speakers          被 @ 必答；否则按分数阈值；成本闸门 group_max_speakers
  │    └─ 每个 speaker: manager.get_agent（LRU 缓存）→ Agno run
  └─ SSE 事件流                    speaker_start → delta* → speaker_end → done
       （channels/web.py 负责帧格式；前端 utils/sse.js 解析）
```

落库：`chat_turns`（谁说的、说了什么）→ `chat_memory.maybe_distill` 攒够就压成长期记忆。

### 1.3 单聊

```
POST /api/chat                     api/v1/endpoints/chat.py:chat_endpoint
  ├─ agent/manager.get_agent       按角色构建 Agent（提示词来自 agent/prompts.build_character_prompt）
  ├─ agent/character.attach_state  把情绪/好感度/记忆/场景塞进系统提示
  └─ SSE: delta / final / sticker   final 是去掉动作描写后的正文（services/reply.py）
```

---

## 2. 后端：core（基础设施）

| 文件 | 行 | 职责 | 关键符号 |
|---|---|---|---|
| `core/config.py` | 234 | 全部配置项（pydantic-settings，读 `backend/.env`） | `Settings`、`get_settings()` |
| `core/database.py` | 2172 | **唯一的数据层**：建表 + 幂等迁移 + 所有 SQL | `init_db`、`connect`、`create_book`、`list_characters`、`replace_novel_characters`、`add_manager_memory`…（`_row_to_*` 一堆转换器） |
| `core/paths.py` | 34 | 路径唯一出口（搬家后不再各算 `parents[N]`） | `APP_DIR`、`BACKEND_DIR`、`DATA_DIR`、`MODELS_DIR`、`STICKER_DIR` |
| `core/security.py` | 147 | 会话签名（HMAC + TTL），不落库 | `issue_session`、`verify_session`、`Session` |
| `core/text.py` | 77 | 零依赖中文分词，只服务 BM25 | `tokenize`、`term_frequency` |
| `core/hf_import.py` | 73 | 安全导入 `transformers`（并发首次导入会半初始化，这里加锁+重试） | `load`、`auto_classes` |

**注意**
- `database.py` 2172 行、12 张表全在一个文件里，是整个后端最重的一块。按表拆成 `core/models/*.py` 是合理的下一步，但改动面大（几乎所有模块都 `from core import database as db`）。
- 没有 ORM、没有迁移框架。`init_db()` 里靠 `CREATE TABLE IF NOT EXISTS` + 增量 `ALTER` 做到幂等——目前够用，但 schema 演进要靠人肉判断。

---

## 3. 后端：services（业务逻辑，20 个文件）

### 3.1 抽取链路

| 文件 | 行 | 职责 | 关键符号 |
|---|---|---|---|
| `library.py` | 575 | 作品库：原文落盘、分块、调度抽取、重试、书架事务 | `create_book`、`_run_extraction`、`_sync_shelf_after_extraction`、`schedule_extraction`、`retry_extraction` |
| `extractor.py` | 515 | 抽取流水线（纯逻辑，不碰 DB/文件） | `chunk_text`、`plan_chunks`、`merge_characters`、`attach_excerpts`、`call_llm_json`、`extract_from_chunks`、`enrich_characters` |
| `character_miner.py` | 931 | 纯算法挖角色（零 API 成本，`EXTRACTION_MODE=algo` 走这里） | `discover_names`、`group_forms`、`attribute_dialogues`、`MinedCharacter` |
| `name_engine.py` | 352 | 人名识别引擎：jieba / HanLP / 内置规则，逐级降级 | `get_engine`、`Engine`、`JiebaBackend` |
| `name_library.py` | 473 | 判断「这是不是人名、中文还是译名」 | `analyze`、`is_plausible`、`NameVerdict` |
| `cleaner.py` | 261 | 正文清洗：广告、水印、重复页眉、分隔线 | `clean_text`、`looks_like_ad`、`clean_memories` |

### 3.2 书架与合并

| 文件 | 行 | 职责 | 关键符号 |
|---|---|---|---|
| `shelf.py` | 192 | 书架事务：书进/出时成员与记忆跟着动 | `sync_members`、`supplement_memory`、`add_book`（现已无生产调用）、`remove_book` |
| `merge.py` | 821 | 「同一个人」合并：身份键 → 相似度 → 模型判定 → 经验沉淀 | `identity_keys`、`group_duplicates`、`similarity`、`merge_shelf`、`merge_pair`、`llm_verifier`、`remember_merge`、`pending_pairs` |

`merge.py` 的判定分层是本项目最"产品化"的一段：名字（可信）→ 记忆（可信）→ 相似度 >0.7（自动）→ 0.5~0.7（问模型/人工）→ <0.5（忽略）。

### 3.3 记忆与检索

| 文件 | 行 | 职责 | 关键符号 |
|---|---|---|---|
| `agent_memory.py` | 243 | 角色记忆条目：初始片段、增删、重建、摘要 | `items`、`top`、`remember`、`rebuild`、`summarize` |
| `chat_memory.py` | 216 | 短期对话 → 长期记忆（攒够阈值就压一半） | `pending_turns`、`distill`、`maybe_distill` |
| `rag.py` | 503 | 向量 + BM25 混合检索，块级索引 | `index_character`、`search`、`backend_status`、`retry_embedder`、`LocalEmbedder` |
| `scene.py` | 60 | 一句话归到场景标签（决定检索权重） | `detect`、`detect_many` |
| `summarizer.py` | 57 | 记忆摘要（有 Key 走模型，没有就本地压） | `make_llm_summarizer` |

### 3.4 情绪与表达

| 文件 | 行 | 职责 | 关键符号 |
|---|---|---|---|
| `affect.py` | 493 | 双层状态：情绪（valence/arousal/9 标签）+ 好感度（对用户隐藏） | `extract_signals`、`load_state`、`describe`、`prompt_block`、`tone_band` |
| `emotion_model.py` | 423 | 情绪极性：规则后端 + ONNX 小模型后端，可降级 | `analyze`、`get_backend`、`OnnxBackend`、`RulesBackend` |
| `expression.py` | 134 | 把「此刻情绪」翻译成怎么说话：贴纸概率、语气 | `maybe_sticker`、`pick_sticker`、`asks_about_mood` |
| `reply.py` | 74 | 去掉回复里的动作/神态描写 | `strip_stage_directions` |

### 3.5 基础设施

| 文件 | 行 | 职责 |
|---|---|---|
| `agent_log.py` | 182 | 审计流水（`agent_events`），按书架隔离，带脱敏 |
| `media.py` | 108 | 上传图片校验（类型魔数、大小、base64） |

---

## 4. 后端：agent（大模型相关）

| 文件 | 行 | 职责 | 关键符号 |
|---|---|---|---|
| `prompts.py` | 251 | 提示词模板（角色扮演 + 抽取 + 富化） | `build_character_prompt`、`build_extraction_messages`、`build_enrichment_messages`、`pick_avatar` |
| `character.py` | 114 | 单角色对话闭环：拼状态、取记忆、生成描述 | `attach_state`、`memory_preview`、`describe`、`prepare`（无引用） |
| `manager.py` | 156 | 一个角色 = 一个 Agent，LRU 缓存 | `get_agent`、`build_agent`、`cache_key`、`clear_cache`（无引用） |
| `group.py` | 525 | 群聊引擎：选人、@ 唤醒、串行生成 | `score_member`、`select_speakers`、`parse_mentions`、`run_turn` |
| `extraction.py` | 906 | 角色档案管理员：ReAct 循环 + 工具调用 + 结果裁剪 | `ExtractionAgent`、`_shrink`、`_dumps`、`_verdict` |
| `tools/web.py` | 187 | 联网检索：搜索 → 取正文 → 提炼名单 | `roster_from_web`、`default_search`、`diff_with_local`（仅测试用） |
| `tools/avatar.py` | 443 | 头像爬取：多源 → 打分 → 下载 | `AvatarCrawler`、`download_avatar`、`_name_match_score` |

`extraction.py` 的 `_shrink/_dumps` 是为了**结构化截断**：模型返回的 JSON 太长时按结构裁剪，绝不把 JSON 截断成非法文本。

---

## 5. 后端：api（HTTP 层，49 个路由）

| 文件 | 行 | 路由数 | 覆盖 |
|---|---|---|---|
| `api/v1/deps.py` | 150 | — | 会话解析、payload 组装、SSE 编码、`require_*` 校验 |
| `api/v1/schemas.py` | 91 | — | 12 个 Pydantic 模型 |
| `api/v1/router.py` | 10 | — | 汇总 6 个子路由 |
| `endpoints/system.py` | 118 | 5 | `/health` `/session` `/channels` `/stickers` `/sticker/{name}.svg` |
| `endpoints/books.py` | 199 | 8 | 上传/列表/详情/删除/重抽/角色列表/建角色/名单审核 |
| `endpoints/shelves.py` | 377 | 18 | 书库、书架 CRUD、日志、加/移书、合并三件套、经验记忆、历史、成员 |
| `endpoints/characters.py` | 312 | 11 | 角色 CRUD、状态、记忆、头像候选、头像代理、合并/撤销、单聊历史 |
| `endpoints/chat.py` | 353 | 2 | `/chat`（单聊 SSE）、`/shelves/{id}/chat`（群聊 SSE） |
| `endpoints/knowledge.py` | 71 | 5 | RAG 状态/重建、回忆、蒸馏、记忆清洗 |

**约定**：`deps.py` 用 `from api.v1.deps import *` 供各 endpoints 使用。好处是新增接口不用重复十几行 import；代价是 monkeypatch 要打在**具体模块**上（测试里的坑，已改）。

---

## 6. 后端：入口、解析、渠道

| 文件 | 行 | 职责 |
|---|---|---|
| `main.py` | 187 | `create_app()`、lifespan（init_db → 恢复中断抽取 → 预热 RAG/情绪模型）、CORS、挂 `/api` |
| `readers/document.py` | 81 | txt/md/pdf → 文本，编码探测，`DocumentError` |
| `channels/base.py` | 171 | `Channel` 抽象、`Inbound/Outbound`、`event_to_outbound` |
| `channels/web.py` | 80 | 浏览器 + SSE（当前唯一启用的渠道） |
| `channels/qq.py` / `wechat.py` | 55 / 57 | **预留空壳**，`send` 抛 NotImplemented |
| `channels/registry.py` | 39 | 按名字取渠道 |

这套抽象的意义：以后接 QQ/微信时，只需要实现 `Channel.send`，群聊引擎和事件格式都不用动。

---

## 7. 前端（15 个文件，约 2.5k 行）

| 文件 | 行 | 职责 |
|---|---|---|
| `App.vue` | 68 | 外壳：顶栏、banner、按 `view` 切页面、挂浮层 |
| `stores/app.js` | 1251 | **所有状态与动作**（96 个导出 + 工具函数），单例 |
| `pages/ShelfPage.vue` | 186 | 书架列表、上传、抽取进度、书架增删改 |
| `pages/GroupChatPage.vue` | 209 | 群聊：消息流、@ 提示、输入框 |
| `pages/GroupInfoPage.vue` | 223 | 群信息：成员网格（5×3）、改名、待确认合并、清空对话 |
| `pages/CharacterChatPage.vue` | 241 | 单聊：消息流、贴纸、图片、新对话 |
| `components/modals/CharacterEditor.vue` | 203 | 角色编辑：字段、别名、头像候选 |
| `components/modals/CharacterStatus.vue` | 241 | 角色详情：记忆、情绪、在线状态、合并/撤销/删除 |
| `components/modals/MergePicker.vue` | 152 | 手动合并：选目标角色 |
| `api/client.js` | 78 | `api()` / `upload()` / 路径表 |
| `utils/sse.js` | 40 | SSE 流解析（单聊群聊共用） |
| `utils/format.js` | 50 | 状态文案、字数、时间、头像代理、成员状态 |
| `utils/markdown.js` | 158 | Markdown 渲染 + XSS 净化 |
| `styles/app.css` | 419 | 全局样式（原 App.vue 的 scoped 提升而来） |
| `tests/render-smoke.js` | 96 | SSR 渲染冒烟：四个页面 + 三个浮层各渲染一遍 |

**组件拿状态的方式**：`const { view, group, openShelfChat } = useApp()`——拿到的就是同一份 ref/reactive，所以模板不用加前缀。这是拆文件时最省事的做法，但不是终态（见 §8）。

---

## 8. 数据层：12 张表

| 表 | 行数 | 作用 |
|---|---|---|
| `libraries` | 1 | 书库（最外层，通常只有一个） |
| `shelves` | 1 | 书架 = 一个群，**隔离边界** |
| `books` | 4 | 作品，带 status/progress/stage |
| `characters` | 66 | 角色（含 `canonical_id` 表示已并入别人） |
| `shelf_members` | 66 | 谁在哪个群里（status/joined_at/left_at） |
| `character_memory_items` | 247 | 角色记忆条目（原文片段 + 对话蒸馏） |
| `memory_chunks` | 1696 | 块级向量（RAG 用） |
| `character_affect` | 45 | 每个角色对每个用户的情绪 + 好感度 |
| `group_member_state` | 44 | 群内状态（在线/冷却/发言次数） |
| `chat_turns` | 43 | 对话流水（蒸馏后标记） |
| `agent_events` | 40 | 审计事件（决策、工具调用） |
| `manager_memories` | 137 | 管理 Agent 的书架级经验（同人/不同人） |

索引 11 个，覆盖了主要查询路径（`shelf_id`、`character_id`、`book_id`）。

---

## 9. 脚本与自检

| 文件 | 行 | 用途 |
|---|---|---|
| `backend/tests/smoke_test.py` | ~4000 | 685 项自检，23 个 `*_checks()` 分组，直接 `python` 跑 |
| `scripts/setup.ps1` / `dev.ps1` / `pip.ps1` | — | 装环境、起前后端、pip 包装 |
| `scripts/fetch_model.py` / `fetch_emotion_model.py` | 73 / 156 | 取 RAG 嵌入模型 / 导出+量化情绪模型 |
| `scripts/check_names.py` / `check_nlp.py` / `check_avatar.py` | 74 / 112 / 70 | 人名库、分词引擎、头像爬虫的诊断 |
| `scripts/analyze_novel.py` | 144 | 不开服务，直接看算法在一本书里挖到谁 |
| `scripts/check-frontend.cjs` | 200 | SFC 编译 + 结构 + **绑定完整性** |
| `frontend/tests/run-render.mjs` | 20 | SSR 渲染冒烟运行器（`npm run check:render`） |

---

## 10. 发现的问题（按严重度）

### P0 —— 会咬人的

**1. 分层倒挂：services 依赖 agent**

```
services/extractor.py:16   from agent.prompts import ...
services/library.py:21     from agent.prompts import pick_avatar
```

`agent/prompts.py` 是纯模板代码（不调模型），却放在最高层，导致 services 反向依赖 agent。
**影响**：想单独测 extractor 就得把整个 agent 层拖进来；将来要换 Agent 实现时，services 会被牵连。
**建议**：把 `prompts.py` 下沉到 `services/prompts.py`（或 `core/prompts.py`），agent 层再 import 它。

**2. 三处反向依赖被"函数内 import"藏起来了**

| 位置 | 方向 | 实际依赖 |
|---|---|---|
| `services/library.py`（4 处函数内 import） | services → services | `shelf.py` **模块级** import 了 `library`（用 `library.read_source` / `delete_book`），`library` 只能在函数里 import `shelf` —— 这是真正的循环 |
| `core/database.py:946` `_backfill_memory_scene` | **core → services** | 建库时要给老记忆补场景标签，用了 `services.scene.detect`（纯规则，无副作用） |
| `services/extractor.py:350` `merge.py:653` | services → services | 拆文件时的就近 import，非必需 |

注释里写得很诚实（"延迟导入避免 core 依赖 services"），但依赖方向依然错了。
**建议**：
- `scene.py` 是纯规则（60 行、无 IO），下沉到 `core/scene.py`，core 的反向依赖就消失了；
- 把 `shelf` 需要的两个 library 函数（`read_source` / `delete_book`）改成由调用方传入，或下沉成 `core` 里的小函数，拆掉 shelf↔library 的环。

### P1 —— 该清理的

**3. 死代码**

| 符号 | 现状 |
|---|---|
| `services/shelf.add_book` | 生产代码无调用（只剩测试用）；书架事务已由 `_sync_shelf_after_extraction` 接管 |
| `agent/manager.clear_cache` / `cached_agent_count` | 全局无引用 |
| `agent/character.prepare` | 无引用 |
| `agent/tools/web.diff_with_local` | 只被测试调用 |

**4. 重复实现**

- `_local_summary`：`agent_memory.py` 和 `chat_memory.py` 各写了一份「本地压缩」，逻辑相近 → 应合并成一个工具函数。
- `_now` / `_now_ms`：`database.py`、`group.py`、`chat_memory.py` 各自定义 → 应统一到 `core` 的小工具。
- `_normalise`：`avatar.py`（URL 归一）和 `document.py`（文本归一）同名但语义不同 —— 建议改名，避免看代码时误认。

**5. 超大文件**

| 文件 | 行 | 拆分建议 |
|---|---|---|
| `core/database.py` | 2172 | 按表拆 `core/models/{book,character,shelf,memory,event}.py`，`database.py` 只留连接与迁移 |
| `services/character_miner.py` | 931 | 拆 `miner/{names,dialogue,scoring}.py` |
| `agent/extraction.py` | 906 | ReAct 循环与「结果裁剪/JSON 修复」分开 |
| `services/merge.py` | 821 | 身份键/相似度、判定器、合并动作分三个文件 |

### P2 —— 结构性欠账

**6. 前端 store 是「一个大对象」**（1251 行）
按域拆成 `stores/{library,group,character,ui}.js` 更合理，但要先解决跨 store 依赖（群聊要读书架）。可考虑同时引入 Pinia。

**7. 自检脚本 4000 行单文件**
23 个 `*_checks()` 全在一个文件里。拆成 `tests/{extraction,merge,chat,shelf,api,frontend}_checks.py` + 一个 runner，跑失败时定位更快。

**8. 无鉴权分级**
token 只表达「你是谁」，没有角色/权限。本地单人项目够用，上服务器前要补（至少给管理类接口加一层）。

**9. `channels/qq.py`、`wechat.py` 是空壳**
保留是需求（以后要接），但要注意：它们**能通过 import**，只是 `send()` 抛异常。如果误配置成启用，错误会在发送时才发现。

---

## 11. 想改东西时去哪找

| 我想改… | 去哪 |
|---|---|
| 抽取质量（漏人/错人/别名） | `agent/extraction.py` + `agent/prompts.py` + `services/extractor.py` |
| 人名判定规则 | `services/name_library.py`（词表）、`services/name_engine.py`（引擎） |
| 合并太激进/太保守 | `services/merge.py` 的 `AUTO_MERGE_SCORE` / `SIMILARITY_FLOOR` |
| 群聊谁说话 | `agent/group.py:score_member / select_speakers` |
| 角色说话像不像 | `agent/prompts.py` + `services/affect.py:prompt_block` |
| 记忆检索不准 | `services/rag.py`（权重）+ `services/scene.py`（场景） |
| 情绪/好感度手感 | `services/affect.py`（分档）+ `services/emotion_model.py` |
| 接口加字段 | `api/v1/schemas.py` + 对应 `endpoints/*.py` |
| 表结构改动 | `core/database.py:init_db`（幂等迁移） |
| 页面/交互 | `frontend/src/pages/*.vue` + `stores/app.js` 对应动作 |
| 样式 | `frontend/src/styles/app.css` |
| 联调失败 | 先看 `GET /api/health` 的 `rag/emotion` 后端，再看 `agent_events` 流水 |

---

## 12. 建议的下一步（按投入产出排序）

1. **下沉 `prompts.py`**（半小时，拆掉唯一的分层倒挂）
2. **清死代码 + 合并重复 helper**（1 小时，净减约 150 行）
3. **拆 `core/database.py`**（半天，收益最大但要动所有模块，务必在测试全绿时做）
4. **拆前端 store**（半天，同时决定要不要上 Pinia）
5. **自检脚本分文件**（2 小时，纯搬运）
6. **接 Docker 实测**（需要装 Docker）