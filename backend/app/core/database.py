"""轻量数据层：SQLite + JSON 列（作品 / 角色）。

不引入 ORM：表小、查询简单，直接 sqlite3 更透明，也不用装新依赖。
每次操作独立连接 + WAL，后台抽取任务与请求线程可以并发读写。
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from core.config import get_settings
from core.text import tokenize as _tokenize
from core import scene as scene_mod

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    source_name TEXT,
    source_type TEXT,
    char_count  INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    progress    INTEGER NOT NULL DEFAULT 0,
    stage       TEXT,
    error       TEXT,
    diagnostic  TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS characters (
    id              TEXT PRIMARY KEY,
    book_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    aliases         TEXT NOT NULL DEFAULT '[]',
    role            TEXT NOT NULL DEFAULT '',
    personality     TEXT NOT NULL DEFAULT '[]',
    speech_style    TEXT NOT NULL DEFAULT '',
    background      TEXT NOT NULL DEFAULT '',
    relationships   TEXT NOT NULL DEFAULT '[]',
    quotes          TEXT NOT NULL DEFAULT '[]',
    excerpts        TEXT NOT NULL DEFAULT '[]',
    avatar          TEXT NOT NULL DEFAULT '🎭',
    avatar_url      TEXT,
    avatar_candidates TEXT NOT NULL DEFAULT '[]',
    source          TEXT NOT NULL DEFAULT 'novel',
    is_user_persona INTEGER NOT NULL DEFAULT 0,
    prompt_override TEXT,
    appearances     INTEGER NOT NULL DEFAULT 0,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    canonical_id    TEXT,                               -- 非空表示已并入这个"本体"角色
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_characters_book ON characters(book_id);

-- ---------------------------------------------------------------- 书库 / 书架
-- 三级结构：library（书库，最大）-> shelf（书架，一套独立系统）-> book（书）
-- 书架是隔离边界：记忆、情绪、群聊、管理 Agent 经验、日志都按书架分区。
CREATE TABLE IF NOT EXISTS libraries (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shelves (
    id          TEXT PRIMARY KEY,
    library_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shelves_library ON shelves(library_id);

-- ---------------------------------------------------------------- Agent 审计日志
-- 管理与角色两种 Agent 共用的行为日志。按 shelf_id 隔离：同一书架的日志才是一条
-- 时间线，跨书架互不干扰；trace_id 用于把一次任务内的多条事件串起来。
CREATE TABLE IF NOT EXISTS agent_events (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    trace_id    TEXT NOT NULL DEFAULT '',
    owner_type  TEXT NOT NULL DEFAULT 'system',   -- manager | character | system
    owner_id    TEXT NOT NULL DEFAULT '',         -- 角色 id 或空
    library_id  TEXT,
    shelf_id    TEXT,
    book_id     TEXT,
    user_id     TEXT,
    session_id  TEXT,
    event_type  TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',     -- debug | info | warn | error
    payload     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_shelf ON agent_events(shelf_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_trace ON agent_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_events_book  ON agent_events(book_id, ts);

-- ---------------------------------------------------------------- 初始记忆条目
-- 角色的初始记忆（小说原文片段）按「条目」存储，每条带来源书。
-- 为什么要这样：书架里删掉一本书时，必须能精确回收「只来自那本书」的片段，
-- 再重建其余片段——单个 JSON 数组做不到这件事。
-- characters.excerpts 列保留为**派生缓存**（由本表同步生成），老代码/提示词不用改。
CREATE TABLE IF NOT EXISTS character_memory_items (
    id             TEXT PRIMARY KEY,
    character_id   TEXT NOT NULL,
    origin_character_id TEXT,                             -- 这段记忆本来属于谁（合并/撤销用）
    source_book_id TEXT,                                  -- 删书时按此回收
    kind           TEXT NOT NULL DEFAULT 'excerpt',       -- excerpt | summary | fact | dialogue
    scene          TEXT NOT NULL DEFAULT '',              -- 场景标签（RAG 检索加权用）
    tokens         TEXT NOT NULL DEFAULT '',              -- 切好的检索 token（空格分隔）
    vector         BLOB,                                  -- 向量（float32，RAG 语义检索用）
    content        TEXT NOT NULL,
    importance     REAL NOT NULL DEFAULT 0.5,
    added_reason   TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memitems_char   ON character_memory_items(character_id, kind);
CREATE INDEX IF NOT EXISTS idx_memitems_source ON character_memory_items(source_book_id);

-- 块级向量：一条长记忆会被切成多个 ~220 字的块，每块一个向量。
-- 整段编码的话，1200 字的片段只截得到开头，中段信息全丢——检索质量主要卡在这。
CREATE TABLE IF NOT EXISTS memory_chunks (
    id           TEXT PRIMARY KEY,
    item_id      TEXT NOT NULL,
    character_id TEXT NOT NULL,
    idx          INTEGER NOT NULL DEFAULT 0,
    text         TEXT NOT NULL,
    vector       BLOB,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_char ON memory_chunks(character_id);
CREATE INDEX IF NOT EXISTS idx_chunks_item ON memory_chunks(item_id);

-- 管理 Agent 的经验记忆（按书架）。目前只用来记「谁和谁是同一个人」：
--   kind='same_person'      确认过是同一个人 → 下次直接合并，不再重复判断
--   kind='different_person' 确认过不是同一个人 → 下次直接跳过，不再重复询问
-- 设计文档 §7.3 说的「书架级经验」就落在这里。
CREATE TABLE IF NOT EXISTS manager_memories (
    id          TEXT PRIMARY KEY,
    shelf_id    TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'same_person',
    content     TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    tokens      TEXT NOT NULL DEFAULT '',
    hits        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mm_shelf ON manager_memories(shelf_id, kind);

-- ---------------------------------------------------------------- 情绪 / 好感度
-- 双层结构：
--   情绪 emotion       快变量：valence(-1~1) / arousal(0~1) + 离散标签，每轮向 baseline 回落
--   好感度 favorability 慢变量：0~100，只在"事件"上小步变化，每轮最多 ±5
-- 作用域是「角色 × 用户 × 书架」：换书架等于换世界观，好感度不串。
-- 注意：这些数值**不给用户看**，只用来决定角色怎么说话（见 services/affect.py）。
CREATE TABLE IF NOT EXISTS character_affect (
    character_id      TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    shelf_id          TEXT NOT NULL DEFAULT '',
    favorability      INTEGER NOT NULL DEFAULT 40,
    valence           REAL NOT NULL DEFAULT 0.0,
    arousal           REAL NOT NULL DEFAULT 0.35,
    emotion           TEXT NOT NULL DEFAULT '平静',
    baseline          TEXT NOT NULL DEFAULT '{}',
    turns             INTEGER NOT NULL DEFAULT 0,
    evidence_event_id TEXT,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (character_id, user_id, shelf_id)
);

-- ---------------------------------------------------------------- 群聊
-- 一个书架 = 一个群。成员来自书架里的书（用户自建角色不入群：那是"你自己"）。
-- group_member_state 记录谁在潜水、谁刚说过话（状态机见 services/group_chat.py）。
CREATE TABLE IF NOT EXISTS shelf_members (
    shelf_id       TEXT NOT NULL,
    character_id   TEXT NOT NULL,
    source_book_id TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active',   -- active | muted | left
    joined_at      TEXT NOT NULL,
    left_at        TEXT,
    PRIMARY KEY (shelf_id, character_id)
);

CREATE TABLE IF NOT EXISTS group_member_state (
    shelf_id         TEXT NOT NULL,
    character_id     TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'idle',   -- idle | listening | speaking | cooldown | muted
    last_spoke_at    TEXT,
    extraversion     REAL NOT NULL DEFAULT 0.5,
    assertiveness    REAL NOT NULL DEFAULT 0.5,
    initiative_floor REAL NOT NULL DEFAULT 0.2,
    turns            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (shelf_id, character_id)
);

-- 发言流水（群聊与单聊共用；短期记忆/摘要的原料）
CREATE TABLE IF NOT EXISTS chat_turns (
    id           TEXT PRIMARY KEY,
    shelf_id     TEXT NOT NULL DEFAULT '',
    character_id TEXT,
    user_id      TEXT NOT NULL DEFAULT '',
    session_id   TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL,                    -- user | assistant | system
    speaker_name TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL DEFAULT '',
    images       TEXT NOT NULL DEFAULT '[]',
    stickers     TEXT NOT NULL DEFAULT '[]',
    mentions     TEXT NOT NULL DEFAULT '[]',
    intent       TEXT NOT NULL DEFAULT '',           -- 用户这句的意图（倾诉/求助/闲聊/危机）
    emotion      TEXT NOT NULL DEFAULT '',           -- 用户这句的情绪标签
    round_no     INTEGER NOT NULL DEFAULT 0,
    distilled    INTEGER NOT NULL DEFAULT 0,        -- 已蒸馏成长期记忆（**仍保留**，供历史回看）
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_shelf ON chat_turns(shelf_id, created_at);
"""

#: 老库迁移时自动创建的书库 / 书架名称
DEFAULT_LIBRARY_TITLE = "我的书库"
DEFAULT_SHELF_TITLE = "默认书架"

#: agent_events.payload 里单个字符串的上限，避免把整篇原文写进日志
MAX_EVENT_PAYLOAD_CHARS = 4000

#: 初始记忆条目的类型
MEMORY_KINDS = ("excerpt", "summary", "fact")

JSON_FIELDS = ("aliases", "personality", "relationships", "quotes", "excerpts", "avatar_candidates")

#: 允许通过 update_character 修改的字段
EDITABLE_CHARACTER_FIELDS = (
    "name",
    "aliases",
    "role",
    "personality",
    "speech_style",
    "background",
    "relationships",
    "quotes",
    "excerpts",
    "avatar",
    "avatar_url",
    "avatar_candidates",
    "prompt_override",
    "is_user_persona",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ms() -> str:
    """事件表用毫秒精度：同一秒内可能连写多条，秒精度无法排序。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@contextmanager
def connect():
    settings = get_settings()
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.db_path, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_default_shelf(conn: sqlite3.Connection) -> str:
    """保证至少有一个书库和一个书架，并把没归架的书放进默认书架。

    v1 的库只有 books / characters，升级到「书库 -> 书架 -> 书」三级结构时靠这段
    完成回填。幂等：重复调用不会重复建库、建架，也不会覆盖已有的 shelf_id。
    """
    row = conn.execute("SELECT id FROM libraries ORDER BY created_at LIMIT 1").fetchone()
    if row is None:
        library_id = new_id("lib")
        conn.execute(
            "INSERT INTO libraries (id, title, description, created_at) VALUES (?, ?, '', ?)",
            (library_id, DEFAULT_LIBRARY_TITLE, _now()),
        )
    else:
        library_id = row["id"]

    row = conn.execute(
        "SELECT id FROM shelves WHERE library_id = ? ORDER BY created_at LIMIT 1",
        (library_id,),
    ).fetchone()
    if row is None:
        shelf_id = new_id("shelf")
        conn.execute(
            "INSERT INTO shelves (id, library_id, title, description, created_at) "
            "VALUES (?, ?, ?, '', ?)",
            (shelf_id, library_id, DEFAULT_SHELF_TITLE, _now()),
        )
    else:
        shelf_id = row["id"]

    conn.execute("UPDATE books SET shelf_id = ? WHERE shelf_id IS NULL", (shelf_id,))
    return shelf_id


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # 老库补列（SCHEMA 用的是 CREATE TABLE IF NOT EXISTS，不会自动加列）
        book_columns = {row["name"] for row in conn.execute("PRAGMA table_info(books)")}
        if "diagnostic" not in book_columns:
            conn.execute("ALTER TABLE books ADD COLUMN diagnostic TEXT")
        if "shelf_id" not in book_columns:
            conn.execute("ALTER TABLE books ADD COLUMN shelf_id TEXT")

        character_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(characters)")
        }
        if "avatar_url" not in character_columns:
            conn.execute("ALTER TABLE characters ADD COLUMN avatar_url TEXT")
        if "avatar_candidates" not in character_columns:
            conn.execute(
                "ALTER TABLE characters ADD COLUMN avatar_candidates TEXT NOT NULL DEFAULT '[]'"
            )
        if "canonical_id" not in character_columns:
            # 同系列多本书会抽出同一个人；canonical_id 让重复角色"并入本体"而不是删掉
            conn.execute("ALTER TABLE characters ADD COLUMN canonical_id TEXT")

        memory_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(character_memory_items)").fetchall()
        }
        if "origin_character_id" not in memory_columns:
            # 撤销合并时要知道"这段记忆本来属于谁"。老数据按当前归属回填即可——
            # 迁移前没有发生过合并，当前归属就是原始归属。
            conn.execute(
                "ALTER TABLE character_memory_items ADD COLUMN origin_character_id TEXT"
            )
            conn.execute(
                "UPDATE character_memory_items SET origin_character_id = character_id "
                "WHERE origin_character_id IS NULL"
            )
        turn_columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_turns)")}
        if "intent" not in turn_columns:
            conn.execute("ALTER TABLE chat_turns ADD COLUMN intent TEXT NOT NULL DEFAULT ''")
        if "emotion" not in turn_columns:
            conn.execute("ALTER TABLE chat_turns ADD COLUMN emotion TEXT NOT NULL DEFAULT ''")

        if "scene" not in memory_columns:
            conn.execute(
                "ALTER TABLE character_memory_items ADD COLUMN scene TEXT NOT NULL DEFAULT ''"
            )
        if "tokens" not in memory_columns:
            conn.execute(
                "ALTER TABLE character_memory_items ADD COLUMN tokens TEXT NOT NULL DEFAULT ''"
            )
            # 老数据的 token 回填：RAG 检索靠它，没 token 等于检索不到
            _backfill_memory_tokens(conn)
        else:
            # 新库/新条目已经有 token，但老条目可能缺，补一次（幂等）
            _backfill_memory_tokens(conn)
        if "vector" not in memory_columns:
            # 向量由 services/rag.py 异步补齐；缺了只是检索退化成词法
            conn.execute("ALTER TABLE character_memory_items ADD COLUMN vector BLOB")
        _backfill_memory_scene(conn)

        # 成员状态改成"在线/离线"两态：老数据 active -> online，muted -> offline
        conn.execute("UPDATE shelf_members SET status = 'online' WHERE status = 'active'")
        conn.execute("UPDATE shelf_members SET status = 'offline' WHERE status = 'muted'")

        # 发言流水加"已蒸馏"标记：蒸馏只打标记不删行，否则重进群聊看不到历史
        turn_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(chat_turns)").fetchall()
        }
        if "distilled" not in turn_columns:
            conn.execute(
                "ALTER TABLE chat_turns ADD COLUMN distilled INTEGER NOT NULL DEFAULT 0"
            )

        _ensure_default_shelf(conn)
        _migrate_excerpts_to_memory_items(conn)


def _loads(raw: Any, default: Any = None) -> Any:
    if raw in (None, ""):
        return default if default is not None else []
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default if default is not None else []


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 作品
# ---------------------------------------------------------------------------
def _row_to_book(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["char_count"] = int(data.get("char_count") or 0)
    data["chunk_count"] = int(data.get("chunk_count") or 0)
    data["progress"] = int(data.get("progress") or 0)
    return data


def create_book(
    *,
    title: str,
    source_name: str,
    source_type: str,
    char_count: int,
    chunk_count: int,
    shelf_id: Optional[str] = None,
) -> dict:
    book_id = new_id("book")
    with connect() as conn:
        if shelf_id:
            exists = conn.execute("SELECT 1 FROM shelves WHERE id = ?", (shelf_id,)).fetchone()
            if exists is None:
                raise ValueError(f"书架不存在：{shelf_id}")
        else:
            shelf_id = _ensure_default_shelf(conn)
        conn.execute(
            """
            INSERT INTO books (id, shelf_id, title, source_name, source_type, char_count,
                               chunk_count, status, progress, stage, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'extracting', 0, '排队中', NULL, ?)
            """,
            (
                book_id,
                shelf_id,
                title,
                source_name,
                source_type,
                char_count,
                chunk_count,
                _now(),
            ),
        )
    return get_book(book_id)  # type: ignore[return-value]


def get_book(book_id: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return _row_to_book(row) if row else None


def list_books(shelf_id: Optional[str] = None) -> list[dict]:
    with connect() as conn:
        if shelf_id:
            rows = conn.execute(
                "SELECT * FROM books WHERE shelf_id = ? ORDER BY created_at DESC",
                (shelf_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM books ORDER BY created_at DESC").fetchall()
        counts = {
            r["book_id"]: r["n"]
            for r in conn.execute(
                "SELECT book_id, COUNT(*) AS n FROM characters "
                "WHERE canonical_id IS NULL GROUP BY book_id"
            ).fetchall()
        }
    books = []
    for row in rows:
        book = _row_to_book(row)
        book["character_count"] = int(counts.get(book["id"], 0))
        books.append(book)
    return books


def update_book(book_id: str, **fields: Any) -> None:
    allowed = {
        "title", "status", "progress", "stage", "error", "chunk_count",
        "char_count", "source_type", "source_name", "diagnostic",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with connect() as conn:
        conn.execute(f"UPDATE books SET {assignments} WHERE id = ?", (*updates.values(), book_id))


def delete_book(book_id: str) -> bool:
    """删除作品：连带其角色、这些角色的记忆条目，以及**其他角色身上来自该书的片段**。

    最后一条是书架场景的关键：同系列里删掉一本书后，别的角色不该还留着
    只来自那本书的初始记忆。
    """
    with connect() as conn:
        # 0) 角色没了，就不该还留在群里
        for table in ("shelf_members", "group_member_state"):
            conn.execute(
                f"DELETE FROM {table} WHERE character_id IN "
                "(SELECT id FROM characters WHERE book_id = ?)",
                (book_id,),
            )
        # 0.5) 情绪状态：角色没了，情绪不该留成孤儿行
        conn.execute(
            "DELETE FROM character_affect WHERE character_id IN "
            "(SELECT id FROM characters WHERE book_id = ?)",
            (book_id,),
        )
        # 1) 该书角色自己的全部记忆条目
        conn.execute(
            "DELETE FROM character_memory_items WHERE character_id IN "
            "(SELECT id FROM characters WHERE book_id = ?)",
            (book_id,),
        )
        conn.execute(
            "DELETE FROM memory_chunks WHERE character_id IN "
            "(SELECT id FROM characters WHERE book_id = ?)",
            (book_id,),
        )
        # 2) 其他角色身上「来源是这本书」的片段：删完要重建它们的 excerpts 缓存
        affected = [
            row["character_id"]
            for row in conn.execute(
                "SELECT DISTINCT character_id FROM character_memory_items "
                "WHERE source_book_id = ?",
                (book_id,),
            ).fetchall()
        ]
        conn.execute(
            "DELETE FROM character_memory_items WHERE source_book_id = ?", (book_id,)
        )
        cursor = conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        deleted = cursor.rowcount > 0
        conn.execute("DELETE FROM characters WHERE book_id = ?", (book_id,))
        for character_id in affected:
            _sync_excerpts_from_items(conn, character_id)
    return deleted


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
def _row_to_character(row: sqlite3.Row) -> dict:
    data = dict(row)
    for field in JSON_FIELDS:
        data[field] = _loads(data.get(field))
    data["is_user_persona"] = bool(data.get("is_user_persona"))
    data["appearances"] = int(data.get("appearances") or 0)
    data["sort_order"] = int(data.get("sort_order") or 0)
    #: 非空表示这个角色已经并入本体（同系列书里的同一个人），不再单独出现
    data["is_merged"] = bool(data.get("canonical_id"))
    return data


def list_characters(book_id: str, *, include_merged: bool = False) -> list[dict]:
    """一本书的角色。默认**不含**已并入本体的重复角色。"""
    merged_filter = "" if include_merged else "AND canonical_id IS NULL"
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM characters WHERE book_id = ? {merged_filter}
            ORDER BY is_user_persona DESC, sort_order ASC, appearances DESC, name ASC
            """,
            (book_id,),
        ).fetchall()
    return [_row_to_character(row) for row in rows]


def set_canonical(character_id: str, canonical_id: Optional[str]) -> bool:
    """把角色标记为"已并入本体"（传 None 表示取消合并）。"""
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE characters SET canonical_id = ?, updated_at = ? WHERE id = ?",
            (canonical_id, _now(), character_id),
        )
    return cursor.rowcount > 0


def restore_memory_items(character_id: str, from_character_id: str, book_id: str) -> int:
    """把 ``from_character_id`` 身上原本属于 ``character_id`` 的记忆条目迁回去。

撤销合并 / 删本体所在书时用。**按原始归属判断，而不是按来源书**——
同一个人和它的重复项可能来自同一本书，只看 source_book_id 会把对方的记忆一起搬走。
"""
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE character_memory_items SET character_id = ? "
            "WHERE character_id = ? AND ("
            "  origin_character_id = ? "
            "  OR (origin_character_id IS NULL AND source_book_id = ?)"
            ")",
            (character_id, from_character_id, character_id, book_id),
        )
        _sync_excerpts_from_items(conn, character_id)
        _sync_excerpts_from_items(conn, from_character_id)
    return cursor.rowcount


def list_merged_children(character_id: str) -> list[dict]:
    """哪些角色已经并入了这个本体。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE canonical_id = ? ORDER BY name ASC",
            (character_id,),
        ).fetchall()
    return [_row_to_character(row) for row in rows]


def release_merged_children(book_id: str) -> int:
    """删书前把"寄居在本书角色身上"的其它书角色解放出来。

    场景：B 书的「路明非」并入了 A 书的「路明非」（A 是系列第一本）。现在删 A——
    如果不先处理，B 的路明非会指向一个不存在的本体而永久消失，它的记忆也会跟着
    A 的角色被一起删掉。

    这里做两件事：把属于孩子自己那本书的记忆迁回去、清掉合并标记。
    于是删完 A 之后，B 的路明非回到"独立角色"的状态——这就是"回档"。
    """
    with connect() as conn:
        canonicals = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM characters WHERE book_id = ?", (book_id,)
            ).fetchall()
        ]
        if not canonicals:
            return 0

        placeholders = ",".join("?" for _ in canonicals)
        children = conn.execute(
            f"SELECT id, book_id, canonical_id FROM characters "
            f"WHERE canonical_id IN ({placeholders})",
            tuple(canonicals),
        ).fetchall()

        released = 0
        for child in children:
            conn.execute(
                "UPDATE character_memory_items SET character_id = ? "
                "WHERE character_id = ? AND ("
                "  origin_character_id = ? "
                "  OR (origin_character_id IS NULL AND source_book_id = ?)"
                ")",
                (child["id"], child["canonical_id"], child["id"], child["book_id"]),
            )
            conn.execute(
                "UPDATE characters SET canonical_id = NULL, updated_at = ? WHERE id = ?",
                (_now(), child["id"]),
            )
            _sync_excerpts_from_items(conn, child["id"])
            released += 1

        for canonical_id in canonicals:
            _sync_excerpts_from_items(conn, canonical_id)
    return released


def transfer_memory_items(from_character_id: str, to_character_id: str) -> int:
    """把 A 的记忆条目整体迁到 B（``source_book_id`` 保持不变）。

    **来源必须留着**：删书时要靠它精确回档，否则摘要一合并就再也拆不开了。
    """
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE character_memory_items SET character_id = ?, "
            "origin_character_id = COALESCE(origin_character_id, character_id) "
            "WHERE character_id = ?",
            (to_character_id, from_character_id),
        )
        _sync_excerpts_from_items(conn, to_character_id)
        _sync_excerpts_from_items(conn, from_character_id)
    return cursor.rowcount


def get_character(character_id: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
    return _row_to_character(row) if row else None


def insert_character(*, book_id: str, **fields: Any) -> dict:
    character_id = new_id("char")
    now = _now()
    data = {field: fields.get(field) for field in EDITABLE_CHARACTER_FIELDS}
    values = {
        "id": character_id,
        "book_id": book_id,
        "name": data["name"],
        "aliases": _dumps(data["aliases"] or []),
        "role": data["role"] or "",
        "personality": _dumps(data["personality"] or []),
        "speech_style": data["speech_style"] or "",
        "background": data["background"] or "",
        "relationships": _dumps(data["relationships"] or []),
        "quotes": _dumps(data["quotes"] or []),
        "excerpts": _dumps(data["excerpts"] or []),
        "avatar": data["avatar"] or "🎭",
        "source": fields.get("source") or "novel",
        "is_user_persona": 1 if data["is_user_persona"] else 0,
        "prompt_override": data["prompt_override"],
        "appearances": int(fields.get("appearances") or 0),
        "sort_order": int(fields.get("sort_order") or 0),
        "created_at": now,
        "updated_at": now,
    }
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    with connect() as conn:
        conn.execute(f"INSERT INTO characters ({columns}) VALUES ({placeholders})", tuple(values.values()))
        # 初始记忆以条目表为准；characters.excerpts 由它同步生成
        _seed_memory_items(
            conn,
            character_id=character_id,
            book_id=book_id,
            excerpts=data["excerpts"] or [],
        )
    return get_character(character_id)  # type: ignore[return-value]


def update_character(character_id: str, **fields: Any) -> Optional[dict]:
    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in EDITABLE_CHARACTER_FIELDS:
            continue
        if key in JSON_FIELDS:
            updates[key] = _dumps(value or [])
        elif key == "is_user_persona":
            updates[key] = 1 if value else 0
        else:
            updates[key] = value
    if not updates:
        return get_character(character_id)

    updates["updated_at"] = _now()
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with connect() as conn:
        conn.execute(f"UPDATE characters SET {assignments} WHERE id = ?", (*updates.values(), character_id))
    return get_character(character_id)


def delete_character(character_id: str) -> bool:
    with connect() as conn:
        # 连带清掉记忆条目、情绪状态、群成员关系，避免孤儿行
        conn.execute(
            "DELETE FROM character_memory_items WHERE character_id = ?", (character_id,)
        )
        conn.execute("DELETE FROM character_affect WHERE character_id = ?", (character_id,))
        for table in ("shelf_members", "group_member_state"):
            conn.execute(f"DELETE FROM {table} WHERE character_id = ?", (character_id,))
        conn.execute("DELETE FROM memory_chunks WHERE character_id = ?", (character_id,))
        cursor = conn.execute("DELETE FROM characters WHERE id = ?", (character_id,))
    return cursor.rowcount > 0


def _character_name_key(name: str) -> str:
    """重抽时匹配角色用的身份键：去掉空白和标点，统一小写。"""
    text = str(name or "").strip().lower()
    return re.sub(r"[\s·・.,，。、;；:：'\"“”‘’()（）\[\]【】<>《》\-—_]+", "", text)


def replace_novel_characters(book_id: str, characters: list[dict]) -> list[dict]:
    """重新抽取时更新「小说抽取」出来的角色，**保留角色身份**。

    旧实现是"全删重建"，代价很大：

    * 角色 id 全变 → 群成员关系、情绪状态、聊天里对它的引用一起失联
    * 用户挑过的头像被打回 🎭
    * 角色从对话里学到的记忆（``kind='fact'``）被连带清空

    现在的规则：

    * **同名**（归一化后）→ 原地更新抽取字段，id / 头像 / 化身标记 / prompt_override 保留
    * **新名字** → 插入
    * **消失的角色** → 删除，并回收它的成员关系、情绪、记忆、向量
    * 每个角色的「本书原文片段」重算；**其它来源与对话记忆保留**
    """
    incoming = [
        item for item in (characters or []) if str(item.get("name") or "").strip()
    ]
    kept_ids: set[str] = set()
    matched: dict[int, str] = {}
    to_insert: list[tuple[int, dict]] = []

    with connect() as conn:
        existing_rows = conn.execute(
            "SELECT * FROM characters WHERE book_id = ? AND source = 'novel'",
            (book_id,),
        ).fetchall()
        by_key: dict[str, sqlite3.Row] = {}
        for row in existing_rows:
            by_key.setdefault(_character_name_key(row["name"]), row)

        for index, character in enumerate(incoming):
            row = by_key.get(_character_name_key(character["name"]))
            if row is None:
                to_insert.append((index, character))
                continue
            kept_ids.add(row["id"])
            matched[index] = row["id"]
            conn.execute(
                """
                UPDATE characters SET
                    name = ?, aliases = ?, role = ?, personality = ?,
                    speech_style = ?, background = ?, relationships = ?,
                    quotes = ?, appearances = ?, sort_order = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    character["name"],
                    _dumps(character.get("aliases") or []),
                    character.get("role") or "",
                    _dumps(character.get("personality") or []),
                    character.get("speech_style") or "",
                    character.get("background") or "",
                    _dumps(character.get("relationships") or []),
                    _dumps(character.get("quotes") or []),
                    int(character.get("appearances") or 0),
                    index,
                    _now(),
                    row["id"],
                ),
            )
            # 只重算"来自本书的原文片段"，对话学到的 fact 与别处片段都留着
            conn.execute(
                "DELETE FROM character_memory_items "
                " WHERE character_id = ? AND source_book_id = ? AND kind = 'excerpt'",
                (row["id"], book_id),
            )
            _seed_memory_items(
                conn,
                character_id=row["id"],
                book_id=book_id,
                excerpts=character.get("excerpts") or [],
                added_reason="重新抽取时重算的原文片段",
            )

        # 这次没抽到的角色：连同关系行一起回收，别留孤儿
        for row in existing_rows:
            if row["id"] in kept_ids:
                continue
            for table in ("character_affect", "shelf_members", "group_member_state"):
                conn.execute(f"DELETE FROM {table} WHERE character_id = ?", (row["id"],))
            conn.execute(
                "DELETE FROM character_memory_items WHERE character_id = ?", (row["id"],)
            )
            conn.execute(
                "DELETE FROM memory_chunks WHERE character_id = ?", (row["id"],)
            )
            conn.execute("DELETE FROM characters WHERE id = ?", (row["id"],))

    inserted: dict[int, str] = {}
    for index, character in to_insert:
        created = insert_character(
            book_id=book_id,
            name=character["name"],
            aliases=character.get("aliases") or [],
            role=character.get("role") or "",
            personality=character.get("personality") or [],
            speech_style=character.get("speech_style") or "",
            background=character.get("background") or "",
            relationships=character.get("relationships") or [],
            quotes=character.get("quotes") or [],
            excerpts=character.get("excerpts") or [],
            avatar=character.get("avatar") or "🎭",
            source="novel",
            is_user_persona=False,
            appearances=character.get("appearances") or 0,
            sort_order=index,
        )
        inserted[index] = created["id"]

    result: list[dict] = []
    for index in range(len(incoming)):
        character_id = matched.get(index) or inserted.get(index)
        if not character_id:
            continue
        row = get_character(character_id)
        if row:
            result.append(row)
    return result


def recover_interrupted_extractions(reason: str = "服务重启") -> list[dict]:
    """把停在 ``extracting`` 的书标成失败。

    抽取任务跑在**进程内**（``library._tasks`` 是个 dict）。服务重启后任务没了，
    但数据库里状态还停在 ``extracting``，前端会一直轮询一个永远不动的进度条，
    用户最后只能删书重传。启动时回收一次，让它变成"可重试"。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title FROM books WHERE status = 'extracting'"
        ).fetchall()
        if not rows:
            return []
        conn.execute(
            "UPDATE books SET status = 'failed', progress = 100, stage = '已中断', "
            "error = ? WHERE status = 'extracting'",
            (f"{reason}：抽取任务被中断，请点「重新抽取」",),
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 初始记忆条目（characters.excerpts 是它的派生缓存）
# ---------------------------------------------------------------------------
def _row_to_memory_item(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["importance"] = float(data.get("importance") or 0.0)
    return data


def _backfill_memory_tokens(conn: sqlite3.Connection) -> int:
    """给还没有 token 的记忆条目补上切词结果（RAG 检索依赖它）。"""
    rows = conn.execute(
        "SELECT id, content FROM character_memory_items WHERE tokens = ''"
    ).fetchall()
    for row in rows:
        tokens = " ".join(_tokenize(row["content"]))
        conn.execute(
            "UPDATE character_memory_items SET tokens = ? WHERE id = ?",
            (tokens, row["id"]),
        )
    return len(rows)


def _backfill_memory_scene(conn: sqlite3.Connection) -> int:
    """给老记忆补场景标签（RAG 的场景加权靠它）。"""
    rows = conn.execute(
        "SELECT id, content FROM character_memory_items WHERE scene = ''"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE character_memory_items SET scene = ? WHERE id = ?",
            (scene_mod.detect(row["content"] or ""), row["id"]),
        )
    return len(rows)


def _insert_memory_item(
    conn: sqlite3.Connection,
    *,
    character_id: str,
    content: str,
    source_book_id: Optional[str],
    kind: str,
    importance: float,
    added_reason: str,
    scene: str = "",
) -> str:
    item_id = new_id("mem")
    conn.execute(
        """
        INSERT INTO character_memory_items
            (id, character_id, origin_character_id, source_book_id, kind, scene, tokens,
             content, importance, added_reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            character_id,
            character_id,  # 记住"这段记忆本来属于谁"，撤销合并时靠它精确还原
            source_book_id,
            kind if kind in MEMORY_KINDS else "excerpt",
            scene or "",
            " ".join(_tokenize(content)),
            content,
            float(importance),
            added_reason or "",
            _now_ms(),
        ),
    )
    return item_id


def _sync_excerpts_from_items(conn: sqlite3.Connection, character_id: str) -> None:
    """把条目表里 kind='excerpt' 的内容回写成 characters.excerpts 派生缓存。

    为什么要同步：提示词（services/prompts.py）、前端、接口都还在读
    ``character["excerpts"]``；保持它和条目表一致，就不用一次性改所有读取方。
    """
    rows = conn.execute(
        """
        SELECT content FROM character_memory_items
         WHERE character_id = ? AND kind = 'excerpt'
         ORDER BY importance DESC, rowid ASC
        """,
        (character_id,),
    ).fetchall()
    conn.execute(
        "UPDATE characters SET excerpts = ?, updated_at = ? WHERE id = ?",
        (_dumps([r["content"] for r in rows]), _now(), character_id),
    )


def list_memory_items(
    character_id: str,
    *,
    kind: Optional[str] = None,
    source_book_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    clauses = ["character_id = ?"]
    params: list[Any] = [character_id]
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if source_book_id:
        clauses.append("source_book_id = ?")
        params.append(source_book_id)
    sql = (
        "SELECT * FROM character_memory_items WHERE "
        + " AND ".join(clauses)
        + " ORDER BY importance DESC, rowid ASC, id ASC"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
    with connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_memory_item(row) for row in rows]


def add_memory_items(
    character_id: str,
    contents: list[str],
    *,
    source_book_id: Optional[str] = None,
    kind: str = "excerpt",
    importance: float = 0.5,
    added_reason: str = "",
    scene: str = "",
) -> list[dict]:
    """批量追加记忆条目，并刷新 excerpts 派生缓存。"""
    cleaned = [c.strip() for c in (contents or []) if isinstance(c, str) and c.strip()]
    if not cleaned:
        return []
    created_ids = []
    with connect() as conn:
        for content in cleaned:
            created_ids.append(
                _insert_memory_item(
                    conn,
                    character_id=character_id,
                    content=content,
                    source_book_id=source_book_id,
                    kind=kind,
                    importance=importance,
                    added_reason=added_reason,
                    scene=scene,
                )
            )
        _sync_excerpts_from_items(conn, character_id)
        rows = conn.execute(
            "SELECT * FROM character_memory_items WHERE character_id = ? "
            "ORDER BY importance DESC, rowid ASC",
            (character_id,),
        ).fetchall()
    keep = set(created_ids)
    return [_row_to_memory_item(row) for row in rows if row["id"] in keep]


def update_memory_vector(item_id: str, vector: Optional[bytes]) -> bool:
    """写入/清空一条记忆的整段向量（块级向量见 memory_chunks）。"""
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE character_memory_items SET vector = ? WHERE id = ?",
            (vector, item_id),
        )
    return cursor.rowcount > 0


def update_memory_content(item_id: str, content: str) -> bool:
    """改写一条记忆的正文（正文清洗用）。

    同步更新切词、**作废旧向量和块向量**（否则检索还在用旧内容的向量），
    并刷新 ``characters.excerpts`` 缓存。
    """
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE character_memory_items SET content = ?, tokens = ?, vector = NULL "
            "WHERE id = ?",
            (content, " ".join(_tokenize(content)), item_id),
        )
        conn.execute("DELETE FROM memory_chunks WHERE item_id = ?", (item_id,))
        row = conn.execute(
            "SELECT character_id FROM character_memory_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row:
            _sync_excerpts_from_items(conn, row["character_id"])
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# 块级向量（memory_chunks）
# ---------------------------------------------------------------------------
def replace_memory_chunks(
    item_id: str, character_id: str, chunks: list[tuple[str, Optional[bytes]]]
) -> int:
    """重建一条记忆的全部块向量。"""
    with connect() as conn:
        conn.execute("DELETE FROM memory_chunks WHERE item_id = ?", (item_id,))
        now = _now_ms()
        for index, (text, vector) in enumerate(chunks):
            conn.execute(
                "INSERT INTO memory_chunks (id, item_id, character_id, idx, text, vector, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id("chunk"), item_id, character_id, index, text, vector, now),
            )
    return len(chunks)


def list_memory_chunks(character_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_chunks WHERE character_id = ? ORDER BY item_id, idx",
            (character_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 管理 Agent 经验记忆（书架级）
# ---------------------------------------------------------------------------
def _row_to_manager_memory(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["payload"] = _loads(data.get("payload"), {})
    data["hits"] = int(data.get("hits") or 0)
    return data


def add_manager_memory(
    shelf_id: str,
    kind: str,
    content: str,
    payload: Optional[dict] = None,
) -> dict:
    """记一条管理 Agent 的经验（幂等：同 shelf + kind + content 只留一条）。"""
    text = str(content or "").strip()
    if not shelf_id or not text:
        raise ValueError("书架和内容都不能为空")
    kind = str(kind or "same_person").strip() or "same_person"
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM manager_memories WHERE shelf_id = ? AND kind = ? AND content = ?",
            (shelf_id, kind, text),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE manager_memories SET hits = hits + 1 WHERE id = ?",
                (existing["id"],),
            )
            return _row_to_manager_memory(
                conn.execute(
                    "SELECT * FROM manager_memories WHERE id = ?", (existing["id"],)
                ).fetchone()
            )
        memory_id = new_id("mm")
        conn.execute(
            "INSERT INTO manager_memories "
            "(id, shelf_id, kind, content, payload, tokens, hits, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (
                memory_id,
                shelf_id,
                kind,
                text[:400],
                _dumps(payload or {}),
                " ".join(_tokenize(text)),
                _now(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM manager_memories WHERE id = ?", (memory_id,)
        ).fetchone()
    return _row_to_manager_memory(row)


def list_manager_memories(
    shelf_id: str, *, kind: Optional[str] = None, limit: int = 200
) -> list[dict]:
    clauses = ["shelf_id = ?"]
    params: list[Any] = [shelf_id]
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    params.append(max(1, min(int(limit), 1000)))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM manager_memories WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [_row_to_manager_memory(row) for row in rows]


def delete_manager_memory(memory_id: str) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM manager_memories WHERE id = ?", (memory_id,))
    return cursor.rowcount > 0


def items_without_chunks(character_id: str, *, force: bool = False) -> list[dict]:
    """还没建块向量的记忆条目（``force`` 时返回全部）。"""
    with connect() as conn:
        if force:
            rows = conn.execute(
                "SELECT * FROM character_memory_items WHERE character_id = ?",
                (character_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM character_memory_items i WHERE i.character_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM memory_chunks c WHERE c.item_id = i.id)",
                (character_id,),
            ).fetchall()
    return [_row_to_memory_item(row) for row in rows]


def delete_memory_items(
    *,
    character_id: Optional[str] = None,
    source_book_id: Optional[str] = None,
    kind: Optional[str] = None,
) -> int:
    """按角色 / 来源书 / 类型删除记忆条目。

    **至少给一个过滤条件**——防止误传空参把整张表清空。
    """
    if not any((character_id, source_book_id, kind)):
        raise ValueError("delete_memory_items 需要至少一个过滤条件")

    clauses = []
    params: list[Any] = []
    if character_id:
        clauses.append("character_id = ?")
        params.append(character_id)
    if source_book_id:
        clauses.append("source_book_id = ?")
        params.append(source_book_id)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    where = " AND ".join(clauses)

    with connect() as conn:
        affected = [
            r["character_id"]
            for r in conn.execute(
                f"SELECT DISTINCT character_id FROM character_memory_items WHERE {where}",
                tuple(params),
            ).fetchall()
        ]
        cursor = conn.execute(
            f"DELETE FROM character_memory_items WHERE {where}", tuple(params)
        )
        # 块向量跟着条目一起清，否则会留下指向已删记忆的孤儿块
        _purge_orphan_chunks(conn, affected)
        for cid in affected:
            _sync_excerpts_from_items(conn, cid)
    return cursor.rowcount


def _purge_orphan_chunks(
    conn: sqlite3.Connection, character_ids: Optional[list[str]] = None
) -> int:
    """清掉已经不存在的记忆条目对应的块向量。"""
    if character_ids:
        placeholders = ",".join("?" for _ in character_ids)
        cursor = conn.execute(
            f"DELETE FROM memory_chunks WHERE character_id IN ({placeholders}) "
            "AND NOT EXISTS (SELECT 1 FROM character_memory_items i WHERE i.id = memory_chunks.item_id)",
            tuple(character_ids),
        )
    else:
        cursor = conn.execute(
            "DELETE FROM memory_chunks WHERE NOT EXISTS "
            "(SELECT 1 FROM character_memory_items i WHERE i.id = memory_chunks.item_id)"
        )
    return cursor.rowcount


def sync_excerpts_cache(character_id: str) -> None:
    """按条目表重算 ``characters.excerpts`` 派生缓存。"""
    with connect() as conn:
        _sync_excerpts_from_items(conn, character_id)


def delete_memory_item(item_id: str) -> bool:
    """按条目 id 精确删除（保留其余条目的来源信息）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT character_id FROM character_memory_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM character_memory_items WHERE id = ?", (item_id,))
        _sync_excerpts_from_items(conn, row["character_id"])
    return True


def prune_memory_items(character_id: str, *, kind: str = "excerpt", keep: int = 3) -> int:
    """只保留重要度前 ``keep`` 条，其余按 id 精确删除（来源信息不丢）。"""
    rows = list_memory_items(character_id, kind=kind)
    removed = 0
    for row in rows[max(0, keep) :]:
        if delete_memory_item(row["id"]):
            removed += 1
    return removed


def _seed_memory_items(
    conn: sqlite3.Connection,
    *,
    character_id: str,
    book_id: Optional[str],
    excerpts: list[str],
    added_reason: str = "角色抽取产出的原文片段",
) -> None:
    for content in excerpts or []:
        if isinstance(content, str) and content.strip():
            _insert_memory_item(
                conn,
                character_id=character_id,
                content=content,
                source_book_id=book_id,
                kind="excerpt",
                importance=0.5,
                added_reason=added_reason,
            )
    _sync_excerpts_from_items(conn, character_id)


def _migrate_excerpts_to_memory_items(conn: sqlite3.Connection) -> int:
    """把 v1 的 ``characters.excerpts``（JSON 数组）拆成记忆条目。

    幂等：只处理「有 excerpts 但一条记忆条目都没有」的角色。
    """
    rows = conn.execute(
        """
        SELECT c.id AS id, c.book_id AS book_id, c.excerpts AS excerpts
          FROM characters c
         WHERE NOT EXISTS (
                   SELECT 1 FROM character_memory_items m WHERE m.character_id = c.id
               )
           AND c.excerpts IS NOT NULL
           AND c.excerpts != ''
           AND c.excerpts != '[]'
        """
    ).fetchall()

    migrated = 0
    for row in rows:
        excerpts = _loads(row["excerpts"], [])
        if not isinstance(excerpts, list):
            continue
        for content in excerpts:
            if isinstance(content, str) and content.strip():
                _insert_memory_item(
                    conn,
                    character_id=row["id"],
                    content=content,
                    source_book_id=row["book_id"],
                    kind="excerpt",
                    importance=0.5,
                    added_reason="迁移自 v1 excerpts",
                )
                migrated += 1
    return migrated


# ---------------------------------------------------------------------------
# 情绪 / 好感度
# ---------------------------------------------------------------------------
def _row_to_affect(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["favorability"] = int(data.get("favorability") or 0)
    data["valence"] = float(data.get("valence") or 0.0)
    data["arousal"] = float(data.get("arousal") or 0.0)
    data["turns"] = int(data.get("turns") or 0)
    baseline = _loads(data.get("baseline"), {})
    data["baseline"] = baseline if isinstance(baseline, dict) else {}
    return data


def get_affect(character_id: str, user_id: str, shelf_id: str = "") -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM character_affect "
            "WHERE character_id = ? AND user_id = ? AND shelf_id = ?",
            (character_id, user_id, shelf_id or ""),
        ).fetchone()
    return _row_to_affect(row) if row else None


def save_affect(
    character_id: str,
    user_id: str,
    shelf_id: str = "",
    *,
    favorability: int,
    valence: float,
    arousal: float,
    emotion: str,
    baseline: Optional[dict] = None,
    turns: int = 0,
    evidence_event_id: Optional[str] = None,
) -> dict:
    """整行 upsert。数值的**约束在 services/affect.py 里做**，这里只负责落盘。"""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO character_affect
                (character_id, user_id, shelf_id, favorability, valence, arousal,
                 emotion, baseline, turns, evidence_event_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(character_id, user_id, shelf_id) DO UPDATE SET
                favorability      = excluded.favorability,
                valence           = excluded.valence,
                arousal           = excluded.arousal,
                emotion           = excluded.emotion,
                baseline          = excluded.baseline,
                turns             = excluded.turns,
                evidence_event_id = excluded.evidence_event_id,
                updated_at        = excluded.updated_at
            """,
            (
                character_id,
                user_id,
                shelf_id or "",
                max(0, min(100, int(favorability))),
                max(-1.0, min(1.0, float(valence))),
                max(0.0, min(1.0, float(arousal))),
                emotion or "平静",
                _dumps(baseline or {}),
                int(turns),
                evidence_event_id,
                _now(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM character_affect "
            "WHERE character_id = ? AND user_id = ? AND shelf_id = ?",
            (character_id, user_id, shelf_id or ""),
        ).fetchone()
    return _row_to_affect(row)


def delete_affect(
    *,
    character_id: Optional[str] = None,
    user_id: Optional[str] = None,
    shelf_id: Optional[str] = None,
) -> int:
    """按条件删情绪状态（测试清理 / 用户重置用）。至少给一个条件。"""
    if not any(x is not None for x in (character_id, user_id, shelf_id)):
        raise ValueError("delete_affect 需要至少一个过滤条件")
    clauses, params = [], []
    if character_id is not None:
        clauses.append("character_id = ?")
        params.append(character_id)
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if shelf_id is not None:
        clauses.append("shelf_id = ?")
        params.append(shelf_id)
    with connect() as conn:
        cursor = conn.execute(
            f"DELETE FROM character_affect WHERE {' AND '.join(clauses)}", tuple(params)
        )
    return cursor.rowcount


# ---------------------------------------------------------------------------
# 书库 / 书架
#
# 三级结构：library（书库，最大）-> shelf（书架，一套独立系统）-> book（书）
# 书架是隔离边界：日志、记忆、情绪、群聊都按 shelf_id 分区。
# ---------------------------------------------------------------------------
def _row_to_library(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["shelf_count"] = int(data.get("shelf_count") or 0)
    data["book_count"] = int(data.get("book_count") or 0)
    return data


def _row_to_shelf(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["book_count"] = int(data.get("book_count") or 0)
    data["character_count"] = int(data.get("character_count") or 0)
    return data


def create_library(title: str, description: str = "") -> dict:
    library_id = new_id("lib")
    with connect() as conn:
        conn.execute(
            "INSERT INTO libraries (id, title, description, created_at) VALUES (?, ?, ?, ?)",
            (library_id, (title or "未命名书库").strip()[:80], description or "", _now()),
        )
    return get_library(library_id)  # type: ignore[return-value]


def get_library(library_id: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT l.*,
                   (SELECT COUNT(*) FROM shelves s WHERE s.library_id = l.id) AS shelf_count,
                   (SELECT COUNT(*) FROM books b
                      JOIN shelves s2 ON s2.id = b.shelf_id
                     WHERE s2.library_id = l.id) AS book_count
              FROM libraries l WHERE l.id = ?
            """,
            (library_id,),
        ).fetchone()
    return _row_to_library(row) if row else None


def list_libraries() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT l.*,
                   (SELECT COUNT(*) FROM shelves s WHERE s.library_id = l.id) AS shelf_count,
                   (SELECT COUNT(*) FROM books b
                      JOIN shelves s2 ON s2.id = b.shelf_id
                     WHERE s2.library_id = l.id) AS book_count
              FROM libraries l ORDER BY l.created_at ASC
            """
        ).fetchall()
    return [_row_to_library(row) for row in rows]


def create_shelf(library_id: str, title: str, description: str = "") -> dict:
    shelf_id = new_id("shelf")
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM libraries WHERE id = ?", (library_id,)).fetchone()
        if exists is None:
            raise ValueError(f"书库不存在：{library_id}")
        conn.execute(
            "INSERT INTO shelves (id, library_id, title, description, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (shelf_id, library_id, (title or "未命名书架").strip()[:80], description or "", _now()),
        )
    return get_shelf(shelf_id)  # type: ignore[return-value]


def get_shelf(shelf_id: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM books b WHERE b.shelf_id = s.id) AS book_count,
                   (SELECT COUNT(*) FROM characters c
                      JOIN books b2 ON b2.id = c.book_id
                     WHERE b2.shelf_id = s.id) AS character_count
              FROM shelves s WHERE s.id = ?
            """,
            (shelf_id,),
        ).fetchone()
    return _row_to_shelf(row) if row else None


def list_shelves(library_id: Optional[str] = None) -> list[dict]:
    with connect() as conn:
        sql = """
            SELECT s.*,
                   (SELECT COUNT(*) FROM books b WHERE b.shelf_id = s.id) AS book_count,
                   (SELECT COUNT(*) FROM characters c
                      JOIN books b2 ON b2.id = c.book_id
                     WHERE b2.shelf_id = s.id) AS character_count
              FROM shelves s
        """
        if library_id:
            rows = conn.execute(
                sql + " WHERE s.library_id = ? ORDER BY s.created_at ASC", (library_id,)
            ).fetchall()
        else:
            rows = conn.execute(sql + " ORDER BY s.created_at ASC").fetchall()
    return [_row_to_shelf(row) for row in rows]


def set_book_shelf(book_id: str, shelf_id: str) -> bool:
    """把一本书移动到另一个书架（同书库内）。"""
    with connect() as conn:
        shelf = conn.execute("SELECT 1 FROM shelves WHERE id = ?", (shelf_id,)).fetchone()
        if shelf is None:
            raise ValueError(f"书架不存在：{shelf_id}")
        cursor = conn.execute(
            "UPDATE books SET shelf_id = ? WHERE id = ?", (shelf_id, book_id)
        )
    return cursor.rowcount > 0


def update_shelf(shelf_id: str, *, title: Optional[str] = None, description: Optional[str] = None) -> dict:
    """改书架名称/描述（群聊改名也走这里）。"""
    updates: dict[str, Any] = {}
    if title is not None:
        cleaned = str(title).strip()
        if not cleaned:
            raise ValueError("书架名称不能为空")
        updates["title"] = cleaned[:60]
    if description is not None:
        updates["description"] = str(description).strip()[:200]
    if not updates:
        return get_shelf(shelf_id) or {}
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with connect() as conn:
        conn.execute(
            f"UPDATE shelves SET {assignments} WHERE id = ?",
            (*updates.values(), shelf_id),
        )
    return get_shelf(shelf_id) or {}


def delete_shelf(shelf_id: str) -> bool:
    """删书架。**书架里还有书就拒绝**，不静默丢数据。

    "删书架要连书一起删"的级联逻辑放在路由层（`core` 不该反向依赖 `services`）：
    路由先逐本 `library.delete_book()`，再调本函数。

    书架一旦没了，挂在它上面的东西（日志、群成员、成员状态、发言流水、情绪状态）
    就都失去意义，一并清掉——否则会留下一堆指向已删书架的孤儿行。
    """
    with connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM books WHERE shelf_id = ?", (shelf_id,)
        ).fetchone()["n"]
        if count:
            raise ValueError(f"书架里还有 {count} 本书，不能直接删除")
        for table in (
            "agent_events",
            "shelf_members",
            "group_member_state",
            "chat_turns",
            "character_affect",
        ):
            conn.execute(f"DELETE FROM {table} WHERE shelf_id = ?", (shelf_id,))
        cursor = conn.execute("DELETE FROM shelves WHERE id = ?", (shelf_id,))
    return cursor.rowcount > 0


def delete_library(library_id: str) -> bool:
    """删书库（连带其书架）。库里还有书就拒绝。"""
    with connect() as conn:
        count = conn.execute(
            """
            SELECT COUNT(*) AS n FROM books b
              JOIN shelves s ON s.id = b.shelf_id
             WHERE s.library_id = ?
            """,
            (library_id,),
        ).fetchone()["n"]
        if count:
            raise ValueError(f"书库里还有 {count} 本书，不能直接删除")
        conn.execute(
            "DELETE FROM agent_events WHERE shelf_id IN "
            "(SELECT id FROM shelves WHERE library_id = ?)",
            (library_id,),
        )
        cursor = conn.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Agent 审计日志
# ---------------------------------------------------------------------------
_EVENT_STRING_LIMIT = 1000


def _shrink(value: Any) -> Any:
    """把 payload 里过长的字符串截断，避免整篇原文挤进日志表。"""
    if isinstance(value, str):
        if len(value) <= _EVENT_STRING_LIMIT:
            return value
        return value[:_EVENT_STRING_LIMIT] + "…[截断]"
    if isinstance(value, dict):
        return {str(k): _shrink(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shrink(v) for v in value]
    return value


def _row_to_event(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["payload"] = _loads(data.get("payload"), {})
    if not isinstance(data["payload"], dict):
        data["payload"] = {"value": data["payload"]}
    return data


def log_event(
    event_type: str,
    *,
    owner_type: str = "system",
    owner_id: str = "",
    trace_id: str = "",
    library_id: Optional[str] = None,
    shelf_id: Optional[str] = None,
    book_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    severity: str = "info",
    payload: Optional[dict] = None,
) -> dict:
    """写一条 Agent 行为事件。

    ``shelf_id`` 缺省时会从 ``book_id`` 反查：调用方只要给书，日志就自动落到
    正确的书架，保证「各书架日志互不干扰」不依赖调用方记得传参。
    """
    if shelf_id is None and book_id:
        book = get_book(book_id)
        if book:
            shelf_id = book.get("shelf_id")
    if library_id is None and shelf_id:
        shelf = get_shelf(shelf_id)
        if shelf:
            library_id = shelf.get("library_id")

    event_id = new_id("evt")
    body = _shrink(payload or {})
    dumped = _dumps(body)
    if len(dumped) > MAX_EVENT_PAYLOAD_CHARS:
        dumped = _dumps({"truncated": True, "preview": dumped[:MAX_EVENT_PAYLOAD_CHARS]})
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_events (id, ts, trace_id, owner_type, owner_id,
                                      library_id, shelf_id, book_id, user_id, session_id,
                                      event_type, severity, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                _now_ms(),
                trace_id or "",
                owner_type,
                owner_id or "",
                library_id,
                shelf_id,
                book_id,
                user_id,
                session_id,
                event_type,
                severity,
                dumped,
            ),
        )
        row = conn.execute("SELECT * FROM agent_events WHERE id = ?", (event_id,)).fetchone()
    return _row_to_event(row)


def list_events(
    *,
    shelf_id: Optional[str] = None,
    library_id: Optional[str] = None,
    book_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    event_type: Optional[str] = None,
    severity: Optional[str] = None,
    owner_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """按书架/书/任务查事件，时间倒序。书架间互不可见。"""
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("shelf_id", shelf_id),
        ("library_id", library_id),
        ("book_id", book_id),
        ("trace_id", trace_id),
        ("event_type", event_type),
        ("severity", severity),
        ("owner_type", owner_type),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.extend([max(1, min(int(limit), 1000)), max(0, int(offset))])
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM agent_events{where} ORDER BY ts DESC, rowid DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
    return [_row_to_event(row) for row in rows]


def count_events(**filters: Any) -> int:
    return len(list_events(limit=1000, **filters))


# ---------------------------------------------------------------------------
# 群聊：成员 / 状态 / 发言流水
# ---------------------------------------------------------------------------
def _row_to_member(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["personality"] = _loads(data.get("personality"), [])
    # 群成员同时也是"角色"，补一个 id 让 affect / rag 之类按角色 id 取状态的模块能直接用
    data["id"] = data.get("character_id") or data.get("id")
    # 没有 state 行时保持 None：交给 group_chat 按性格推导，别用 0.5 糊住
    for field in ("extraversion", "assertiveness", "initiative_floor"):
        data[field] = None if data.get(field) is None else float(data[field])
    data["turns"] = int(data.get("turns") or 0)
    data["state"] = data.get("state") or "idle"
    return data


def ensure_shelf_members(shelf_id: str) -> list[dict]:
    """把书架里所有书的角色同步成群成员（幂等）。

    * 新角色 → 插入 ``online``（在线，可以直接收发消息）
    * 书被移出书架 → 标 ``left``
    * 手动设成 ``offline``（离线）的保持不变
    * **用户自建角色不入群**——那是"你自己"，不是群里另一个 AI
    """
    with connect() as conn:
        wanted = {
            row["id"]: row
            for row in conn.execute(
                """
                SELECT c.id AS id, c.book_id AS book_id
                  FROM characters c JOIN books b ON b.id = c.book_id
                 WHERE b.shelf_id = ? AND c.is_user_persona = 0
                   AND c.canonical_id IS NULL
                """,
                (shelf_id,),
            ).fetchall()
        }
        existing = {
            row["character_id"]: row
            for row in conn.execute(
                "SELECT * FROM shelf_members WHERE shelf_id = ?", (shelf_id,)
            ).fetchall()
        }
        now = _now()
        for character_id, row in wanted.items():
            if character_id not in existing:
                conn.execute(
                    "INSERT INTO shelf_members "
                    "(shelf_id, character_id, source_book_id, status, joined_at) "
                    "VALUES (?, ?, ?, 'online', ?)",
                    (shelf_id, character_id, row["book_id"], now),
                )
            elif existing[character_id]["status"] == "left":
                conn.execute(
                    "UPDATE shelf_members SET status = 'online', left_at = NULL "
                    "WHERE shelf_id = ? AND character_id = ?",
                    (shelf_id, character_id),
                )
        for character_id, row in existing.items():
            if character_id not in wanted and row["status"] != "left":
                conn.execute(
                    "UPDATE shelf_members SET status = 'left', left_at = ? "
                    "WHERE shelf_id = ? AND character_id = ?",
                    (now, shelf_id, character_id),
                )
    return list_shelf_members(shelf_id)


def list_shelf_members(shelf_id: str, *, include_left: bool = False) -> list[dict]:
    where = "" if include_left else "AND m.status != 'left'"
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT m.shelf_id, m.character_id, m.source_book_id, m.status,
                   m.joined_at, m.left_at,
                   c.name, c.role, c.avatar, c.avatar_url, c.personality,
                   c.speech_style, c.book_id,
                   COALESCE(s.state, 'idle') AS state,
                   s.last_spoke_at, s.extraversion, s.assertiveness,
                   s.initiative_floor, s.turns
              FROM shelf_members m
              JOIN characters c ON c.id = m.character_id
              LEFT JOIN group_member_state s
                     ON s.shelf_id = m.shelf_id AND s.character_id = m.character_id
             WHERE m.shelf_id = ? {where}
             ORDER BY c.sort_order ASC, c.appearances DESC, c.name ASC
            """,
            (shelf_id,),
        ).fetchall()
    return [_row_to_member(row) for row in rows]


#: 成员在线状态。兼容旧值：active -> online，muted -> offline
MEMBER_STATUS_ALIASES = {"active": "online", "muted": "offline"}
MEMBER_STATUSES = ("online", "offline", "left")


def normalize_member_status(status: str) -> str:
    key = str(status or "").strip().lower()
    key = MEMBER_STATUS_ALIASES.get(key, key)
    if key not in MEMBER_STATUSES:
        raise ValueError(f"未知成员状态：{status}")
    return key


def set_member_status(shelf_id: str, character_id: str, status: str) -> bool:
    """设置成员在线状态。

    * ``online``  在线：能收消息，也会按规则发言
    * ``offline`` 离线：收不到消息，也不会发言（@全体成员 会把所有人叫上线）
    * ``left``    已退群（书被移出书架）

    兼容旧接口：传 ``active`` / ``muted`` 会分别映射到 online / offline。
    """
    status = normalize_member_status(status)
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE shelf_members SET status = ?, left_at = ? "
            "WHERE shelf_id = ? AND character_id = ?",
            (status, _now() if status == "left" else None, shelf_id, character_id),
        )
        conn.execute(
            "UPDATE group_member_state SET state = ? "
            "WHERE shelf_id = ? AND character_id = ? AND state IN ('muted', 'idle')",
            ("muted" if status == "offline" else "idle", shelf_id, character_id),
        )
    return cursor.rowcount > 0


def set_all_members_online(shelf_id: str) -> int:
    """把书架里所有**已退群之外**的成员设为在线（@全体成员 时用）。"""
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE shelf_members SET status = 'online', left_at = NULL "
            "WHERE shelf_id = ? AND status != 'left'",
            (shelf_id,),
        )
        conn.execute(
            "UPDATE group_member_state SET state = 'idle' "
            "WHERE shelf_id = ? AND state = 'muted'",
            (shelf_id,),
        )
    return cursor.rowcount


def upsert_member_state(shelf_id: str, character_id: str, **fields: Any) -> dict:
    """写成员运行态（状态机 / 性格参数）。只接受已知列。"""
    allowed = {
        "state", "last_spoke_at", "extraversion",
        "assertiveness", "initiative_floor", "turns",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO group_member_state (shelf_id, character_id) VALUES (?, ?)",
            (shelf_id, character_id),
        )
        if updates:
            assignments = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE group_member_state SET {assignments} "
                "WHERE shelf_id = ? AND character_id = ?",
                (*updates.values(), shelf_id, character_id),
            )
        row = conn.execute(
            "SELECT * FROM group_member_state WHERE shelf_id = ? AND character_id = ?",
            (shelf_id, character_id),
        ).fetchone()
    return _row_to_member(row)


def record_member_spoke(shelf_id: str, character_id: str) -> None:
    """记一次发言：状态转 cooldown、刷新时间、轮次 +1。"""
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO group_member_state (shelf_id, character_id) VALUES (?, ?)",
            (shelf_id, character_id),
        )
        conn.execute(
            """
            UPDATE group_member_state
               SET state = 'cooldown', last_spoke_at = ?, turns = turns + 1
             WHERE shelf_id = ? AND character_id = ?
            """,
            (_now(), shelf_id, character_id),
        )


def add_chat_turn(
    *,
    shelf_id: str,
    user_id: str,
    role: str,
    content: str = "",
    character_id: Optional[str] = None,
    session_id: str = "",
    speaker_name: str = "",
    images: Optional[list] = None,
    stickers: Optional[list] = None,
    mentions: Optional[list] = None,
    intent: str = "",
    emotion: str = "",
    round_no: int = 0,
) -> dict:
    turn_id = new_id("turn")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO chat_turns
                (id, shelf_id, character_id, user_id, session_id, role, speaker_name,
                 content, images, stickers, mentions, intent, emotion, round_no, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                turn_id,
                shelf_id or "",
                character_id,
                user_id or "",
                session_id or "",
                role,
                speaker_name or "",
                content or "",
                _dumps(images or []),
                _dumps(stickers or []),
                _dumps(mentions or []),
                intent or "",
                emotion or "",
                int(round_no),
                _now_ms(),
            ),
        )
        row = conn.execute("SELECT * FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
    return dict(row)


def list_chat_turns(
    shelf_id: str,
    *,
    user_id: Optional[str] = None,
    character_id: Optional[str] = None,
    limit: int = 50,
    ascending: bool = True,
    pending_only: bool = False,
) -> list[dict]:
    """列出发言流水。默认**包含已蒸馏的**——那是聊天历史，不该消失。

    ``pending_only=True`` 时只返回还没蒸馏的（蒸馏/计数用）。
    """
    clauses = ["shelf_id = ?"]
    params: list[Any] = [shelf_id]
    if user_id:
        clauses.append("user_id = ?")
        params.append(user_id)
    if character_id:
        clauses.append("character_id = ?")
        params.append(character_id)
    if pending_only:
        clauses.append("distilled = 0")
    order = "ASC" if ascending else "DESC"
    params.append(max(1, min(int(limit), 500)))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM chat_turns WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at {order}, rowid {order} LIMIT ?",
            tuple(params),
        ).fetchall()
    turns = []
    for row in rows:
        data = dict(row)
        for field in ("images", "stickers", "mentions"):
            data[field] = _loads(data.get(field), [])
        data["distilled"] = bool(data.get("distilled"))
        turns.append(data)
    return turns


def list_history(
    shelf_id: str,
    *,
    user_id: str,
    character_id: Optional[str] = None,
    limit: int = 60,
) -> list[dict]:
    """聊天历史：取**最近** N 条，按时间正序返回（前端直接渲染）。"""
    rows = list_chat_turns(
        shelf_id,
        user_id=user_id,
        character_id=character_id,
        limit=limit,
        ascending=False,
    )
    rows.reverse()
    return rows


def count_chat_turns(
    shelf_id: str, *, user_id: Optional[str] = None, pending_only: bool = False
) -> int:
    clauses = ["shelf_id = ?"]
    params: list[Any] = [shelf_id]
    if user_id:
        clauses.append("user_id = ?")
        params.append(user_id)
    if pending_only:
        clauses.append("distilled = 0")
    with connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM chat_turns WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchone()
    return int(row["n"] or 0)


def mark_turns_distilled(turn_ids: list[str]) -> int:
    """把流水标记成"已蒸馏"。

    **不删行**：这些同时也是用户在群聊/单聊里看到的历史记录，
    删了就会"退出再进来一片空白"。
    """
    ids = [str(item) for item in (turn_ids or []) if item]
    if not ids:
        return 0
    with connect() as conn:
        placeholders = ",".join("?" for _ in ids)
        cursor = conn.execute(
            f"UPDATE chat_turns SET distilled = 1 WHERE id IN ({placeholders})",
            tuple(ids),
        )
    return cursor.rowcount


def clear_history(
    shelf_id: str, *, user_id: str, character_id: Optional[str] = None
) -> int:
    """清空某个用户在该书架的聊天历史（可选只清某个角色）。

    **只删流水，不删记忆**：蒸馏出来的长期记忆是角色的"记忆"，
    用户清的是"聊天记录"——混在一起会让角色失忆。
    """
    clauses = ["shelf_id = ?", "user_id = ?"]
    params: list[Any] = [shelf_id, user_id]
    if character_id:
        clauses.append("character_id = ?")
        params.append(character_id)
    with connect() as conn:
        cursor = conn.execute(
            f"DELETE FROM chat_turns WHERE {' AND '.join(clauses)}", tuple(params)
        )
    return cursor.rowcount


def delete_chat_turns(turn_ids: list[str]) -> int:
    """真正删除流水（只在"清空历史"这类显式操作里用）。"""
    ids = [str(item) for item in (turn_ids or []) if item]
    if not ids:
        return 0
    with connect() as conn:
        placeholders = ",".join("?" for _ in ids)
        cursor = conn.execute(
            f"DELETE FROM chat_turns WHERE id IN ({placeholders})", tuple(ids)
        )
    return cursor.rowcount
