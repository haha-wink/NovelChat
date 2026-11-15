"""writing 技能的工具：按文体给结构和语气建议。

只给**框架**，不代写正文——先对齐结构再填内容，返工最少。

文体靠关键词判断；判不出来就用通用结构。
"""

from __future__ import annotations

from core.tools import protocol as tool_protocol

#: 文体 -> (结构, 语气建议, 篇幅建议)
GENRES: dict[str, dict] = {
    "邮件": {
        "keywords": ("邮件", "发信", "回信", "抄送"),
        "outline": ["称呼（谁、什么关系）", "一句话说明来意", "正文：事实 + 请求（分段）",
                    "明确的时间/交付物", "结尾客套"],
        "tone": "正式但不生硬；把请求写成疑问句，别用命令句",
        "length": "150~300 字，超过 3 段就分小标题",
    },
    "周报": {
        "keywords": ("周报", "日报", "汇报"),
        "outline": ["本周完成（按结果写，不按过程写）", "数据/影响（有数字就放数字）",
                    "遇到的问题 + 已采取的动作", "下周计划（3 条以内）", "需要支持的"],
        "tone": "结论先行，动词开头；不写'参与了''协助了'这类模糊说法",
        "length": "每段 2~3 行，全文一屏能看完",
    },
    "报告": {
        "keywords": ("报告", "方案", "分析", "调研"),
        "outline": ["结论（第一段就说完）", "背景与目标", "方法/数据来源",
                    "发现（分点，每点一句结论 + 一句证据）", "建议与下一步"],
        "tone": "客观、可验证；每个判断后面跟依据",
        "length": "正文控制在 2 页内，细节放附录",
    },
    "文案": {
        "keywords": ("文案", "标题", "推广", "朋友圈", "海报", "宣传"),
        "outline": ["钩子（第一句让人停下来）", "痛点或场景（具体的、能看见的）",
                    "给出解法（一句话）", "证据或反差点", "行动号召"],
        "tone": "短句、不用形容词堆砌；具体名词比抽象概念有力",
        "length": "总长 100 字内，第一句不超过 20 字",
    },
    "通知": {
        "keywords": ("通知", "公告", "告知"),
        "outline": ["什么事（一句话）", "什么时候、在哪", "谁需要做什么",
                    "找谁问（联系方式）"],
        "tone": "直白、无歧义；时间地点必须精确到分钟和楼层",
        "length": "100 字内",
    },
}

GENERIC = {
    "outline": ["结论/目的（一句话）", "背景（为什么现在要说）",
                "主体（3 点以内，每点一句结论 + 一句支撑）", "下一步/请求"],
    "tone": "一个段落只说一件事；能用动词就不用名词化表达",
    "length": "先短后长：写完删掉三分之一的形容词",
}


def _detect(text: str) -> str:
    for name, spec in GENRES.items():
        if any(word in text for word in spec["keywords"]):
            return name
    return ""


def _tool_writing_outline(args: dict) -> dict:
    raw = str(args.get("text") or "").strip()
    if not raw:
        return {"ok": False, "error": "没说要写什么"}

    genre = _detect(raw)
    spec = GENRES.get(genre, GENERIC)

    lines = [f"按这个结构写" + (f"（{genre}）" if genre else "（通用）") + "："]
    for index, item in enumerate(spec["outline"], 1):
        lines.append(f"{index}. {item}")
    lines.append(f"语气：{spec['tone']}")
    lines.append(f"篇幅：{spec['length']}")

    return {
        "ok": True,
        "text": "\n".join(lines),
        "genre": genre or "通用",
        "outline": spec["outline"],
    }


tool_protocol.register(
    tool_protocol.ToolSpec(
        name="writing_outline",
        description="按文体（邮件/周报/报告/文案/通知）给出结构和语气建议",
        parameters={"text": {"type": "string", "description": "用户要写什么"}},
        handler=_tool_writing_outline,
        required=("text",),
        domain="work",
    )
)