"""时间与问候：本地时间、时间问候、劳累问候。

## 两个工具（注册在 core/tool_protocol，由 LangGraph 层封装）

| 工具 | 作用 |
|---|---|
| `local_time` | 拿本地时间 + 时段 + 建议的问候语 |
| `greeting_check` | 结合**时间 + 对话频率**判断："现在该不该打招呼/劝人休息" |

## 判断规则

**时间问候**：看"这个时段是不是第一次说话"。
把一天切成若干时段（早上/上午/中午/下午/傍晚/晚上/深夜），
如果上一条用户消息落在**不同**时段（或今天还没说过话），就该打一声招呼。

**劳累问候**（两个条件满足任一）：

* 现在是深夜（≥23:00 或 <05:00）
* 最近一小时消息 ≥ 20 条，且这一轮已经聊了 ≥ 30 分钟

频率数据从 `chat_turns` 现算，不需要额外表。

## 为什么用"上一句的时段"而不是定时器

定时器（每天 8 点发早安）在聊天里很怪：用户下午才来，收到一句"早上好"。
按时段差判断，用户什么时候来都能接上话，也不会同一个时段反复问候。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from core import database as db
from core import tool_protocol

logger = logging.getLogger(__name__)

#: 时段划分（左闭右开）：(起始, 结束, 名称, 问候语)
PERIODS: tuple[tuple[int, int, str, str], ...] = (
    (5, 9, "早上", "早。"),
    (9, 11, "上午", "上午好。"),
    (11, 14, "中午", "中午了，吃饭了吗？"),
    (14, 18, "下午", "下午好。"),
    (18, 23, "傍晚", "傍晚了。"),
    (23, 24, "晚上", "晚上好。"),
    (0, 5, "深夜", "还没睡？"),
)

#: 深夜起止（跨零点）
LATE_NIGHT_START = 23
LATE_NIGHT_END = 5

#: 劳累问候的触发条件
FATIGUE_MESSAGES_PER_HOUR = 20
FATIGUE_SESSION_MINUTES = 30


def period_of(hour: int) -> tuple[str, str]:
    """小时 -> (时段名, 问候语)。左闭右开，跨零点的深夜单独一段。"""
    hour = int(hour) % 24
    for start, end, label, text in PERIODS:
        if start <= hour < end:
            return label, text
    return "深夜", "还没睡？"


def is_late_night(hour: int) -> bool:
    return hour >= LATE_NIGHT_START or hour < LATE_NIGHT_END


def time_context(now: Optional[datetime] = None) -> dict:
    """本地时间 + 时段。"""
    moment = _as_aware(now) if now is not None else datetime.now().astimezone()
    period, greeting = period_of(moment.hour)
    weekday = "一二三四五六日"[moment.weekday()]
    return {
        "iso": moment.isoformat(timespec="seconds"),
        "hour": moment.hour,
        "minute": moment.minute,
        "weekday": f"周{weekday}",
        "period": period,
        "greeting": greeting,
        "late_night": is_late_night(moment.hour),
    }


def _as_aware(moment: datetime) -> datetime:
    """naive 当成"本地时间"补上时区，避免 naive/aware 相减炸掉。"""
    if moment.tzinfo is None:
        return moment.astimezone()
    return moment


def _minutes_since(stamp: Optional[str], now: datetime) -> Optional[float]:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return (_as_aware(now) - _as_aware(moment)).total_seconds() / 60


def frequency(shelf_id: str, *, now: Optional[datetime] = None) -> dict:
    """从对话流水里算频率：最近一小时多少条、这一轮聊了多久。"""
    moment = _as_aware(now) if now is not None else datetime.now().astimezone()
    try:
        turns = db.list_chat_turns(shelf_id, limit=60)
    except Exception:  # noqa: BLE001 - 频率算不出来就不问候
        logger.exception("读取对话频率失败（已忽略）")
        return {"messages_last_hour": 0, "session_minutes": 0.0, "last_user_at": None}

    recent: list[datetime] = []
    last_user_at: Optional[str] = None
    # 倒序遍历：要的是**最新**那条用户消息（之前正序取到的是最旧的，
    # 结果每次都判成"跨时段"，问候语每轮都发一遍）
    for turn in reversed(turns):
        stamp = turn.get("created_at")
        minutes = _minutes_since(stamp, moment)
        if minutes is None:
            continue
        if minutes <= 60:
            recent.append(moment - timedelta(minutes=minutes))
        if turn.get("role") == "user" and last_user_at is None:
            last_user_at = stamp

    session_minutes = 0.0
    if recent:
        session_minutes = round((moment - min(recent)).total_seconds() / 60, 1)

    return {
        "messages_last_hour": len(recent),
        "session_minutes": session_minutes,
        "last_user_at": last_user_at,
    }


def decide(
    *, now: Optional[datetime] = None, frequency_data: Optional[dict] = None
) -> dict:
    """核心判断：现在要不要发时间问候 / 劳累问候。

    ``frequency_data`` 至少包含 ``messages_last_hour`` / ``session_minutes`` /
    ``last_user_at``；不传就当作"没有历史"。
    """
    moment = _as_aware(now) if now is not None else datetime.now().astimezone()
    context = time_context(moment)
    freq = frequency_data or {}
    hour = context["hour"]
    period = context["period"]

    # ---- 时间问候：跨时段才发 ----
    last_user_at = freq.get("last_user_at")
    last_period = None
    if last_user_at:
        try:
            last_moment = _as_aware(datetime.fromisoformat(str(last_user_at)))
            last_period = period_of(last_moment.astimezone().hour)[0]
        except (TypeError, ValueError):
            last_period = None

    need_time = last_period != period
    # 同一时段里隔了很久（>3 小时）也补一句，不然接话太突兀
    gap_minutes = _minutes_since(last_user_at, moment) if last_user_at else None
    if not need_time and gap_minutes is not None and gap_minutes >= 180:
        need_time = True

    # ---- 劳累问候 ----
    late = is_late_night(hour)
    busy = (
        int(freq.get("messages_last_hour") or 0) >= FATIGUE_MESSAGES_PER_HOUR
        and float(freq.get("session_minutes") or 0) >= FATIGUE_SESSION_MINUTES
    )
    need_fatigue = late or busy

    parts: list[str] = []
    if need_time:
        parts.append(context["greeting"])
    if need_fatigue:
        if late:
            parts.append("这么晚了还没睡，明天还撑得住吗？")
        else:
            parts.append("聊了挺久了，眼睛不累吗？要不要歇会儿。")

    return {
        "period": period,
        "hour": hour,
        "late_night": late,
        "need_time_greeting": need_time,
        "need_fatigue_greeting": need_fatigue,
        "text": "".join(parts),
        "gap_minutes": round(gap_minutes, 1) if gap_minutes is not None else None,
        "messages_last_hour": int(freq.get("messages_last_hour") or 0),
        "session_minutes": float(freq.get("session_minutes") or 0),
    }


# ---------------------------------------------------------------------------
# 注册为工具（第一层协议）
# ---------------------------------------------------------------------------


def _local_time(args: dict) -> dict:
    context = time_context()
    return {
        "text": context["greeting"],
        **context,
    }


def _greeting_check(args: dict) -> dict:
    shelf_id = str(args.get("shelf_id") or "")
    data = frequency(shelf_id) if shelf_id else {}
    result = decide(frequency_data=data)
    if not result["text"]:
        result["text"] = ""
    return result


tool_protocol.register(
    tool_protocol.ToolSpec(
        name="local_time",
        description="获取当前本地时间、时段与建议的问候语",
        parameters={},
        handler=_local_time,
        domain="time",
    )
)
tool_protocol.register(
    tool_protocol.ToolSpec(
        name="greeting_check",
        description="结合当前时间与最近对话频率，判断是否需要发时间问候或劳累问候",
        parameters={
            "shelf_id": {"type": "string", "description": "书架 id，用来算对话频率"},
        },
        handler=_greeting_check,
        required=("shelf_id",),
        domain="time",
    )
)


def describe() -> dict:
    return {
        "periods": [name for _, name, _ in PERIODS],
        "fatigue_rule": {
            "messages_per_hour": FATIGUE_MESSAGES_PER_HOUR,
            "session_minutes": FATIGUE_SESSION_MINUTES,
            "late_night": f">= {LATE_NIGHT_START}:00 或 < {LATE_NIGHT_END}:00",
        },
    }