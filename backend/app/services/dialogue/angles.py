"""发言角度：让并行说话的人各干一件事。

## 为什么需要它

群聊并行生成时，四个人看到的是**同一段上下文**（用户的同一句话 + 同一份历史），
于是很容易给出同一个答案：

```
用户：我没干的事有好多，还没吃饭、没汇总工作表、没洗衣服、没给花浇水，帮我排个先后？
陈雯雯：先吃饭吧，这个最要紧。
樱井小暮：先吃饭，这一条不用犹豫。
源稚生：先吃饭。你带着感冒又乏力……
帕西：先把顺序排出来：先吃饭，哪怕只是热一碗汤……
```

四条回复的信息量等于一条，还占满屏幕。

## 怎么解

分发前给每个发言人**预分配一个角度**（分工），生成时只用那个角度说话：

| 角度 | 职责 |
|---|---|
| 共情 | 只接住情绪，不给建议 |
| 方案 | 只给一条具体可做的 |
| 补充 | 补一个别人没提到的细节 |
| 轻松 | 缓和气氛，可以打趣 |
| 追问 | 问一个具体的小问题 |

角度按性格分配：温柔的人先共情、冷静的人给方案、爱玩的人活跃气氛……
保证同一次里**不重样**。

这只是第一道闸；第二道是聚合节点里的向量相似度过滤（`dedupe`），
万一还是撞了，由它兜底。
"""

from __future__ import annotations

from dataclasses import dataclass

from services.dialogue import persona


@dataclass(frozen=True)
class Angle:
    key: str
    name: str
    #: 拼进提示词的约束
    instruction: str


ANGLES: tuple[Angle, ...] = (
    Angle(
        key="empathy",
        name="共情",
        instruction="只接住对方的情绪（一句就够），**不要给建议、不要列步骤**。",
    ),
    Angle(
        key="plan",
        name="方案",
        instruction="只给**一条**最具体的可执行做法，说清先做什么；不要重复别人的安慰。",
    ),
    Angle(
        key="detail",
        name="补充",
        instruction="补一个别人大概率没提到的**细节或提醒**（时间、身体、后果），不要重复主干建议。",
    ),
    Angle(
        key="light",
        name="轻松",
        instruction="用轻松的语气缓和一下气氛，可以打趣或自嘲，别讲道理。",
    ),
    Angle(
        key="ask",
        name="追问",
        instruction="问**一个**具体的小问题，把话头递回去；不要总结、不要给建议。",
    ),
)

#: 角度 -> 更容易胜任的性格维度
_AFFINITY: dict[str, dict[str, float]] = {
    "empathy": {"warmth": 1.0, "calmness": 0.3},
    "plan": {"calmness": 1.0, "warmth": 0.2},
    "detail": {"calmness": 0.6, "playfulness": 0.2},
    "light": {"playfulness": 1.0, "energy": 0.5},
    "ask": {"energy": 0.4, "warmth": 0.4},
}

#: 危机场景只有三种角度合适（不能打趣、不能追问）
CRISIS_ANGLES = ("empathy", "plan", "detail")


def by_key(key: str) -> Angle:
    for angle in ANGLES:
        if angle.key == key:
            return angle
    return ANGLES[0]


def _score(member: dict, angle: Angle) -> float:
    traits = persona.analyze_temperament(member).as_dict()
    weights = _AFFINITY.get(angle.key, {})
    return sum(weights.get(dim, 0.0) * traits.get(dim, 0.0) for dim in weights) - 0.15 * traits.get(
        "sharpness", 0.0
    )


def assign(speakers: list[dict], *, crisis: bool = False) -> list[Angle]:
    """给这批发言人分配角度，保证同一次里不重样。

    ``speakers`` 只要带 ``name`` 和性格字段即可。返回顺序与输入一致。
    """
    pool = [angle for angle in ANGLES if (not crisis or angle.key in CRISIS_ANGLES)]
    usage: dict[str, int] = {angle.key: 0 for angle in pool}
    result: list[Angle] = []

    for member in speakers:
        # 人比角度多时会重复，但优先选**用得最少**的那个
        best = max(
            pool,
            key=lambda angle: _score(member, angle) - 0.6 * usage[angle.key],
        )
        usage[best.key] = usage.get(best.key, 0) + 1
        result.append(best)

    return result


def describe() -> dict:
    return {
        "angles": [
            {"key": angle.key, "name": angle.name, "instruction": angle.instruction}
            for angle in ANGLES
        ],
        "crisis_angles": list(CRISIS_ANGLES),
    }