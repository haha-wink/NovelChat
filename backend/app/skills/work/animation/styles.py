"""动画风格目录：用户话里的风格词 → 风格键；风格键 → 调色/纸墨/叠加层参数。

模板只有两套**画法**（水彩笔刷 / 像素逻辑网格），所以绝大多数「风格」不换画法，
只换观感：纸色、墨色、整幅调色（色相/饱和度/亮度）、叠加层（扫描线、暗角）。
这些参数由 ``scene_gen`` 注入 ``generated.js`` 的 ``STYLE_PACK``，在浏览器里生效；
``engine._apply_style_config`` 则把风格键写进 ``config.js`` 的 ``PROJECT.style``。

新增风格：往 ``STYLES`` 里加一条（``words`` 写用户可能说的词），再把键加进 ``ORDER``；
画法级的新风格（比如真正的油画笔刷）才需要动模板的 ``src/``。
"""

from __future__ import annotations

#: 风格键 → 参数。hue/sat/light 是整幅调色的乘/加量（色相按度、饱和/亮度按倍率）。
STYLES: dict[str, dict] = {
    "watercolor": {
        "label": "水彩",
        "words": ("水彩", "手绘", "watercolor", "watercolour"),
        "paper": "#F3EBDC", "ink": "#2B2233",
        "hue": 0, "sat": 1.0, "light": 1.0, "overlay": "none",
    },
    "pixel": {
        "label": "像素",
        "words": ("像素", "pixel", "8bit", "8-bit", "点阵"),
        "paper": "#0B0E18", "ink": "#0B0E18",
        "hue": 0, "sat": 1.0, "light": 1.0, "overlay": "none",
    },
    "ink": {
        "label": "水墨",
        "words": ("水墨", "国画", "国风", "宣纸", "山水画", "写意", "ink"),
        "paper": "#F2E9D8", "ink": "#1F1C18",
        "hue": -6, "sat": 0.18, "light": 1.02, "overlay": "none",
    },
    "neon": {
        "label": "赛博朋克",
        "words": ("赛博", "赛博朋克", "霓虹", "cyberpunk", "neon", "科幻", "未来感"),
        "paper": "#070A14", "ink": "#0A0E1A",
        "hue": 38, "sat": 1.55, "light": 0.62, "overlay": "scanlines",
    },
    "film": {
        "label": "胶片",
        "words": ("胶片", "复古", "怀旧", "港风", "film", "retro", "老电影"),
        "paper": "#E9DDC6", "ink": "#241E1A",
        "hue": 12, "sat": 0.78, "light": 1.0, "overlay": "vignette",
    },
    "noir": {
        "label": "黑白",
        "words": ("黑白", "黑白片", "单色", "noir", "monochrome", "b&w"),
        "paper": "#F4F1E8", "ink": "#0E0E0E",
        "hue": 0, "sat": 0.0, "light": 1.04, "overlay": "vignette",
    },
    "pastel": {
        "label": "小清新",
        "words": ("小清新", "清新", "柔光", "奶油", "pastel", "soft", "甜"),
        "paper": "#FFFDF4", "ink": "#3A3238",
        "hue": 0, "sat": 0.55, "light": 1.16, "overlay": "none",
    },
    "anime": {
        "label": "动漫",
        "words": ("动漫", "二次元", "漫画", "番剧", "anime", "cel", "赛璐璐"),
        "paper": "#FFFFFF", "ink": "#1A1A1A",
        "hue": 0, "sat": 1.45, "light": 1.1, "overlay": "none",
    },
    "sunset": {
        "label": "日落",
        "words": ("日落", "黄昏", "晚霞", "夕阳", "sunset", "dusk", "暖色调"),
        "paper": "#F6E3C4", "ink": "#2A1F22",
        "hue": 26, "sat": 1.25, "light": 0.98, "overlay": "vignette",
    },
}

#: 识别顺序：先具体后笼统；watercolor 是兜底，不参与关键词匹配
ORDER: tuple[str, ...] = (
    "pixel", "ink", "neon", "film", "noir", "pastel", "anime", "sunset", "watercolor",
)

DEFAULT = "watercolor"


def resolve_style(text: str = "", *, fallback: str = DEFAULT) -> str:
    """从用户原话/参数里认出风格键；认不出返回 fallback（默认水彩）。"""
    body = str(text or "").lower()
    if body in STYLES:  # 直接传键（模型/接口就这么传）
        return body
    for key in ORDER:
        if key == DEFAULT:
            continue
        for word in STYLES[key]["words"]:
            if word.lower() in body:
                return key
    return fallback if fallback in STYLES else DEFAULT


def style_label(key: str) -> str:
    return str(STYLES.get(str(key or ""), STYLES[DEFAULT])["label"])


def style_pack(key: str) -> dict:
    """给 generated.js 用的参数（只带视觉字段，不带触发词）。"""
    item = STYLES.get(str(key or ""), STYLES[DEFAULT])
    return {
        "key": str(key or DEFAULT),
        "label": item["label"],
        "paper": item["paper"],
        "ink": item["ink"],
        "hue": item["hue"],
        "sat": item["sat"],
        "light": item["light"],
        "overlay": item["overlay"],
    }


def catalog() -> list[dict]:
    """工具描述用：〔键/中文名〕列表。"""
    return [
        {"key": key, "label": STYLES[key]["label"]}
        for key in ORDER
    ]