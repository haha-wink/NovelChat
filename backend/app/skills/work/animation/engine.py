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
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.tools import protocol as tool_protocol
from core.config import get_settings
from core.paths import DATA_DIR, PROJECT_ROOT

try:  # 同包内的风格目录（单独跑 engine.py 时退回绝对导入）
    from . import styles as style_catalog
except ImportError:  # pragma: no cover
    from skills.work.animation import styles as style_catalog

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

#: 自动推算的片长上限（秒）：再长这台机器渲不完
MAX_AUTO_DURATION = 90.0

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
#: 同一时刻只允许一个渲染任务跑：Chrome / ffmpeg / npm 并发会互相打死
_RENDER_SLOT = threading.Semaphore(1)


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


#: 连续几次几乎相同的需求 = 卡住了（不问用户不换思路就会一直重复同样的画面）
STUCK_REPEAT_LIMIT = 3


def _prompt_fingerprint(prompt: str) -> str:
    """需求指纹：去空白后取 sha1 前 16 位（空白/标点差异不算新需求）。"""
    import hashlib

    text = re.sub(r"\s+", "", str(prompt or ""))[:400]
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _repeat_count(conversation_id: str, fingerprint: str) -> int:
    """同一个对话里、同一个需求指纹之前起过几次（用于“卡住”判定）。"""
    if not conversation_id:
        return 0
    try:
        recent = list_jobs(limit=10, conversation_id=conversation_id, prune=False)
    except Exception:  # noqa: BLE001 - 列表失败不能阻止起任务
        return 0
    return sum(1 for job in recent if job.get("fingerprint") == fingerprint)


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


def _kill_tree(pid: int) -> None:
    """杀掉整棵进程树（Windows 上 Chrome 会留一堆子进程，不杀干净下次更慢）。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=60)
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        logger.warning("清理渲染进程失败（pid=%s）", pid)


def _render_timeout(duration: float, fps: int) -> int:
    """按帧数估算超时：这台机器实测约 1.1s/帧，留 1.2 倍余量。"""
    frames = max(1, int(float(duration or 10) * fps))
    estimated = frames * 1.2 + 120
    return int(min(7200, max(600, estimated)))


def _kill_leftover_chrome() -> None:
    """清掉上次残留的 headless chrome（占内存，还会拖慢下一次）。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/IM", "chrome-headless-shell.exe", "/F", "/T"],
                capture_output=True, timeout=60,
            )
        else:
            subprocess.run(["pkill", "-f", "chrome-headless-shell"],
                           capture_output=True, timeout=60)
    except Exception:  # noqa: BLE001
        pass


def _render_once(job_id: str, project: Path, cmd: list[str], timeout: int) -> tuple[int, str]:
    """跑一次渲染，边跑边把"第几帧"写回任务状态（用户能看见进度）。"""
    import re as _re

    process = subprocess.Popen(
        cmd,
        cwd=str(project),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_job_env(project),
    )

    expired = threading.Event()

    def watchdog() -> None:
        if process.poll() is None:
            expired.set()
            _kill_tree(process.pid)

    guard = threading.Timer(timeout, watchdog)
    guard.daemon = True
    guard.start()

    lines: list[str] = []
    truncated = False
    try:
        for line in process.stdout:  # type: ignore[union-attr]
            lines.append(line.rstrip("\n"))
            if len(lines) > 400:
                del lines[:200]
            match = _re.search(r"frame\s+(\d+)\s*/\s*(\d+)", line)
            if match:
                done, total = int(match.group(1)), int(match.group(2))
                # 注意用 STAGE_RENDER：写成 SCAFFOLD 会让界面从"逐帧渲染"退回去
                _touch(job_id, STAGE_RENDER, progress=round(done / max(1, total), 3),
                       note=f"渲染 {done}/{total} 帧")
        process.wait(timeout=60)
    except Exception:  # noqa: BLE001
        truncated = True
    finally:
        guard.cancel()

    if expired.is_set():
        raise RuntimeError(
            f"渲染超时（{timeout}s）：这台机器一帧约 1.1 秒，"
            f"按当前片长算渲不完。把时长改短，或减少歌词行数。"
        )
    return process.returncode or 0, "\n".join(lines)


def _real_render(job_id: str, project: Path) -> str:
    """真渲染：调技能包里的 render.mjs。失败重试一次（headless 偶尔会崩）。"""
    node = _which("node")
    if not node:
        raise RuntimeError("没有 node，无法渲染")
    chrome = _find_chrome()

    job = job_status(job_id) or {}
    duration = float(job.get("duration") or 10)
    fast = _normalize_quality(job.get("quality")) == FAST_QUALITY
    # 长片子降帧率：帧数减半，渲染时间也减半，观感差别不大；快模式一律 12fps
    fps = 12 if (fast or duration > 30) else 24
    timeout = _render_timeout(duration, fps)

    size = ["--w=1280", "--h=720"] if fast else []
    cmd = [node, "render.mjs", "--clip", "--out=out/video.mp4", f"--fps={fps}", *size]
    song = str(job.get("audio") or "")
    if song and Path(song).is_file():
        # 短片走 --clip（不经过 _encode_frames），配乐得直接交给 render.mjs 混
        cmd.append(f"--audio={song}")
    if chrome:
        cmd.append(f"--chrome={chrome}")

    # 帧数多就走并行渲染（串行太慢：这块机器 1.1 秒/帧）
    frames = int(duration * fps)
    if frames > 240:
        attempts = 2
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                return _render_parallel(job_id, project, duration, fps,
                                        "", fast=fast)
            except RuntimeError as exc:
                last_error = str(exc)
                if attempt < attempts:
                    logger.warning("并行渲染失败，重试：%s", last_error[:200])
                    _kill_leftover_chrome()
                    _touch(job_id, STAGE_SCAFFOLD, note="重试渲染（上次失败）")
                    time.sleep(3)
                    continue
                raise
        raise RuntimeError(last_error)

    target = project / "out" / "video.mp4"
    attempts = 2
    last_error = ""
    for attempt in range(1, attempts + 1):
        logger.info("开始渲染（第 %d/%d 次）：时长 %.1fs，%dfps，超时 %ds",
                    attempt, attempts, duration, fps, timeout)
        try:
            code, output = _render_once(job_id, project, cmd, timeout)
        except RuntimeError as exc:
            last_error = str(exc)
            if attempt < attempts:
                _kill_leftover_chrome()
                _touch(job_id, STAGE_SCAFFOLD, note="重试渲染（上次中断）")
                time.sleep(3)
                continue
            raise
        if code == 0 and target.exists():
            return str(target)

        last_error = f"render.mjs 退出码 {code}：{(output or '')[-500:]}"
        try:
            (project / "out" / "render.log").write_text(output or "", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        if attempt < attempts:
            logger.warning("渲染失败，重试一次：%s", last_error[:200])
            _kill_leftover_chrome()
            _touch(job_id, STAGE_SCAFFOLD, note="重试渲染（上次失败）")
            time.sleep(3)

    raise RuntimeError(last_error or "渲染失败")


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
    slot_held = False
    try:
        caps = capabilities()

        # 排队：真渲染才需要（Chrome/ffmpeg 并发会互相打死）；
        # 假渲染器是毫秒级的，排队只会拖慢测试
        if caps["renderer"] != "fake":
            if not _RENDER_SLOT.acquire(blocking=False):
                _touch(job_id, STAGE_QUEUED, note="排队中（前面还有任务在渲染）")
                _RENDER_SLOT.acquire()
                slot_held = True
                _touch(job_id, STAGE_SCAFFOLD, note="排到了，开始渲染")
            else:
                slot_held = True
                _touch(job_id, STAGE_SCAFFOLD)
        else:
            _touch(job_id, STAGE_SCAFFOLD)
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
                f"- 风格：{style_catalog.style_label(str(job.get('style') or ''))}（{job.get('style') or 'watercolor'}）\n"
                f"- 歌词：{len(lyric_lines)} 行\n\n"
                "| 时间 | 歌词 | 画面事件 |\n|------|------|----------|\n"
                + table + "\n",
                encoding="utf-8",
            )
        else:
            storyboard.write_text(
                "# 分镜（草稿）\n\n"
                f"- 需求：{job.get('prompt') or ''}\n"
                f"- 风格：{style_catalog.style_label(str(job.get('style') or ''))}（{job.get('style') or 'watercolor'}）\n"
                "- 说明：由 animation 技能自动生成，逐镜细节需要人工补充。\n",
                encoding="utf-8",
            )

        # 自动开渲：分镜写下来只作记录，不再停下来等用户点确认。

        # 歌词 MV：把歌词真正填进模板（不然渲染出来只是模板默认画面）
        lyric_info = _prepare_lyric_project(project, job)

        # 非歌词任务也认时长和风格：模板默认 11s，不写回去「前30s / 3秒」就永远被忽略
        style = str(job.get("style") or "watercolor")
        if not lyric_info.get("lines"):
            wanted = float(job.get("duration") or 0)
            if wanted > 0:
                _write_project_duration(project, wanted)
            # 像素风以前只有歌词 MV 会套配置，非歌词任务不写等于没设
            _apply_style_config(project, style)

        # 逐句场景：不再永远用 demo 的"小人+月亮"
        try:
            from skills.work.animation import scene_gen

            timeline = _lyric_timeline(job)[0]
            if timeline:
                info = scene_gen.install(project, timeline,
                                         style=style,
                                         prompt=str(job.get("prompt") or ""))
                logger.info("逐句场景：%d 个（替换 demo=%s）",
                            info.get("scenes", 0), info.get("replaced_demo"))
                _touch(job_id, STAGE_RENDER, scenes=info.get("scenes", 0),
                       note=f"{info.get('scenes', 0)} 个镜头")
        except Exception:  # noqa: BLE001 - 场景生成失败就退回 demo 画面
            logger.exception("逐句场景生成失败（用 demo 兜底）")

        # 非歌词任务：以前模板 demo 是水彩「小人+月亮」，用户给的约束完全没用上。
        # 现在有画面关键词时按 prompt 拆镜头（最多 4 段），水彩/像素/其他风格都生成 generated.js；
        # 纯水彩且一个关键词都没有时保留模板 demo（demo 的笔刷质量比程序化构图好）。
        if not lyric_info.get("lines"):
            try:
                from skills.work.animation import scene_gen

                prompt_text = str(job.get("prompt") or "")
                theme = scene_gen.scene_keywords(prompt_text)
                if not theme and style_catalog.resolve_style(style) == "watercolor":
                    logger.info("非歌词水彩且无关键词：保留模板 demo")
                    _touch(job_id, STAGE_RENDER, note="无关键词，用模板画面")
                else:
                    span = float(job.get("duration") or 0) or _project_duration(project)
                    shots = _prompt_shots(prompt_text, span)
                    info = scene_gen.install(
                        project, shots, style=style, prompt=prompt_text,
                    )
                    logger.info("非歌词镜头：%d 个（%ss，替换 demo=%s）",
                                info.get("scenes", 0), span, info.get("replaced_demo"))
                    _touch(job_id, STAGE_RENDER, scenes=info.get("scenes", 0),
                           note=f"{info.get('scenes', 0)} 个镜头")
            except Exception:  # noqa: BLE001 - 生成失败就退回 demo 画面
                logger.exception("非歌词镜头生成失败（用 demo 兜底）")
        if lyric_info["lines"]:
            # 时长要写回任务：渲染阶段靠它算超时和帧率
            _touch(job_id, STAGE_RENDER, duration=lyric_info["duration"],
                   note=f"填入 {lyric_info['lines']} 行歌词（{lyric_info['duration']}s）")
            job["duration"] = lyric_info["duration"]
            job["clip_start"] = float(lyric_info.get("offset") or 0.0)

        _touch(job_id, STAGE_RENDER)
        if caps["renderer"] == "fake":
            video = _fake_render(job_id, project)
        else:
            _ensure_node_modules(job_id, project)
            _touch(job_id, STAGE_RENDER, note="逐帧渲染")
            video = _real_render(job_id, project)
            # 配乐统一在渲染之后混一次：短片 --clip 自带音轨，_mux_audio 会覆盖成单条
            song = str(job.get("audio") or "")
            if song and Path(song).is_file() and Path(video).exists():
                video = _mux_audio(
                    video, song,
                    start=float(job.get("clip_start") or 0.0),
                    duration=float(job.get("duration") or 0.0),
                )

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
        _touch(job_id, STAGE_FAILED, error=str(exc)[:1800])
    finally:
        if slot_held:
            try:
                _RENDER_SLOT.release()
            except Exception:  # noqa: BLE001
                pass



#: 画质档位：standard = 1080p；fast = 720p + 12fps
STANDARD_QUALITY = "standard"
FAST_QUALITY = "fast"


def _normalize_quality(value) -> str:
    """把用户/模型给的各种写法归一到两档；认不出就当 standard。"""
    raw = str(value or "").strip().lower()
    if raw in ("fast", "720p", "720", "quick", "快速"):
        return FAST_QUALITY
    return STANDARD_QUALITY


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
    quality: str = "standard",
    section: str = "",
    conversation_id: str = "",
) -> dict:
    """起一个渲染任务，立刻返回（渲染在后台线程）。"""
    job_id = f"anim_{uuid.uuid4().hex[:12]}"
    folder = jobs_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    fingerprint = _prompt_fingerprint(prompt)
    previous = _repeat_count(conversation_id, fingerprint)
    stuck = previous >= STUCK_REPEAT_LIMIT - 1
    job = {
        "id": job_id,
        "prompt": str(prompt or "")[:2000],
        "style": style_catalog.resolve_style(str(style or "")),
        #: standard = 1920×1080；fast = 1280×720 + 12fps（渲染约 3 倍快，画面略软）
        "quality": _normalize_quality(quality),
        "user_id": str(user_id or ""),
        #: 属于哪个对话：任务栏按对话隔离，不再全局混在一起
        "conversation_id": str(conversation_id or ""),
        "stage": STAGE_QUEUED,
        #: 不再等用户手动确认：起任务就直接渲染（confirm 接口保留兼容）
        "requires_confirmation": False,
        "dir": str(folder),
        "video": "",
        "error": "",
        "lyrics": str(lyrics or "")[:8000],
        "lrc": str(lrc or "")[:20000],
        "lyrics_file": str(lyrics_file or ""),
        "audio": str(audio or ""),
        "section": str(section or ""),
        #: 需求指纹 + 这是第几次：连续相似时提示 agent 换思路，而不是重做一遍
        "fingerprint": fingerprint,
        "repeat_count": previous + 1,
        "stuck": stuck,
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


#: 这些状态说明还有后台线程在干活
RUNNING_STAGES = (STAGE_QUEUED, STAGE_SCAFFOLD, STAGE_STORYBOARD,
                  STAGE_RENDER, STAGE_ENCODE)


def _annotate(job: dict, *, live: bool) -> dict:
    """补两个现场字段：live（本进程真有线程吗）/ stale（是不是重启留下的）。

    注意：live 必须由调用方在**持锁时**取好再传进来。
    这里不能再抢 _LOCK——threading.Lock 不可重入，会直接把整个进程卡死。
    """
    stage = str(job.get("stage") or "")
    running = stage in RUNNING_STAGES
    job["live"] = live
    job["stale"] = running and not live
    return job


def job_status(job_id: str) -> Optional[dict]:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            return _annotate(dict(job), live=True)
    # 进程重启后从磁盘读：内存里没有 -> live=False（僵尸或已完成）
    path = jobs_dir() / str(job_id) / "status.json"
    if not path.exists():
        return None
    try:
        return _annotate(json.loads(path.read_text(encoding="utf-8")), live=False)
    except Exception:  # noqa: BLE001
        return None


def list_jobs(limit: int = 20, conversation_id: Optional[str] = None,
              *, prune: bool = True) -> list[dict]:
    """任务列表；``conversation_id`` 给了就只看这个对话的。

    ``prune=True`` 时顺手清掉永远不会再动的垃圾，免得任务栏每次打开都翻出陈年旧账：

    * 进程重启后停在「渲染中」、本进程没有线程的僵尸（stale）
    * 重启后还停在「等确认分镜」的（没人会确认了）
    * 失败超过 6 小时的

    只删该删的，正在跑的和刚失败（留给用户看原因）的一律保留。
    """
    now = time.time()
    items: list[dict] = []
    with _LOCK:
        live_ids = set(_JOBS)
        live_copies = {job_id: dict(job) for job_id, job in _JOBS.items()}

    for folder in jobs_dir().glob("anim_*"):
        if not folder.is_dir():
            continue
        job_id = folder.name
        if job_id in live_copies:
            job = live_copies[job_id]
            age = now - now  # 内存里的任务就是「活的」，不按磁盘时间判僵尸
        else:
            path = folder / "status.json"
            if not path.exists():
                continue
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                age = now - path.stat().st_mtime
            except Exception:  # noqa: BLE001
                continue
        stage = str(job.get("stage") or "")
        stale = (stage in RUNNING_STAGES and job_id not in live_ids and age > 1800)
        abandoned = (stage == STAGE_AWAITING and job_id not in live_ids)
        old_failed = (stage == STAGE_FAILED and age > 6 * 3600)
        if prune and (stale or abandoned or old_failed):
            logger.info("任务栏自动清理：%s（%s）", job_id, stage)
            delete_job(job_id)
            continue
        if conversation_id is not None and str(job.get("conversation_id") or "") != conversation_id:
            continue
        items.append(job)

    # 内存里刚起、还没落盘的任务（正常都会写，这里兜底）
    for job_id, job in live_copies.items():
        if any(item.get("id") == job_id for item in items):
            continue
        if conversation_id is not None and str(job.get("conversation_id") or "") != conversation_id:
            continue
        items.append(dict(job))

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

    # 风格：参数 > 原话里的词（水墨 / 赛博朋克 / 胶片 / 黑白 / 动漫 / 小清新 / 日落…）
    style = style_catalog.resolve_style(
        str(args.get("style") or "") + " " + prompt
    )

    # 歌词可以从参数给，也可以直接写在 prompt 里（"…配这首歌" 后面换行开始）
    lyrics = str(args.get("lyrics") or "").strip()
    if not lyrics and "\n" in prompt:
        head, _, rest = prompt.partition("\n")
        if _needs_storyboard(head) and rest.strip():
            lyrics = rest.strip()
            prompt = head.strip()

    # 上传的歌词文件优先（LRC 有真实时间轴）
    lyrics_file = str(args.get("lyrics_file") or "").strip()

    lrc = ""
    if lyrics_file and Path(lyrics_file).is_file():
        try:
            lrc = Path(lyrics_file).read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            lrc = ""

    # 只给了歌：先看歌里有没有内嵌歌词（ID3 USLT / MP4 ©lyr / FLAC，或同名 .lrc）
    audio_path = str(args.get("audio") or "").strip()
    lyrics_source = "歌词文件" if lrc.strip() else ""
    if not lrc.strip() and not lyrics and audio_path:
        embedded, source = read_audio_lyrics(audio_path)
        if embedded.strip():
            lrc, lyrics_source = embedded, source

    section = str(args.get("section") or "").strip() or parse_section(prompt)
    conversation_id = str(args.get("conversation_id") or "").strip()

    # 时长：显式参数 > 从原话里抠（"前30s的MV"）
    duration = float(args.get("duration") or 0) or _parse_duration(prompt)

    job = create_job(
        prompt, style=style, user_id=str(args.get("user_id") or ""),
        lyrics=lyrics,
        lrc=lrc,
        lyrics_file=lyrics_file,
        audio=str(args.get("audio") or ""),
        duration=duration,
        quality=str(args.get("quality") or "standard"),
        section=section,
        conversation_id=conversation_id,
    )
    label = _STAGE_LABELS.get(job["stage"], job["stage"])
    if job["stage"] == STAGE_FAILED:
        return {"ok": False, "error": job.get("error") or "渲染失败", "job_id": job["id"]}
    text = f"动画已经开做了（任务 {job['id']}，当前：{label}），好了我把成片发你。"
    if job.get("stuck"):
        text += (f"（同一个需求已经起过 {int(job.get('repeat_count') or 0)} 次，画面很可能不会变："
                 "换个风格 / 换组镜头 / 拆成小段再做，或者明说这次具体要改哪一处，别重复做同款。）")
    hints: list[str] = []
    if audio_path and not lyrics and not lrc.strip():
        hints.append("这首歌里没找到内嵌歌词，也没传 .lrc；要逐句歌词就把歌词贴我或传 .lrc")
    elif lyrics_source:
        hints.append(f"歌词来源：{lyrics_source}")
    if section in ("chorus", "verse"):
        hints.append("只做" + ("副歌" if section == "chorus" else "主歌"))
    if hints:
        text += "（" + "；".join(hints) + "）"
    return {"ok": True, "text": text, "job_id": job["id"], "stage": job["stage"],
            "style": style, "section": section,
            "lyrics_source": lyrics_source}


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
                      "description": "画面风格：watercolor 水彩（默认）/ pixel 像素 / "
                                     "ink 水墨 / neon 赛博朋克 / film 胶片 / "
                                     "noir 黑白 / pastel 小清新 / anime 动漫 / sunset 日落"},
            "lyrics": {"type": "string",
                       "description": "歌词全文（多行）。歌词 MV 用，会逐句打在画面上"},
            "lyrics_file": {"type": "string",
                            "description": "歌词文件路径（.lrc 有真实时间轴，.txt 均分）"},
            "audio": {"type": "string",
                      "description": "配乐文件路径（可选），渲染完会混进成片"},
            "duration": {"type": "number",
                         "description": "时长秒（可选），不给就按音频时长/歌词行数估"},
            "section": {"type": "string",
                        "description": "只做一段：full 整首（默认）/ chorus 副歌（重复最多的那段）/ "
                                       "verse 主歌（第一段）"},
            "quality": {"type": "string",
                        "description": "standard（1080p，默认）/ fast（720p + 12fps，约 3 倍快）"},
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


def _remove_folder(folder: Path) -> tuple[bool, str]:
    """可靠删目录：Windows 上 node_modules / chrome 可能短暂占用文件。

    以前直接 ``shutil.rmtree(ignore_errors=True)``，删不掉也当成功——
    用户重启后任务又冒出来，就是这里骗了自己。现在重试 + 改名兜底 + 回读验证。
    """
    if not folder.exists():
        return True, ""
    last = ""
    for attempt in range(3):
        try:
            shutil.rmtree(folder)
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:200]
        if not folder.exists():
            return True, ""
        time.sleep(0.4 * (attempt + 1))
    # 改名挪开再删：即使进程还开着句柄，至少原目录名不再出现在任务列表
    trash = folder.with_name(f".trash_{folder.name}_{int(time.time())}")
    try:
        folder.rename(trash)
        shutil.rmtree(trash, ignore_errors=True)
        if not folder.exists():
            return True, ""
    except Exception as exc:  # noqa: BLE001
        last = str(exc)[:200]
    return False, last or "目录删不掉（文件可能还被渲染进程占用）"


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
        stamp_path = jobs_dir() / job_id / "status.json"
        stamp = stamp_path.stat().st_mtime if stamp_path.exists() else 0.0
        recent = (time.time() - stamp) < 10 * 60
    if live and recent and job.get("stage") not in ("done", "failed"):
        return {"ok": False, "error": "任务还在跑，等它结束再删"}

    removed, reason = _remove_folder(jobs_dir() / job_id)
    if not removed:
        return {"ok": False, "error": f"删除失败：{reason}（稍后再试）"}

    with _LOCK:
        _JOBS.pop(job_id, None)
    logger.info("动画任务已删除：%s", job_id)
    return {"ok": True, "job_id": job_id}


def update_prompt(
    job_id: str,
    prompt: Optional[str] = None,
    style: Optional[str] = None,
) -> dict:
    """改任务的需求描述/风格（不重渲）。配合 ``rerender`` 用来"按新需求重做一版"。

    只改 status.json 里的 prompt/style；真正的重渲染由调用方接着调 ``rerender``。
    拒绝改正在跑的任务，避免渲染线程读到一半的新标签。
    """
    job = job_status(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在"}
    if job.get("stage") in RUNNING_STAGES and str(job.get("id")) in _JOBS:
        stamp = jobs_dir() / str(job_id) / "status.json"
        age = time.time() - (stamp.stat().st_mtime if stamp.exists() else 0.0)
        if age < 10 * 60:
            return {"ok": False, "error": "任务还在跑，等它结束再改需求"}
    with _LOCK:
        live = dict(_JOBS.get(str(job_id)) or job)
        if prompt:
            live["prompt"] = str(prompt).strip()
        if style:
            live["style"] = str(style).strip()
        live["note"] = "已改需求，等待重渲"
        live["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _JOBS[str(job_id)] = live
        _write_status(live)
    return {"ok": True, "job": job_status(str(job_id))}


def rerender(job_id: str, quality: str = "fast") -> dict:
    """换个画质重渲（项目、歌词、风格都不变）。

    任务栏的「720p / 1080p」就是调它：已经渲完或失败的任务可以重来一遍，
    真在跑的（10 分钟内还在动）拒绝，避免两个渲染抢 Chrome。
    """
    job = job_status(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在"}
    wanted = _normalize_quality(quality)
    with _LOCK:
        live = _JOBS.get(job_id)
        if live is not None:
            stamp_path = jobs_dir() / job_id / "status.json"
            stamp = stamp_path.stat().st_mtime if stamp_path.exists() else 0.0
            moving = live.get("stage") in RUNNING_STAGES and (time.time() - stamp) < 10 * 60
            if moving:
                return {"ok": False, "error": "任务还在跑，等它结束再换画质"}
            live = dict(live)
        else:
            live = dict(job)
        live.update({
            "quality": wanted,
            "stage": STAGE_QUEUED,
            "error": "",
            "video": "",
            "requires_confirmation": False,
            "note": f"换画质重渲（{wanted}）",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        _JOBS[job_id] = live
        _write_status(live)
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return {"ok": True, "job": job_status(job_id)}


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


# ---------------------------------------------------------------------------
# 歌词来源：用户贴的 / 上传的 .lrc / 音频里内嵌的歌词
# ---------------------------------------------------------------------------

#: 音频标签里可能放歌词的键（规范化后比较）
_LYRIC_TAG_KEYS = frozenset({"lyrics", "unsyncedlyrics", "syncedlyrics", "lyric"})


def _decode_tag(raw: bytes, encoding: int = 3) -> str:
    """ID3 文本解码（0 latin1 / 1 utf-16(BOM) / 2 utf-16be / 3 utf-8）。"""
    if not raw:
        return ""
    if encoding == 0:
        return raw.decode("latin-1", "replace")
    if encoding == 1:
        return raw.decode("utf-16", "replace")
    if encoding == 2:
        return raw.decode("utf-16-be", "replace")
    return raw.decode("utf-8", "replace")


def _norm_tag(text: str) -> str:
    return re.sub(r"[^a-z]", "", str(text or "").lower())


def _id3_lyrics(path: Path) -> str:
    """MP3：从 ID3v2 的 USLT / TXXX:LYRICS 取歌词（多数音乐软件下载时会写）。"""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) < 10 or data[:3] != b"ID3":
        return ""
    major = int(data[3])
    flags = int(data[5])
    size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) \
        | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
    body = data[10:10 + size]
    if flags & 0x40 and len(body) >= 4:  # 扩展头
        ext = int.from_bytes(body[:4], "big")
        body = body[4 + ext:]
    if major < 3:  # v2.2 的三字符帧 id 太少见了
        return ""

    def frame_size(raw: bytes) -> int:
        if major >= 4:
            return ((raw[0] & 0x7F) << 21) | ((raw[1] & 0x7F) << 14) \
                | ((raw[2] & 0x7F) << 7) | (raw[3] & 0x7F)
        return int.from_bytes(raw, "big")

    pos = 0
    while pos + 10 <= len(body):
        frame_id = body[pos:pos + 4]
        length = frame_size(body[pos + 4:pos + 8])
        pos += 10
        payload = body[pos:pos + length]
        pos += length
        if length <= 0 or not frame_id[:1].isalpha():
            continue
        if frame_id == b"USLT" and len(payload) > 4:
            # USLT 正文前还有一段「内容描述符」，用 NUL 跟正文隔开
            decoded = _decode_tag(payload[4:], payload[0])
            text = decoded.split("\x00", 1)[1] if "\x00" in decoded else decoded
            text = text.strip().lstrip("\ufeff")
            if text:
                return text
        elif frame_id == b"TXXX" and payload:
            decoded = _decode_tag(payload[1:], payload[0])
            desc, _, value = decoded.partition("\x00")
            if _norm_tag(desc) in _LYRIC_TAG_KEYS and value.strip():
                return value.strip()
    return ""


def _mp4_lyrics(path: Path) -> str:
    """M4A/MP4：读 ©lyr 原子（Apple / QQ 音乐常见）。"""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    containers = {b"moov", b"udta", b"trak", b"mdia", b"minf", b"stbl", b"ilst"}

    def walk(offset: int, end: int) -> str:
        while offset + 8 <= end:
            size = int.from_bytes(data[offset:offset + 4], "big")
            kind = data[offset + 4:offset + 8]
            header = 8
            if size == 1 and offset + 16 <= end:
                size = int.from_bytes(data[offset + 8:offset + 16], "big")
                header = 16
            elif size == 0:
                size = end - offset
            if size < header or offset + size > end:
                return ""
            start = offset + header
            if kind in containers:
                found = walk(start, offset + size)
            elif kind == b"meta":
                found = walk(start + 4, offset + size)
            elif kind in (b"\xa9lyr", b"\xa9LYR"):
                found = walk(start, offset + size)
            elif kind == b"data":
                raw = data[start + 8:offset + size]
                return raw.rstrip(b"\x00").decode("utf-8", "replace")
            else:
                found = ""
            if found:
                return found
            offset += size
        return ""

    return walk(0, len(data)).strip()


def _flac_lyrics(path: Path) -> str:
    """FLAC：Vorbis comment 里的 LYRICS / UNSYNCEDLYRICS。"""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if data[:4] != b"fLaC":
        return ""
    pos = 4
    while pos + 4 <= len(data):
        block_type = data[pos] & 0x7F
        last = bool(data[pos] & 0x80)
        length = int.from_bytes(data[pos + 1:pos + 4], "big")
        block = data[pos + 4:pos + 4 + length]
        pos += 4 + length
        if block_type == 4 and len(block) > 8:
            vendor = int.from_bytes(block[:4], "little")
            cursor = 4 + vendor
            if cursor + 4 > len(block):
                return ""
            count = int.from_bytes(block[cursor:cursor + 4], "little")
            cursor += 4
            for _ in range(min(count, 512)):
                if cursor + 4 > len(block):
                    break
                size = int.from_bytes(block[cursor:cursor + 4], "little")
                cursor += 4
                raw = block[cursor:cursor + size]
                cursor += size
                key, _, value = raw.decode("utf-8", "replace").partition("=")
                if _norm_tag(key) in _LYRIC_TAG_KEYS and value.strip():
                    return value.strip()
        if last:
            break
    return ""


def _sidecar_lyrics(path: Path) -> str:
    """音频旁边的同名 .lrc / .txt（用户经常把歌词和歌一起放）。"""
    for suffix in (".lrc", ".txt"):
        probe = path.with_suffix(suffix)
        if not probe.is_file():
            continue
        try:
            text = probe.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.strip():
            return text.strip()
    return ""


def read_audio_lyrics(audio: str) -> tuple[str, str]:
    """从音频里找歌词：返回 (歌词文本, 来源)；找不到返回 ("", "")。"""
    path = Path(str(audio or ""))
    if not path.is_file():
        return "", ""
    suffix = path.suffix.lower()
    readers = {
        ".mp3": [(_id3_lyrics, "ID3 内嵌歌词")],
        ".m4a": [(_mp4_lyrics, "MP4 ©lyr 内嵌歌词")],
        ".mp4": [(_mp4_lyrics, "MP4 ©lyr 内嵌歌词")],
        ".aac": [(_mp4_lyrics, "MP4 ©lyr 内嵌歌词")],
        ".flac": [(_flac_lyrics, "FLAC 内嵌歌词")],
    }.get(suffix, [
        (_id3_lyrics, "ID3 内嵌歌词"),
        (_mp4_lyrics, "MP4 内嵌歌词"),
        (_flac_lyrics, "FLAC 内嵌歌词"),
    ])
    for reader, label in readers:
        try:
            text = reader(path)
        except Exception:  # noqa: BLE001 - 坏标签不能挡住整个任务
            text = ""
        if str(text or "").strip():
            return str(text).strip(), label
    text = _sidecar_lyrics(path)
    if text:
        return text, "同名 .lrc/.txt"
    return "", ""


def _job_lyrics_text(job: dict) -> tuple[str, str]:
    """任务里的歌词：显式 LRC > 上传的歌词文件 > 音频内嵌 / 同名 .lrc。"""
    lrc = str(job.get("lrc") or "").strip()
    if lrc:
        return lrc, "歌词"
    file = str(job.get("lyrics_file") or "").strip()
    if file and Path(file).is_file():
        try:
            text = Path(file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if text.strip():
            return text.strip(), "歌词文件"
    return read_audio_lyrics(str(job.get("audio") or ""))


#: 用户可能说的段落词 → 段落键
_SECTION_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("chorus", ("副歌", "高潮", "最抓耳", "爆点", "chorus", "hook", "记忆点")),
    ("verse", ("主歌", "verse", "第一段", "开头段", "前段")),
)


def parse_section(text: str) -> str:
    """从原话里认出要哪一段：chorus（副歌）/ verse（主歌）；认不出返回空。"""
    body = str(text or "")
    for key, words in _SECTION_WORDS:
        if any(word in body for word in words):
            return key
    return ""


def _norm_line(text: str) -> str:
    return re.sub(r"[\s\u3000\W_]+", "", str(text or "")).lower()


def _chorus_runs(timed: list[dict]) -> list[list[dict]]:
    """把「反复出现的那几句」按连续成段切开（每段是一个候选副歌）。"""
    counts = Counter(_norm_line(item["text"]) for item in timed if _norm_line(item["text"]))
    repeated = {text for text, count in counts.items() if count >= 2 and len(text) >= 2}
    if not repeated:
        return []
    runs: list[list[dict]] = []
    current: list[dict] = []
    for item in timed:
        if _norm_line(item["text"]) in repeated:
            current.append(item)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _chorus_span(timed: list[dict], end_all: float) -> Optional[tuple[float, float]]:
    """副歌：最长的那段重复行（并列时取靠后的）。"""
    runs = _chorus_runs(timed)
    if not runs:
        return None
    best = max(runs, key=lambda run: (len(run), float(run[-1]["start"])))
    index = timed.index(best[-1])
    start = float(best[0]["start"])
    if index + 1 < len(timed):
        end = float(timed[index + 1]["start"])
    else:
        end = min(float(best[-1]["start"]) + 4.0, end_all)
    return (start, max(start + 0.8, end))


def _pick_lines(lines: list[str], request: str) -> tuple[list[str], str]:
    """没有时间戳的纯文本歌词：按重复行猜主歌/副歌。"""
    if request not in ("chorus", "verse"):
        return lines, "full"
    counts = Counter(_norm_line(line) for line in lines)
    repeated = {text for text, count in counts.items() if count >= 2 and len(text) >= 2}
    if request == "chorus":
        picked = [line for line in lines if _norm_line(line) in repeated]
        if picked:
            return picked, "chorus"
        return lines[-max(2, len(lines) // 3):], "chorus"
    split = 0
    for index, line in enumerate(lines):
        if _norm_line(line) in repeated:
            split = index
            break
    if split >= 2:
        return lines[:split], "verse"
    return lines[:max(2, len(lines) // 2)], "verse"



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


def _write_project_duration(project: Path, duration: float) -> None:
    """把时长写进 config.js 的 PROJECT.duration（保留 pixel 等其它字段）。"""
    config = project / "src" / "config.js"
    if not config.exists():
        return
    source = config.read_text(encoding="utf-8")
    start = source.find("const PROJECT = {")
    end = source.find("};", start) if start >= 0 else -1
    if start < 0 or end < 0:
        return
    block = re.sub(r"duration:\s*[\d.]+", f"duration: {float(duration):.2f}",
                   source[start:end], count=1)
    config.write_text(source[:start] + block + source[end:], encoding="utf-8")


def _project_duration(project: Path) -> float:
    """读 config.js 里 PROJECT.duration（读不出来就用模板默认的 11s）。"""
    try:
        source = (project / "src" / "config.js").read_text(encoding="utf-8")
    except OSError:
        return 11.0
    match = re.search(r"duration:\s*([\d.]+)", source)
    return float(match.group(1)) if match else 11.0


#: 像素风的 PROJECT 字段（与模板 config.js 注释里的示例一致）
_PIXEL_FIELDS = "  pixel: true, pixelGrid: [320, 180], lyricUI: 'rpgBox',"


def _apply_style_config(project: Path, style: str) -> None:
    """把风格写进 config.js 的 PROJECT。

    像素风补 pixel / pixelGrid / lyricUI；其它非默认风格写一行 ``style: '<键>'``，
    由 scene_gen 注入的 STYLE_PACK 在浏览器里调色。只动未注释字段，
    模板里 `//   pixel: true,` 这类示例行原样保留。
    """
    config = project / "src" / "config.js"
    if not config.exists():
        return
    key = style_catalog.resolve_style(str(style or ""))
    source = config.read_text(encoding="utf-8")
    start = source.find("const PROJECT = {")
    end = source.find("};", start) if start >= 0 else -1
    if start < 0 or end < 0:
        return
    block = source[start:end]
    # 先摘掉已有字段（重复调用也不会插两遍）
    stripped = re.sub(
        r"(?m)^[ \t]*(?:pixel|pixelGrid|lyricUI|style)\s*:[^\n]*\n", "", block
    )
    if key == "pixel":
        stripped = stripped.replace(
            "const PROJECT = {", "const PROJECT = {\n" + _PIXEL_FIELDS, 1
        )
    elif key != "watercolor":
        line = "  style: '" + key + "',"
        stripped = stripped.replace(
            "const PROJECT = {", "const PROJECT = {\n" + line, 1
        )
    if stripped != block:
        config.write_text(source[:start] + stripped + source[end:], encoding="utf-8")


def _prepare_lyric_project(project: Path, job: dict) -> dict:
    """把歌词按**真实时间轴**写进 src/karaoke.js 的 LY，总时长/风格写进 config.js。

    只做副歌/主歌时时间轴会平移到 0，offset（音频起点）回报给调用方去截音频。
    """
    plan = _lyric_plan(job)
    timeline, duration = plan["timeline"], plan["duration"]
    if not timeline:
        return {"lines": 0, "duration": 0, "audio": "", "lrc": False,
                "offset": 0.0, "section": plan.get("section") or "full",
                "source": plan.get("source") or ""}

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
        replacement = (f"const PROJECT = {{\n  duration: {duration:.2f}, "
                       f"bpm: 120, offset: 0,\n}};")
        start = source.find("const PROJECT = {")
        if start >= 0:
            end = source.find("};", start)
            if end > start:
                source = source[:start] + replacement + source[end + 2:]
                config.write_text(source, encoding="utf-8")
        _apply_style_config(project, str(job.get("style") or "watercolor"))

    audio = str(job.get("audio") or "").strip()
    return {
        "lines": len(timeline),
        "duration": duration,
        "audio": audio if audio and Path(audio).exists() else "",
        "lrc": bool(plan.get("source")),
        "offset": float(plan.get("offset") or 0.0),
        "section": plan.get("section") or "full",
        "source": plan.get("source") or "",
    }



def _mux_audio(video: str, audio: str, *, start: float = 0.0,
               duration: float = 0.0) -> str:
    """把配乐混进成片；失败就保留无声版（不掩盖画面成果）。

    start/duration 对齐「只做副歌/主歌」：从音频的 start 秒开始取 duration 秒。
    """
    ffmpeg = _which("ffmpeg")
    if not ffmpeg or not audio or not Path(audio).exists():
        return video
    target = str(Path(video).with_name("video_with_audio.mp4"))
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", video]
    if start > 0.01:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", audio, "-c:v", "copy", "-c:a", "aac", "-shortest"]
    if duration > 0.01:
        cmd += ["-t", f"{duration:.3f}"]
    cmd.append(target)
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if Path(target).exists() and Path(target).stat().st_size > 0:
            logger.info("已混入配乐：%s（起点 %.2fs）", target, start)
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


def _lyric_plan(job: dict) -> dict:
    """歌词 → 镜头时间轴，并按「副歌 / 主歌」截取。

    返回的 offset 是截取起点（秒）：混音时要按它对音频做 -ss，否则声画对不上。
    """
    text, source = _job_lyrics_text(job)
    audio = str(job.get("audio") or "")
    audio_path = Path(audio) if audio and Path(audio).exists() else None
    probe = float(job.get("duration") or 0)
    if not probe and audio_path is not None:
        probe = _probe_duration(audio_path)

    request = parse_section(str(job.get("section") or "")) \
        or parse_section(str(job.get("prompt") or ""))
    section = request or "full"

    entries = parse_lrc(text) if text else []
    timed = [item for item in entries if item["start"] is not None]
    if timed:
        start_all = float(timed[0]["start"])
        end_all = float(timed[-1]["start"]) + 3.0
        if probe:
            end_all = max(end_all, probe)
        span_start, span_end = 0.0, end_all  # full：保留原时间轴，前奏不剪
        if request:
            runs = _chorus_runs(timed)
            chorus = _chorus_span(timed, end_all)
            if request == "chorus":
                if chorus:
                    span_start, span_end = chorus
                else:
                    # 没识别出重复段：最后三分之一通常是副歌，给一段总比空手强
                    span_start = max(start_all, end_all * 0.62)
                    span_end = end_all
            elif request == "verse":
                if runs:
                    # 主歌 = 第一段副歌之前（不是「最长那段副歌」之前）
                    span_start, span_end = start_all, float(runs[0][0]["start"])
                else:
                    span_start = start_all
                    span_end = start_all + (end_all - start_all) * 0.5
        limit = float(job.get("duration") or 0)
        if limit > 0:
            span_end = min(span_end, span_start + limit)

        picked = [item for item in timed
                  if span_start <= float(item["start"]) < span_end]
        shifted: list[tuple[float, float, str]] = []
        for index, item in enumerate(picked):
            begin = float(item["start"]) - span_start
            if index + 1 < len(picked):
                finish = float(picked[index + 1]["start"]) - span_start
            else:
                finish = span_end - span_start
            shifted.append((round(begin, 3),
                            round(max(begin + 0.6, finish - 0.12), 3),
                            item["text"]))
        total = round(span_end - span_start, 3)
        return {"timeline": shifted, "duration": total,
                "offset": round(span_start, 3), "section": section,
                "source": source, "lines": len(shifted)}

    lines = [item["text"] for item in entries] or _lyric_lines(job)
    if not lines:
        return {"timeline": [], "duration": round(probe, 3), "offset": 0.0,
                "section": section, "source": source, "lines": 0}
    picked_lines, picked_section = _pick_lines(lines, request)
    per = (probe / len(picked_lines)) if probe else 3.0
    total = probe or max(6.0, per * len(picked_lines))
    if not float(job.get("duration") or 0) and total > MAX_AUTO_DURATION:
        per = max(1.2, MAX_AUTO_DURATION / max(1, len(picked_lines)))
        total = per * len(picked_lines)
    timeline = []
    for index, line in enumerate(picked_lines):
        begin = round(index * per, 3)
        finish = round(min(total, begin + per * 0.92), 3)
        timeline.append((begin, finish, line))
    return {"timeline": timeline, "duration": round(total, 3), "offset": 0.0,
            "section": picked_section if request else section,
            "source": source, "lines": len(timeline)}


def _lyric_timeline(job: dict) -> tuple[list[tuple[float, float, str]], float]:
    """兼容旧调用：返回 (时间轴, 总时长)。"""
    plan = _lyric_plan(job)
    return plan["timeline"], float(plan["duration"])

#: "30秒" / "1分30秒" / "2 分钟" / "前 30s"
_DURATION_PATTERNS = (
    # "30s的MV" 里 s 后面直接跟中文，所以不能用 \b
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:秒钟|秒|s(?![a-zA-Z0-9])|sec(?![a-zA-Z0-9]))"),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:分钟|分|min\b)"),
    re.compile(r"一分半"),
)


def _parse_duration(text: str) -> float:
    """从用户原话里抠时长（"前30s的MV" -> 30.0）。抠不出来返回 0。"""
    body = str(text or "")
    if "一分半" in body:
        return 90.0
    seconds = 0.0
    found = False
    for match in _DURATION_PATTERNS[0].finditer(body):
        seconds += float(match.group(1))
        found = True
    for match in _DURATION_PATTERNS[1].finditer(body):
        seconds += float(match.group(1)) * 60
        found = True
    return round(seconds, 2) if found else 0.0

def _prompt_shots(prompt: str, span: float, *, max_shots: int = 4) -> list[tuple[float, float, str]]:
    """把一句需求拆成几个镜头（按标点），平均分时间；拆不出就整段一个镜头。

    这样“雨夜的车站，她撑着伞等末班车，灯一闪一闪”会得到 3 个镜头，
    而不是一张图从头放到尾。
    """
    text = " ".join(str(prompt or "").split())
    parts = [
        part.strip()
        for part in re.split(r"[，。；、,;!?！？|]+", text)
        if len(part.strip()) >= 2
    ]
    # 去掉“帮我做一段…MV”这种指令前缀：它没有画面信息，留着只会让每个镜头都带上
    if parts:
        cleaned = []
        for part in parts:
            part = re.sub(
                r"^(帮我|给我|请|麻烦)?(做|生成|剪|来|出|拍)(一段|一个|个)?", "", part
            )
            part = re.sub(r"(动画|短片|视频|MV|mv|动图|GIF|gif)$", "", part.strip())
            for label in ("水彩", "像素", "水墨", "赛博朋克", "胶片", "黑白",
                          "小清新", "动漫", "日落", "风格"):
                part = part.replace(label, "")
            part = part.strip(" ，,、")
            if len(part) >= 2:
                cleaned.append(part)
        parts = cleaned or parts
    parts = parts[: max(1, int(max_shots))]
    if not parts:
        return [(0.0, max(1.0, float(span or 11.0)), text or "一段动画")]
    step = max(1.0, float(span or 11.0)) / len(parts)
    return [
        (round(index * step, 2), round((index + 1) * step, 2), part)
        for index, part in enumerate(parts)
    ]


def _workers() -> int:
    """并行渲染用几个 worker。核多就多开，最多 6 个（再多人机都卡）。"""
    try:
        cores = os.cpu_count() or 4
    except Exception:  # noqa: BLE001
        cores = 4
    return max(2, min(6, cores // 2))


def _encode_frames(job_id: str, project: Path, fps: int, audio: str = "") -> str:
    """把 out/frames/*.jpg 编成 mp4（顺带混音）。"""
    ffmpeg = _which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("没有 ffmpeg，无法编码")

    out = project / "out" / "video.mp4"
    has_audio = bool(audio) and Path(audio).is_file()
    # ffmpeg 的选项绑定「下一个输入/输出」：所有输入写在一起，编码参数放后面。
    # 以前把 -c:v libx264 夹在两个 -i 之间，它被当成第二个输入的解码器，
    # 于是带配乐的任务必崩：Unknown decoder 'libx264' + Decoder not found。
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-framerate", str(fps),
        "-i", "out/frames/f%05d.jpg",
    ]
    if has_audio:
        cmd += ["-i", audio]
    cmd += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    if has_audio:
        cmd += ["-map", "0:v:0", "-map", "1:a:0",
                "-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd.append("out/video.mp4")

    _touch(job_id, STAGE_ENCODE, progress=0.98, note="编码视频")
    completed = subprocess.run(cmd, cwd=str(project), capture_output=True,
                               text=True, timeout=1800, env=_job_env(project))
    if completed.returncode != 0:
        raise RuntimeError(f"编码失败：{(completed.stderr or '')[-400:]}")
    if not out.exists():
        raise RuntimeError("编码结束但没找到 out/video.mp4")
    return str(out)


def _render_parallel(job_id: str, project: Path, duration: float, fps: int,
                     audio: str = "", fast: bool = False) -> str:
    """长片子：把时间轴切成几片，**起多个 render.mjs 进程**同时渲。

    注意 --workers 参数没用：多个 worker 共享同一个浏览器进程，
    JS/Cairo 是单线程的，实测没有提速。真正的并行要靠多进程。
    """
    node = _which("node")
    if not node:
        raise RuntimeError("没有 node，无法渲染")
    chrome = _find_chrome()

    # 每个进程约吃 500MB 内存，别开太多
    try:
        cores = os.cpu_count() or 4
    except Exception:  # noqa: BLE001
        cores = 4
    shards = max(1, min(4, cores // 3))
    span = duration / shards

    timeout = _render_timeout(duration / shards, fps) * 2
    processes = []
    outputs: dict[int, list[str]] = {}

    for index in range(shards):
        begin = index * span
        end = duration if index == shards - 1 else (index + 1) * span
        cmd = [node, "render.mjs", "--frames", "--workers=1",
               f"--range={begin:.2f}:{end:.2f}", f"--fps={fps}"]
        if fast:
            cmd += ["--w=1280", "--h=720"]
        if chrome:
            cmd.append(f"--chrome={chrome}")
        logger.info("分片 %d/%d：%.1f~%.1f 秒", index + 1, shards, begin, end)
        processes.append(subprocess.Popen(
            cmd, cwd=str(project), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", env=_job_env(project),
        ))
        outputs[index] = []

    import re as _re
    deadline = time.time() + timeout
    finished: dict[int, int] = {}

    while any(p.poll() is None for p in processes):
        for index, proc in enumerate(processes):
            if proc.poll() is not None and index not in finished:
                finished[index] = proc.returncode or 0
        total_done = _count_frames(project)
        total_frames = max(1, int(duration * fps))
        _touch(job_id, STAGE_RENDER,
               progress=round(min(0.97, total_done / total_frames), 3),
               note=f"渲染 {total_done}/{total_frames} 帧（{shards} 个进程）")
        if time.time() > deadline:
            for proc in processes:
                if proc.poll() is None:
                    _kill_tree(proc.pid)
            raise RuntimeError(f"渲染超时（{timeout}s）：把时长改短一些再试")
        time.sleep(3)

    for index, proc in enumerate(processes):
        try:
            tail, _ = proc.communicate(timeout=60)
        except Exception:  # noqa: BLE001
            tail = ""
        code = proc.returncode or 0
        if code != 0:
            try:
                (project / "out" / f"render-{index}.log").write_text(tail or "", encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"分片 {index} 渲染失败（退出码 {code}）：{(tail or '')[-300:]}")

    return _encode_frames(job_id, project, fps, audio)


def _count_frames(project: Path) -> int:
    folder = project / "out" / "frames"
    if not folder.exists():
        return 0
    return sum(1 for item in folder.glob("f*.jpg") if item.stat().st_size > 1000)
