"""角色：增删改查、状态、头像、合并、单聊历史。"""

from fastapi import APIRouter

from api.v1.deps import *  # noqa: F401,F403

router = APIRouter()


@router.delete("/characters/{character_id}/history")
async def clear_character_history(
    character_id: str, request: Request, token: str = "", mode: str = "distill"
) -> dict:
    """清空我与这个角色的单聊历史。

    * ``mode=distill``（默认）：先把这段对话蒸馏成长期记忆，再删流水。
    * ``mode=purge``：连记忆一起删。
    """
    character = await anyio.to_thread.run_sync(require_character, character_id)
    session = resolve_session(token, request)
    book = await anyio.to_thread.run_sync(db.get_book, character["book_id"]) or {}
    shelf_id = str(book.get("shelf_id") or "")

    summarizer = None
    try:
        summarizer = memory_summarizer()
    except Exception:  # noqa: BLE001
        pass

    return await anyio.to_thread.run_sync(
        functools.partial(
            chat_memory.clear_conversation,
            shelf_id,
            user_id=session.user_id,
            character_id=character_id,
            summarizer=summarizer,
            mode=mode,
        )
    )


@router.get("/characters/{character_id}/history")
async def character_history(
    character_id: str, request: Request, token: str = "", limit: int = 60
) -> dict:
    """单聊历史（当前用户与这个角色的最近 N 条）。"""
    character = await anyio.to_thread.run_sync(require_character, character_id)
    session = resolve_session(token, request)
    book = await anyio.to_thread.run_sync(db.get_book, character["book_id"]) or {}
    shelf_id = str(book.get("shelf_id") or "")
    turns = await anyio.to_thread.run_sync(
        functools.partial(
            db.list_history,
            shelf_id,
            user_id=session.user_id,
            character_id=character_id,
            limit=limit,
        )
    )
    return {"character_id": character_id, "turns": turns}


@router.get("/characters/{character_id}")
async def get_character(character_id: str, user_name: str = "朋友") -> dict:
    character = await anyio.to_thread.run_sync(require_character, character_id)
    return await anyio.to_thread.run_sync(
        functools.partial(character_payload, character, with_prompt=True, user_name=user_name)
    )


@router.get("/characters/{character_id}/status")
async def character_status(character_id: str, request: Request, token: str = "") -> dict:
    """角色详情 + 当前状态（点角色卡片看的就是这个）。

    **不含好感度/情绪数值**：用户只看到「什么心情」「对你什么态度」这类人话。
    数值留在库里和 affect_change 日志里，供我们自己调参。
    """
    character = await anyio.to_thread.run_sync(require_character, character_id)
    book = (await anyio.to_thread.run_sync(db.get_book, character["book_id"])) or {}
    shelf = None
    if book.get("shelf_id"):
        shelf = await anyio.to_thread.run_sync(db.get_shelf, book["shelf_id"])

    memories = await anyio.to_thread.run_sync(
        functools.partial(db.list_memory_items, character_id, kind="excerpt")
    )
    by_source: dict[str, int] = {}
    for item in memories:
        key = item.get("source_book_id") or "(未知来源)"
        by_source[key] = by_source.get(key, 0) + 1

    # 这个角色在群里的状态（模型/静默）——详情面板要据此显示「静默/恢复」
    member = None
    if shelf:
        members = await anyio.to_thread.run_sync(
            functools.partial(db.list_shelf_members, shelf["id"], include_left=True)
        )
        member = next((m for m in members if m["character_id"] == character_id), None)

    children = await anyio.to_thread.run_sync(
        functools.partial(db.list_merged_children, character_id)
    )

    user_id = ""
    if token:
        session = resolve_session(token, request)
        user_id = session.user_id

    turns = 0
    last_active = None
    if user_id:
        state = await anyio.to_thread.run_sync(
            functools.partial(affect.load_state, character, user_id)
        )
        described = affect.describe(state)
        turns = int(state.get("turns") or 0)
        last_active = state.get("updated_at")
    else:
        baseline = affect.baseline_for(character)
        described = affect.describe(
            {
                "emotion": affect.label_for(baseline["valence"], baseline["arousal"]),
                "favorability": affect.DEFAULT_FAVORABILITY,
            }
        )
        described["attitude_text"] = "还没聊过，暂时看不出态度"

    return {
        "character": {
            "id": character["id"],
            "name": character["name"],
            "aliases": character.get("aliases") or [],
            "role": character.get("role") or "",
            "personality": character.get("personality") or [],
            "speech_style": character.get("speech_style") or "",
            "background": character.get("background") or "",
            "relationships": character.get("relationships") or [],
            "avatar": character.get("avatar") or "🎭",
            "avatar_url": character.get("avatar_url"),
            "source": character.get("source") or "novel",
            "is_user_persona": bool(character.get("is_user_persona")),
            "appearances": int(character.get("appearances") or 0),
            "created_at": character.get("created_at"),
            "updated_at": character.get("updated_at"),
            "book_id": character.get("book_id"),
            "book_title": book.get("title") or "",
            "shelf_id": book.get("shelf_id"),
            "shelf_title": (shelf or {}).get("title") or "",
        },
        "status": {
            "mood": described["mood"],
            "mood_text": described["mood_text"],
            "attitude_text": described["attitude_text"],
            "turns": turns,
            "last_active": last_active,
            # 群聊（P4）接入前先占位，前端已有展示位
            "presence": "在场",
            "memory": {
                "total": len(memories),
                "by_source": by_source,
                # 同系列多本书合并后的摘要（没有就是空串）
                "summary": agent_memory.summary(character_id),
            },
            "member": (
                {"status": member["status"], "state": member.get("state")}
                if member
                else None
            ),
            # 已经并入了这个角色的其它角色（可以撤销）
            "merged_children": [
                {"id": child["id"], "name": child["name"], "book_id": child["book_id"]}
                for child in children
            ],
        },
    }


@router.get("/characters/{character_id}/memories")
async def list_character_memories(character_id: str) -> dict:
    """角色的初始记忆条目（带来源书）。用于排查「为什么这个角色像失忆了」。"""
    character = await anyio.to_thread.run_sync(require_character, character_id)
    items = await anyio.to_thread.run_sync(
        functools.partial(db.list_memory_items, character_id, kind="excerpt")
    )
    by_source: dict[str, int] = {}
    for item in items:
        key = item.get("source_book_id") or "(未知来源)"
        by_source[key] = by_source.get(key, 0) + 1
    return {
        "character_id": character["id"],
        "name": character["name"],
        "count": len(items),
        "by_source": by_source,
        "items": items,
    }


@router.patch("/characters/{character_id}")
async def update_character(character_id: str, payload: CharacterUpdate) -> dict:
    await anyio.to_thread.run_sync(require_character, character_id)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="没有要修改的字段")
    if "name" in fields and not (fields["name"] or "").strip():
        raise HTTPException(status_code=400, detail="角色名不能为空")

    character = await anyio.to_thread.run_sync(
        functools.partial(db.update_character, character_id, **fields)
    )
    if not character:
        raise HTTPException(status_code=404, detail="角色不存在")
    return character_payload(character)


@router.delete("/characters/{character_id}")
async def delete_character(character_id: str) -> dict:
    deleted = await anyio.to_thread.run_sync(db.delete_character, character_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="角色不存在")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# 头像（爬取候选 + 代理图片，避免热链/防盗链）
# ---------------------------------------------------------------------------
@router.post("/characters/{character_id}/avatars")
async def crawl_character_avatars(character_id: str, payload: AvatarRequest | None = None) -> dict:
    from agent.tools.avatar import AvatarCrawler

    settings = get_settings()
    if not settings.agent_enable_avatars:
        raise HTTPException(
            status_code=400,
            detail="头像爬取已关闭（backend/.env 里 AGENT_ENABLE_AVATARS=false）",
        )

    character = await anyio.to_thread.run_sync(require_character, character_id)
    book = await anyio.to_thread.run_sync(require_book, character["book_id"])
    work_title = (payload.work_title if payload else "") or book.get("title") or ""

    crawler = AvatarCrawler(max_candidates=settings.avatar_max_candidates)
    candidates = await crawler.find(character_name=character["name"], work_title=work_title)
    await anyio.to_thread.run_sync(
        functools.partial(db.update_character, character_id, avatar_candidates=candidates)
    )
    if not candidates:
        logger.info("角色 %s 没抓到头像：%s", character["name"], crawler.explain())
    return {
        "character": character["name"],
        "work_title": work_title,
        "candidates": candidates,
        # 每个数据源到底怎么了（断网/被墙/限流/解析空），前端会显示出来
        "attempts": crawler.attempts,
        "explain": crawler.explain(),
    }


@router.get("/avatar")
async def avatar_proxy(url: str) -> Response:
    """把白名单域名下的图片代理回来（Bangumi/维基对热链不友好）。"""
    from agent.tools.avatar import download_avatar, is_allowed_image_url

    if not is_allowed_image_url(url):
        raise HTTPException(status_code=403, detail="图片域名不在允许列表内")
    try:
        data, suffix = await download_avatar(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"图片下载失败：{str(exc)[:120]}") from exc
    media = {
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(suffix, "image/jpeg")
    return Response(content=data, media_type=media, headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------------------
# 贴纸（Twemoji）
# ---------------------------------------------------------------------------
#: 本地缓存目录：第一次访问从 CDN 拉，之后直接读盘，断网也能用
#: Twemoji 文件名就是小写十六进制码点，多码点用 - 连。**必须校验**，否则能穿越目录


@router.post("/characters/{character_id}/merge")
async def merge_character(character_id: str, payload: MergeRequest) -> dict:
    """手动把某个角色并入另一个角色（补自动规则漏掉的情况）。"""
    try:
        result = await anyio.to_thread.run_sync(
            functools.partial(
                merge.merge_pair,
                character_id,
                payload.target_id,
                summarizer=memory_summarizer(),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**result, "member_count": len(result.get("members") or [])}


@router.post("/characters/{character_id}/unmerge")
async def unmerge_character(character_id: str) -> dict:
    """撤销合并：让角色重新独立，并取回自己那本书的记忆。"""
    try:
        result = await anyio.to_thread.run_sync(
            functools.partial(
                merge.unmerge, character_id, summarizer=memory_summarizer()
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**result, "member_count": len(result.get("members") or [])}
