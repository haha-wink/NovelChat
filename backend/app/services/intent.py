"""意图分析器：这句话要「记忆」还是要「工具」。

## 为什么需要它

现有的 `dialogue.classify` 只回答「情绪多重、想倾诉还是求助」。
但真正决定"接下来做什么"的，还有两个问题：

1. **要不要翻旧账？**——「我之前跟你说过我分手了」是在唤起长期记忆，
   光靠当前这句话的情绪判断不出来，得去 RAG 里捞。
2. **要不要动工具？**——危机要热线、焦虑要呼吸引导、求助要小步骤。

而且这两件事**可以同时成立**（复合意图）：

```
「我很难受，我之前跟你说过我分手了」
   ├─ 情绪倾诉   → 先共情
   └─ 记忆唤起   → 去 RAG 捞"分手"那段
```

## 怎么判断

规则优先（零模型调用），因为这几类信号很明确：

| 信号 | 判断 |
|---|---|
| 记忆线索词（之前/上次/我说过/还记得…） | 需要长期记忆 |
| 危机 | 需要危机工具 |
| 倾诉 + 高强度 | 需要安慰工具 |
| 焦虑 + 中高强度 | 需要呼吸引导 |
| 求助 | 需要小步骤工具 |

**暴露成 function call 工具**（`intent_analyze`），
既能在图里直接调，也能让模型自己调——两种用法共用一份判断逻辑。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from core.tools import protocol as tool_protocol

logger = logging.getLogger(__name__)

#: 唤起长期记忆的线索词
MEMORY_CUES = (
    "之前", "上次", "以前", "上回", "那次", "当初",
    "我说过", "我说过的", "跟你说过", "跟你讲过", "和你说过",
    "还记得", "记得我", "我们聊过", "你答应过", "你上次说",
)

#: 回忆类话题（配合"又/还是"这类词时，也说明在往回看）
RETRO_HINTS = ("又", "还是", "依然", "一直")

#: 意图标签
LABEL_VENT = "情绪倾诉"
LABEL_MEMORY = "记忆唤起"
LABEL_HELP = "求助"
LABEL_CRISIS = "危机"
LABEL_CHAT = "闲聊"


@dataclass
class IntentPlan:
    """一句话的需求拆解。"""

    labels: list[str] = field(default_factory=list)
    #: 需要检索长期记忆
    needs_memory: bool = False
    #: 建议调用的工具名
    tools: list[str] = field(default_factory=list)
    #: 复合意图（两个以上标签）
    compound: bool = False
    #: 命中的线索词（排查用）
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "labels": list(self.labels),
            "needs_memory": self.needs_memory,
            "tools": list(self.tools),
            "compound": self.compound,
            "evidence": list(self.evidence),
        }


def analyze(
    text: str,
    *,
    emotion: str = "平静",
    intent: str = "chat",
    intensity: float = 0.0,
    crisis: bool = False,
) -> IntentPlan:
    """拆解一句话：要记忆吗？要工具吗？是不是复合意图？"""
    body = str(text or "")
    plan = IntentPlan()

    # ---- 情绪倾诉 ----
    if intent == "vent" or (emotion in ("难过", "疲惫", "焦虑") and intensity >= 0.3):
        plan.labels.append(LABEL_VENT)

    # ---- 记忆唤起 ----
    cues = [cue for cue in MEMORY_CUES if cue in body]
    if cues:
        plan.needs_memory = True
        plan.labels.append(LABEL_MEMORY)
        plan.evidence.extend(cues[:2])
    elif any(word in body for word in RETRO_HINTS) and len(body) >= 12:
        # "又加班到这么晚" 这种，也常常想让人记起之前聊过的事
        plan.needs_memory = True
        plan.labels.append(LABEL_MEMORY)
        plan.evidence.append("回溯语气")

    # ---- 危机 ----
    if crisis:
        plan.labels.append(LABEL_CRISIS)
        plan.tools.append("crisis_helpline")

    # ---- 求助 / 安慰 / 呼吸 ----
    if intent == "seek_help":
        plan.labels.append(LABEL_HELP)
        plan.tools.append("small_step")

    if LABEL_VENT in plan.labels and not crisis:
        plan.tools.append("comfort_phrase")

    if emotion == "焦虑" and intensity >= 0.5 and not crisis:
        plan.tools.append("breathing_guide")

    if not plan.labels:
        plan.labels.append(LABEL_CHAT)

    # 去重保序
    seen: set[str] = set()
    plan.labels = [item for item in plan.labels if not (item in seen or seen.add(item))]
    plan.tools = [item for item in plan.tools if not (item in seen or seen.add(item))]
    plan.compound = len(plan.labels) >= 2
    return plan


def analyze_dict(text: str, **kwargs) -> dict:
    return analyze(text, **kwargs).as_dict()


def _tool_handler(args: dict) -> dict:
    """function call 入口：接收已判断好的情绪/意图，输出需求拆解。"""
    text = str(args.get("text") or "")
    if not text.strip():
        return {"ok": False, "error": "缺少 text"}
    try:
        intensity = float(args.get("intensity") or 0.0)
    except (TypeError, ValueError):
        intensity = 0.0

    plan = analyze(
        text,
        emotion=str(args.get("emotion") or "平静"),
        intent=str(args.get("intent") or "chat"),
        intensity=intensity,
        crisis=bool(args.get("crisis")),
    )
    return {"ok": True, **plan.as_dict()}


tool_protocol.register(
    tool_protocol.ToolSpec(
        name="intent_analyze",
        description=(
            "分析用户这句话需要什么：是否要检索长期记忆、是否要调用工具。"
            "支持复合意图（例如同时「情绪倾诉」和「记忆唤起」）。"
        ),
        parameters={
            "text": {"type": "string", "description": "用户原话"},
            "emotion": {"type": "string", "description": "用户情绪标签"},
            "intent": {"type": "string",
                       "description": "vent / seek_help / chat / crisis"},
            "intensity": {"type": "number", "description": "情绪强度 0~1"},
            "crisis": {"type": "boolean", "description": "是否危机"},
        },
        handler=_tool_handler,
        required=("text",),
        domain="intent",
    )
)


def describe() -> dict:
    return {
        "memory_cues": list(MEMORY_CUES),
        "labels": [LABEL_VENT, LABEL_MEMORY, LABEL_HELP, LABEL_CRISIS, LABEL_CHAT],
        "tools": ["intent_analyze"],
    }