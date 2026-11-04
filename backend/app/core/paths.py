"""路径的唯一真相来源。

后端代码从 ``backend/`` 整体搬进了 ``backend/app/``，如果各处继续用
``Path(__file__).parents[N]`` 自己推算，层级一变就全崩。这里统一定义一次。

目录约定::

    <repo>/backend/app/core/paths.py   ← 本文件
    <repo>/backend/app/                ← APP_DIR，导入根
    <repo>/backend/                    ← BACKEND_DIR，放 data / tests / .env
    <repo>/                            ← PROJECT_ROOT
"""

from __future__ import annotations

from pathlib import Path

#: 后端应用的导入根（uvicorn --app-dir backend/app）
APP_DIR = Path(__file__).resolve().parents[1]

#: backend/：放 data、tests、.env、requirements
BACKEND_DIR = APP_DIR.parent

#: 仓库根目录
PROJECT_ROOT = BACKEND_DIR.parent

#: 运行时数据（原文分块、数据库、模型、贴纸）——不进 git
DATA_DIR = BACKEND_DIR / "data"
WORKSPACE_DIR = DATA_DIR / "workspace"
LIBRARY_DIR = WORKSPACE_DIR / "books"
MODELS_DIR = DATA_DIR / "models"
STICKER_DIR = DATA_DIR / "stickers"
UPLOADS_DIR = DATA_DIR / "uploads"
BACKUPS_DIR = DATA_DIR / "backups"
