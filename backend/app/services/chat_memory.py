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


def _mood_digest(turns: list[dict]) -> str:
    """从这批流水里压出「情绪 + 意图」两行摘要。

    对话记忆如果只记"聊了什么"，角色记不住"你每次提到工作就会焦虑"。
    所以蒸馏时把情绪分布一起写进去，和事实一起向量化。
    """
    from collections import Counter

    intents = Counter(str(item.get("intent") or "") for item in turns)
    emotions = Counter(str(item.get("emotion") or "") for item in turns)
    intents.pop("", None)
    emotions.pop("", None)

    parts: list[str] = []
    if emotions:
        parts.append("情绪：" + "、".join(f"{name}×{count}" for name, count in emotions.most_common(3)))
    if intents:
        from services import dialogue

        labels = "、".join(
            f"{dialogue.INTENT_LABELS.get(name, name)}×{count}"
            for name, count in intents.most_common(3)
        )
        parts.append("意图：" + labels)
    return "；".join(parts)


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
    mood = _mood_digest(targets)
    if mood:
        text = f"{text}\n[本轮情绪] {mood}"

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


def clear_conversation(
    shelf_id: str,
    *,
    user_id: str,
    character_id: Optional[str] = None,
    summarizer=None,
    mode: str = "distill",
    conversation_id: Optional[str] = None,
) -> dict:
    """清空对话。两种语义：

    * ``distill``（默认）：先把这段对话压成长期记忆写进角色记忆，再删流水。
      这是"聊完一次就让它记住"的主路径——角色从此知道用户的喜好和状态。
    * ``purge``：连记忆一起删掉（连同这段对话蒸馏出的 fact），彻底重来。

    群聊时一次摘要、写给**所有说过话的角色**：LLM 只调一次，
    每个人都记得"我和用户聊过这些"。
    """
    from core.config import get_settings

    settings = get_settings()
    turns = pending_turns(shelf_id, user_id=user_id)
    if conversation_id:
        turns = [turn for turn in turns if str(turn.get("conversation_id") or "") == conversation_id]
    if character_id:
        turns = [turn for turn in turns if str(turn.get("character_id") or "") == character_id]
    if not turns:
        return {"ok": True, "mode": mode, "removed": 0, "distilled": 0,
                "reason": "没有可清空的对话"}

    # 谁说过话 -> 谁该记住
    speakers: list[str] = []
    for turn in turns:
        cid = str(turn.get("character_id") or "")
        if cid and cid not in speakers:
            speakers.append(cid)
    if character_id and character_id not in speakers:
        speakers.insert(0, character_id)

    result: dict = {"ok": True, "mode": mode, "turns": len(turns)}

    # ---- 1. 蒸馏（只调一次摘要模型）----
    if mode == "distill" and speakers:
        transcript = "\n".join(
            f"{turn.get('speaker_name') or ('我' if turn.get('role') == 'user' else '对方')}："
            f"{turn.get('content') or ''}"
            for turn in turns
        )[:6000]

        summary = None
        if summarizer is not None:
            try:
                summary = summarizer(transcript)
            except Exception:  # noqa: BLE001 - 摘要失败不能丢对话
                logger.exception("清空时蒸馏失败，回退本地压缩")

        text = (
            " ".join((summary or "").split())[:MAX_SUMMARY_CHARS]
            if summary else _local_summary(turns)
        )
        mood = _mood_digest(turns)
        if mood:
            text = f"{text}\n[本轮情绪] {mood}"

        for cid in speakers:
            try:
                agent_memory.remember(
                    cid,
                    [text],
                    kind="fact",
                    scene=DIALOGUE_SCENE,
                    importance=0.8,
                    reason=f"清空对话时蒸馏（{len(turns)} 条）",
                )
            except Exception:  # noqa: BLE001 - 单个角色写入失败不影响其他
                logger.exception("写入角色记忆失败：%s", cid)
        result["distilled"] = len(speakers)
        result["used_llm"] = bool(summary)
        result["memory"] = text[:120]

        for cid in speakers:
            try:
                from services import rag

                rag.index_character(cid)
            except Exception:  # noqa: BLE001
                logger.exception("蒸馏后重建索引失败：%s", cid)

    # ---- 2. purge：把这段对话留下的 fact 一并删掉 ----
    if mode == "purge":
        removed_memory = 0
        for cid in speakers:
            try:
                removed_memory += db.delete_memory_items(character_id=cid, kind="fact")
            except Exception:  # noqa: BLE001
                logger.exception("删除角色记忆失败：%s", cid)
        result["memory_removed"] = removed_memory
        for cid in speakers:
            try:
                from services import rag

                rag.index_character(cid)
            except Exception:  # noqa: BLE001
                logger.exception("删除记忆后重建索引失败：%s", cid)

    # ---- 3. 删流水（原文同时是聊天记录，清空就该消失）----
    db.mark_turns_distilled([turn["id"] for turn in turns])
    result["removed"] = db.delete_chat_turns([turn["id"] for turn in turns])

    agent_log.log(
        "chat_history_cleared",
        owner_type="manager",
        shelf_id=shelf_id,
        owner_id=character_id or "",
        payload={
            "scope": "character" if character_id else "shelf",
            "mode": mode,
            "turns": len(turns),
            "speakers": len(speakers),
            "memory_removed": result.get("memory_removed", 0),
        },
    )
    return result


def maybe_distill(
    shelf_id: str,
    character_id: str,
    *,
    summarizer=None,
    user_id: Optional[str] = None,
    character_name: str = "",
) -> Optional[dict]:
    """条数超阈值才蒸馏；默认阈值为 0（关闭）。

    现在记忆改由用户显式"清空对话"时蒸馏（``clear_conversation``），
    不再按固定条数自动触发——这样既能保证记忆完整，又不会每轮都多花一次摘要调用。
    """
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