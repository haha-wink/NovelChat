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
import operator
from typing import Annotated, Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from core import database as db
from services.dialogue import dialogue, expression
from services.dialogue import reply as reply_mod

logger = logging.getLogger(__name__)


def _keep_last(left: str, right: str) -> str:
    """并行写入时保留最后一次非空值。"""
    return right or left


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
    #: 本轮用户发言的判断结果（情绪/强度/意图/危机），选人靠它
    signals: Any
    rng: Any
    generate: Any          # (member, transcript) -> AsyncIterator[str]

    # ---- 中间 ----
    names: list[str]
    mentions: list[str]
    #: 用 reducer 合并：并行发言时每个节点只追加自己那条
    transcript: Annotated[list, operator.add]
    spoken: Annotated[list, operator.add]
    pending_forced: Annotated[list, operator.add]
    #: 并行时"谁最后说话"没有确定答案，取最后写入的那个即可（只用于选人冷却加权）
    last_speaker_id: Annotated[str, _keep_last]
    round_no: int
    queue: list[dict]
    #: Send 分叉时带下去的"这个人"
    current: dict
    #: 本轮的时间问候（prepare 算好，pick 指派给人）
    greeting: dict
    #: 当前对话 id（一个书架可以有多个对话）
    conversation_id: str
    #: 本轮模式：chat / work
    mode: str
    #: 本轮技能产出的素材块（群聊一轮跑一次，供所有发言人参考）
    skill_block: str
    #: 本轮命中的技能名
    skill_name: str
    #: 本轮每条发言（并行写入，聚合节点用来查重）
    replies: Annotated[list, operator.add]
    #: 被判定重复而折叠掉的（character_id 列表）
    dropped: Annotated[list, operator.add]
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

    # 【时间问候】必须在写用户消息之前算：写完就分不清"这条是不是刚发的"，
    # 会每轮都判成跨时段（用户看到的就是每次回复都带一句"傍晚了。"）
    greeting = _decide_greeting(shelf_id)

    # 没有对话就先建一个（老前端不带 conversation_id 时也能用）
    conversation_id = str(state.get("conversation_id") or "")
    if not conversation_id:
        conversation = db.ensure_default_conversation(
            shelf_id, state.get("user_id") or ""
        )
        conversation_id = conversation["id"]

    # 【技能层】群聊一轮跑一次：命中技能后把素材放进 state，
    # 所有发言人共享（否则工作模式在群里等于没开）
    skill_block, skill_name, skill_jobs = _run_skill(state, shelf_id)
    for job in skill_jobs:
        _emit({"type": "skill_job", "skill": skill_name, **job})

    plan = state.get("dialogue_plan")
    db.add_chat_turn(
        shelf_id=shelf_id,
        conversation_id=conversation_id,
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
        "finished": False,
        "greeting": greeting,
        "conversation_id": conversation_id,
        "skill_block": skill_block,
        "skill_name": skill_name,
    }


async def pick_node(state: GroupState) -> dict:
    """选人：按「用户这句话需要什么样的人」来选，而不是「谁话多」。

    第一轮走完整的需求分析（情绪 / 强度 / 意图 -> 人数 + 性格适配）；
    后续轮次是被 @ 出来的追问，只让被点到的人接。
    """
    from agent import group as group_mod
    from services.dialogue import dispatch

    options = state.get("options") or {}
    round_no = int(state.get("round_no") or 0) + 1
    members = state.get("members") or []
    signals = state.get("signals")
    text = state.get("text") or ""
    pending = [
        name for name in (state.get("pending_forced") or [])
        if name not in (state.get("spoken") or [])
    ]

    # 第二轮起：只让被 @ 的人接，不再引入新人
    if round_no > 1 and pending:
        by_name = {str(m.get("name") or ""): m for m in members}
        queue = [
            {
                "character_id": str(by_name[name].get("character_id") or ""),
                "name": name,
                "score": 1.0,
                "forced": True,
                "reason": "被角色 @ 追问",
                "emotion": str(by_name[name].get("emotion") or "平静"),
                "valence": float(by_name[name].get("valence") or 0.0),
                "arousal": float(by_name[name].get("arousal") or 0.35),
            }
            for name in pending
            if name in by_name
        ]
        if not queue:
            return {"finished": True, "queue": [], "round_no": round_no - 1}
        # 第二轮也要分工，否则追问轮又会几个人说同一句
        from services.dialogue import angles as _angles

        for index, (item, angle) in enumerate(zip(
            queue,
            _angles.assign(
                [next((m for m in members if m["name"] == row["name"]), row) for row in queue],
                crisis=bool(getattr(signals, "crisis", False)) if signals else False,
            ),
        )):
            queue[index].update({
                "angle": angle.key,
                "angle_name": angle.name,
                "angle_instruction": angle.instruction,
            })
        _emit({"type": "round_start", "round": round_no})
        return {"queue": queue, "round_no": round_no, "pending_forced": [], "finished": False}

    emotion = getattr(signals, "emotion", "平静") if signals else "平静"
    intent = getattr(signals, "intent", "chat") if signals else "chat"
    intensity = float(getattr(signals, "intensity", 0.0) if signals else 0.0)
    crisis = bool(getattr(signals, "crisis", False) if signals else False)
    mentions = state.get("mentions") or []

    # 谁在等你回答：只看最后一段连续的角色发言
    awaiting: list[dict] = []
    try:
        from services.memory import rag

        recent = db.list_chat_turns(state.get("shelf_id") or "", limit=12)
        # 字面能对上就不用向量；对不上时用语义补（"我不烧了" -> "有没有发烧"）
        awaiting = dispatch.find_awaiting(recent, text=text, semantic=rag.similarity)
    except Exception:  # noqa: BLE001 - 读不到历史就按常规选人
        logger.exception("查找待回答的问题失败（已忽略）")

    decision = dispatch.select(
        members,
        text=text,
        emotion=emotion,
        intent=intent,
        intensity=intensity,
        crisis=crisis,
        mentions=mentions,
        explicit_names=pending,
        all_called=group_mod.MENTION_ALL in mentions,
        last_speaker_id=state.get("last_speaker_id") or None,
        cooldown_seconds=int(options.get("cooldown_seconds", 45)),
        rng=state.get("rng"),
        relevance=lambda member: group_mod._relevance(member, text),
        awaiting=awaiting,
    )

    if not decision.picks:
        return {"finished": True, "queue": [], "round_no": round_no - 1}

    _emit({"type": "round_start", "round": round_no})
    # 【预分配分工】每人一个角度，避免四个人说同一句"先吃饭"
    from services.dialogue import angles

    assigned = angles.assign(
        [
            {"name": pick.name, **factor}
            for pick, factor in zip(
                decision.picks,
                [next((m for m in members if m["name"] == pick.name), {})
                 for pick in decision.picks],
            )
        ],
        crisis=crisis,
    )

    awaiting_map = {item["character_id"]: item for item in awaiting}
    queue = [
        {
            "character_id": pick.character_id,
            "name": pick.name,
            "score": pick.score,
            "forced": pick.forced,
            "reason": pick.reason,
            "emotion": pick.emotion,
            "valence": pick.valence,
            "arousal": pick.arousal,
            "fit": pick.fit,
            #: 他上一轮问过的问题（在等他回答时才有）
            "asked": (awaiting_map.get(pick.character_id) or {}).get("question", ""),
            "asked_match": (awaiting_map.get(pick.character_id) or {}).get("match", 0.0),
            #: 本轮分工（共情 / 方案 / 补充 / 轻松 / 追问）
            "angle": assigned[index].key,
            "angle_name": assigned[index].name,
            "angle_instruction": assigned[index].instruction,
        }
        for index, pick in enumerate(decision.picks)
    ]
    _emit({
        "type": "dispatch",
        "emotion": decision.emotion,
        "intent": decision.intent,
        "intensity": decision.intensity,
        "target": decision.target,
        "picked": [pick.name for pick in decision.picks],
        "awaiting": [item.get("name") for item in awaiting],
    })

    # 【时间工具】该不该打招呼 / 劝人休息：由随机一个发言人带出来
    greeting = _prepare_greeting(state, queue)
    if greeting:
        _emit({
            "type": "greeting",
            "speaker": greeting["speaker"],
            "text": greeting["text"],
            "period": greeting.get("period"),
            "late_night": greeting.get("late_night"),
            "reason": greeting.get("reason"),
        })

    return {"queue": queue, "round_no": round_no, "pending_forced": [], "finished": False}


def _skill_jobs(result) -> list[dict]:
    """从技能执行结果里挑出"起了任务"的工具输出（目前就是动画）。"""
    jobs = []
    for output in getattr(result, "outputs", []) or []:
        if isinstance(output, dict) and output.get("ok") and output.get("job_id"):
            jobs.append({
                "job_id": str(output["job_id"]),
                "prompt": str(output.get("prompt") or ""),
                "stage": str(output.get("stage") or ""),
            })
    return jobs


def _run_skill(state: GroupState, shelf_id: str) -> tuple[str, str, list[dict]]:
    """按当前模式跑一次技能层，返回 (素材块, 技能名, 起了的任务)。"""
    import skills

    signals = state.get("signals")
    if signals is None:
        return "", "", []
    payload = {
        "text": state.get("text") or "",
        "emotion": getattr(signals, "emotion", "平静"),
        "intent": getattr(signals, "intent", "chat"),
        "intensity": getattr(signals, "intensity", 0.0),
        "crisis": getattr(signals, "crisis", False),
    }
    mode = str(state.get("mode") or "chat")
    try:
        result = skills.run_for(payload, mode=mode,
                               extra={"user_id": state.get("user_id") or ""})
    except Exception:  # noqa: BLE001 - 技能失败不能挡住群聊
        logger.exception("群聊技能执行失败（已忽略）")
        return "", "", []

    if result is None:
        return "", "", []
    block = result.as_block()
    if block:
        logger.info("群聊技能：%s（%d 条素材，模式 %s）",
                    result.skill, len(result.materials), mode)
    return block, result.skill, _skill_jobs(result)


def _decide_greeting(shelf_id: str) -> dict:
    """跑 greeting_check 工具，拿到"该不该问候"。"""
    try:
        from core.tools import protocol as tool_protocol
        from services.runtime import time_tools  # noqa: F401 - 导入即注册

        result = tool_protocol.execute("greeting_check", {"shelf_id": shelf_id})
    except Exception:  # noqa: BLE001 - 问候是锦上添花
        logger.exception("时间问候判断失败（已忽略）")
        return {}
    if not result.get("ok") or not result.get("text"):
        return {}

    reasons = []
    if result.get("need_time_greeting"):
        reasons.append("跨时段")
    if result.get("need_fatigue_greeting"):
        reasons.append("深夜" if result.get("late_night") else "聊太久")
    return {
        "text": result["text"],
        "period": result.get("period"),
        "late_night": result.get("late_night"),
        "reason": "+".join(reasons) or "问候",
    }


def _prepare_greeting(state: GroupState, queue: list[dict]) -> dict:
    """跑 greeting_check 工具，命中就把问候挂到随机一个发言人身上。

    问候语由角色自己的嘴说出来，所以这里只标记 ``queue[i]["greeting"]``，
    真正的拼接发生在 ``speak_node``。
    """
    if not queue:
        return {}
    greeting = state.get("greeting") or {}
    text = str(greeting.get("text") or "")
    if not text:
        return {}

    # 随机挑一个人把问候带出来（用户要求：不用固定人选）
    rng = state.get("rng")
    index = rng.randrange(len(queue)) if rng is not None else 0
    queue[index]["greeting"] = text
    return {
        "speaker": queue[index]["name"],
        "text": text,
        "period": greeting.get("period"),
        "late_night": greeting.get("late_night"),
        "reason": greeting.get("reason") or "问候",
    }


async def speak_node(state: GroupState) -> dict:
    """让队列里的下一个人说话：生成 → 校验 → 落库 → 贴纸。"""
    from agent import group as group_mod

    # 并行分叉时，每个人由 Send 把自己的那条塞进 current
    speaker = state.get("current") or {}
    if not speaker:
        return {}
    members = state.get("members") or []
    member = next(
        (m for m in members if str(m.get("character_id")) == speaker["character_id"]),
        None,
    )
    if member is None:
        return {}

    _emit({
        "type": "speaker_start",
        "character_id": speaker["character_id"],
        "name": speaker["name"],
        "avatar": member.get("avatar") or "🎭",
        "avatar_url": member.get("avatar_url"),
        "round": state.get("round_no") or 1,
        "forced": speaker.get("forced", False),
        "reason": speaker.get("reason", ""),
        "fit": speaker.get("fit"),
        "angle": speaker.get("angle_name"),
    })

    transcript = list(state.get("transcript") or [])
    # 这个角色上一轮问过问题、而这句像是在回答他 -> 先把"回答"交回给他
    asked = str(speaker.get("asked") or "")
    hint = ""
    if asked and float(speaker.get("asked_match") or 0) > 0:
        hint = (
            f"你上一轮问过对方：「{asked}」\n"
            f"对方刚刚回的是：「{state.get('text') or ''}」\n"
            "先接住这个回答（确认、放心、或继续问一句），不要重新问一遍同一个问题。"
        )
    member_view = {**member, "_hint": hint}
    skill_block = str(state.get("skill_block") or "")
    if skill_block:
        member_view["_skill_block"] = skill_block
    angle = {
        "key": speaker.get("angle") or "",
        "name": speaker.get("angle_name") or "",
        "instruction": speaker.get("angle_instruction") or "",
    }
    if angle["name"]:
        member_view["_angle"] = angle

    pieces: list[str] = []
    generate = state.get("generate")
    try:
        # 不逐段推给前端：群聊要等聚合查重之后再一次性发出去，
        # 否则重复内容会先显示、再被折叠，用户会看到闪烁
        async for piece in generate(member_view, transcript):
            if not piece:
                continue
            pieces.append(piece)
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
        return {}

    plan = state.get("dialogue_plan")
    if plan is not None:
        verdict = dialogue.verify(reply, plan)
        if verdict.notes:
            from services.ops import agent_log

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

    # 时间问候：由这个人带一句（拼在回复最前面，不额外发一条消息）
    greeting = str(speaker.get("greeting") or "")
    if greeting and greeting not in reply:
        reply = f"{greeting}{reply}"

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
        conversation_id=str(state.get("conversation_id") or ""),
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

    # 角色 @ 了别人 -> 下一轮必答（用 reducer 合并，并行也不丢）
    spoken_now = set(state.get("spoken") or [])
    forced = [
        name for name in reply_mentions
        if name != group_mod.MENTION_ALL and name not in spoken_now
    ]

    return {
        "transcript": [{"speaker": speaker["name"], "text": reply}],
        "spoken": [speaker["name"]],
        "last_speaker_id": speaker["character_id"],
        "pending_forced": forced,
        # 聚合节点要用：本轮所有回复
        "replies": [{
            "character_id": speaker["character_id"],
            "name": speaker["name"],
            "text": reply,
            "angle": angle["name"],
        }],
    }


async def dedupe_node(state: GroupState) -> dict:
    """聚合：把内容重复的回复折叠掉（并行生成的兜底）。

    分工（angle）已经在提示词里做了第一道防重；这里用向量相似度兜底——
    万一两个人还是说了同一件事，只保留信息量更大的那条。

    已经被流式推到前端的消息不能"撤回"，所以这里发一个 ``speaker_drop``
    事件让前端折叠（前端收到后把那条气泡移除）。
    """
    from services.memory import rag
    from core.config import get_settings

    replies = list(state.get("replies") or [])
    if len(replies) < 2:
        return {}

    threshold = float(getattr(get_settings(), "group_duplicate_threshold", 0.78))
    kept: list[dict] = []
    dropped: list[dict] = []

    for item in replies:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        duplicate_of = None
        for previous in kept:
            score = _similarity(text, str(previous.get("text") or ""), rag.similarity)
            if score is not None and score >= threshold:
                duplicate_of = previous
                break
        if duplicate_of is None:
            kept.append(item)
            continue
        dropped.append({**item, "similar_to": duplicate_of.get("name")})

    if not dropped:
        return {}

    for item in dropped:
        _emit({
            "type": "speaker_drop",
            "character_id": item.get("character_id"),
            "name": item.get("name"),
            "similar_to": item.get("similar_to"),
            "reason": "content_duplicate",
        })
    logger.info(
        "群聊去重：保留 %d 条，折叠 %d 条（%s）",
        len(kept), len(dropped), "、".join(str(item.get("name")) for item in dropped),
    )
    return {"dropped": [item.get("character_id") for item in dropped]}


def _similarity(left: str, right: str, semantic):
    """先看字面，差得远再算向量（省一次编码）。"""
    import difflib

    literal = difflib.SequenceMatcher(None, left, right).ratio()
    if literal >= 0.92:
        # 几乎一字不差，不用浪费一次编码
        return literal
    # 其余一律问向量：**取两者较大值**
    # （同义改写字面只有 0.13，光看字面会漏；反过来向量偶尔也会漏字面重复）
    try:
        score = semantic(left, right)
    except Exception:  # noqa: BLE001 - 向量算不出来就信字面
        return literal
    return max(literal, float(score or 0.0))


def fan_out(state: GroupState):
    """本轮要说话的人 -> 每人一个 Send，并行跑。

    这是 LangGraph 相对手写循环真正省事的地方：并发、合并、什么时候算"这一批跑完"，
    框架都替你管了；我们只描述"派给谁"。
    """
    queue = state.get("queue") or []
    if state.get("finished") or not queue:
        return "finish"
    return [
        Send("speak", {**state, "current": speaker})
        for speaker in queue
    ]


async def decide_node(state: GroupState) -> dict:
    """并行那批跑完之后：还有没有下一轮。"""
    return {}


def route_after_speak(state: GroupState) -> str:
    pending = [
        name for name in (state.get("pending_forced") or [])
        if name not in (state.get("spoken") or [])
    ]
    if not pending:
        return "finish"
    if int(state.get("round_no") or 0) >= int((state.get("options") or {}).get("max_rounds", 3)):
        return "finish"
    return "pick"


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
    # pick 之后用 Send 把本轮所有发言人**同时**派出去（fan-out）
    graph.add_conditional_edges(
        "pick", fan_out, {"speak": "speak", "finish": "finish"}
    )
    # 并行的那批都跑完后，统一判断要不要下一轮
    graph.add_edge("speak", "dedupe")
    graph.add_edge("dedupe", "decide")
    graph.add_node("decide", decide_node)
    graph.add_node("dedupe", dedupe_node)
    graph.add_conditional_edges(
        "decide", route_after_speak, {"pick": "pick", "finish": "finish"}
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
        "nodes": ["prepare", "pick", "speak", "dedupe", "finish"],
        "compiled": get_group_graph() is not None,
    }