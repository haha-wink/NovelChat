"""接口层共享依赖：导入、会话解析、payload 组装、SSE 编码。

各 endpoints 模块用 ``from api.v1.deps import *`` 一次拿到这些公共能力，
避免每个文件重复十几行 import（也避免循环导入）。
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
from services import agent_log
from services import affect
from agent import character as character_agent
from services import expression
from services import extractor
from agent import group as group_chat
from services import library
from services import merge
from services import shelf
from services import agent_memory
from services import chat_memory
from services import cleaner
from services import rag
from services import scene as scene_mod
from services import reply as reply_mod
from channels import event_to_outbound
from channels import registry as channel_registry
from channels.web import channel as web_channel
from services import summarizer as summarizer_module
from agent.manager import get_agent
from services.media import ImageValidationError, decode_images
from agent.prompts import build_character_prompt

logger = logging.getLogger(__name__)


#: Agno 内部事件里需要丢弃的片段（不是正文）
_SKIP_MARKERS = ("update_user_memory", "completed in")



from api.v1.schemas import *  # noqa: F401,F403
from core.paths import STICKER_DIR  # noqa: E402

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def sse(payload: dict) -> str:
    """SSE 帧序列化。统一走 web 渠道，别处再接平台时格式只改一处。"""
    return web_channel.to_sse(payload)


def extract_token(body_token: Optional[str], request: Request) -> str:
    if body_token and body_token.strip():
        return body_token.strip()
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def resolve_session(body_token: Optional[str], request: Request) -> Session:
    try:
        return verify_session(extract_token(body_token, request))
    except InvalidSessionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def conversation_session(session: Session, character_id: str) -> str:
    """把「用户 + 会话 + 角色」确定性地映射成一个 Agno session_id。

    这样同一个 token 下切换角色不会串记忆，刷新页面也还能接上同一段剧情。
    """
    raw = f"{session.user_id}:{session.session_id}:{character_id}".encode("utf-8")
    digest = hmac.new(get_settings().session_secret_bytes, raw, hashlib.sha256).hexdigest()
    return digest[:32]


def should_skip(content: str) -> bool:
    return any(marker in content for marker in _SKIP_MARKERS)


def memory_summarizer():
    """有 Key 用模型做记忆摘要；没有就返回 None，记忆层自动走本地压缩。"""
    settings = get_settings()
    return summarizer_module.make_llm_summarizer(
        settings.api_key_for(None), settings.deepseek_model
    )


def book_payload(book: dict) -> dict:
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


def character_payload(character: dict, *, with_prompt: bool = False, user_name: str = "朋友") -> dict:
    payload = dict(character)
    if with_prompt:
        book = db.get_book(character["book_id"]) or {}
        payload["prompt_preview"] = build_character_prompt(
            character, book_title=book.get("title") or "未知作品", user_name=user_name
        )
    return payload


def require_book(book_id: str) -> dict:
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="作品不存在")
    return book


def require_character(character_id: str) -> dict:
    character = db.get_character(character_id)
    if not character:
        raise HTTPException(status_code=404, detail="角色不存在")
    return character


