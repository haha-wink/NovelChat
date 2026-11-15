# 项目分层图

> 用 AST 扫了一遍所有 import 生成，不是手画的印象。
> 扫描结果：**0 处倒挂**。

---

## 0. 2026-11 重构后的结构（当前以此为准）

```text
backend/app/
├── core/                 基础设施 · 任何层都可用，它不依赖任何业务
│   ├── config.py  database.py  paths.py  security.py  scene.py  text.py
│   └── tools/            ① 工具协议层：ToolSpec 注册表 / execute / OpenAI schema
├── readers/              文档 → 文本
├── channels/             接入层：web（QQ/微信往后加在这里）
├── services/             领域层（不知道"聊天"是什么）
│   ├── dialogue/         对话理解与生成口径
│   │     dialogue · affect · emotion_model · persona · dispatch · angles
│   │     prompts · style · reply · expression · intent
│   ├── memory/           记忆与检索
│   │     agent_memory · rag · chat_memory · summarizer
│   ├── catalog/          书 / 角色 / 合并（内容管理）
│   │     library · merge · shelf · character_miner · name_library
│   │     name_engine · extractor
│   ├── ops/              运维杂项
│   │     agent_log · housekeeping · media · cleaner
│   └── runtime/          小型工具提供者
│         time_tools
├── skills/               ★ 技能库（可插拔，按模式加载）
│   ├── base.py           Skill / SkillResult
│   ├── loader.py         SKILL.md 解析 → Skill
│   ├── registry.py       注册表：按 mode 过滤 + 按信号打分
│   ├── chat/             chat 模式可用
│   │   ├── comfort/      SKILL.md + engine.py（comfort_phrase / breathing_guide）
│   │   ├── steady/       SKILL.md + engine.py（crisis_helpline）
│   │   └── suggest/      SKILL.md + engine.py（small_step）
│   └── work/             work 模式可用
│       └── animation/    SKILL.md + engine.py（animation_start / status / capabilities）
├── agent/                编排层（LangGraph 图 + 提示词装配 + 工具绑定）
│   ├── graph.py  group_graph.py  group.py  manager.py  character.py  extraction.py
│   └── tools/            ② 框架层：StructuredTool / ToolNode 封装
└── api/                  入口层：路由壳 + schema
```

分层规则：

```text
core  →  services  →  skills  →  agent  →  api
            ↑            ↑
        readers      channels
```

- `services` 不能 import `agent` / `skills`
- `skills` 可以用 `services`（comfort 要 persona），但 `services` 不能反过来用 `skills`
- `core` 不依赖任何业务层

## 模式（工作模式 vs 情感聊天）

技能的适用范围写在 `SKILL.md` 的 `modes` 里，由 `skills.registry.available(mode)` 过滤：

| 模式 | 可用技能 |
|---|---|
| `chat`（默认） | comfort / steady / suggest |
| `work` | steady / suggest / animation（+ 以后的专业技能） |

安全类（steady）两个模式都在——危机处理不能因为模式受限而失效。

---


## 1. 全景

```
┌──────────────────────────────────────────────────────────────────────┐
│                          浏览器（Vue 3 SPA）                         │
│   pages/  components/  stores/app.js  api/  utils/                   │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ HTTP / SSE
┌───────────────────────────────▼──────────────────────────────────────┐
│  api/          入口层 · 1727 行                                       │
│                                                                      │
│   endpoints/  chat(593) characters(273) shelves(325) books(165)      │
│               system(102) knowledge(80)                              │
│   deps.py     会话校验 · 依赖注入                                     │
│   schemas.py  请求/响应模型                                           │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ 可向下依赖任意层
┌───────────────────────────────▼──────────────────────────────────────┐
│  agent/        编排层 · 2535 行                                       │
│                                                                      │
│   graph.py        单聊状态图（8 节点 · LangGraph）                     │
│   group_graph.py  群聊状态图（4 节点 + 循环 · LangGraph）              │
│   manager.py      角色 Agent 构建 + LRU 缓存（Agno）                   │
│   character.py    attach_state（copy 副本 + 注入状态）                 │
│   group.py        run_turn 壳 + 选人规则 select_speakers              │
│   extraction.py   上传 → 抽角色（812 行）                              │
│   tools/          avatar（头像爬取）· web（联网检索）                   │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│  services/     业务层 · 6433 行（22 个模块）                          │
│                                                                      │
│  ┌── 对话 ──────────┐ ┌── 记忆 ──────────┐ ┌── 角色 ──────────┐      │
│  │ dialogue  495    │ │ agent_memory 197 │ │ library     537  │      │
│  │ affect    418    │ │ rag          411 │ │ merge       703  │      │
│  │ prompts   252    │ │ chat_memory  202 │ │ character_miner  │      │
│  │ style     167    │ │ summarizer    38 │ │             774  │      │
│  │ reply      48    │ │                  │ │ shelf       160  │      │
│  │ expression 97    │ │                  │ │ extractor   434  │      │
│  └──────────────────┘ └──────────────────┘ └──────────────────┘      │
│  ┌── 文本/命名 ─────┐ ┌── 运维 ──────────┐                            │
│  │ cleaner   213    │ │ agent_log   152  │                            │
│  │ name_engine 280  │ │ housekeeping 52  │                            │
│  │ name_library 378 │ │ media        82  │                            │
│  │ emotion_model 343│ │                  │                            │
│  └──────────────────┘ └──────────────────┘                            │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│  core/         基础层 · 2423 行（不依赖任何上层）                      │
│                                                                      │
│   database  1931   SQLite（21 张表）· 迁移 · 全部读写                  │
│   config     215   pydantic-settings                                 │
│   security   111   会话签名 / HMAC                                    │
│   paths/scene/text/hf_import                                         │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│  readers/  58 行（文档解析）    channels/  288 行（QQ/微信占位）        │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. 依赖方向（AST 实测）

```
   api ──────┬──────────┬──────────┬────────────┐
             ▼          ▼          ▼            ▼
          agent      services   channels     readers
             │          │
             ▼          ▼
           services    core
                        ▲
                        │
                      all
```

实测结果：

| 依赖 | 文件数 | 是否允许 |
|---|---|---|
| `services → core` | 13 | ✅ |
| `agent → services` | 6 | ✅ |
| `agent → core` | 6 | ✅ |
| `api → agent` | 5 | ✅ |
| `api → services` | 4 | ✅ |
| `api → channels` / `api → readers` | 各 1 | ✅ |
| **`services → agent`** | **0** | 必须为 0 ✅ |
| **`core → services`** | **0** | 必须为 0 ✅ |
| **任何向上依赖** | **0** | 0 处倒挂 ✅ |

---

## 3. 一轮对话穿过哪些层

```
      浏览器
        │  POST /api/chat  {token, character_id, prompt}
        ▼
   ┌─ api/v1/endpoints/chat.py ─────────────────────────────────┐
   │  resolve_session()    ← core/security                      │
   │  decode_images()                                           │
   │  填 TurnState + 注入 llm=run_agent                         │
   └──────────────────────┬─────────────────────────────────────┘
                          ▼
   ┌─ agent/graph.py（LangGraph 单聊图）────────────────────────┐
   │                                                            │
   │  preprocess ──► services/dialogue.preprocess               │
   │  classify   ──► services/dialogue.classify_with_judge      │
   │                   └─ 歧义时 ──► services/extractor         │
   │                                  .call_llm_json（判官）     │
   │  observe    ──► services/affect.observe_user_message       │
   │                   └─► core/database（写 character_affect）  │
   │  recall     ──► services/rag.recall_block                  │
   │                   └─► core/database（memory_chunks）        │
   │  plan       ──► services/dialogue.plan + affect.prompt_block│
   │  generate   ──► agent/manager.get_agent（Agno + DeepSeek） │
   │  verify     ──► services/reply + services/dialogue.verify  │
   │  persist    ──► core/database.add_chat_turn                │
   │                   + services/chat_memory.maybe_distill      │
   │                       └─► agent_memory.remember             │
   │                       └─► rag.index_character               │
   └──────────────────────┬─────────────────────────────────────┘
                          │ SSE: delta / final / sticker / done
                          ▼
                      浏览器
```

群聊走 `agent/group_graph.py`：

```
prepare → pick → speak → (speak | pick | finish)
  │        │       └─ generate(member, transcript) 由 chat.py 注入
  │        └─ group.select_speakers（纯规则）
  └─ @解析 / @全体唤醒 / 用户那句落库
```

---

## 4. services 层内部分组（22 个模块）

按耦合关系归成 5 组：

```
对话        dialogue ──► extractor（判官）
            affect ────► agent_log, emotion_model
            prompts ───► style ──► name_library
            reply / expression（叶子）

记忆        chat_memory ──► agent_memory ──► rag
                           agent_log, dialogue, rag

角色        library ──► character_miner, cleaner, extractor,
                        merge, prompts, rag, shelf, agent_log
            shelf  ──► library, merge, agent_memory, agent_log
            merge  ──► agent_memory, extractor, agent_log
            extractor ──► character_miner, prompts

文本/命名    cleaner / name_engine ──► name_library / emotion_model

运维        agent_log / housekeeping / media
```

**一个观察**：`library`（537 行）是 services 里 connect 最多的枢纽——它同时管书、
馆、抽取入口和合并入口。如果哪天要拆，这是第一个该动的地方。

---

## 5. 数据落到哪（21 张表）

```
core/database.py 单文件 1931 行，直接 sqlite3，无 ORM
│
├─ 内容      libraries → shelves → books → characters
├─ 群聊      shelf_members / chat_turns（intent·emotion·images·distilled）
├─ 记忆      character_memory_items（kind=excerpt|fact，带 scene/tokens）
│            memory_chunks（向量）
├─ 情绪      character_affect（valence/arousal/favorability + baseline）
├─ 抽取      extraction_jobs / extraction_logs
├─ 合并      manager_memories / merge_logs
├─ 运维      agent_events（日志）/ sessions
└─ 模型缓存  name_library 相关表
```

另外 Agno 自己有一份 `data/workspace/agent_memory.db`（会话历史 + 原生记忆），
**不在这 21 张表里**——排查"角色记不记得"时容易漏掉它。

---

## 6. 外部依赖边界

```
                    ┌─────────────────┐
                    │   DeepSeek API  │  ← 对话 / 抽取 / 判官 / 蒸馏摘要
                    └────────┬────────┘
                             │
  ┌──────────┐    ┌──────────▼──────────┐    ┌──────────────┐
  │  Agno    │───►│   backend/app       │◄───│ 前端 Vue SPA │
  └──────────┘    └──────────┬──────────┘    └──────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   bge-small-zh        erlangshen-sentiment    jieba / ddgs
   （向量检索）          （情绪兜底）            （人名 / 联网）
   本地 91MB            本地 98MB ONNX          可选
```

---

## 7. 分层是怎么保证的

`core / readers / channels` **不允许**向上依赖 `services / agent / api`。
仓库里没有独立的检查脚本，但可以随时复现：

```python
import ast
from pathlib import Path

LAYERS = ["core", "readers", "channels", "services", "agent", "api"]
ORDER = {n: i for i, n in enumerate(LAYERS)}
root = Path("backend/app")
bad = []
for layer in LAYERS:
    for p in (root / layer).rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module] if isinstance(node, ast.ImportFrom) else [])
            for full in mods:
                head = (full or "").split(".")[0]
                if head in ORDER and ORDER[head] > ORDER[layer]:
                    bad.append(f"{p}: {layer} -> {head}")
print(bad or "0 处倒挂")
```

---

## 8. 体量分布

| 层 | 文件 | 行数 | 占比 |
|---|---:|---:|---:|
| services | 22 | 6433 | 48% |
| agent | 8 | 2535 | 19% |
| core | 7 | 2423 | 18% |
| api | 9 | 1727 | 13% |
| channels | 5 | 288 | 2% |
| readers | 1 | 58 | <1% |
| **合计** | **52** | **13464** | |

三个最重的单文件：

| 文件 | 行数 | 说明 |
|---|---:|---|
| `core/database.py` | 1931 | 21 张表的全部读写，**最该拆的一个** |
| `agent/extraction.py` | 812 | 抽取流水线 |
| `services/character_miner.py` | 774 | 从原文挖角色 |