"""书库与书架：书架的增删改、成员、历史、合并、经验记忆。"""

from fastapi import APIRouter

from api.v1.deps import *  # noqa: F401,F403

router = APIRouter()


@router.get("/libraries")
async def list_libraries() -> list[dict]:
    return await anyio.to_thread.run_sync(db.list_libraries)


@router.post("/libraries", status_code=201)
async def create_library(req: LibraryCreate) -> dict:
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="书库名称不能为空")
    return await anyio.to_thread.run_sync(
        functools.partial(db.create_library, title[:80], (req.description or "").strip()[:200])
    )


@router.get("/shelves")
async def list_shelves(library_id: str = "") -> list[dict]:
    return await anyio.to_thread.run_sync(
        functools.partial(db.list_shelves, library_id or None)
    )


@router.post("/shelves", status_code=201)
async def create_shelf(req: ShelfCreate) -> dict:
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="书架名称不能为空")
    try:
        return await anyio.to_thread.run_sync(
            functools.partial(
                db.create_shelf,
                req.library_id,
                title[:80],
                (req.description or "").strip()[:200],
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/shelves/{shelf_id}/logs")
async def shelf_logs(
    shelf_id: str,
    limit: int = 100,
    event_type: str = "",
    severity: str = "",
    owner_type: str = "",
    trace_id: str = "",
) -> dict:
    """书架级 Agent 行为日志。跨书架不可见。"""
    shelf = await anyio.to_thread.run_sync(db.get_shelf, shelf_id)
    if not shelf:
        raise HTTPException(status_code=404, detail="书架不存在")
    events = await anyio.to_thread.run_sync(
        functools.partial(
            db.list_events,
            shelf_id=shelf_id,
            trace_id=trace_id or None,
            event_type=event_type or None,
            severity=severity or None,
            owner_type=owner_type or None,
            limit=limit,
        )
    )
    return {"shelf": shelf, "count": len(events), "events": events}


@router.post("/shelves/{shelf_id}/books/{book_id}")
async def add_book_to_shelf(shelf_id: str, book_id: str) -> dict:
    """把书放进书架：角色入群 + 给书架里**原有角色**补初始记忆。"""
    try:
        result = await anyio.to_thread.run_sync(
            functools.partial(
                shelf.add_book, shelf_id, book_id, summarizer=memory_summarizer()
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**result, "member_count": len(result.get("members") or [])}


@router.delete("/shelves/{shelf_id}/books/{book_id}")
async def remove_book_from_shelf(shelf_id: str, book_id: str) -> dict:
    """把书从书架拿出去（= 删除这本书）：角色退群、初始记忆精确回收。"""
    try:
        result = await anyio.to_thread.run_sync(
            functools.partial(
                shelf.remove_book, shelf_id, book_id, summarizer=memory_summarizer()
            )
        )
    except ValueError as exc:
        code = 404 if "不存在" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return {**result, "member_count": len(result.get("members") or [])}


@router.get("/shelves/{shelf_id}/merge/candidates")
async def merge_candidates(shelf_id: str, floor: float = 0.5, limit: int = 10) -> dict:
    """「疑似同一个人」的候选对（只读，不合并）。

    界面拿它显示"待确认的合并"，用户点一下才真的合。
    """
    try:
        pairs = await anyio.to_thread.run_sync(
            functools.partial(merge.pending_pairs, shelf_id, floor=floor, limit=limit)
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    learned = await anyio.to_thread.run_sync(
        functools.partial(db.list_manager_memories, shelf_id, kind="same_person")
    )
    return {
        "shelf_id": shelf_id,
        "count": len(pairs),
        "pairs": pairs,
        "learned_count": len(learned),
    }


@router.post("/shelves/{shelf_id}/merge/resolve")
async def resolve_merge(shelf_id: str, payload: MergeResolve) -> dict:
    """用户拍板一对候选：``same=true`` 合并，``false`` 记成"不是同一人"。

    两种结果都会写进管理 Agent 记忆，下次不再拿这对来问。
    """
    if payload.left_id == payload.right_id:
        raise HTTPException(status_code=400, detail="不能和自己比较")

    left = await anyio.to_thread.run_sync(require_character, payload.left_id)
    right = await anyio.to_thread.run_sync(require_character, payload.right_id)

    async def shelf_of(character: dict) -> str:
        book = await anyio.to_thread.run_sync(db.get_book, character["book_id"]) or {}
        return str(book.get("shelf_id") or "")

    if await shelf_of(left) != shelf_id or await shelf_of(right) != shelf_id:
        raise HTTPException(status_code=400, detail="只能处理同一个书架里的角色")
    if left.get("is_merged") or right.get("is_merged"):
        raise HTTPException(status_code=400, detail="其中一个已经并入别人了")

    if not payload.same:
        memory = await anyio.to_thread.run_sync(
            functools.partial(
                merge.remember_different,
                shelf_id,
                [left.get("name") or "", right.get("name") or ""],
                source="manual",
                reason="用户确认不是同一人",
            )
        )
        return {
            "resolved": "different",
            "memory_id": (memory or {}).get("id"),
            "members": await anyio.to_thread.run_sync(db.list_shelf_members, shelf_id),
        }

    # 出场多的当本体
    child, target = (
        (left, right)
        if int(left.get("appearances") or 0) <= int(right.get("appearances") or 0)
        else (right, left)
    )
    try:
        result = await anyio.to_thread.run_sync(
            functools.partial(
                merge.merge_pair,
                child["id"],
                target["id"],
                summarizer=memory_summarizer(),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 注意：result 里自带 "merged"，直接把布尔塞进同名键会被覆盖，
    # 所以用 resolved 表达"用户选择了什么"。
    return {**result, "resolved": "merged"}


@router.post("/shelves/{shelf_id}/merge")
async def merge_shelf_characters(shelf_id: str, verify: bool = True) -> dict:
    """触发「同一个人合并成一个 Agent」。

    抽取完成时会自动跑一遍；这里给用户手动补跑（比如刚改过性格描述）。
    ``verify=true`` 且配了 Key 时，名字不同但性格/关系相似的候选会联网问一次模型。
    """
    settings = get_settings()
    verifier = (
        merge.llm_verifier(
            api_key=settings.deepseek_api_key.strip(),
            model=settings.extraction_model_name,
        )
        if verify and settings.deepseek_api_key.strip()
        else None
    )
    try:
        result = await anyio.to_thread.run_sync(
            functools.partial(
                merge.merge_shelf,
                shelf_id,
                summarizer=memory_summarizer(),
                verifier=verifier,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "groups": result["groups"],
        "merged": result["merged"],
        "summarized": result["summarized"],
        "auto_merged": result.get("auto_merged") or [],
        "verified": result.get("verified") or [],
        "rejected": result.get("rejected") or [],
        "pending": result.get("pending") or [],
        "learned": result.get("learned") or [],
        "member_count": len(result["members"]),
    }


@router.get("/shelves/{shelf_id}/memories")
async def list_shelf_memories(shelf_id: str, kind: str = "", limit: int = 200) -> dict:
    """管理 Agent 在这个书架攒下的经验（目前是"谁和谁是同一个人"）。"""
    memories = await anyio.to_thread.run_sync(
        functools.partial(
            db.list_manager_memories, shelf_id, kind=kind or None, limit=limit
        )
    )
    return {"shelf_id": shelf_id, "count": len(memories), "memories": memories}


@router.delete("/memories/{memory_id}")
async def delete_shelf_memory(memory_id: str) -> dict:
    """删掉一条经验（记错了可以反悔，下次会重新判定）。"""
    ok = await anyio.to_thread.run_sync(db.delete_manager_memory, memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"deleted": True, "id": memory_id}


@router.patch("/shelves/{shelf_id}")
async def update_shelf(shelf_id: str, payload: ShelfUpdate) -> dict:
    """重命名书架 / 群聊名（群聊名就是书架名）。"""
    try:
        updated = await anyio.to_thread.run_sync(
            functools.partial(
                db.update_shelf,
                shelf_id,
                title=payload.title,
                description=payload.description,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="书架不存在")
    return updated


@router.delete("/shelves/{shelf_id}")
async def delete_shelf(shelf_id: str, cascade: bool = True) -> dict:
    """删书架。

    ``cascade=true``（默认）会**连同书架上的书一起删**——角色、记忆、聊天记录
    也一并回收。这是用户在界面上二次确认后的操作，所以默认给足语义。
    ``cascade=false`` 时书架里还有书就拒绝（不静默丢数据）。
    """
    shelf_row = await anyio.to_thread.run_sync(db.get_shelf, shelf_id)
    if not shelf_row:
        raise HTTPException(status_code=404, detail="书架不存在")

    deleted_books = 0
    if cascade:
        books = await anyio.to_thread.run_sync(db.list_books, shelf_id)
        for book in books:
            ok = await anyio.to_thread.run_sync(library.delete_book, book["id"])
            if ok:
                deleted_books += 1

    try:
        ok = await anyio.to_thread.run_sync(db.delete_shelf, shelf_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": ok, "books_deleted": deleted_books, "shelf_id": shelf_id}


@router.delete("/shelves/{shelf_id}/history")
async def clear_shelf_history(shelf_id: str, request: Request, token: str = "") -> dict:
    """清空我在这个书架里的群聊历史。

    只删聊天流水，**不动长期记忆**——否则角色会"失忆"。
    """
    session = resolve_session(token, request)
    removed = await anyio.to_thread.run_sync(
        functools.partial(
            db.clear_history, shelf_id, user_id=session.user_id
        )
    )
    # 清空是破坏性操作，必须留痕；否则"聊天记录怎么没了"根本无从追查
    if removed:
        agent_log.log(
            "chat_history_cleared",
            owner_type="manager",
            shelf_id=shelf_id,
            payload={"scope": "shelf", "removed": removed},
        )
    return {"ok": True, "removed": removed}


@router.get("/shelves/{shelf_id}/history")
async def shelf_history(
    shelf_id: str, request: Request, token: str = "", limit: int = 60
) -> dict:
    """群聊历史（当前用户在这个书架里说过/听过的最近 N 条）。"""
    session = resolve_session(token, request)
    turns = await anyio.to_thread.run_sync(
        functools.partial(
            db.list_history, shelf_id, user_id=session.user_id, limit=limit
        )
    )
    return {"shelf_id": shelf_id, "turns": turns}


@router.get("/shelves/{shelf_id}/members")
async def shelf_members(shelf_id: str) -> dict:
    """群成员：书架里的角色 + 各自的发言状态与性格画像。"""
    shelf = await anyio.to_thread.run_sync(db.get_shelf, shelf_id)
    if not shelf:
        raise HTTPException(status_code=404, detail="书架不存在")
    members = await anyio.to_thread.run_sync(db.ensure_shelf_members, shelf_id)
    return {
        "shelf": shelf,
        "count": len(members),
        "members": [
            {
                "character_id": member["character_id"],
                "name": member["name"],
                "role": member.get("role") or "",
                "avatar": member.get("avatar") or "🎭",
                "avatar_url": member.get("avatar_url"),
                "status": member["status"],
                "state": member.get("state"),
                "turns": member.get("turns", 0),
                "last_spoke_at": member.get("last_spoke_at"),
                "profile": group_chat.profile_for(member),
            }
            for member in members
        ],
    }


@router.patch("/shelves/{shelf_id}/members/{character_id}")
async def update_shelf_member(
    shelf_id: str, character_id: str, payload: MemberUpdate
) -> dict:
    """切换成员在线状态：``online``（在线）/ ``offline``（离线）/ ``left``（移出群）。

    兼容旧值：``active`` -> online，``muted`` -> offline。
    离线成员收不到消息、也不会发言；``@全体成员`` 会把所有人叫上线。
    """
    try:
        normalized = db.normalize_member_status(payload.status)
        ok = await anyio.to_thread.run_sync(
            functools.partial(db.set_member_status, shelf_id, character_id, normalized)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="成员不存在")
    return {"ok": True, "character_id": character_id, "status": normalized}
