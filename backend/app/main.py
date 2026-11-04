"""FastAPI 应用入口。

启动方式（两种都行，不再要求先 ``cd backend``）：

    python backend/main.py
    python -m uvicorn main:app --reload --app-dir backend
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from contextlib import asynccontextmanager

# 让 `python backend/main.py` 能直接 import 同目录的 api/services/core/readers
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from api.v1.router import router as api_router  # noqa: E402
from core import db  # noqa: E402
from core.config import get_settings  # noqa: E402

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backend")


def _warm_emotion_model() -> None:
    """后台预热情绪小模型（ONNX 加载约 4s）。

    情绪判断在**每轮对话**的兜底路径上，冷启动会让第一条消息莫名多等几秒。
    """
    try:
        from services import emotion_model

        backend = emotion_model.get_backend()
        if backend.name != "onnx":
            logger.info("情绪模型后端：%s（未启用 ONNX）", backend.name)
            return
        started = time.perf_counter()
        backend._ensure_loaded()  # noqa: SLF001 - 预热专用入口
        logger.info(
            "情绪模型预热完成（%.1fs）：%s",
            time.perf_counter() - started,
            emotion_model.info().get("onnx", {}).get("model_file"),
        )
    except Exception:  # noqa: BLE001 - 预热失败只是回退规则后端
        logger.exception("情绪模型预热失败（将回退规则后端）")


def _warm_rag() -> None:
    """后台预热 RAG：加载模型（约 7s）并给已有记忆补向量。

    不预热的话，第一次聊天会莫名多等 7 秒——用户只会觉得"卡了"。
    """
    try:
        from services import rag

        started = time.perf_counter()
        status = rag.backend_status()
        indexed = rag.index_all()
        logger.info(
            "RAG 预热完成（%s，%.1fs）：%d 个角色 / %d 条向量",
            status["backend"],
            time.perf_counter() - started,
            indexed["characters"],
            indexed["vectors"],
        )
    except Exception:  # noqa: BLE001 - 预热失败只是退化成词法检索
        logger.exception("RAG 预热失败（聊天将退化为词法检索）")


def _warm_models() -> None:
    """后台依次预热 RAG 与情绪模型。

    **必须串行**：两者都会 ``import transformers``，两个线程同时做首次导入时，
    后一个可能拿到半初始化的模块（实测报 ``cannot import name 'AutoModel'``），
    结果 RAG 静默退化成词法检索——不报错，只是检索质量下降，很难发现。
    """
    _warm_rag()
    # 向量后端偶发加载失败会被永久缓存，这里补一次重试，避免"悄悄降级"挂一整个进程
    try:
        from core.config import get_settings as _get_settings
        from services import rag

        if _get_settings().rag_backend != "lexical" and rag.backend() != "vector":
            logger.warning("RAG 向量后端首次加载失败，重试一次")
            rag.retry_embedder()
    except Exception:  # noqa: BLE001 - 重试失败就继续用词法
        logger.exception("RAG 向量后端重试失败")
    _warm_emotion_model()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    db.init_db()
    logger.info("数据目录：%s", settings.data_path)
    logger.info("作品库：%s", settings.library_path)

    if not settings.deepseek_api_key.strip():
        logger.warning("未配置 DEEPSEEK_API_KEY：上传小说后角色抽取会失败，配置后可点「重新抽取」")

    # 清理上次异常退出残留的聊天图片临时目录
    upload_root = settings.uploads_path
    if upload_root.is_dir():
        for stale in upload_root.glob("chat-*"):
            shutil.rmtree(stale, ignore_errors=True)

    # 回收"抽取到一半进程没了"的书：任务在进程内，重启后不会自己恢复，
    # 不标记失败的话前端会一直转圈，用户只能删书重传。
    try:
        interrupted = db.recover_interrupted_extractions()
        from services import agent_log

        for book in interrupted:
            logger.warning("抽取任务被重启中断：%s（%s）", book["title"], book["id"])
            agent_log.log(
                "extraction_failed",
                owner_type="manager",
                book_id=book["id"],
                severity="warn",
                payload={"reason": "service_restarted"},
            )
        if interrupted:
            logger.warning("已把 %d 本被中断的书标为可重试", len(interrupted))
    except Exception:  # noqa: BLE001 - 回收失败不该挡住启动
        logger.exception("回收被中断的抽取任务失败")

    # 模型预热放后台线程：加载不能挡在启动路径上（RAG 与情绪模型串行，见 _warm_models）
    threading.Thread(target=_warm_models, name="model-warmup", daemon=True).start()

    try:
        yield
    finally:
        from services import library

        for book_id in list(library._tasks):
            library.delete_task(book_id)


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="小说角色扮演 API",
        description="上传小说 -> 抽取角色 -> 与角色 AI 对话（FastAPI + Agno + DeepSeek）",
        version="2.0.0",
        lifespan=lifespan,
    )

    # 只放行配置里的来源；`*` 时自动关闭 credentials（浏览器规范要求）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_credentials=settings.allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix="/api")

    @app.get("/")
    def read_root() -> dict:
        return {"status": "ok", "message": "Backend is running!", "docs": "/docs"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    # 传 app 对象而非 import 字符串：从任何目录启动都能跑
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
