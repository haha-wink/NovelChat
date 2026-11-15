"""工具层 · 第一层：Function Call 协议。

## 三层结构

```
③ Skill   业务层    services/skills.py      「安慰一个人」这件事，由哪些工具组成
     ↑
② Tool    框架层    agent/tools/langgraph_tools.py   LangGraph 的 StructuredTool / ToolNode
     ↑
① Protocol 协议层   core/tool_protocol.py    函数的 JSON Schema + 执行器
```

**为什么要分三层**：同一件事在不同场合要换形态。

* 模型直接调用（function calling）—— 需要 JSON Schema
* LangGraph 编排（ToolNode）—— 需要 `StructuredTool`
* 业务组合（一个 skill 调三个工具）—— 需要能按名字找回来

如果只有一层，这三种用法会互相污染：业务代码里出现 LangChain 类型，
或者协议层被迫处理 LangGraph 的 state。

## 本层职责

* 定义 ``ToolSpec``：名字、描述、参数 JSON Schema、处理函数
* 维护注册表，支持按名字取用
* ``execute`` 做参数校验 + 异常兜底，**永不抛异常**（工具挂了不能影响聊天）
* ``openai_schema`` 产出 function calling 用的声明（L2 从这里转 StructuredTool）

## 为什么放在 core

``services`` 不能依赖 ``agent``（分层铁律）。技能层（services/skills.py）要用协议层，
所以协议层必须待在更底层。它也确实只依赖标准库，够格进 core。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的定义。"""

    name: str
    description: str
    #: JSON Schema 的 properties 部分，例如 {"seconds": {"type": "integer", ...}}
    parameters: dict[str, dict]
    #: 真正干活的函数：收 dict，返回 dict
    handler: Callable[[dict], dict]
    #: 必填参数名
    required: tuple[str, ...] = ()
    #: 归属域（诊断用）
    domain: str = ""

    def openai_schema(self) -> dict:
        """转成 function calling 的声明格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": list(self.required),
                },
            },
        }


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    """注册一个工具（同名覆盖，便于测试替换）。"""
    _REGISTRY[spec.name] = spec
    return spec


def unregister(name: str) -> None:
    """注销一个工具（测试替换、热更新用）。"""
    _REGISTRY.pop(name, None)


def get(name: str) -> Optional[ToolSpec]:
    return _REGISTRY.get(name)


def names() -> list[str]:
    return sorted(_REGISTRY)


def specs() -> list[ToolSpec]:
    return [_REGISTRY[name] for name in names()]


def openai_schema(tool_names: Optional[list[str]] = None) -> list[dict]:
    """产出 function calling 声明；不传名字就是全部。"""
    chosen = [get(name) for name in (tool_names or names())]
    return [spec.openai_schema() for spec in chosen if spec is not None]


def execute(name: str, arguments: Optional[dict] = None) -> dict:
    """执行一个工具。

    返回 ``{"ok": bool, "name": str, ...}``，**任何异常都吞掉**并转成 ok=False。
    工具是锦上添花，不能因为它挂了把整轮对话搞崩。
    """
    spec = get(name)
    if spec is None:
        return {"ok": False, "name": name, "error": "没有这个工具"}

    args = dict(arguments or {})
    missing = [key for key in spec.required if key not in args]
    if missing:
        return {"ok": False, "name": name, "error": f"缺少参数：{', '.join(missing)}"}

    try:
        result = spec.handler(args)
        if not isinstance(result, dict):
            result = {"value": result}
        return {"ok": True, "name": name, **result}
    except Exception as exc:  # noqa: BLE001 - 工具失败不影响对话
        logger.exception("工具执行失败：%s", name)
        return {"ok": False, "name": name, "error": str(exc)}


def describe() -> dict:
    """给 /api/health 或调试看的工具清单。"""
    return {
        "count": len(_REGISTRY),
        "tools": [
            {"name": spec.name, "domain": spec.domain, "required": list(spec.required)}
            for spec in specs()
        ],
    }