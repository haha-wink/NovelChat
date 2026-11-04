"""场景识别：把一段话/一条记忆归到某个场景标签。

为什么需要场景：角色的记忆是"碎片"，直接按相关度检索出来的东西经常跨场景乱跳。
用户正聊着雨夜的车站，却把校园食堂的片段捞出来，会很出戏。

规则式就够用——场景标签是**粗粒度**的（十来类），关键词命中比模型更可控、更可测。
分错一个场景的代价很小（只是检索加权），而引入模型的代价是依赖和延迟。
"""

from __future__ import annotations

from typing import Optional

#: 场景 -> 关键词。**顺序有意义**：先匹配到的优先（战斗/离别这类强场景放前面）
SCENE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("战斗", ("剑", "刀", "枪", "杀", "血", "追", "爆炸", "开火", "战", "攻击", "击退", "刀锋")),
    ("离别", ("再见", "告别", "离开", "走了", "送别", "分手", "永别", "背影")),
    ("争执", ("生气", "争吵", "吵", "怒", "骂", "反驳", "吼", "翻脸")),
    ("告白", ("喜欢", "爱你", "表白", "心动", "脸红", "求婚")),
    ("雨夜", ("雨", "伞", "湿透", "雷", "夜色")),
    ("校园", ("教室", "学校", "同学", "上课", "考试", "宿舍", "操场", "校长", "学院")),
    ("酒馆", ("酒", "喝", "醉", "酒吧", "碰杯")),
    ("旅途", ("车站", "列车", "火车", "机场", "飞机", "路上", "出发")),
    ("日常", ("吃", "饭", "睡", "天气", "早饭", "午饭", "晚饭", "聊天")),
)

#: 什么都没命中时的标签
DEFAULT_SCENE = "日常"

#: 任务/事件类记忆（蒸馏出来的"我们聊过什么"）单独一类
DIALOGUE_SCENE = "对话"


def detect(text: str, *, default: str = DEFAULT_SCENE) -> str:
    """识别一句话最可能的场景；识别不出返回 ``default``。"""
    content = str(text or "")
    if not content:
        return default
    for scene, keywords in SCENE_RULES:
        if any(keyword in content for keyword in keywords):
            return scene
    return default


def detect_many(texts: list[str], *, default: str = DEFAULT_SCENE) -> str:
    """多条文本取"命中最多"的场景（用于给一段对话打标签）。"""
    votes: dict[str, int] = {}
    for text in texts:
        scene = detect(text, default="")
        if scene:
            votes[scene] = votes.get(scene, 0) + 1
    if not votes:
        return default
    return max(votes.items(), key=lambda item: (item[1], item[0]))[0]


def label(text: str, *, default: Optional[str] = None) -> str:
    """给记忆条目用的展示标签。"""
    scene = detect(text, default=default or DEFAULT_SCENE)
    return f"（{scene}）"