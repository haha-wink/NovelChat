"""工具层 · 第三层：业务技能（Skill）。

一个 Skill = 「什么场合用」+「要调哪些工具」+「怎么拼成给模型的话」。

分三层的好处在这里最明显：**业务只关心"安慰一个人需要什么"**，
不用知道这些工具是 JSON Schema、StructuredTool 还是 ToolNode。

## 现有技能

| 技能 | 什么时候用 | 调用的工具 |
|---|---|---|
| `comfort` | 用户在倾诉（vent） | 呼吸引导、接住情绪的一句话 |
| `steady` | 危机（crisis） | 现实求助信息、稳定话术 |
| `suggest` | 求助（seek_help） | 一条今天能做的小步骤 |

## 一次技能的产出

``SkillResult.materials`` 会被拼进提示词的【可用素材】，
让模型自然融入，而不是照念。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from core import tool_protocol as protocol

# ---------------------------------------------------------------------------
# L1 工具的具体实现（注册进协议层）
# ---------------------------------------------------------------------------


def _breathing_guide(args: dict) -> dict:
    seconds = int(args.get("seconds") or 4)
    rounds = int(args.get("rounds") or 3)
    text = (
        f"先慢慢吸气 {seconds} 秒，停两秒，再慢慢呼出来；这样来 {rounds} 轮。"
        "不用做得标准，能把注意力放到呼吸上就行。"
    )
    return {"text": text, "seconds": seconds, "rounds": rounds}


def _comfort_phrase(args: dict) -> dict:
    emotion = str(args.get("emotion") or "难过")
    library = {
        "难过": "这确实挺难受的，不用急着好起来。",
        "焦虑": "先把这口气缓过来，事情可以一件一件来。",
        "疲惫": "累了就是累了，不用硬撑给我看。",
        "愤怒": "换谁碰上都会生气，这很正常。",
        "开心": "看得出来你今天状态不错。",
        "平静": "我在这儿。",
    }
    return {"text": library.get(emotion, library["难过"]), "emotion": emotion}


def _small_step(args: dict) -> dict:
    topic = str(args.get("topic") or "").strip()
    base = "挑一件最小的：喝口水、站起来走两步、把待办里最烦的那条写成一句话。"
    if topic:
        base = f"先只处理「{topic}」里最小的一步，比如把它写成一句话，或者只做五分钟。"
    return {"text": base, "topic": topic}


def _crisis_helpline(args: dict) -> dict:
    region = str(args.get("region") or "中国").strip()
    return {
        "text": (
            "如果这种念头很强烈，先别一个人扛着：找个现在能联系上的、你信得过的人说一声；"
            "或者拨打当地心理援助热线（国内可拨 12356 心理援助热线，紧急情况 120/110）。"
        ),
        "region": region,
    }


#: 注册（导入本模块即生效）
protocol.register(
    protocol.ToolSpec(
        name="breathing_guide",
        description="给一个简单的呼吸放松引导，适合焦虑或情绪激动时",
        parameters={
            "seconds": {"type": "integer", "description": "吸气秒数，默认 4"},
            "rounds": {"type": "integer", "description": "做几轮，默认 3"},
        },
        handler=_breathing_guide,
        domain="comfort",
    )
)
protocol.register(
    protocol.ToolSpec(
        name="comfort_phrase",
        description="按用户情绪挑一句接住他的话",
        parameters={"emotion": {"type": "string", "description": "用户当前情绪标签"}},
        handler=_comfort_phrase,
        required=("emotion",),
        domain="comfort",
    )
)
protocol.register(
    protocol.ToolSpec(
        name="small_step",
        description="给一条今天就能做的小建议",
        parameters={"topic": {"type": "string", "description": "用户烦的那件事，可空"}},
        handler=_small_step,
        domain="suggest",
    )
)
protocol.register(
    protocol.ToolSpec(
        name="crisis_helpline",
        description="提供现实中的求助渠道（热线/身边人），危机场景专用",
        parameters={"region": {"type": "string", "description": "地区，默认中国"}},
        handler=_crisis_helpline,
        domain="crisis",
    )
)


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


@dataclass
class SkillResult:
    """技能执行结果。"""

    skill: str
    materials: list[str] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)
    note: str = ""

    def as_block(self) -> str:
        """拼成提示词里的【可用素材】块。"""
        if not self.materials:
            return ""
        lines = ["【可用素材（自然融入，不要照念）】"]
        lines.extend(f"- {item}" for item in self.materials)
        return "\n".join(lines)


@dataclass
class Skill:
    """一个业务技能。"""

    name: str
    #: 适用度打分：0 = 不该用
    when: Callable[[dict], float]
    #: 要调的工具，形如 [("breathing_guide", {"seconds": 4})]
    steps: list[tuple[str, dict]]
    description: str = ""

    def run(self, context: Optional[dict] = None) -> SkillResult:
        context = context or {}
        result = SkillResult(skill=self.name)
        for tool_name, base_args in self.steps:
            args = {**base_args, **{k: v for k, v in context.items() if k in base_args}}
            output = protocol.execute(tool_name, args)
            result.outputs.append(output)
            if output.get("ok") and output.get("text"):
                result.materials.append(str(output["text"]))
            elif not output.get("ok"):
                result.note = output.get("error") or "工具失败"
        return result


def _when_comfort(signals: dict) -> float:
    if signals.get("crisis"):
        return 0.0
    return 0.9 if signals.get("intent") == "vent" else 0.0


def _when_steady(signals: dict) -> float:
    return 1.0 if signals.get("crisis") else 0.0


def _when_suggest(signals: dict) -> float:
    return 0.8 if signals.get("intent") == "seek_help" else 0.0


SKILLS: dict[str, Skill] = {
    "comfort": Skill(
        name="comfort",
        description="用户在倾诉：先接住情绪，需要时可以带一个呼吸引导",
        when=_when_comfort,
        steps=[
            ("comfort_phrase", {"emotion": "难过"}),
            ("breathing_guide", {"seconds": 4, "rounds": 3}),
        ],
    ),
    "steady": Skill(
        name="steady",
        description="危机：给现实求助渠道，稳住对方",
        when=_when_steady,
        steps=[("crisis_helpline", {"region": "中国"})],
    ),
    "suggest": Skill(
        name="suggest",
        description="求助：给一条今天就能做的小步骤",
        when=_when_suggest,
        steps=[("small_step", {"topic": ""})],
    ),
}


def choose(signals: dict) -> Optional[Skill]:
    """按本轮判断挑一个技能；都不合适就返回 None。"""
    best: Optional[Skill] = None
    best_score = 0.0
    for skill in SKILLS.values():
        try:
            score = float(skill.when(signals or {}))
        except Exception:  # noqa: BLE001 - 技能判据出错不影响聊天
            score = 0.0
        if score > best_score:
            best, best_score = skill, score
    return best


def run_for(signals: dict, *, extra: Optional[dict] = None) -> Optional[SkillResult]:
    """挑一个技能并执行，返回结果（没有合适技能就 None）。"""
    skill = choose(signals or {})
    if skill is None:
        return None
    context = {
        "emotion": (signals or {}).get("emotion") or "难过",
        "topic": (extra or {}).get("topic") or "",
        **(extra or {}),
    }
    return skill.run(context)


def describe() -> dict:
    return {
        "skills": [
            {"name": skill.name, "description": skill.description,
             "tools": [name for name, _ in skill.steps]}
            for skill in SKILLS.values()
        ]
    }