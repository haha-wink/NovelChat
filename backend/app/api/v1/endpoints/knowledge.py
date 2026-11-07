"""知识层：RAG 索引与检索、记忆清洗、对话蒸馏。"""

from fastapi import APIRouter

from api.v1.deps import *  # noqa: F401,F403

router = APIRouter()


# RAG 记忆库：状态 / 重建索引 / 检索预览 / 对话蒸馏
# ---------------------------------------------------------------------------
@router.get("/rag/status")
async def rag_status() -> dict:
    return await anyio.to_thread.run_sync(rag.backend_status)


@router.post("/rag/index")
async def rag_reindex(force: bool = False) -> dict:
    """给记忆补向量（换模型或首次启用时跑一次；平时写入时会自动索引）。"""
    return await anyio.to_thread.run_sync(functools.partial(rag.index_all, force=force))


@router.get("/characters/{character_id}/recall")
async def recall_memory(character_id: str, q: str) -> dict:
    """调试用：看这句「q」会检索到哪些记忆（**不**注入提示词）。"""
    await anyio.to_thread.run_sync(require_character, character_id)
    hits = await anyio.to_thread.run_sync(
        functools.partial(rag.search, character_id, q, top_k=8)
    )
    return {
        "query": q,
        "scene": scene_mod.detect(q),
        "backend": rag.backend(),
        "hits": hits,
    }


@router.post("/characters/{character_id}/distill")
async def distill_character(character_id: str) -> dict:
    """把该角色的对话流水蒸馏成一条长期记忆（7 天窗口外的部分）。"""
    character = await anyio.to_thread.run_sync(require_character, character_id)
    book = await anyio.to_thread.run_sync(db.get_book, character["book_id"])
    shelf_id = str((book or {}).get("shelf_id") or "")
    if not shelf_id:
        raise HTTPException(status_code=400, detail="这个角色还没有归属书架")
    result = await anyio.to_thread.run_sync(
        functools.partial(
            chat_memory.distill,
            shelf_id,
            character_id,
            summarizer=memory_summarizer(),
            character_name=character.get("name") or "",
        )
    )
    await anyio.to_thread.run_sync(rag.index_character, character_id)
    return result


@router.post("/maintenance/clean-memories")
async def clean_memories(dry_run: bool = True, character_id: Optional[str] = None) -> dict:
    """存量清洗：把已抽好的记忆里混着的广告/水印洗掉（**不重抽**，角色 id 不变）。

    默认 ``dry_run=true``，只返回会改多少条、给了样例；确认后再传 ``false`` 真正落库。
    ``character_id`` 可以只洗一个角色。
    改动过的条目会作废旧向量，建议随后调一次 ``/api/rag/index?force=true``。
    """
    return await anyio.to_thread.run_sync(
        functools.partial(
            cleaner.clean_memories, dry_run=dry_run, character_id=character_id
        )
    )


@router.post("/maintenance/clean-chat-images")
async def clean_chat_images(days: int = 0) -> dict:
    """手动清理聊天图片。

    ``days`` 留空或 <=0 时用配置里的保留期（``CHAT_IMAGE_RETENTION_DAYS``）。
    想永久保留就把配置设成 0——服务层对 ``days <= 0`` 是"不删"，不会误清空。
    """
    from services import housekeeping

    settings = get_settings()
    keep_days = int(days) if days > 0 else int(settings.chat_image_retention_days)
    result = await anyio.to_thread.run_sync(
        functools.partial(
            housekeeping.cleanup_chat_images,
            settings.uploads_path / "chat",
            days=keep_days,
        )
    )
    agent_log.log(
        "chat_images_cleaned",
        owner_type="manager",
        severity="info",
        payload={"days": keep_days, **result},
    )
    return {"days": keep_days, **result}
