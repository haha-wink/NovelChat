"""情绪与好感度：双层状态机。

## 为什么是两层

| 层 | 变化速度 | 范围 | 作用 |
|---|---|---|---|
| **情绪** emotion | 每轮可变，自动回落 | valence -1~1 / arousal 0~1 + 标签 | 决定"这句话怎么说" |
| **好感度** favorability | 每轮最多 ±5，通常 ±1~2 | 0~100 | 决定"用哪种关系语气" |

只有情绪 → 角色翻脸比翻书快；只有好感度 → 永远一个语气。
两层叠加才有「今天心情不好，但依然信任你」这种真实感。

## 数值不给用户看

用户看不到任何数字，也不需要——情绪必须**由言行外显**：
伤心就说"我太伤心了，呜呜"、用省略号、发 🥺；恼火就短句反问、发 😤。
``prompt_block()`` 负责把这些翻译成给模型的表达要求，且**禁止**它说破内部数值。

## 数据从哪来

* 基线：由角色性格推出（寡言 → 低唤醒；热情 → 高唤醒），存 ``character_affect.baseline``；
* 每轮增量：``extract_signals()`` 识别**言语行为**（道谢/夸奖/侮辱/威胁/亲密/关心/道歉/承诺/失约）；
* 规则读不懂时（无命中且长句、或正负信号打架）：交给 ``emotion_model`` 的极性判断兜底。

每次变化都写一条 ``affect_change`` 事件，带证据——这是唯一能回溯"角色为什么突然冷淡了"的线索。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from core import database as db
from services.ops import agent_log
from services.dialogue import emotion_model

logger = logging.getLogger(__name__)

#: 新用户/新书架的初始好感度（中性偏客气）
DEFAULT_FAVORABILITY = 40

#: 单轮增量上限（防抖：再刺激也不会一步登天）
MAX_DELTA_VALENCE = 0.5
MAX_DELTA_AROUSAL = 0.4
MAX_DELTA_FAVORABILITY = 5

#: 每轮向基线回落的比例（情绪会自然平复）
DECAY_VALENCE = 0.35
DECAY_AROUSAL = 0.45

#: 长于这个长度且规则无命中时，才动用情绪模型（省算力）
MODEL_FALLBACK_CHARS = 60

# ---------------------------------------------------------------------------
# 性格 -> 情绪基线
# ---------------------------------------------------------------------------
#: 性格 -> 情绪基线（相对 DEFAULT_BASELINE 的**增量**）
BASELINE_RULES: tuple[tuple[tuple[str, ...], float, float], ...] = (
    # 外向型：更正向、更活跃
    (("热情", "开朗", "活泼", "话多", "健谈", "外向", "直爽"), 0.20, 0.25),
    # 温和型：略正向、中等活跃
    (("温柔", "温和", "善良", "体贴", "耐心", "包容"), 0.15, 0.00),
    # 内敛型：唤醒度更低
    (("冷静", "克制", "理性", "沉稳", "镇定"), -0.05, -0.10),
    (("寡言", "沉默", "冷淡", "话少", "疏离", "冷漠"), -0.10, -0.15),
    # 戒备型：负向且唤醒度偏高
    (("多疑", "警惕", "戒备", "谨慎", "敏感"), -0.15, 0.10),
    # 易怒型：唤醒度最高
    (("暴躁", "易怒", "冲动", "急躁"), -0.10, 0.35),
    # 消沉型
    (("忧郁", "悲观", "阴沉", "孤僻", "自卑"), -0.25, -0.05),
    (("天真", "单纯", "烂漫"), 0.20, 0.15),
    (("傲慢", "自负", "高傲", "目中无人"), -0.05, 0.15),
)

DEFAULT_BASELINE = {"valence": 0.0, "arousal": 0.35}

# ---------------------------------------------------------------------------
# 言语行为 -> 情绪/好感度增量
# ---------------------------------------------------------------------------
SIGNAL_RULES: tuple[tuple[str, tuple[str, ...], float, float, int], ...] = (
    # kind,       词表,                                              dv,     da,    df
    ("thanks",  ("谢谢", "多谢", "感谢", "辛苦了", "麻烦你了", "拜托"), 0.15,  0.00,  1),
    ("praise",  ("厉害", "了不起", "佩服", "优秀", "太棒", "真棒", "牛", "好看", "漂亮", "喜欢"), 0.25, 0.10, 2),
    ("insult",  ("废物", "蠢货", "蠢", "傻子", "垃圾", "滚开", "闭嘴", "讨厌", "恶心", "白痴", "没用", "烦人"), -0.35, 0.30, -3),
    ("threat",  ("否则", "不然", "命令你", "警告你", "你必须", "赶紧", "立刻给我"), -0.30, 0.25, -3),
    ("intimacy", ("抱抱", "想你了", "喜欢你", "爱你", "亲一下", "陪你"), 0.20, 0.15, 2),
    ("care",    ("你还好吗", "还好吗", "担心你", "注意身体", "别太累", "小心点"), 0.20, 0.05, 1),
    ("apology", ("对不起", "抱歉", "我错了", "原谅我"), 0.15, 0.00, 1),
    ("promise", ("我答应", "我保证", "我会做到", "一定做到"), 0.10, 0.00, 1),
    ("broken",  ("忘了", "没做到", "失约", "放你鸽子", "没守约"), -0.20, 0.10, -2),
)

#: 正向 / 负向（用于判断"信号打架"）
_POSITIVE_KINDS = frozenset({"thanks", "praise", "intimacy", "care", "apology", "promise"})
_NEGATIVE_KINDS = frozenset({"insult", "threat", "broken"})

#: 某些信号会直接把情绪推到对应标签
_SIGNAL_LABEL_HINT = {"care": "关切", "intimacy": "羞赧"}

# ---------------------------------------------------------------------------
# 好感度 -> 语气策略
# ---------------------------------------------------------------------------
FAVORABILITY_BANDS: tuple[tuple[int, int, str, str, str], ...] = (
    (0, 20, "cold", "冷淡、戒备", "只答必要信息，不主动展开，不接话茬"),
    (21, 40, "distant", "疏离但礼貌", "客气、用敬称、不接梗"),
    (41, 60, "normal", "平常", "正常交谈，偶尔反问"),
    (61, 80, "close", "亲近", "主动关心、可以开玩笑、用昵称"),
    (81, 100, "intimate", "亲昵、信任", "可以说心里话、袒护对方、可以示弱"),
)

#: 情绪 -> 外显方式（这是"用户看得见"的唯一通道）
EMOTION_STYLE: dict[str, str] = {
    "平静": "语气平稳，句子可长可短，不需要刻意表现",
    "喜悦": "语气轻快，可用语气词（呀/啦）和 😆✨ 这类表情",
    "期待": "语气上扬，会主动追问、往前推话题",
    "关切": "语气温和，多问对方的感受，句子放软",
    "羞赧": "语气含混，句子变短，可用 😳 这类表情",
    "失落": "语气低落，句子变短，可用省略号、「我太伤心了，呜呜」、🥺💧 这类表情",
    "恼怒": "句子短促，可用反问和「哼」，可用 😤 这类表情",
    "警惕": "语气审慎，会追问动机，减少自称、不轻易表态",
    "厌恶": "语气冷淡，措辞疏离，可用 🥱 这类表情",
}


@dataclass
class Signal:
    kind: str
    d_valence: float
    d_arousal: float
    d_favorability: int
    evidence: str = ""


#: 情绪 -> 一句人话（给"角色详情"面板用，不暴露任何数值）
MOOD_TEXT: dict[str, str] = {
    "平静": "情绪平稳，看不出什么起伏",
    "喜悦": "心情不错，语气会轻快一些",
    "期待": "有点期待，愿意多聊两句",
    "关切": "正在关心你的状态",
    "羞赧": "有点不自在，话会变少",
    "失落": "情绪低落，说话有气无力",
    "恼怒": "正在生气，句子会变短",
    "警惕": "对你有些戒备，先观察",
    "厌恶": "明显不太想多谈",
}

#: 好感度档位 -> 一句人话（**只说态度，不给区间、不给数值**）
ATTITUDE_TEXT: dict[str, str] = {
    "cold": "客气但疏远，只回答必要的话",
    "distant": "保持距离，礼貌但不会过分热络",
    "normal": "平常相处，愿意正常聊",
    "close": "愿意主动关心你，也会开玩笑",
    "intimate": "很信任你，愿意说心里话",
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _toward(current: float, target: float, rate: float) -> float:
    """向目标靠拢（情绪回落）。"""
    return current + (target - current) * rate


def baseline_for(character: dict) -> dict:
    """由性格/身份推出情绪基线。角色编辑后基线会跟着变。"""
    traits = " ".join(
        [
            " ".join(str(x) for x in (character.get("personality") or [])),
            str(character.get("role") or ""),
            str(character.get("speech_style") or ""),
            str(character.get("background") or "")[:200],
        ]
    )
    valence = DEFAULT_BASELINE["valence"]
    arousal = DEFAULT_BASELINE["arousal"]
    for keywords, dv, da in BASELINE_RULES:
        if any(word in traits for word in keywords):
            valence += dv
            arousal += da
    valence = _clamp(valence, -1.0, 1.0)
    arousal = _clamp(arousal, 0.0, 1.0)
    return {"valence": round(valence, 3), "arousal": round(arousal, 3)}


def label_for(valence: float, arousal: float, hint: Optional[str] = None) -> str:
    """把 (valence, arousal) 映射成 9 个离散情绪之一。

    顺序有讲究：先判"高唤醒的负面"（恼怒/厌恶），再判"中等的负面"（警惕/失落）。
    否则情绪会掉进「明明很不爽却显示平静」的死区。
    """
    if hint:
        return hint
    if valence <= -0.45 and arousal >= 0.55:
        return "恼怒"
    if valence <= -0.30 and arousal >= 0.40:
        return "厌恶"
    if valence <= -0.15 and arousal >= 0.35:
        return "警惕"
    if valence <= -0.20:
        return "失落"
    if valence >= 0.50 and arousal >= 0.55:
        return "喜悦"
    if valence >= 0.30 and arousal >= 0.40:
        return "期待"
    if valence >= 0.25:
        return "关切"
    if arousal >= 0.65:
        return "警惕"
    return "平静"


def extract_signals(text: str) -> list[Signal]:
    """从用户消息里识别言语行为。同一类只算一次（避免"谢谢谢谢谢谢"刷分）。"""
    text = (text or "").strip()
    if not text:
        return []

    signals: list[Signal] = []
    for kind, words, dv, da, df in SIGNAL_RULES:
        for word in words:
            if word in text:
                signals.append(
                    Signal(kind=kind, d_valence=dv, d_arousal=da, d_favorability=df, evidence=word)
                )
                break
    return signals


def _has_conflict(signals: list[Signal]) -> bool:
    kinds = {s.kind for s in signals}
    return bool(kinds & _POSITIVE_KINDS) and bool(kinds & _NEGATIVE_KINDS)


def _needs_model(signals: list[Signal], text: str) -> bool:
    """什么时候才值得动用情绪模型：规则没命中且句子够长，或正负打架。"""
    if _has_conflict(signals):
        return True
    return not signals and len((text or "").strip()) >= MODEL_FALLBACK_CHARS


def shelf_for(character: dict) -> str:
    """角色 -> 所属书架（情绪按书架隔离）。"""
    book = db.get_book(str(character.get("book_id") or ""))
    return str((book or {}).get("shelf_id") or "")


def load_state(character: dict, user_id: str) -> dict:
    """读状态；没有就按基线初始化。基线随角色编辑更新。"""
    character_id = str(character.get("id") or "")
    shelf_id = shelf_for(character)
    baseline = baseline_for(character)
    row = db.get_affect(character_id, user_id, shelf_id)
    if row is None:
        return {
            "character_id": character_id,
            "user_id": user_id,
            "shelf_id": shelf_id,
            "favorability": DEFAULT_FAVORABILITY,
            "valence": baseline["valence"],
            "arousal": baseline["arousal"],
            "emotion": label_for(baseline["valence"], baseline["arousal"]),
            "baseline": baseline,
            "turns": 0,
            "evidence_event_id": None,
        }
    if not row.get("baseline"):
        row["baseline"] = baseline
    return row


# ---------------------------------------------------------------------------
# 语气策略（给提示词用，不含任何数字）
# ---------------------------------------------------------------------------
def tone_band(favorability: int) -> dict:
    for low, high, key, label, guidance in FAVORABILITY_BANDS:
        if low <= favorability <= high:
            return {"key": key, "label": label, "guidance": guidance}
    high_band = FAVORABILITY_BANDS[-1]
    return {"key": high_band[2], "label": high_band[3], "guidance": high_band[4]}


def forbidden_rules(state: dict) -> list[str]:
    """硬约束：防止"好感度很低却很亲密"这类崩人设。"""
    rules: list[str] = []
    favorability = int(state.get("favorability") or 0)
    emotion = str(state.get("emotion") or "平静")
    if favorability < 30:
        rules.append("不要用亲昵称呼（宝、亲爱的、小名），不要主动示好")
    if favorability > 70:
        rules.append("不要表现出无来由的敌意或猜忌")
    if emotion in ("恼怒", "厌恶"):
        rules.append("可以有情绪，但不要人身攻击、不要跳出角色身份")
    return rules


def describe(state: dict) -> dict:
    """把内部状态翻译成**给人看的人话**。

    刻意不返回 favorability / valence / arousal —— 用户看不到数值，也不需要：
    他只需要知道"她现在什么心情、对我什么态度"。
    """
    emotion = str(state.get("emotion") or "平静")
    band = tone_band(int(state.get("favorability") or DEFAULT_FAVORABILITY))
    return {
        "mood": emotion,
        "mood_text": MOOD_TEXT.get(emotion, MOOD_TEXT["平静"]),
        "attitude_text": ATTITUDE_TEXT.get(band["key"], ATTITUDE_TEXT["normal"]),
    }


def prompt_block(state: dict) -> str:
    """把状态编译成给模型的表达要求。**不含任何数值。**"""
    emotion = str(state.get("emotion") or "平静")
    band = tone_band(int(state.get("favorability") or DEFAULT_FAVORABILITY))
    style = EMOTION_STYLE.get(emotion, EMOTION_STYLE["平静"])

    lines = [
        "【此刻状态】",
        f"心情：{emotion}（{style}）",
        f"你对对方的态度：{band['label']}（{band['guidance']}）",
        "【表达规则】",
        "- 用言行体现情绪，不要说破「情绪值」「好感度」这类内部信息。",
        "- 情绪低落时可以直接表达（例如「我太伤心了，呜呜」），不要忍着。",
    ]
    for rule in forbidden_rules(state):
        lines.append(f"- {rule}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主入口：吃一条用户消息，推进状态
# ---------------------------------------------------------------------------
def observe_user_message(
    *,
    character: dict,
    user_id: str,
    text: str,
    image_count: int = 0,
    session_id: str = "",
    trace_id: str = "",
) -> dict:
    """根据用户这一条消息推进情绪与好感度，落库并写 ``affect_change`` 事件。

    **永不抛异常**：情绪出问题不能影响聊天。
    """
    try:
        return _observe(
            character=character,
            user_id=user_id,
            text=text,
            image_count=image_count,
            session_id=session_id,
            trace_id=trace_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("情绪更新失败（已忽略，不影响回复）")
        return {}


def _observe(
    *,
    character: dict,
    user_id: str,
    text: str,
    image_count: int,
    session_id: str,
    trace_id: str,
) -> dict:
    state = load_state(character, user_id)
    baseline = state.get("baseline") or baseline_for(character)

    before = {
        "favorability": int(state["favorability"]),
        "valence": round(float(state["valence"]), 3),
        "arousal": round(float(state["arousal"]), 3),
        "emotion": state["emotion"],
    }

    # 1) 情绪先自然回落（好感度不回落）
    valence = _toward(float(state["valence"]), float(baseline["valence"]), DECAY_VALENCE)
    arousal = _toward(float(state["arousal"]), float(baseline["arousal"]), DECAY_AROUSAL)
    favorability = int(state["favorability"])

    # 2) 言语行为信号
    signals = extract_signals(text)
    dv = sum(s.d_valence for s in signals)
    da = sum(s.d_arousal for s in signals)
    df = sum(s.d_favorability for s in signals)

    # 3) 规则打架 -> 减半，交给模型修正
    conflicted = _has_conflict(signals)
    if conflicted:
        dv *= 0.5
        da *= 0.5
        df = int(round(df * 0.5))

    # 4) 兜底：规则读不懂时才问情绪模型
    model_signal = None
    if _needs_model(signals, text):
        model_signal = emotion_model.analyze(text)
        if model_signal is not None and model_signal.confidence >= 0.5:
            dv += model_signal.polarity * 0.15 * model_signal.confidence
            da += abs(model_signal.polarity) * 0.08 * model_signal.confidence
            df += int(round(model_signal.polarity * 1 * model_signal.confidence))
        else:
            model_signal = None

    # 5) 只发图不说话：轻微正反馈
    if image_count and not (text or "").strip():
        dv += 0.05
        da += 0.05

    # 6) 单轮上限
    dv = _clamp(dv, -MAX_DELTA_VALENCE, MAX_DELTA_VALENCE)
    da = _clamp(da, -MAX_DELTA_AROUSAL, MAX_DELTA_AROUSAL)
    df = int(_clamp(df, -MAX_DELTA_FAVORABILITY, MAX_DELTA_FAVORABILITY))

    valence = _clamp(valence + dv, -1.0, 1.0)
    arousal = _clamp(arousal + da, 0.0, 1.0)
    favorability = int(_clamp(favorability + df, 0, 100))

    # 7) 情绪标签：优先被"关切/亲密"这类信号定性
    hint = None
    for signal in signals:
        if signal.kind in _SIGNAL_LABEL_HINT and valence >= 0:
            hint = _SIGNAL_LABEL_HINT[signal.kind]
            break
    emotion = label_for(valence, arousal, hint)

    after = {
        "favorability": favorability,
        "valence": round(valence, 3),
        "arousal": round(arousal, 3),
        "emotion": emotion,
    }

    # 8) 没变化就不写事件，省日志
    changed = before != after
    event = None
    if changed:
        event = agent_log.log(
            "affect_change",
            owner_type="character",
            owner_id=str(character.get("id") or ""),
            book_id=str(character.get("book_id") or ""),
            shelf_id=state.get("shelf_id") or "",
            user_id=user_id,
            session_id=session_id,
            trace_id=trace_id,
            payload={
                "before": before,
                "after": after,
                "signals": [
                    {"kind": s.kind, "evidence": s.evidence, "df": s.d_favorability}
                    for s in signals
                ],
                "conflicted": conflicted,
                "model": (
                    {
                        "backend": model_signal.backend,
                        "polarity": model_signal.polarity,
                        "confidence": model_signal.confidence,
                    }
                    if model_signal
                    else None
                ),
            },
        )

    return_value = db.save_affect(
        str(character.get("id") or ""),
        user_id,
        state.get("shelf_id") or "",
        favorability=favorability,
        valence=valence,
        arousal=arousal,
        emotion=emotion,
        baseline=baseline,
        turns=int(state.get("turns") or 0) + 1,
        evidence_event_id=(event or {}).get("id"),
    )
    # 调用方要判断"这一轮心情变没变"（决定要不要发表情贴纸），
    # 所以把变化前的心情一起带回去，省一次额外查询。
    if isinstance(return_value, dict):
        return_value["previous_emotion"] = before["emotion"]
    return return_value