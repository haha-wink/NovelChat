"""系统：健康检查 / 会话 / 渠道 / 贴纸。"""

from fastapi import APIRouter

from api.v1.deps import *  # noqa: F401,F403

router = APIRouter()

TWEMOJI_CDN = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/svg/{name}.svg"
_STICKER_NAME = re.compile(r"^[0-9a-f]{1,8}(-[0-9a-f]{1,8})*$")



@router.get("/health")
async def health() -> dict:
    from agent import graph as agent_graph
    from services import emotion_model
    from services import name_engine

    settings = get_settings()
    return {
        "status": "ok",
        # 对话工作流跑在哪张图上（节点顺序一目了然）
        "workflow": agent_graph.describe(),
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
