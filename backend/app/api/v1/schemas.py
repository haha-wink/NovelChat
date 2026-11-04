"""请求 / 响应体（Pydantic）。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field  # noqa: F401

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


class MergeResolve(BaseModel):
    left_id: str
    right_id: str
    same: bool = True


class ShelfUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
