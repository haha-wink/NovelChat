"""工作记忆：工作模式里「这个对话当前在做什么」的临时笔记。

它和长期记忆的区别：

```
chat_turns（流水）       → 对话历史，用户能翻到
working_memory（本文件） → 当前任务的约束/进度/试过的方案，只活在这个对话里
character_memory_items  → 长期记忆，RAG 检索，跨对话、跨会话
```

为什么单独一层：

* **只记工作模式**：聊天模式写工作记忆会和角色记忆抢上下文，两边都变脏；
* **不进 RAG**：任务笔记（"要 720p、副歌、赛博朋克"）不该在下次聊天时被当成往事捞出来；
* **清空对话时蒸馏**：``clear_conversation`` 把工作记忆和流水一起压成一条长期记忆，
  这样"我上次让它做 MV 时要求过什么"才会留下。

工作记忆按对话隔离；切换聊天/工作模式时前端会新建对话，天然不会互相污染。
"""

from __future__ import annotations

import logging
from typing import Optional

from core import database as db

logger = logging.getLogger(__name__)

#: 一个对话最多留多少条工作记忆（再多说明任务该拆了）
MAX_ITEMS = 60

#: 单条上限（和 db.add_working_memory 的截断保持一致）
MAX_CHARS = 2000


def remember(
    conversation_id: str,
    content: str,
    *,
    shelf_id: str = "",
    user_id: str = "",
    kind: str = "note",
    mode: str = "work",
    agent_id: str = "",
) -> Optional[dict]:
    """记一条工作记忆。聊天模式直接不写（返回 None）。

    ``agent_id`` 用来分栏：``admin`` 是管理员，``character:<id>`` 是某个角色，
    空字符串是用户要求 / 任务这类公共笔记。"""
    if str(mode or "").strip().lower() != "work":
        return None
    if not conversation_id:
        return None
    text = " ".join(str(content or "").split())[:MAX_CHARS]
    if not text:
        return None
    created = db.add_working_memory(
        conversation_id,
        text,
        shelf_id=shelf_id,
        user_id=user_id,
        mode="work",
        kind=kind,
        agent_id=agent_id,
    )
    if created:
        _prune(conversation_id)
    return created


def _prune(conversation_id: str) -> None:
    """超过上限就删最老的：工作记忆是草稿纸，不是档案。"""
    try:
        rows = db.list_working_memory(conversation_id, limit=MAX_ITEMS + 20)
        if len(rows) <= MAX_ITEMS:
            return
        for row in rows[: len(rows) - MAX_ITEMS]:
            db.delete_working_memory(row["id"])
    except Exception:  # noqa: BLE001 - 修剪失败不影响主流程
        logger.exception("工作记忆修剪失败（已忽略）")


def items(conversation_id: str, *, limit: int = 50, agent_id: Optional[str] = None) -> list[dict]:
    return db.list_working_memory(conversation_id, limit=limit, agent_id=agent_id)


def digest(
    conversation_id: str, *, limit: int = 40, agent_id: Optional[str] = None
) -> str:
    """拼成一段文本：清空对话/蒸馏时并入长期记忆。"""
    return db.working_memory_text(conversation_id, limit=limit, agent_id=agent_id)


def admin_digest(conversation_id: str, *, limit: int = 30) -> str:
    """管理员视角：本轮计划 + 各角色汇报 + 任务，拼给管理员决策用。"""
    return db.working_memory_text(conversation_id, limit=limit, agent_id="admin")


def clear(conversation_id: str) -> int:
    return db.clear_working_memory(conversation_id)


def stats(conversation_id: str) -> dict:
    rows = items(conversation_id)
    return {"count": len(rows), "chars": sum(len(row.get("content") or "") for row in rows)}