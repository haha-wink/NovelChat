# animation 技能的外部依赖

## 必须装什么

| 依赖 | 用途 | 说明 |
|---|---|---|
| **node** | 跑 `vendor/template/render.mjs` | 有 npm 即可 |
| **ffmpeg** | 视频编码 | 必须在 PATH 里，或放到 `bin/ffmpeg.exe` |
| **无头 Chrome** | 逐帧渲染 | 常见安装路径会被自动探测，也可放 `bin/chrome/` |

装好后重启后端，或调用 `GET /api/skills/validate` 让状态刷新。

## 一键下载

```bash
python scripts/fetch_animation_deps.py            # ffmpeg + Chrome headless-shell
python scripts/fetch_animation_deps.py --dry-run  # 只看要下什么
python scripts/fetch_animation_deps.py --force    # 重下
python scripts/fetch_animation_deps.py --full-chrome   # 用完整版 Chrome（200MB）
```

下载完会自动跑一次 `-version` 验证，并落到本包的 `bin/`：

```
bin/ffmpeg.exe
bin/chrome/chrome-headless-shell.exe
```

默认用 **chrome-headless-shell**（120MB），比完整版 Chrome 小 80MB，无头渲染够用。

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

## 对上游代码的本地改动

`vendor/template/render.mjs` 有一处修改：

```js
// 上游：只有 Linux 关沙箱
const sandbox = process.platform === 'linux' ? ['--no-sandbox'] : [];
// 本地：所有平台都关
const sandbox = ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'];
```

原因：容器 / 受限环境（包括 Windows 沙箱）里开着 Chrome 沙箱，
浏览器直接起不来（Exit code 2147483651）。本地渲染不需要沙箱。

其余文件保持上游原样（`UPSTREAM_SKILL.md`、`template/` 其他文件、`references/`、`scripts/`）。
