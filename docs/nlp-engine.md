# 人名识别引擎（jieba / HanLP / 内置规则）

本项目**不自己维护人名表**，优先调用成熟的中文 NLP 库做中文人名与外文译名识别；
只有在「一个库都装不上」时才回退到内置规则（`backend/services/name_library.py`），
保证功能不会因为缺依赖而失效。

## 用的是哪个库、为什么

| 库 | 装它值不值 | 说明 |
|---|---|---|
| **jieba** | ✅ 首选 | 纯 Python、无需编译、约 19MB。`jieba.posseg` 会给出 `nr`（人名）、`nr1`（姓氏）、`nr2`（名字）、`nrf`（**音译人名**）、`nrj`（日文人名），正好覆盖「中外人名」两类需求 |
| HanLP | ⚠️ 可选 | 效果更好，但 2.x 依赖 torch、首次运行要下模型（几百 MB）。机器够强再上 |
| pkuseg | ❌ 不建议 | 需要编译 + 老版本 numpy，**Python 3.13 没有预编译包**，装起来基本是坑 |
| pypinyin / spacy / thulac / ltp | — | 与人名识别不直接相关或同样偏重，不引入 |

词性对照：

```
nr    人名          nr1   汉语姓氏（单字，不单独算角色）
nr2   汉语名字      nrf   音译人名（外国人名）  ← 关键
nrj   日语人名      nrfg  部分 jieba 版本的人名标记
```

## 安装（三步）

```powershell
# 1. 装（推荐 jieba）
python -m pip install jieba
# 国内镜像更快：
python -m pip install jieba -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 验证
python scripts\check_nlp.py

# 3. 没网时离线装 wheel
python -m pip download jieba --no-deps -d wheels     # 在能上网的机器上执行
python -m pip install wheels\jieba-*.whl             # 拷到本机后执行
```

验证成功的样子：

```
=== 当前引擎 ===
  backend: jieba
  is_nlp: True
=== 各后端探测 ===
  jieba    OK
  hanlp    MISSING
```

仍显示 `backend: builtin` 就说明没装成功，此时会打印完整安装说明。

再确认一下后端也切过去了（重启后端后）：

```powershell
curl http://127.0.0.1:8000/api/health
# 关注这一段：
# "name_engine": {"backend": "jieba", "available": true, "is_nlp": true,
#                 "preferred": "auto", "degraded": false, ...}
```

`degraded: true` 表示你指定了 `NAME_ENGINE=jieba` 但没装成功，正在用内置规则。

## 切换引擎

`backend/.env`：

```dotenv
NAME_ENGINE=auto      # 默认：先 jieba → 再 HanLP → 都没有才用内置规则
NAME_ENGINE=jieba     # 强制 jieba
NAME_ENGINE=hanlp     # 强制 HanLP
NAME_ENGINE=builtin   # 只用内置规则（完全不依赖第三方库）
```

`GET /api/health` 里 `name_engine` 字段会直接告诉你当前跑的是哪个。

## Agent 怎么用它

抽角色时，Agent 有两个工具直接吃这个引擎：

```jsonc
// 1) 独立扫全文认人名（找漏抽）
{"action": "name_scan", "args": {"limit": 40}}
// 返回：库认出的人名 + count + tag(nr/nrf)
//       not_in_candidates   库认出但算法没挖到的（可能漏抽）
//       candidates_not_found 算法挖到但库没认出的（昵称/生僻名）

// 2) 校验某个名字
{"action": "name_check", "args": {"names": ["路明非", "恺撒·加图索", "掌门"]}}
// 返回：kind=cn/foreign/nickname/unknown、score、surname、given、engine_tag、reasons
```

调用前 Agent 会先把自己已有的名单 `add_word` 进 jieba 词典（`engine.learn()`），
避免「古德里安」「龙德施泰特」这种名字被切碎。

## 代码位置

| 文件 | 作用 |
|---|---|
| `backend/services/name_engine.py` | 引擎抽象：`JiebaBackend` / `HanLPBackend` / `BuiltinBackend`，`detect()` / `classify()` / `learn()` |
| `backend/services/name_library.py` | 内置规则兜底（姓氏/名字用字/音译字/称谓词），同时也是 `classify()` 的底分来源 |
| `scripts/check_nlp.py` | 体检 + 安装说明 + 样例扫描 |
| `backend/api/routes.py` | `GET /api/health` 暴露 `name_engine` |

## 常见问题

**装完还是 builtin？**
1. 确认装到了项目的虚拟环境：`.\ai-agent-cyber-town.venv\Scripts\python.exe -m pip install jieba`
2. 重启后端（引擎是进程内单例）
3. `python scripts\check_nlp.py` 再看一次

**扫描很慢？**
jieba 首次加载词典约 1 秒，之后单次 30 万字约 2~5 秒；`name_scan` 只在 Agent 调它时才跑。

**公司网络装不上？**
用 `--proxy` 指定代理，或按上面「离线装 wheel」走；实在装不上就用 `NAME_ENGINE=builtin`，
内置规则也能跑（只是音译名判断不如 jieba 准）。