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
import os
import re
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


def _find_npm() -> Optional[str]:
    """找 npm 可执行文件。

    Windows 上 ``shutil.which("npm")`` 可能返回 ``npm.ps1``——那是 PowerShell 脚本，
    subprocess 直接跑会失败（rc=4294963248）。所以优先找同目录的 ``npm.cmd``。
    """
    for candidate in (BIN_DIR / "npm.cmd", BIN_DIR / "npm"):
        if candidate.is_file():
            return str(candidate)

    node = _which("node")
    if node:
        folder = Path(node).parent
        for name in ("npm.cmd", "npm.bat", "npm"):
            candidate = folder / name
            if candidate.is_file():
                return str(candidate)

    for name in ("npm.cmd", "npm.bat", "npm"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _find_chrome() -> Optional[str]:
    # 包内自带的 Chrome 优先（含 headless-shell：比全量 Chrome 小 80MB）
    for candidate in (
        BIN_DIR / "chrome" / "chrome.exe",
        BIN_DIR / "chrome" / "chrome",
        BIN_DIR / "chrome" / "chrome-headless-shell.exe",
        BIN_DIR / "chrome" / "chrome-headless-shell",
        BIN_DIR / "chrome-linux64" / "chrome",
        BIN_DIR / "chrome-headless-shell.exe",
        BIN_DIR / "chrome.exe",
    ):
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


def _job_env(project: Path) -> dict:
    """给子进程一份干净环境：缓存和临时目录都落在项目内。

    默认的 TEMP 在用户目录，沙箱（以及某些 Docker 配置）写不进去——
    Puppeteer 建临时 profile 时会直接 EPERM。
    """
    cache = project / ".npm-cache"
    temp = project / ".tmp"
    cache.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    # 渲染脚本会直接 spawn("ffmpeg")，所以把技能包的 bin/ 塞进 PATH。
    # 这样即使 ffmpeg 没装到系统里，包内那份也能被找到。
    if BIN_DIR.exists():
        env["PATH"] = str(BIN_DIR) + os.pathsep + env.get("PATH", "")
    env["npm_config_cache"] = str(cache)
    env["npm_config_logs_dir"] = str(cache / "_logs")
    env["npm_config_update_notifier"] = "false"
    env["TMPDIR"] = str(temp)
    env["TEMP"] = str(temp)
    env["TMP"] = str(temp)
    return env


def _ensure_node_modules(job_id: str, project: Path) -> None:
    """第一次渲染前装依赖（p5 / p5.brush / puppeteer-core）。

    上游脚手架脚本会顺手 npm install，我们这边是异步任务，所以自己补这一步。
    装过就跳过（判断 node_modules 是否存在）。
    """
    node_modules = project / "node_modules"
    if node_modules.exists() and any(node_modules.iterdir()):
        return

    npm = _find_npm()
    if not npm:
        raise RuntimeError("没有 npm，无法安装渲染依赖")

    _touch(job_id, STAGE_SCAFFOLD, note="安装渲染依赖（首次较慢）")
    logger.info("安装渲染依赖：%s", project)
    env = _job_env(project)

    completed = subprocess.run(
        [npm, "install", "--no-audit", "--no-fund"],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=int(get_settings().animation_render_timeout or 900),
        env=env,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "")[-300:]
        raise RuntimeError(f"npm install 失败（{completed.returncode}）：{tail}")


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
        env=_job_env(project),
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

    if not job:
        # 进程重启后内存里没有：从 status.json 捞回来，否则 confirm 续跑会直接崩
        job = job_status(job_id) or {}
        if not job:
            logger.warning("动画任务不存在，无法续跑：%s", job_id)
            return
        with _LOCK:
            _JOBS[job_id] = dict(job)

    project = Path(job.get("dir") or "") / "project"

    lyric_info: dict = {}
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
        lyric_lines = [text for _s, _e, text in _lyric_timeline(job)[0]]
        if not lyric_lines:
            lyric_lines = _lyric_lines(job)
        if lyric_lines:
            table = "\n".join(
                f"| {index * 3:.0f}–{index * 3 + 3:.0f}s | {line} | （待填画面） |"
                for index, line in enumerate(lyric_lines)
            )
            storyboard.write_text(
                "# 分镜（草稿）\n\n"
                f"- 需求：{job.get('prompt') or ''}\n"
                f"- 风格：{job.get('style') or 'watercolor'}\n"
                f"- 歌词：{len(lyric_lines)} 行\n\n"
                "| 时间 | 歌词 | 画面事件 |\n|------|------|----------|\n"
                + table + "\n",
                encoding="utf-8",
            )
        else:
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

        # 歌词 MV：把歌词真正填进模板（不然渲染出来只是模板默认画面）
        lyric_info = _prepare_lyric_project(project, job)
        if lyric_info["lines"]:
            _touch(job_id, STAGE_RENDER,
                   note=f"填入 {lyric_info['lines']} 行歌词（{lyric_info['duration']}s）")

        _touch(job_id, STAGE_RENDER)
        if caps["renderer"] == "fake":
            video = _fake_render(job_id, project)
        else:
            _ensure_node_modules(job_id, project)
            _touch(job_id, STAGE_RENDER, note="逐帧渲染")
            video = _real_render(job_id, project)

        _touch(job_id, STAGE_ENCODE)
        if lyric_info.get("audio"):
            video = _mux_audio(video, lyric_info["audio"])
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
    lyrics: str = "",
    audio: str = "",
    duration: float = 0.0,
    lrc: str = "",
    lyrics_file: str = "",
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
        "lyrics": str(lyrics or "")[:8000],
        "lrc": str(lrc or "")[:20000],
        "lyrics_file": str(lyrics_file or ""),
        "audio": str(audio or ""),
        "duration": float(duration or 0),
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
        if live is None:
            # 重启后从磁盘读出来的任务：先放进内存再续跑
            live = dict(job)
            _JOBS[job_id] = live
        live["requires_confirmation"] = False
        live["stage"] = STAGE_SCAFFOLD
        live["note"] = "已确认分镜，开始渲染"
        _write_status(live)
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

    # 歌词可以从参数给，也可以直接写在 prompt 里（"…配这首歌" 后面换行开始）
    lyrics = str(args.get("lyrics") or "").strip()
    if not lyrics and "\n" in prompt:
        head, _, rest = prompt.partition("\n")
        if _needs_storyboard(head) and rest.strip():
            lyrics = rest.strip()
            prompt = head.strip()

    # 上传的歌词文件优先（LRC 有真实时间轴）
    lyrics_file = str(args.get("lyrics_file") or "").strip()

    needs_confirm = _needs_storyboard(prompt) or bool(lyrics) or bool(lyrics_file)
    lrc = ""
    if lyrics_file and Path(lyrics_file).is_file():
        try:
            lrc = Path(lyrics_file).read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            lrc = ""

    job = create_job(
        prompt, style=style, user_id=str(args.get("user_id") or ""),
        lyrics=lyrics,
        lrc=lrc,
        lyrics_file=lyrics_file,
        audio=str(args.get("audio") or ""),
        duration=float(args.get("duration") or 0),
    )
    label = _STAGE_LABELS.get(job["stage"], job["stage"])

    # 注意：create_job 是异步起线程的，这里 stage 通常还是 scaffold。
    # 所以要按 prompt 提前判断"会不会停在等确认"，否则用户以为已经在做了，
    # 结果永远停在 awaiting_confirmation（歌词 MV 就是这么卡住的）
    if needs_confirm or job["stage"] == STAGE_AWAITING:
        text = (f"分镜我先写好了（任务 {job['id']}），你确认一下我再开始渲染。"
                "在右侧任务栏点「确认分镜」就行。")
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
            "lyrics": {"type": "string",
                       "description": "歌词全文（多行）。歌词 MV 用，会逐句打在画面上"},
            "lyrics_file": {"type": "string",
                            "description": "歌词文件路径（.lrc 有真实时间轴，.txt 均分）"},
            "audio": {"type": "string",
                      "description": "配乐文件路径（可选），渲染完会混进成片"},
            "duration": {"type": "number",
                         "description": "时长秒（可选），不给就按音频时长/歌词行数估"},
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

# ---------------------------------------------------------------------------
# 任务清理：磁盘会一直涨，留最近若干个就行
# ---------------------------------------------------------------------------

def _job_folders() -> list[tuple[str, float, Path]]:
    """(job_id, 创建时间戳, 目录) 按时间倒序。"""
    items = []
    for folder in jobs_dir().glob("anim_*"):
        if not folder.is_dir():
            continue
        stamp = 0.0
        status = folder / "status.json"
        if status.exists():
            try:
                stamp = status.stat().st_mtime
            except OSError:
                stamp = 0.0
        items.append((folder.name, stamp, folder))
    items.sort(key=lambda row: row[1], reverse=True)
    return items


def delete_job(job_id: str) -> dict:
    """删掉一个任务（状态 + 整个项目目录）。正在跑的拒绝删。"""
    job = job_status(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在"}
    # 真正在跑 = 本进程里有活的任务对象。进程重启后残留的目录虽然状态写着
    # "render"，其实早就是僵尸了，必须允许删（否则用户永远清不掉）
    live = job_id in _JOBS
    recent = False
    if live:
        stamp_path = Path(str(job.get("dir") or "")) / "status.json"
        stamp = stamp_path.stat().st_mtime if stamp_path.exists() else 0.0
        recent = (time.time() - stamp) < 10 * 60
    if live and recent and job.get("stage") not in ("done", "failed"):
        return {"ok": False, "error": "任务还在跑，等它结束再删"}

    folder = Path(str(job.get("dir") or "")) or (jobs_dir() / job_id)
    try:
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        else:
            shutil.rmtree(jobs_dir() / job_id, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"删除失败：{exc}"}

    with _LOCK:
        _JOBS.pop(job_id, None)
    logger.info("动画任务已删除：%s", job_id)
    return {"ok": True, "job_id": job_id}


def cleanup(keep: int = 10, max_mb: int = 2000, stale_hours: float = 6.0) -> dict:
    """清理老任务：只留最近 keep 个，且总量不超过 max_mb。

    只动 done / failed 的；正在跑的一律不碰。
    """
    # keep=0 表示一个都不留
    keep = max(0, int(keep))
    folders = _job_folders()

    def size_of(folder: Path) -> int:
        total = 0
        for item in folder.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        return total

    removed: list[str] = []
    freed = 0
    kept: list[str] = []
    total_bytes = 0
    limit = max(1, int(max_mb)) * 1024 * 1024

    now = time.time()
    for index, (job_id, stamp, folder) in enumerate(folders):
        job = job_status(job_id) or {}
        running = job.get("stage") not in ("done", "failed", None)
        # 卡在"渲染中"但几小时没动静的，是进程被杀留下的僵尸，按垃圾处理
        stale = running and (now - stamp) > stale_hours * 3600
        if running and not stale and job_id in _JOBS:
            kept.append(job_id)
            continue

        size = size_of(folder)
        too_many = index >= keep
        too_big = (total_bytes + size) > limit and total_bytes > 0
        if too_many or too_big:
            result = delete_job(job_id)
            if result.get("ok"):
                removed.append(job_id)
                freed += size
            continue

        kept.append(job_id)
        total_bytes += size

    if removed:
        logger.info("动画任务清理：删除 %d 个，回收 %.1f MB",
                    len(removed), freed / 1024 / 1024)
    return {
        "ok": True,
        "removed": removed,
        "freed_mb": round(freed / 1024 / 1024, 1),
        "kept": len(kept),
    }


def stats() -> dict:
    """任务占用情况（界面上显示"共 X 个 / Y MB"）。"""
    folders = _job_folders()
    total = 0
    for _job_id, _stamp, folder in folders:
        for item in folder.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    return {"count": len(folders), "total_mb": round(total / 1024 / 1024, 1)}

# ---------------------------------------------------------------------------
# 歌词 MV：把歌词真正填进模板的 karaoke.js / config.js
# ---------------------------------------------------------------------------

def _lyric_lines(job: dict) -> list[str]:
    """从任务里取歌词行（用户给的多行文本）。"""
    raw = str(job.get("lyrics") or "").strip()
    if not raw:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _probe_duration(path: Path) -> float:
    """用 ffmpeg 读音频时长（读不出来就返回 0）。"""
    ffmpeg = _which("ffmpeg")
    if not ffmpeg:
        return 0.0
    try:
        result = subprocess.run(
            [ffmpeg, "-i", str(path)], capture_output=True, text=True, timeout=60,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr or "")
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _prepare_lyric_project(project: Path, job: dict) -> dict:
    """把歌词按**真实时间轴**写进 src/karaoke.js 的 LY，总时长写进 config.js。

    LRC 给到时间戳就按 LRC，否则按音频时长均分，再否则每句 3 秒。
    """
    timeline, duration = _lyric_timeline(job)
    if not timeline:
        return {"lines": 0, "duration": 0, "audio": "", "lrc": False}

    timed = any(entry[1] - entry[0] < 3.5 for entry in timeline)
    entries = [
        f"  [{start}, {end}, {json.dumps(text, ensure_ascii=False)}],"
        for start, end, text in timeline
    ]
    ly_block = "const LY = [\n" + "\n".join(entries) + "\n];"

    karaoke = project / "src" / "karaoke.js"
    if karaoke.exists():
        source = karaoke.read_text(encoding="utf-8")
        head = source.find("const LY = [")
        if head >= 0:
            tail = source.find("];", head)
            if tail > head:
                source = source[:head] + ly_block + source[tail + 2:]
                karaoke.write_text(source, encoding="utf-8")

    config = project / "src" / "config.js"
    if config.exists():
        source = config.read_text(encoding="utf-8")
        style = str(job.get("style") or "watercolor").lower()
        if style == "pixel":
            # 像素歌词模式：跳过水彩纸纹，歌词画成 RPG 对话框
            replacement = (
                f"const PROJECT = {{\n  duration: {duration:.2f}, bpm: 120, offset: 0,\n"
                f"  pixel: true, pixelGrid: [320, 180], lyricUI: 'rpgBox',\n}};"
            )
        else:
            replacement = f"const PROJECT = {{\n  duration: {duration:.2f}, bpm: 120, offset: 0,\n}};"
        start = source.find("const PROJECT = {")
        if start >= 0:
            end = source.find("};", start)
            if end > start:
                source = source[:start] + replacement + source[end + 2:]
                config.write_text(source, encoding="utf-8")

    audio = str(job.get("audio") or "").strip()
    return {
        "lines": len(timeline),
        "duration": duration,
        "audio": audio if audio and Path(audio).exists() else "",
        "lrc": bool(str(job.get("lrc") or "").strip()),
    }


def _mux_audio(video: str, audio: str) -> str:
    """把配乐混进成片；失败就保留无声版（不掩盖画面成果）。"""
    ffmpeg = _which("ffmpeg")
    if not ffmpeg or not audio or not Path(audio).exists():
        return video
    target = str(Path(video).with_name("video_with_audio.mp4"))
    try:
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", video, "-i", audio,
             "-c:v", "copy", "-c:a", "aac", "-shortest", target],
            capture_output=True, text=True, timeout=600,
        )
        if Path(target).exists() and Path(target).stat().st_size > 0:
            logger.info("已混入配乐：%s", target)
            return target
    except Exception:  # noqa: BLE001
        logger.exception("混音失败（保留无声版）")
    return video

#: 一行开头的时间戳（一行可能有多个，比如副歌重复）
_LRC_STAMP = re.compile(r"^\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_LRC_OFFSET = re.compile(r"\[offset:\s*(-?\d+)\]", re.IGNORECASE)
_LRC_META = re.compile(r"^\[(ti|ar|al|by|re|ve|offset):", re.IGNORECASE)


def parse_lrc(text: str) -> list[dict]:
    """解析 LRC，返回 [{start, text}]（按时间排序）。

    支持一行多个时间戳（`[00:05.20][00:12.00]同一句`）、`[offset:±ms]`；
    没有时间戳的纯文本行放在最后，start 为 None。
    """
    raw = str(text or "")
    offset = 0.0
    match = _LRC_OFFSET.search(raw)
    if match:
        offset = float(match.group(1)) / 1000.0

    timed: list[dict] = []
    untimed: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or _LRC_META.match(line):
            continue

        stamps: list[float] = []
        rest = line
        while True:
            found = _LRC_STAMP.match(rest)
            if not found:
                break
            minutes, seconds, fraction = found.groups()
            value = int(minutes) * 60 + int(seconds)
            if fraction:
                value += int(fraction) / (10 ** len(fraction))
            stamps.append(round(value + offset, 3))
            rest = rest[found.end():]

        content = rest.strip()
        if not stamps:
            if content:
                untimed.append({"start": None, "text": content})
            continue
        for start in stamps:
            timed.append({"start": start, "text": content})

    timed.sort(key=lambda item: item["start"])
    return timed + untimed


def _lyric_timeline(job: dict) -> tuple[list[tuple[float, float, str]], float]:
    """把歌词整理成 [(start, end, text)]，并给出总时长。

    优先级：LRC 时间戳 > 音频时长均分 > 每句 3 秒均分。
    """
    lrc_text = str(job.get("lrc") or "").strip()
    if not lrc_text:
        lrc_path = str(job.get("lyrics_file") or "").strip()
        if lrc_path and Path(lrc_path).is_file():
            try:
                lrc_text = Path(lrc_path).read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                lrc_text = ""

    audio = str(job.get("audio") or "").strip()
    audio_path = Path(audio) if audio and Path(audio).exists() else None
    total = float(job.get("duration") or 0)
    if not total and audio_path is not None:
        total = _probe_duration(audio_path)

    if lrc_text:
        parsed = parse_lrc(lrc_text)
        timed = [item for item in parsed if item["start"] is not None]
        if timed:
            result = []
            for index, item in enumerate(timed):
                start = float(item["start"])
                if index + 1 < len(timed):
                    end = float(timed[index + 1]["start"])
                else:
                    end = start + 3.0
                # 收尾留一点，避免最后一句立刻消失
                result.append((round(start, 3), round(max(start + 0.6, end - 0.12), 3),
                               item["text"]))
            if not total:
                total = result[-1][1] + 2.0
            return result, round(total, 3)

    lines = _lyric_lines(job)
    if not lines:
        return [], round(total, 3)

    per = (total / len(lines)) if total else 3.0
    total = total or max(6.0, per * len(lines))
    result = []
    for index, line in enumerate(lines):
        start = round(index * per, 3)
        end = round(min(total, start + per * 0.92), 3)
        result.append((start, end, line))
    return result, round(total, 3)
