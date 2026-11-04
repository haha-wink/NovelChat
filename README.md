# 小说角色扮演（Novel Roleplay）

上传小说 → 自动抽出角色 → 角色进入书架群 → 和它们群聊 / 单聊。
每个角色有独立的**记忆**（原文片段 + 对话蒸馏）、**情绪与好感度**、**在线状态**。

- 后端：FastAPI + Agno + DeepSeek，SQLite（无 ORM）
- 前端：Vue 3 + Vite，单页应用
- 本地小模型：RAG 嵌入（bge-small-zh）+ 情绪分类（Erlangshen，ONNX int8）

---

## 目录结构

```
.
├── backend/                  # 后端服务
│   ├── app/                  # 应用包（导入根：core.* / services.* / agent.*）
│   │   ├── main.py           # 应用入口（lifespan、CORS、挂路由）
│   │   ├── api/              # HTTP 层
│   │   │   ├── deps.py       # 会话解析 / payload 组装 / SSE 编码
│   │   │   └── v1/
│   │   │       ├── router.py         # 路由汇总
│   │   │       ├── schemas.py        # Pydantic 请求/响应模型
│   │   │       └── endpoints/        # 按域拆分的接口
│   │   │           ├── system.py     # health / session / channels / sticker
│   │   │           ├── books.py      # 上传、列表、重抽、名单审核
│   │   │           ├── shelves.py    # 书库书架、成员、历史、合并、经验记忆
│   │   │           ├── characters.py # 角色 CRUD、状态、头像、合并
│   │   │           ├── chat.py       # 单聊 / 群聊 SSE
│   │   │           └── knowledge.py  # RAG 索引、记忆清洗
│   │   ├── agent/            # Agent 层（要调用大模型的核心逻辑）
│   │   │   ├── character.py  # 单角色对话
│   │   │   ├── group.py      # 群聊引擎（选角、@ 唤醒、串行生成）
│   │   │   ├── manager.py    # 按角色构建 Agent（LRU 缓存）
│   │   │   ├── extraction.py # 角色档案管理员（ReAct 循环）
│   │   │   ├── prompts.py    # 提示词模板
│   │   │   └── tools/        # Agent 工具：联网检索、头像抓取
│   │   ├── services/         # 业务逻辑层（不碰 HTTP）
│   │   ├── core/             # 配置、数据库、鉴权、路径
│   │   │   ├── config.py
│   │   │   ├── database.py   # SQLite 数据层（建表 + 迁移 + 读写）
│   │   │   ├── paths.py      # 路径的唯一真相
│   │   │   ├── security.py
│   │   │   └── text.py       # 轻量中文分词（给 BM25）
│   │   ├── readers/          # txt / md / pdf 解析
│   │   └── channels/         # 渠道抽象（web 已启用，QQ/微信预留）
│   ├── data/                 # 运行时数据（不入库）：数据库、原文、模型、贴纸
│   ├── tests/smoke_test.py   # 自检脚本（不依赖 pytest，直接跑）
│   ├── requirements.txt
│   └── .env                  # 密钥与开关（不入库）
├── frontend/                 # 前端应用（Vue 3 + Vite）
│   ├── src/
│   │   ├── App.vue           # 外壳：顶栏 + 视图切换 + 浮层
│   │   ├── pages/            # 四个视图
│   │   │   ├── ShelfPage.vue          书架 / 上传 / 书架管理
│   │   │   ├── GroupChatPage.vue      群聊
│   │   │   ├── GroupInfoPage.vue      群聊信息（成员、合并候选）
│   │   │   └── CharacterChatPage.vue  单聊
│   │   ├── components/modals/ # 浮层：合并选择、角色编辑、角色详情
│   │   ├── stores/app.js     # 状态与动作（单例，组件解构同名绑定）
│   │   ├── api/client.js     # 请求封装
│   │   ├── utils/            # format.js、sse.js、markdown.js
│   │   └── styles/app.css    # 全局样式
│   └── tests/render-smoke.js # SSR 渲染冒烟（npm run check:render）
├── docs/                     # 文档
└── scripts/                  # 安装 / 启动 / 取模型 / 自检脚本
```

> **为什么 `backend/app` 是导入根**：启动用 `uvicorn main:app --app-dir backend/app`，
> 模块名保持 `core.xxx` / `services.xxx`（不带 `app.` 前缀），迁移成本最低。

---

## 快速开始

```powershell
# 1. 装环境（建 venv + 装依赖）
.\scripts\setup.ps1

# 2. 取本地模型（一次性，约 190MB）
.\scripts\fetch_model.ps1
.\ai-agent-cyber-town.venv\Scripts\python.exe .\scripts\fetch_emotion_model.py

# 3. 配置密钥
copy backend\.env.example backend\.env   # 填 DEEPSEEK_API_KEY

# 4. 起服务（后端 8000 + 前端 5173）
.\scripts\dev.ps1
```

打开 http://localhost:5173 ，上传一部 txt / md / pdf 小说即可。

---

## 自检

```powershell
# 后端：685 项
.\ai-agent-cyber-town.venv\Scripts\python.exe backend\tests\smoke_test.py

# 前端：静态结构 + 绑定完整性（不跑浏览器）
node scripts\check-frontend.cjs

# 前端：真渲染（SSR 把四个页面 + 三个浮层各渲染一遍，抓 undefined 绑定）
cd frontend; npm run check:render

# 前端：生产构建
cd frontend; npm run build
```

---

## 配置

全部在 `backend/.env`（模板见 `backend/.env.example`）。常用项：

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | 必填，抽取与对话都用它 |
| `EXTRACTION_MODE` | `algo`（纯算法，免费）/ `hybrid` / `agent`（默认，最准） |
| `EMOTION_MODEL` | `auto` / `onnx` / `rules` |
| `RAG_BACKEND` | `auto` / `vector` / `lexical` |
| `GROUP_MAX_SPEAKERS` / `GROUP_MAX_ROUNDS` | 群聊成本闸门 |

---

## 部署（可选）

`docker-compose.yml` 提供一键起容器（后端 + 前端静态站点）。
**注意**：这是给后续上服务器用的脚手架，本机没装 Docker，未实际验证。

```bash
docker compose up -d --build
```

数据库与原文卷挂载在 `backend/data`，升级镜像不会丢数据。

---

## 文档

| 文件 | 内容 |
|---|---|
| `docs/agent-architecture.md` | 完整设计：分层、数据模型、提示词、RAG、各阶段实现 |
| `docs/manager-agent-workflow.md` | 管理 Agent 的工作流（抽取链、书架链、合并链） |
| `docs/module-analysis.md` | **模块与文件分析**：分层、三条主链路、逐模块职责、问题清单 |
| `docs/project-map.md` | 文件级地图：每个文件干什么 |
| `docs/manual-test.md` | 人工验收清单 |
| `docs/nlp-engine.md` | 人名识别引擎的取舍与降级链 |