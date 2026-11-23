"""系统：健康检查 / 会话 / 渠道 / 贴纸。"""

import functools
import re
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

import skills.registry as skills_registry  # noqa: E402
from api.v1.deps import *  # noqa: F401,F403

router = APIRouter()

#: 进程启动时间（健康检查里给前端对版本用）
_STARTED_AT = datetime.now().isoformat(timespec="seconds")

TWEMOJI_CDN = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/svg/{name}.svg"
_STICKER_NAME = re.compile(r"^[0-9a-f]{1,8}(-[0-9a-f]{1,8})*$")



def build_info() -> dict:
    """这份后端是哪个提交、什么时候起的——排查"改了没生效"用的。

    前端也会显示同样的字段；两边对不上就说明前端是旧缓存。
    """
    commit = "unknown"
    try:
        head = (Path(__file__).resolve().parents[5] / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = (Path(__file__).resolve().parents[5] / ".git" / head[5:].strip())
            commit = ref.read_text(encoding="utf-8").strip()[:7]
        else:
            commit = head[:7]
    except Exception:  # noqa: BLE001 - 读不到就算了
        pass
    return {
        "commit": commit,
        "started_at": _STARTED_AT,
        "features": ["batch_group", "dedupe", "angles", "greeting", "intent_tool"],
    }


def animation_describe() -> dict:
    import skills.work.animation.engine as animation_engine

    try:
        return animation_engine.describe()
    except Exception:  # noqa: BLE001 - 动画模块出问题不能拖垮 health
        return {"available": False, "error": "动画模块未就绪"}


def angles_describe() -> dict:
    """发言分工清单。"""
    from services.dialogue import angles

    return angles.describe()


@router.get("/skills/validate")
async def skills_validate() -> dict:
    """体检技能包：写错的 SKILL.md、未知工具、未知占位符都会列出来。"""
    import skills.validate as skill_validate

    report = await anyio.to_thread.run_sync(
        functools.partial(skill_validate.validate_paths, [skills_registry.SKILLS_DIR])
    )
    return report.as_dict()


@router.get("/animation/capabilities")
async def animation_capabilities() -> dict:
    """这台机器能不能渲动画。"""
    import skills.work.animation.engine as animation_engine

    return await anyio.to_thread.run_sync(animation_engine.capabilities)


@router.get("/animation")
async def animation_jobs(limit: int = 20, conversation_id: str = "") -> list[dict]:
    """任务列表（附磁盘占用统计，界面右侧任务栏用）。

    ``conversation_id`` 给了就只看这个对话的任务：一个对话一个任务栏，
    打开群聊时不会翻出别的对话（甚至别的书架）的陈年任务。
    """
    import skills.work.animation.engine as animation_engine

    return await anyio.to_thread.run_sync(
        functools.partial(
            animation_engine.list_jobs, limit=limit,
            conversation_id=(conversation_id or None),
        )
    )




#: 任务号白名单：防止 ../ 之类的路径穿越
_JOB_ID = re.compile(r"^anim_[0-9a-f]{6,}$")


def _serve_video(path: Path, request: Request, *, download: bool) -> Response:
    """发视频文件，支持 Range（否则浏览器拖不动进度条）。"""
    size = path.stat().st_size
    disposition = "attachment" if download else "inline"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'{disposition}; filename="{path.name}"',
        "Cache-Control": "private, max-age=3600",
    }

    range_header = request.headers.get("range") or ""
    match = re.match(r"bytes=(\d*)-(\d*)", range_header) if range_header else None
    if match is None:
        return FileResponse(path, media_type="video/mp4", headers=headers)

    start = int(match.group(1) or 0)
    end = int(match.group(2)) if match.group(2) else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    length = end - start + 1

    def chunks():
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                piece = handle.read(min(65536, remaining))
                if not piece:
                    break
                remaining -= len(piece)
                yield piece

    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(length)
    return StreamingResponse(chunks(), status_code=206, media_type="video/mp4",
                             headers=headers)




@router.delete("/animation")
async def animation_clear() -> dict:
    """清空所有任务（含目录）。正在本进程里跑的仍然会被挡住。"""
    import skills.work.animation.engine as animation_engine

    return await anyio.to_thread.run_sync(
        functools.partial(animation_engine.cleanup, keep=0, max_mb=0)
    )


@router.get("/animation/stats")
async def animation_stats() -> dict:
    """任务占用：多少个、共多大（任务栏用）。"""
    import skills.work.animation.engine as animation_engine

    return await anyio.to_thread.run_sync(animation_engine.stats)


@router.delete("/animation/{job_id}")
async def animation_delete(job_id: str) -> dict:
    """删掉一个任务（含项目目录）。"""
    import skills.work.animation.engine as animation_engine

    return await anyio.to_thread.run_sync(animation_engine.delete_job, job_id)


@router.post("/animation/cleanup")
async def animation_cleanup(keep: int = 8, max_mb: int = 1500, stale_hours: float = 6.0) -> dict:
    """清理老任务：只留最近 keep 个，总量不超过 max_mb。"""
    import skills.work.animation.engine as animation_engine

    return await anyio.to_thread.run_sync(
        functools.partial(animation_engine.cleanup, keep=keep,
                          max_mb=max_mb, stale_hours=stale_hours)
    )




#: 允许上传的素材类型
_ANIM_ASSET_KINDS = {
    "lyrics": {".lrc", ".txt"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
}


def _assets_dir() -> Path:
    path = Path(get_settings().data_path) / "animation" / "assets"
    path.mkdir(parents=True, exist_ok=True)
    return path


@router.post("/animation/start", status_code=201)
async def animation_start_job(payload: dict) -> dict:
    """界面直接起任务（不经过模型）：歌词文件 / 配乐 / 一句话需求。"""
    import skills.work.animation.engine as animation_engine

    prompt = str(payload.get("prompt") or "帮我做一段歌词MV").strip()
    lyrics_file = str(payload.get("lyrics_file") or "").strip()
    audio = str(payload.get("audio") or "").strip()

    lrc = ""
    if lyrics_file:
        path = Path(lyrics_file)
        if not path.is_file():
            raise HTTPException(status_code=400, detail="歌词文件不存在，请重新上传")
        lrc = path.read_text(encoding="utf-8", errors="replace")

    if audio and not Path(audio).is_file():
        raise HTTPException(status_code=400, detail="音频文件不存在，请重新上传")

    # 时长：显式给的优先，否则从原话里抠（"前30s的MV"）
    duration = float(payload.get("duration") or 0) or animation_engine._parse_duration(prompt)

    job = await anyio.to_thread.run_sync(
        functools.partial(
            animation_engine.create_job,
            prompt,
            style=str(payload.get("style") or "watercolor"),
            lyrics=str(payload.get("lyrics") or ""),
            lrc=lrc,
            lyrics_file=lyrics_file,
            audio=audio,
            duration=duration,
            quality=str(payload.get("quality") or "standard"),
            section=str(payload.get("section") or ""),
            conversation_id=str(payload.get("conversation_id") or ""),
        )
    )
    return {"ok": True, "job": job}


@router.post("/animation/{job_id}/quality")
async def animation_quality(job_id: str, quality: str = "fast") -> dict:
    """换画质重渲：fast = 720p/12fps，standard = 1080p。项目与风格不变。"""
    import skills.work.animation.engine as animation_engine

    if not _JOB_ID.match(job_id):
        raise HTTPException(status_code=400, detail="任务号不合法")
    return await anyio.to_thread.run_sync(
        functools.partial(animation_engine.rerender, job_id, quality)
    )


@router.post("/animation/assets", status_code=201)
async def animation_upload_asset(file: UploadFile = File(...), kind: str = Form("lyrics")) -> dict:
    """上传歌词（.lrc/.txt）或配乐（mp3/wav…）。返回落盘路径，供起任务时引用。"""
    import skills.work.animation.engine as animation_engine

    kind = (kind or "lyrics").strip().lower()
    allowed = _ANIM_ASSET_KINDS.get(kind)
    if not allowed:
        raise HTTPException(status_code=400, detail=f"kind 只能是 {list(_ANIM_ASSET_KINDS)}")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"{kind} 只支持 {'、'.join(sorted(allowed))}，你传的是 {suffix or '无扩展名'}",
        )

    raw = await file.read()
    limit = 40 * 1024 * 1024 if kind == "audio" else 2 * 1024 * 1024
    if not raw:
        raise HTTPException(status_code=400, detail="文件是空的")
    if len(raw) > limit:
        raise HTTPException(status_code=413, detail=f"文件太大（上限 {limit // 1024 // 1024}MB）")

    target = _assets_dir() / f"{kind}_{uuid.uuid4().hex[:12]}{suffix}"
    target.write_bytes(raw)

    payload = {
        "ok": True,
        "kind": kind,
        "path": str(target),
        "name": file.filename or target.name,
        "bytes": len(raw),
    }

    if kind == "lyrics":
        text = raw.decode("utf-8", errors="replace")
        parsed = animation_engine.parse_lrc(text)
        lines = [item["text"] for item in parsed if item.get("text")]
        payload["lines"] = len(lines)
        payload["timed"] = any(item.get("start") is not None for item in parsed)
        payload["preview"] = lines[:5]

    return payload


@router.get("/animation/assets")
async def animation_list_assets() -> list[dict]:
    """已上传的素材（界面上给个下拉，不用重复传）。"""
    folder = _assets_dir()
    items = []
    for path in sorted(folder.iterdir(), reverse=True)[:50]:
        if not path.is_file():
            continue
        kind = "lyrics" if path.name.startswith("lyrics_") else "audio"
        items.append({
            "kind": kind,
            "path": str(path),
            "name": path.name,
            "bytes": path.stat().st_size,
        })
    return items


@router.get("/animation/{job_id}/video")
async def animation_video(job_id: str, request: Request, download: int = 0):
    """把渲染好的 mp4 发出去（群里说"做好了"，得让用户真能点开）。"""
    import skills.work.animation.engine as animation_engine

    if not _JOB_ID.match(job_id):
        raise HTTPException(status_code=400, detail="任务号不合法")

    job = await anyio.to_thread.run_sync(animation_engine.job_status, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="没有这个任务")
    video = Path(str(job.get("video") or ""))
    if not video.is_file():
        raise HTTPException(status_code=404, detail="这个任务还没有成片")

    return await anyio.to_thread.run_sync(
        functools.partial(_serve_video, video, request, download=bool(download))
    )


@router.get("/animation/{job_id}")
async def animation_job(job_id: str) -> dict:
    import skills.work.animation.engine as animation_engine

    job = await anyio.to_thread.run_sync(animation_engine.job_status, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.post("/animation/{job_id}/confirm")
async def animation_confirm(job_id: str) -> dict:
    """歌词/MV 类任务：确认分镜后继续渲染。"""
    import skills.work.animation.engine as animation_engine

    return await anyio.to_thread.run_sync(animation_engine.confirm, job_id)


def tools_describe() -> dict:
    """工具层清单（协议层 + 框架层）。"""
    from agent.tools import langgraph_tools
    import skills  # noqa: F401 - 导入即加载技能与引擎（含 animation 工具）
    from services.dialogue import intent
    from services.runtime import time_tools  # noqa: F401 - 导入即注册工具

    return langgraph_tools.describe()


def skills_describe() -> dict:
    """业务技能清单。"""
    import skills

    return skills.describe()


@router.get("/health")
async def health() -> dict:
    from agent import graph as agent_graph
    from agent import group_graph as agent_group_graph
    from agent.tools import langgraph_tools
    from services.dialogue import angles as _angles
    import skills
    from services.dialogue import emotion_model
    from services.catalog import name_engine

    settings = get_settings()
    return {
        "status": "ok",
        # 对话工作流跑在哪张图上（节点顺序一目了然）
        "build": build_info(),
        "animation": animation_describe(),
        "workflow": agent_graph.describe(),
        "group_workflow": agent_group_graph.describe(),
        "group_tuning": {
            "duplicate_threshold": settings.group_duplicate_threshold,
            "angles": [item["name"] for item in angles_describe()["angles"]],
        },
        # 工具三层：协议层清单 + 业务技能
        "tools": tools_describe(),
        "skills": skills_describe(),
        "api_key_configured": bool(settings.deepseek_api_key.strip()),
        "model": settings.deepseek_model,
        "extraction_model": settings.extraction_model_name,
        "extraction_mode": settings.extraction_mode_name,
        "supported_formats": list(SUPPORTED_SUFFIXES),
        "books": len(db.list_books()),
        # 人名识别引擎：装没装 jieba/HanLP，一眼可见
        "name_engine": name_engine.get_engine().info(),
        # 情绪模型后端：rules 永远可用，onnx 是可选加速
        "emotion_model": emotion_model.info(),
        # RAG 记忆库后端：vector（bge-small-zh）/ lexical（BM25 降级）
        "rag": rag.status_light(),
        # 前端/排查用：确认跑的是不是带这些能力的后端
        "features": [
            "avatars",
            "avatar_proxy",
            "character_audit",
            "rag_memory",
            "memory_distill",
            "extraction_agent" if settings.extraction_mode_name == "agent" else "extraction_hybrid",
        ],
        "avatar_enabled": settings.agent_enable_avatars,
        "web_enabled": settings.agent_enable_web,
    }


@router.post("/session", response_model=SessionResponse)
async def create_session(req: SessionRequest) -> SessionResponse:
    session = issue_session(req.display_name)
    return SessionResponse(
        token=session.token,
        user_id=session.user_id,
        session_id=session.session_id,
        display_name=session.display_name,
    )


# ---------------------------------------------------------------------------
# 作品
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 书库 / 书架
#
# 三级结构：libraries（书库，最大）-> shelves（书架，一套独立系统）-> books（书）
# 书架是隔离边界，Agent 日志按书架分区，跨书架互不可见。
# ---------------------------------------------------------------------------
@router.get("/stickers")
async def sticker_catalog() -> dict:
    """贴纸目录：情绪 -> 候选贴纸。"""
    return {"emotions": expression.catalog()}


@router.get("/sticker/{name}.svg")
async def sticker_svg(name: str) -> FileResponse:
    """Twemoji 贴纸（CC-BY 4.0）。首访落盘缓存，之后走本地。"""
    if not _STICKER_NAME.match(name):
        raise HTTPException(status_code=400, detail="非法的贴纸名")
    target = STICKER_DIR / f"{name}.svg"
    if not target.exists():
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(TWEMOJI_CDN.format(name=name))
            if response.status_code != 200:
                raise HTTPException(status_code=404, detail="没有这张贴纸")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.content)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("贴纸拉取失败 %s：%s", name, exc)
            raise HTTPException(status_code=502, detail="暂时取不到贴纸") from exc
    return FileResponse(
        target,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=604800"},
    )


# ---------------------------------------------------------------------------
# 角色名单审核（agent）
# ---------------------------------------------------------------------------


@router.get("/channels")
async def list_channels() -> dict:
    """渠道能力清单：哪个平台能用、支不支持流式 / @ / 图片。

    现在只有 web 是 enabled；QQ / 微信用 is_ready=False 标着，接入计划写在
    ``channels/qq.py`` / ``channels/wechat.py`` 的文件头注释里。
    """
    return {
        "default": channel_registry.DEFAULT_CHANNEL,
        "channels": channel_registry.describe_all(),
    }


# ---------------------------------------------------------------------------
