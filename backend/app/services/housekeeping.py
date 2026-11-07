"""后台清理：聊天图片按保留期删除。

图片是"原图落盘 + 历史可读回"的，好处是刷新后还能看到；
坏处是它会一直变大。这里按天清理，默认留 30 天。

只删 ``uploads/chat/<32位hex>/`` 这种我们自己生成的目录，
目录里少一个文件、多一个子目录都不会误伤别的东西。
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 我们自己生成的图片目录名：32 位十六进制
_FOLDER = re.compile(r"^[0-9a-f]{32}$")


def chat_image_dirs(root: Path) -> list[Path]:
    """列出所有聊天图片目录（非法的名字直接跳过）。"""
    if not root.is_dir():
        return []
    return [item for item in root.iterdir() if item.is_dir() and _FOLDER.match(item.name)]


def cleanup_chat_images(
    root: Path, *, days: int, now: float | None = None
) -> dict:
    """删掉超过 ``days`` 天的图片目录。

    ``days <= 0`` 表示永久保留，直接返回不删。
    返回 ``{"removed": 目录数, "kept": 目录数, "freed_bytes": 字节数}``。
    """
    if days <= 0:
        return {"removed": 0, "kept": len(chat_image_dirs(root)), "freed_bytes": 0}

    cutoff = (now if now is not None else time.time()) - days * 86400
    removed = 0
    kept = 0
    freed = 0

    for directory in chat_image_dirs(root):
        try:
            modified = directory.stat().st_mtime
        except OSError:
            continue
        if modified >= cutoff:
            kept += 1
            continue

        size = 0
        for item in directory.rglob("*"):
            if item.is_file():
                try:
                    size += item.stat().st_size
                except OSError:
                    pass
        shutil.rmtree(directory, ignore_errors=True)
        removed += 1
        freed += size

    if removed:
        logger.info(
            "清理聊天图片：删除 %d 个目录（保留 %d 个），释放 %.1f MB",
            removed, kept, freed / 1024 / 1024,
        )
    return {"removed": removed, "kept": kept, "freed_bytes": freed}