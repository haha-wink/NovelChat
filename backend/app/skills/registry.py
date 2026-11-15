"""技能注册表：按**模式**过滤 + 按**信号**打分选人。

``mode`` 是唯一的功能闸门：

```
chat（默认）：情感聊天 + 简单任务     → comfort / steady / suggest / greeting
work（工作模式）：可以调专业技能      → 上面全部 + animation / 未来的 writing、research
```

在注册表这一层过滤，而不是在 agent 里写 ``if mode == 'work'``——
这样加一个新技能只需要放一个目录，不用改任何分支。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from skills.base import Skill, SkillResult
from skills import loader

logger = logging.getLogger(__name__)

#: 模式 -> 说明（前端开关文案用它）
MODES: dict[str, str] = {
    "chat": "情感聊天（默认）",
    "work": "工作模式（可调用专业技能）",
}

#: 技能目录：chat/ 与 work/ 两个子目录
SKILLS_DIR = Path(__file__).resolve().parent

_SKILLS: dict[str, Skill] = {}
_LOADED = False


def _extra_dirs() -> list[Path]:
    """外部可插拔技能目录（settings.skills_extra_dirs，逗号分隔）。"""
    from core.config import get_settings

    raw = str(getattr(get_settings(), "skills_extra_dirs", "") or "")
    return [Path(item.strip()) for item in raw.split(",") if item.strip()]


def _ensure_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True

    for folder in ("chat", "work"):
        directory = SKILLS_DIR / folder
        # 先加载引擎（注册工具），再解析 SKILL.md（引用工具名）
        try:
            loader.load_engines(directory)
        except Exception:  # noqa: BLE001
            logger.exception("加载技能引擎失败：%s", directory)
        for skill in loader.load_skill_dir(directory):
            register(skill, replace=True)

    # 外部技能包：把一个目录丢进去就能用，不用改代码
    for directory in _extra_dirs():
        try:
            loader.load_engines(directory)
            for skill in loader.load_skill_dir(directory):
                register(skill, replace=True)
                logger.info("外部技能已加载：%s（%s）", skill.name, directory)
        except Exception:  # noqa: BLE001
            logger.exception("加载外部技能失败：%s", directory)

    logger.info("技能加载完成：%d 个（%s）", len(_SKILLS), "、".join(_SKILLS))


def register(skill: Skill, *, replace: bool = False) -> None:
    if skill.name in _SKILLS and not replace:
        logger.warning("技能重名，已忽略：%s", skill.name)
        return
    _SKILLS[skill.name] = skill


def reload() -> list[Skill]:
    """重新扫目录（改完 SKILL.md 不用重启进程）。"""
    global _LOADED
    _SKILLS.clear()
    _LOADED = False
    _ensure_loaded()
    return list(_SKILLS.values())


def all_skills() -> list[Skill]:
    _ensure_loaded()
    return list(_SKILLS.values())


def get(name: str) -> Optional[Skill]:
    _ensure_loaded()
    return _SKILLS.get(name)


def _usable(skill: Skill) -> tuple[bool, list[str]]:
    from skills import capabilities

    return capabilities.check(skill.requires)


def available(mode: str = "chat") -> list[Skill]:
    """当前模式下**且依赖齐全**的技能。"""
    wanted = (mode or "chat").strip().lower()
    result = []
    for skill in all_skills():
        if wanted not in skill.modes:
            continue
        usable, _missing = _usable(skill)
        if usable:
            result.append(skill)
    return result


def unavailable(mode: str = "chat") -> list[dict]:
    """该模式下可用但缺依赖的技能（前端/health 用来解释为什么没有）。"""
    wanted = (mode or "chat").strip().lower()
    result = []
    for skill in all_skills():
        if wanted not in skill.modes:
            continue
        usable, missing = _usable(skill)
        if not usable:
            result.append({"name": skill.name, "missing": missing})
    return result


def choose(signals: dict, *, mode: str = "chat") -> Optional[Skill]:
    """在该模式的技能里挑分最高的一个。"""
    best: Optional[Skill] = None
    best_score = 0.0
    for skill in available(mode):
        score = skill.score(signals or {})
        if score > best_score:
            best, best_score = skill, score
    return best


def run_for(
    signals: dict, *, mode: str = "chat", extra: Optional[dict] = None
) -> Optional[SkillResult]:
    """挑一个技能并执行；没有合适的返回 None。"""
    skill = choose(signals or {}, mode=mode)
    if skill is None:
        return None
    context = {
        "emotion": (signals or {}).get("emotion") or "难过",
        "topic": (extra or {}).get("topic") or "",
        # 技能里用 $text 引用用户原话
        "text": (signals or {}).get("text") or "",
        "intent": (signals or {}).get("intent") or "chat",
        "mode": mode,
        **(extra or {}),
    }
    return skill.run(context)


def describe() -> dict:
    return {
        "modes": [{"key": key, "label": label} for key, label in MODES.items()],
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "modes": list(skill.modes),
                "tools": [name for name, _ in skill.steps],
                "requires": list(skill.requires),
                "external": skill.external,
                "available": _usable(skill)[0],
                "missing": _usable(skill)[1],
            }
            for skill in all_skills()
        ],
        "disabled": {"chat": unavailable("chat"), "work": unavailable("work")},
    }