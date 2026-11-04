"""角色记忆：初始记忆条目 + 检索。

P1 只做**初始记忆**（来自小说原文的片段）。后续 P2 会在这层之上加「精炼摘要」，
所以这里的方法都按「条目」抽象，不假设内容一定是原文。

为什么要有这一层，而不是各处直接调 ``core.db``：

* **可回溯**：每条记忆都记 ``source_book_id``，删书时能精确回收，而不是整块清空；
* **可重建**：回收后按重要度重排 top-k，角色不会因为删了一本书就失忆；
* **派生缓存**：``characters.excerpts`` 由条目表同步生成，提示词/前端不用改。

典型用法::

    from services import agent_memory

    agent_memory.remember(character_id, ["原文片段…"], source_book_id=book_id)
    agent_memory.items(character_id)                    # 该角色全部初始记忆
    agent_memory.top(character_id, limit=3)             # 提示词用（重要度优先）
    agent_memory.forget_source(character_id, book_id)   # 删某本书的片段
    agent_memory.forget_book(book_id)                   # 删书：所有角色身上的该书片段
"""

from __future__ import annotations

import logging
from typing import Optional

from core import db

logger = logging.getLogger(__name__)

#: 提示词里默认带几段初始记忆
DEFAULT_TOP_K = 3


def items(
    character_id: str,
    *,
    kind: Optional[str] = None,
    source_book_id: Optional[str] = None,
) -> list[dict]:
    """该角色的记忆条目（重要度优先，其次按写入顺序）。"""
    return db.list_memory_items(
        character_id, kind=kind, source_book_id=source_book_id
    )


def top(character_id: str, *, limit: int = DEFAULT_TOP_K, kind: str = "excerpt") -> list[str]:
    """取提示词要用的前 N 条内容。"""
    return [
        row["content"]
        for row in db.list_memory_items(character_id, kind=kind, limit=limit)
    ]


def remember(
    character_id: str,
    excerpts: list[str],
    *,
    source_book_id: Optional[str] = None,
    importance: float = 0.5,
    reason: str = "",
    kind: str = "excerpt",
    scene: str = "",
) -> list[dict]:
    """追加记忆条目，并刷新 ``characters.excerpts`` 缓存（只对 excerpt 生效）。

    ``kind``：excerpt（原著片段）/ fact（事实、对话蒸馏）/ dialogue 等；
    ``scene``：场景标签，RAG 检索时同场景会加权。
    """
    if not scene:
        try:
            from services import scene as scene_mod

            scene = scene_mod.detect(" ".join(excerpts))
        except Exception:  # noqa: BLE001 - 场景识别失败不影响写入
            scene = ""
    created = db.add_memory_items(
        character_id,
        excerpts,
        source_book_id=source_book_id,
        kind=kind,
        importance=importance,
        added_reason=reason or "追加初始记忆",
        scene=scene,
    )
    if created:
        logger.info(
            "角色 %s 追加 %d 条记忆（kind=%s scene=%s 来源 %s）",
            character_id, len(created), kind, scene, source_book_id,
        )
        # 新记忆立刻进向量索引，下一轮检索就能捞到（失败只是退化成词法）
        try:
            from services import rag

            rag.index_character(character_id)
        except Exception:  # noqa: BLE001
            logger.exception("记忆向量索引失败（已忽略）")
    return created


def forget_source(character_id: str, source_book_id: str) -> int:
    """删掉某角色身上来自指定书的初始记忆，并重建缓存。"""
    removed = db.delete_memory_items(
        character_id=character_id, source_book_id=source_book_id, kind="excerpt"
    )
    if removed:
        logger.info("角色 %s 回收 %d 条来自《%s》的初始记忆", character_id, removed, source_book_id)
    return removed


def forget_book(book_id: str) -> int:
    """删书场景：把所有角色身上「来源是这本书」的初始记忆全部回收。

    返回回收总条数。调用方负责后续的群成员/关系处理（P5 书架事务）。
    """
    return db.delete_memory_items(source_book_id=book_id, kind="excerpt")


def rebuild(character_id: str, *, limit: Optional[int] = None, prune: bool = False) -> list[str]:
    """回收后重建该角色的初始记忆。

    条目表是唯一事实来源，所以「重建」= 按剩余条目重算 ``excerpts`` 缓存，
    再返回提示词用的前 N 条。**默认不删任何条目**——删书只回收那本书的片段，
    其余片段应原样保留。

    ``prune=True`` 时才按重要度截断到 ``limit`` 条（用于控制提示词体积）。
    """
    if prune:
        db.prune_memory_items(character_id, kind="excerpt", keep=limit or DEFAULT_TOP_K)
    else:
        db.sync_excerpts_cache(character_id)
    return top(character_id, limit=limit or DEFAULT_TOP_K)


def replace_source(
    character_id: str,
    source_book_id: str,
    excerpts: list[str],
    *,
    importance: float = 0.5,
) -> list[dict]:
    """用新的片段替换某本书给这个角色的初始记忆（重抽/角色档案更新时用）。"""
    db.delete_memory_items(
        character_id=character_id, source_book_id=source_book_id, kind="excerpt"
    )
    return remember(
        character_id,
        excerpts,
        source_book_id=source_book_id,
        importance=importance,
        reason="替换该来源的初始记忆",
    )


def stats(character_id: str) -> dict:
    """按来源统计初始记忆条数，便于排查「为什么这个角色像失忆了」。"""
    rows = items(character_id, kind="excerpt")
    by_source: dict[str, int] = {}
    for row in rows:
        key = row.get("source_book_id") or "(未知来源)"
        by_source[key] = by_source.get(key, 0) + 1
    return {"total": len(rows), "by_source": by_source}


# ---------------------------------------------------------------------------
# 记忆摘要（跨书合并后压成一段）
# ---------------------------------------------------------------------------
MAX_SUMMARY_INPUT_CHARS = 6000
MAX_SUMMARY_CHARS = 1200


def summary(character_id: str) -> str:
    """取该角色当前的记忆摘要（没有就返回空串）。"""
    rows = db.list_memory_items(character_id, kind="summary")
    return rows[0]["content"] if rows else ""


def _local_summary(items: list[dict]) -> str:
    """没有模型时的本地压缩：去重 + 每条截断 + 总长封顶。"""
    seen: set[str] = set()
    parts: list[str] = []
    for row in items:
        content = " ".join(str(row.get("content") or "").split())
        if not content:
            continue
        key = content[:40]
        if key in seen:
            continue
        seen.add(key)
        parts.append(content[:220])
        if sum(len(part) for part in parts) >= MAX_SUMMARY_CHARS:
            break
    return "；".join(parts)[:MAX_SUMMARY_CHARS]


def summarize(character_id: str, *, summarizer=None) -> str:
    """把该角色的初始记忆（可能来自多本书）压成一段摘要。

    **原始条目一条都不删**——摘要只是派生视图。删书时按 ``source_book_id`` 回收原始
    条目、再调一次本函数，就能"回档"到只剩另一本书的状态。如果摘要直接替换原文，
    这一步就永远做不回来了。

    ``summarizer`` 是可选的可调用对象（同步）：收一段长文本、返回摘要。
    不传就用本地压缩；它抛异常也会自动回退，不影响加书/删书。
    """
    items = db.list_memory_items(character_id, kind="excerpt")
    if not items:
        db.delete_memory_items(character_id=character_id, kind="summary")
        return ""

    # 必须把"要总结谁"写在最前面。片段里往往有好几个人物，不说清楚的话模型会去总结
    # 最显眼的那个——实测「陈雯雯」的摘要写成了路明非。
    character = db.get_character(character_id) or {}
    aliases = [a for a in (character.get("aliases") or []) if a]
    header = f"要总结的角色：{character.get('name') or '(未知)'}"
    if aliases:
        header += f"（别名：{'、'.join(aliases)}）"

    text = header + "\n\n原文片段：\n" + "\n".join(
        f"- {row['content']}" for row in items
    )
    text = text[:MAX_SUMMARY_INPUT_CHARS]
    result = None
    if summarizer is not None:
        try:
            result = summarizer(text)
        except Exception:  # noqa: BLE001 - 摘要失败不能拖垮加书
            logger.exception("记忆摘要失败，回退本地压缩")
    if not result:
        result = _local_summary(items)

    result = (result or "").strip()[:MAX_SUMMARY_CHARS]
    db.delete_memory_items(character_id=character_id, kind="summary")
    if result:
        db.add_memory_items(
            character_id,
            [result],
            kind="summary",
            importance=0.9,
            added_reason="初始记忆摘要（跨书合并）",
        )
    return result