"""Agent 行为审计日志（管理 Agent 与角色 Agent 共用，按书架隔离）。

为什么单独一层，而不是各处直接写 ``db.log_event``：

* **串起来**：一次抽取任务会写十几条事件，用同一个 ``trace_id`` 就能还原完整链路；
* **脱敏**：API Key、token、密码永远不许进日志表；
* **分区**：事件必须落到 ``shelf_id`` 上，同一书架的日志才是一条时间线，跨书架互不干扰；
* **不闯祸**：写日志失败绝不能影响业务 —— 这里全部吞掉并降级为普通 logging。

典型用法::

    from services import agent_log

    trace_id = agent_log.new_trace_id("extract")
    agent_log.log(
        "extraction_started",
        trace_id=trace_id,
        book_id=book_id,
        payload={"mode": mode},
    )
    ...
    agent_log.log("extraction_finished", trace_id=trace_id, book_id=book_id,
                  payload={"characters": len(characters)})

    agent_log.timeline(trace_id)        # 一次任务的完整链路
    agent_log.recent(shelf_id=shelf_id) # 某书架最近的事件
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from core import db

logger = logging.getLogger(__name__)

#: 这些键的值一律替换掉，绝不写入日志
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "deepseek_api_key",
        "authorization",
        "token",
        "session_secret",
        "password",
        "secret",
    }
)

_REDACTED = "***"


def new_trace_id(prefix: str = "trace") -> str:
    """生成一次任务的追踪 id，用于把多条事件串成一条链路。"""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                cleaned[key] = _REDACTED
            else:
                cleaned[key] = _redact(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def resolve_scope(*, book_id: Optional[str] = None, shelf_id: Optional[str] = None) -> dict:
    """把已知的 book/shelf 补全成 {shelf_id, library_id}，供需要显式传参的场合用。"""
    scope: dict[str, Optional[str]] = {"shelf_id": shelf_id, "library_id": None}
    if book_id and not scope["shelf_id"]:
        book = db.get_book(book_id)
        if book:
            scope["shelf_id"] = book.get("shelf_id")
    if scope["shelf_id"]:
        shelf = db.get_shelf(scope["shelf_id"])
        if shelf:
            scope["library_id"] = shelf.get("library_id")
    return scope


def log(
    event_type: str,
    *,
    owner_type: str = "system",
    owner_id: str = "",
    trace_id: str = "",
    book_id: Optional[str] = None,
    shelf_id: Optional[str] = None,
    library_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    severity: str = "info",
    payload: Optional[dict] = None,
) -> Optional[dict]:
    """写一条事件。**永不抛异常**：日志失败只降级成 logging，不影响业务。"""
    try:
        return db.log_event(
            event_type,
            owner_type=owner_type,
            owner_id=owner_id,
            trace_id=trace_id,
            book_id=book_id,
            shelf_id=shelf_id,
            library_id=library_id,
            user_id=user_id,
            session_id=session_id,
            severity=severity,
            payload=_redact(payload or {}),
        )
    except Exception:  # noqa: BLE001 - 审计日志不许拖垮主流程
        logger.exception("写 Agent 事件失败：%s（已忽略）", event_type)
        return None


def decision(message: str, **kwargs: Any) -> Optional[dict]:
    """记一条"管理 Agent 做了什么判断"，payload 里带理由。"""
    payload = dict(kwargs.pop("payload", {}) or {})
    payload.setdefault("reason", message)
    return log("decision", owner_type=kwargs.pop("owner_type", "manager"), payload=payload, **kwargs)


def tool_call(tool: str, *, arguments: Optional[dict] = None, **kwargs: Any) -> Optional[dict]:
    payload = {"tool": tool, "arguments": arguments or {}}
    return log(
        "tool_call",
        owner_type=kwargs.pop("owner_type", "manager"),
        payload=payload,
        **kwargs,
    )


def tool_result(
    tool: str,
    *,
    ok: bool = True,
    summary: Any = None,
    **kwargs: Any,
) -> Optional[dict]:
    payload = {"tool": tool, "ok": ok, "summary": summary}
    return log(
        "tool_result",
        owner_type=kwargs.pop("owner_type", "manager"),
        severity=kwargs.pop("severity", "info" if ok else "warn"),
        payload=payload,
        **kwargs,
    )


def timeline(trace_id: str, *, limit: int = 200) -> list[dict]:
    """按 trace_id 还原一次任务的完整链路（时间正序）。"""
    events = db.list_events(trace_id=trace_id, limit=limit)
    return list(reversed(events))


def recent(
    *,
    shelf_id: Optional[str] = None,
    book_id: Optional[str] = None,
    event_type: Optional[str] = None,
    severity: Optional[str] = None,
    owner_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """某书架 / 某本书最近的事件（时间倒序）。跨书架不可见。"""
    return db.list_events(
        shelf_id=shelf_id,
        book_id=book_id,
        event_type=event_type,
        severity=severity,
        owner_type=owner_type,
        limit=limit,
        offset=offset,
    )