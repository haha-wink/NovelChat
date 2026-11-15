"""notes 技能的工具：把一段杂乱的话整理成纪要。

三段式：结论 / 待办 / 未决问题。纯规则，零模型调用。

为什么用规则而不是让模型总结：这类整理是**结构问题**，不是语义问题——
"带问号的进未决、带'要/需要'的进待办"已经能覆盖绝大多数日常表达，
而且零延迟、零成本、结果稳定（同样的输入永远同样的输出）。
"""

from __future__ import annotations

import re

from core.tools import protocol as tool_protocol

#: 切句但保留句末标点——"回滚谁来定？" 的"？"是判断未决问题的唯一依据
_SENTENCE = re.compile(r"[^。！!？?\n]+[。！!？?]?")

#: 待办信号
TODO_WORDS = ("要", "需要", "得", "打算", "准备", "记得", "安排", "明天", "下周", "截止")
#: 结论信号
DONE_WORDS = ("已经", "完成了", "搞定了", "确认了", "决定了", "最后", "所以", "因为")


def _sentences(text: str) -> list[str]:
    parts = []
    for chunk in _SENTENCE.findall(text):
        cleaned = chunk.strip()
        if len(cleaned.strip("。！!？? ")) >= 3:
            parts.append(cleaned)
    return parts


def _tool_note_digest(args: dict) -> dict:
    raw = str(args.get("text") or "").strip()
    if not raw:
        return {"ok": False, "error": "没有可整理的内容"}

    done: list[str] = []
    todo: list[str] = []
    open_questions: list[str] = []

    for sentence in _sentences(raw):
        if sentence.endswith(("？", "?")) or "吗" in sentence[-3:] or "怎么办" in sentence:
            open_questions.append(sentence)
        elif any(word in sentence for word in TODO_WORDS):
            todo.append(sentence)
        elif any(word in sentence for word in DONE_WORDS):
            done.append(sentence)
        else:
            # 其余归结论：陈述句默认就是"已经知道的事"
            done.append(sentence)

    sections = []
    if done:
        sections.append("结论：" + "；".join(done[:5]))
    if todo:
        sections.append("待办：" + "；".join(todo[:5]))
    if open_questions:
        sections.append("未决：" + "；".join(open_questions[:5]))

    return {
        "ok": True,
        "text": "\n".join(sections),
        "done": done,
        "todo": todo,
        "open": open_questions,
    }


tool_protocol.register(
    tool_protocol.ToolSpec(
        name="note_digest",
        description="把一段话整理成纪要（结论 / 待办 / 未决问题）",
        parameters={"text": {"type": "string", "description": "要整理的原文"}},
        handler=_tool_note_digest,
        required=("text",),
        domain="work",
    )
)