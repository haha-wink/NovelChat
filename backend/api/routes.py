"""HTTP 接口层：作品（小说）/ 角色 / 会话 / 聊天。

流程：
  POST /api/books          上传小说（txt/md/pdf）-> 后台抽取角色
  GET  /api/books          书架列表（带状态与进度，前端轮询）
  GET  /api/books/{id}/characters   角色列表（小说抽取 + 用户自定义）
  POST /api/books/{id}/characters   新建角色（例如「我」）
  POST /api/session        服务端签发会话 token
  POST /api/chat           token + character_id -> 该角色 Agent 流式回复
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import AsyncIterator, List, Optional

import anyio
import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from agno.media import Image as AgnoImage

from core import db
from core.config import get_settings
from core.security import InvalidSessionError, Session, issue_session, verify_session
from readers.document import SUPPORTED_SUFFIXES, DocumentError, extract_text
from services import agent_log, affect, character_agent, expression, extractor, group_chat, library, merge, shelf
from services import agent_memory
from services import chat_memory, cleaner, rag, scene as scene_mod
from services import reply as reply_mod
from channels import event_to_outbound
from channels import registry as channel_registry
from channels.web import channel as web_channel
from services import summarizer as summarizer_module
from services.agent_manager import get_agent
from services.media import ImageValidationError, decode_images
from services.prompts import build_character_prompt

logger = logging.getLogger(__name__)

router = APIRouter()

#: Agno 内部事件里需要丢弃的片段（不是正文）
_SKIP_MARKERS = ("update_user_memory", "completed in")


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
class SessionRequest(BaseModel):
    display_name: str = ""


class SessionResponse(BaseModel):
    token: str
    user_id: str
    session_id: str
    display_name: str


class CharacterCreate(BaseModel):
    name: str
    role: str = ""
    personality: List[str] = []
    speech_style: str = ""
    background: str = ""
    avatar: str = ""
    is_user_persona: bool = False


class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    personality: Optional[List[str]] = None
    speech_style: Optional[str] = None
    background: Optional[str] = None
    avatar: Optional[str] = None
    #: 用户从头像候选里挑中的图片地址
    avatar_url: Optional[str] = None
    avatar_candidates: Optional[List[dict]] = None
    quotes: Optional[List[str]] = None
    excerpts: Optional[List[str]] = None
    prompt_override: Optional[str] = None
    is_user_persona: Optional[bool] = None


class AvatarRequest(BaseModel):
    #: 留空则用作品标题
    work_title: str = ""


class ChatRequest(BaseModel):
    token: Optional[str] = None
    character_id: Optional[str] = None
    #: 预留：将来支持多角色同场时使用
    character_ids: Optional[List[str]] = None
    prompt: str = ""
    images_base64: Optional[List[str]] = None
    #: 兼容字段：仅当 ALLOW_CLIENT_API_KEY=true 时生效
    api_key: Optional[str] = None


class MergeRequest(BaseModel):
    target_id: str


@router.post("/characters/{character_id}/merge")
async def merge_character(character_id: str, payload: MergeRequest) -> dict:
    """手动把某个角色并入另一个角色（补自动规则漏掉的情况）。"""
    try:
        result = await anyio.to_thread.run_sync(
            functools.partial(
                merge.merge_pair,
                character_id,
                payload.target_id,
                summarizer=_memory_summarizer(),
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
                merge.unmerge, character_id, summarizer=_memory_summarizer()
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**result, "member_count": len(result.get("members") or [])}


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
    await anyio.to_thread.run_sync(_require_character, character_id)
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
    character = await anyio.to_thread.run_sync(_require_character, character_id)
    book = await anyio.to_thread.run_sync(db.get_book, character["book_id"])
    shelf_id = str((book or {}).get("shelf_id") or "")
    if not shelf_id:
        raise HTTPException(status_code=400, detail="这个角色还没有归属书架")
    result = await anyio.to_thread.run_sync(
        functools.partial(
            chat_memory.distill,
            shelf_id,
            character_id,
            summarizer=_memory_summarizer(),
            character_name=character.get("name") or "",
        )
    )
    await anyio.to_thread.run_sync(rag.index_character, character_id)
    return result


class MemberUpdate(BaseModel):
    #: active（正常）/ muted（静默，仍可被 @ 唤醒）/ left（移出群）
    status: str


class LibraryCreate(BaseModel):
    title: str
    description: str = ""


class ShelfCreate(BaseModel):
    library_id: str
    title: str
    description: str = ""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _sse(payload: dict) -> str:
    """SSE 帧序列化。统一走 web 渠道，别处再接平台时格式只改一处。"""
    return web_channel.to_sse(payload)


def _extract_token(body_token: Optional[str], request: Request) -> str:
    if body_token and body_token.strip():
        return body_token.strip()
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def _resolve_session(body_token: Optional[str], request: Request) -> Session:
    try:
        return verify_session(_extract_token(body_token, request))
    except InvalidSessionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _conversation_session(session: Session, character_id: str) -> str:
    """把「用户 + 会话 + 角色」确定性地映射成一个 Agno session_id。

    这样同一个 token 下切换角色不会串记忆，刷新页面也还能接上同一段剧情。
    """
    raw = f"{session.user_id}:{session.session_id}:{character_id}".encode("utf-8")
    digest = hmac.new(get_settings().session_secret_bytes, raw, hashlib.sha256).hexdigest()
    return digest[:32]


def _should_skip(content: str) -> bool:
    return any(marker in content for marker in _SKIP_MARKERS)


def _memory_summarizer():
    """有 Key 用模型做记忆摘要；没有就返回 None，记忆层自动走本地压缩。"""
    settings = get_settings()
    return summarizer_module.make_llm_summarizer(
        settings.api_key_for(None), settings.deepseek_model
    )


def _book_payload(book: dict) -> dict:
    payload = dict(book)
    payload["character_count"] = len(db.list_characters(book["id"]))
    # 抽取诊断（哪些候选被丢弃、补了谁）解析成对象，方便前端/排查
    raw = payload.get("diagnostic")
    if raw:
        try:
            payload["diagnostic"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return payload


def _character_payload(character: dict, *, with_prompt: bool = False, user_name: str = "朋友") -> dict:
    payload = dict(character)
    if with_prompt:
        book = db.get_book(character["book_id"]) or {}
        payload["prompt_preview"] = build_character_prompt(
            character, book_title=book.get("title") or "未知作品", user_name=user_name
        )
    return payload


def _require_book(book_id: str) -> dict:
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="作品不存在")
    return book


def _require_character(character_id: str) -> dict:
    character = db.get_character(character_id)
    if not character:
        raise HTTPException(status_code=404, detail="角色不存在")
    return character


# ---------------------------------------------------------------------------
# 基础接口
# ---------------------------------------------------------------------------
@router.get("/health")
async def health() -> dict:
    from services import emotion_model, name_engine

    settings = get_settings()
    return {
        "status": "ok",
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
                shelf.add_book, shelf_id, book_id, summarizer=_memory_summarizer()
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
                shelf.remove_book, shelf_id, book_id, summarizer=_memory_summarizer()
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


class MergeResolve(BaseModel):
    left_id: str
    right_id: str
    same: bool = True


@router.post("/shelves/{shelf_id}/merge/resolve")
async def resolve_merge(shelf_id: str, payload: MergeResolve) -> dict:
    """用户拍板一对候选：``same=true`` 合并，``false`` 记成"不是同一人"。

    两种结果都会写进管理 Agent 记忆，下次不再拿这对来问。
    """
    if payload.left_id == payload.right_id:
        raise HTTPException(status_code=400, detail="不能和自己比较")

    left = await anyio.to_thread.run_sync(_require_character, payload.left_id)
    right = await anyio.to_thread.run_sync(_require_character, payload.right_id)

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
                summarizer=_memory_summarizer(),
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
                summarizer=_memory_summarizer(),
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


class ShelfUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None


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
    session = _resolve_session(token, request)
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
    session = _resolve_session(token, request)
    turns = await anyio.to_thread.run_sync(
        functools.partial(
            db.list_history, shelf_id, user_id=session.user_id, limit=limit
        )
    )
    return {"shelf_id": shelf_id, "turns": turns}


@router.delete("/characters/{character_id}/history")
async def clear_character_history(
    character_id: str, request: Request, token: str = ""
) -> dict:
    """清空我与这个角色的单聊历史（同样不动长期记忆）。"""
    character = await anyio.to_thread.run_sync(_require_character, character_id)
    session = _resolve_session(token, request)
    book = await anyio.to_thread.run_sync(db.get_book, character["book_id"]) or {}
    shelf_id = str(book.get("shelf_id") or "")
    removed = await anyio.to_thread.run_sync(
        functools.partial(
            db.clear_history,
            shelf_id,
            user_id=session.user_id,
            character_id=character_id,
        )
    )
    if removed:
        agent_log.log(
            "chat_history_cleared",
            owner_type="manager",
            shelf_id=shelf_id,
            owner_id=character_id,
            payload={
                "scope": "character",
                "removed": removed,
                "character": character.get("name") or "",
            },
        )
    return {"ok": True, "removed": removed}


@router.get("/characters/{character_id}/history")
async def character_history(
    character_id: str, request: Request, token: str = "", limit: int = 60
) -> dict:
    """单聊历史（当前用户与这个角色的最近 N 条）。"""
    character = await anyio.to_thread.run_sync(_require_character, character_id)
    session = _resolve_session(token, request)
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


@router.post("/shelves/{shelf_id}/chat")
async def shelf_chat(shelf_id: str, req: ChatRequest, request: Request):
    """群聊：一条消息 -> 若干角色按「发言意愿」接力说话（SSE 逐条推送）。

    成本红线：一轮最多 ``group_max_speakers`` 人、最多 ``group_max_rounds`` 轮，
    也就是单次最多 4 次模型调用（默认 2×2）。
    """
    settings = get_settings()
    session = _resolve_session(req.token, request)

    shelf = await anyio.to_thread.run_sync(db.get_shelf, shelf_id)
    if not shelf:
        raise HTTPException(status_code=404, detail="书架不存在")

    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="群聊目前只支持文字消息")
    if len(prompt) > settings.max_prompt_chars:
        raise HTTPException(
            status_code=413, detail=f"单次输入过长（上限 {settings.max_prompt_chars} 字）"
        )

    api_key = settings.api_key_for(req.api_key)
    if not api_key:
        raise HTTPException(
            status_code=503, detail="服务端未配置 DEEPSEEK_API_KEY"
        )

    async def generate(member: dict, transcript: list[dict]):
        """给 group_chat 用的生成器：构建角色 Agent -> 流式产出文本片段。"""
        character = await anyio.to_thread.run_sync(
            _require_character, member["character_id"]
        )
        book = (await anyio.to_thread.run_sync(db.get_book, character["book_id"])) or {}
        agent = await anyio.to_thread.run_sync(
            functools.partial(
                get_agent,
                api_key=api_key,
                character=character,
                book_title=book.get("title") or "",
                user_name=session.display_name,
            )
        )
        agent, _state = await anyio.to_thread.run_sync(
            functools.partial(
                character_agent.attach_state,
                agent,
                character,
                session.user_id,
                user_text=prompt,
            )
        )
        # 附加群聊上下文：后说话的人要接得上前面的话
        lines = [f"{t['speaker']}：{t['text']}" for t in transcript[-8:]]
        agent.additional_context = (
            (agent.additional_context or "")
            + "\n\n【群聊上下文，最近几条】\n"
            + "\n".join(lines)
            + f"\n\n你在群聊里，用「{member['name']}」的身份说话：**只说 1 句，最多 2 句短话**；"
            "不要写动作、神态、心理描写，不要复述别人说过的话。"
        )

        accumulated = ""
        async for chunk in agent.arun(
            transcript[0]["text"],
            user_id=session.user_id,
            session_id=_conversation_session(session, member["character_id"]),
            stream=True,
        ):
            content = getattr(chunk, "content", None) or ""
            if not content or _should_skip(content):
                continue
            if content.startswith(accumulated):
                delta = content[len(accumulated):]
                accumulated = content
            elif accumulated.endswith(content):
                delta = ""
            else:
                delta = content
                accumulated += content
            if delta:
                yield delta

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in group_chat.run_turn(
                shelf_id=shelf_id,
                user_id=session.user_id,
                text=prompt,
                generate=generate,
                session_id=f"group:{session.user_id}:{shelf_id}",
            ):
                # 引擎事件 -> 渠道无关的出站消息 -> web 载荷。
                # 这一圈看着绕，但换来的是：接 QQ/微信时引擎与这里一行都不用改。
                outbound = event_to_outbound(
                    event, channel=web_channel.name, conversation_id=shelf_id
                )
                yield web_channel.to_sse(web_channel.render(outbound))
                if await request.is_disconnected():
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("群聊出错（shelf=%s）", shelf_id)
            yield _sse({"type": "error", "message": f"群聊失败：{exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/books")
async def list_books(shelf_id: str = "") -> list[dict]:
    return await anyio.to_thread.run_sync(
        lambda: [_book_payload(b) for b in db.list_books(shelf_id or None)]
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

    return await anyio.to_thread.run_sync(functools.partial(_book_payload, book))


@router.get("/books/{book_id}")
async def get_book(book_id: str) -> dict:
    book = await anyio.to_thread.run_sync(_require_book, book_id)
    return await anyio.to_thread.run_sync(functools.partial(_book_payload, book))


@router.delete("/books/{book_id}")
async def delete_book(book_id: str) -> dict:
    deleted = await anyio.to_thread.run_sync(library.delete_book, book_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="作品不存在")
    return {"deleted": True}


@router.post("/books/{book_id}/retry")
async def retry_book(book_id: str) -> dict:
    settings = get_settings()
    book = await anyio.to_thread.run_sync(_require_book, book_id)
    if not settings.deepseek_api_key.strip():
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 DEEPSEEK_API_KEY，请在 backend/.env 中设置后重启后端",
        )
    if library.extraction_running(book_id):
        raise HTTPException(status_code=409, detail="这本书正在抽取中")

    library.retry_extraction(book_id)
    return await anyio.to_thread.run_sync(functools.partial(_book_payload, book))


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
@router.get("/books/{book_id}/characters")
async def list_characters(book_id: str) -> list[dict]:
    await anyio.to_thread.run_sync(_require_book, book_id)
    characters = await anyio.to_thread.run_sync(db.list_characters, book_id)
    return [_character_payload(c) for c in characters]


@router.post("/books/{book_id}/characters", status_code=201)
async def create_character(book_id: str, payload: CharacterCreate) -> dict:
    await anyio.to_thread.run_sync(_require_book, book_id)
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
    return _character_payload(character)


@router.get("/characters/{character_id}")
async def get_character(character_id: str, user_name: str = "朋友") -> dict:
    character = await anyio.to_thread.run_sync(_require_character, character_id)
    return await anyio.to_thread.run_sync(
        functools.partial(_character_payload, character, with_prompt=True, user_name=user_name)
    )


@router.get("/characters/{character_id}/status")
async def character_status(character_id: str, request: Request, token: str = "") -> dict:
    """角色详情 + 当前状态（点角色卡片看的就是这个）。

    **不含好感度/情绪数值**：用户只看到「什么心情」「对你什么态度」这类人话。
    数值留在库里和 affect_change 日志里，供我们自己调参。
    """
    character = await anyio.to_thread.run_sync(_require_character, character_id)
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
        session = _resolve_session(token, request)
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
    character = await anyio.to_thread.run_sync(_require_character, character_id)
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
    await anyio.to_thread.run_sync(_require_character, character_id)
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
    return _character_payload(character)


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
    from services.avatar_crawler import AvatarCrawler

    settings = get_settings()
    if not settings.agent_enable_avatars:
        raise HTTPException(
            status_code=400,
            detail="头像爬取已关闭（backend/.env 里 AGENT_ENABLE_AVATARS=false）",
        )

    character = await anyio.to_thread.run_sync(_require_character, character_id)
    book = await anyio.to_thread.run_sync(_require_book, character["book_id"])
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
    from services.avatar_crawler import download_avatar, is_allowed_image_url

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
STICKER_DIR = Path(__file__).resolve().parent.parent / "data" / "stickers"
TWEMOJI_CDN = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/svg/{name}.svg"
#: Twemoji 文件名就是小写十六进制码点，多码点用 - 连。**必须校验**，否则能穿越目录
_STICKER_NAME = re.compile(r"^[0-9a-f]{1,8}(-[0-9a-f]{1,8})*$")


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
@router.post("/books/{book_id}/verify")
async def verify_book_characters(book_id: str) -> dict:
    settings = get_settings()
    api_key = settings.deepseek_api_key.strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 DEEPSEEK_API_KEY，请在 backend/.env 中设置后重启后端",
        )

    book = await anyio.to_thread.run_sync(_require_book, book_id)
    text = await anyio.to_thread.run_sync(library.read_source, book_id)
    if not text:
        raise HTTPException(status_code=400, detail="这本书的原文丢失了，请重新上传")

    from services.extraction_agent import ExtractionAgent

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
@router.post("/chat")
async def chat_endpoint(req: ChatRequest, request: Request):
    settings = get_settings()
    session = _resolve_session(req.token, request)

    character_ids = [cid for cid in (req.character_ids or []) if cid] or (
        [req.character_id] if req.character_id else []
    )
    if not character_ids:
        raise HTTPException(status_code=400, detail="缺少 character_id：请先选择角色")
    if len(character_ids) > 1:
        raise HTTPException(status_code=400, detail="当前版本一次只能和一个角色对话")
    character_id = character_ids[0]

    prompt = (req.prompt or "").strip()
    if len(prompt) > settings.max_prompt_chars:
        raise HTTPException(
            status_code=413, detail=f"单次输入过长（上限 {settings.max_prompt_chars} 字）"
        )

    try:
        images = decode_images(
            req.images_base64,
            max_count=settings.max_images,
            max_image_bytes=settings.max_image_bytes,
            max_total_bytes=settings.max_total_image_bytes,
        )
    except ImageValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not prompt and not images:
        raise HTTPException(status_code=400, detail="内容为空：请输入文字或上传截图")

    api_key = settings.api_key_for(req.api_key)
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 DEEPSEEK_API_KEY，请在 backend/.env 中设置后重启后端",
        )

    character = await anyio.to_thread.run_sync(_require_character, character_id)
    book = await anyio.to_thread.run_sync(_require_book, character["book_id"])

    build = functools.partial(
        get_agent,
        api_key=api_key,
        character=character,
        book_title=book["title"],
        user_name=session.display_name,
    )
    try:
        agent = await anyio.to_thread.run_sync(build)
        # 注入本轮情绪状态 + RAG 检索到的相关记忆。
        # attach_state 返回的是 Agent 的**副本**，缓存里的共享实例不受影响（并发下不串状态）。
        agent, affect_state = await anyio.to_thread.run_sync(
            functools.partial(
                character_agent.attach_state,
                agent,
                character,
                session.user_id,
                user_text=prompt,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("构建角色 Agent 失败")
        raise HTTPException(status_code=500, detail=f"角色初始化失败：{exc}") from exc

    agno_session = _conversation_session(session, character_id)

    # 本轮用了什么情绪说话，记一笔便于复盘语气
    agent_log.log(
        "chat_turn_started",
        owner_type="character",
        owner_id=character_id,
        book_id=book["id"],
        user_id=session.user_id,
        session_id=agno_session,
        severity="debug",
        payload={
            "mood": affect_state.get("emotion"),
            "turns": affect_state.get("turns", 0),
            "has_images": bool(images),
        },
    )

    async def event_stream() -> AsyncIterator[str]:
        tmp_dir: Path | None = None
        try:
            agno_images = []
            if images:
                upload_root = settings.uploads_path
                upload_root.mkdir(parents=True, exist_ok=True)
                tmp_dir = upload_root / f"chat-{uuid.uuid4().hex}"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                for index, item in enumerate(images):
                    file_path = tmp_dir / f"image-{index}.{item.suffix}"
                    file_path.write_bytes(item.data)
                    agno_images.append(AgnoImage(filepath=str(file_path)))

            accumulated = ""
            async for chunk in agent.arun(
                prompt or "请看着这张截图，说说你的想法。",
                images=agno_images or None,
                user_id=session.user_id,
                session_id=agno_session,
                stream=True,
            ):
                if await request.is_disconnected():
                    logger.info("客户端断开，停止流（session=%s）", agno_session)
                    break

                content = getattr(chunk, "content", None) or ""
                if not content or _should_skip(content):
                    continue

                if content.startswith(accumulated):
                    delta = content[len(accumulated):]
                    accumulated = content
                elif accumulated.endswith(content):
                    delta = ""
                else:
                    delta = content
                    accumulated += content

                if delta:
                    yield _sse({"type": "delta", "text": delta})

            # 去掉动作/神态旁白（模型偶尔仍会写）。只影响最终文本——
            # 前端收到 final 后用干净文本覆盖刚才流式显示的版本。
            clean_reply = reply_mod.strip_stage_directions(accumulated)
            if clean_reply and clean_reply != accumulated.strip():
                accumulated = clean_reply
                yield _sse({"type": "final", "text": clean_reply})

            # 情绪与好感度推进：放在回复生成完成之后，不拖慢首字；失败不影响回复。
            # 数值不返回给前端（用户不看数字），只影响下一轮的语气。
            state_after: dict = {}
            try:
                state_after = await anyio.to_thread.run_sync(
                    functools.partial(
                        affect.observe_user_message,
                        character=character,
                        user_id=session.user_id,
                        text=prompt,
                        image_count=len(images),
                        session_id=agno_session,
                    )
                ) or {}
            except Exception:  # noqa: BLE001
                logger.exception("情绪更新失败（已忽略）")

            # 记一条对话流水：7 天窗口内保留原文，超期由 chat_memory 蒸馏成长期记忆。
            # 没有这条记录，角色就永远不记得"我们聊过什么"。
            try:
                shelf_id = str(book.get("shelf_id") or "")
                if shelf_id and accumulated.strip():
                    for role, text, speaker in (
                        ("user", prompt, session.display_name),
                        ("assistant", accumulated, character.get("name") or ""),
                    ):
                        await anyio.to_thread.run_sync(
                            functools.partial(
                                db.add_chat_turn,
                                shelf_id=shelf_id,
                                user_id=session.user_id,
                                role=role,
                                content=text,
                                character_id=character["id"],
                                session_id=agno_session,
                                speaker_name=speaker,
                            )
                        )
            except Exception:  # noqa: BLE001
                logger.exception("记录对话流水失败（已忽略）")

            # 条数过了阈值就顺手蒸馏一半（低于阈值时只有一次 COUNT，很便宜）
            try:
                shelf_id = str(book.get("shelf_id") or "")
                if shelf_id:
                    await anyio.to_thread.run_sync(
                        functools.partial(
                            chat_memory.maybe_distill,
                            shelf_id,
                            character["id"],
                            summarizer=_memory_summarizer(),
                            user_id=session.user_id,
                            character_name=character.get("name") or "",
                        )
                    )
            except Exception:  # noqa: BLE001
                logger.exception("对话蒸馏失败（已忽略）")

            # 贴纸：问起状态/心情、或者这一轮心情变了 -> 一定发；否则按情绪强度掷骰子。
            try:
                mood_changed = bool(
                    state_after.get("previous_emotion")
                    and state_after["previous_emotion"] != state_after.get("emotion")
                )
                sticker = expression.maybe_sticker(
                    state_after,
                    mood_changed=mood_changed,
                    asked_about_mood=expression.asks_about_mood(prompt),
                )
                if sticker:
                    yield _sse({"type": "sticker", **sticker})
            except Exception:  # noqa: BLE001
                logger.exception("贴纸决策失败（已忽略）")

            yield _sse({"type": "done"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("流式响应出错（session=%s）", agno_session)
            yield _sse({"type": "error", "message": f"生成失败：{exc}"})
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
