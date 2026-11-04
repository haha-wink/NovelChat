"""会话签名。

原实现里 ``user_id`` / ``session_id`` 完全由浏览器提供，任何人都能
报出别人的 id 去读写对方的长期记忆。这里改成**服务端签发**：

* ``user_id`` 由显示名经 HMAC 派生 —— 同名 = 同一段长期记忆（保留原有体验），
  但没有密钥就无法猜出别人的 ``user_id``。
* ``session_id`` 每次签发都是新的，代表"一段新对话"。
* token 用 HMAC-SHA256 签名并带过期时间，客户端改不动内容。

注意：这解决的是"伪造/枚举 id"，不是真正的账号体系。
如果需要"同一名字必须证明是你"，还得加登录，属于产品决策。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass

from .config import get_settings

#: 显示名允许：中英文、数字、下划线、连字符、空格
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff \-]{0,31}$")
_DEFAULT_NAME = "匿名用户"


class InvalidSessionError(Exception):
    """token 缺失、被篡改或已过期。"""


@dataclass(frozen=True)
class Session:
    user_id: str
    session_id: str
    display_name: str
    token: str
    expires_at: int


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def normalize_display_name(raw: str | None) -> str:
    """清洗显示名；非法输入回落到默认名，不直接报错（避免前端体验中断）。"""
    name = (raw or "").strip()
    if not name:
        return _DEFAULT_NAME
    # 去掉控制字符 / 换行，压缩空白
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"\s+", " ", name)[:32].strip()
    if not name:
        return _DEFAULT_NAME
    if not _NAME_RE.match(name):
        # 允许其余可打印字符，但仍做长度截断，避免注入到 prompt/日志
        name = re.sub(r"[^\w\u4e00-\u9fff \-]", "", name)[:32].strip()
    return name or _DEFAULT_NAME


def derive_user_id(display_name: str) -> str:
    """显示名 -> 稳定且不可枚举的 user_id。"""
    key = get_settings().session_secret_bytes
    digest = hmac.new(key, display_name.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()
    return f"u_{digest[:16]}"


def _sign(raw: bytes) -> bytes:
    return hmac.new(get_settings().session_secret_bytes, raw, hashlib.sha256).digest()


def issue_session(display_name: str | None = None) -> Session:
    """签发一个新的会话 token。"""
    settings = get_settings()
    name = normalize_display_name(display_name)
    expires_at = int(time.time()) + max(60, settings.session_ttl_seconds)
    payload = {
        "u": derive_user_id(name),
        "s": uuid.uuid4().hex,
        "n": name,
        "e": expires_at,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    token = f"{_b64e(raw)}.{_b64e(_sign(raw))}"
    return Session(
        user_id=payload["u"],
        session_id=payload["s"],
        display_name=name,
        token=token,
        expires_at=expires_at,
    )


def verify_session(token: str | None) -> Session:
    """校验 token，失败抛 :class:`InvalidSessionError`。"""
    if not token or not isinstance(token, str):
        raise InvalidSessionError("缺少会话 token")
    parts = token.split(".")
    if len(parts) != 2:
        raise InvalidSessionError("会话 token 格式不正确")
    raw_b64, sig_b64 = parts
    try:
        raw = _b64d(raw_b64)
        signature = _b64d(sig_b64)
    except (ValueError, TypeError):
        raise InvalidSessionError("会话 token 无法解码") from None

    if not hmac.compare_digest(signature, _sign(raw)):
        raise InvalidSessionError("会话 token 签名校验失败")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidSessionError("会话 token 内容损坏") from None

    if not isinstance(payload, dict):
        raise InvalidSessionError("会话 token 内容损坏")

    expires_at = payload.get("e")
    user_id = payload.get("u")
    session_id = payload.get("s")
    if not isinstance(expires_at, int) or not isinstance(user_id, str) or not isinstance(session_id, str):
        raise InvalidSessionError("会话 token 字段缺失")
    if expires_at < int(time.time()):
        raise InvalidSessionError("会话已过期，请重新开始对话")
    if not user_id.startswith("u_") or not re.fullmatch(r"[0-9a-f]{16}", user_id[2:]):
        raise InvalidSessionError("会话 token 用户信息非法")
    if not re.fullmatch(r"[0-9a-f]{32}", session_id):
        raise InvalidSessionError("会话 token 会话信息非法")

    return Session(
        user_id=user_id,
        session_id=session_id,
        display_name=str(payload.get("n") or _DEFAULT_NAME),
        token=token,
        expires_at=expires_at,
    )
