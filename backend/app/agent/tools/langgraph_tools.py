"""工具层 · 第二层：LangGraph 封装。

把协议层的 ``ToolSpec`` 转成框架认识的两种形态：

1. ``StructuredTool``：给模型做 function calling 用（LangChain 标准）
2. ``ToolNode``：LangGraph 预置节点，读取 ``AIMessage.tool_calls`` 并执行，
   结果写回 ``ToolMessage``——适合"模型自己决定调哪个工具"的场景

## 和本项目的实际用法

当前对话**不让模型自由调工具**（角色说话不应该有工具调用延迟），
而是由 Skill（第三层）按 SOP 决定调什么。所以：

* ``StructuredTool`` / ``ToolNode`` 是**能力预留 + 可测**，
  真要接"模型自主调用"时，把 ``ToolNode`` 挂进图即可；
* 但 graph 里的 ``skill`` 节点走的是 ``protocol.execute``（同步、零延迟）。

这样既没有把 LangChain 类型漏进业务层，又保留了升级路径。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolNode

from core import tool_protocol as protocol

# 导入即注册：工具定义在业务层（services/skills.py），
# 这里保证只要用到 LangGraph 工具，注册表就一定是满的。
from services import skills  # noqa: F401

logger = logging.getLogger(__name__)


def to_structured_tool(spec: protocol.ToolSpec) -> StructuredTool:
    """协议层的工具 -> LangChain StructuredTool。

    ``args_schema`` 用 dict 描述，LangChain 会转成 pydantic 模型；
    这里直接复用协议层的 JSON Schema。
    """

    def _run(**kwargs: Any) -> dict:
        return protocol.execute(spec.name, kwargs)

    return StructuredTool(
        name=spec.name,
        description=spec.description,
        func=_run,
        args_schema=spec.parameters,
    )


def to_langgraph_tools(tool_names: Optional[list[str]] = None) -> list[StructuredTool]:
    """一批工具转成 LangChain 工具。"""
    chosen = tool_names or protocol.names()
    tools: list[StructuredTool] = []
    for name in chosen:
        spec = protocol.get(name)
        if spec is None:
            continue
        tools.append(to_structured_tool(spec))
    return tools


def build_tool_node(tool_names: Optional[list[str]] = None) -> ToolNode:
    """构造一个 LangGraph 的 ToolNode（模型自主调工具时挂进图用）。"""
    return ToolNode(to_langgraph_tools(tool_names))


def invoke_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """按 ``AIMessage.tool_calls`` 的形状批量执行，返回 ToolMessage 风格的 dict。

    不依赖具体模型：拿到 ``[{"name":..., "args":..., "id":...}]`` 就能跑。
    """
    results: list[dict] = []
    for call in tool_calls or []:
        name = str((call or {}).get("name") or "")
        args = dict((call or {}).get("args") or {})
        output = protocol.execute(name, args)
        results.append(
            {
                "role": "tool",
                "tool_call_id": (call or {}).get("id") or name,
                "name": name,
                "content": output,
            }
        )
    return results


def describe() -> dict:
    return {
        "protocol": protocol.describe(),
        "langgraph_tools": protocol.names(),
        "tool_node_ready": True,
    }