# animation 技能的外部依赖

## 必须装什么

| 依赖 | 用途 | 说明 |
|---|---|---|
| **node** | 跑 `vendor/template/render.mjs` | 有 npm 即可 |
| **ffmpeg** | 视频编码 | 必须在 PATH 里，或放到 `bin/ffmpeg.exe` |
| **无头 Chrome** | 逐帧渲染 | 常见安装路径会被自动探测，也可放 `bin/chrome/` |

装好后重启后端，或调用 `GET /api/skills/validate` 让状态刷新。

## 能不能放进包里

可以。把可执行文件放进本包的 `bin/` 目录，引擎会**优先**用包内的：

```
skills/work/animation/bin/
├── ffmpeg.exe            # 或 ffmpeg
└── chrome/
    └── chrome.exe        # 或 chrome-linux64/chrome
```

放进去以后这个技能包就是**完全自包含**的，拷到别的机器也能用。

## 包内结构

```
skills/work/animation/
├── SKILL.md              # 技能声明（模式/触发词/步骤）
├── engine.py             # 工具实现（起任务、查进度、能力探测）
├── DEPS.md               # 本文件
├── bin/                  # 可选：放 ffmpeg / chrome
└── vendor/               # 上游引擎（MIT, John Heibel）
    ├── template/         # p5.js 渲染模板 + ANIMATION_GUIDE.md
    ├── references/       # 分镜、音乐视频、像素 MV 的写法
    ├── scripts/          # new_project.sh、beat_grid.py
    ├── examples/         # 一个完整例子
    ├── UPSTREAM_SKILL.md # 上游的技能说明（原样保留）
    └── LICENSE
```
