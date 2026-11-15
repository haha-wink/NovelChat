"""技能包的公共类型。

一个技能 = `SKILL.md`（元数据 + 说明）+ 可选 `engine.py`（注册它要用的工具）。

和外部 skill 包（如 painted-animation）的格式对齐：YAML frontmatter 描述
「什么时候用、用什么模式、调哪些工具」，正文是给模型看的说明。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class SkillResult:
    """技能执行结果。"""

    skill: str
    materials: list[str] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)
    note: str = ""

    def as_block(self) -> str:
        """拼成提示词里的【可用素材】块。"""
        if not self.materials:
            return ""
        lines = ["【可用素材（自然融入，不要照念）】"]
        lines.extend(f"- {item}" for item in self.materials)
        return "\n".join(lines)


#: `$text` 这类占位符，运行时用上下文替换
_PLACEHOLDER = re.compile(r"\$(\w+)")


def _fill(value: Any, context: dict) -> Any:
    """把 steps 里的 `$text` 换成真实值（只处理字符串，递归进 dict/list）。"""
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            key = match.group(1)
            return str(context.get(key) or "")

        return _PLACEHOLDER.sub(replace, value)
    if isinstance(value, dict):
        return {key: _fill(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, context) for item in value]
    return value


@dataclass
class Skill:
    """一个业务技能（由 SKILL.md 解析而来）。"""

    name: str
    description: str = ""
    #: 适用模式：chat（情感聊天，默认）/ work（工作模式）
    modes: tuple[str, ...] = ("chat",)
    #: 适用度打分：0 = 不该用。由 when 声明式规则生成
    when: Optional[Callable[[dict], float]] = None
    #: 要调的工具，形如 [("breathing_guide", {"seconds": 4})]
    steps: list[tuple[str, dict]] = field(default_factory=list)
    #: SKILL.md 正文（给模型看的说明，可选注入）
    body: str = ""
    #: 技能目录（排查用）
    path: str = ""

    def run(self, context: Optional[dict] = None) -> SkillResult:
        """按 steps 依次调工具，收集素材。同步、零额外模型调用。"""
        from core.tools import protocol

        context = context or {}
        result = SkillResult(skill=self.name)
        for tool_name, base_args in self.steps:
            args = _fill(base_args, context)
            output = protocol.execute(tool_name, args)
            result.outputs.append(output)
            if output.get("ok") and output.get("text"):
                result.materials.append(str(output["text"]))
            elif not output.get("ok"):
                result.note = output.get("error") or "工具失败"
        return result

    def score(self, signals: dict) -> float:
        if self.when is None:
            return 0.0
        try:
            return float(self.when(signals or {}))
        except Exception:  # noqa: BLE001 - 判据出错当成不适用
            return 0.0