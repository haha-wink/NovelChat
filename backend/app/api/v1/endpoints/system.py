"""系统：健康检查 / 会话 / 渠道 / 贴纸。"""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

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
    from services import angles

    return angles.describe()


@router.get("/animation/capabilities")
async def animation_capabilities() -> dict:
    """这台机器能不能渲动画。"""
    import skills.work.animation.engine as animation_engine

    return await anyio.to_thread.run_sync(animation_engine.capabilities)


@router.get("/animation")
async def animation_jobs(limit: int = 20) -> list[dict]:
    import skills.work.animation.engine as animation_engine

    return await anyio.to_thread.run_sync(
        functools.partial(animation_engine.list_jobs, limit=limit)
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
    from services import intent, time_tools  # noqa: F401 - 导入即注册工具

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
    from services import angles as _angles
    import skills
    from services import emotion_model
    from services import name_engine

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
