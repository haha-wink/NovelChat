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

#: 人数上限（用户要求：不少于 1，不高于 10）
MAX_SPEAKERS = 10
MIN_SPEAKERS = 1

#: @全体成员 的默认上限：不允许一句 @ 就把 40 多个人全叫出来
DEFAULT_ALL_CAP = 10

#: 打分权重（适配度为主）
W_FIT = 0.55
W_RELEVANCE = 0.20
W_ENERGY = 0.10
W_AROUSAL = 0.05
W_NOISE = 0.10

PENALTY_COOLDOWN = 0.20
PENALTY_REPEAT = 0.15

#: 被显式 @ 时的加分，保证排在前面
FORCED_BONUS = 1.0


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
) -> int:
    """这句话该来几个人。返回 1~10。"""
    if all_called:
        cap = min(MAX_SPEAKERS, usable or MAX_SPEAKERS)
        return max(MIN_SPEAKERS, cap)

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

    if usable:
        count = min(count, usable)
    return max(MIN_SPEAKERS, min(MAX_SPEAKERS, count))


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

    target = target_count(
        emotion=emotion,
        intent=intent,
        intensity=intensity,
        crisis=crisis,
        all_called=all_called,
        explicit=len(explicit_names),
        usable=len(usable),
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

    decision.picks = chosen[:MAX_SPEAKERS]
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
    }