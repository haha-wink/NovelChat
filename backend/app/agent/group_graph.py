"""群聊一轮的工作流（LangGraph 版）。

原来 ``group.run_turn`` 是一个 while + for 的双层循环：

```
while 还有轮次:
    选人（第一轮看 @，之后看有没有人 @ 别人）
    for 每个发言人:
        生成 → 去旁白 → 校验 → 落库 → 贴纸
    if 没人被 @ 就不再开新一轮
```

能跑，但同样有"不好测、不好改、看不见"的问题。改成图之后：

```
START → prepare → pick ─┬─(有人)─→ speak ─┬─(本批还有)→ speak
                        │                 ├─(还有轮次)→ pick
                        └─(没人)─→ finish ←┴─(结束)─────┘
                                             → END
```

* ``prepare`` 一次性做：成员加载、@全体唤醒、用户那句落库
* ``pick``   每轮选人（复用 ``group.select_speakers``，规则没动）
* ``speak``  一个发言人：生成、校验、落库、贴纸
* ``finish`` 汇总（``done`` 事件）

**和单聊图的关系**：单聊的 ``agent/graph.py`` 是"一个角色一轮"的图；
这里每个 ``speak`` 节点做的就是同一件事，只是生成函数由端点注入（群聊要带上下文）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from core import database as db
from services import dialogue, expression
from services import reply as reply_mod

logger = logging.getLogger(__name__)


class GroupState(TypedDict, total=False):
    # ---- 输入 ----
    shelf_id: str
    user_id: str
    text: str
    session_id: str
    members: list[dict]
    image_urls: list[str]
    options: dict
    dialogue_plan: Any
    rng: Any
    generate: Any          # (member, transcript) -> AsyncIterator[str]

    # ---- 中间 ----
    names: list[str]
    mentions: list[str]
    transcript: list[dict]
    spoken: list[str]
    last_speaker_id: str
    pending_forced: list[str]
    round_no: int
    queue: list[dict]
    cursor: int
    finished: bool


def _emit(payload: dict) -> None:
    """往自定义流里写一个事件。"""
    get_stream_writer()(payload)


async def prepare_node(state: GroupState) -> dict:
    """准备：成员、@ 解析、@全体唤醒、用户那句落库。"""
    from agent import group as group_mod

    shelf_id = state.get("shelf_id") or ""
    text = state.get("text") or ""
    members = state.get("members")
    if members is None:
        members = db.ensure_shelf_members(shelf_id)

    if not members:
        _emit({"type": "system", "text": "这个书架里还没有角色，先把书加进来吧"})
        _emit({"type": "done", "rounds": 0, "speakers": []})
        return {"finished": True}

    names = [str(m.get("name") or "") for m in members]
    mentions = group_mod.parse_mentions(text, set(names))

    # @全体成员 = 把所有人叫上线，再让所有在线成员各回一句。
    # 必须在选角之前，否则离线的人根本没资格被选中。
    if group_mod.MENTION_ALL in mentions:
        woken = db.set_all_members_online(shelf_id)
        if woken:
            members = db.ensure_shelf_members(shelf_id)
            names = [str(m.get("name") or "") for m in members]
            logger.info("被 @全体成员 叫上线：%d 人", woken)

    plan = state.get("dialogue_plan")
    db.add_chat_turn(
        shelf_id=shelf_id,
        user_id=state.get("user_id") or "",
        role="user",
        content=text,
        mentions=mentions,
        images=list(state.get("image_urls") or []),
        intent=(plan.signals.intent if plan else ""),
        emotion=(plan.signals.emotion if plan else ""),
        session_id=state.get("session_id") or "",
    )
    _emit({"type": "user", "text": text, "mentions": mentions})

    return {
        "members": members,
        "names": names,
        "mentions": mentions,
        "transcript": [{"speaker": "用户", "text": text}],
        "spoken": [],
        "last_speaker_id": "",
        "pending_forced": [],
        "round_no": 0,
        "queue": [],
        "cursor": 0,
        "finished": False,
    }


async def pick_node(state: GroupState) -> dict:
    """选人：这一轮谁说话。规则本身仍由 group.select_speakers 决定。"""
    from agent import group as group_mod

    options = state.get("options") or {}
    round_no = int(state.get("round_no") or 0) + 1
    members = state.get("members") or []

    speakers = group_mod.select_speakers(
        members,
        text=state.get("text") or "",
        mentions=(state.get("mentions") or []) if round_no == 1 else [],
        last_speaker_id=state.get("last_speaker_id") or None,
        max_speakers=int(options.get("max_speakers", 3)),
        threshold=float(options.get("threshold", 0.3)),
        all_threshold=float(options.get("all_threshold", 0.18)),
        cooldown_seconds=int(options.get("cooldown_seconds", 0)),
        explicit_names=list(state.get("pending_forced") or []),
        always_reply=bool(options.get("always_reply", True)),
        rng=state.get("rng"),
    )

    if not speakers:
        return {"finished": True, "queue": [], "cursor": 0, "round_no": round_no - 1}

    _emit({"type": "round_start", "round": round_no})
    queue = [
        {
            "character_id": item.character_id,
            "name": item.name,
            "score": item.score,
            "forced": item.forced,
            "reason": item.reason,
            "emotion": item.emotion,
            "valence": item.valence,
            "arousal": item.arousal,
        }
        for item in speakers
    ]
    return {
        "queue": queue,
        "cursor": 0,
        "round_no": round_no,
        "pending_forced": [],
        "finished": False,
    }


async def speak_node(state: GroupState) -> dict:
    """让队列里的下一个人说话：生成 → 校验 → 落库 → 贴纸。"""
    from agent import group as group_mod

    queue = state.get("queue") or []
    index = int(state.get("cursor") or 0)
    if index >= len(queue):
        return {"finished": True}

    speaker = queue[index]
    members = state.get("members") or []
    member = next(
        (m for m in members if str(m.get("character_id")) == speaker["character_id"]),
        None,
    )
    if member is None:
        return {"cursor": index + 1}

    _emit({
        "type": "speaker_start",
        "character_id": speaker["character_id"],
        "name": speaker["name"],
        "avatar": member.get("avatar") or "🎭",
        "avatar_url": member.get("avatar_url"),
        "round": state.get("round_no") or 1,
        "forced": speaker.get("forced", False),
        "reason": speaker.get("reason", ""),
    })

    transcript = list(state.get("transcript") or [])
    pieces: list[str] = []
    generate = state.get("generate")
    try:
        async for piece in generate(member, transcript):
            if not piece:
                continue
            pieces.append(piece)
            _emit({
                "type": "delta",
                "character_id": speaker["character_id"],
                "text": piece,
            })
    except Exception as exc:  # noqa: BLE001 - 一个人失败不该炸整个群
        logger.exception("群聊生成失败：%s", speaker["name"])
        _emit({
            "type": "error",
            "character_id": speaker["character_id"],
            "message": f"{speaker['name']} 没能说话：{exc}",
        })

    # 去动作旁白 → 后置校验（安全兜底 / 去说教 / 危机补充）。
    # 前端在 speaker_end 用最终文本覆盖流式内容，所以流不受影响。
    reply = reply_mod.strip_stage_directions("".join(pieces).strip())
    if not reply:
        return {"cursor": index + 1}

    plan = state.get("dialogue_plan")
    if plan is not None:
        verdict = dialogue.verify(reply, plan)
        if verdict.notes:
            from services import agent_log

            agent_log.log(
                "reply_verified",
                owner_type="character",
                owner_id=speaker["character_id"],
                shelf_id=state.get("shelf_id") or "",
                severity="debug",
                payload={"notes": verdict.notes, "scope": "group"},
            )
        if verdict.text:
            reply = verdict.text

    # 情绪推进：群里每个人的心情也被用户这句话影响（规则引擎，不发模型请求）
    mood_changed = False
    try:
        state_after = await asyncio.to_thread(
            group_mod.affect.observe_user_message,
            character=member,
            user_id=state.get("user_id") or "",
            text=state.get("text") or "",
            session_id=state.get("session_id") or "",
        )
        mood_changed = bool(
            state_after.get("previous_emotion")
            and state_after["previous_emotion"] != state_after.get("emotion")
        )
    except Exception:  # noqa: BLE001 - 情绪失败不能影响发言
        logger.exception("群聊情绪更新失败（已忽略）")

    reply_mentions = group_mod.parse_mentions(reply, set(state.get("names") or []))
    await asyncio.to_thread(
        db.add_chat_turn,
        shelf_id=state.get("shelf_id") or "",
        user_id=state.get("user_id") or "",
        role="assistant",
        character_id=speaker["character_id"],
        speaker_name=speaker["name"],
        content=reply,
        mentions=reply_mentions,
        round_no=state.get("round_no") or 1,
        session_id=state.get("session_id") or "",
    )
    db.record_member_spoke(state.get("shelf_id") or "", speaker["character_id"])

    sticker = expression.maybe_sticker(
        {
            "emotion": speaker.get("emotion") or "平静",
            "valence": speaker.get("valence") or 0.0,
            "arousal": speaker.get("arousal") or 0.35,
        },
        mood_changed=mood_changed,
        asked_about_mood=expression.asks_about_mood(state.get("text") or ""),
        rng=state.get("rng"),
    )
    _emit({
        "type": "speaker_end",
        "character_id": speaker["character_id"],
        "name": speaker["name"],
        "text": reply,
        "mentions": reply_mentions,
        "round": state.get("round_no") or 1,
        "sticker": sticker,
    })

    # 角色 @ 了别人 -> 下一轮必答
    pending = list(state.get("pending_forced") or [])
    spoken = list(state.get("spoken") or [])
    for name in reply_mentions:
        if name != group_mod.MENTION_ALL and name not in spoken and name not in pending:
            pending.append(name)

    spoken.append(speaker["name"])
    transcript.append({"speaker": speaker["name"], "text": reply})

    await asyncio.sleep(0)  # 让出控制权，保证流式体验

    return {
        "cursor": index + 1,
        "transcript": transcript,
        "spoken": spoken,
        "last_speaker_id": speaker["character_id"],
        "pending_forced": pending,
    }


def route_after_speak(state: GroupState) -> str:
    queue = state.get("queue") or []
    index = int(state.get("cursor") or 0)
    if index < len(queue):
        return "speak"
    if not (state.get("pending_forced") or []):
        return "finish"
    if int(state.get("round_no") or 0) >= int((state.get("options") or {}).get("max_rounds", 3)):
        return "finish"
    return "pick"


def route_after_pick(state: GroupState) -> str:
    return "finish" if state.get("finished") else "speak"


def route_after_prepare(state: GroupState) -> str:
    return "finish" if state.get("finished") else "pick"


async def finish_node(state: GroupState) -> dict:
    """收尾：发 done。"""
    if state.get("members"):
        _emit({
            "type": "done",
            "rounds": state.get("round_no") or 0,
            "speakers": list(state.get("spoken") or []),
        })
    return {"finished": True}


def build_group_graph():
    graph = StateGraph(GroupState)
    graph.add_node("prepare", prepare_node)
    graph.add_node("pick", pick_node)
    graph.add_node("speak", speak_node)
    graph.add_node("finish", finish_node)

    graph.add_edge(START, "prepare")
    graph.add_conditional_edges(
        "prepare", route_after_prepare, {"pick": "pick", "finish": "finish"}
    )
    graph.add_conditional_edges(
        "pick", route_after_pick, {"speak": "speak", "finish": "finish"}
    )
    graph.add_conditional_edges(
        "speak",
        route_after_speak,
        {"speak": "speak", "pick": "pick", "finish": "finish"},
    )
    graph.add_edge("finish", END)
    return graph.compile()


_GROUP_GRAPH = None


def get_group_graph():
    global _GROUP_GRAPH
    if _GROUP_GRAPH is None:
        _GROUP_GRAPH = build_group_graph()
    return _GROUP_GRAPH


def describe() -> dict:
    return {
        "engine": "langgraph",
        "nodes": ["prepare", "pick", "speak", "finish"],
        "compiled": get_group_graph() is not None,
    }