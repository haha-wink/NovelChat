"""角色 Agent：单角色对话的闭环。

## 两段式组装（本模块存在的理由）

```
┌─ 稳定部分（构建 Agent 时一次）──────────────────────────────┐
│ 角色档案 + 初始记忆 top-k   ->  Agent.instructions            │
└──────────────────────────────────────────────────────────────┘
┌─ 易变部分（每轮一次）──────────────────────────────────────┐
│ 此刻情绪 + 对你的态度       ->  Agent.additional_context      │
└──────────────────────────────────────────────────────────────┘
```

**为什么不用一个提示词打通**：Agno 的 Agent 按 `(角色, 用户名)` 缓存，``instructions``
只在构建时读取。把每轮都变的情绪写进去，结果只有两种——缓存失效（每轮重建 Agent），
或者状态永远停在第一轮。而 ``additional_context`` 是在**每轮组装系统消息时**才拼接的
（`agno/agent/_messages.py:1086`），天生适合承载易变部分。

**为什么要 `copy.copy`**：缓存里的 Agent 实例被多个请求共享。直接在它身上改
``additional_context``，并发时会把 A 的情绪状态串给 B。所以每轮操作副本，缓存对象
始终保持 ``additional_context is None``。

## 闭环在哪

```
本轮：load_state() -> prompt_block() -> 注入 -> 生成回复
之后：affect.observe_user_message() 根据用户这句话更新状态（见 api/routes.py）
下一轮：读到更新后的状态 -> 语气跟着变
```
"""

from __future__ import annotations

import copy
import logging
from typing import Optional, Tuple

from agno.agent import Agent

from core.config import get_settings
from services.dialogue import affect
from services.memory import rag
from agent.manager import get_agent

logger = logging.getLogger(__name__)


def attach_state(
    agent: Agent,
    character: dict,
    user_id: str,
    *,
    user_text: str = "",
    include_affect: bool = True,
) -> Tuple[Agent, dict]:
    """把本轮情绪状态 + RAG 检索到的相关记忆挂到 Agent 的**副本**上。

    返回副本而不是原对象：缓存对象是共享的，改原对象会在并发下串状态。

    ``user_text`` 是用户这一句原话：有它才做记忆检索（按"场景 + 语义"捞相关片段）。
    检索失败返回空串，不影响回复。

    ``include_affect=False``（工作模式）只挂 RAG 长期记忆，不带情绪/好感——
    工作模式里角色只负责干活，派生状态不参与任务判断。
    """
    state = affect.load_state(character, user_id) if include_affect else {}
    per_run = copy.copy(agent)
    context = affect.prompt_block(state) if include_affect else ""

    if user_text:
        try:
            recalled = rag.recall_block(
                str(character.get("id") or ""),
                user_text,
                top_k=int(get_settings().rag_top_k),
            )
        except Exception:  # noqa: BLE001 - RAG 挂掉不能影响聊天
            logger.exception("记忆检索失败（已忽略）")
            recalled = ""
        if recalled:
            context = f"{context}\n\n{recalled}"

    per_run.additional_context = context
    return per_run, state


def prepare(
    *,
    api_key: str,
    character: dict,
    book_title: str,
    user_name: str = "朋友",
    user_id: str = "",
) -> Tuple[Agent, dict]:
    """取（缓存的）角色 Agent + 注入本轮状态。便捷封装。"""
    agent = get_agent(
        api_key=api_key,
        character=character,
        book_title=book_title,
        user_name=user_name,
    )
    return attach_state(agent, character, user_id)


def memory_preview(character: dict, *, limit: Optional[int] = None) -> list[str]:
    """当前会进提示词的初始记忆（排查"角色像失忆"时用）。"""
    from core.config import get_settings
    from services.memory import agent_memory

    if limit is None:
        limit = int(get_settings().excerpt_chunks)
    return agent_memory.top(str(character.get("id") or ""), limit=max(1, limit))


def describe(character: dict, user_id: str) -> dict:
    """给日志/排查用的状态摘要（**不要直接返回给用户看数值**）。"""
    state = affect.load_state(character, user_id)
    return {
        "mood": state.get("emotion"),
        "turns": state.get("turns", 0),
        "prompt_block": affect.prompt_block(state),
    }