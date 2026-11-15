# 技能包（Skills）

> 一个技能 = 一个目录。加技能不用改任何 Python 代码。

## 1. 目录结构

```
backend/app/skills/
├── base.py          Skill / SkillResult（步骤执行、占位符替换、打分）
├── loader.py        SKILL.md 解析 → Skill；导入 engine.py 注册工具
├── registry.py      注册表：按模式过滤 + 按依赖过滤 + 按信号打分
├── capabilities.py  外部依赖探测（node / ffmpeg / chrome / numpy）
├── chat/            chat 模式可用
│   ├── comfort/     SKILL.md + engine.py
│   ├── steady/      SKILL.md + engine.py
│   └── suggest/     SKILL.md + engine.py
└── work/            work 模式可用
    ├── planning/    SKILL.md + engine.py   ← 零依赖
    ├── notes/       SKILL.md + engine.py   ← 零依赖
    ├── writing/     SKILL.md + engine.py   ← 零依赖
    └── animation/   SKILL.md + engine.py   ← 大技能，需要 node/ffmpeg/chrome
```

## 2. SKILL.md 格式

```markdown
---
name: planning
description: 用户列了一堆要做的事，帮他排先后顺序
modes: [work]                 # chat / work（可以是多个）
requires: []                  # 外部依赖，空 = 纯 Python
external: false               # 是否大技能（自带引擎/素材）
when:                         # 什么时候用（声明式，无 Python）
  keywords: [待办, 先后顺序, 排个序]
  action_words: [帮我排, 排一下]
  intent: [seek_help]
  not_crisis: true
  base: 0.7                   # 基础分
  bonus: 0.15                 # 命中 action_words 时再加
steps:
  - [task_breakdown, {text: $text}]     # 工具名, 参数（$text 会被替换成用户原话）
---
正文：给模型看的补充说明（会被当作技能说明，可选）
```

### `when` 支持的条件

| 字段 | 含义 |
|---|---|
| `intent` / `emotion` | 命中其中任一（列表或单值） |
| `keywords` | 用户原话包含任一关键词 |
| `action_words` | 同时需要出现任一动作词（防"只提到名词就抢戏"） |
| `min_intensity` | 情绪强度下限 |
| `crisis` / `not_crisis` | 危机必须 / 必须不成立 |
| `base` / `bonus` | 基础分 / 全部命中后的加分 |

打分最高的技能被执行；都是 0 就不执行任何技能。

### `steps` 与占位符

```yaml
steps:
  - [comfort_phrase, {emotion: $emotion}]
  - [animation_start, {prompt: $text, style: watercolor}]
```

可用的占位符：`$text`（用户原话）、`$emotion`、`$intent`、`$topic`、`$mode`。

**逗号按层级拆分**——`[工具, {a: 1, b: 2}]` 里的逗号不会被当成两段（这个坑踩过：参数曾被拆空）。

## 3. engine.py：技能自己的工具

```python
"""planning 技能的工具。"""
from core.tools import protocol as tool_protocol

def _tool_task_breakdown(args: dict) -> dict:
    return {"ok": True, "text": "..."}

tool_protocol.register(
    tool_protocol.ToolSpec(
        name="task_breakdown",
        description="把待办拆成有序清单",
        parameters={"text": {"type": "string"}},
        handler=_tool_task_breakdown,
        required=("text",),
        domain="work",
    )
)
```

`loader.load_engines()` 会在加载技能时自动导入它——**工具跟着技能走**，删掉目录就等于删掉能力。

## 4. 可插拔：把技能放到仓库外

把带 `SKILL.md` 的目录丢进去就能用，不用改代码：

```bash
# .env
SKILLS_EXTRA_DIRS=D:/my-skills,D:/team-skills
```

支持两种布局：

```
D:/my-skills/my-skill/SKILL.md     ← 单个技能
D:/my-skills/{a,b,c}/SKILL.md      ← 一组技能
```

外部技能同样走 `requires` / `modes` 过滤，行为与内置完全一致。

## 4.5 自包含大技能（animation 就是这么做的）

大技能可以把**引擎素材和依赖都放进自己的包里**，拷走目录就能用：

```
skills/work/animation/
├── SKILL.md              # 技能声明
├── engine.py             # 工具实现
├── DEPS.md               # 依赖说明 + 怎么放
├── bin/                  # 可选：ffmpeg.exe / chrome/
└── vendor/               # 上游引擎（MIT, John Heibel）
    ├── template/         # p5.js 渲染模板
    ├── references/       # 分镜 / 音乐视频 / 像素 MV 写法
    ├── scripts/          # new_project.sh、beat_grid.py
    ├── examples/         # 完整例子
    └── LICENSE
```

查找顺序（`engine.skill_dir()`）：

```
配置 ANIMATION_SKILL_DIR  >  包内 vendor/  >  老位置 _external/ai--main
```

依赖查找顺序（`capabilities.check`）：

```
技能包 bin/  >  PATH
```

所以把 `ffmpeg.exe` 丢进 `skills/work/animation/bin/`，
这个技能立刻变成「依赖齐全」——不用改 PATH，也不用装到系统里。

## 5. 依赖声明与自动隐身

大技能在 `SKILL.md` 里声明依赖：

```yaml
requires: [node, ffmpeg, chrome]
```

`registry.available(mode)` 会**过滤掉依赖不齐的技能**，并在 `describe()` 里说明缺什么：

```json
"disabled": {
  "work": [{ "name": "animation", "missing": ["ffmpeg", "chrome"] }]
}
```

效果：没装 ffmpeg 的机器上，工作模式照常可用（planning/notes/writing 都在），
动画技能自动隐身，而不是每次调用都报错。

装了依赖后调用 `skills.capabilities.refresh()` 即可重新探测（进程内缓存）。

## 6. 当前技能清单

| 技能 | 模式 | 依赖 | 做什么 |
|---|---|---|---|
| comfort | chat | 无 | 接住情绪 + 呼吸引导 |
| steady | chat, work | 无 | 危机求助渠道（**安全技能，两种模式都在**） |
| suggest | chat, work | 无 | 给一条今天能做的小步骤 |
| planning | work | 无 | 把一堆待办排成有序清单（身体 > 时限 > 配合 > 家务） |
| notes | work | 无 | 把杂乱的话整理成纪要（结论/待办/未决） |
| writing | work | 无 | 按文体给结构和语气建议 |
| animation | work | node, ffmpeg, chrome | 起后台渲染任务做动画/短片 |

## 7. 为什么技能不直接调模型

技能层是**回合内同步执行**的（`steps` → 工具 → 素材块），刻意保持零模型调用：

- 延迟可控（毫秒级，不拖慢回复）
- 成本可控（一条消息不多花 token）
- 结果可测（纯函数，同样的输入永远同样的输出）

需要模型参与的（蒸馏、摘要、动画渲染）都放进**后台任务**，不占回合。