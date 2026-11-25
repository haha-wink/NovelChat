# NovelChat —— 和小说里的人聊你的事

上传小说 → 自动抽出角色 → 角色进群 → 和它们单聊 / 群聊；切到工作模式，它们还能帮你干活——做计划、记笔记、写文案、渲染动画 MV。

**它不是角色扮演。** 角色不演剧情、不推进故事线；它用原著里的性格、语气和记忆，听你说你自己的事——今天累不累、为什么难过、要不要聊聊。

- 后端：FastAPI + LangGraph + Agno + DeepSeek，SQLite（无 ORM，手写 SQL + 幂等迁移）
- 前端：Vue 3 + Vite 单页应用，SSE 流式渲染
- 本地小模型：RAG 嵌入（bge-small-zh-v1.5）+ 情绪分类（Erlangshen，ONNX int8，CPU 推理）

## 目录

- [核心流程](#核心流程)
- [功能特色](#功能特色)
- [架构总览](#架构总览)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [自检](#自检)
- [项目结构](#项目结构)
- [文档](#文档)
- [Roadmap（预留计划）](#roadmap预留计划)
- [数据与密钥](#数据与密钥)
- [特别鸣谢](#特别鸣谢)

---

## 核心流程

```
上传小说 (txt / md / pdf)
   → 角色抽取（4 种模式，名单可人工审核）
   → 角色自动进群（书架 = 群）
   → 单聊 / 群聊（SSE 流式，情绪与意图逐轮识别）
   → 工作模式（管理员 Agent 统筹，角色 Agent 执行，成片/文档/计划产出）
```

## 功能特色

### 角色抽取与管理

- 支持 txt / md / pdf，GB18030 回退解码；入库前自动清洗正文（广告行、分隔线、重复页眉）。
- 四种抽取模式（`EXTRACTION_MODE`）：
  - `algo` 纯算法挖掘，零 API 成本，性格是「平均 8 字/句、几乎不用感叹号」这类可量化描述；
  - `hybrid`（默认）算法挖角色与台词，一次模型调用补性格/背景；
  - `agent` 角色档案管理员 Agent：审核名单、联网对照原著、调阈值、抓头像（ReAct 循环）；
  - `llm` 逐块抽取，质量上限最高。
- 内置中文人名引擎：jieba 人名标注 + 单姓/复姓/外国人名库，称谓与口语碎片自动过滤，别名/称呼合并为同一角色。
- 跨书同名角色自动处理：相似度超阈值直接合并，存疑对进入待确认列表；合并做记忆相加并生成摘要，**可撤销回档**（角色恢复独立、记忆迁回）。
- 头像从公开数据源（Bangumi 等）抓候选，域名白名单 + 本地代理，由用户选定。
- 说话风格从原著台词统计：条数、平均句长、口头禅、标点习惯，原话直接进提示词——比堆性格形容词有效得多。

### 单聊与群聊

- 每轮识别用户**情绪与意图**（倾诉 / 求助 / 闲聊 / 危机），据此生成应对策略（SOP 块）；危机场景走安全流程，指向现实求助渠道，且不允许玩梗。
- 情绪分析：本地 ONNX 情绪模型（正负向 + 否定词翻转）与规则双通道，单次推理 < 200ms；模型缺失自动降级为规则。
- 对话判官三档（`DIALOGUE_JUDGE_MODE`）：`off` 纯规则 / `auto` 只在规则拿不准（反讽、长期低落、意图歧义）时问一次模型 / `always` 每轮精判。
- 群聊是独立状态图（五节点，含聚合查重）：
  - **并行生成**——被选中的角色真并发出稿，等待时间接近单人；
  - **聚合查重**——内容雷同的回复被折叠，保留角度不同的那条；
  - **分工角度**——温柔的先共情、冷静的给方案、活泼的活跃气氛，同一轮角度不重样；危机场景不安排打趣与追问。
- 发言规则：意愿分 = 0.30×外向 + 0.25×相关 + 0.15×情绪 + 噪声(±0.10)，过线才开口；被 @ 必答且排第一，@全体成员 全员应答；在线/离线在场状态、发言冷却、角色 @ 角色触发下一轮必答。
- **等回答者检测**：上一轮有角色提问且用户没答，这一轮提问者优先发言。
- 好感度体系：按书架独立累计，冷淡/疏离/平常/亲昵分档，低好感禁止亲昵称呼、高好感禁止无来由敌意；变化逐条写审计日志，提示词只给心情描述、不暴露数值。
- 时段问候（早/午/晚/深夜、长对话疲劳关怀）；Twemoji 贴纸按情绪概率发送（素材内置，离线可用）。

### 记忆系统（4 层）

| 层 | 内容 | 说明 |
|---|---|---|
| 初始记忆 | 角色原文片段 | 抽取时生成，随重抽替换、随删书回收 |
| 长期记忆 | 对话蒸馏出的事实与画像 | 向量化入 RAG，可检索、可清洗 |
| 短期流水 | 逐轮对话原文 | 保留可回看，达阈值蒸馏进长期记忆 |
| 工作记忆 | 工作模式的任务笔记 | 按 Agent 分栏，管理员可读全部 |

- 混合检索：0.65×向量 + 0.35×BM25，按场景一致性加权（一致 ×1.30、不一致 ×0.85、摘要 ×1.05），设最低分阈值与召回预算，控制单次上下文成本。
- bge-small-zh-v1.5 本地嵌入（512 维），单次查询编码 < 200ms；向量后端不可用自动降级 BM25 词法检索。
- 记忆清洗：先 dry-run 出报告再落库，改动后旧向量自动作废重建。

### 单轮对话状态图（LangGraph）

```
preprocess → classify → intent → observe → recall → plan → skill → generate → verify → persist
```

- `verify` 做安全与语气复核：不安全内容整段替换，说教腔软化（「你应该」→「你可以」），危机回复缺求助提示时自动补上。
- checkpoint 落 SQLite：流式生成中断后，前端带同一 `turn_id` + `resume=true` 从断点续跑，本轮已完成的部分不重做、不重复消耗调用。
- 断点按天自动清理，保留期可配（`GRAPH_CHECKPOINT_RETENTION_DAYS`）。

### 工作模式：多 Agent 协作

- 管理员 Agent（虚拟成员，不占角色卡）以 ReAct 循环统筹：规划 → 派发 → 观察 → 再规划（上限 3 轮）；实时过程（思考/派发/观察）随 SSE 推送。
- **工作记忆分栏**：`agent_id` = 公共 / `admin` / `character:<id>`；管理员读同一对话全部分栏做决策与校验，角色只读自己那栏与管理员计划。
- **单执行者约束**：一轮只有第一个动手的角色拿到工具权限，其余角色只出方案，避免同一需求产出多个版本；执行汇报写回工作记忆，由管理员统一汇总、只发一次（断线重连不重复）。
- 角色 Agent 工具：起动画任务、查任务进度、改需求/风格重渲。
- 同一 ReAct 框架复用于抽取链的角色档案管理员（miner_report / text_evidence / avatar / tune 等工具）。

### 技能层（内置 7 个技能，可插拔）

- 聊天模式：comfort（共情倾诉）/ steady（危机稳定）/ suggest（求助建议）；工作模式：planning（计划）/ notes（笔记）/ writing（写作）/ animation（动画 MV）。
- 每个技能是一份声明式 `SKILL.md`：keywords / action_words / intent 打分决定选谁，`requires` 声明外部依赖（node / ffmpeg / chrome），依赖不齐技能自动隐身、不影响其他功能。
- 注册表启动即体检：未知模式、未注册工具、未知占位符、未知依赖直接判 error。
- `SKILLS_EXTRA_DIRS` 支持仓库外技能包热加载，加目录即生效、撤目录即消失。

### 动画 MV 流水线

- 工作模式里说一句「做个 MV」即可成片：LLM 生成分镜 → p5.js 程序化作画 → chrome-headless-shell 逐帧截取 → ffmpeg 编码。
- 9 种风格（水彩 / 像素 / 水墨 / 赛博朋克 / 胶片 / 黑白 / 小清新 / 动漫 / 日落）+ 独立像素模式，认用户话里的风格词。
- 歌词 MV：LRC 时间轴逐句对齐、副歌/主歌切片（时间轴平移 + 音频按起点裁剪）、音频内嵌歌词自动读取（MP3 USLT / MP4 ©lyr / 同名 `.lrc`）、配乐上传与音频偏移。
- 画面按约束生成：prompt 与歌词里的元素（雨、夜、车站、山、海、城市……）决定构图、天气与配色；非歌词任务按标点拆成多个镜头。
- 720p 快速档（12fps，约为 1080p 三倍速）与 1080p 画质可切换；多进程分片渲染、超时自适应、失败重试、真实进度百分比。
- 任务面板按对话隔离：起任务即自动开渲，可改需求/风格重渲，僵尸与失败任务自动清理；成片走 HTTP 出口，支持 Range 播放与下载。
- 同一需求连续 3 次相似会触发卡住检测，提示换思路。

### 工程质量

- **零依赖自检**：不依赖 pytest 的自检脚本（7600+ 行），**1121 项断言全部通过**，覆盖抽取、记忆、检索、群聊、图编排、技能、动画、HTTP 层。
- 前端三重检查：静态结构与绑定完整性检查、SSR 真渲染冒烟（4 页面 + 3 浮层，专抓 undefined 绑定）、生产构建。
- 分层依赖治理：core → services → agent → api 单向依赖，AST 自检反向 import 与循环依赖。
- 失败降级贯穿全链路：模型缺失 → 规则，向量不可用 → BM25，头像源全挂 → 返回空并记录原因，单角色生成失败不影响整轮群聊。
- 规模：后端 92 个 Python 文件约 2.6 万行、72 个 REST 路由、14 张数据表；前端 17 个文件约 6200 行。

## 架构总览

```
frontend/  Vue 3 + Vite 单页应用（书架 / 单聊 / 群聊 / 群信息四视图）
    │  REST + SSE
backend/app/
    ├── api/        HTTP 层：会话解析、payload 组装、SSE 编码、按域拆分的 endpoints
    ├── agent/      Agent 层：单聊状态图、群聊图、管理员、抽取、提示词、工具
    ├── skills/     技能层：chat×3 + work×4；animation 自带渲染流水线与 vendor 模板
    ├── services/   业务层：书库/目录、记忆/RAG、对话、动画任务、运维清理
    ├── core/       配置、SQLite 数据层（建表+迁移）、安全、路径、工具协议
    ├── readers/    txt / md / pdf 解析
    └── channels/   渠道抽象（web 已启用，更多平台预留）

本地小模型：bge-small-zh-v1.5（嵌入）· Erlangshen ONNX int8（情绪）· jieba（人名）
存储：SQLite（app.db + graph_checkpoints.db）+ backend/data/（原文、模型、成片，不入库）
```

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.13 · FastAPI · LangGraph · Agno · DeepSeek API · SQLite（无 ORM） |
| 本地模型 | bge-small-zh-v1.5 · Erlangshen（ONNX int8）· jieba |
| 前端 | Vue 3 · Vite |
| 渲染流水线 | p5.js · chrome-headless-shell · ffmpeg · Node.js |

## 快速开始

环境要求：Windows（提供 PowerShell 一键脚本，其他平台可按脚本内命令手动执行）、Python 3.11+（仓库使用 3.13）、Node.js（前端与动画渲染需要）、一个 DeepSeek API Key。

```powershell
# 1. 装环境（建 venv + 装依赖）
.\scripts\setup.ps1

# 2. 取本地模型（一次性，约 190MB）：RAG 嵌入 + 情绪分类
.\scripts\fetch_model.ps1
.\.venv\Scripts\python.exe .\scripts\fetch_emotion_model.py

# 3.（可选）取动画渲染依赖（ffmpeg + chrome-headless-shell，装进技能包 bin/）
.\.venv\Scripts\python.exe .\scripts\fetch_animation_deps.py

# 4. 配置密钥
copy backend\.env.example backend\.env    # 填 DEEPSEEK_API_KEY

# 5. 起服务（后端 8000 + 前端 5173）
.\scripts\dev.ps1
```

打开 http://localhost:5173 ，上传一部 txt / md / pdf 小说即可开始。

> 未装动画依赖也能正常使用聊天与工作能力，animation 技能会自动隐身；依赖齐了自动出现。

### 常用配置

全部在 `backend/.env`，完整模板与逐项注释见 [`backend/.env.example`](backend/.env.example)：

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | 必填，抽取与对话都用它，只保存在服务端 |
| `SESSION_SECRET` | 强烈建议填写；留空则重启后旧会话失效、用户身份变更 |
| `EXTRACTION_MODE` | `algo` / `hybrid`（默认）/ `agent` / `llm` |
| `EMOTION_MODEL` | `auto` / `onnx` / `rules` |
| `RAG_BACKEND` | `auto` / `vector` / `lexical` |
| `DIALOGUE_JUDGE_MODE` | `off` / `auto`（默认，歧义才问模型）/ `always` |
| `GROUP_MAX_SPEAKERS` / `GROUP_MAX_ROUNDS` | 群聊成本闸门 |
| `GRAPH_CHECKPOINT_RETENTION_DAYS` | 对话断点保留天数（默认 7） |

## 自检

```powershell
# 后端：1121 项断言，离线可跑（约 5 分钟）
.\.venv\Scripts\python.exe backend\tests\smoke_test.py

# 前端：静态结构 + 绑定完整性（不跑浏览器）
node scripts\check-frontend.cjs

# 前端：SSR 真渲染冒烟（4 页面 + 3 浮层）
cd frontend; npm run check:render

# 前端：生产构建
cd frontend; npm run build
```

## 项目结构

```
.
├── backend/
│   ├── app/                  # 应用包（导入根：core.* / services.* / agent.*）
│   │   ├── main.py           # 入口（lifespan、CORS、挂路由）
│   │   ├── api/v1/           # 路由、Pydantic 模型、按域拆分的 endpoints
│   │   ├── agent/            # 单聊图、群聊图、管理员、抽取、提示词、工具
│   │   ├── skills/           # 技能包（chat / work；animation 含渲染引擎与模板）
│   │   ├── services/         # 业务逻辑（书库、记忆/RAG、对话、动画任务）
│   │   ├── core/             # 配置、数据库、安全、路径、工具协议
│   │   ├── readers/          # txt / md / pdf 解析
│   │   └── channels/         # 渠道抽象
│   ├── tests/smoke_test.py   # 零依赖自检（1121 项）
│   ├── data/                 # 运行时数据（不入库）：数据库、原文、模型、成片
│   └── .env.example          # 配置模板（含逐项注释）
├── frontend/
│   ├── src/                  # Vue 3：4 视图 + 3 浮层 + 状态单例 + SSE 封装
│   └── tests/                # SSR 渲染冒烟
├── docs/                     # 设计与分析文档（见下节索引）
└── scripts/                  # 安装 / 启动 / 取模型 / 自检脚本
```

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/agent-architecture.md`](docs/agent-architecture.md) | 完整设计：分层、数据模型、提示词、RAG、各阶段实现 |
| [`docs/langgraph-workflow.md`](docs/langgraph-workflow.md) | 单轮对话状态图逐节点解析 |
| [`docs/character-agent-workflow.md`](docs/character-agent-workflow.md) | 角色 Agent 工作流 |
| [`docs/manager-agent-workflow.md`](docs/manager-agent-workflow.md) | 管理 Agent 工作流（抽取链、书架链、合并链） |
| [`docs/memory-layer.md`](docs/memory-layer.md) | 记忆分层 / RAG / 幻觉与注意力 |
| [`docs/skills.md`](docs/skills.md) | 技能层与 SKILL.md 规范 |
| [`docs/animation-skill.md`](docs/animation-skill.md) | 动画技能包与渲染流水线 |
| [`docs/speaking-rules.md`](docs/speaking-rules.md) | 群聊发言规则与意愿分 |
| [`docs/tool-layer.md`](docs/tool-layer.md) | Agent 工具层 |
| [`docs/nlp-engine.md`](docs/nlp-engine.md) | 人名识别引擎的取舍与降级链 |
| [`docs/module-analysis.md`](docs/module-analysis.md) | 逐模块职责与三条主链路 |
| [`docs/project-map.md`](docs/project-map.md) | 文件级地图：每个文件干什么 |
| [`docs/manual-test.md`](docs/manual-test.md) | 人工验收清单 |

## Roadmap（预留计划）

- [ ] 通用工具注册表：联网搜索 / 文档解析 / 代码执行等，扩展角色 Agent 的执行能力
- [ ] 单聊接入工作记忆与会话窗口
- [ ] ASR 歌词识别：无内嵌歌词的音频自动转写
- [ ] 渠道接入：QQ 等更多平台（渠道抽象与能力矩阵已预留）
- [ ] 容器化部署验证与完善（`docker-compose.yml` 脚手架已就位）
- [ ] 更多渲染笔刷：当前风格为调色/纸墨/叠加层级别，计划扩展油画等真实笔触
- [ ] 管理经验向量检索、工作记忆按任务分段与摘要

## 特别鸣谢

- [ryan123-hub/ai-](https://github.com/ryan123-hub/ai-)（painted-animation）：本项目动画技能的渲染模板（`backend/app/skills/work/animation/vendor/template/`）源自该项目——一套基于 p5.js 程序化作画 + headless Chrome 逐帧渲染 + ffmpeg 编码的水彩/像素风动画流水线（MIT License）。NovelChat 在其基础上扩展了多风格目录、歌词时间轴切片、多进程分片渲染与任务面板集成。
