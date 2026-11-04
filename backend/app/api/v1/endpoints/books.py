"""作品：上传、列表、删除、重新抽取、名单审核。"""

from fastapi import APIRouter

from api.v1.deps import *  # noqa: F401,F403

router = APIRouter()


@router.get("/books")
async def list_books(shelf_id: str = "") -> list[dict]:
    return await anyio.to_thread.run_sync(
        lambda: [book_payload(b) for b in db.list_books(shelf_id or None)]
    )


@router.post("/books", status_code=201)
async def upload_book(
    file: UploadFile = File(...),
    title: str = Form(""),
    shelf_id: str = Form(""),
) -> dict:
    settings = get_settings()

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件是空的")
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过上限（{settings.max_upload_bytes // (1024 * 1024)}MB）",
        )

    try:
        text, kind = await anyio.to_thread.run_sync(extract_text, file.filename or "", raw)
    except DocumentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if len(text) < settings.min_book_chars:
        raise HTTPException(
            status_code=400,
            detail=f"正文太短（{len(text)} 字），至少需要 {settings.min_book_chars} 字才能抽角色",
        )
    if len(text) > settings.max_book_chars:
        raise HTTPException(
            status_code=413,
            detail=f"正文超过上限（{settings.max_book_chars} 字），请拆分后再上传",
        )

    book_title = (title or "").strip() or Path(file.filename or "未命名小说").stem
    create = functools.partial(
        library.create_book,
        title=book_title[:80],
        source_name=(file.filename or "")[:200],
        source_type=kind,
        text=text,
        shelf_id=shelf_id.strip(),
    )
    try:
        book = await anyio.to_thread.run_sync(create)
    except ValueError as exc:  # 书架不存在
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    agent_log.log(
        "book_added",
        owner_type="manager",
        book_id=book["id"],
        payload={
            "title": book["title"],
            "chars": book["char_count"],
            "chunks": book["chunk_count"],
            "source_type": kind,
        },
    )

    library.schedule_extraction(book["id"])
    logger.info("新作品《%s》已入库（%d 字，%d 块），开始抽取角色",
                book["title"], book["char_count"], book["chunk_count"])

    return await anyio.to_thread.run_sync(functools.partial(book_payload, book))


@router.get("/books/{book_id}")
async def get_book(book_id: str) -> dict:
    book = await anyio.to_thread.run_sync(require_book, book_id)
    return await anyio.to_thread.run_sync(functools.partial(book_payload, book))


@router.delete("/books/{book_id}")
async def delete_book(book_id: str) -> dict:
    deleted = await anyio.to_thread.run_sync(library.delete_book, book_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="作品不存在")
    return {"deleted": True}


@router.post("/books/{book_id}/retry")
async def retry_book(book_id: str) -> dict:
    settings = get_settings()
    book = await anyio.to_thread.run_sync(require_book, book_id)
    if not settings.deepseek_api_key.strip():
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 DEEPSEEK_API_KEY，请在 backend/.env 中设置后重启后端",
        )
    if library.extraction_running(book_id):
        raise HTTPException(status_code=409, detail="这本书正在抽取中")

    library.retry_extraction(book_id)
    return await anyio.to_thread.run_sync(functools.partial(book_payload, book))


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
@router.get("/books/{book_id}/characters")
async def list_characters(book_id: str) -> list[dict]:
    await anyio.to_thread.run_sync(require_book, book_id)
    characters = await anyio.to_thread.run_sync(db.list_characters, book_id)
    return [character_payload(c) for c in characters]


@router.post("/books/{book_id}/characters", status_code=201)
async def create_character(book_id: str, payload: CharacterCreate) -> dict:
    await anyio.to_thread.run_sync(require_book, book_id)
    try:
        character = await anyio.to_thread.run_sync(
            functools.partial(
                library.create_character,
                book_id,
                name=payload.name,
                role=payload.role,
                personality=payload.personality,
                speech_style=payload.speech_style,
                background=payload.background,
                avatar=payload.avatar,
                is_user_persona=payload.is_user_persona,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return character_payload(character)


@router.post("/books/{book_id}/verify")
async def verify_book_characters(book_id: str) -> dict:
    settings = get_settings()
    api_key = settings.deepseek_api_key.strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 DEEPSEEK_API_KEY，请在 backend/.env 中设置后重启后端",
        )

    book = await anyio.to_thread.run_sync(require_book, book_id)
    text = await anyio.to_thread.run_sync(library.read_source, book_id)
    if not text:
        raise HTTPException(status_code=400, detail="这本书的原文丢失了，请重新上传")

    from agent.extraction import ExtractionAgent

    agent = ExtractionAgent(
        book_title=book.get("title") or "",
        text=text,
        api_key=api_key,
        model=settings.extraction_model_name,
        max_steps=settings.agent_max_steps,
        enable_web=settings.agent_enable_web,
        enable_avatars=False,
    )
    try:
        result = await agent.run()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent 审核失败")
        raise HTTPException(status_code=500, detail=f"审核失败：{str(exc)[:200]}") from exc

    chunks = await anyio.to_thread.run_sync(library.read_chunks, book_id)
    characters = extractor.attach_excerpts(
        result.get("characters") or [],
        chunks,
        excerpt_chunks=settings.excerpt_chunks,
        excerpt_chars=settings.excerpt_chars,
    )[: settings.max_characters]

    created = await anyio.to_thread.run_sync(
        functools.partial(db.replace_novel_characters, book_id, characters)
    )
    return {
        "characters": len(created),
        "report": result.get("report") or {},
        "trace": result.get("trace") or [],
    }


# ---------------------------------------------------------------------------
# 聊天
# ---------------------------------------------------------------------------
