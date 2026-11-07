"""群聊引擎：谁说话、说几轮、被 @ 之后怎么接。

**一个书架 = 一个群**。每一回合的流程：

```
用户发言（可 @某人 / @全体成员）
   ↓ 解析 @
给每个在场成员算「发言意愿」分
   ↓ 过阈值 + 取 top-k
串行生成（必须串行：后一个人要看到前一个人刚说了什么）
   ↓ 角色话里 @ 了谁
下一轮由被 @ 的人必答
   ↓ 达到 max_rounds 或没人愿意说
结束
```

## 为什么拆成两半

* **纯逻辑**（``parse_mentions`` / ``profile_for`` / ``score_member`` / ``select_speakers``）
  不碰网络、不碰数据库，可以完全离线测；
* ``run_turn`` 只接受一个 ``generate`` 回调，测试时传假生成器即可，不必调模型。

## 成本红线

``max_speakers × max_rounds`` 就是单回合最多几次模型调用（默认 2×2=4）。
**无人过阈值就直接结束**——不为了热闹硬让角色说话，这是省钱的最后一道闸。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable, Optional

from core import database as db
from services import affect
from services import dialogue
from services import expression
from services import reply as reply_mod

logger = logging.getLogger(__name__)

#: @全体成员的几种写法
MENTION_ALL = "all"
ALL_MARKERS = ("@全体成员", "@所有人", "@全体", "@all", "@All")

#: 性格 -> (外向度, 果断度)。没命中就用默认值
SPEECH_RULES: tuple[tuple[tuple[str, ...], float, float], ...] = (
    (("热情", "开朗", "活泼", "话多", "健谈", "外向", "直爽"), 0.85, 0.70),
    (("温柔", "温和", "善良", "体贴", "耐心", "包容"), 0.60, 0.45),
    (("冷静", "克制", "理性", "沉稳", "镇定"), 0.45, 0.50),
    (("寡言", "沉默", "话少", "冷淡", "疏离", "冷漠"), 0.25, 0.35),
    (("多疑", "警惕", "戒备", "谨慎", "敏感"), 0.40, 0.55),
    (("暴躁", "易怒", "冲动", "急躁"), 0.80, 0.85),
    (("天真", "单纯", "烂漫"), 0.70, 0.50),
    (("傲慢", "自负", "高傲", "目中无人"), 0.65, 0.75),
)

DEFAULT_PROFILE = {"extraversion": 0.5, "assertiveness": 0.5, "initiative_floor": 0.2}

#: 分项权重（加起来 0.90，剩下 0.10 给噪声）
W_EXTRAVERSION = 0.30
W_RELEVANCE = 0.25
W_AROUSAL = 0.15
W_RELATION = 0.10
W_NOISE = 0.10

PENALTY_COOLDOWN = 0.20
PENALTY_REPEAT = 0.15

#: 被「@全体成员」点到时的加成。没有它的话，0.30×外向度 的基础分本来就够不着阈值，
#: @全体 等于白叫——只有最话痨的一两个能过线。
ALL_CALL_BOOST = 0.15


@dataclass
class Speaker:
    character_id: str
    name: str
    score: float
    forced: bool = False
    reason: str = ""
    emotion: str = "平静"
    valence: float = 0.0
    arousal: float = 0.35


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_mentions(text: str, names: set[str]) -> list[str]:
    """从一句话里解析 @。返回 ``["all"]`` 或角色名列表。

    长名字优先，并且**匹配后把片段消耗掉**——否则 ``@路明非`` 会同时命中 ``@路明``。
    """
    text = text or ""
    for marker in ALL_MARKERS:
        if marker in text:
            return [MENTION_ALL]

    working = text
    found: list[str] = []
    for name in sorted((n for n in names if n), key=len, reverse=True):
        for prefix in (f"@{name}", f"＠{name}"):
            if prefix in working:
                if name not in found:
                    found.append(name)
                working = working.replace(prefix, "@")  # 消耗掉，避免被更短的名字再次命中
    return found


def profile_for(character: dict) -> dict:
    """由性格推出 (外向度, 果断度, 主动开口下限)。命中第一条规则即返回。"""
    traits = " ".join(
        [
            " ".join(str(x) for x in (character.get("personality") or [])),
            str(character.get("role") or ""),
            str(character.get("speech_style") or ""),
        ]
    )
    for keywords, extraversion, assertiveness in SPEECH_RULES:
        if any(word in traits for word in keywords):
            return {
                "extraversion": extraversion,
                "assertiveness": assertiveness,
                "initiative_floor": round(0.25 * extraversion, 3),
            }
    return dict(DEFAULT_PROFILE)


def _relevance(member: dict, text: str) -> float:
    """用户这句话跟这个角色有多相关：点名 > 别名 > 无关。"""
    text = text or ""
    name = str(member.get("name") or "")
    if name and name in text:
        return 0.9
    for alias in member.get("aliases") or []:
        alias = str(alias or "")
        if alias and alias in text:
            return 0.7
    # 提到他关系里的人，也算沾边
    for relation in member.get("relationships") or []:
        other = str((relation or {}).get("with") or "")
        if other and other in text:
            return 0.4
    return 0.0


def _seconds_since(last_spoke_at: Optional[str], now: datetime) -> Optional[float]:
    if not last_spoke_at:
        return None
    try:
        moment = datetime.fromisoformat(last_spoke_at)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds()


def score_member(
    member: dict,
    *,
    text: str,
    last_speaker_id: Optional[str] = None,
    now: Optional[datetime] = None,
    cooldown_seconds: int = 45,
    rng: Optional[random.Random] = None,
) -> tuple[float, str]:
    """算一个成员的发言意愿分，返回 (分数, 理由)。"""
    now = now or _now()
    rng = rng or random
    profile = profile_for(member)
    raw_extraversion = member.get("extraversion")
    extraversion = (
        float(raw_extraversion) if raw_extraversion is not None else profile["extraversion"]
    )
    arousal = float(member.get("arousal") or 0.35)
    relevance = _relevance(member, text)

    score = (
        W_EXTRAVERSION * extraversion
        + W_RELEVANCE * relevance
        + W_AROUSAL * min(1.0, max(0.0, arousal))
        + W_NOISE * rng.uniform(-1.0, 1.0)
    )
    reasons = [f"外向{extraversion:.2f}"]

    if relevance >= 0.7:
        reasons.append("被点名")
        score += W_RELATION  # 相关的人更愿意接话

    gap = _seconds_since(member.get("last_spoke_at"), now)
    if gap is not None and gap < cooldown_seconds:
        score -= PENALTY_COOLDOWN
        reasons.append(f"冷却中({int(gap)}s)")

    if last_speaker_id and str(member.get("character_id")) == str(last_speaker_id):
        score -= PENALTY_REPEAT
        reasons.append("刚说过")

    return round(score, 3), " / ".join(reasons)


def select_speakers(
    members: list[dict],
    *,
    text: str,
    mentions: list[str],
    last_speaker_id: Optional[str] = None,
    max_speakers: int = 2,
    threshold: float = 0.55,
    all_threshold: float = 0.35,
    cooldown_seconds: int = 45,
    explicit_names: Optional[list[str]] = None,
    always_reply: bool = True,
    now: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
) -> list[Speaker]:
    """选出本轮发言的人。

    * 被 **显式 @** 的角色必答（跳阈值、跳冷却），按 @ 顺序排前面；
    * ``@全体成员`` 把阈值降到 ``all_threshold``，但仍受 ``max_speakers`` 限制；
    * 其余按分数排序，过阈值才入选；
    * ``always_reply=True`` 时，若没人过线，让**意愿最高**的那位兜底接一句——
      否则不 @ 就没人说话，群里会死寂（这是之前的真实反馈）。
    """
    now = now or _now()
    rng = rng or random
    max_speakers = max(1, int(max_speakers))

    usable = [
        m for m in members
        if str(m.get("status") or "online") in ("online", "active")
        and str(m.get("state") or "idle") != "muted"
    ]
    if not usable:
        return []

    by_name = {str(m.get("name") or ""): m for m in usable}
    effective_threshold = all_threshold if MENTION_ALL in mentions else threshold
    all_called = MENTION_ALL in mentions

    # 0) @全体成员 = 对每个人都单独 @ 了一遍：所有在线成员必答，
    #    不受 max_speakers 限制、也不看阈值（用户要的就是"都回一句"）。
    if all_called:
        everyone: list[Speaker] = []
        for member in usable:
            everyone.append(
                Speaker(
                    character_id=str(member.get("character_id") or ""),
                    name=str(member.get("name") or ""),
                    score=1.0,
                    forced=True,
                    reason="被@全体成员叫到",
                    emotion=str(member.get("emotion") or "平静"),
                    valence=float(member.get("valence") or 0.0),
                    arousal=float(member.get("arousal") or 0.35),
                )
            )
        return everyone

    # 1) 必答：显式 @ 的人（本轮传入的 + 上一轮角色 @ 出来的）
    forced_names: list[str] = []
    for name in list(explicit_names or []) + [m for m in mentions if m != MENTION_ALL]:
        if name in by_name and name not in forced_names:
            forced_names.append(name)

    chosen: list[Speaker] = []
    chosen_ids: set[str] = set()
    for name in forced_names:
        if len(chosen) >= max_speakers:
            break
        member = by_name[name]
        character_id = str(member.get("character_id") or "")
        if character_id in chosen_ids:
            continue
        chosen.append(
            Speaker(
                character_id=character_id,
                name=name,
                score=1.0,
                forced=True,
                reason="被 @",
                emotion=str(member.get("emotion") or "平静"),
                valence=float(member.get("valence") or 0.0),
                arousal=float(member.get("arousal") or 0.35),
            )
        )
        chosen_ids.add(character_id)

    # 2) 其余按意愿分排队
    scored: list[tuple[float, dict, str]] = []
    if len(chosen) < max_speakers:
        for member in usable:
            character_id = str(member.get("character_id") or "")
            if character_id in chosen_ids:
                continue
            value, reason = score_member(
                member,
                text=text,
                last_speaker_id=last_speaker_id,
                now=now,
                cooldown_seconds=cooldown_seconds,
                rng=rng,
            )
            if all_called:
                value = round(value + ALL_CALL_BOOST, 3)
                reason = f"{reason} / 被@全体"
            scored.append((value, member, reason))
        scored.sort(key=lambda item: item[0], reverse=True)
        for value, member, reason in scored:
            if len(chosen) >= max_speakers:
                break
            if value < effective_threshold:
                continue
            chosen.append(
                Speaker(
                    character_id=str(member.get("character_id") or ""),
                    name=str(member.get("name") or ""),
                    score=value,
                    reason=reason,
                    emotion=str(member.get("emotion") or "平静"),
                    valence=float(member.get("valence") or 0.0),
                    arousal=float(member.get("arousal") or 0.35),
                )
            )

    # 3) 兜底：一个都没过线时，最少也要有一个人接话
    if not chosen and always_reply and scored:
        value, member, reason = scored[0]
        chosen.append(
            Speaker(
                character_id=str(member.get("character_id") or ""),
                name=str(member.get("name") or ""),
                score=value,
                reason=f"{reason} / 兜底接话",
                emotion=str(member.get("emotion") or "平静"),
                valence=float(member.get("valence") or 0.0),
                arousal=float(member.get("arousal") or 0.35),
            )
        )
    return chosen


async def run_turn(
    *,
    shelf_id: str,
    user_id: str,
    text: str,
    generate: Callable[[dict, list[dict]], AsyncIterator[str]],
    session_id: str = "",
    members: Optional[list[dict]] = None,
    image_urls: Optional[list[str]] = None,
    dialogue_plan=None,
    tweaks: Optional[dict] = None,
    rng: Optional[random.Random] = None,
) -> AsyncIterator[dict]:
    """跑完一整个回合，逐条产出事件。

    ``generate(member, transcript)`` 由调用方提供：它负责构建 Agent 并流式产出文本片段。
    ``transcript`` 是本回合**已说过的话**（含用户那句），让后说话的人能接上。
    ``rng`` 可注入，测试时用固定种子保证可复现。
    """
    from core.config import get_settings

    settings = get_settings()
    options = {
        "max_speakers": settings.group_max_speakers,
        "max_rounds": settings.group_max_rounds,
        "threshold": settings.group_speak_threshold,
        "all_threshold": settings.group_all_threshold,
        "cooldown_seconds": settings.group_cooldown_seconds,
        "always_reply": settings.group_always_reply,
    }
    options.update(tweaks or {})
    rng = rng or random.Random()

    if members is None:
        members = db.ensure_shelf_members(shelf_id)
    if not members:
        yield {"type": "system", "text": "这个书架里还没有角色，先把书加进来吧"}
        yield {"type": "done", "rounds": 0, "speakers": []}
        return

    names = {str(m.get("name") or "") for m in members}
    mentions = parse_mentions(text, names)

    # @全体成员 = 把所有人叫上线（离线 -> 在线），再让所有在线成员都回一句。
    # 这一步必须在选角之前，否则离线的人根本没资格被选中。
    if MENTION_ALL in mentions:
        woken = db.set_all_members_online(shelf_id)
        if woken:
            members = db.ensure_shelf_members(shelf_id)
            logger.info("被 @全体成员 叫上线：%d 人", woken)

    db.add_chat_turn(
        shelf_id=shelf_id,
        user_id=user_id,
        role="user",
        content=text,
        mentions=mentions,
        images=list(image_urls or []),
        intent=(dialogue_plan.signals.intent if dialogue_plan else ""),
        emotion=(dialogue_plan.signals.emotion if dialogue_plan else ""),
    )
    yield {"type": "user", "text": text, "mentions": mentions}

    transcript: list[dict] = [{"speaker": "用户", "text": text}]
    spoken: list[str] = []
    last_speaker_id: Optional[str] = None
    pending_forced: list[str] = []
    round_no = 0

    while round_no < int(options["max_rounds"]):
        round_no += 1
        speakers = select_speakers(
            members,
            text=text,
            mentions=mentions if round_no == 1 else [],
            last_speaker_id=last_speaker_id,
            max_speakers=int(options["max_speakers"]),
            threshold=float(options["threshold"]),
            all_threshold=float(options["all_threshold"]),
            cooldown_seconds=int(options["cooldown_seconds"]),
            explicit_names=pending_forced,
            rng=rng,
        )
        pending_forced = []
        if not speakers:
            round_no -= 1
            break

        yield {"type": "round_start", "round": round_no}

        for speaker in speakers:
            member = next(
                (m for m in members if str(m.get("character_id")) == speaker.character_id),
                None,
            )
            if member is None:
                continue

            yield {
                "type": "speaker_start",
                "character_id": speaker.character_id,
                "name": speaker.name,
                "avatar": member.get("avatar") or "🎭",
                "avatar_url": member.get("avatar_url"),
                "round": round_no,
                "forced": speaker.forced,
                "reason": speaker.reason,
            }

            pieces: list[str] = []
            try:
                async for piece in generate(member, transcript):
                    if not piece:
                        continue
                    pieces.append(piece)
                    yield {
                        "type": "delta",
                        "character_id": speaker.character_id,
                        "text": piece,
                    }
            except Exception as exc:  # noqa: BLE001 - 一个人失败不该炸整个群
                logger.exception("群聊生成失败：%s", speaker.name)
                yield {
                    "type": "error",
                    "character_id": speaker.character_id,
                    "message": f"{speaker.name} 没能说话：{exc}",
                }

            # 去掉动作/神态旁白（模型偶尔还是会写）——只影响最终文本，
            # 前端在 speaker_end 时用这个文本覆盖流式内容。
            reply = reply_mod.strip_stage_directions("".join(pieces).strip())
            if not reply:
                continue

            # 【后置校验】安全兜底 / 去说教 / 危机补充。
            # 前端在 speaker_end 会用这里的最终文本覆盖流式内容，所以流不受影响。
            if dialogue_plan is not None:
                verdict = dialogue.verify(reply, dialogue_plan)
                if verdict.notes:
                    agent_log.log(
                        "reply_verified",
                        owner_type="character",
                        owner_id=speaker.character_id,
                        shelf_id=shelf_id,
                        severity="debug",
                        payload={"notes": verdict.notes, "scope": "group"},
                    )
                if verdict.text:
                    reply = verdict.text

            # 情绪推进：群聊里每个人的心情也被用户这句话影响（规则引擎，不发模型请求）
            mood_changed = False
            try:
                state_after = await asyncio.to_thread(
                    affect.observe_user_message,
                    character=member,
                    user_id=user_id,
                    text=text,
                    session_id=session_id,
                )
                mood_changed = bool(
                    state_after.get("previous_emotion")
                    and state_after["previous_emotion"] != state_after.get("emotion")
                )
            except Exception:  # noqa: BLE001 - 情绪失败不能影响发言
                logger.exception("群聊情绪更新失败（已忽略）")

            reply_mentions = parse_mentions(reply, names)
            db.add_chat_turn(
                shelf_id=shelf_id,
                user_id=user_id,
                role="assistant",
                character_id=speaker.character_id,
                speaker_name=speaker.name,
                content=reply,
                mentions=reply_mentions,
                round_no=round_no,
                session_id=session_id,
            )
            db.record_member_spoke(shelf_id, speaker.character_id)

            transcript.append({"speaker": speaker.name, "text": reply})
            spoken.append(speaker.name)
            last_speaker_id = speaker.character_id

            sticker = expression.maybe_sticker(
                {
                    "emotion": speaker.emotion or "平静",
                    "valence": speaker.valence or 0.0,
                    "arousal": speaker.arousal or 0.35,
                },
                mood_changed=mood_changed,
                asked_about_mood=expression.asks_about_mood(text),
                rng=rng,
            )
            yield {
                "type": "speaker_end",
                "character_id": speaker.character_id,
                "name": speaker.name,
                "text": reply,
                "mentions": reply_mentions,
                "round": round_no,
                "sticker": sticker,
            }

            # 角色 @ 了别人 -> 下一轮必答
            for name in reply_mentions:
                if name != MENTION_ALL and name not in spoken and name not in pending_forced:
                    pending_forced.append(name)

            await asyncio.sleep(0)  # 让出控制权，保证流式体验

        if not pending_forced:
            break

    yield {"type": "done", "rounds": round_no, "speakers": spoken}