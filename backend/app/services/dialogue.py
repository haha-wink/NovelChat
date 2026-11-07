"""对话工作流：把「一句话」变成「怎么回」的结构化判断。

## 为什么单独一层

角色 Agent 原来的流程是「拼提示词 → 生成」，中间少了两件事：

1. **这句话是什么**——用户在倾诉、求助，还是闲聊？情绪有多重？有没有风险？
2. **该怎么回**——不同意图的应对方式完全不同：倾诉要共情，求助要共情之后给引导，
   危机要先稳住再指现实求助。把这些塞进那 9 条扮演规则里，模型会顾此失彼。

所以拆成三段，每段都便宜（纯规则，零模型调用）：

```
preprocess(text)          清洗（去零宽、压空白、限长）
        ↓
classify(text)            -> TurnSignals(情绪 / 强度 / 意图 / 风险)
        ↓
plan(signals)             -> Plan(sop 文本 + 要用的素材)
        ↓
（生成回复）
        ↓
verify(reply, plan)       安全兜底 + 去说教 + 危机补充
```

## 和 services/affect.py 的分工

`affect.extract_signals` 认的是**社交行为**（道谢、称赞、辱骂、亲密、承诺…），
用来推动角色对用户的好感度；这里认的是**用户此刻的状态和诉求**，
用来决定这一轮怎么接。两者互补，不重复。

## 要不要上模型

`classify` 是**纯规则**：零调用、可复现、可测。

没有接"LLM 再判一次"——那会让每轮多一次模型调用，而收益只在规则模糊时才有。
真遇到规则不够用的情况（比如反讽、隐喻），在这里加一层带 key 才启用的模型判断即可，
接口形态不用变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# 一、预处理
# ---------------------------------------------------------------------------

#: 零宽字符、BOM、方向控制符——复制粘贴来的文本里常有
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_SPACES = re.compile(r"[ \t\u3000]+")
_BLANK_LINES = re.compile(r"\n{3,}")

#: 单次输入上限（真正的长度校验在接口层，这里只是兜底）
MAX_CHARS = 4000


def preprocess(text: str) -> str:
    """清洗用户输入。

    只做"不影响语义"的处理：去不可见字符、压空白、压空行、限长。
    **不删标点、不动 emoji**——「？？」和「……」本身就携带情绪。
    """
    text = str(text or "")
    text = _INVISIBLE.sub("", text)
    text = _SPACES.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    text = text.strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    return text


# ---------------------------------------------------------------------------
# 二、情绪与意图识别
# ---------------------------------------------------------------------------

#: 意图
INTENT_VENT = "vent"          # 倾诉：倒苦水，需要被接住
INTENT_HELP = "seek_help"     # 求助：想要办法
INTENT_CHAT = "chat"          # 闲聊
INTENT_CRISIS = "crisis"      # 危机：自伤/自杀风险

INTENT_LABELS = {
    INTENT_VENT: "倾诉",
    INTENT_HELP: "求助",
    INTENT_CHAT: "闲聊",
    INTENT_CRISIS: "危机",
}

#: 用户情绪词表（与 affect 的 9 个角色情绪分开：这里描述的是"用户此刻"）
EMOTION_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("焦虑", ("焦虑", "紧张", "心慌", "害怕", "担心", "不安", "睡不着", "慌")),
    ("难过", ("难过", "伤心", "想哭", "哭了", "委屈", "失落", "低落", "心痛", "崩溃")),
    ("愤怒", ("生气", "气死", "愤怒", "火大", "烦透", "讨厌", "恼火", "受不了")),
    ("疲惫", ("累", "疲惫", "撑不住", "没力气", "没劲", "乏", "熬不动")),
    ("开心", ("开心", "高兴", "太好了", "舒服", "幸福", "愉快", "哈哈")),
)

#: 求助信号（想要"办法"）
_HELP_WORDS = (
    "怎么办", "怎么才能", "怎么才能", "如何", "能不能", "可以吗", "帮我",
    "有没有办法", "有没有什么", "该不该", "要不要", "教我", "建议", "支招",
    "该怎么做", "有什么办法", "求助",
)

#: 倾诉信号（只想被听见）
_VENT_WORDS = (
    "好累", "太累", "难受", "心里堵", "好烦", "烦死", "憋得慌", "委屈",
    "没人懂", "没人理解", "想找人说说", "说说话", "陪我聊聊", "撑不下去",
)

#: 危机：明确指向自己
_CRISIS_HARD = (
    "自杀", "自残", "割腕", "遗书", "结束生命", "活不下去", "活着没意思",
    "不想活了", "轻生", "跳楼",
)
#: 危机：需要"我"主语才成立（"你去死"不算）
_CRISIS_SOFT = re.compile(r"我[^。！？!?\n]{0,8}(想死|去死|死了算了|不如死)")
#: 「想死你了」是撒娇，不是危机
_AFFECTION = re.compile(r"想死你|想死我")


def is_crisis(text: str) -> bool:
    """危机判断：宁可多判（多判只是多一句关心），不可漏判。"""
    text = text or ""
    if _AFFECTION.search(text) and not any(word in text for word in _CRISIS_HARD):
        return False
    if any(word in text for word in _CRISIS_HARD):
        return True
    return bool(_CRISIS_SOFT.search(text))


@dataclass
class TurnSignals:
    """一条用户消息的判断结果。"""

    text: str = ""
    emotion: str = "平静"
    #: 强度 0~1，越高越需要稳住
    intensity: float = 0.0
    intent: str = INTENT_CHAT
    crisis: bool = False
    #: 命中的依据（排查用，不发给模型）
    evidence: list[str] = field(default_factory=list)

    @property
    def intent_label(self) -> str:
        return INTENT_LABELS.get(self.intent, "闲聊")


def classify(text: str) -> TurnSignals:
    """规则分类：用户情绪 + 强度 + 意图。

    顺序有意义：**先判危机**——危机一旦成立，意图就是危机，
    后面的情绪/意图识别只用于给回复定语气。
    """
    raw = str(text or "")
    signals = TurnSignals(text=raw)
    evidence: list[str] = []

    # 1) 情绪：命中最多的那个胜出，同分按"负面优先"（负面更需要被接住）
    best_emotion = "平静"
    best_hits = 0
    for emotion, words in EMOTION_WORDS:
        hits = [word for word in words if word in raw]
        if len(hits) > best_hits:
            best_emotion, best_hits = emotion, len(hits)
            evidence.extend(hits[:2])
    signals.emotion = best_emotion

    # 2) 强度：词命中 + 标点 + 长度
    intensity = min(0.55, 0.25 * best_hits)
    if re.search(r"[!！]{2,}", raw):
        intensity += 0.15
        evidence.append("连续感叹号")
    if re.search(r"[?？]{2,}", raw):
        intensity += 0.1
        evidence.append("连续问号")
    if len(raw) >= 60:
        intensity += 0.1
    if best_emotion in ("难过", "焦虑", "愤怒", "疲惫") and best_hits:
        intensity += 0.15
    signals.intensity = round(min(1.0, intensity), 2)

    # 3) 意图
    if is_crisis(raw):
        signals.crisis = True
        signals.intent = INTENT_CRISIS
        signals.emotion = signals.emotion if best_hits else "难过"
        signals.intensity = max(signals.intensity, 0.9)
        evidence.append("危机词")
    else:
        help_hit = [word for word in _HELP_WORDS if word in raw]
        vent_hit = [word for word in _VENT_WORDS if word in raw]
        evidence.extend(help_hit[:2] + vent_hit[:1])
        if help_hit and not vent_hit:
            signals.intent = INTENT_HELP
        elif vent_hit or (best_hits and signals.intensity >= 0.4):
            signals.intent = INTENT_VENT
        elif help_hit and vent_hit:
            # 又倒苦水又问办法：先共情再给方法，仍按求助处理
            signals.intent = INTENT_HELP
        else:
            signals.intent = INTENT_CHAT

    signals.evidence = evidence
    return signals


# ---------------------------------------------------------------------------
# 三、规划层（SOP）
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    """这一轮怎么回。"""

    signals: TurnSignals
    sop: list[str] = field(default_factory=list)
    #: 可用素材（放松引导、短句、小建议），让模型自然融入而不是照念
    materials: list[str] = field(default_factory=list)

    def context_block(self) -> str:
        """拼进 ``additional_context`` 的那段文本。"""
        lines = [
            "【本轮判断（内部参考，不要说破）】",
            f"用户情绪：{self.signals.emotion}（强度 {self.signals.intensity:.1f}）"
            f"｜意图：{self.signals.intent_label}",
            "【应对策略】",
        ]
        lines.extend(f"- {item}" for item in self.sop)
        if self.materials:
            lines.append("【可用素材（自然融入，不要照念）】")
            lines.extend(f"- {item}" for item in self.materials)
        return "\n".join(lines)


def plan(signals: TurnSignals, *, character: Optional[dict] = None) -> Plan:
    """按意图 + 强度 + 风险决定策略。

    SOP 只写"做什么"，不写"说什么"——具体措辞交给模型和角色人设。
    """
    result = Plan(signals=signals)
    intent = signals.intent
    heavy = signals.intensity >= 0.6

    if signals.crisis:
        result.sop = [
            "先稳住对方：一句关心的话开头，不要追问细节、不要讲道理",
            "明确告诉对方「你愿意说，我就在这儿听着」，不评判、不轻描淡写",
            "如果对方有具体计划或已经受伤，直接建议找身边的人或拨打当地急救/心理援助电话",
            "这一轮不要开玩笑、不要用梗、不要提你自己的设定",
            "保持角色口吻，但把「人」放在「角色」前面",
        ]
        result.materials = [
            "现实求助提示（用角色语气自然带出）：找个信得过的人说一声，或者打当地心理援助热线",
        ]
        return result

    if intent == INTENT_VENT:
        result.sop = [
            "以共情为主：先承认对方的感受（「这事儿确实挺难受的」），别急着给方案",
            "不要列清单、不要 1/2/3、不要说「你应该」",
            "最多问一句开放的问题，把话头留给对方",
            "回复要短" + ("，对方情绪很重，尤其要慢、要少" if heavy else ""),
        ]
        if heavy:
            result.materials.append("一句接住情绪的话，不要超过 20 字")
        return result

    if intent == INTENT_HELP:
        result.sop = [
            "先接住情绪（一句，不铺垫），再给办法",
            "办法只给 1~2 条，具体到能马上做，不要泛泛而谈",
            "给完不要追问「你觉得呢」，把决定权留给对方",
            "不要同时列优点缺点、不要写完整方案",
        ]
        result.materials.append("一条今天就能做的小步骤")
        return result

    # 闲聊
    result.sop = [
        "正常闲聊，保持人设与语感",
        "不要主动往情绪话题上引，也不要突然说教",
    ]
    if heavy:
        result.sop.append("对方情绪偏重，可以顺势多问一句")
    return result


# ---------------------------------------------------------------------------
# 四、后置校验
# ---------------------------------------------------------------------------

#: 说教口吻 -> 软一点的说法
_SOFTEN = (
    ("你应该", "你可以"),
    ("你必须", "你可以"),
    ("建议你", "要不你"),
    ("一定要", "可以"),
    ("你必须得", "你可以"),
)

#: 明确不安全/不合适的输出
_UNSAFE = (
    "你去死", "活该", "自找的", "有病吧", "神经病", "闭嘴吧你",
)

#: 危机回复里应当出现的"现实求助"痕迹
_CARE_MARKERS = ("热线", "求助", "专业", "医生", "身边", "信得过", "朋友", "家人")


@dataclass
class Verdict:
    text: str
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.notes)


def verify(reply: str, plan_obj: Plan) -> Verdict:
    """后置校验：安全兜底 + 去说教 + 危机补充。

    只做"低风险改写"——不重写句子、不改变角色口吻，
    改写痕迹记在 ``notes`` 里，进审计日志。
    """
    text = (reply or "").strip()
    notes: list[str] = []

    # 1) 不安全内容：整段替换成安全兜底（宁可失真，不留风险）
    for bad in _UNSAFE:
        if bad in text:
            notes.append(f"命中不安全词：{bad}")
            fallback = "……这话我说不出口。你要是现在很难受，先别一个人扛着。"
            return Verdict(text=fallback, notes=notes)

    # 2) 说教口吻：软化（只替换开头/句首的固定搭配，不碰引号内容）
    for old, new in _SOFTEN:
        if old in text:
            text = text.replace(old, new)
            notes.append(f"软化说教：{old} -> {new}")

    # 3) 危机：回复里没有任何"现实求助"痕迹时补一句
    if plan_obj.signals.crisis and not any(marker in text for marker in _CARE_MARKERS):
        text = text.rstrip("。！？!?") + "。别一个人扛着，找个信得过的人说一声，好吗？"
        notes.append("危机补充现实求助提示")

    return Verdict(text=text, notes=notes)


def describe(signals: TurnSignals) -> dict:
    """给日志/排查用的摘要。"""
    return {
        "intent": signals.intent,
        "emotion": signals.emotion,
        "intensity": signals.intensity,
        "crisis": signals.crisis,
        "evidence": signals.evidence,
    }