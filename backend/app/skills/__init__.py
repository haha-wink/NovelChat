"""技能包。导入即就绪（首次使用时才真正扫目录）。"""

from skills.base import Skill, SkillResult
from skills.registry import (
    MODES,
    all_skills,
    available,
    choose,
    describe,
    get,
    reload,
    register,
    run_for,
)

__all__ = [
    "Skill", "SkillResult", "MODES",
    "all_skills", "available", "choose", "describe", "get", "reload",
    "register", "run_for",
]

# 导入即加载：技能自带的 engine.py 会在这一步注册工具（comfort_phrase 等）。
# 不这么做的话，先调 core.tools.protocol.names() 会拿到空表。
all_skills()  # noqa: B018 - 触发 registry._ensure_loaded
