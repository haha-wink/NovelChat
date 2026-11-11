"""角色性格画像：把「性格描述」翻译成「能接住什么情绪」。

## 为什么需要这一层

群聊选人原来只看「外向度」——话多的先开口。但用户的诉求不是"谁话多"，
而是"谁能接住我现在这样"。一个寡言但体贴的人，比一个话多但暴躁的人更该来安慰。

性格数据在库里长这样（抽取阶段写进去的）：

    楚子航: ["沉默寡言", "执着", "孤独", "重情"]
    恺撒:  ["话多，动辄长篇（平均 46.8 字/句）", "语气激烈，常带感叹"]
    路明非: ["语气平缓，几乎不用感叹号", "常把话说一半（多用省略号）"]

**没有一条是标准的性格词**。所以这里做两层：

1. 关键词桶：既认抽象词（温柔/冷静/暴躁），也认上面这类风格短语（平缓/话多/激烈）
2. 五维向量 -> 按用户情绪算适配度

## 五个维度

| 维度 | 含义 | 高的人适合 |
|---|---|---|
| `warmth` | 温柔、体贴、耐心 | 难过、疲惫、危机 |
| `energy` | 热情、话多、外向 | 开心、闲聊、振奋 |
| `calmness` | 冷静、理性、沉稳 | 焦虑、愤怒、危机 |
| `sharpness` | 锋利、暴躁、毒舌 | 几乎不适配（负面项） |
| `playfulness` | 天真、幽默、爱玩 | 开心、闲聊 |
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 关键词桶：抽象性格词 + 抽取阶段产出的风格短语
# ---------------------------------------------------------------------------

TRAIT_WORDS: dict[str, tuple[str, ...]] = {
    "warmth": (
        "温柔", "温和", "善良", "体贴", "耐心", "包容", "细心", "温暖", "柔和",
        "善解人意", "会照顾", "亲切", "关怀", "柔软",
    ),
    "energy": (
        "热情", "开朗", "活泼", "话多", "健谈", "直爽", "豪爽", "爽朗", "外向",
        "主动", "爱说", "长篇", "感叹",
    ),
    "calmness": (
        "冷静", "克制", "理性", "沉稳", "镇定", "认真", "严肃", "逻辑", "平缓",
        "平静", "客观", "深思", "分寸",
    ),
    "sharpness": (
        "暴躁", "易怒", "冲动", "急躁", "毒舌", "刻薄", "冷嘲", "尖锐", "傲慢",
        "激烈", "咄咄逼人", "不耐烦",
    ),
    "playfulness": (
        "天真", "单纯", "烂漫", "顽皮", "幽默", "爱玩", "打趣", "俏皮", "孩子气",
        "活泼可爱", "点子多",
    ),
}

#: 各维度的基准值——都命不中时，人就是"普通温和"的
BASELINE = {
    "warmth": 0.45,
    "energy": 0.40,
    "calmness": 0.45,
    "sharpness": 0.15,
    "playfulness": 0.30,
}


@dataclass
class Temperament:
    """一个角色的性格向量（0~1）。"""

    warmth: float = BASELINE["warmth"]
    energy: float = BASELINE["energy"]
    calmness: float = BASELINE["calmness"]
    sharpness: float = BASELINE["sharpness"]
    playfulness: float = BASELINE["playfulness"]
    #: 命中的关键词，排查用
    hits: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, float]:
        return {
            "warmth": round(self.warmth, 3),
            "energy": round(self.energy, 3),
            "calmness": round(self.calmness, 3),
            "sharpness": round(self.sharpness, 3),
            "playfulness": round(self.playfulness, 3),
        }


#: 情绪 -> 各维度的权重（加起来必须为正；sharpness 在安抚场景是负权重）
FIT_WEIGHTS: dict[str, dict[str, float]] = {
    "难过": {"warmth": 0.60, "calmness": 0.25, "energy": 0.10, "sharpness": -0.45, "playfulness": -0.05},
    "焦虑": {"warmth": 0.45, "calmness": 0.40, "energy": 0.05, "sharpness": -0.40, "playfulness": -0.05},
    "疲惫": {"warmth": 0.55, "calmness": 0.25, "energy": 0.05, "sharpness": -0.35, "playfulness": 0.00},
    "愤怒": {"calmness": 0.55, "warmth": 0.30, "energy": -0.05, "sharpness": -0.50, "playfulness": -0.10},
    "开心": {"energy": 0.45, "playfulness": 0.40, "warmth": 0.15, "calmness": 0.00, "sharpness": -0.20},
    "平静": {"energy": 0.30, "playfulness": 0.25, "calmness": 0.20, "warmth": 0.20, "sharpness": -0.30},
}

#: 危机场景单独一套：要稳、要暖，不要活泼也不要锋利
CRISIS_WEIGHTS = {
    "warmth": 0.55,
    "calmness": 0.40,
    "energy": 0.05,
    "sharpness": -0.60,
    "playfulness": -0.20,
}

#: 权重的理论最大/最小，用来把结果归一化到 0~1
_MAX_FIT = sum(value for value in FIT_WEIGHTS["难过"].values() if value > 0)
_MIN_FIT = sum(value for value in FIT_WEIGHTS["难过"].values() if value < 0)


def _traits_text(character: dict) -> str:
    parts: list[str] = []
    for key in ("personality", "role", "speech_style", "background"):
        value = character.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return " ".join(parts)


def analyze_temperament(character: dict) -> Temperament:
    """从角色档案里读出五维性格。

    命中多少个关键词就加多少，最后截断到 0~1。同一维度最多被 3 个词加权，
    避免"温柔温柔温柔"这种重复把分数顶满。
    """
    text = _traits_text(character)
    result = Temperament()
    hits: list[str] = []

    for dimension, words in TRAIT_WORDS.items():
        matched = [word for word in words if word in text][:3]
        if not matched:
            continue
        hits.extend(matched)
        boost = 0.18 * len(matched)
        current = getattr(result, dimension)
        setattr(result, dimension, min(1.0, current + boost))

    result.hits = hits
    return result


def fit_for(character: dict, *, emotion: str, crisis: bool = False) -> float:
    """这个角色接住「用户这个情绪」的适配度（0~1）。

    高于 0.5 = 比一般人更合适；低于 0.3 = 最好别让他来。
    """
    traits = analyze_temperament(character)
    weights = CRISIS_WEIGHTS if crisis else FIT_WEIGHTS.get(emotion, FIT_WEIGHTS["平静"])
    values = traits.as_dict()

    positive = sum(weight * values[dim] for dim, weight in weights.items() if weight > 0)
    negative = sum(weight * values[dim] for dim, weight in weights.items() if weight < 0)

    span = sum(weight for weight in weights.values() if weight > 0)
    span = span or 1.0
    raw = (positive + negative) / span  # 负数会把分往下拉
    return round(max(0.0, min(1.0, raw)), 3)


def describe(character: dict) -> dict:
    """给日志/排查用的一句话画像。"""
    traits = analyze_temperament(character)
    return {
        "name": character.get("name"),
        "traits": traits.as_dict(),
        "hits": traits.hits,
        "fit_难过": fit_for(character, emotion="难过"),
        "fit_开心": fit_for(character, emotion="开心"),
        "fit_危机": fit_for(character, emotion="难过", crisis=True),
    }