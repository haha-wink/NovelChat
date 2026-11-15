"""动画技能（接入 painted-animation skill 包）。

## 为什么不能照搬

`painted-animation` 原本是给**有 bash 权限的 agent** 用的：
它要建项目、`npm install`、写场景代码、用无头 Chrome 逐帧渲、ffmpeg 编码，
中间还有人看图确认——**分钟级**流程。

而本项目第③层（``services/skills.py`` 的 ``Skill``）是**聊天回合内同步执行**的：
`when(打分) → steps(工具) → as_block()` 拼成【可用素材】，全程 200ms 量级。

硬塞进去 = 一次回复卡十几分钟。

## 怎么接

把技能拆成「**起任务**」和「**跑任务**」两半：

```
聊天回合内                     后台线程
animation_start  ──起任务──►   scaffold → storyboard → render → encode
  400ms 返回 job_id              每步写 status.json
        │                               │
        └── 素材: "正在做，好了发你" ─────┘
                                        │
animation_status / GET /api/animation/{id} ─┘  查进度
```

这样第③层拿到的仍然是一个**立刻返回的素材块**，符合原有契约。

## 依赖与降级

真渲染需要 node + ffmpeg + 无头 Chrome。缺任何一个：
- ``capabilities()`` 如实报告
- ``animation_start`` 返回 ``ok: False`` + 缺什么，聊天里说清而不是假装在做
- 设 ``ANIMATION_RENDERER=fake`` 可跑通全流程（测试用，不产真视频）

MIT 许可（John Heibel），模板拷自技能包的 ``template/``。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.tools import protocol as tool_protocol
from core.config import get_settings
from core.paths import DATA_DIR, PROJECT_ROOT

logger = logging.getLogger(__name__)

#: 任务状态流转
STAGE_QUEUED = "queued"
STAGE_SCAFFOLD = "scaffold"
STAGE_STORYBOARD = "storyboard"
STAGE_AWAITING = "awaiting_confirmation"   # 歌词 MV：分镜要先给用户看
STAGE_RENDER = "render"
STAGE_ENCODE = "encode"
STAGE_DONE = "done"
STAGE_FAILED = "failed"

#: 触发词（用户明确要一段动的东西）
ANIMATION_HINTS = (
    "动画", "短片", "视频", "片头", "mv", "MV", "动图", "gif", "GIF",
    "做一段", "剪一段", "渲染", "水彩动画", "像素动画", "歌词",
)

#: 需要先确认分镜的类型（技能包里的硬门禁）
STORYBOARD_GATE_HINTS = ("歌词", "mv", "MV", "音乐", "歌曲", "配上歌")

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# 能力探测
# ---------------------------------------------------------------------------

def _which(*names: str) -> Optional[str]:
    """先找包内 bin/，再找 PATH。"""
    for name in names:
        for candidate in (BIN_DIR / name, BIN_DIR / f"{name}.exe"):
            if candidate.is_file():
                return str(candidate)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _find_chrome() -> Optional[str]:
    # 包内自带的 Chrome 优先
    for candidate in (BIN_DIR / "chrome" / "chrome.exe",
                      BIN_DIR / "chrome" / "chrome",
                      BIN_DIR / "chrome-linux64" / "chrome",
                      BIN_DIR / "chrome.exe"):
        if candidate.is_file():
            return str(candidate)

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for item in candidates:
        if item and Path(item).exists():
            return item
    return None


#: 本技能包目录（engine.py 所在位置）
PACKAGE_DIR = Path(__file__).resolve().parent
#: 包内自带的引擎素材
VENDOR_DIR = PACKAGE_DIR / "vendor"
#: 可选：把 ffmpeg / chrome 放进这里，包就完全自包含
BIN_DIR = PACKAGE_DIR / "bin"


def skill_dir() -> Path:
    """上游引擎目录（要有 template/）。

    优先级：配置 > 包内 vendor/ > 老位置 _external/ai--main。
    包内带了一份拷贝，所以整个技能包可以单独拷走。
    """
    configured = (get_settings().animation_skill_dir or "").strip()
    if configured:
        return Path(configured)
    if (VENDOR_DIR / "template").exists():
        return VENDOR_DIR
    return PROJECT_ROOT / "_external" / "ai--main"


def capabilities() -> dict:
    """这台机器能不能真渲染。"""
    renderer = (get_settings().animation_renderer or "auto").strip().lower()
    fake = renderer == "fake"
    node = _which("node")
    ffmpeg = _which("ffmpeg")
    chrome = _find_chrome()
    root = skill_dir()
    # 包内 vendor/ 没有自己的 SKILL.md（上游那份改名 UPSTREAM_SKILL.md），
    # 所以按"有没有 template/"判断素材齐不齐
    has_package = (root / "template").exists() or (root / "SKILL.md").exists()
    ok = fake or bool(node and ffmpeg and chrome and has_package)
    bundled = sorted(
        item.name for item in BIN_DIR.iterdir()
    ) if BIN_DIR.exists() else []
    return {
        "renderer": "fake" if fake else "real",
        "available": ok,
        "skill_dir": str(root),
        "skill_package": has_package,
        #: 包内 bin/ 里放了什么（ffmpeg.exe / chrome/ …）
        "bundled": bundled,
        "self_contained": bool(bundled) or has_package,
        "node": node or "",
        "ffmpeg": ffmpeg or "",
        "chrome": chrome or "",
        "missing": [
            name for name, value in (
                ("node", node), ("ffmpeg", ffmpeg), ("chrome", chrome),
            ) if not value and not fake
        ] + ([] if has_package or fake else ["skill 包"]),
    }


def available() -> bool:
    return bool(capabilities()["available"])


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

def jobs_dir() -> Path:
    path = Path(DATA_DIR) / "animation"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_status(job: dict) -> None:
    try:
        target = Path(job["dir"]) / "status.json"
        target.write_text(
            json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 - 状态写不进去不该影响渲染
        logger.exception("写任务状态失败")


def _touch(job_id: str, stage: str, **extra) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(extra)
        job["stage"] = stage
        job["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_status(job)


def _needs_storyboard(prompt: str) -> bool:
    text = str(prompt or "")
    return any(hint in text for hint in STORYBOARD_GATE_HINTS)


def _fake_render(job_id: str, project: Path, seconds: float = 0.6) -> str:
    """测试用渲染器：不碰 node/ffmpeg，产出占位文件。"""
    out = project / "out"
    out.mkdir(parents=True, exist_ok=True)
    time.sleep(seconds)
    target = out / "video.mp4"
    target.write_bytes(b"\x00\x00\x00\x18ftypmp42fake-render")
    return str(target)


def _real_render(job_id: str, project: Path) -> str:
    """真渲染：调技能包里的 render.mjs。"""
    node = _which("node")
    if not node:
        raise RuntimeError("没有 node，无法渲染")
    chrome = _find_chrome()
    cmd = [node, "render.mjs", "--clip", "--out=out/video.mp4"]
    if chrome:
        cmd.append(f"--chrome={chrome}")
    logger.info("开始渲染：%s", " ".join(cmd))
    completed = subprocess.run(
        cmd,
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=int(get_settings().animation_render_timeout or 900),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"render.mjs 退出码 {completed.returncode}："
            f"{(completed.stderr or completed.stdout or '')[-400:]}"
        )
    target = project / "out" / "video.mp4"
    if not target.exists():
        raise RuntimeError("渲染结束但没找到 out/video.mp4")
    return str(target)


def _run_job(job_id: str) -> None:
    """后台线程：脚手架 → 分镜 → 渲染 → 编码。"""
    with _LOCK:
        job = dict(_JOBS.get(job_id) or {})
    project = Path(job.get("dir") or "") / "project"

    try:
        _touch(job_id, STAGE_SCAFFOLD)

        caps = capabilities()
        if caps["renderer"] == "fake":
            project.mkdir(parents=True, exist_ok=True)
        else:
            template = skill_dir() / "template"
            if not template.exists():
                raise RuntimeError(f"技能包模板不存在：{template}")
            if project.exists():
                shutil.rmtree(project, ignore_errors=True)
            shutil.copytree(template, project)

        # 分镜文件：技能包要求先写下来（歌词 MV 还要人确认）
        _touch(job_id, STAGE_STORYBOARD)
        storyboard = Path(job["dir"]) / "STORYBOARD.md"
        storyboard.write_text(
            "# 分镜（草稿）\n\n"
            f"- 需求：{job.get('prompt') or ''}\n"
            f"- 风格：{job.get('style') or 'watercolor'}\n"
            "- 说明：由 animation 技能自动生成，逐镜细节需要人工补充。\n",
            encoding="utf-8",
        )

        if job.get("requires_confirmation"):
            _touch(job_id, STAGE_AWAITING,
                   note="歌词/MV 类需要先确认分镜，确认后再渲染")
            return

        _touch(job_id, STAGE_RENDER)
        if caps["renderer"] == "fake":
            video = _fake_render(job_id, project)
        else:
            video = _real_render(job_id, project)

        _touch(job_id, STAGE_ENCODE)
        size = Path(video).stat().st_size if Path(video).exists() else 0
        _touch(
            job_id, STAGE_DONE,
            video=video, size=size,
            note="渲染完成" if size else "渲染完成（产物为空）",
        )
        logger.info("动画任务完成：%s -> %s", job_id, video)

    except Exception as exc:  # noqa: BLE001 - 任务失败要让用户能查到原因
        logger.exception("动画任务失败：%s", job_id)
        _touch(job_id, STAGE_FAILED, error=str(exc)[:400])


def create_job(
    prompt: str,
    *,
    style: str = "watercolor",
    user_id: str = "",
    background: bool = True,
) -> dict:
    """起一个渲染任务，立刻返回（渲染在后台线程）。"""
    job_id = f"anim_{uuid.uuid4().hex[:12]}"
    folder = jobs_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id,
        "prompt": str(prompt or "")[:2000],
        "style": str(style or "watercolor"),
        "user_id": str(user_id or ""),
        "stage": STAGE_QUEUED,
        "requires_confirmation": _needs_storyboard(prompt),
        "dir": str(folder),
        "video": "",
        "error": "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _LOCK:
        _JOBS[job_id] = job
    _write_status(job)

    if background:
        thread = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
        thread.start()
    else:
        _run_job(job_id)
    return job_status(job_id) or job


def job_status(job_id: str) -> Optional[dict]:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            return dict(job)
    # 进程重启后从磁盘读
    path = jobs_dir() / str(job_id) / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def list_jobs(limit: int = 20) -> list[dict]:
    with _LOCK:
        items = [dict(job) for job in _JOBS.values()]
    if not items:
        for folder in sorted(jobs_dir().glob("anim_*"), reverse=True)[:limit]:
            path = folder / "status.json"
            if path.exists():
                try:
                    items.append(json.loads(path.read_text(encoding="utf-8")))
                except Exception:  # noqa: BLE001
                    continue
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items[:limit]


def confirm(job_id: str) -> dict:
    """确认分镜，继续渲染（歌词/MV 类任务需要走这一步）。"""
    job = job_status(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在"}
    if job.get("stage") != STAGE_AWAITING:
        return {"ok": False, "error": f"当前状态是 {job.get('stage')}，不需要确认"}
    with _LOCK:
        live = _JOBS.get(job_id)
        if live is not None:
            live["requires_confirmation"] = False
    thread = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    thread.start()
    return {"ok": True, "job": job_status(job_id)}


# ---------------------------------------------------------------------------
# 工具层（第①层：协议）——第三层 Skill 通过它们起任务
# ---------------------------------------------------------------------------

_STAGE_LABELS = {
    STAGE_QUEUED: "排队中",
    STAGE_SCAFFOLD: "搭项目",
    STAGE_STORYBOARD: "写分镜",
    STAGE_AWAITING: "等确认分镜",
    STAGE_RENDER: "逐帧渲染",
    STAGE_ENCODE: "编码视频",
    STAGE_DONE: "完成",
    STAGE_FAILED: "失败",
}


def _tool_capabilities(args: dict) -> dict:
    caps = capabilities()
    if caps["available"]:
        return {"ok": True, "text": "这台机器可以渲动画", **caps}
    return {
        "ok": False,
        "error": "缺少：" + "、".join(caps["missing"]),
        **caps,
    }


def _tool_start(args: dict) -> dict:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "缺少 prompt（要做什么动画）"}

    caps = capabilities()
    if not caps["available"]:
        # 不装死：把"缺什么"当成素材交回去，模型会如实告诉用户
        return {
            "ok": True,
            "ready": False,
            "text": "动画功能还没法用：这台机器缺少 "
                    + "、".join(caps["missing"]) + "，装好之后我就能做。",
            "missing": caps["missing"],
        }

    style = str(args.get("style") or "watercolor")
    job = create_job(prompt, style=style, user_id=str(args.get("user_id") or ""))
    label = _STAGE_LABELS.get(job["stage"], job["stage"])
    if job["stage"] == STAGE_AWAITING:
        text = f"分镜我先写好了（任务 {job['id']}），你确认一下我再开始渲染。"
    elif job["stage"] == STAGE_FAILED:
        return {"ok": False, "error": job.get("error") or "渲染失败", "job_id": job["id"]}
    else:
        text = f"动画已经开做了（任务 {job['id']}，当前：{label}），好了我把成片发你。"
    return {"ok": True, "text": text, "job_id": job["id"], "stage": job["stage"]}


def _tool_status(args: dict) -> dict:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return {"ok": False, "error": "缺少 job_id"}
    job = job_status(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在"}
    label = _STAGE_LABELS.get(job["stage"], job["stage"])
    if job["stage"] == STAGE_DONE:
        text = f"动画做好了：{job.get('video') or ''}"
    elif job["stage"] == STAGE_FAILED:
        text = f"动画做失败了：{job.get('error') or '未知原因'}"
    elif job["stage"] == STAGE_AWAITING:
        text = "分镜写好了，等你确认再开始渲染。"
    else:
        text = f"还在做：{label}"
    return {"ok": True, "text": text, **job}


tool_protocol.register(
    tool_protocol.ToolSpec(
        name="animation_start",
        description="用户要一段动画/短片/动图时：起一个渲染任务（异步，立刻返回）",
        parameters={
            "prompt": {"type": "string", "description": "要做什么动画，用用户的原话"},
            "style": {"type": "string",
                      "description": "watercolor（手绘水彩，默认）/ pixel（像素风）"},
        },
        handler=_tool_start,
        required=("prompt",),
        domain="media",
    )
)

tool_protocol.register(
    tool_protocol.ToolSpec(
        name="animation_status",
        description="查一个动画任务的进度和产物路径",
        parameters={"job_id": {"type": "string", "description": "任务 id"}},
        handler=_tool_status,
        required=("job_id",),
        domain="media",
    )
)

tool_protocol.register(
    tool_protocol.ToolSpec(
        name="animation_capabilities",
        description="这台机器能不能渲染动画（node / ffmpeg / Chrome 是否齐全）",
        parameters={},
        handler=_tool_capabilities,
        domain="media",
    )
)


def describe() -> dict:
    caps = capabilities()
    return {
        "available": caps["available"],
        "renderer": caps["renderer"],
        "missing": caps["missing"],
        "skill_dir": caps["skill_dir"],
        "tools": ["animation_start", "animation_status", "animation_capabilities"],
        "stages": list(_STAGE_LABELS.values()),
        "jobs": len(list_jobs(limit=200)),
    }