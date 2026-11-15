"""SKILL.md 解析：把技能目录变成可执行的 ``Skill``。

格式（和外部 skill 包一致）：

```markdown
---
name: comfort
description: 用户在倾诉：先接住情绪
modes: [chat]
when:
  intent: [vent]
  not_crisis: true
  min_intensity: 0.3
steps:
  - [comfort_phrase, {emotion: 难过}]
  - [breathing_guide, {seconds: 4, rounds: 3}]
---
正文：给模型看的补充说明（可选）
```

``when`` 支持的条件：

| 字段 | 含义 |
|---|---|
| ``intent`` / ``emotion`` | 命中其中任一（列表或单值） |
| ``keywords`` | 用户原话里出现任一关键词 |
| ``action_words`` | 同时需要出现任一动作词（防"只提到名词"） |
| ``min_intensity`` | 情绪强度下限 |
| ``crisis`` / ``not_crisis`` | 危机必须/必须不成立 |
| ``base`` / ``bonus`` | 基础分 / 全部命中后的加分 |
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from skills.base import Skill

logger = logging.getLogger(__name__)

#: skills 包根目录（判断一个 engine 是不是内置的）
SKILLS_PACKAGE_DIR = Path(__file__).resolve().parent

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


def _split_top_level(text: str) -> list[str]:
    """按顶层逗号拆，括号里的逗号不算。

    ``animation_start, {prompt: $text, style: watercolor}`` 必须拆成两段，
    而不是四段——之前就是这里把步骤参数拆空了。
    """
    parts: list[str] = []
    depth = 0
    quote = ""
    chunk = ""
    for char in text:
        if quote:
            chunk += char
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            chunk += char
            continue
        if char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(chunk)
            chunk = ""
        else:
            chunk += char
    if chunk.strip():
        parts.append(chunk)
    return [part.strip() for part in parts if part.strip()]


def _parse_scalar(text: str) -> Any:
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in _split_top_level(inner)]
    if text.startswith("{") and text.endswith("}"):
        return _parse_inline_map(text[1:-1])
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text.strip("'\"")


def _parse_inline_map(text: str) -> dict:
    """解析 `{emotion: 难过, seconds: 4}` 这种一行写完的映射。"""
    result: dict[str, Any] = {}
    for part in _split_top_level(text):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        result[key.strip()] = _parse_scalar(value)
    return result


def _parse_frontmatter(raw: str) -> dict:
    """只支持本文件头注释里列的那几种写法——够用且零依赖。"""
    data: dict[str, Any] = {}
    lines = raw.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if ":" not in line:
            index += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()

        if rest:
            data[key] = _parse_scalar(rest)
            index += 1
            continue

        # 嵌套块
        block: list[str] = []
        index += 1
        while index < len(lines) and (lines[index].startswith(("  ", "\t"))
                                      or not lines[index].strip()):
            block.append(lines[index])
            index += 1
        data[key] = _parse_block(key, block)
    return data


def _parse_block(key: str, block: list[str]) -> Any:
    items = [line for line in block if line.strip()]
    if not items:
        return {}

    if key == "steps":
        steps: list[tuple[str, dict]] = []
        for line in items:
            stripped = line.strip()
            if not stripped.startswith("-"):
                continue
            payload = stripped[1:].strip()
            if payload.startswith("[") and payload.endswith("]"):
                parsed = _parse_scalar(payload)
                if isinstance(parsed, list) and parsed:
                    name = str(parsed[0])
                    args = parsed[1] if len(parsed) > 1 and isinstance(parsed[1], dict) else {}
                    steps.append((name, args))
        return steps

    result: dict[str, Any] = {}
    for line in items:
        if ":" not in line:
            continue
        sub_key, _, sub_value = line.strip().partition(":")
        result[sub_key.strip()] = _parse_scalar(sub_value)
    return result


def _as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def build_when(rules: dict):
    """把声明式条件编译成打分函数。"""

    def when(signals: dict) -> float:
        if not rules:
            return 0.0
        crisis = bool(signals.get("crisis"))
        if rules.get("crisis") and not crisis:
            return 0.0
        if rules.get("not_crisis") and crisis:
            return 0.0

        wanted_intents = _as_list(rules.get("intent"))
        if wanted_intents and str(signals.get("intent") or "") not in wanted_intents:
            return 0.0

        wanted_emotions = _as_list(rules.get("emotion"))
        if wanted_emotions and str(signals.get("emotion") or "") not in wanted_emotions:
            return 0.0

        minimum = rules.get("min_intensity")
        if minimum is not None:
            try:
                if float(signals.get("intensity") or 0.0) < float(minimum):
                    return 0.0
            except (TypeError, ValueError):
                return 0.0

        text = str(signals.get("text") or "")
        keywords = _as_list(rules.get("keywords"))
        if keywords and not any(str(word) in text for word in keywords):
            return 0.0
        action_words = _as_list(rules.get("action_words"))
        if action_words and not any(str(word) in text for word in action_words):
            return 0.0

        base = float(rules.get("base") or 0.7)
        bonus = float(rules.get("bonus") or 0.0)
        if bonus and action_words:
            return min(1.0, base + bonus)
        return base

    return when


def parse_skill(path: Path) -> Optional[Skill]:
    """把一个 SKILL.md 读成 Skill。"""
    skill_file = path / "SKILL.md" if path.is_dir() else path
    if not skill_file.exists():
        return None
    text = skill_file.read_text(encoding="utf-8").lstrip("\ufeff")
    match = _FRONTMATTER.match(text)
    if not match:
        logger.warning("SKILL.md 缺少 frontmatter：%s", skill_file)
        return None
    meta = _parse_frontmatter(match.group(1))
    body = match.group(2).strip()

    name = str(meta.get("name") or skill_file.parent.name).strip()
    if not name:
        return None

    modes = tuple(str(item) for item in _as_list(meta.get("modes"))) or ("chat",)
    steps = meta.get("steps") or []
    if not isinstance(steps, list):
        steps = []

    return Skill(
        name=name,
        description=str(meta.get("description") or "").strip(),
        modes=modes,
        when=build_when(meta.get("when") or {}),
        steps=[(str(item[0]), dict(item[1] or {})) for item in steps],
        requires=tuple(str(item) for item in _as_list(meta.get("requires"))),
        external=bool(meta.get("external") or False),
        body=body,
        path=str(skill_file),
    )


def load_skill_dir(directory: Path) -> list[Skill]:
    """加载技能包。

    支持两种布局：
    - ``目录/SKILL.md``          -> 单个技能（外部插拔包常用）
    - ``目录/子目录/SKILL.md``   -> 一组技能（内置 chat/ work/ 用这种）
    """
    skills: list[Skill] = []
    if not directory.exists() or not directory.is_dir():
        return skills

    if (directory / "SKILL.md").exists():
        skill = parse_skill(directory)
        return [skill] if skill is not None else []

    for child in sorted(directory.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        skill = parse_skill(child)
        if skill is not None:
            skills.append(skill)
    return skills


def _engine_module_name(engine: Path) -> Optional[str]:
    """内置技能用包路径导入，保证和 `import skills.xxx.engine` 是**同一个模块对象**。

    否则（用 importlib 按文件加载）会得到一份副本：工具注册在副本上，
    测试/调用方却改的是原模块，打洞无效。
    """
    try:
        relative = engine.resolve().relative_to(SKILLS_PACKAGE_DIR.resolve())
    except ValueError:
        return None
    parts = list(relative.with_suffix("").parts)
    return ".".join(["skills", *parts])


def load_engines(directory: Path) -> None:
    """导入技能自带的 engine.py——它们负责注册自己用到的工具。"""
    import importlib
    import importlib.util

    if not directory.exists():
        return
    for child in sorted(directory.iterdir()):
        engine = child / "engine.py"
        if not engine.exists():
            continue
        module_name = _engine_module_name(engine)
        try:
            if module_name:
                importlib.import_module(module_name)
            else:
                # 外部目录：只能按文件加载
                spec = importlib.util.spec_from_file_location(
                    f"skills._external_engines.{child.name}", engine
                )
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - 引擎坏了不该拖垮技能列表
            logger.exception("加载技能引擎失败：%s", engine)
