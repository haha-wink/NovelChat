"""按角色构建 Agent：一个角色 = 一个 Agent，带 LRU 缓存。

相比旧版：
* Team/路由模式整套去掉，改为「选定角色 -> 单个 Agent」；
* system 指令由角色档案编译（services/prompts.py）；
* 会话记忆仍走 Agno + SQLite，按 user_id / session_id 隔离，
  这样换角色、开新对话都不会串味。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Optional

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.memory import MemoryManager
from agno.models.deepseek import DeepSeek

from core.config import get_settings
from services import agent_memory
from services.prompts import build_character_prompt

logger = logging.getLogger(__name__)

#: 缓存键 -> Agent，按插入顺序淘汰
_agent_cache: "OrderedDict[str, Agent]" = OrderedDict()
_cache_lock = threading.Lock()

MEMORY_CAPTURE_INSTRUCTIONS = """从对话中提取值得长期记住的信息：
- 用户希望被怎么称呼、以及用户自称的身份
- 用户与当前角色的关系、称呼方式
- 用户在本角色面前透露的经历、喜好、忌讳
- 你与用户之间已经发生过的关键剧情（约定、冲突、承诺）"""


def build_instructions(*, character: dict, book_title: str, user_name: str) -> str:
    """**稳定部分**：角色档案 + 初始记忆 top-k + 跨书记忆摘要。

    易变的「此刻状态」故意不放这里——Agent 会被 LRU 缓存，``instructions`` 只在构建时
    读取；把每轮都变的状态写进来，要么缓存失效（每轮重建 Agent），要么状态永远停在
    第一轮。状态改走 ``Agent.additional_context``，见 services/character_agent.py。
    """
    settings = get_settings()
    character_id = str(character.get("id") or "")
    memory = agent_memory.top(character_id, limit=max(1, int(settings.excerpt_chunks)))
    profile = dict(character)
    if memory:
        profile["excerpts"] = memory
    return build_character_prompt(
        profile,
        book_title=book_title,
        user_name=user_name,
        summary=agent_memory.summary(character_id),
    )


def cache_key(api_key: str, character: dict, user_name: str) -> str:
    raw = "|".join(
        [
            api_key,
            str(character.get("id") or ""),
            str(character.get("updated_at") or ""),
            (user_name or "").strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_agent(
    *,
    api_key: str,
    character: dict,
    book_title: str,
    user_name: str = "朋友",
) -> Agent:
    settings = get_settings()
    settings.ensure_dirs()

    model = DeepSeek(id=settings.deepseek_model, api_key=api_key)
    db = SqliteDb(db_file=str(settings.memory_db_path))

    memory_manager = MemoryManager(
        model=model,
        db=db,
        memory_capture_instructions=MEMORY_CAPTURE_INSTRUCTIONS,
    )

    prompt = build_instructions(character=character, book_title=book_title, user_name=user_name)
    name = (character.get("name") or "角色").strip()[:32] or "角色"

    return Agent(
        name=name,
        role=(character.get("role") or "").strip() or None,
        model=model,
        instructions=[prompt],
        db=db,
        add_history_to_context=True,
        num_history_runs=settings.history_runs,
        memory_manager=memory_manager,
        enable_agentic_memory=True,
        update_memory_on_run=True,
        add_memories_to_context=True,
        markdown=True,
    )


def get_agent(
    *,
    api_key: str,
    character: dict,
    book_title: str,
    user_name: str = "朋友",
) -> Agent:
    """取（或构建）该角色的 Agent；角色档案被编辑后缓存键会变，自动重建。"""
    if not api_key or not api_key.strip():
        raise ValueError("缺少 DeepSeek API Key")

    key = cache_key(api_key, character, user_name)

    with _cache_lock:
        cached = _agent_cache.get(key)
        if cached is not None:
            _agent_cache.move_to_end(key)
            return cached

    agent = build_agent(
        api_key=api_key.strip(),
        character=character,
        book_title=book_title,
        user_name=user_name,
    )

    settings = get_settings()
    with _cache_lock:
        _agent_cache[key] = agent
        _agent_cache.move_to_end(key)
        while len(_agent_cache) > max(1, settings.agent_cache_size):
            evicted, _ = _agent_cache.popitem(last=False)
            logger.debug("Agent 缓存淘汰：%s", evicted)

    logger.info("为角色「%s」构建 Agent（《%s》）", character.get("name"), book_title)
    return agent


def clear_cache() -> None:
    with _cache_lock:
        _agent_cache.clear()


def cached_agent_count() -> int:
    with _cache_lock:
        return len(_agent_cache)
