# 手动测试步骤

> 面向"自己跑一遍确认功能都对"的场景。分成**不花钱**和**花钱**两部分，
> 建议先跑完 0~2 节（零成本），再决定要不要跑 3 节里的对话类用例。

---

## 0. 测试前准备（约 3 分钟）

### 0.1 备份数据（**必做**）

```powershell
cd E:\NovelChat
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force backend\data\backups | Out-Null
Copy-Item backend\data\workspace\app.db "backend\data\backups\$stamp-before-test.db"
Copy-Item backend\data -Destination "backend\data\backups\$stamp-data" -Recurse -Force
```

出问题时的回滚方式：

```powershell
Copy-Item backend\data\backups\<那个时间戳>-before-test.db backend\data\workspace\app.db -Force
```

### 0.2 确认关键文件在位

```powershell
Test-Path backend\.env                                                    # 应有 DEEPSEEK_API_KEY
Test-Path backend\data\models\bge-small-zh-v1.5\model.safetensors         # RAG 模型（约 91MB）
Test-Path backend\data\models\erlangshen-sentiment\onnx\model_quantized.onnx  # 情绪模型（约 98MB）
(Get-ChildItem backend\data\stickers -Filter *.svg).Count                 # 应为 18
```

缺模型的话跑一次：`pwsh scripts\fetch_model.ps1`

### 0.3 启动

```powershell
cd E:\NovelChat
.\scripts\dev.ps1
```

会起两个进程：

| 服务 | 地址 |
|---|---|
| 后端 API | http://127.0.0.1:8000 （文档 http://127.0.0.1:8000/docs ） |
| 前端页面 | http://localhost:5173 |

> 启动后**等 8 秒左右**再打开页面：RAG 模型在后台加载（约 7 秒），
> 控制台会打印 `RAG 预热完成（vector，7.x s）`。

---

## 1. 零成本检查（不发任何模型请求）

### 1.1 健康检查

浏览器打开 <http://127.0.0.1:8000/api/health>，逐项确认：

| 字段 | 期望值 | 说明 |
|---|---|---|
| `api_key_configured` | `true` | 否则聊天会 503 |
| `name_engine.backend` | `jieba` | 人名识别引擎 |
| `emotion_model.backend` | `onnx` | 情绪小模型（没模型时会显示 `rules`，属正常降级） |
| `rag.backend` | `vector` | 若为 `lexical` 说明模型没加载成功，看 `rag.error` |
| `rag.ready` | `true` | |
| `features` | 含 `rag_memory` / `memory_distill` | |

☐ 通过　☐ 不通过（看第 5 节排查）

### 1.2 渠道清单

<http://127.0.0.1:8000/api/channels>

期望：`default` 是 `web`；`web.enabled=true`，`qq` / `wechat` 是 `false`（预留状态是正常的）。

☐ 通过

### 1.3 贴纸素材

浏览器打开 <http://127.0.0.1:8000/api/sticker/1f604.svg>

期望：能直接看到一张彩色笑脸（Twemoji SVG）。

☐ 通过

### 1.4 前端记忆库徽章

打开 <http://localhost:5173>，页面顶部应有一行紫色提示：

> 🧠 记忆库：语义向量（bge-small-zh-v1.5）

如果显示「词法检索」= 模型没就绪（功能仍可用，只是检索变关键词匹配）。

☐ 通过

### 1.5 全量自检（可选，约 60 秒）

```powershell
$py = ".venv\Scripts\python.exe"
& $py backend\tests\smoke_test.py
```

期望最后一行：`共 554 项，失败 0 项`。

> 这个脚本会自己创建临时书架/书，跑完自动删掉，**不会动你的真实数据**。

☐ 通过

---

## 2. 主流程测试（**需要新建一本书，会产生抽取费用**）

### 2.0 重要前提

**不要对已有的《龙族Ⅰ》《龙族Ⅱ》点「重新抽取」**——那会重新生成角色，所有角色 id、头像、你改过的资料、聊天状态全部作废。

新功能一律用**新上传一本小书**来测（几万字即可，抽取费用很低），测完删掉。

### 2.1 上传与抽取

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 书架页点上传，选一个 txt（**建议先复制一份测试用的短篇**） | 出现进度卡片 |
| 2 | 观察进度条 | 阶段从「分块」→「抽取角色」→「完成」 |
| 3 | 完成后点这本书 | **直接进入群聊页**（不再有角色列表页） |
| 4 | 看群成员条 | 出现从这本书抽出来的角色，带头像和状态 |

☐ 通过　☐ 不通过

**这一条同时验证了 P5 的改动**：点书 = 进群，角色列表已经并进群成员。

### 2.2 正文清洗（入库时就该生效）

后端控制台在抽取时应该打印类似：

```
正文清洗：去掉 1234 字（3.2%），12 行广告/分隔线，0 行重复页眉
```

如果那本书本身干净，可能不打印（少于 0.5% 不打日志，避免刷屏）。

要主动验证，用接口对**这本书抽出来的某个角色**跑 dry-run：

```powershell
chcp 65001 > $null
$base = "http://127.0.0.1:8000"
curl.exe -s -X POST "$base/api/maintenance/clean-memories?dry_run=true"
```

期望返回 `scanned / changed / removed_chars / samples`；`dry_run=true` **不会改数据**。

☐ 通过

### 2.3 群聊：@ 唤醒

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 输入 `大家好`（不 @ 任何人） | **可能没人说话**——这是设计，闲聊不花钱 |
| 2 | 输入 `@某人 你怎么看` | 被 @ 的角色**必答**；其他人不受影响（离线的人仍然离线） |
| 3 | 输入 `@全体成员 都来说两句` | **所有在线成员都会回一句**（等于逐个 @ 了一遍），离线的人会被叫上线 |
| 4 | 观察多人接话 | 一个角色说完后，若它 @ 了别人，**那个人下一轮会必答** |

> ⚠️ `@全体成员` 的成本 = 在线人数 × 模型调用。24 人在线就是 24 次请求。
> 想省钱就先把不想参与的人设为**离线**，再 @全体。

☐ 通过　☐ 不通过

**为什么第 1 步可能没人说话**：选角阈值 0.55，纯闲聊时角色分数不够就不会开口。这是成本闸门，不是 bug。

### 2.3.1 回复应当是"说话"，不带动作描写

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 随便聊几句 | 回复是**纯对话**，不会出现"他抬起头，揉了揉眼睛"这类旁白 |
| 2 | 看长度 | 一般 1~2 句，不会长篇大论 |

如果仍出现动作描写：后端会兜底删掉整行的 `*动作*` / `（动作）`。
真漏了就把那条回复发我，我补规则。

☐ 通过

### 2.4 群聊：成员状态（在线 / 离线）

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 点一个成员卡片 | 弹出详情面板 |
| 2 | 看「此刻状态」 | **状态：在线 · 心情描述**、记忆段数、互动轮次 |
| 3 | 点「离线」 | 面板状态变「离线」，成员条变灰 |
| 4 | 普通发一句（不 @ 他） | 离线的人**不会**说话 |
| 5 | 发 `@这个离线的人 你说` | **仍会被 @ 叫上线并回复**（单独 @ 会把人叫上线） |
| 6 | 点「上线」 | 恢复在线 |

☐ 通过

> 语义：离线 = 收不到消息、也不会发言；@ 他 = 直接把他叫上线。

### 2.5 贴纸（规则驱动，不是纯随机）

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 问 `你现在心情怎么样？` | **一定**出一张贴纸（问心情 = 用表情回答） |
| 2 | 说一句让他情绪明显变化的话（夸 / 骂） | 下一轮**一定**出贴纸（心情变了要看得见） |
| 3 | 平淡闲聊 | 基本不出（概率上限 30%） |

☐ 通过

### 2.6 RAG 记忆检索（**这是本次重点**）

先拿一个角色 id：

```powershell
chcp 65001 > $null
$base = "http://127.0.0.1:8000"
$books  = (curl.exe -s "$base/api/books" | ConvertFrom-Json)
$chars  = (curl.exe -s "$base/api/books/$($books[0].id)/characters" | ConvertFrom-Json)
$charId = $chars[0].id
curl.exe -s "$base/api/books/$($books[0].id)/characters" | Select-Object -First 1
```

再看"这句话会捞到哪些记忆"：

```powershell
$q = [uri]::EscapeDataString("你还记得那天下雨吗")
curl.exe -s "$base/api/characters/$charId/recall?q=$q"
```

期望：

- `backend` 是 `vector`
- `scene` 识别出场景（如 `雨夜`）
- `hits` 里每条带 `score` / `scene` / `kind` / `content` / `source_book_id`

换几个不同的问题（战斗、感情、日常），**hits 应该明显不同**。

☐ 通过　☐ 不通过（若 hits 全空，看第 5 节）

### 2.7 对话蒸馏（短期 → 长期记忆）

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 在群聊里聊 10 来轮，聊一些具体话题 | — |
| 2 | 手动触发蒸馏 | 见下方命令 |
| 3 | 再问那个话题 | 角色应该能"想起来"（RAG 捞到对话记忆） |

```powershell
curl.exe -s -X POST "$base/api/characters/$charId/distill"
```

期望返回 `distilled > 0`、`summary` 非空、`used_llm` 为 `true`（有 Key 时）。

> 自动蒸馏也在跑：未蒸馏对话超过 40 条会自动处理一半（`chat_distill_threshold`）。
> 所以聊得多的话，你可能不用手动触发。

☐ 通过

### 2.8 手动合并 + 撤销

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 点开角色 A 详情 → 「合并到…」 | 弹出成员列表（不含自己） |
| 2 | 选角色 B → 合并 | 群成员少一个，A 的记忆并入 B |
| 3 | 重开 B 的详情 | 出现「已并入的角色」，能看到 A |
| 4 | 点「撤销合并」 | A 重新出现在群成员里，记忆还回去了 |

☐ 通过

---

## 3. 书架事务（加书 / 删书 / 回档）

> 这一节改动较大，建议**只在测试书上做**。

### 3.1 加书：角色入群 + 旧角色记忆补强

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 再上传一本同系列的书（同一书架） | — |
| 2 | 观察原角色 | 如果新书里出现了旧角色的名字，它的**记忆条数会增加**（来自新书的片段） |
| 3 | 观察群成员 | 两本书的角色在**同一个群**里 |

验证补强的命令：

```powershell
chcp 65001 > $null
$q = [uri]::EscapeDataString("新书里出现过的事")
curl.exe -s "$base/api/characters/$charId/recall?q=$q"
```

看 `hits` 里有没有 `source_book_id` 指向新书的条目。

☐ 通过

### 3.2 删书：回档

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 删掉刚加的那本书 | 它的角色退群 |
| 2 | 再看旧角色的记忆 | 来自那本书的片段**被精确回收**，原有记忆完好 |

☐ 通过

---

## 4. 命令行速查

> **注意编码**：Windows PowerShell 5.1 的 `Invoke-RestMethod` 会按 Latin1 解码 JSON，
> 中文会变成 `é¾æ` 这种乱码（数据本身没错，只是显示问题）。
> 所以下面一律用 **`curl.exe`**（Win10+ 自带），并在开头切到 UTF-8 代码页。

```powershell
chcp 65001 > $null
$base = "http://127.0.0.1:8000"

# 作品 / 书架 / 成员
curl.exe -s "$base/api/books"
curl.exe -s "$base/api/shelves"
$shelfId = (curl.exe -s "$base/api/shelves" | ConvertFrom-Json)[0].id
curl.exe -s "$base/api/shelves/$shelfId/members"

# 管理 Agent 时间线（排查"它到底做了什么"）
curl.exe -s "$base/api/shelves/$shelfId/logs"

# RAG 状态 / 重建索引
curl.exe -s "$base/api/rag/status"
curl.exe -s -X POST "$base/api/rag/index?force=false"

# 贴纸目录
curl.exe -s "$base/api/stickers"

# 只想要某个字段时，自己接 ConvertFrom-Json（但中文会再次乱码，看数量就行）
(curl.exe -s "$base/api/books" | ConvertFrom-Json).Count
```

带中文的 GET（比如 recall 的 `q`）必须 **URL 编码**：

```powershell
$books  = (curl.exe -s "$base/api/books" | ConvertFrom-Json)
$chars  = (curl.exe -s "$base/api/books/$($books[0].id)/characters" | ConvertFrom-Json)
$charId = $chars[0].id
$q = [uri]::EscapeDataString("你还记得那天下雨吗")
curl.exe -s "$base/api/characters/$charId/recall?q=$q"
```

> 带中文 body 的 POST（群聊、聊天）建议直接走界面，别在命令行里折腾编码。

---

## 5. 出问题怎么查（三板斧）

### 5.1 聊天返回 503

```
detail: 服务端未配置 DEEPSEEK_API_KEY
```

→ 检查 `backend\.env` 有没有 `DEEPSEEK_API_KEY=sk-...`，改完**重启后端**。

### 5.2 RAG 变成词法检索

`GET /api/rag/status` 看 `error` 字段：

| error | 处理 |
|---|---|
| `模型目录不存在：...` | 跑 `pwsh scripts\fetch_model.ps1` |
| `配置为词法模式` | `backend\.env` 里 `RAG_BACKEND=lexical` 了，删掉这行 |
| 其它 | 看后端控制台 `RAG 预热失败` 的完整堆栈 |

### 5.3 群聊没人说话

按顺序确认：

1. 群里至少有 2 个成员吗（成员条数量）？
2. 是不是所有人都被「静默」了？
3. 试 `@某个具体名字` —— 被点名的人一定回，不回就是别的问题；
4. 看后端日志有没有 `群聊生成失败`。

### 5.4 角色"失忆"（一问三不知）

```powershell
chcp 65001 > $null
curl.exe -s "$base/api/characters/$charId/status"
```

看 `status.memory.total`：

- 为 0 → 抽取阶段没拿到片段，是抽取问题；
- 有几十条但 recall 捞不到 → 检索问题，跑一次 `POST /api/rag/index?force=true` 重建索引。

### 5.5 前端改了没生效

前端是 Vite 热更新；如果没反应，`Ctrl+Shift+R` 强刷。后端改了要等自动重载（或手动重启）。

---

## 6. 测试后清理

```powershell
# 1. 删掉测试用的书（界面里删也行）
#    —— 删书会连带清掉它的角色、记忆、群成员关系

# 2. 如果建过测试书架，删掉（书架里有书会被拒绝，先删书）
```

如果测试过程中动了不该动的数据：

```powershell
Copy-Item backend\data\backups\<时间戳>-before-test.db backend\data\workspace\app.db -Force
# 重启后端
```

---

## 7. ⚠️ 不要做的操作

| 操作 | 后果 |
|---|---|
| 对《龙族Ⅰ》《龙族Ⅱ》点「重新抽取」 | **角色 id 全部重生成**，头像/资料/情绪状态/记忆清零 |
| 全库真清洗（`dry_run=false`）之前不做备份 | 记忆正文被改写，不可逆 |
| 手动删 `backend\data\workspace\app.db` | 全部数据丢失 |
| 开着服务时直接替换 `app.db` | 可能被 WAL 覆盖，先停服务 |

---

## 8. 一页版检查清单

复制这段，测一项勾一项：

```
[ ] 0.1 已备份 app.db 与 data 目录
[ ] 0.2 .env / 模型 / 18 个贴纸在位
[ ] 0.3 dev.ps1 启动，看到 RAG 预热完成
[ ] 1.1 /api/health 六项字段正常
[ ] 1.2 /api/channels web 启用、QQ/微信预留
[ ] 1.3 贴纸 SVG 能打开
[ ] 1.4 前端顶部有「记忆库：语义向量」
[ ] 1.5 smoke_test 554 项全过
[ ] 2.1 新增一本书，抽取完成，点书直接进群
[ ] 2.2 清洗 dry-run 有统计
[ ] 2.3 @某人必答 / @全体全员回复 / 角色互相@接力
[ ] 2.3.1 回复是纯对话，没有动作描写
[ ] 2.4 成员在线/离线切换，离线不发言
[ ] 2.5 问心情必出贴纸，心情变化也出
[ ] 2.6 /recall 能按不同问题捞到不同记忆
[ ] 2.7 蒸馏产出对话记忆，之后能检索到
[ ] 2.8 手动合并 + 撤销
[ ] 3.1 加书 → 旧角色记忆增加
[ ] 3.2 删书 → 记忆精确回收
[ ] 6  清理测试数据
```

---

## 附：预计费用

| 操作 | 大致消耗 |
|---|---|
| 抽取一本 5 万字小说 | 几次到十几次模型调用 |
| 群聊一轮 | 最多 4 次（`max_speakers 2 × max_rounds 2`），闲聊常常 0 次 |
| 单角色聊天一轮 | 1 次 |
| 对话蒸馏一次 | 1 次（仅蒸馏时） |
| RAG 检索 | **0 次**（本地模型，不联网） |
| 贴纸 | **0 次** |
| 正文清洗 | **0 次** |

不花钱的部分（第 1 节 + 2.6 的 recall 接口）已经能验证大半功能。
---

## 3. 书架 / 群聊 / 历史（P11）

### 3.1 聊天历史

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 在群聊里说两句，点「← 书架」，再点该书架「群聊」进来 | **刚才的对话还在** |
| 2 | 进单聊说两句，返回群聊，再点该角色进单聊 | 单聊记录还在 |
| 3 | 刷新浏览器（F5）后重新进入 | 记录仍在（存在后端 `chat_turns`，不是前端内存） |
| 4 | 直接打开 http://localhost:5173 | **默认直接进上次那个书架的群聊**（没有记录则进第一个书架） |

| 5 | 群聊 → 信息 → 清空对话 | 确认后当前聊天记录清空；角色之前记住的事不受影响 |
| 6 | 单聊 → 新对话 | 同上（会连带清掉这个角色的单聊记录） |

> 历史只显示**你自己**说过/听过的；换一个用户名就是一套新历史。
> 蒸馏成长记忆后原文**不会被删**，所以久远的对话也还在。

☐ 通过

### 3.2 书架管理（书库 → 书架 → 书）

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 书架页点「＋ 新建书架」 | 输入名称后出现新书架卡片 |
| 2 | 上传小说前先在「上传到」里选目标书架 | 书落到该书架下 |
| 3 | 点书架卡片上的「群聊」 | 直接进入这个书架的群（原来的「选择角色」已移除） |
| 4 | 点「改名」 | 书架名和群聊名**同步**变 |
| 5 | 点「删除」 | 二次确认提示「会同时删除里面的 N 本书」；确认后书架、书、角色、聊天记录一起清掉 |

> ⚠️ 删书架是**连书一起删**，不可恢复。只有确认框过得了这一步。

☐ 通过

### 3.3 群聊信息面板

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 群聊右上角只有一个「信息」按钮（原来的添加角色/AI审核/刷新已迁进去） | **跳转**到独立的「群聊信息（N）」界面（和「← 书架」一样是换页，不是弹窗） |
| 2 | 看成员网格（群聊页上已没有成员条，全在这里） | 一行 5 个，默认最多 3 行；成员超过 15 个时第 15 格变成「更多」 |
| 3 | 成员少于 15 个 | 全部显示，面板高度自适应 |
| 4 | 点「更多」 | 展开全部成员 |
| 5 | 点任意成员头像 | 跳到该角色详情 |
| 6 | 改「群聊名称」→ 保存 | 群标题立即更新，书架页同名 |

☐ 通过

### 3.4 自动合并与「待确认的合并」

| 步骤 | 操作 | 预期 |
|---|---|---|
| 1 | 往同一个书架再传一本同系列小说 | 抽取完成后，**同名角色自动合并成一个**（群里人数不涨） |
| 1b | 两个名字不同、但性格/关系很像的角色 | 相似度 > 0.7 时**也会自动合并**，不进待确认列表 |
| 2 | 进「群聊信息」 | 底部有「待确认的合并（N）」区块 |
| 3 | 看候选行 | 显示 `A ↔ B`、相似度、共同关联的人 |
| 4 | 点「是同一人」 | 两个角色合并（出场多的保留），候选消失，经验 +1 |
| 5 | 点「不是」 | 不合并；这对以后不再出现 |
| 6 | 再进群聊信息 | 标题右侧「已记住 N 条合并经验」增加 |

> 候选只出现在「名字不同但关系网/性格很像」的情况。同名角色在抽取完成时就已经自动合并，
> 不进这个列表。判定结果会按书架记下来，下次遇到同样的组合直接复用。

☐ 通过
