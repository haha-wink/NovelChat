"""SKILL.md 校验：写错了要**立刻报**，而不是静默加载失败。

技能包是数据驱动的，好处是不用改代码，坏处是写错一个字就悄无声息：
`modes: [wrok]` 会让技能永远不出现，`steps` 里工具名拼错会运行时才炸。

这里把这些问题集中到启动时检查：

```
$ python -m skills.validate                    # 检查内置技能
$ python -m skills.validate D:/my-skills       # 检查外部技能包
```

也能通过 `GET /api/skills/validate` 看，或在 health 里看 `skill_issues`。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from skills import loader

#: when 里认得的条件
KNOWN_WHEN_KEYS = {
    "intent", "emotion", "keywords", "action_words",
    "min_intensity", "crisis", "not_crisis", "base", "bonus",
}

#: steps 参数里认得的占位符
KNOWN_PLACEHOLDERS = {
    "text", "emotion", "intent", "topic", "mode",
    # 素材类：由调用方（群聊/单聊）从上传的文件里填
    "style", "lyrics", "lyrics_file", "audio", "duration",
}

#: frontmatter 认得的字段
KNOWN_META_KEYS = {
    "name", "description", "modes", "when", "steps", "requires", "external",
}

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass
class Issue:
    severity: str
    skill: str
    field: str
    message: str
    path: str = ""

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "skill": self.skill,
            "field": self.field,
            "message": self.message,
            "path": self.path,
        }

    def __str__(self) -> str:
        mark = "✗" if self.severity == SEVERITY_ERROR else "!"
        return f"{mark} [{self.skill}] {self.field}: {self.message}"


@dataclass
class Report:
    checked: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [item for item in self.issues if item.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [item for item in self.issues if item.severity == SEVERITY_WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "errors": [item.as_dict() for item in self.errors],
            "warnings": [item.as_dict() for item in self.warnings],
        }


def _known_modes() -> set[str]:
    from skills.registry import MODES

    return set(MODES)


def _known_tools() -> set[str]:
    from core.tools import protocol

    return set(protocol.names())


def _check_placeholders(value, *, skill: str, tool: str, path: str) -> list[Issue]:
    """步骤参数里的 $xxx 必须是已知占位符。"""
    issues: list[Issue] = []
    if isinstance(value, str):
        for token in value.split("$")[1:]:
            name = ""
            for char in token:
                if char.isalnum() or char == "_":
                    name += char
                else:
                    break
            if name and name not in KNOWN_PLACEHOLDERS:
                issues.append(Issue(
                    SEVERITY_ERROR, skill, f"steps.{tool}",
                    f"未知占位符 ${name}（可用：{', '.join(sorted(KNOWN_PLACEHOLDERS))}）",
                    path,
                ))
    elif isinstance(value, dict):
        for item in value.values():
            issues.extend(_check_placeholders(item, skill=skill, tool=tool, path=path))
    elif isinstance(value, list):
        for item in value:
            issues.extend(_check_placeholders(item, skill=skill, tool=tool, path=path))
    return issues


def validate_skill(skill, *, modes: set[str], tools: set[str]) -> list[Issue]:
    """校验已解析出来的 Skill。"""
    issues: list[Issue] = []
    name = skill.name or "(未命名)"
    path = getattr(skill, "path", "")

    if not skill.name:
        issues.append(Issue(SEVERITY_ERROR, name, "name", "缺少 name", path))

    if not skill.description.strip():
        issues.append(Issue(SEVERITY_WARNING, name, "description",
                            "没有写 description（模型挑技能时会缺依据）", path))

    if not skill.modes:
        issues.append(Issue(SEVERITY_ERROR, name, "modes", "modes 不能为空", path))
    for mode in skill.modes:
        if mode not in modes:
            issues.append(Issue(
                SEVERITY_ERROR, name, "modes",
                f"未知模式 {mode!r}（可用：{', '.join(sorted(modes))}）", path))

    rules = getattr(skill, "when_rules", None) or {}
    for key in rules:
        if key not in KNOWN_WHEN_KEYS:
            issues.append(Issue(
                SEVERITY_WARNING, name, f"when.{key}",
                f"未知条件 {key!r}（认得：{', '.join(sorted(KNOWN_WHEN_KEYS))}）", path))

    dir_name = getattr(skill, "dir_name", "")
    if dir_name and skill.name and dir_name != skill.name:
        issues.append(Issue(
            SEVERITY_WARNING, name, "name",
            f"name 和目录名不一致（目录 {dir_name}）", path))

    if not skill.steps:
        issues.append(Issue(SEVERITY_WARNING, name, "steps",
                            "没有 steps，这个技能不会调用任何工具", path))
    for tool_name, args in skill.steps:
        if tool_name not in tools:
            issues.append(Issue(
                SEVERITY_ERROR, name, f"steps.{tool_name}",
                f"工具 {tool_name!r} 没有注册（engine.py 没导入？名字拼错？）", path))
        if not isinstance(args, dict):
            issues.append(Issue(SEVERITY_ERROR, name, f"steps.{tool_name}",
                                "参数必须是 {key: value}", path))
        else:
            issues.extend(_check_placeholders(args, skill=name, tool=tool_name, path=path))

    # 依赖名必须在能力探测里认得
    from skills import capabilities

    for dep in skill.requires:
        if dep not in capabilities.DEPENDENCY_HINTS:
            issues.append(Issue(
                SEVERITY_WARNING, name, "requires",
                f"未知依赖 {dep!r}（已知：{', '.join(sorted(capabilities.DEPENDENCY_HINTS))}）",
                path))

    return issues


def validate_skills(skills: Iterable, *, tools: Optional[set[str]] = None) -> Report:
    modes = _known_modes()
    known_tools = tools if tools is not None else _known_tools()
    report = Report()
    seen: dict[str, str] = {}

    for skill in skills:
        report.checked += 1
        path = getattr(skill, "path", "")
        if skill.name in seen:
            report.issues.append(Issue(
                SEVERITY_ERROR, skill.name, "name",
                f"技能重名：{seen[skill.name]} 与 {path}", path))
        seen[skill.name] = path
        report.issues.extend(validate_skill(skill, modes=modes, tools=known_tools))

    return report


def validate_paths(paths: Iterable[Path]) -> Report:
    """校验磁盘上的技能包（不依赖注册表，装之前先体检用）。"""
    from skills.registry import _ensure_loaded  # noqa: F401 - 确保引擎已导入

    import skills  # noqa: F401

    skills_found = []
    for path in paths:
        folder = Path(path)
        if (folder / "SKILL.md").exists():
            skill = loader.parse_skill(folder)
            if skill is not None:
                skills_found.append(skill)
            else:
                report = Report(checked=1)
                report.issues.append(Issue(
                    SEVERITY_ERROR, folder.name, "frontmatter",
                    "SKILL.md 解析失败（缺少 --- 包裹的 frontmatter？）", str(folder)))
                return report
            continue
        if folder.is_dir():
            skills_found.extend(loader.load_skill_dir(folder))
        else:
            report = Report(checked=1)
            report.issues.append(Issue(
                SEVERITY_ERROR, str(folder), "path", "路径不存在", str(folder)))
            return report

    return validate_skills(skills_found)


def format_report(report: Report) -> str:
    lines = [f"检查 {report.checked} 个技能："
             f"{len(report.errors)} 个错误，{len(report.warnings)} 个警告"]
    for issue in report.errors + report.warnings:
        lines.append(f"  {issue}")
        if issue.path:
            lines.append(f"     {issue.path}")
    if report.ok and not report.warnings:
        lines.append("  全部通过")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args:
        report = validate_paths([Path(item) for item in args])
    else:
        from skills import registry

        report = validate_paths([registry.SKILLS_DIR])
    print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())