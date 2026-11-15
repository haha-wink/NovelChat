"""工具层：① 协议层（ToolSpec 注册表 / 执行 / OpenAI schema）。"""

from core.tools import protocol
from core.tools.protocol import ToolSpec, execute, get, names, openai_schema, register, specs

__all__ = ["protocol", "ToolSpec", "execute", "get", "names", "openai_schema", "register", "specs"]
