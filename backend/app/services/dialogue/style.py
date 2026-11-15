"""说话风格统计：把「像不像这个人」变成可执行的数字。

提示词里写「冷淡、话少」，模型只能猜；写「平均 12 字、62% 以问号或省略号收尾、
常说『其实』『大概』」，模型才照着做。这里负责从角色原话里算出这些数字。

样本不足时返回空统计（``{}``），渲染层据此整段省略——不会让模型看到一堆 0。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Sequence

from services.catalog import name_library
from services.catalog.name_library import translit_ratio

#: 取连续汉字段，用于提口头禅
_HAN_RUN = re.compile(r"[\u4e00-\u9fff]+")

#: 含这些字的片段太通用，不算口头禅（"这个""我们"之类）
_STOP_CHARS = frozenset(
    "的了是在和就都而及与着或一个上不我你他她它这那也很又还个吗呢吧啊呀哦嗯"
    "么什怎样为以要会能可就才再最更已经过被把给对从到说地得"
)

#: 句子首尾可能带的引号/括号，统计长度和标点前先剥掉
_TRIM = " \t\n\r“”\"'‘’「」『』《》（）()"

#: 句末标点：判断"怎么收尾"
_ENDS = {
    "question": ("？", "?"),
    "ellipsis": ("……", "…", "..."),
    "exclaim": ("！", "!"),
    "period": ("。",),
}


_SPACES = re.compile(r"\s+")

#: 纯拉丁字母且这么短的（GG、OK、lol）通常是游戏梗或误抽，当样本没价值
_MIN_LATIN = 4
_LATIN_ONLY = re.compile(r"^[A-Za-z0-9\s._-]+$")


def _lines(quotes: Sequence[str]) -> list[str]:
    """清洗原话：去空白与引号、去重、丢掉噪声行。

    去重很重要：抽取时同一句台词常被不同章节各收一次，
    不去重的话"爸爸"会被当成口头禅，重复条目还会挤掉真正的样本。
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in quotes or []:
        text = str(raw or "").strip().strip(_TRIM)
        if not text or len(text) < 2:
            continue
        if _LATIN_ONLY.match(text) and len(text) <= _MIN_LATIN:
            continue
        key = _SPACES.sub("", text)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned


def pick_quotes(
    quotes: Sequence[str], *, limit: int = 12, chars: int = 700
) -> list[str]:
    """挑若干条原话塞进提示词：先均匀抽样，再按字符预算截断。

    均匀抽样是为了覆盖不同阶段的语言风格（早期 / 中期 / 后期），
    只取开头或结尾都会让模型学到某个片段的腔调。
    """
    cleaned = _lines(quotes)
    if not cleaned:
        return []
    if limit > 0 and len(cleaned) > limit:
        step = len(cleaned) / limit
        cleaned = [cleaned[int(index * step)] for index in range(limit)]

    picked: list[str] = []
    used = 0
    for line in cleaned:
        if picked and used + len(line) > chars:
            break
        picked.append(line)
        used += len(line)
    return picked


def _catchphrases(
    lines: Sequence[str], aliases: Sequence[str], *, top: int = 3, max_len: int = 4
) -> list[str]:
    """找重复出现的说法（按"出现在几条不同的话里"计数，不看词频）。

    用文档频率而不是总次数：一句话里重复十遍的，不代表这个人的口头禅。
    含停用字、或本身就是角色名（别名）片段的，直接排除。
    """
    banned = "".join(str(alias) for alias in aliases)
    counts: Counter[str] = Counter()

    def _looks_like_name(gram: str) -> bool:
        """「康斯」「坦丁」这种是专名碎片，不是口头禅。

        判据：整串都是译名字（translit_ratio 接近 1），或人名判定给了高分。
        """
        if translit_ratio(gram) >= 0.99:
            return True
        verdict = name_library.analyze(gram)
        return verdict.kind in ("cn", "foreign") and verdict.score >= 0.5

    for line in lines:
        seen: set[str] = set()
        for run in _HAN_RUN.findall(line):
            for size in range(2, max_len + 1):
                for start in range(len(run) - size + 1):
                    gram = run[start: start + size]
                    if any(char in _STOP_CHARS for char in gram):
                        continue
                    if banned and gram in banned:
                        continue
                    if _looks_like_name(gram):
                        continue
                    seen.add(gram)
        counts.update(seen)

    total = len(lines)
    # 样本多时要求出现在 3 条以上：2 条往往只是同一段话的近重复
    floor = 3 if total >= 6 else 2
    candidates = [
        gram for gram, appears in counts.items() if floor <= appears < total
    ]
    # 出现次数多、字数多的优先；同分时更长的优先
    candidates.sort(key=lambda gram: (-counts[gram] * len(gram), -len(gram), gram))

    picked: list[str] = []
    for gram in candidates:
        if any(gram in longer for longer in picked):
            continue  # 已被更长的候选包含，避免「其实」「其实吧」同时出现
        picked.append(gram)
        if len(picked) >= top:
            break
    return picked


def analyze(
    quotes: Sequence[str], *, aliases: Sequence[str] = (), min_samples: int = 3
) -> dict:
    """统计原话特征。样本不足 ``min_samples`` 条时返回空字典。"""
    lines = _lines(quotes)
    if len(lines) < min_samples:
        return {}

    lengths = [len(line) for line in lines]
    total = len(lines)

    def ratio(kind: str) -> int:
        hit = 0
        for line in lines:
            stripped = line.rstrip("”’」』）》")
            if stripped.endswith(_ENDS[kind]):
                hit += 1
        return round(hit * 100 / total)

    return {
        "count": total,
        "avg": round(sum(lengths) / total, 1),
        "min": min(lengths),
        "max": max(lengths),
        "question": ratio("question"),
        "ellipsis": ratio("ellipsis"),
        "exclaim": ratio("exclaim"),
        "period": ratio("period"),
        "catchphrases": _catchphrases(lines, aliases),
    }


def render(stats: dict) -> str:
    """把统计渲染成提示词里的一段（没有样本时返回空串）。"""
    if not stats:
        return ""

    lines = [
        f"- 每条平均 {stats['avg']} 字（最短 {stats['min']} 字，最长 {stats['max']} 字）"
    ]

    tails: list[str] = []
    if stats.get("question") >= 15:
        tails.append(f"{stats['question']}% 以问号结尾")
    if stats.get("ellipsis") >= 15:
        tails.append(f"{stats['ellipsis']}% 用省略号收尾")
    if stats.get("exclaim") >= 15:
        tails.append(f"{stats['exclaim']}% 带感叹号")
    if stats.get("period") >= 40:
        tails.append(f"{stats['period']}% 是完整句号结尾")
    if tails:
        lines.append("- " + "，".join(tails))

    catchphrases = stats.get("catchphrases") or []
    if catchphrases:
        lines.append("- 常出现的说法：" + "、".join(f"「{word}」" for word in catchphrases))

    return "\n".join(lines)