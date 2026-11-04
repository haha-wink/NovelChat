"""书架事务：书加进来 / 拿出去，群成员和角色记忆都要跟着动。

**一个书架 = 一套系统 = 一个群**。所有书的角色都进同一个群，所以书的进出必须做
成套处理，否则会留下"人在群里但书没了"或"角色对同系列的新书一无所知"。

## 加书

```
1. 书归到这个书架（从原书架移过来时，原书架同步退群）
2. 该书角色入群
3. **已有角色的初始记忆补强**（关键）
   在新书正文里找旧角色的名字/别名，命中就补一段原文片段，
   并且 source_book_id 记为新书 —— 同系列里"某人又出场了"，角色才不显得失忆
```

## 拿出去（= 删除这本书）

```
1. 删书（library.delete_book 会连带回收记忆条目、情绪状态、群成员关系）
2. 重新同步群成员：这本书的角色退群
```

第 3 步的记忆补强是 P1「记忆条目带来源」的直接收益：补进来的片段能按来源精确回收。
"""

from __future__ import annotations

import logging
from typing import Optional

from core import database as db
from services import agent_log
from services import agent_memory
from services import library
from services import merge

logger = logging.getLogger(__name__)

#: 补强片段在命中位置前后各取多少字
SNIPPET_WINDOW = 160

#: 每个旧角色最多补几段（控制提示词体积）
MAX_SNIPPETS_PER_CHARACTER = 1


def sync_members(shelf_id: str) -> list[dict]:
    """把书架里所有书的角色同步成群成员（幂等）。"""
    return db.ensure_shelf_members(shelf_id)


def _snippet_for(text: str, names: list[str]) -> Optional[str]:
    """在正文里找第一个命中的名字，截取它周围一小段。"""
    for name in names:
        name = str(name or "").strip()
        if not name:
            continue
        index = text.find(name)
        if index < 0:
            continue
        start = max(0, index - SNIPPET_WINDOW)
        end = min(len(text), index + len(name) + SNIPPET_WINDOW)
        snippet = text[start:end].strip()
        if snippet:
            return snippet
    return None


def supplement_memory(shelf_id: str, new_book_id: str) -> dict[str, int]:
    """用新书的正文，给书架里**已有角色**补初始记忆。

    返回 ``{character_id: 补了几段}``。只补真的在新书里出现过的角色——
    没出现就不补，避免把无关内容塞进角色记忆。
    """
    text = library.read_source(new_book_id)
    if not text:
        logger.info("新书 %s 没有正文，跳过记忆补强", new_book_id)
        return {}

    added: dict[str, int] = {}
    for book in db.list_books(shelf_id):
        if book["id"] == new_book_id:
            continue
        for character in db.list_characters(book["id"]):
            if character.get("is_user_persona"):
                continue
            names = [character.get("name") or ""] + list(character.get("aliases") or [])
            snippet = _snippet_for(text, names)
            if not snippet:
                continue
            already = agent_memory.items(character["id"], source_book_id=new_book_id)
            if len(already) >= MAX_SNIPPETS_PER_CHARACTER:
                continue
            agent_memory.remember(
                character["id"],
                [snippet],
                source_book_id=new_book_id,
                reason=f"同书架加入《{db.get_book(new_book_id).get('title', '')}》时补强",
            )
            added[character["id"]] = 1
    return added


def add_book(shelf_id: str, book_id: str, *, summarizer=None) -> dict:
    """把一本书放到书架里（可从别的书架移过来）。

    顺序：补记忆 → **合并同名角色** → 重算摘要 → 同步群成员。
    """
    shelf = db.get_shelf(shelf_id)
    if not shelf:
        raise ValueError("书架不存在")
    book = db.get_book(book_id)
    if not book:
        raise ValueError("作品不存在")

    previous = book.get("shelf_id")
    if previous == shelf_id:
        # 已经在这个书架里：仍跑一遍合并，补上可能的重复角色
        merged = merge.merge_shelf(shelf_id, summarizer=summarizer)
        return {
            "moved": False,
            "reason": "已经在这个书架里",
            "shelf_id": shelf_id,
            "members": merged["members"],
            "supplemented": {},
            "merged": merged["merged"],
        }

    db.set_book_shelf(book_id, shelf_id)
    if previous:
        sync_members(previous)  # 原书架：这本书的角色退群

    sync_members(shelf_id)
    supplemented = supplement_memory(shelf_id, book_id)
    # 同系列里会出现"同一个人"（例如两本都有路明非）——合并成一个 Agent
    merged = merge.merge_shelf(shelf_id, summarizer=summarizer)

    agent_log.log(
        "shelf_book_added",
        owner_type="manager",
        book_id=book_id,
        shelf_id=shelf_id,
        payload={
            "title": book.get("title") or "",
            "from_shelf": previous,
            "members": len(merged["members"]),
            "supplemented": len(supplemented),
            "merged": len(merged["merged"]),
        },
    )
    return {
        "moved": True,
        "from_shelf": previous,
        "shelf_id": shelf_id,
        "members": merged["members"],
        "supplemented": supplemented,
        "merged": merged["merged"],
    }


def remove_book(shelf_id: str, book_id: str, *, summarizer=None) -> dict:
    """把一本书从书架里拿出去 —— 等同删除这本书，并**回档**。

    顺序很重要：
    1. 先把寄居在本书角色身上的其它书角色解放出来（否则它们会指向即将消失的本体）
    2. 删书（连带回收记忆条目、情绪状态、成员关系）
    3. 重算书架里所有角色的记忆摘要 —— 删掉的那本书贡献的片段不在里面了
    """
    book = db.get_book(book_id)
    if not book:
        raise ValueError("作品不存在")
    if book.get("shelf_id") != shelf_id:
        raise ValueError("这本书不在该书架里")

    released = db.release_merged_children(book_id)

    agent_log.log(
        "shelf_book_removed",
        owner_type="manager",
        book_id=book_id,
        shelf_id=shelf_id,
        payload={"title": book.get("title") or "", "released_merged": released},
    )
    deleted = library.delete_book(book_id)
    summaries = merge.refresh_summaries(shelf_id, summarizer=summarizer)
    members = db.ensure_shelf_members(shelf_id)
    return {
        "deleted": deleted,
        "shelf_id": shelf_id,
        "members": members,
        "released_merged": released,
        "summaries": summaries,
    }