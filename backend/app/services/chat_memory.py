"""短期对话 -> 长期记忆。

## 为什么要蒸馏

``chat_turns`` 是原始流水，越积越多，而且它不该原样进提示词——一来太长，二来
里面大段是寒暄。真正该留下来的是"我们之间发生过什么"。

```
chat_turns（7 天窗口，原始）
   │  超过窗口 / 超过条数阈值
   ▼
一条 kind='fact'、scene='对话' 的记忆条目  ->  进 RAG 库
   │
   ▼
原始流水删除（只留最近 7 天）
```

蒸馏出来的条目参与 RAG 检索，所以角色下次听到相关话题时会被捞出来——
这就是"记得我们聊过什么"。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from core import database as db
from services import agent_memory
from services import agent_log
from core import scene as scene_mod

logger = logging.getLogger(__name__)

#: 蒸馏出来的记忆统一打这个场景标签（区别于原著片段）
DIALOGUE_SCENE = scene_mod.DIALOGUE_SCENE

#: 本地压缩摘要最多多少字
MAX_SUMMARY_CHARS = 600


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _turns_ms(turn: dict) -> int:
    try:
        return int(str(turn.get("created_at") or "0").strip() or 0)
    except ValueError:
        return 0


def _local_summary(turns: list[dict]) -> str:
    """没有模型时的兜底：把用户说过的话去重后拼起来（话题比寒暄有信息量）。"""
    pieces: list[str] = []
    seen: set[str] = set()
    for turn in turns:
        if turn.get("role") != "user":
            continue
        text = " ".join(str(turn.get("content") or "").split())[:120]
        if not text or text in seen:
            continue
        seen.add(text)
        pieces.append(text)
        if sum(len(piece) for piece in pieces) >= MAX_SUMMARY_CHARS:
            break
    if not pieces:
        # 用户一句没说（不可能，但兜底）就退化成角色说过的话
        pieces = [
            " ".join(str(turn.get("content") or "").split())[:60]
            for turn in turns[:3]
        ]
    return "我们聊过：" + "；".join(pieces)[:MAX_SUMMARY_CHARS]


def pending_turns(shelf_id: str, *, user_id: Optional[str] = None, limit: int = 500) -> list[dict]:
    """该书架还没蒸馏的对话流水（按时间正序）。"""
    return db.list_chat_turns(shelf_id, user_id=user_id, limit=limit, ascending=True)


def distill(
    shelf_id: str,
    character_id: str,
    *,
    summarizer=None,
    keep_days: Optional[int] = None,
    keep_recent: Optional[int] = None,
    user_id: Optional[str] = None,
    character_name: str = "",
) -> dict:
    """把一段对话压成一条记忆，并把原文标记为"已蒸馏"。

    **不删原文**：聊天流水同时是用户重进群聊/单聊时看到的历史记录，
    删了会变成"退出再进来一片空白"。蒸馏只打标记，让它不再参与下一轮。

    * ``keep_days``：多久以内的原文保留（默认读配置）
    * ``keep_recent``：另外再保留最近 N 条不蒸馏（阈值触发时用）
    """
    from core.config import get_settings

    settings = get_settings()
    days = int(keep_days if keep_days is not None else settings.chat_retention_days)
    cutoff = _now_ms() - days * 24 * 3600 * 1000

    turns = pending_turns(shelf_id, user_id=user_id)
    if not turns:
        return {"distilled": 0, "kept": 0, "reason": "没有对话"}

    keep_recent = max(0, int(keep_recent or 0))
    newest_kept = {turn["id"] for turn in turns[-keep_recent:]} if keep_recent else set()
    targets = [
        turn for turn in turns
        if turn["id"] not in newest_kept and _turns_ms(turn) < cutoff
    ]
    # 条数阈值触发时，即使还没过期也要处理掉老的
    if not targets and keep_recent:
        targets = [turn for turn in turns if turn["id"] not in newest_kept]

    if not targets:
        return {"distilled": 0, "kept": len(turns), "reason": "都还在窗口内"}

    transcript = "\n".join(
        f"{turn.get('speaker_name') or ('我' if turn.get('role') == 'user' else '对方')}：{turn.get('content') or ''}"
        for turn in targets
    )[:6000]

    summary = None
    if summarizer is not None:
        try:
            summary = summarizer(transcript)
        except Exception:  # noqa: BLE001 - 摘要失败不能丢数据
            logger.exception("对话蒸馏摘要失败，回退本地压缩")
    text = " ".join((summary or "").split())[:MAX_SUMMARY_CHARS] if summary else _local_summary(targets)
    if character_name:
        text = f"（与{character_name}的对话）{text}"

    agent_memory.remember(
        character_id,
        [text],
        kind="fact",
        scene=DIALOGUE_SCENE,
        importance=0.7,
        reason=f"对话蒸馏（{len(targets)} 条）",
    )
    # 蒸馏后**保留原文**：它同时是用户重进群聊时看到的历史记录。
    # 只打标记，让它不再参与下一轮蒸馏。
    archived = db.mark_turns_distilled([turn["id"] for turn in targets])

    # 蒸馏出来的记忆要能立刻被检索到
    try:
        from services import rag

        rag.index_character(character_id)
    except Exception:  # noqa: BLE001
        logger.exception("蒸馏后建索引失败（已忽略）")

    agent_log.log(
        "chat_distilled",
        owner_type="manager",
        shelf_id=shelf_id,
        payload={
            "character_id": character_id,
            "turns": len(targets),
            "archived": archived,
            "used_llm": bool(summary),
        },
    )
    return {
        "distilled": len(targets),
        "archived": archived,
        "kept": len(turns) - len(targets),
        "summary": text,
        "used_llm": bool(summary),
    }


def maybe_distill(
    shelf_id: str,
    character_id: str,
    *,
    summarizer=None,
    user_id: Optional[str] = None,
    character_name: str = "",
) -> Optional[dict]:
    """条数超阈值才蒸馏；否则返回 None（每轮调用都很便宜）。"""
    from core.config import get_settings

    threshold = int(get_settings().chat_distill_threshold)
    if threshold <= 0:
        return None
    count = db.count_chat_turns(shelf_id, user_id=user_id, pending_only=True)
    if count <= threshold:
        return None
    # 保留一半原文，把另一半压掉，避免每轮都触发
    return distill(
        shelf_id,
        character_id,
        summarizer=summarizer,
        keep_recent=max(1, threshold // 2),
        user_id=user_id,
        character_name=character_name,
    )


def cleanup_window(
    shelf_id: str, *, character_id: str = "", summarizer=None, keep_days: Optional[int] = None
) -> dict:
    """只做过期清理：不蒸馏，直接删掉超出窗口的原文（有 character_id 时先蒸馏）。"""
    if character_id:
        return distill(shelf_id, character_id, summarizer=summarizer, keep_days=keep_days)
    from core.config import get_settings

    days = int(keep_days if keep_days is not None else get_settings().chat_retention_days)
    cutoff = _now_ms() - days * 24 * 3600 * 1000
    stale = [t["id"] for t in pending_turns(shelf_id) if _turns_ms(t) < cutoff]
    return {"distilled": 0, "removed": db.delete_chat_turns(stale)}