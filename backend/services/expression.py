"""表达层：把「此刻的情绪」翻译成具体怎么说话——贴纸、语气。

情绪模型算出 valence/arousal，好感度给出距离感，但这些都是数字。
真正让角色"活"的是表达：**什么时候该甩个表情，什么时候不该**。

设计要点：

* **不是每句都发表情**。真人只在情绪够强时才用表情，所以这里按 arousal 给概率，
  上限也就三分之一。否则每条消息都挂个笑脸，比不说话还假。
* **贴纸用 Twemoji**（CC-BY 4.0，开源可商用），用 `<img>` 渲染而不是直接写 emoji 字符，
  这样在 Windows / 安卓 / iOS 上长得一样。原生 emoji 字符各平台差异很大。
* 情绪标签直接复用 ``affect.label_for()`` 的九个标签，不另造一套。
"""

from __future__ import annotations

import logging
import random
import re
from typing import Optional

logger = logging.getLogger(__name__)

#: 每个情绪对应的候选贴纸。codepoint 是 Twemoji 的文件名（小写十六进制）。
EMOTION_STICKERS: dict[str, list[tuple[str, str]]] = {
    "平静": [("1f610", "😐"), ("1f60c", "😌")],
    "喜悦": [("1f604", "😄"), ("1f60a", "😊"), ("2728", "✨")],
    "期待": [("1f929", "🤩"), ("1f440", "👀")],
    "关切": [("1f97a", "🥺"), ("1f917", "🤗")],
    "羞赧": [("1f633", "😳"), ("1f60a", "😊")],
    "失落": [("1f622", "😢"), ("1f61e", "😞"), ("1f4a7", "💧")],
    "恼怒": [("1f620", "😠"), ("1f624", "😤")],
    "警惕": [("1f928", "🤨"), ("1f440", "👀")],
    "厌恶": [("1f644", "🙄"), ("1f612", "😒")],
}

#: 情绪不明确时的兜底
FALLBACK_EMOTION = "平静"

#: 用户是不是在问"你现在怎么样"——这类问句按规则必须用贴纸回答
MOOD_QUESTION = re.compile(
    r"(心情|情绪|状态|感觉|感受).{0,6}(怎么样|如何|好吗|呢|吗|？|\?)"
    # 「你是不是生气了」这类问句没有问号也没有"吗"，靠"你 + 情绪词"识别
    r"|(你|您).{0,5}(还好吗|怎么了|怎么样|开心|难过|生气|委屈|害怕|高兴|不开心|还好吧)"
    r"|^(在吗|在不在|你还好吗)[？?！!。]?$"
    r"|(开心|难过|生气|委屈|害怕|高兴)(吗|么|不)"
)

#: 概率参数：够激动才发，最多也就 30%
BASE_CHANCE = 0.08
AROUSAL_WEIGHT = 0.18
INTENSITY_WEIGHT = 0.10
MAX_CHANCE = 0.30


def _intensity(valence: float) -> float:
    """情绪强度：离中性越远越强（0~1）。"""
    return min(1.0, abs(float(valence or 0.0)))


def sticker_chance(state: dict) -> float:
    """这条回复带贴纸的概率（0~1）。"""
    arousal = max(0.0, min(1.0, float(state.get("arousal") or 0.0)))
    intensity = _intensity(state.get("valence") or 0.0)
    return min(
        MAX_CHANCE,
        BASE_CHANCE + AROUSAL_WEIGHT * arousal + INTENSITY_WEIGHT * intensity,
    )


def pick_sticker(state: dict, *, rng: Optional[random.Random] = None) -> dict:
    """按情绪挑一张贴纸（必然返回，不掷骰子）。"""
    rng = rng or random
    raw_emotion = str(state.get("emotion") or FALLBACK_EMOTION)
    # 未知情绪按平静处理（返回的 emotion 也写平静，免得调用方以为真的是那个情绪）
    emotion = raw_emotion if raw_emotion in EMOTION_STICKERS else FALLBACK_EMOTION
    candidates = EMOTION_STICKERS[emotion]
    codepoint, emoji = candidates[rng.randrange(len(candidates))]
    return {
        "emotion": emotion,
        "codepoint": codepoint,
        "emoji": emoji,
        "url": f"/api/sticker/{codepoint}.svg",
    }


def asks_about_mood(text: str) -> bool:
    """用户是不是在问角色的状态/心情（问句 -> 必须用贴纸回答）。"""
    return bool(MOOD_QUESTION.search(str(text or "")))


def maybe_sticker(
    state: dict,
    *,
    mood_changed: bool = False,
    asked_about_mood: bool = False,
    rng: Optional[random.Random] = None,
) -> Optional[dict]:
    """决定这句要不要带贴纸；不带就返回 None。

    **规则优先于概率**：

    * 用户问起状态/心情 -> 一定发（表情就是最直接的回答）
    * 这一轮心情变了      -> 一定发（让情绪变化被看见）
    * 其余按 arousal 掷骰子

    ``state`` 需要含 ``emotion`` / ``valence`` / ``arousal``（缺了就当平静处理）。
    """
    rng = rng or random
    try:
        if asked_about_mood or mood_changed:
            return pick_sticker(state or {}, rng=rng)
        chance = sticker_chance(state or {})
        if rng.random() >= chance:
            return None
        return pick_sticker(state or {}, rng=rng)
    except Exception:  # noqa: BLE001 - 贴纸失败绝不能影响聊天
        logger.exception("挑贴纸失败")
        return None


def catalog() -> dict:
    """给前端/调试用的贴纸目录。"""
    return {
        emotion: [
            {"codepoint": codepoint, "emoji": emoji, "url": f"/api/sticker/{codepoint}.svg"}
            for codepoint, emoji in pairs
        ]
        for emotion, pairs in EMOTION_STICKERS.items()
    }


def all_codepoints() -> set[str]:
    return {codepoint for pairs in EMOTION_STICKERS.values() for codepoint, _ in pairs}