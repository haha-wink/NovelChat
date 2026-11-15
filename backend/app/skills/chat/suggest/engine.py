"""suggest 用到的工具（第①层：协议）。

由 skills.loader 在加载技能包时导入并注册，调用方不用手动 import。
"""

from __future__ import annotations

from core.tools import protocol as tool_protocol


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


tool_protocol.register(
    tool_protocol.ToolSpec(
        name="small_step",
        description="给一条今天就能做的小建议",
        parameters={"topic": {"type": "string", "description": "用户烦的那件事，可空"}},
        handler=_small_step,
        domain="suggest",
    )
)
