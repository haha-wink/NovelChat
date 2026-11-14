"""需求分析 -> 选谁回答。

## 为什么要换掉原来的选人逻辑

原来是「谁话多谁先说」：外向度 0.30 权重，阈值 0.30。
但默认角色的期望分只有 0.2025，**大多数时候没人过线，靠兜底抽签**。
用户体感就是"每次随机来人"。

现在的顺序反过来：

```
用户这句话
   ↓ 需求分析（dialogue.classify 已有的情绪/强度/意图/危机）
该来几个人？(1~10)      该来什么人？(persona.fit_for)
   ↓                        ↓
   └──────── 打分：适配度为主 ────────┘
                    ↓
              选定 1~10 个角色
```

## 人数怎么定

| 场景 | 人数 | 理由 |
|---|---|---|
| 危机 | 2 | 要多一点人在场，但不能一窝蜂 |
| 倾诉 | 2 ~ 6（随强度） | 越难受，越需要人接住 |
| 求助 | 2 ~ 5 | 一个人给办法，一两个人帮腔 |
| 闲聊 | 1 ~ 3 | 热闹一点就够 |
| @全体 | 最多 10 | 硬上限，防止一句 @ 打爆模型调用 |
| 显式 @ | 被 @ 的人 + 最多 2 个 | 用户的指向优先 |

## 适配度怎么算（见 services/persona.py）

* 难过/疲惫 -> 温柔、耐心、体贴的人优先；暴躁、毒舌的人扣分
* 焦虑/愤怒 -> 冷静、理性、沉稳的人优先
* 开心      -> 热情、活泼、爱玩的人优先
* 危机      -> 温柔 + 冷静，活泼和锋利都扣分
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from services import persona

#: 普通场景的人数上限（用户要求：不少于 1，不高于 10）
MAX_SPEAKERS = 10

#: @全体成员 的硬上限：不再限制人数，等于"所有能发言的人"
#: （早期版本 42 人全来会打爆模型调用，但用户明确要求 @全体 时全员回应，
#:   所以这里只留一个安全阀，防止未来上千人的书架把后端拖垮）
ALL_CALL_CEILING = 120
MIN_SPEAKERS = 1

#: @全体成员 的默认上限：不允许一句 @ 就把 40 多个人全叫出来
DEFAULT_ALL_CAP = 10

#: 打分权重（适配度为主）
#:
#: 噪声只留 0.04：之前 0.10 会把适配度差 0.01~0.05 的人打乱，
#: 结果"谁适配"被随机数盖住；现在适配度占 0.60，噪声只负责打破平局。
W_FIT = 0.60
W_RELEVANCE = 0.18
W_ENERGY = 0.10
W_AROUSAL = 0.04
W_NOISE = 0.04

PENALTY_COOLDOWN = 0.20
PENALTY_REPEAT = 0.15

#: 被显式 @ 时的加分，保证排在前面
FORCED_BONUS = 1.0

#: 「上一轮他问了你问题」的加分。群聊最伤的问题就是用户回答 A 的提问、
#: 接话的却是 C——这个分寸来自"记得谁在等回答"。
AWAITING_BONUS = 0.35


@dataclass
class Pick:
    """一个入选的发言人。"""

    character_id: str
    name: str
    score: float
    fit: float
    reason: str
    forced: bool = False
    emotion: str = "平静"
    valence: float = 0.0
    arousal: float = 0.35


@dataclass
class Decision:
    """一次选人的完整结果（日志/排查用）。"""

    emotion: str
    intent: str
    intensity: float
    crisis: bool
    target: int
    picks: list[Pick] = field(default_factory=list)
    #: 上一轮问过问题的角色（谁在等用户回答）
    awaiting: list[dict] = field(default_factory=list)
    note: str = ""

    def names(self) -> list[str]:
        return [pick.name for pick in self.picks]


# ---------------------------------------------------------------------------
# 人数决策
# ---------------------------------------------------------------------------

def target_count(
    *,
    emotion: str,
    intent: str,
    intensity: float,
    crisis: bool = False,
    all_called: bool = False,
    explicit: int = 0,
    usable: int = 0,
    awaiting: bool = False,
) -> int:
    """这句话该来几个人。返回 1~10。"""
    if all_called:
        # @全体成员 = 所有可发言的人都要回一句（用户明确要求）
        everyone = usable or 0
        return max(MIN_SPEAKERS, min(ALL_CALL_CEILING, everyone))

    if crisis:
        count = 2
    elif intent == "vent":
        # 强度 0..1 -> 2..6 人
        count = 2 + int(round(intensity * 4))
    elif intent == "seek_help":
        count = 2 + int(round(intensity * 3))
    else:
        count = 1 + (1 if intensity >= 0.5 else 0)

    # 显式 @ 时，被 @ 的人一定要在，另外最多再补 2 个
    if explicit:
        count = max(count, explicit)

    # 有人在等你回答：至少两个人在场，一个接话、一个搭腔，
    # 不然就会变回"一对一问答"，没有群聊的感觉
    if awaiting:
        count = max(count, 2)

    if usable:
        count = min(count, usable)
    return max(MIN_SPEAKERS, min(MAX_SPEAKERS, count))


# ---------------------------------------------------------------------------
# 谁在等你回答
# ---------------------------------------------------------------------------

#: 问句收尾词
_QUESTION_TAILS = ("吗", "呢", "么", "嘛")
#: 判断感情色彩时忽略的字符
_IGNORE_CHARS = set("的了是在有和与就都也还很太没不我你他她它们这那个吗呢吧啊呀哦嗯？?，,。.！!、…~ ")


def _looks_like_question(text: str) -> bool:
    body = (text or "").strip()
    if not body:
        return False
    if "？" in body or "?" in body:
        return True
    return body.rstrip("。！!…~ ").endswith(_QUESTION_TAILS)


def _overlap(text: str, question: str) -> float:
    """两个短句的字面重合度（中文没有空格，按字符集合算）。

    用**包含度**而不是 Jaccard：回答通常很短（"没有发烧"），
    问题很长（"现在有没有发烧？"），Jaccard 会被长度稀释到 0.07，
    而按较短的一方归一能到 1.0——这正是我们要的信号。
    """
    left = {ch for ch in str(text or "") if ch not in _IGNORE_CHARS}
    right = {ch for ch in str(question or "") if ch not in _IGNORE_CHARS}
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def find_awaiting(
    turns: list[dict],
    *,
    text: str = "",
    limit: int = 6,
    semantic=None,
) -> list[dict]:
    """从最近的流水里找出「上一轮问过用户问题」的角色。

    只看**最后一段连续的角色发言**（遇到用户消息就停），
    因为那一轮才是用户正在回答的对象。返回按"与这句话的相关度"降序。
    """
    # 从最新往回扫，遇到用户消息算一轮；最多看两轮
    # （用户可能隔了一轮才回答，比如别人先搭了句话）
    blocks: list[list[dict]] = []
    current: list[dict] = []
    for turn in reversed(turns or []):
        role = str(turn.get("role") or "")
        if role == "user":
            if current:
                blocks.append(current)
                current = []
            if len(blocks) >= 2:
                break
            continue
        if role == "assistant":
            current.append(turn)
    if current:
        blocks.append(current)

    found: list[dict] = []
    seen: set[str] = set()
    for depth, block in enumerate(blocks):
        for turn in block[:limit]:
            content = str(turn.get("content") or "")
            if not _looks_like_question(content):
                continue
            character_id = str(turn.get("character_id") or "")
            if not character_id or character_id in seen:
                continue
            seen.add(character_id)
            literal = _overlap(text, content)
            match = literal
            # 字面能对上就不用再算向量；对不上时用语义补一刀
            # （"我不烧了" vs "有没有发烧" 这种同义改写，字面重合很低）
            if semantic is not None and literal < 0.6:
                try:
                    score = semantic(text, content)
                    if score is not None:
                        match = round(max(literal, 0.7 * float(score)), 3)
                except Exception:  # noqa: BLE001 - 语义算不出来就用字面
                    logger.exception("语义匹配失败（回退字面）")
            match = round(match, 3)
            found.append(
                {
                    "character_id": character_id,
                    "name": str(turn.get("speaker_name") or ""),
                    #: 越靠后越旧：0 = 上一轮，1 = 上上轮
                    "depth": depth,
                    "question": content,
                    "match": match,
                    "literal": round(literal, 3),
                }
            )

    # 先看像不像在回答，再看新旧
    found.sort(key=lambda item: (item["match"], -item["depth"]), reverse=True)
    return found


# ---------------------------------------------------------------------------
# 选人
# ---------------------------------------------------------------------------

def _seconds_since(stamp: Optional[str], now: datetime) -> Optional[float]:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds()


def _usable(members: list[dict]) -> list[dict]:
    return [
        member for member in members
        if str(member.get("status") or "online") in ("online", "active")
        and str(member.get("state") or "idle") != "muted"
    ]


def _pick_awaiting(awaiting: list[dict]) -> Optional[dict]:
    """从「上一轮提问的角色」里挑出最可能在等回答的那一个。

    * 字面重合度高（用户明显在回答某个具体问题）-> 就是他
    * 全都很低但上一轮只有一个提问者（用户回了个「嗯」）-> 也只能是他
    * 一轮里好几个人都问了、用户又答得含糊 -> 不猜，按常规选人
    """
    if not awaiting:
        return None
    best = awaiting[0]
    if float(best.get("match") or 0.0) >= 0.2:
        return best
    latest = [item for item in awaiting if int(item.get("depth") or 0) == 0]
    if len(latest) == 1:
        return latest[0]
    return None


def select(
    members: list[dict],
    *,
    text: str,
    emotion: str,
    intent: str,
    intensity: float,
    crisis: bool = False,
    mentions: Optional[list[str]] = None,
    explicit_names: Optional[list[str]] = None,
    all_called: bool = False,
    last_speaker_id: Optional[str] = None,
    cooldown_seconds: int = 45,
    now: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
    relevance=None,
    awaiting: Optional[list[dict]] = None,
) -> Decision:
    """按需求分析结果选人。

    ``relevance(member) -> float`` 由调用方注入（点名/别名/关系），
    没传就当作全部 0——也就是纯看性格适配。
    """
    now = now or datetime.now(timezone.utc)
    rng = rng or random.Random()
    mentions = list(mentions or [])
    explicit_names = [name for name in (explicit_names or []) if name]

    usable = _usable(members)
    decision = Decision(
        emotion=emotion, intent=intent, intensity=intensity, crisis=crisis, target=0
    )
    if not usable:
        decision.note = "没有可发言的成员"
        return decision

    # 只给"最匹配的那个提问者"加权：一轮里可能好几个人都问了问题，
    # 全加等于没加（适配度最高的会抢走），必须挑一个。
    awaiting = list(awaiting or [])
    target_awaiting = _pick_awaiting(awaiting)
    awaiting_boost = {}
    if target_awaiting is not None:
        character_id = str(target_awaiting.get("character_id") or "")
        match = float(target_awaiting.get("match") or 0.0)
        awaiting_boost[character_id] = round(AWAITING_BONUS * (0.5 + match), 3)

    target = target_count(
        emotion=emotion,
        intent=intent,
        intensity=intensity,
        crisis=crisis,
        all_called=all_called,
        explicit=len(explicit_names),
        usable=len(usable),
        awaiting=target_awaiting is not None,
    )
    decision.target = target

    by_name = {str(member.get("name") or ""): member for member in usable}
    chosen: list[Pick] = []
    chosen_ids: set[str] = set()

    # ① 被显式 @ 的人必答，排在前面
    for name in explicit_names:
        member = by_name.get(name)
        if member is None:
            continue
        character_id = str(member.get("character_id") or "")
        if character_id in chosen_ids:
            continue
        chosen.append(
            Pick(
                character_id=character_id,
                name=name,
                score=FORCED_BONUS,
                fit=persona.fit_for(member, emotion=emotion, crisis=crisis),
                reason="被 @",
                forced=True,
                emotion=str(member.get("emotion") or "平静"),
                valence=float(member.get("valence") or 0.0),
                arousal=float(member.get("arousal") or 0.35),
            )
        )
        chosen_ids.add(character_id)

    # ② 其余按「适配度 + 相关性」排序
    scored: list[tuple[float, dict, float, str]] = []
    for member in usable:
        character_id = str(member.get("character_id") or "")
        if character_id in chosen_ids:
            continue

        fit = persona.fit_for(member, emotion=emotion, crisis=crisis)
        extraversion = float(member.get("extraversion") or 0.5)
        arousal = float(member.get("arousal") or 0.35)
        rel = 0.0
        if relevance is not None:
            try:
                rel = float(relevance(member) or 0.0)
            except Exception:  # noqa: BLE001 - 相关性算不出来不影响选人
                rel = 0.0

        score = (
            W_FIT * fit
            + W_RELEVANCE * rel
            + W_ENERGY * extraversion
            + W_AROUSAL * arousal
            + W_NOISE * rng.uniform(-1.0, 1.0)
        )
        reasons = [f"适配{fit:.2f}"]

        # 上一轮他问过你问题 -> 这句大概率是在回他，优先让他接
        if character_id in awaiting_boost:
            score += awaiting_boost[character_id]
            reasons.append("在等你回答")

        if rel >= 0.7:
            reasons.append("被点名")
        gap = _seconds_since(member.get("last_spoke_at"), now)
        if gap is not None and gap < cooldown_seconds:
            score -= PENALTY_COOLDOWN
            reasons.append(f"冷却中({int(gap)}s)")
        if last_speaker_id and character_id == str(last_speaker_id):
            score -= PENALTY_REPEAT
            reasons.append("刚说过")

        scored.append((round(score, 3), member, fit, " / ".join(reasons)))

    scored.sort(key=lambda item: item[0], reverse=True)
    for score, member, fit, reason in scored:
        if len(chosen) >= target:
            break
        chosen.append(
            Pick(
                character_id=str(member.get("character_id") or ""),
                name=str(member.get("name") or ""),
                score=score,
                fit=fit,
                reason=reason,
                emotion=str(member.get("emotion") or "平静"),
                valence=float(member.get("valence") or 0.0),
                arousal=float(member.get("arousal") or 0.35),
            )
        )

    # ③ 兜底：至少一个人（用户明确要求"不少于 1 人"）
    if not chosen and scored:
        score, member, fit, reason = scored[0]
        chosen.append(
            Pick(
                character_id=str(member.get("character_id") or ""),
                name=str(member.get("name") or ""),
                score=score,
                fit=fit,
                reason=f"{reason} / 兜底",
                emotion=str(member.get("emotion") or "平静"),
                valence=float(member.get("valence") or 0.0),
                arousal=float(member.get("arousal") or 0.35),
            )
        )

    limit = ALL_CALL_CEILING if all_called else MAX_SPEAKERS
    decision.picks = chosen[:limit]
    decision.awaiting = awaiting
    decision.note = f"按「{emotion}」适配度选 {len(decision.picks)} 人"
    return decision


def describe(decision: Decision) -> dict:
    """日志用摘要。"""
    return {
        "emotion": decision.emotion,
        "intent": decision.intent,
        "intensity": decision.intensity,
        "crisis": decision.crisis,
        "target": decision.target,
        "picked": [
            {"name": pick.name, "score": pick.score, "fit": pick.fit, "reason": pick.reason}
            for pick in decision.picks
        ],
        "awaiting": [item.get("name") for item in decision.awaiting],
    }