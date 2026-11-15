"""planning 技能的工具：把一堆待办排成有序清单。

纯规则、零模型调用、零外部依赖——所以它在任何机器上都能用。

排序不是随便拍的，按「身体 > 有时限 > 要别人配合 > 家务 > 其他」，
这是有心理学依据的：饿着肚子做任何事都会更慢更差，而家务是最不该
在精力低谷时做的（容易拖到第二天，但代价最小）。
"""

from __future__ import annotations

import re

from core.tools import protocol as tool_protocol

#: 按优先级打分：数字越小越先做
PRIORITY_RULES: list[tuple[int, str, tuple[str, ...]]] = [
    (1, "身体先", ("吃饭", "吃点", "睡", "休息", "吃药", "喝水", "洗澡", "上厕所")),
    (2, "今天有时限", ("工作表", "汇报", "截止", "开会", "交", "打卡", "提交", "报告")),
    (3, "要别人配合", ("回复", "回消息", "打电话", "通知", "约", "联系", "确认")),
    (4, "家务", ("洗衣", "衣服", "浇花", "打扫", "收", "扔", "买", "洗")),
    (5, "其他", ()),
]

#: 拆分待办：顿号、逗号、分号、以及"还/然后/接着"这类连接
_SPLITTER = re.compile(r"[、，,；;]|(?:还有|然后|接着|之后|再)")

#: 去掉开头的语气词
_LEADING = ("还没", "没有", "没", "要", "得", "需要", "打算", "忘了")


def _clean(item: str) -> str:
    text = item.strip()
    for prefix in _LEADING:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text.strip(" 。.！!？?")


def _priority(text: str) -> tuple[int, str]:
    for rank, label, words in PRIORITY_RULES:
        if any(word in text for word in words):
            return rank, label
    return 5, "其他"


def _tool_task_breakdown(args: dict) -> dict:
    raw = str(args.get("text") or "").strip()
    if not raw:
        return {"ok": False, "error": "没有可拆解的内容"}

    items: list[str] = []
    for chunk in _SPLITTER.split(raw):
        cleaned = _clean(chunk or "")
        # 太短的片段（"我"、"还有"）不要
        if len(cleaned) < 2:
            continue
        if cleaned in items:
            continue
        items.append(cleaned)

    if not items:
        return {"ok": False, "error": "没听出具体要做的事"}

    ranked = []
    for index, item in enumerate(items):
        rank, label = _priority(item)
        ranked.append((rank, index, item, label))
    ranked.sort(key=lambda row: (row[0], row[1]))

    lines = []
    for position, (rank, _index, item, label) in enumerate(ranked, 1):
        lines.append(f"{position}. {item}（{label}）")

    return {
        "ok": True,
        "text": "按这个顺序来：" + "；".join(lines),
        "count": len(ranked),
        "order": [{"item": item, "why": label} for _rank, _i, item, label in ranked],
    }


tool_protocol.register(
    tool_protocol.ToolSpec(
        name="task_breakdown",
        description="把用户列的待办拆成有序清单（身体 > 有时限 > 要人配合 > 家务 > 其他）",
        parameters={
            "text": {"type": "string", "description": "用户原话，通常会列好几件事"},
        },
        handler=_tool_task_breakdown,
        required=("text",),
        domain="work",
    )
)