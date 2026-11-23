"""按歌词逐句生成场景脚本（scene JS）。

上游模板只有 ``src/scenes/demo.js`` 一个示例场景（"坠落的小星星"：小人 + 月亮 + 山丘），
不管歌词唱什么，画面永远是那一幅。本模块按**每一句歌词的内容**生成一个独立镜头：

- 关键词（雨 / 伞 / 雪 / 海 / 车站 / 星 / 月亮 / 夜 / 灯 / 窗 / 花 / 路 / 风 / 山 / 城市…）
  决定天空、天气、主体、构图和转场；
- 抽不到关键词时按镜头序号轮换预设构图，不会千篇一律；
- ``style == "pixel"`` 走像素模式的逻辑网格原语（``src/pixel.js``），否则走水彩笔刷（``paint()`` / ``inkLine()``）。

生成的 JS 写进**任务目录副本**的 ``project/src/scenes/generated.js``，并把
``project/studio.html`` 里对 ``demo.js`` 的引用换成它。``vendor/template/`` 只读，这里不碰。

对外接口：

    build_scene(timeline, *, style="watercolor") -> str
    scene_keywords(line) -> list[str]
    install(project_dir, timeline, *, style="watercolor") -> dict
"""

import json
import re
from pathlib import Path

try:  # 同包的风格目录
    from . import styles as style_lib
except ImportError:  # pragma: no cover - 单独跑单文件时
    from skills.work.animation import styles as style_lib

# ---------------------------------------------------------------------------
# 关键词：画面元素 → 歌词里可能出现的写法
# ---------------------------------------------------------------------------
# 键就是 scene_keywords() 返回的词；值里的写法默认按长度从长到短匹配，
# 所以"小雨"会命中"雨"，"车站"不会又被拆出一个"车"。
_ELEMENTS: dict[str, tuple[str, ...]] = {
    "雨": ("下雨", "大雨", "小雨", "细雨", "阵雨", "雨滴", "雨点", "雨水", "淋雨", "雨"),
    "伞": ("撑伞", "雨伞", "打伞", "伞"),
    "雪": ("雪花", "大雪", "小雪", "风雪", "下雪", "雪"),
    "霜": ("霜", "结霜"),
    "星": ("星星", "星光", "星辰", "繁星", "流星", "银河", "星"),
    "月亮": ("月亮", "月光", "月牙", "月色", "明月", "月"),
    "太阳": ("太阳", "阳光", "日出", "日落", "夕阳", "黄昏", "朝阳", "朝阳"),
    "夜": ("深夜", "夜晚", "夜里", "午夜", "黑夜", "晚上", "夜"),
    "云": ("白云", "乌云", "云海", "云端", "云朵", "云"),
    "风": ("微风", "大风", "吹过", "风吹", "风"),
    "海": ("大海", "海边", "海浪", "沙滩", "海岸", "浪", "潮水", "潮", "海"),
    "船": ("小船", "帆船", "渡船", "船"),
    "山": ("远山", "山谷", "山顶", "山峰", "山岭", "山"),
    "河": ("河水", "河流", "小河", "江水", "溪水", "湖泊", "河", "江"),
    "路": ("马路", "路口", "道路", "小道", "铁轨", "远方", "天涯", "路"),
    "走": ("走路", "行走", "走远", "走过", "离开", "出发", "远行", "跑去", "走"),
    "回家": ("回家", "归途", "故乡", "归来"),
    "车站": ("车站", "站台", "月台", "站牌", "候车", "末班车"),
    "车": ("列车", "火车", "地铁", "班车", "汽车", "单车", "自行车"),
    "灯": ("路灯", "台灯", "灯火", "灯光", "霓虹", "烛光", "街灯", "灯"),
    "窗": ("窗台", "窗外", "窗户", "玻璃", "窗"),
    "花": ("花瓣", "鲜花", "玫瑰", "樱花", "野花", "开花", "花"),
    "草": ("青草", "草地", "野草", "草"),
    "树": ("树林", "树叶", "树枝", "大树", "树"),
    "城市": ("城市", "高楼", "大厦", "都市", "人间", "街"),
    "房间": ("房间", "屋子", "屋里", "家里", "屋内"),
    "眼泪": ("眼泪", "泪光", "哭泣", "哭", "泪"),
    "拥抱": ("拥抱", "牵手", "抱着", "抱"),
    "秋天": ("秋天", "落叶", "枫叶", "秋"),
    "春天": ("春天", "春"),
    "夏天": ("夏天", "夏"),
}

#: 元素 → 构图（一个构图覆盖多个同族元素）
_ELEMENT_KIND: dict[str, str] = {
    "车站": "station",
    "车": "station",
    "下雨": "rain",
    "雨": "rain",
    "伞": "rain",
    "雪": "snow",
    "霜": "snow",
    "海": "sea",
    "船": "sea",
    "河": "sea",
    "星": "night",
    "月亮": "night",
    "夜": "night",
    "灯": "window",
    "窗": "window",
    "房间": "window",
    "城市": "city",
    "路": "road",
    "走": "road",
    "回家": "road",
    "花": "meadow",
    "草": "meadow",
    "树": "meadow",
    "春天": "meadow",
    "夏天": "meadow",
    "秋天": "meadow",
    "风": "wind",
    "云": "wind",
    "山": "mountain",
    "太阳": "mountain",
}

#: 一句歌词命中多个构图时的优先级（越靠前越优先，先定"在哪里"，再让天气当叠加）
_KIND_ORDER = (
    "station",
    "rain",
    "snow",
    "sea",
    "night",
    "city",
    "window",
    "road",
    "meadow",
    "wind",
    "mountain",
)

#: 没有关键词时的预设构图，按镜头序号轮换
_FALLBACK_KINDS = ("meadow", "night", "road", "sea", "mountain", "city", "window", "wind")

#: 构图 → 相机运动（pan = 横向平移，road 专用）
_CAMERA: dict[str, str] = {
    "rain": "hand",
    "night": "rise",
    "station": "push",
    "sea": "drift",
    "meadow": "pan",
    "wind": "drift",
    "window": "push",
    "road": "pan",
    "snow": "drift",
    "mountain": "drift",
    "city": "rise",
}

#: 构图 → 情绪曲线（镜头开始时 → 镜头后段），每镜都有一次情绪变化
_MOOD: dict[str, tuple[str, str]] = {
    "rain": ("sad", "hopeful"),
    "night": ("hopeful", "starstruck"),
    "station": ("nervous", "hopeful"),
    "sea": ("thinking", "relieved"),
    "meadow": ("happy", "laugh"),
    "wind": ("excited", "proud"),
    "window": ("sad", "relieved"),
    "road": ("determined", "hopeful"),
    "snow": ("shy", "happy"),
    "mountain": ("proud", "hopeful"),
    "city": ("cool", "starstruck"),
}


# ---------------------------------------------------------------------------
# 歌词 → 关键词 / 镜头计划
# ---------------------------------------------------------------------------
def scene_keywords(line: str) -> list[str]:
    """从一句歌词里抽出画面元素关键词（中文）。

    返回的元素按在歌词里出现的先后排序，每个元素最多出现一次；
    长写法优先（"末班车"命中"车"就不再让"车站"蹭到，"小雨"只会留下"雨"）。
    """
    text = str(line or "")
    if not text:
        return []

    # 先按"最长写法"排序元素，命中后把那段文字抹成等宽的占位符，
    # 这样后面的元素不会在同一段文字里重复命中，且下标不变。
    elements = sorted(_ELEMENTS, key=lambda key: -max(len(alias) for alias in _ELEMENTS[key]))
    hits: list[tuple[int, str]] = []
    scanned = text
    for element in elements:
        for alias in sorted(_ELEMENTS[element], key=len, reverse=True):
            position = scanned.find(alias)
            if position >= 0:
                hits.append((position, element))
                scanned = scanned[:position] + "\u3000" * len(alias) + scanned[position + len(alias):]
                break

    hits.sort(key=lambda item: item[0])
    return [element for _position, element in hits]


#: 没有关键词时的兜底构图：不同风格先给不同场景
#: （赛博朋克先城市/夜，水墨先山/风，胶片先城市/路，日落先海/山……）
_STYLE_FALLBACK: dict[str, tuple[str, ...]] = {
    "watercolor": _FALLBACK_KINDS,
    "pixel": _FALLBACK_KINDS,
    "ink": ("mountain", "wind", "sea", "night", "meadow", "road", "window", "city"),
    "neon": ("city", "night", "window", "road", "mountain", "sea", "meadow", "wind"),
    "film": ("city", "road", "window", "night", "sea", "mountain", "meadow", "wind"),
    "noir": ("city", "window", "night", "road", "mountain", "sea", "meadow", "wind"),
    "pastel": ("meadow", "wind", "sea", "night", "city", "road", "window", "mountain"),
    "anime": ("meadow", "city", "night", "wind", "sea", "road", "window", "mountain"),
    "sunset": ("sea", "mountain", "road", "city", "meadow", "wind", "night", "window"),
}


def _pick_kind(words: list[str], index: int, fallback: tuple[str, ...] = _FALLBACK_KINDS) -> str:
    """关键词 → 构图；一个都没有就按序号在「这种风格的预设构图」里轮换。"""
    wanted = {_ELEMENT_KIND[word] for word in words if word in _ELEMENT_KIND}
    for kind in _KIND_ORDER:
        if kind in wanted:
            return kind
    return fallback[index % len(fallback)]


def _normalise(timeline) -> list[list]:
    """把 [(start, end, text)] 整理成按开始时间排序、区间不重叠的 [start, end, text]。"""
    rows: list[list] = []
    for item in timeline or []:
        try:
            start = float(item[0])
            end = float(item[1])
            text = str(item[2])
        except (TypeError, ValueError, IndexError):
            continue
        if not (start >= 0):
            start = 0.0
        if end <= start:
            end = start + 1.5
        rows.append([start, end, " ".join(text.split())])

    rows.sort(key=lambda row: row[0])
    # 区间重叠：前一句收在下一句开头（render 时一个时刻只属于一个镜头）
    for index in range(len(rows) - 1):
        if rows[index][1] > rows[index + 1][0]:
            rows[index][1] = max(rows[index][0] + 0.4, rows[index + 1][0])
    return rows


def _plan(timeline, style: str = "watercolor", prompt: str = "") -> list[dict]:
    """歌词时间轴 → 镜头计划（一条 = 一个镜头）。

    ``prompt`` 是用户的完整需求：它的关键词会并进每个镜头，保证「雨夜的车站」
    这种约束在每句歌词里都成立，而不是只有歌词里提到才有。
    """
    key = style_lib.resolve_style(str(style or ""))
    fallback = _STYLE_FALLBACK.get(key, _FALLBACK_KINDS)
    theme = scene_keywords(prompt)
    seed_bias = sum(ord(char) for char in str(prompt or "")) % 97
    scenes: list[dict] = []
    seen: dict[str, int] = {}
    for index, (start, _end, text) in enumerate(_normalise(timeline)):
        words = scene_keywords(text)
        merged = list(words)
        for word in theme:
            if word not in merged:
                merged.append(word)
        kind = _pick_kind(merged, index, fallback)
        repeat = seen.get(kind, 0)
        seen[kind] = repeat + 1
        moods = _MOOD.get(kind, ("hopeful", "happy"))
        scenes.append({
            "t": round(start, 3),
            "kind": kind,
            # 同类构图第二次出现时换一套配色（ALT）
            "v": repeat % 2,
            "cam": _CAMERA.get(kind, "drift"),
            "seed": 3 + index * 7 + seed_bias,
            "mood": list(moods),
            # 天气 / 天色叠加：下雨的站台、有星的窗、落花的田野都成立
            "wet": "雨" in merged,
            "flaky": "雪" in merged,
            "starry": (("星" in merged) or ("月亮" in merged)) and "雨" not in merged,
            "petals": ("花" in merged) and kind in ("meadow", "road", "wind"),
            "kw": merged,
            "txt": text,
        })
    return scenes


def _scenes_js(scenes: list[dict]) -> str:
    """把镜头计划渲染成 JS 数据表。"""
    rows = ["  // 镜头表：一句歌词一条，t = 开始秒（结束时间由 timeline.js 算到下一镜或 PROJECT.duration）",
            "  const SCENES = ["]
    for scene in scenes:
        rows.append(
            "    {{ t: {t:.3f}, kind: '{kind}', v: {v}, cam: '{cam}', seed: {seed}, "
            "mood: {mood}, wet: {wet}, flaky: {flaky}, starry: {starry}, petals: {petals}, "
            "kw: {kw}, txt: {txt} }},".format(
                t=scene["t"],
                kind=scene["kind"],
                v=scene["v"],
                cam=scene["cam"],
                seed=scene["seed"],
                mood=json.dumps(scene["mood"], ensure_ascii=False),
                wet="true" if scene["wet"] else "false",
                flaky="true" if scene["flaky"] else "false",
                starry="true" if scene["starry"] else "false",
                petals="true" if scene["petals"] else "false",
                kw=json.dumps(scene["kw"], ensure_ascii=False),
                txt=json.dumps(scene["txt"], ensure_ascii=False),
            )
        )
    rows.append("  ];")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# 场景 JS（拆成几段拼起来，方便读；__SCENES__ / __STYLE__ 是占位符）
# ---------------------------------------------------------------------------

_JS_OPEN = r"""// generated.js —— 由 backend/app/skills/work/animation/scene_gen.py 按歌词自动生成，请勿手改。
// 一句歌词一个镜头：关键词（雨/雪/海/车站/夜/灯/窗/花/路/风/山）决定天空、天气、主体、构图和转场，
// 抽不到关键词时按镜头序号轮换预设构图。每个镜头都是纯函数：画面只由 t / lt / dur 决定。
(() => {
  const STYLE = "__STYLE__";
  const PIXEL = !!(PROJECT.pixel || STYLE === 'pixel');

__SCENES__

  // ---------- 配色 ----------
  // 每类构图一套颜色槽：
  //   top / bot / seam     天空（上、下、接缝处晕染）
  //   hill / hill2         远景山形
  //   ground / ground2     地面
  //   accent / accent2     这一场的招牌色（海面、路面、窗光、楼影……）
  //   line / light / dark  线条、光、剪影
  const BASE = {
    rain: { top: '#6E7F96', bot: '#9EB0BE', seam: '#C3D1DB', hill: '#61737A', hill2: '#54676E', ground: '#79897F', ground2: '#4E5F5C', accent: '#B9C7CE', accent2: '#3C4A58', line: '#3C4A58', light: '#FFD98A', dark: '#33424E' },
    night: { top: '#1E2550', bot: '#2E3B76', seam: '#4A5AA0', hill: '#2A3260', hill2: '#20274E', ground: '#2A3157', ground2: '#1B2040', accent: '#FFD96A', accent2: '#7B5CA8', line: '#141833', light: '#FFE9A8', dark: '#141833' },
    station: { top: '#2B3050', bot: '#5E5470', seam: '#8A6E72', hill: '#3A3A54', hill2: '#2E3048', ground: '#6E6470', ground2: '#4A4352', accent: '#C9713F', accent2: '#3A3448', line: '#26222E', light: '#FFD98A', dark: '#221F2A' },
    sea: { top: '#8FB6CE', bot: '#E7D2B4', seam: '#FFF1CE', hill: '#4E7E93', hill2: '#3C6C82', ground: '#E3CFAA', ground2: '#C8B08A', accent: '#3A9C98', accent2: '#1F5F70', line: '#2B4450', light: '#FFD98A', dark: '#1F3A46' },
    meadow: { top: '#8EC3E6', bot: '#E8EFD2', seam: '#FFF4D8', hill: '#7FA45C', hill2: '#5F8A48', ground: '#9DBE68', ground2: '#6E9452', accent: '#E27A92', accent2: '#4E7A3E', line: '#2B3A2A', light: '#FFE9A8', dark: '#31432C' },
    wind: { top: '#A8D2DC', bot: '#EEF0DE', seam: '#FFF6DE', hill: '#87A87A', hill2: '#688D60', ground: '#A7BE84', ground2: '#7B9C66', accent: '#E8AA38', accent2: '#6E9F58', line: '#33403A', light: '#FFF0C2', dark: '#3A463C' },
    window: { top: '#1A1730', bot: '#241E3A', seam: '#3A3050', hill: '#241F3C', hill2: '#1B1730', ground: '#3A2C28', ground2: '#241A18', accent: '#F0BE46', accent2: '#6B4A34', line: '#120E1C', light: '#FFC766', dark: '#141020' },
    road: { top: '#6F87A8', bot: '#D9C3B0', seam: '#F6E2C8', hill: '#6A6C8A', hill2: '#565878', ground: '#5A5560', ground2: '#403C4A', accent: '#F2E3C4', accent2: '#3E3A48', line: '#2A2632', light: '#FFD98A', dark: '#221E2C' },
    city: { top: '#1C2038', bot: '#3A3A5C', seam: '#6A5A78', hill: '#23264A', hill2: '#1A1D3A', ground: '#1E2138', ground2: '#141626', accent: '#F0BE46', accent2: '#4A4470', line: '#101220', light: '#FFD98A', dark: '#12141F' },
    snow: { top: '#B9CFE0', bot: '#E6EEF2', seam: '#FFFFFF', hill: '#8FA8BC', hill2: '#6E8AA2', ground: '#E9F1F4', ground2: '#C9D8E0', accent: '#7FA8BE', accent2: '#54748C', line: '#2E4052', light: '#FFE9C0', dark: '#3A4C5C' },
    mountain: { top: '#D9A277', bot: '#F6DFC0', seam: '#FFEFD2', hill: '#9A83A0', hill2: '#7B6789', ground: '#6E5F72', ground2: '#4C4054', accent: '#F2C27A', accent2: '#8C6E7A', line: '#3A2E3C', light: '#FFE0A8', dark: '#332A38' }
  };

  // 同一类构图的第二句歌词换个色调，免得连着两句雨是一样的灰
  const ALT = {
    rain: { top: '#5A6B7E', bot: '#8FA0AC', seam: '#B8C6D0', light: '#FFC766' },
    night: { top: '#2A2038', bot: '#4A3A5C', seam: '#8C6E7A', accent: '#FFCF8A', accent2: '#9A6E8E' },
    station: { top: '#3A3352', bot: '#7E6A78', seam: '#B98A82' },
    sea: { top: '#C9A6B8', bot: '#F2D6BC', seam: '#FFE8D2', accent: '#5A7E9C', accent2: '#2E4A64' },
    meadow: { top: '#F0C9A8', bot: '#F7E7C4', seam: '#FFF6E0', ground: '#C8B06E', ground2: '#A08A56' },
    wind: { top: '#CBE2E8', bot: '#F4F2E2', seam: '#FFF9E6' },
    window: { top: '#241C2E', bot: '#2E2440', accent: '#FFD06A' },
    road: { top: '#3E4560', bot: '#8A7290', seam: '#C79AA2', light: '#FFC766' },
    city: { top: '#26203A', bot: '#4E3450', seam: '#9A6E7A', accent: '#FFD08A' },
    snow: { top: '#CFE0EA', bot: '#F2F6F8', hill: '#A8BFCC' },
    mountain: { top: '#C98A78', bot: '#F0D2B4', hill: '#8A6E8C' }
  };

  const pal = (kind, v) => {
    const base = BASE[kind] || BASE.meadow;
    return (v && ALT[kind]) ? { ...base, ...ALT[kind] } : base;
  };

  // ---------- 风格包 ----------
  // 只动调色（色相/饱和/亮度）、纸色、墨色和叠加层，不换画法；
  // pixel 走逻辑网格微调，其余风格靠这套调色拉开差别。
  const STYLE_PACK = __STYLE_PACK__;
  (() => {
    const pack = STYLE_PACK || {};
    if (pack.paper) PAL.paper = pack.paper;
    if (pack.ink) PAL.ink = pack.ink;
    const hue = Number(pack.hue) || 0;
    const sat = (pack.sat === undefined || pack.sat === null) ? 1 : Number(pack.sat);
    const light = (pack.light === undefined || pack.light === null) ? 1 : Number(pack.light);
    if (hue || sat !== 1 || light !== 1) {
      const toHsl = (hex) => {
        const v = String(hex).replace('#', '');
        const n = v.length === 3 ? v.split('').map(c => c + c).join('') : v;
        const r = parseInt(n.slice(0, 2), 16) / 255;
        const g = parseInt(n.slice(2, 4), 16) / 255;
        const b = parseInt(n.slice(4, 6), 16) / 255;
        const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
        const l = (mx + mn) / 2;
        const s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
        let h = 0;
        if (d !== 0) {
          if (mx === r) h = ((g - b) / d) % 6;
          else if (mx === g) h = (b - r) / d + 2;
          else h = (r - g) / d + 4;
          h *= 60;
          if (h < 0) h += 360;
        }
        return [h, s, l];
      };
      const toHex = (h, s, l) => {
        h = ((h % 360) + 360) % 360;
        s = Math.max(0, Math.min(1, s));
        l = Math.max(0, Math.min(1, l));
        const c = (1 - Math.abs(2 * l - 1)) * s;
        const x = c * (1 - Math.abs((h / 60) % 2 - 1));
        const m = l - c / 2;
        let rgb;
        if (h < 60) rgb = [c, x, 0];
        else if (h < 120) rgb = [x, c, 0];
        else if (h < 180) rgb = [0, c, x];
        else if (h < 240) rgb = [0, x, c];
        else if (h < 300) rgb = [x, 0, c];
        else rgb = [c, 0, x];
        return '#' + rgb.map(v => Math.round((v + m) * 255).toString(16).padStart(2, '0')).join('');
      };
      const shift = (hex) => {
        if (!/^#[0-9a-f]{3,6}$/i.test(String(hex))) return hex;
        const hsl = toHsl(hex);
        return toHex(hsl[0] + hue, hsl[1] * sat, hsl[2] * light);
      };
      for (const k in BASE) for (const f in BASE[k]) BASE[k][f] = shift(BASE[k][f]);
      for (const k in ALT) for (const f in ALT[k]) ALT[k][f] = shift(ALT[k][f]);
    }
    const overlay = String(pack.overlay || 'none');
    if (overlay === 'scanlines' || overlay === 'vignette') {
      const apply = (c, t) => {
        c.save();
        if (overlay === 'scanlines') {
          c.globalAlpha = 0.16;
          c.fillStyle = '#000';
          for (let y = 0; y < H; y += 4) c.fillRect(0, y, W, 2);
          c.globalAlpha = 0.05;
          c.fillStyle = '#9BE8FF';
          const band = ((t * 120) % (H + 260)) - 130;
          c.fillRect(0, band, W, 90);
        } else {
          const g = c.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.34,
                                           W / 2, H / 2, Math.max(W, H) * 0.74);
          g.addColorStop(0, 'rgba(0,0,0,0)');
          g.addColorStop(1, 'rgba(0,0,0,0.45)');
          c.fillStyle = g;
          c.fillRect(0, 0, W, H);
        }
        c.restore();
      };
      // karaoke.js 也会写 window.overlayHook（歌词层），所以用访问器串成链：
      // 先画风格层，再让后来的歌词层画在上面。
      let hook = window.overlayHook;
      Object.defineProperty(window, 'overlayHook', {
        configurable: true,
        get() { return hook; },
        set(fn) {
          const prev = hook;
          hook = (c, t) => {
            if (prev) prev(c, t);
            if (typeof fn === 'function') fn(c, t);
          };
        },
      });
      window.overlayHook = apply;
    }
  })();

  // ---------- 通用画法 ----------
  // 一块实色（wash 是不透明白水彩色，ink: null 表示不描边，大块背景必须不描边）
  const slab = (x, y, w, h, o) => paint(rectPts(x, y, w, h, 2), o);

  // 天空：从顶到地平线铺 4 条渐变色带（顶色 → 接缝色 → 地面色，到地平线正好等于地面色，不留台阶）
  // 地平线以下铺地面色，接缝处再加一层很淡的雾
  function skyBands(top, bot, seam, seamY) {
    const n = 5;
    for (let i = 0; i < n; i++) {
      const u = i / n;
      const k = (u + (i + 1) / n) * 0.5;
      slab(-900, -240 + (seamY + 240) * u, W + 1800, (seamY + 240) / n + 2, { wash: mixCol(top, bot, k), ink: null });
    }
    slab(-900, seamY, W + 1800, H + 300, { wash: bot, ink: null });
    paint(ellPts(W / 2, seamY, W * 0.92, H * 0.055, 28, 16), { fill: mixCol(seam, bot, 0.35), fillOp: 55, bleed: 0.5, tex: 0.7, ink: null });
  }

  // 星空：位置全部用 hash(i) 固定，闪烁用时间函数，逐元素 boilSeed 保证抖动种子稳定
  function stars(t, n, yMax, seed) {
    for (let i = 0; i < n; i++) {
      boilSeed('star' + seed + '_' + i);
      const x = hash(i * 3.1 + seed) * (W + 240) - 120;
      const y = hash(i * 7.7 + seed + 1) * yMax;
      const tw = 0.5 + 0.5 * Math.sin(t * (1.6 + 2.2 * hash(i + seed)) + i);
      paint(starPts(x, y, (4 + 6 * hash(i + 2.5 + seed)) * (0.6 + 0.6 * tw), 0.34, 4), { wash: PAL.cream, washOp: 70 + 165 * tw, ink: null });
      if (tw > 0.92) glow(x, y, 46 + 30 * tw, '#FFE9A8', 0.3);
    }
  }

  function moonAt(x, y, r, col) {
    boilSeed('moon');
    glow(x, y, r * 4, '#FFE9B0', 0.32);
    paint(ellPts(x, y, r, r, 26, 1.4), { wash: col, fill: PAL.ochre, fillOp: 45, tex: 0.5, ink: PAL.ink, sw: 0.7 });
    paint(ellPts(x - r * 0.3, y - r * 0.26, r * 0.2, r * 0.15, 10), { fill: mixCol(col, PAL.ochre, 0.55), fillOp: 130, ink: null });
    paint(ellPts(x + r * 0.22, y + r * 0.3, r * 0.13, r * 0.1, 8), { fill: mixCol(col, PAL.ochre, 0.45), fillOp: 110, ink: null });
  }

  function sunAt(x, y, r, col) {
    boilSeed('sun');
    glow(x, y, r * 4.4, col, 0.35);
    paint(ellPts(x, y, r, r * 0.96, 24, 2), { wash: col, fill: PAL.cream, fillOp: 60, ink: null });
  }

  function clouds(t, y, col, n, sc, seed) {
    for (let i = 0; i < n; i++) {
      boilSeed('cloud' + seed + i);
      const x = ((hash(i * 2.7 + seed) * (W + 800) + t * (7 + 9 * hash(i + seed))) % (W + 800)) - 400;
      const yy = y + (hash(i + 9 + seed) - 0.5) * 150;
      paint(ellPts(x, yy, 210 * sc * (0.7 + 0.6 * hash(i + 3 + seed)), 54 * sc, 22, 8), { fill: col, fillOp: 85, bleed: 0.3, tex: 0.5, ink: null });
      paint(ellPts(x + 90 * sc, yy + 18, 130 * sc, 34 * sc, 18, 6), { fill: col, fillOp: 70, bleed: 0.3, tex: 0.5, ink: null });
    }
  }

  // 远山：一串压扁的椭圆，比多边形好画也好晕染
  function hills(cols, baseY, seed) {
    for (let k = 0; k < cols.length; k++) {
      boilSeed('hill' + seed + k);
      const cx = ((k * 820 + seed * 260) % (W + 1000)) - 360;
      const rx = 640 + 240 * hash(k + seed);
      const ry = 170 + 80 * hash(k * 2 + seed);
      paint(ellPts(cx, baseY + ry * 0.55, rx, ry, 32, 3), { wash: cols[k % cols.length], fill: mixCol(cols[k % cols.length], PAL.night, 0.28), fillOp: 80, bleed: 0.1, tex: 0.6, ink: null });
    }
  }

  // 地面：整幅一条 + 一条地平线
  function groundBand(y, col, col2) {
    paint(rectPts(-900, y, W + 1800, H + 300, 3), { wash: col, fill: col2, fillOp: 80, bleed: 0.06, tex: 0.6, ink: null });
    inkLine([[-500, y + 2], [W / 2, y - 4], [W + 500, y + 3]], 0.9, mixCol(col, PAL.ink, 0.5), 'inkfine', 0.5);
  }

  function grass(t, y0, n, col, seed) {
    for (let i = 0; i < n; i++) {
      boilSeed('grass' + seed + i);
      const x = hash(i * 1.9 + seed) * (W + 400) - 200;
      const y = y0 + (hash(i + 5 + seed) - 0.5) * 40;
      const bend = wob(t, 0.45, hash(i + seed) * 3) * 8;
      inkLine([[x, y], [x + bend * 0.55, y - 20 - 14 * hash(i + 9 + seed)], [x + bend, y - 38 - 12 * hash(i + 2 + seed)]], 0.7, col, 'inkfine', 0.4);
    }
  }

  function flowers(t, y0, n, colA, colB, seed) {
    for (let i = 0; i < n; i++) {
      boilSeed('flw' + seed + i);
      const x = hash(i * 2.3 + seed) * (W + 300) - 150;
      const y = y0 + hash(i + 7 + seed) * 150;
      const bend = wob(t, 0.5, hash(i + seed) * 2) * 10;
      inkLine([[x, y + 62], [x + bend * 0.6, y + 30], [x + bend, y]], 0.9, mixCol(colA, PAL.ink, 0.45), 'inkfine', 0.4);
      const r = 9 + 9 * hash(i + 3 + seed);
      paint(ellPts(x + bend, y, r, r * 0.7, 10, 0, hash(i) * 3), { wash: i % 3 ? colA : colB, washOp: 230, ink: null });
      paint(ellPts(x + bend, y, r * 0.3, r * 0.3, 8), { wash: PAL.ochre, ink: null });
    }
  }

  function rainLines(t, k, col, seed) {
    const n = Math.round(12 + 20 * k);
    for (let i = 0; i < n; i++) {
      boilSeed('rain' + seed + i);
      const x = (hash(i * 1.7 + seed) * (W + 400) + t * (150 + 170 * hash(i + seed))) % (W + 400) - 200;
      const y = (hash(i * 9.3 + seed) * H + t * (540 + 420 * hash(i + 1 + seed))) % (H + 200) - 120;
      inkLine([[x, y], [x - 24, y + 92 + 70 * hash(i + 3 + seed)]], 0.5 + 1.1 * hash(i + 5 + seed), col, 'inkfine', 0);
    }
  }

  function snowFlakes(t, k, col, seed) {
    const n = Math.round(36 + 46 * k);
    for (let i = 0; i < n; i++) {
      boilSeed('snow' + seed + i);
      const x = (hash(i * 2.9 + seed) * (W + 300) + Math.sin(t * 0.9 + i) * 46 + t * (18 + 26 * hash(i + seed))) % (W + 300) - 150;
      const y = (hash(i * 6.1 + seed) * H + t * (60 + 80 * hash(i + 2 + seed))) % (H + 160) - 80;
      const r = 4 + 6 * hash(i + 4 + seed);
      paint(ellPts(x, y, r, r, 8), { wash: col, washOp: 140 + 100 * hash(i + 6 + seed), ink: null });
    }
  }

  function petals(t, n, colA, colB, seed) {
    for (let i = 0; i < n; i++) {
      boilSeed('petal' + seed + i);
      const x = (hash(i * 3.7 + seed) * (W + 300) + Math.sin(t * 1.3 + i) * 60 + t * (26 + 34 * hash(i + seed))) % (W + 300) - 150;
      const y = (hash(i * 8.9 + seed) * H + t * (64 + 84 * hash(i + 1 + seed))) % (H + 160) - 80;
      const r = 7 + 8 * hash(i + 2 + seed);
      paint(ellPts(x, y, r, r * 0.5, 10, 0, t * 1.4 + i), { wash: i % 3 ? colA : colB, washOp: 210, ink: null });
    }
  }

  // 伞：半圆伞面 + 伞骨 + 一小截伞柄，自带倾斜
  function umbrella(x, y, r, col, dk, tilt) {
    boilSeed('umbrella');
    push();
    translate(x, y);
    rotate(tilt || 0);
    const pts = [];
    for (let i = 0; i <= 14; i++) {
      const a = Math.PI + i / 14 * Math.PI;
      pts.push([Math.cos(a) * r, Math.sin(a) * r * 0.78]);
    }
    pts.push([r * 0.92, 6], [-r * 0.92, 6]);
    paint(pts, { wash: col, fill: dk, fillOp: 55, tex: 0.5, ink: PAL.ink, sw: 1 });
    for (const dx of [-0.62, -0.2, 0.2, 0.62]) inkLine([[0, -r * 0.78], [dx * r, 4]], 0.5, PAL.ink, 'inkfine', 0.3);
    inkLine([[0, 4], [r * 0.14, r * 0.8]], 3, dk, 'ink', 0.2);
    pop();
  }

  function lampPost(x, y, sc, glowCol) {
    boilSeed('lamp' + Math.round(x));
    inkLine([[x, y], [x, y - 300 * sc]], 3.2 * sc, PAL.ink, 'ink', 0.2);
    inkLine([[x, y - 300 * sc], [x + 90 * sc, y - 320 * sc]], 2.6 * sc, PAL.ink, 'ink', 0.3);
    glow(x + 96 * sc, y - 300 * sc, 190 * sc, glowCol, 0.55);
    paint(ellPts(x + 96 * sc, y - 306 * sc, 26 * sc, 22 * sc, 14), { wash: glowCol, ink: PAL.ink, sw: 0.8 });
  }

  // 风线：一条条横向的长弧，按 t 往右扫
  function windLines(t, y0, y1, col, seed) {
    for (let i = 0; i < 4; i++) {
      boilSeed('wind' + seed + i);
      const y = lerp(y0, y1, hash(i + 1 + seed));
      const x0 = -300 + frac(t * 0.3 + hash(i + 4 + seed)) * (W + 600);
      inkLine(through([[x0, y], [x0 + 250, y - 46 - 30 * hash(i + seed)], [x0 + 520, y + 18], [x0 + 780, y - 12 - 24 * hash(i + 2 + seed)]], 5), 1.4, col, 'ink', 0.5);
    }
  }

  // 锯齿山脊
  function ridge(x, baseY, w, h, col, seed) {
    boilSeed('ridge' + seed);
    const pts = [[x - w / 2, baseY]];
    for (let i = 0; i <= 6; i++) {
      const u = i / 6;
      pts.push([x - w / 2 + u * w, baseY - h * (0.35 + 0.65 * Math.abs(Math.sin(i * 1.7 + seed)))]);
    }
    pts.push([x + w / 2, baseY]);
    paint(pts, { wash: col, fill: mixCol(col, PAL.night, 0.3), fillOp: 80, bleed: 0.08, tex: 0.6, ink: null });
  }

  // 飞叶 / 飞鸟 / 脚印这类小东西
  function leaves(t, n, col, seed) {
    for (let i = 0; i < n; i++) {
      boilSeed('leaf' + seed + i);
      const x = (hash(i * 2.1 + seed) * (W + 400) + t * (200 + 160 * hash(i + seed))) % (W + 400) - 200;
      const y = H * 0.30 + hash(i + 6 + seed) * H * 0.46 + Math.sin(t * 2 + i) * 26;
      const r = 9 + 8 * hash(i + 3 + seed);
      paint(ellPts(x, y, r, r * 0.42, 10, 0, t * 2 + i), { wash: i % 2 ? col : mixCol(col, PAL.ochre, 0.4), washOp: 225, ink: null });
    }
  }

  function birds(t, n, y0, col, seed) {
    for (let i = 0; i < n; i++) {
      boilSeed('bird' + seed + i);
      const x = 200 + hash(i + seed) * (W - 200) + t * (16 + 12 * hash(i + 2));
      const y = y0 + hash(i + 4 + seed) * 140 + Math.sin(t * 1.6 + i) * 12;
      const f = 8 + 4 * Math.sin(t * 6 + i);
      inkLine([[x - f, y], [x, y - f * 0.7], [x + f, y]], 0.9, col, 'inkfine', 0.4);
    }
  }

  function footprints(t, y, col, seed) {
    for (let i = 0; i < 9; i++) {
      boilSeed('fp' + i);
      const x = 240 + i * 190 + (i % 2 ? 26 : 0);
      paint(ellPts(x, y + 40 + i * 6, 22, 9, 10, 0, 0.2), { wash: col, washOp: 120, ink: null });
    }
  }
"""

_JS_STAGE = r"""
  // ---------- 水彩场景：每个构图一个画法 ----------
  // 约定：签名统一 (t, lt, dur, S, P, C)
  //   t   全片时间（做闪烁、风、boil 这类连续运动）
  //   lt  本镜头局部时间（做"这一镜里发生的事"，别用 t 做局部时序）
  //   dur 本镜头时长
  //   S   镜头数据（kind/v/cam/seed/mood/wet/flaky/starry/petals）
  //   P   配色
  //   C   相机状态（cx/cy/z），主角要放进画面就用 C.cx

  // 1) 雨：灰蓝天，撑伞的人站在积水边
  function scRain(t, lt, dur, S, P, C) {
    const umb = mixCol(PAL.clay, P.top, 0.45);
    skyBands(P.top, P.bot, P.seam, H * 0.52);
    clouds(t, H * 0.18, mixCol(P.top, PAL.cream, 0.35), 2, 1, S.seed);
    hills([P.hill, P.hill2], H * 0.60, S.seed);
    groundBand(H * 0.64, P.ground, P.ground2);

    // 积水 + 落雨砸出来的涟漪
    for (let i = 0; i < 4; i++) {
      boilSeed('puddle' + i);
      const x = 200 + i * 430 + hash(i + S.seed) * 110;
      const y = H * 0.78 + hash(i + 3 + S.seed) * 130;
      paint(ellPts(x, y, 180 + 80 * hash(i + 1 + S.seed), 32, 20, 3), { wash: P.accent, washOp: 120, ink: null });
      const k = frac(t * 0.55 + i * 0.31);
      if (k < 0.92) inkLine(ellPts(x, y, 44 + 140 * k, 12 + 15 * k, 18), 0.5, P.line, 'inkfine', 0.4);
    }

    lampPost(1600, H * 0.64, 1.05, P.light);
    rainLines(t, 0.85, P.line, S.seed);

    // 主角：撑着伞，身体跟着呼吸起伏
    const u = 24;
    const gx = C.cx + 40;
    const gy = H * 0.82 + 2 * Math.sin(t * 1.2);
    clawd(gx, gy, u, {
      ...emotions(lt, [[0, S.mood[0]], [dur * 0.55, S.mood[1]]]),
      view: 'q', aR: 1.2, aL: -0.5, dy: 0.06 * Math.sin(t * 1.2), boilKey: 'lead' + S.seed
    });
    umbrella(gx + 80, gy - 10.6 * u, 150, umb, mixCol(umb, PAL.ink, 0.45), 0.12);

    // 前景再撒一层雨，人就在雨里了
    rainLines(t + 0.4, 0.35, mixCol(P.line, P.bot, 0.45), S.seed + 31);
  }

  // 2) 夜：满天星，抬头的人
  function scNight(t, lt, dur, S, P, C) {
    slab(-900, -500, W + 1800, H + 1000, { wash: P.top, ink: null });
    paint(ellPts(W / 2, H, W * 0.85, H * 0.46, 30, 12), { fill: P.bot, fillOp: 130, bleed: 0.3, tex: 0.6, ink: null });
    stars(t, 44, H * 0.60, S.seed);
    moonAt(1500, 230, 86, P.accent);
    hills([P.hill, P.hill2], H * 0.78, S.seed);
    groundBand(H * 0.86, P.ground, P.ground2);

    // 流星：每 5 秒一颗，带一条拖尾
    const phase = frac(t / 5 + hash(S.seed));
    if (phase < 0.16) {
      boilSeed('shoot');
      const k = phase / 0.16;
      const x = 420 + 900 * hash(S.seed + 3);
      const y = 60 + 260 * k;
      const tail = [[x - 300, y - 176 + 22], [x - 150, y - 92 + 10], [x, y]];
      paint(ribbon(tail, 2, 18), { wash: PAL.cream, washOp: 160 * (1 - k), ink: null });
      paint(starPts(x, y, 16 * (1 - 0.4 * k), 0.34, 4), { wash: PAL.cream, ink: null });
    }

    // 主角：仰头看星，脚边一点草
    const u = 23;
    const gx = C.cx - 210 + 20 * Math.sin(t * 0.4);
    const gy = H * 0.87;
    clawd(gx, gy, u, {
      ...emotions(lt, [[0, S.mood[0]], [dur * 0.5, S.mood[1]]]),
      view: 'front', lookY: -0.55, boilKey: 'lead' + S.seed
    });
    grass(t, gy + 6, 12, mixCol(P.ground, PAL.cream, 0.3), S.seed);
  }

  // 3) 车站：黄昏站台，顶棚、柱子、铁轨，末班车进站时整片暖光扫过
  function scStation(t, lt, dur, S, P, C) {
    skyBands(P.top, P.bot, P.seam, H * 0.56);
    stars(t, 16, H * 0.30, S.seed + 5);
    groundBand(H * 0.63, P.ground, P.ground2);

    // 铁轨（画面下方，往右收）
    for (const dy of [0, 26]) inkLine([[120, H * 0.70 + dy], [1500, H * 0.775 + dy]], 2.4, P.line, 'ink', 0.2);
    for (let i = 0; i < 10; i++) {
      const k = i / 9;
      inkLine([[lerp(140, 1490, k), H * 0.705 + lerp(0, 56, k)], [lerp(120, 1470, k), H * 0.745 + lerp(0, 56, k)]], 1.6, mixCol(P.line, P.ground2, 0.4), 'inkfine', 0);
    }

    // 站台顶棚
    slab(-900, H * 0.16, W + 1800, 96, { wash: P.accent, fill: mixCol(P.accent, PAL.ink, 0.35), fillOp: 90, tex: 0.5, ink: PAL.ink, sw: 1.2 });

    // 柱子 + 顶灯
    for (let i = 0; i < 4; i++) {
      const x = 150 + i * 560;
      slab(x - 14, H * 0.21, 28, H * 0.45, { wash: P.accent2, fill: mixCol(P.accent2, PAL.ink, 0.3), fillOp: 70, ink: PAL.ink, sw: 0.8 });
      glow(x, H * 0.27, 130, P.light, 0.5);
      paint(ellPts(x, H * 0.26, 22, 18, 12), { wash: P.light, ink: PAL.ink, sw: 0.6 });
    }

    // 长椅剪影
    slab(1180, H * 0.55, 330, 24, { wash: P.accent2, ink: PAL.ink, sw: 0.8 });
    slab(1180, H * 0.62, 330, 24, { wash: P.accent2, ink: PAL.ink, sw: 0.8 });
    for (const x of [1200, 1470]) inkLine([[x, H * 0.64], [x, H * 0.68]], 3, P.line, 'ink', 0.2);

    // 进站：镜头中段有一束车灯从右往左扫过（seam 之一，event + 反应）
    const enter = seg(lt, dur * 0.42, dur * 0.42 + 0.35) * (1 - seg(lt, dur * 0.62, dur * 0.95));
    if (enter > 0.02) {
      const x = lerp(1900, -320, ease(enter));
      const a = Math.sin(Math.PI * enter);
      glow(x, H * 0.58, 460, P.light, 0.6 * a);
      paint(ellPts(x, H * 0.60, 230, 54, 20), { wash: P.light, washOp: 230 * a, ink: null });
      for (let i = 0; i < 3; i++) inkLine([[x + 240 + i * 120, H * 0.34], [x + 300 + i * 120, H * 0.70]], 2, P.light, 'dry', 0.3);
    }

    // 主角：等车，侧身，脚尖跟着心跳点
    const u = 23;
    const gx = 700;
    const gy = H * 0.688 + Math.abs(Math.sin(Math.PI * bpOf(t))) * 4;
    clawd(gx, gy, u, {
      ...emotions(lt, [[0, S.mood[0]], [dur * 0.5, S.mood[1]]]),
      view: 'side', flip: true, aL: -0.2, aR: -0.2, boilKey: 'lead' + S.seed
    });
  }

  // 4) 海：海平线、一层层浪、太阳的倒影，岸上站着人
  function scSea(t, lt, dur, S, P, C) {
    skyBands(P.top, P.bot, P.seam, H * 0.42);
    clouds(t, H * 0.16, mixCol(P.top, PAL.cream, 0.5), 2, 1, S.seed);
    sunAt(1500, H * 0.40, 78, P.light);

    // 海面
    slab(-900, H * 0.44, W + 1800, H + 300, { wash: P.accent, fill: P.accent2, fillOp: 90, bleed: 0.1, tex: 0.6, ink: null });
    inkLine([[-500, H * 0.445], [W / 2, H * 0.438], [W + 500, H * 0.446]], 2, mixCol(P.accent, PAL.cream, 0.5), 'ink', 0.4);

    // 一层层往前推的浪
    for (let r = 0; r < 7; r++) {
      const y = H * 0.48 + r * 66;
      const k = r / 6;
      const pts = [];
      for (let i = 0; i <= 8; i++) pts.push([-320 + i * (W + 640) / 8, y + Math.sin(t * 1.5 + i * 0.8 + r * 0.6) * (9 + 16 * k)]);
      inkLine(pts, 0.6 + 1.6 * k, mixCol(P.accent, PAL.cream, 0.35 + 0.3 * k), 'ink', 0.5);
      if (r % 2 === 1) {
        for (let i = 0; i < 4; i++) {
          boilSeed('foam' + r + i);
          const x = hash(i + r * 3 + S.seed) * W;
          paint(ellPts(x, y + 10, 40 + 60 * hash(i + r + S.seed), 7, 10), { wash: PAL.cream, washOp: 120, ink: null });
        }
      }
    }

    // 太阳在水上的反光
    for (let i = 0; i < 12; i++) {
      boilSeed('refl' + i);
      paint(ellPts(1500 + Math.sin(t * 2 + i) * 40, H * 0.47 + i * 34, 90 - i * 5, 6, 10), { wash: P.light, washOp: 130 - i * 8, ink: null });
    }

    // 岸
    groundBand(H * 0.80, P.ground, P.ground2);
    const u = 22;
    const gx = C.cx - 260;
    const gy = H * 0.86;
    clawd(gx, gy, u, {
      ...emotions(lt, [[0, S.mood[0]], [dur * 0.55, S.mood[1]]]),
      view: 'side', aL: -0.6 + 0.12 * Math.sin(t * 1.1), aR: -0.3, dy: 0.05 * Math.sin(t * 0.9), boilKey: 'lead' + S.seed
    });
    grass(t, H * 0.90, 10, mixCol(P.ground2, PAL.ink, 0.3), S.seed);
  }

  // 5) 花田：花、草、飘的花瓣，主角一边走一边看花
  function scMeadow(t, lt, dur, S, P, C) {
    skyBands(P.top, P.bot, P.seam, H * 0.50);
    clouds(t, H * 0.18, PAL.cream, 3, 1, S.seed);
    sunAt(1620, 190, 66, P.light);
    hills([P.hill, P.hill2], H * 0.64, S.seed);
    groundBand(H * 0.72, P.ground, P.ground2);
    grass(t, H * 0.74, 34, P.accent2, S.seed);
    flowers(t, H * 0.72, 20, P.accent, PAL.cream, S.seed);
    petals(t, 18, P.accent, PAL.cream, S.seed + 2);

    // 走路的姿态交给 stroll（它会给 x / walk / view / flip / dy）
    const u = 22;
    const walk = stroll(lt, 0, dur, C.cx - 560, C.cx + 520, u);
    clawd(walk.x, H * 0.84, u, {
      ...emotions(lt, [[0, S.mood[0]], [dur * 0.55, S.mood[1]]]),
      view: walk.view, walk: walk.walk, flip: walk.flip, dy: walk.dy, boilKey: 'lead' + S.seed
    });
  }

  // 6) 风：草全倒向一边，云跑得飞快，人张开手迎着风
  function scWind(t, lt, dur, S, P, C) {
    skyBands(P.top, P.bot, P.seam, H * 0.48);
    clouds(t, H * 0.16, PAL.cream, 4, 1.2, S.seed);
    hills([P.hill, P.hill2], H * 0.66, S.seed);
    groundBand(H * 0.74, P.ground, P.ground2);

    // 被吹弯的草
    for (let i = 0; i < 40; i++) {
      boilSeed('wg' + i);
      const x = hash(i * 1.7 + S.seed) * (W + 300) - 150;
      const y = H * 0.80 + hash(i + 4 + S.seed) * 160;
      const bend = 26 + 16 * Math.sin(t * 2 + i * 0.5);
      inkLine([[x, y], [x + bend * 0.5, y - 26], [x + bend, y - 46]], 0.8, P.accent2, 'inkfine', 0.4);
    }

    windLines(t, H * 0.34, H * 0.68, mixCol(P.line, PAL.paper, 0.72), S.seed);
    leaves(t, 14, PAL.ochre, S.seed + 4);

    // 主角：张手迎风，重心被风推得晃
    const u = 23;
    const gx = C.cx + 40;
    const gy = H * 0.84;
    clawd(gx, gy, u, {
      ...emotions(lt, [[0, S.mood[0]], [dur * 0.5, S.mood[1]]]),
      view: 'front', aL: 0.95, aR: 0.55, dx: 0.22 * Math.sin(t * 1.6), dy: 0.05 * Math.sin(t * 1.2), boilKey: 'lead' + S.seed
    });
  }

  // 7) 窗：屋里很暗，一扇窗亮着，玻璃上有雨痕，人背对镜头看着窗外
  function scWindow(t, lt, dur, S, P, C) {
    slab(-900, -500, W + 1800, H + 1000, { wash: P.dark, ink: null });
    slab(-900, H * 0.70, W + 1800, H + 300, { wash: P.ground, fill: P.ground2, fillOp: 90, tex: 0.5, ink: null });

    // 窗外的夜空
    const wx = 380;
    const wy = 150;
    const ww = 780;
    const wh = 560;
    slab(wx - 40, wy - 40, ww + 80, wh + 80, { wash: P.top, ink: null });
    stars(lt + 0, 16, wy + wh * 0.5, S.seed);
    moonAt(wx + ww * 0.76, wy + wh * 0.24, 42, P.accent);
    groundBand(wy + wh - 60, P.hill, P.hill2);

    // 玻璃上的雨痕（窗里的雨就不该再撒到人脸上了）
    if (S.wet) {
      for (let i = 0; i < 26; i++) {
        boilSeed('glass' + i);
        const gx = wx + hash(i * 2.7 + S.seed) * ww;
        const gy = wy + (hash(i * 5.3 + S.seed) * wh * 0.6 + frac(t * 0.4 + hash(i)) * wh * 0.5);
        inkLine([[gx, gy], [gx + 2, gy + 26 + 30 * hash(i + 3)]], 0.8, mixCol(P.bot, PAL.cream, 0.5), 'inkfine', 0.2);
      }
    }

    // 窗框
    paint(rrPts(wx - 40, wy - 40, ww + 80, wh + 80, 14), { ink: P.accent2, sw: 4 });
    slab(wx + ww / 2 - 12, wy - 40, 24, wh + 80, { wash: P.accent2, ink: null });
    slab(wx - 40, wy + wh / 2 - 12, ww + 80, 24, { wash: P.accent2, ink: null });
    // 窗台
    slab(wx - 70, wy + wh + 34, ww + 140, 26, { wash: mixCol(P.accent2, PAL.cream, 0.25), ink: PAL.ink, sw: 0.8 });

    // 桌上的台灯（唯一的暖光）
    slab(1300, H * 0.60, 220, 22, { wash: P.accent2, ink: PAL.ink, sw: 0.8 });
    for (const x of [1320, 1490]) inkLine([[x, H * 0.615], [x, H * 0.70]], 5, P.dark, 'ink', 0.2);
    glow(1410, H * 0.52, 260, P.light, 0.6);
    paint(rrPts(1370, H * 0.40, 84, 96, 18), { wash: P.accent, fill: mixCol(P.accent, PAL.cream, 0.4), fillOp: 70, ink: PAL.ink, sw: 1 });
    inkLine([[1412, H * 0.505], [1412, H * 0.60]], 4, P.dark, 'ink', 0.2);

    // 主角：背对镜头站在窗前（剪影层次靠窗光打）
    const u = 26;
    const gx = 1660;
    const gy = H * 0.855;
    clawd(gx, gy, u, {
      ...emotions(lt, [[0, S.mood[0]], [dur * 0.55, S.mood[1]]]),
      view: 'back', dy: 0.05 * Math.sin(t * 1.1), boilKey: 'lead' + S.seed
    });
  }

  // 8) 路：横向平移的街道（人行道 / 路缘石 / 车道线 / 路灯），人一直在画面里走，
  // 远山跟着相机走一半位移 = 视差
  function scRoad(t, lt, dur, S, P, C) {
    skyBands(P.top, P.bot, P.seam, H * 0.50);
    clouds(t, H * 0.16, mixCol(P.top, PAL.cream, 0.5), 2, 1, S.seed);

    push();
    translate((C.cx - 960) * 0.55, 0);
    hills([P.hill, P.hill2], H * 0.62, S.seed);
    pop();

    // 人行道
    slab(-1600, H * 0.64, W + 3200, H * 0.10, { wash: mixCol(P.ground, PAL.cream, 0.5), tex: 0.5, ink: null });
    inkLine([[-1600, H * 0.635], [W / 2, H * 0.628], [W + 1600, H * 0.636]], 2.4, P.line, 'ink', 0.4);
    // 路缘石
    slab(-1600, H * 0.735, W + 3200, 9, { wash: mixCol(P.line, PAL.ink, 0.25), ink: null });
    // 路面
    slab(-1600, H * 0.745, W + 3200, H, { wash: P.ground2, fill: P.ground, fillOp: 55, tex: 0.6, ink: null });

    // 车道线：只画靠近脚边的这一条，横着一截一截
    for (let i = 0; i < 16; i++) {
      const x = -700 + i * 250;
      paint(rectPts(x, H * 0.905, 150, 13, 1), { wash: P.accent, washOp: 235, ink: null });
    }

    for (let i = 0; i < 6; i++) lampPost(-260 + i * 430, H * 0.64, 0.8, P.light);

    // 主角：一直在画面中央走（相机在动，他不动）
    const u = 22;
    const gx = C.cx + 60;
    const step = frac(t * 1.2);
    clawd(gx, H * 0.86, u, {
      ...emotions(lt, [[0, S.mood[0]], [dur * 0.55, S.mood[1]]]),
      view: 'side', walk: bpOf(t) * 0.5, dy: 0.06 * Math.abs(Math.sin(Math.PI * step)),
      aL: -0.75 + 0.3 * Math.sin(Math.PI * step), aR: -0.75 - 0.3 * Math.sin(Math.PI * step), boilKey: 'lead' + S.seed
    });
  }

  // 9) 雪：冷色的雪原、覆雪山、一串脚印，人缩着脖子
  function scSnow(t, lt, dur, S, P, C) {
    skyBands(P.top, P.bot, P.seam, H * 0.46);
    sunAt(1560, 210, 58, P.light);
    ridge(300, H * 0.66, 900, 300, P.hill2, 1);
    ridge(1200, H * 0.66, 1100, 380, P.hill, 2);
    groundBand(H * 0.70, P.ground, P.ground2);
    footprints(t, H * 0.74, mixCol(P.ground2, PAL.ink, 0.35), S.seed);

    // 两棵光秃的树
    for (const [x, sc] of [[340, 1], [1660, 0.8]]) {
      boilSeed('tree' + x);
      inkLine([[x, H * 0.72], [x + 6, H * 0.72 - 300 * sc]], 5 * sc, mixCol(P.accent2, PAL.ink, 0.4), 'ink', 0.3);
      for (let i = 0; i < 5; i++) {
        const y = H * 0.72 - 90 * sc - i * 42 * sc;
        inkLine([[x + 4, y], [x + (i % 2 ? -80 : 80) * sc, y - 46 * sc]], 3 * sc, mixCol(P.accent2, PAL.ink, 0.4), 'ink', 0.3);
      }
      paint(ellPts(x, H * 0.72 - 150 * sc, 120 * sc, 90 * sc, 20, 8), { fill: mixCol(P.accent, PAL.cream, 0.5), fillOp: 120, ink: null });
    }

    snowFlakes(t, 0.9, mixCol(P.bot, '#FFFFFF', 0.6), S.seed);

    // 主角：缩着肩，呼出的气用两团淡色表示
    const u = 23;
    const gx = C.cx + 120;
    const gy = H * 0.85 + 2 * Math.sin(t * 1.1);
    clawd(gx, gy, u, {
      ...emotions(lt, [[0, S.mood[0]], [dur * 0.55, S.mood[1]]]),
      view: 'q', aL: 0.15, aR: 0.15, dy: 0.04 * Math.sin(t * 1.1), boilKey: 'lead' + S.seed
    });
    for (let i = 0; i < 3; i++) {
      boilSeed('breath' + i);
      const k = frac(t * 0.5 + i * 0.33);
      paint(ellPts(gx + 150 + k * 90, gy - 4.6 * u - k * 26, 26 + k * 34, 18 + k * 22, 12), { wash: PAL.cream, washOp: 90 * (1 - k), ink: null });
    }
  }

  // 10) 山：层层山脊 + 云海 + 一只很小的身影站在山脊上
  function scMountain(t, lt, dur, S, P, C) {
    skyBands(P.top, P.bot, P.seam, H * 0.40);
    sunAt(1400, 250, 88, P.light);
    clouds(t, H * 0.30, mixCol(P.bot, PAL.cream, 0.5), 3, 1, S.seed);
    ridge(500, H * 0.60, 1400, 420, P.hill2, 3);
    ridge(1250, H * 0.64, 1500, 520, P.hill, 4);
    // 云海
    paint(ellPts(W / 2, H * 0.66, W * 0.72, H * 0.06, 26, 14), { fill: PAL.cream, fillOp: 95, bleed: 0.4, tex: 0.5, ink: null });
    ridge(900, H * 0.88, 1900, 300, P.ground, 5);
    groundBand(H * 0.90, P.ground, P.ground2);
    birds(t, 4, H * 0.24, mixCol(P.line, PAL.ink, 0.3), S.seed);

    // 主角：远小的一点点，抬手挡光
    const u = 15;
    const gx = 1120;
    const gy = H * 0.895;
    clawd(gx, gy, u, {
      ...emotions(lt, [[0, S.mood[0]], [dur * 0.55, S.mood[1]]]),
      view: 'q', aR: 0.85, dy: 0.04 * Math.sin(t * 0.9), boilKey: 'lead' + S.seed
    });
  }

  // 11) 城市：楼群剪影 + 亮着的窗 + 天台上的背影
  function scCity(t, lt, dur, S, P, C) {
    slab(-900, -500, W + 1800, H + 1000, { wash: P.top, ink: null });
    paint(ellPts(W / 2, H * 0.18, W * 0.72, H * 0.30, 26, 12), { fill: P.bot, fillOp: 110, bleed: 0.3, tex: 0.6, ink: null });
    stars(t, 20, H * 0.28, S.seed + 9);
    moonAt(1580, 160, 54, P.accent);

    // 楼群：一块块剪影 + 零星亮窗（亮窗按 hash 决定，永远不会跳）
    for (let i = 0; i < 9; i++) {
      boilSeed('bld' + i);
      const bw = 150 + 90 * hash(i + S.seed);
      const bh = 220 + 420 * hash(i * 3 + S.seed);
      const x = -160 + i * 210;
      slab(x, H * 0.74 - bh, bw, bh + 200, { wash: i % 2 ? P.dark : mixCol(P.dark, P.accent2, 0.45), ink: null });
      for (let r = 0; r < 5; r++) {
        for (let c = 0; c < 3; c++) {
          if (hash(i * 7 + r * 5 + c * 3 + S.seed) < 0.45) {
            const lit = 0.55 + 0.45 * Math.sin(t * 2 + i + r + c);
            paint(rectPts(x + 26 + c * (bw - 52) / 3, H * 0.74 - bh + 40 + r * 70, 24, 32), { wash: P.accent, washOp: 110 + 120 * lit, ink: null });
          }
        }
      }
    }

    // 天台
    groundBand(H * 0.80, P.dark, P.ground2);
    inkLine([[0, H * 0.78], [W, H * 0.78]], 3, mixCol(P.accent2, PAL.cream, 0.4), 'ink', 0.2);

    // 主角：天台背影
    const u = 24;
    const gx = 860;
    const gy = H * 0.865;
    clawd(gx, gy, u, {
      ...emotions(lt, [[0, S.mood[0]], [dur * 0.55, S.mood[1]]]),
      view: 'back', dy: 0.05 * Math.sin(t * 1.0), boilKey: 'lead' + S.seed
    });
  }

  // ---------- 天气 / 天色叠加（任何构图都能下雨、下雪、有星、飘花瓣） ----------
  function overlays(t, S, P) {
    if (S.petals) petals(t, 18, P.accent, PAL.cream, S.seed + 11);
    if (S.flaky) snowFlakes(t, 0.85, mixCol(P.bot, '#FFFFFF', 0.55), S.seed + 7);
    if (S.wet && S.kind !== 'window' && S.kind !== 'rain') rainLines(t, 0.7, mixCol(P.line, PAL.ink, 0.3), S.seed + 3);
    if (S.starry && S.kind !== 'night' && S.kind !== 'city' && S.kind !== 'window') stars(t, 22, H * 0.40, S.seed + 13);
  }

  const DRAW = {
    rain: scRain, night: scNight, station: scStation, sea: scSea, meadow: scMeadow, wind: scWind,
    window: scWindow, road: scRoad, snow: scSnow, mountain: scMountain, city: scCity
  };
"""

_JS_PIXEL = r"""
  // ---------- 像素模式 ----------
  // 逻辑网格 320x180，但**不用**模板的 pxBegin/pxBlit：它们在 WEBGL 主画布上调 noSmooth()，
  // 这个模板里 noSmooth 会把当前帧之后的所有绘制都吃掉（整帧黑掉，实测如此），
  // 所以这里自己管一块全尺寸缓冲，逻辑坐标乘 PSCALE 整数倍画进去，最后 1:1 贴到画布上
  // （尺寸相同的贴图不重采样，天然就是最近邻的硬像素质感）。
  const PXBUF = { g: null };
  const pxRound = (v) => Math.round(v);

  function pxBegin(hex) {
    if (!PXBUF.g) { PXBUF.g = createGraphics(W, H); PXBUF.g.pixelDensity(1); }
    const g = PXBUF.g;
    g.noStroke();
    if (hex) g.background(hex); else g.clear();
    return g;
  }

  function pxRect(g, x, y, w, h, hex) {
    const G = g || PXBUF.g;
    if (!G) return;
    G.noStroke();
    G.fill(hex);
    G.rect(pxRound(x * PSCALE), pxRound(y * PSCALE), Math.max(1, pxRound(w * PSCALE)), Math.max(1, pxRound(h * PSCALE)));
  }

  function pxBlit() { if (PXBUF.g) image(PXBUF.g, 0, 0, W, H); }

  // 像素模式的淡入淡出 / 抖动溶解都画在缓冲里（blit 之前），不碰主画布
  function fadeBlack(k) {
    const a = clamp(k);
    if (a <= 0 || !PXBUF.g) return;
    PXBUF.g.noStroke();
    PXBUF.g.fill(0, 0, 0, 255 * a);
    PXBUF.g.rect(0, 0, W, H);
  }

  function ditherDissolve(k) {
    const a = clamp(k);
    if (a <= 0 || !PXBUF.g) return;
    const g = PXBUF.g, step = 8 * PSCALE;
    g.noStroke();
    g.fill(255);
    for (let y = 0; y < H; y += step) {
      for (let x = 0; x < W; x += step) {
        if (hash(x * 0.01 + y * 13 + Math.floor(T * 24)) < a) g.rect(x, y, step, step);
      }
    }
  }

  // 天空：按 3 行一条的色带做竖直渐变
  function pxSky(g, top, bot, horizon) {
    const step = 3;
    for (let y = 0; y < PH; y += step) {
      const k = clamp((y + step * 0.5) / Math.max(1, horizon));
      pxRect(g, 0, y, PW, Math.min(step, PH - y), mixCol(top, bot, k));
    }
  }

  // 像素圆：逐行画横线
  function pxDisc(g, cx, cy, r, col) {
    const c = pxRound(cx);
    const cc = pxRound(cy);
    for (let dy = -r; dy <= r; dy++) {
      const w = pxRound(Math.sqrt(Math.max(0, r * r - dy * dy)));
      pxRect(g, c - w, cc + dy, w * 2 + 1, 1, col);
    }
  }

  function pxStars(g, t, n, yMax, seed, col) {
    for (let i = 0; i < n; i++) {
      const x = pxRound(hash(i * 3.1 + seed) * PW);
      const y = pxRound(hash(i * 7.7 + seed + 1) * yMax);
      const tw = Math.sin(t * (2 + 2 * hash(i + seed)) + i);
      if (tw > -0.3) pxRect(g, x, y, 1, 1, tw > 0.6 ? col : mixCol(col, '#000000', 0.45));
    }
  }

  function pxRainLines(g, t, seed, k, col) {
    const n = pxRound(18 + 40 * k);
    for (let i = 0; i < n; i++) {
      const x = pxRound((hash(i * 1.7 + seed) * (PW + 40) + t * (40 + 50 * hash(i + seed))) % (PW + 40)) - 20;
      const y = pxRound((hash(i * 9.3 + seed) * PH + t * (150 + 120 * hash(i + 1 + seed))) % (PH + 20)) - 10;
      pxRect(g, x, y, 1, 3 + pxRound(3 * hash(i + 3 + seed)), col);
    }
  }

  function pxSnowFlakes(g, t, seed, k, col) {
    const n = pxRound(26 + 30 * k);
    for (let i = 0; i < n; i++) {
      const x = pxRound((hash(i * 2.9 + seed) * (PW + 20) + Math.sin(t * 0.9 + i) * 6 + t * (6 + 10 * hash(i + seed))) % (PW + 20)) - 10;
      const y = pxRound((hash(i * 6.1 + seed) * PH + t * (16 + 22 * hash(i + 2 + seed))) % (PH + 10)) - 5;
      pxRect(g, x, y, hash(i + 5 + seed) > 0.6 ? 2 : 1, 1, col);
    }
  }

  function pxHills(g, baseY, cols, seed) {
    for (let k = 0; k < cols.length; k++) {
      const r = 28 + pxRound(24 * hash(k + seed));
      const cx = ((k * 150 + seed * 40) % (PW + 120)) - 60;
      pxDisc(g, cx, baseY + pxRound(r * 0.7), r, cols[k]);
      pxDisc(g, cx + pxRound(r * 1.5), baseY + pxRound(r * 0.8), pxRound(r * 0.78), mixCol(cols[k], PAL.ink, 0.18));
    }
  }

  function pxLamp(g, x, y, col) {
    pxRect(g, x, y - 40, 2, 40, PAL.ink);
    pxRect(g, x - 4, y - 44, 10, 2, col);
    pxRect(g, x - 2, y - 50, 6, 3, mixCol(col, '#FFFFFF', 0.4));
  }

  // 方块小人：身体 10s 宽、6.4s 高，四条腿，两只眼睛（会自己眨）
  function pxClawd(g, x, y, s, o) {
    o = o || {};
    const bw = Math.max(6, pxRound(10 * s));
    const bh = Math.max(5, pxRound(6.4 * s));
    const lg = Math.max(1, pxRound(1.6 * s));
    const x0 = pxRound(x - bw / 2);
    const y0 = pxRound(y - bh - lg);
    const col = o.col || '#D97757';
    const dk = mixCol(col, PAL.ink, 0.42);
    pxRect(g, x0, y0, bw, bh, col);
    pxRect(g, x0, y0, bw, 1, mixCol(col, PAL.cream, 0.4));
    pxRect(g, x0, y0 + bh - 1, bw, 1, dk);
    for (let i = 0; i < 4; i++) {
      pxRect(g, x0 + 1 + pxRound(i * (bw - 3) / 3), y0 + bh, Math.max(1, pxRound(0.8 * s)), lg, dk);
    }
    const eh = Math.max(2, pxRound(2.2 * s));
    const ew = Math.max(1, pxRound(1.1 * s));
    const ey = y0 + pxRound(bh * 0.20);
    const ex1 = x0 + pxRound(bw * 0.22);
    const ex2 = x0 + pxRound(bw * 0.72) - ew;
    if (o.blink) {
      pxRect(g, ex1, ey + eh - 1, ew + 1, 1, PAL.ink);
      pxRect(g, ex2, ey + eh - 1, ew + 1, 1, PAL.ink);
    } else {
      pxRect(g, ex1, ey, ew, eh, PAL.ink);
      pxRect(g, ex2, ey, ew, eh, PAL.ink);
    }
    pxRect(g, x0 - 1, y, bw + 2, 1, mixCol(o.shadow || PAL.ink, PAL.cream, 0.35));
  }

  // 1) 雨
  function pxRain(g, t, lt, dur, S, P) {
    pxSky(g, P.top, P.bot, PH * 0.55);
    pxHills(g, PH * 0.58, [P.hill, P.hill2], S.seed);
    pxRect(g, 0, pxRound(PH * 0.66), PW, PH, P.ground);
    for (let i = 0; i < 5; i++) {
      pxRect(g, pxRound(hash(i + S.seed) * (PW - 30)), pxRound(PH * 0.80 + hash(i + 3 + S.seed) * 26), 16 + pxRound(14 * hash(i + 1 + S.seed)), 1, P.accent);
    }
    pxRainLines(g, t, S.seed, 0.85, P.line);
    pxLamp(g, PW - 34, pxRound(PH * 0.66), P.light);
    // 伞 + 人
    const cx = pxRound(PW * 0.46);
    const gy = pxRound(PH * 0.82);
    pxRect(g, cx - 11, gy - 26, 22, 3, mixCol(PAL.clay, P.top, 0.2));
    pxRect(g, cx - 7, gy - 23, 14, 1, mixCol(PAL.clayDk, P.top, 0.2));
    pxRect(g, cx, gy - 29, 1, 3, PAL.ink);
    pxClawd(g, cx, gy, 1.3, { blink: frac(t * 0.5) < 0.08, shadow: P.ground });
  }

  // 2) 夜
  function pxNight(g, t, lt, dur, S, P) {
    pxSky(g, P.top, P.bot, PH * 0.72);
    pxStars(g, t, 40, PH * 0.55, S.seed, PAL.cream);
    pxDisc(g, pxRound(PW * 0.80), pxRound(PH * 0.24), 9, P.accent);
    pxDisc(g, pxRound(PW * 0.80) - 3, pxRound(PH * 0.24) - 2, 8, mixCol(P.top, P.bot, 0.15));
    pxHills(g, PH * 0.76, [P.hill, P.hill2], S.seed);
    pxRect(g, 0, PH - 13, PW, 13, P.ground);
    pxClawd(g, pxRound(PW * 0.30), PH - 12, 1.5, { blink: frac(t * 0.45) < 0.07, shadow: P.ground });
  }

  // 3) 车站
  function pxStation(g, t, lt, dur, S, P) {
    pxSky(g, P.top, P.bot, PH * 0.60);
    pxStars(g, t, 14, PH * 0.30, S.seed + 5, PAL.cream);
    pxRect(g, 0, pxRound(PH * 0.62), PW, PH, P.ground);
    pxRect(g, 0, pxRound(PH * 0.74), PW, 2, P.line);
    pxRect(g, 0, pxRound(PH * 0.80), PW, 2, P.line);
    for (let i = 0; i < 16; i++) pxRect(g, i * 20, pxRound(PH * 0.74), 6, 2, mixCol(P.line, P.ground2, 0.4));
    pxRect(g, 0, pxRound(PH * 0.16), PW, 8, P.accent);
    for (let i = 0; i < 4; i++) {
      const x = 20 + i * 76;
      pxRect(g, x, pxRound(PH * 0.16) + 8, 3, pxRound(PH * 0.46), P.accent2);
      pxRect(g, x - 2, pxRound(PH * 0.22), 7, 2, P.light);
      pxRect(g, x - 4, pxRound(PH * 0.24), 11, 1, mixCol(P.light, PAL.cream, 0.5));
    }
    pxRect(g, PW - 70, pxRound(PH * 0.58), 44, 3, P.accent2);
    const enter = seg(lt, dur * 0.42, dur * 0.42 + 0.35) * (1 - seg(lt, dur * 0.62, dur * 0.95));
    if (enter > 0.02) {
      const x = pxRound(lerp(PW + 30, -30, ease(enter)));
      pxRect(g, x, pxRound(PH * 0.56), 14, 6, P.light);
      pxRect(g, x + 14, pxRound(PH * 0.565), 3, 2, mixCol(P.light, '#FFFFFF', 0.4));
    }
    pxClawd(g, pxRound(PW * 0.42), pxRound(PH * 0.72), 1.5, { blink: frac(t * 0.5) < 0.08, shadow: P.ground });
  }

  // 4) 海
  function pxSea(g, t, lt, dur, S, P) {
    pxSky(g, P.top, P.bot, PH * 0.46);
    pxDisc(g, pxRound(PW * 0.78), pxRound(PH * 0.40), 7, P.light);
    pxRect(g, 0, pxRound(PH * 0.46), PW, PH, P.accent);
    for (let r = 0; r < 6; r++) {
      const y = pxRound(PH * 0.50 + r * 11);
      const off = pxRound(Math.sin(t * 1.4 + r) * 5);
      for (let i = 0; i < 7; i++) pxRect(g, (i * 47 + off + r * 13) % PW, y, 9, 1, mixCol(P.accent, PAL.cream, 0.35 + 0.08 * r));
    }
    for (let i = 0; i < 8; i++) pxRect(g, pxRound(PW * 0.76 + Math.sin(t * 2 + i) * 3), pxRound(PH * 0.48 + i * 5), 8 - i, 1, P.light);
    pxRect(g, 0, pxRound(PH * 0.82), PW, 14, P.ground);
    pxClawd(g, pxRound(PW * 0.34), pxRound(PH * 0.86), 1.4, { blink: frac(t * 0.5) < 0.08, shadow: P.ground });
  }

  // 5) 花田
  function pxMeadow(g, t, lt, dur, S, P) {
    pxSky(g, P.top, P.bot, PH * 0.58);
    pxDisc(g, pxRound(PW * 0.84), pxRound(PH * 0.20), 6, P.light);
    pxHills(g, PH * 0.66, [P.hill, P.hill2], S.seed);
    pxRect(g, 0, pxRound(PH * 0.70), PW, PH, P.ground);
    for (let i = 0; i < 40; i++) {
      const x = pxRound(hash(i * 2.3 + S.seed) * PW);
      const y = pxRound(PH * 0.76 + hash(i + 7 + S.seed) * 22);
      pxRect(g, x, y, 1, 4, mixCol(P.accent2, PAL.ink, 0.3));
      pxRect(g, x - 1, y - 2, 3, 2, i % 3 ? P.accent : PAL.cream);
    }
    // 一路走过去
    pxClawd(g, lerp(PW * 0.30, PW * 0.70, seg(lt, 0, dur)), pxRound(PH * 0.86), 1.5, { blink: frac(t * 0.5) < 0.08, shadow: P.ground });
  }

  // 6) 风
  function pxWind(g, t, lt, dur, S, P) {
    pxSky(g, P.top, P.bot, PH * 0.56);
    for (let i = 0; i < 3; i++) {
      const x = pxRound((hash(i + S.seed) * (PW + 60) + t * 14) % (PW + 60)) - 30;
      pxRect(g, x, pxRound(PH * 0.18 + i * 8), 18, 4, PAL.cream);
      pxRect(g, x + 4, pxRound(PH * 0.16 + i * 8), 10, 2, PAL.cream);
    }
    pxHills(g, PH * 0.68, [P.hill, P.hill2], S.seed);
    pxRect(g, 0, pxRound(PH * 0.72), PW, PH, P.ground);
    for (let i = 0; i < 50; i++) {
      const x = pxRound(hash(i * 1.7 + S.seed) * PW);
      const y = pxRound(PH * 0.78 + hash(i + 4 + S.seed) * 20);
      pxRect(g, x, y, 3 + pxRound(2 * Math.sin(t * 2 + i)), 1, P.accent2);
    }
    for (let i = 0; i < 8; i++) {
      const x = pxRound((hash(i * 2.1 + S.seed) * (PW + 40) + t * 30) % (PW + 40)) - 20;
      const y = pxRound(PH * 0.30 + hash(i + 6 + S.seed) * PH * 0.40 + Math.sin(t * 2 + i) * 3);
      pxRect(g, x, y, 2, 2, PAL.ochre);
    }
    pxClawd(g, pxRound(PW * 0.5), pxRound(PH * 0.84), 1.5, { blink: frac(t * 0.5) < 0.08, shadow: P.ground });
  }

  // 7) 窗
  function pxWindow(g, t, lt, dur, S, P) {
    pxRect(g, 0, 0, PW, PH, P.dark);
    pxRect(g, 0, pxRound(PH * 0.74), PW, PH, P.ground);
    const wx = pxRound(PW * 0.16);
    const wy = pxRound(PH * 0.18);
    const ww = pxRound(PW * 0.46);
    const wh = pxRound(PH * 0.46);
    pxRect(g, wx, wy, ww, wh, P.top);
    pxStars(g, t, 12, wy + wh * 0.5, S.seed, PAL.cream);
    pxDisc(g, wx + ww - 10, wy + 10, 5, P.accent);
    pxRect(g, wx, wy + wh - 8, ww, 8, P.hill);
    if (S.wet) {
      for (let i = 0; i < 14; i++) {
        const x = wx + pxRound(hash(i * 2.7 + S.seed) * ww);
        const y = wy + pxRound((hash(i * 5.3 + S.seed) * 0.5 + frac(t * 0.35 + hash(i)) * 0.5) * wh);
        pxRect(g, x, y, 1, 3, mixCol(P.bot, PAL.cream, 0.4));
      }
    }
    pxRect(g, wx - 2, wy - 2, ww + 4, 2, P.accent2);
    pxRect(g, wx - 2, wy + wh, ww + 4, 2, P.accent2);
    pxRect(g, wx - 2, wy - 2, 2, wh + 4, P.accent2);
    pxRect(g, wx + ww, wy - 2, 2, wh + 4, P.accent2);
    pxRect(g, wx + pxRound(ww / 2) - 1, wy - 2, 2, wh + 4, P.accent2);
    // 台灯
    pxRect(g, PW - 42, pxRound(PH * 0.52), 12, 2, P.accent2);
    pxRect(g, PW - 38, pxRound(PH * 0.42), 10, 10, P.accent);
    pxRect(g, PW - 37, pxRound(PH * 0.54), 2, 10, P.accent2);
    // 窗外站着的人（背对镜头）
    pxClawd(g, pxRound(PW * 0.78), pxRound(PH * 0.86), 1.5, { blink: false, shadow: P.ground });
  }

  // 8) 路：中线与电线杆往左滚，人就一直在画面中间
  function pxRoad(g, t, lt, dur, S, P) {
    pxSky(g, P.top, P.bot, PH * 0.56);
    pxHills(g, PH * 0.62, [P.hill, P.hill2], S.seed);
    pxRect(g, 0, pxRound(PH * 0.66), PW, PH, P.ground);
    for (let i = 0; i < 10; i++) {
      const x = pxRound(((i * 34 - t * 40) % (PW + 40) + PW + 40) % (PW + 40)) - 20;
      pxRect(g, x, pxRound(PH * 0.84), 12, 2, P.accent);
    }
    for (let i = 0; i < 4; i++) {
      const x = pxRound(((i * 90 - t * 40) % (PW + 60) + PW + 60) % (PW + 60)) - 30;
      pxRect(g, x, pxRound(PH * 0.44), 2, pxRound(PH * 0.24), P.accent2);
      pxRect(g, x - 6, pxRound(PH * 0.44), 14, 2, P.accent2);
    }
    pxClawd(g, pxRound(PW * 0.5), pxRound(PH * 0.80), 1.5, { blink: frac(t * 0.5) < 0.08, shadow: P.ground });
  }

  // 9) 雪
  function pxSnow(g, t, lt, dur, S, P) {
    pxSky(g, P.top, P.bot, PH * 0.60);
    for (let i = 0; i < 3; i++) {
      const cx = pxRound(PW * (0.18 + 0.31 * i));
      const baseY = pxRound(PH * 0.66);
      const half = 30 + i * 8;
      const hgt = 34 + i * 6;
      for (let dx = -half; dx <= half; dx += 2) {
        const peak = pxRound(baseY - hgt * (1 - Math.abs(dx) / half));
        pxRect(g, cx + dx, peak, 2, baseY - peak, P.hill2);
        if (Math.abs(dx) < half * 0.45) pxRect(g, cx + dx, peak, 2, pxRound(hgt * 0.35), mixCol(P.bot, '#FFFFFF', 0.6));
      }
    }
    pxRect(g, 0, pxRound(PH * 0.70), PW, PH, P.ground);
    for (let i = 0; i < 2; i++) {
      const x = pxRound(PW * (0.2 + 0.55 * i));
      pxRect(g, x, pxRound(PH * 0.58), 2, pxRound(PH * 0.13), mixCol(P.accent2, PAL.ink, 0.4));
      pxRect(g, x - 5, pxRound(PH * 0.60), 12, 2, mixCol(P.accent2, PAL.ink, 0.4));
    }
    for (let i = 0; i < 7; i++) pxRect(g, 20 + i * 22, pxRound(PH * 0.82) + (i % 2), 4, 2, P.ground2);
    pxSnowFlakes(g, t, S.seed, 0.9, mixCol(P.bot, '#FFFFFF', 0.6));
    pxClawd(g, pxRound(PW * 0.66), pxRound(PH * 0.86), 1.4, { blink: frac(t * 0.4) < 0.1, shadow: P.ground });
  }

  // 10) 山：三层山脊靠逐列正弦高度堆出来
  function pxMountain(g, t, lt, dur, S, P) {
    pxSky(g, P.top, P.bot, PH * 0.55);
    pxDisc(g, pxRound(PW * 0.72), pxRound(PH * 0.26), 6, P.light);
    for (let layer = 0; layer < 3; layer++) {
      const baseY = pxRound(PH * (0.62 + layer * 0.08));
      const col = [P.hill2, P.hill, P.accent2][layer];
      const h = 34 - layer * 7;
      for (let x = 0; x < PW; x += 2) {
        const peak = pxRound(baseY - h * Math.abs(Math.sin(x * 0.035 + layer * 1.7)));
        pxRect(g, x, peak, 2, baseY - peak, col);
      }
    }
    pxRect(g, 0, pxRound(PH * 0.86), PW, PH, P.ground);
    for (let i = 0; i < 3; i++) pxRect(g, pxRound((PW * 0.2 + i * 14 + t * 4) % PW), pxRound(PH * 0.20 + i * 3), 3, 1, P.line);
    pxClawd(g, pxRound(PW * 0.62), pxRound(PH * 0.90), 0.9, { blink: frac(t * 0.5) < 0.08, shadow: P.ground });
  }

  // 11) 城市
  function pxCity(g, t, lt, dur, S, P) {
    pxSky(g, P.top, P.bot, PH * 0.60);
    pxStars(g, t, 18, PH * 0.30, S.seed + 9, PAL.cream);
    pxDisc(g, pxRound(PW * 0.84), pxRound(PH * 0.18), 5, P.accent);
    for (let i = 0; i < 10; i++) {
      const w = 18 + pxRound(10 * hash(i + S.seed));
      const h = 20 + pxRound(40 * hash(i * 3 + S.seed));
      const x = i * 33 - 6;
      const y = pxRound(PH * 0.78) - h;
      pxRect(g, x, y, w, h + 20, i % 2 ? P.dark : mixCol(P.dark, P.accent2, 0.4));
      for (let r = 0; r < 3; r++) {
        for (let c = 0; c < 2; c++) {
          if (hash(i * 7 + r * 5 + c * 3 + S.seed) < 0.5) pxRect(g, x + 4 + c * 9, y + 4 + r * 8, 3, 3, P.accent);
        }
      }
    }
    pxRect(g, 0, pxRound(PH * 0.78), PW, PH, P.dark);
    pxRect(g, 0, pxRound(PH * 0.76), PW, 1, mixCol(P.accent2, PAL.cream, 0.4));
    pxClawd(g, pxRound(PW * 0.46), pxRound(PH * 0.82), 1.5, { blink: frac(t * 0.5) < 0.08, shadow: P.dark });
  }

  const PXDRAW = {
    rain: pxRain, night: pxNight, station: pxStation, sea: pxSea, meadow: pxMeadow, wind: pxWind,
    window: pxWindow, road: pxRoad, snow: pxSnow, mountain: pxMountain, city: pxCity
  };
"""

_JS_CLOSE = r"""
  // ---------- 相机 ----------
  // camBegin/camEnd 必须成对；返回的 C 给画法用（主角要留在画面里就贴 C.cx）
  function beginCam(S, lt, dur) {
    const k = seg(lt, 0, dur);
    if (S.cam === 'pan') {
      const cx = lerp(420, 1500, ease(k));
      camBegin(cx, 600 + 8 * Math.sin(lt * 0.6), 1.04);
      return { cx: cx, cy: 600, z: 1.04 };
    }
    if (S.cam === 'push') {
      const z = 1 + 0.12 * easeOut(k);
      camBegin(960, 560, z);
      return { cx: 960, cy: 560, z: z };
    }
    if (S.cam === 'rise') {
      const cy = lerp(630, 460, ease(k));
      camBegin(960, cy, 1.02);
      return { cx: 960, cy: cy, z: 1.02 };
    }
    const cx = 960 + 24 * Math.sin(lt * 0.5 + S.seed);
    const z = 1.01 + 0.03 * k;
    camBegin(cx, 552 + 8 * Math.cos(lt * 0.4), z);
    return { cx: cx, cy: 552, z: z };
  }

  // ---------- 转场 ----------
  // p：0 → 0.5 盖住（正好是切点），0.5 → 1 揭开。
  // 相邻两镜共用同一处接缝的参数（上一镜结尾 p 走到 0.5，下一镜开头从 0.5 继续）。
  // 四种接缝轮着用：笔刷拖走 / 盖一层纸 / 一道幕布从左推过 / 暖光闪一下。
  // （core.js 的 iris 要画 40 块大水彩，又慢又脏，这里不用它。
  const TR_MODES = ['wipe', 'paper', 'curtain', 'flash'];

  function seam(p, mode, P) {
    if (p <= 0 || p >= 1) return;
    const a = p < 0.5 ? ease(p * 2) : 1 - ease((p - 0.5) * 2);
    if (mode === 'curtain') {
      const w = -120 + (W + 240) * a;
      paint(rectPts(-60, -60, w + 60, H + 120), { wash: mixCol(P.dark, PAL.ink, 0.35), washOp: 245, ink: null });
      inkLine([[-60, -60], [w - 40, H * 0.5], [-60, H + 120]], 2, mixCol(P.accent2, PAL.cream, 0.4), 'ink', 0.6);
      return;
    }
    if (mode === 'flash') { flash(a, mixCol(P.accent, PAL.cream, 0.5)); return; }
    if (mode === 'paper') { flash(a, PAL.paper); return; }
    brushWipe(p, [P.dark, P.accent2]);
  }

  // ---------- 像素：一镜 ----------
  function pxShot(t, lt, dur, S, i) {
    const P = pal(S.kind, S.v);
    const g = pxBegin(P.top);
    (PXDRAW[S.kind] || pxMeadow)(g, t, lt, dur, S, P);
    // 天气叠加画在网格里
    if (S.flaky && S.kind !== 'snow') pxSnowFlakes(g, t, S.seed + 7, 0.8, mixCol(P.bot, '#FFFFFF', 0.55));
    if (S.wet && S.kind !== 'rain' && S.kind !== 'window') pxRainLines(g, t, S.seed + 3, 0.7, P.line);

    // 转场：偶数接缝抖开，奇数接缝黑场；首镜从黑场淡入，末镜淡到黑。全部画在缓冲里，最后再 blit
    const last = i === SCENES.length - 1;
    if (i === 0 && lt < 0.5) {
      fadeBlack(1 - lt / 0.5);
    } else if (i > 0 && lt < 0.3) {
      const k = 1 - lt / 0.3;
      if ((i - 1) % 2) fadeBlack(k); else ditherDissolve(k);
    }
    if (!last && lt > dur - 0.3) {
      const k = (lt - (dur - 0.3)) / 0.3;
      if (i % 2) fadeBlack(k); else ditherDissolve(k);
    }
    if (last && lt > dur - 0.6) fadeBlack((lt - (dur - 0.6)) / 0.6);

    pxBlit();
  }

  // ---------- 水彩：一镜 ----------
  function shot(t, lt, dur, S, i) {
    if (PIXEL) { pxShot(t, lt, dur, S, i); return; }
    const P = pal(S.kind, S.v);
    const C = beginCam(S, lt, dur);
    (DRAW[S.kind] || scMeadow)(t, lt, dur, S, P, C);
    overlays(t, S, P);
    camEnd();

    // 每个接缝都要有转场：首镜从纸面淡入，末镜收在纸上
    const last = i === SCENES.length - 1;
    if (i === 0 && lt < 0.55) seam(0.5 + lt / 1.1, 'paper', P);
    else if (i > 0 && lt < 0.28) seam(0.5 + lt / 0.56, TR_MODES[(i - 1) % 4], P);
    if (!last && lt > dur - 0.28) seam((lt - (dur - 0.28)) / 0.56, TR_MODES[i % 4], P);
    if (last && lt > dur - 0.5) seam((lt - (dur - 0.5)) / 1.0, 'paper', P);
  }

  // 一句歌词 = 一个镜头，全部交给 timeline.js 的 shots() 排
  shots(SCENES.map((S, i) => [S.t, (t, lt, dur) => shot(t, lt, dur, S, i)]));
})();
"""

#: 完整模板：四段拼起来就是一个 generated.js
_JS_TEMPLATE = "".join((_JS_OPEN, _JS_STAGE, _JS_PIXEL, _JS_CLOSE))

#: studio.html 里 demo.js 的 script 标签（vendor 模板里没有缩进）
_DEMO_TAG = re.compile(r'<script\s+src="src/scenes/demo\.js"\s*>\s*</script>')
_GEN_TAG = '<script src="src/scenes/generated.js"></script>'


def _render_js(scenes: list[dict], style: str) -> str:
    """镜头计划 + 风格 → 完整 JS 源码。

    风格只换调色/纸墨/叠加层（STYLE_PACK），画法仍由 STYLE === 'pixel' 决定。
    """
    key = style_lib.resolve_style(str(style or ""))
    pack = style_lib.style_pack(key)
    return (
        _JS_TEMPLATE.replace("__SCENES__", _scenes_js(scenes))
        .replace("__STYLE__", key)
        .replace("__STYLE_PACK__", json.dumps(pack, ensure_ascii=False))
    )


def build_scene(
    timeline: list[tuple[float, float, str]], *, style: str = "watercolor", prompt: str = ""
) -> str:
    """按歌词时间轴生成一个完整的场景 JS 源码字符串。

    timeline 是 [(start, end, text), ...]，一条对应一个镜头；
    prompt 是用户需求（关键词会并进每个镜头）；style == "pixel" 走像素原语，否则水彩。
    """
    return _render_js(_plan(timeline, style, prompt), style)


def install(
    project_dir: Path,
    timeline: list[tuple[float, float, str]],
    *,
    style: str = "watercolor",
    prompt: str = "",
) -> dict:
    """把生成的场景写进任务目录副本，并让 studio.html 加载它而不是 demo.js。

    只动 project_dir 里的副本（engine.py 复制出来的 project/），vendor/template/ 保持只读。
    """
    project = Path(project_dir)
    scenes = _plan(timeline, style, prompt)
    source = _render_js(scenes, style)

    target = project / "src" / "scenes" / "generated.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")

    replaced = _rewrite_studio(project / "studio.html")
    return {
        "file": str(target),
        "style": style_lib.resolve_style(str(style or "")),
        "scenes": len(scenes),
        "replaced_demo": replaced,
    }


def _rewrite_studio(path: Path) -> bool:
    """把 studio.html 的 demo.js 引用换成 generated.js；返回最终是否由 generated.js 生效。"""
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        return False

    if _GEN_TAG in html:
        # 已经接好了（重复调用 install 也不会多插一行）
        return _DEMO_TAG.search(html) is None

    if _DEMO_TAG.search(html):
        html = _DEMO_TAG.sub(_GEN_TAG, html, count=1)
    elif "</body>" in html:
        # 模板万一改了名，退一步：插在 </body> 前，顺序仍在 timeline.js / karaoke.js 之后
        html = html.replace("</body>", "  " + _GEN_TAG + "\n</body>", 1)
    else:
        return False

    try:
        path.write_text(html, encoding="utf-8")
    except OSError:
        return False
    return (_GEN_TAG in html) and (_DEMO_TAG.search(html) is None)