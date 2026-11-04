"""安全地导入 HuggingFace ``transformers``。

## 为什么需要这一层

``transformers`` 5.x 用**模块级 ``__getattr__``**（惰性模块）暴露 ``AutoModel`` 这类
自动类。如果另一个线程正在**首次导入** ``transformers``，这边执行

    from transformers import AutoModel

可能拿到刚放进 ``sys.modules``、但 ``__init__`` 还没执行完的半初始化模块，于是报：

    ImportError: cannot import name 'AutoModel' from 'transformers'

本项目的 RAG 嵌入模型和情绪模型都在**后台线程**里惰性加载，实测踩过这个坑：
一旦失败，RAG 会**静默降级成词法检索**——不报错、不影响聊天，只是检索质量下降，
日志里只留一行 warning，非常难发现。

## 对策

1. 用一把全局锁串行化导入；
2. 失败时把半成品从 ``sys.modules`` 里丢掉，重试一次（首次成功后就再也不会走这条路）。
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: 串行化导入：同一个模块被两个线程同时首次导入是这个坑的根源
_IMPORT_LOCK = threading.Lock()

_cached: Optional[dict[str, Any]] = None


def load() -> dict[str, Any]:
    """返回 ``{"transformers", "AutoModel", "AutoTokenizer"}``，失败抛 ImportError。"""
    global _cached
    if _cached is not None:
        return _cached

    with _IMPORT_LOCK:
        if _cached is not None:
            return _cached

        for attempt in (1, 2):
            try:
                import transformers  # noqa: F401
                from transformers import AutoModel, AutoTokenizer

                _cached = {
                    "transformers": transformers,
                    "AutoModel": AutoModel,
                    "AutoTokenizer": AutoTokenizer,
                }
                return _cached
            except ImportError as exc:
                if attempt == 2:
                    raise
                # 半初始化：丢掉重新导入一次（正常的 ImportError 会在这里原样抛出）
                logger.warning("transformers 导入异常（多半是并发首次导入），重试一次：%s", exc)
                for name in [key for key in sys.modules if key.startswith("transformers")]:
                    sys.modules.pop(name, None)
        raise ImportError("无法导入 transformers")  # pragma: no cover - 兜底


def auto_classes() -> tuple[Any, Any]:
    """便利函数：直接拿 ``(AutoModel, AutoTokenizer)``。"""
    loaded = load()
    return loaded["AutoModel"], loaded["AutoTokenizer"]