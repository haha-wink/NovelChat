# 接入外部 Skill：painted-animation

> 把 [`ryan123-hub/ai-`](https://github.com/ryan123-hub/ai-) 这个 `painted-animation` 技能接进本项目的第③层。
> MIT 许可（Copyright © 2026 John Heibel），模板拷自技能包的 `template/`。

## 1. 为什么不能直接塞进 Skill.steps

原来的 `Skill.run()` 是**同步**的：

```python
for tool_name, base_args in self.steps:
    output = protocol.execute(tool_name, args)   # 一次聊天回合内跑完
    result.materials.append(output["text"])      # 拼成【可用素材】
```

而 painted-animation 的真实流程是：

```
建项目 → npm install → 写场景代码 → 无头 Chrome 渲几百帧 → ffmpeg 编码 → 人工审图
                                                        ≈ 分钟级
```

直接塞进去 = **一次聊天回复卡十几分钟**。

## 2. 拆成两半：起任务 / 跑任务

```
┌─ 聊天回合内（<500ms）────────────────────────────┐
│  skills.choose()  命中 animation                  │
│  → Skill.steps: animation_start                   │
│  → 返回素材:"动画已经开做了（任务 anim_xxx），     │
│              好了我把成片发你。"                   │
└───────────────────────┬───────────────────────────┘
                        │ 立刻返回
┌───────────────────────▼───────────────────────────┐
│  后台线程 _run_job                                 │
│  scaffold → storyboard → render → encode → done    │
│  每步写 <job>/status.json                          │
└───────────────────────┬───────────────────────────┘
                        │
   GET /api/animation/{job_id}   查进度
   POST /api/animation/{id}/confirm   确认分镜后继续
```

第③层拿到的仍然是**立刻返回的素材块**，原有契约没被破坏。

## 3. 三层怎么落的

| 层 | 文件 | 内容 |
|---|---|---|
| ① 协议层 | `core/tool_protocol.py` 注册 | `animation_start` / `animation_status` / `animation_capabilities` |
| ② 框架层 | `agent/tools/langgraph_tools.py` | 自动出现在 `describe()` 里，可被模型当 function call 调 |
| ③ 业务层 | `services/skills.py` | `Skill(name="animation", when=_when_animation, steps=[("animation_start", {"prompt": ""})])` |

图上的位置没变，还是 `plan → skill → generate`，只是这一次 `skill` 节点起的是**异步任务**。

## 4. 触发判据

```python
ANIMATION_HINTS   = ("动画", "短片", "视频", "MV", "动图", "gif", "渲染", "歌词", …)
_ANIMATION_ACTIONS = ("帮我做", "给我做", "做一段", "生成", "剪一段", "做动画", …)
```

| 用户说 | 命中 | 理由 |
|---|---|---|
| 帮我做一段水彩动画，雨夜的车站 | **animation** 0.85 | 有名词 + 动作词 |
| 我看视频也难受 | comfort 0.9 | 只提到"视频"，没有动作词 |
| 今天好累 | comfort 0.9 | 无关 |
| 帮我做个动画（危机时） | steady 1.0 | `crisis` 直接判 0，不抢戏 |

**"提到视频"和"要我做视频"是两回事**——只靠关键词会让情绪倾诉被误判成创作需求。

## 5. 依赖与降级

真渲染需要 **node + ffmpeg + 无头 Chrome**。

```json
GET /api/animation/capabilities
{ "renderer": "real", "available": false,
  "missing": ["ffmpeg", "chrome"],
  "skill_dir": "_external/ai--main", "skill_package": true }
```

- 缺依赖时 `animation_start` **不装死**：返回 `ok=True, ready=False`，正文是
  「动画功能还没法用：这台机器缺少 ffmpeg、chrome，装好之后我就能做。」
  —— 模型会照实转告用户，而不是假装在做
- `ANIMATION_RENDERER=fake` → 假渲染器，跑通全流程但不产真视频（测试用）

## 6. 硬门禁：歌词 MV 必须先确认分镜

技能包规定「有 LRC 的片子必须先交 STORYBOARD.md 并确认」。这里照做：

```
prompt 命中 歌词/MV/音乐/歌曲
  → 写完 STORYBOARD.md 就停在 awaiting_confirmation
  → POST /api/animation/{id}/confirm 才继续渲染
```

非歌词类自动往下跑。

## 7. 产物

```
backend/data/animation/<job_id>/
├── STORYBOARD.md
├── status.json          # 阶段、产物路径、错误
└── project/             # 从技能包 template 拷来的完整工程
    └── out/video.mp4
```

## 8. 还没做的

- **流式进度**：现在只能轮询 `GET /api/animation/{id}`，没接 SSE
- **产物分发**：`video.mp4` 还没挂到静态目录，聊天里发不出去（拿到路径后得手动取）
- **分发自动化**：`_when_animation` 是关键词判据，复杂意图（"给我做个能循环的动图"）会漏
- **并发限流**：多个任务同时渲染会抢 CPU，没做队列