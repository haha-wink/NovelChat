"""角色 Agent 的对话工作流（LangGraph 版）。

## 为什么改成图

原来这套编排摊在 ``endpoints/chat.py`` 里，是一条 150 行的直线代码：
预处理 → 判断 → 推进状态 → 拼上下文 → 调模型 → 校验 → 落库。
能跑，但有三件事很难受：

1. **不好测**：想单独验证"规划层"，得把整个 HTTP 端点跑起来；
2. **不好改**：想加/跳一步（比如"危机时跳过 RAG 直接走安全话术"），
   只能在直线代码里插 if；
3. **看不见**：一轮里到底经过哪几步、每步产出什么，只能靠日志拼。

改成 LangGraph 之后，每一步是一个节点，节点之间传一个 ``TurnState``：

```
START → preprocess → classify → observe → recall → plan → generate → verify → persist → END
```

好处：每一步可单测、加删步骤只改边、流式输出直接在 ``generate`` 节点里往外写。

## 和原实现的对应关系

| 节点 | 原来在哪 |
|---|---|
| preprocess | `dialogue.preprocess` |
| classify | `dialogue.classify_with_judge` |
| observe | `affect.observe_user_message`（生成前推进状态） |
| recall | `rag.recall_block`（原来在 `character.attach_state` 里） |
| plan | `dialogue.plan` → 拼 `additional_context`（原来在端点里拼） |
| generate | `manager.get_agent` + `agent.arun`（**保留 Agno 调模型**，作为可注入的 ``llm``） |
| verify | `reply.strip_stage_directions` + `dialogue.verify` |
| persist | `add_chat_turn` / `maybe_distill` |

**为什么 generate 仍然走 Agno**：Agno 负责会话历史与流式解析，换掉它属于另一个量级的改动。
这里把它收敛成一个**可注入的 callable**（``state["llm"]``），
测试里传假的、将来换 LangChain 的 ChatModel 都只改这一个函数。
"""

from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from core.config import get_settings
from services import affect, agent_log, chat_memory, dialogue, rag
from services import reply as reply_mod

logger = logging.getLogger(__name__)

#: 生成函数：拿到 state，逐段产出文本（异步生成器）
LLMCallable = Callable[[dict], Any]


class TurnState(TypedDict, total=False):
    """一轮对话的状态。所有节点只读自己需要的字段、只写自己产出的字段。"""

    # ---- 输入（调用方填） ----
    user_id: str
    user_name: str
    character: dict
    book_title: str
    shelf_id: str
    raw_text: str
    image_urls: list[str]
    api_key: str
    model: str
    #: 群聊里额外拼给模型的上下文（单聊为空）
    extra_context: str
    #: 是否落库（群聊由引擎统一落库，可以关掉）
    persist: bool
    #: Agno 的会话 id（落库用）
    session_id: str

    # ---- 中间结果（节点写） ----
    text: str
    signals: Any
    plan: Any
    affect_state: dict
    memory_block: str
    state_block: str
    context_block: str
    reply: str
    notes: list[str]


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

def preprocess_node(state: TurnState) -> dict:
    """① 输入预处理。"""
    text = dialogue.preprocess(state.get("raw_text") or "")
    return {"text": text}


async def classify_node(state: TurnState) -> dict:
    """② 情绪 & 意图识别（规则打底 + 可选判官）。"""
    settings = get_settings()
    signals = await dialogue.classify_with_judge(
        state.get("text") or "",
        mode=settings.dialogue_judge_mode,
        api_key=state.get("api_key") or "",
        model=(settings.dialogue_judge_model or settings.deepseek_model),
        timeout=float(settings.dialogue_judge_timeout),
    )
    return {"signals": signals}


async def observe_node(state: TurnState) -> dict:
    """③ Agent 状态调度：先按用户这句话推进角色情绪，再生成。"""
    character = state.get("character") or {}
    try:
        affect_state = await asyncio.to_thread(
            functools.partial(
                affect.observe_user_message,
                character=character,
                user_id=state.get("user_id") or "",
                text=state.get("text") or "",
                image_count=len(state.get("image_urls") or []),
            )
        ) or {}
    except Exception:  # noqa: BLE001 - 情绪失败不能影响聊天
        logger.exception("情绪更新失败（已忽略）")
        affect_state = {}
    return {"affect_state": affect_state}


async def recall_node(state: TurnState) -> dict:
    """④ 记忆检索（RAG）：按用户这句话捞相关往事。"""
    character = state.get("character") or {}
    text = state.get("text") or ""
    block = ""
    if text:
        try:
            block = await asyncio.to_thread(
                functools.partial(
                    rag.recall_block,
                    str(character.get("id") or ""),
                    text,
                    top_k=int(get_settings().rag_top_k),
                )
            )
        except Exception:  # noqa: BLE001 - 检索失败不能影响回复
            logger.exception("记忆检索失败（已忽略）")
            block = ""
    return {"memory_block": block}


async def plan_node(state: TurnState) -> dict:
    """⑤ 规划层：算 SOP，并把"易变上下文"拼成一块。

    这里产出的是给 Agent 的 ``additional_context``：
    此刻状态 + 相关记忆 + 本轮 SOP + 群聊上下文。
    """
    signals = state.get("signals") or dialogue.TurnSignals()
    turn_plan = dialogue.plan(signals, character=state.get("character"))
    try:
        loaded = await asyncio.to_thread(
            functools.partial(
                affect.load_state,
                state.get("character") or {},
                state.get("user_id") or "",
            )
        )
        state_block = affect.prompt_block(loaded)
    except Exception:  # noqa: BLE001
        logger.exception("读情绪状态失败（已忽略）")
        state_block = ""

    parts = [item for item in (state_block, state.get("memory_block"), turn_plan.context_block()) if item]
    extra = (state.get("extra_context") or "").strip()
    if extra:
        parts.append(extra)

    return {
        "plan": turn_plan,
        "state_block": state_block,
        "context_block": "\n\n".join(parts),
    }


async def generate_node(state: TurnState, config: RunnableConfig) -> dict:
    """⑥ 生成回复：调用注入的 llm，逐段写进自定义流。

    ``llm`` 走 ``config["configurable"]`` 而不是 state——checkpointer 只序列化 state，
    而函数不可序列化。顺带的好处是：断流后 resume 只要给同一份 config 就行。
    """
    llm: Optional[LLMCallable] = (config.get("configurable") or {}).get("llm")
    if llm is None:
        raise RuntimeError("缺少生成函数：config.configurable.llm 没有注入")

    writer = get_stream_writer()
    pieces: list[str] = []
    async for piece in llm(state):
        if not piece:
            continue
        pieces.append(piece)
        writer({"type": "delta", "text": piece})
    return {"reply": "".join(pieces).strip()}


def verify_node(state: TurnState) -> dict:
    """⑦ 后置校验：去动作旁白 → 安全兜底 → 去说教 → 危机补充。"""
    writer = get_stream_writer()
    raw = state.get("reply") or ""
    clean = reply_mod.strip_stage_directions(raw)
    turn_plan = state.get("plan") or dialogue.plan(dialogue.TurnSignals())
    verdict = dialogue.verify(clean, turn_plan)

    if verdict.notes:
        character = state.get("character") or {}
        agent_log.log(
            "reply_verified",
            owner_type="character",
            owner_id=str(character.get("id") or ""),
            shelf_id=state.get("shelf_id") or "",
            user_id=state.get("user_id") or "",
            severity="debug",
            payload={"notes": verdict.notes},
        )

    text = verdict.text or clean
    if text != raw:
        writer({"type": "final", "text": text})
    return {"reply": text, "notes": verdict.notes}


async def persist_node(state: TurnState, config: RunnableConfig) -> dict:
    """⑧ 记忆写入：两条流水 + （必要时）蒸馏成长期记忆。"""
    if not state.get("persist", True):
        return {}

    from core import database as db

    shelf_id = state.get("shelf_id") or ""
    if not shelf_id:
        return {}

    character = state.get("character") or {}
    signals = state.get("signals")
    reply = state.get("reply") or ""

    session_id = state.get("session_id") or ""

    def _write() -> None:
        db.add_chat_turn(
            shelf_id=shelf_id,
            user_id=state.get("user_id") or "",
            role="user",
            content=state.get("text") or "",
            character_id=str(character.get("id") or ""),
            session_id=session_id,
            images=list(state.get("image_urls") or []),
            intent=(signals.intent if signals else ""),
            emotion=(signals.emotion if signals else ""),
        )
        if reply:
            db.add_chat_turn(
                shelf_id=shelf_id,
                user_id=state.get("user_id") or "",
                role="assistant",
                content=reply,
                character_id=str(character.get("id") or ""),
                session_id=session_id,
                speaker_name=str(character.get("name") or ""),
            )

    try:
        await asyncio.to_thread(_write)
    except Exception:  # noqa: BLE001
        logger.exception("记录对话流水失败（已忽略）")

    # 条数过阈值就顺手蒸馏一半
    try:
        await asyncio.to_thread(
            functools.partial(
                chat_memory.maybe_distill,
                shelf_id,
                str(character.get("id") or ""),
                summarizer=(config.get("configurable") or {}).get("summarizer"),
                user_id=state.get("user_id") or "",
                character_name=str(character.get("name") or ""),
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception("对话蒸馏失败（已忽略）")

    return {}


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def build_turn_graph(checkpointer=None):
    """编译一轮对话的状态图（进程内复用，编译一次即可）。

    ``checkpointer``：给每个 thread 存一份状态快照。作用只有一个——
    **流式生成到一半连接断了，能用同一个 thread_id 从断点续上**，不用从头再问一遍。
    """
    graph = StateGraph(TurnState)
    graph.add_node("preprocess", preprocess_node)
    graph.add_node("classify", classify_node)
    graph.add_node("observe", observe_node)
    graph.add_node("recall", recall_node)
    graph.add_node("plan", plan_node)
    graph.add_node("generate", generate_node)
    graph.add_node("verify", verify_node)
    graph.add_node("persist", persist_node)

    graph.add_edge(START, "preprocess")
    graph.add_edge("preprocess", "classify")
    graph.add_edge("classify", "observe")
    graph.add_edge("observe", "recall")
    graph.add_edge("recall", "plan")
    graph.add_edge("plan", "generate")
    graph.add_edge("generate", "verify")
    graph.add_edge("verify", "persist")
    graph.add_edge("persist", END)

    return graph.compile(checkpointer=checkpointer)


#: 进程内单例（编译有开销，但图是无状态的，可安全复用）
_TURN_GRAPH = None

#: 断点快照的存储。启动时换成 SQLite（进程重启也能续），
#: 没初始化时退回内存（测试、脚本直接用图的情况）。
_CHECKPOINTER = None
_SAVER_CM = None


def get_checkpointer():
    """没初始化持久化后端时用的内存版。"""
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        from langgraph.checkpoint.memory import InMemorySaver

        _CHECKPOINTER = InMemorySaver()
    return _CHECKPOINTER


async def init_checkpointer(path=None):
    """启动时调用：把断点存进 SQLite，重启后还能续跑。

    返回实际使用的数据库路径（日志用）。
    """
    global _TURN_GRAPH, _CHECKPOINTER, _SAVER_CM

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    settings = get_settings()
    db_file = Path(path) if path else (settings.workspace_path / "graph_checkpoints.db")
    db_file.parent.mkdir(parents=True, exist_ok=True)

    # from_conn_string 是个异步上下文管理器，这里手动 enter 让它活到进程结束
    _SAVER_CM = AsyncSqliteSaver.from_conn_string(str(db_file))
    _CHECKPOINTER = await _SAVER_CM.__aenter__()
    _TURN_GRAPH = build_turn_graph(_CHECKPOINTER)

    try:
        await prune_checkpoints(_CHECKPOINTER, days=int(settings.graph_checkpoint_retention_days))
    except Exception:  # noqa: BLE001 - 清理失败不影响启动
        logger.exception("清理对话断点失败（已忽略）")

    logger.info("对话断点已启用（SQLite）：%s", db_file)
    return db_file


async def close_checkpointer() -> None:
    """进程退出时释放连接。"""
    global _SAVER_CM, _CHECKPOINTER, _TURN_GRAPH
    if _SAVER_CM is not None:
        try:
            await _SAVER_CM.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            logger.exception("关闭断点数据库失败（已忽略）")
    _SAVER_CM = None
    _CHECKPOINTER = None
    _TURN_GRAPH = None


async def prune_checkpoints(saver, *, days: int = 7) -> int:
    """删掉超过保留期的断点，返回删掉的线程数。

    一轮对话跑完，它的快照就没用了（下次是全新的 turn_id），
    不清理的话这张表会一直长。

    注意：``checkpoints`` 表里没有时间列——时间戳在 checkpoint 的 msgpack 体里，
    所以得先解出来再判断，不能一句 SQL 删完。
    """
    if days <= 0:
        return 0

    from datetime import datetime, timedelta, timezone

    conn = getattr(saver, "conn", None)
    serde = getattr(saver, "serde", None)
    if conn is None or serde is None:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    newest: dict[str, datetime] = {}

    # 全新的库还没有这张表（首次启动必然如此），别打成错误
    try:
        check = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoints'"
        )
        if not await check.fetchone():
            return 0
    except Exception:  # noqa: BLE001
        return 0

    try:
        cursor = await conn.execute("SELECT thread_id, type, checkpoint FROM checkpoints")
        for thread_id, ctype, blob in await cursor.fetchall():
            try:
                payload = serde.loads_typed((ctype, blob))
                raw_ts = payload.get("ts") if isinstance(payload, dict) else None
                if not raw_ts:
                    continue
                stamp = datetime.fromisoformat(str(raw_ts))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001 - 解不开就跳过，别误删
                continue
            current = newest.get(thread_id)
            if current is None or stamp > current:
                newest[thread_id] = stamp
    except Exception:  # noqa: BLE001
        logger.exception("读取断点时间失败（跳过清理）")
        return 0

    stale = [thread_id for thread_id, stamp in newest.items() if stamp < cutoff]
    if not stale:
        return 0

    removed = 0
    for thread_id in stale:
        try:
            await conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
            await conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
            removed += 1
        except Exception:  # noqa: BLE001
            logger.exception("删除断点失败：%s", thread_id)
    try:
        await conn.commit()
    except Exception:  # noqa: BLE001
        logger.exception("提交断点清理失败（已忽略）")

    if removed:
        logger.info("清理过期对话断点：%d 个线程（保留 %d 天）", removed, days)
    return removed


def get_turn_graph():
    global _TURN_GRAPH
    if _TURN_GRAPH is None:
        _TURN_GRAPH = build_turn_graph(get_checkpointer())
    return _TURN_GRAPH


def describe() -> dict:
    """给"/api/health"或调试用的图结构摘要。"""
    graph = get_turn_graph()
    return {
        "engine": "langgraph",
        "nodes": [
            "preprocess", "classify", "observe", "recall",
            "plan", "generate", "verify", "persist",
        ],
        "compiled": graph is not None,
    }