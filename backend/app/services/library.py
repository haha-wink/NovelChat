"""作品库：原文落盘、分块、调度角色抽取、角色增删改。"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Optional

import anyio

from core import db
from core.config import get_settings
from services import agent_log
from services import character_miner as miner
from services import cleaner
from services import extractor
from agent.prompts import pick_avatar

logger = logging.getLogger(__name__)

#: book_id -> 后台抽取任务（同时兼作强引用，防止任务被 GC）
_tasks: dict[str, asyncio.Task] = {}


def book_dir(book_id: str) -> Path:
    return get_settings().library_path / book_id


def create_book(
    *,
    title: str,
    source_name: str,
    source_type: str,
    text: str,
    shelf_id: str = "",
) -> dict:
    """保存原文与分块，返回作品记录（状态为 extracting，需调用 schedule_extraction）。"""
    # 入库前先洗正文：盗版站广告会污染抽取、提示词和 RAG 检索
    text, clean_stats = cleaner.clean_text(text)
    # 只去掉几个空白字符不值得打日志（每本都会命中，会刷屏）
    if clean_stats["removed_lines"] or clean_stats["deduped_lines"] or clean_stats["removed_ratio"] >= 0.005:
        logger.info(
            "正文清洗：去掉 %d 字（%.1f%%），%d 行广告/分隔线，%d 行重复页眉",
            clean_stats["removed_chars"],
            clean_stats["removed_ratio"] * 100,
            clean_stats["removed_lines"],
            clean_stats["deduped_lines"],
        )

    chunks, chunk_size = extractor.plan_chunks(text)
    book = db.create_book(
        title=title,
        source_name=source_name,
        source_type=source_type,
        char_count=len(text),
        chunk_count=len(chunks),
        shelf_id=shelf_id or None,
    )

    directory = book_dir(book["id"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source.txt").write_text(text, encoding="utf-8")
    (directory / "chunks.json").write_text(
        json.dumps(chunks, ensure_ascii=False), encoding="utf-8"
    )

    if chunk_size > get_settings().chunk_chars:
        logger.info("原文较长，块长自动放大到 %d 字，共 %d 块", chunk_size, len(chunks))
    return book


def read_chunks(book_id: str) -> list[str]:
    path = book_dir(book_id) / "chunks.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [str(item) for item in data if str(item).strip()]


def delete_book(book_id: str) -> bool:
    task = _tasks.pop(book_id, None)
    if task and not task.done():
        task.cancel()
    deleted = db.delete_book(book_id)
    directory = book_dir(book_id)
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)
    return deleted


# ---------------------------------------------------------------------------
# 抽取调度
# ---------------------------------------------------------------------------
def read_source(book_id: str) -> str:
    path = book_dir(book_id) / "source.txt"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


async def _extract_characters(
    book_id: str,
    chunks: list[str],
    mode: str,
    api_key: str,
    trace_id: str = "",
) -> tuple[list[dict], bool, dict, dict]:
    """按模式产出角色列表。返回 (角色, 是否用了 LLM, 诊断信息, 头像候选)。"""
    settings = get_settings()
    used_llm = False
    characters: list[dict] = []
    avatars: dict[str, list[dict]] = {}
    text = read_source(book_id)
    diagnostic: dict = {"mode": mode}

    if mode in ("algo", "hybrid", "agent"):
        if text:
            mined, miner_diagnostic = await anyio.to_thread.run_sync(
                functools.partial(
                    miner.mine_characters,
                    text,
                    max_characters=settings.max_characters,
                    min_mentions=settings.miner_min_mentions,
                )
            )
            logger.info("算法挖掘：%s", miner_diagnostic)
            diagnostic.update(miner_diagnostic)
            characters = mined
            agent_log.decision(
                "算法挖掘完成",
                book_id=book_id,
                trace_id=trace_id,
                payload={
                    "candidates": len(mined),
                    "quotes_total": miner_diagnostic.get("quotes_total", 0),
                    "quotes_attributed": miner_diagnostic.get("quotes_attributed", 0),
                    "dropped": len(miner_diagnostic.get("dropped") or []),
                },
            )
            db.update_book(
                book_id,
                progress=60,
                stage=(
                    f"识别出 {len(mined)} 个角色"
                    f"（台词归属 {miner_diagnostic.get('quotes_attributed', 0)}"
                    f"/{miner_diagnostic.get('quotes_total', 0)}）"
                ),
            )

        if characters and mode == "agent" and api_key:
            from agent.extraction import ExtractionAgent

            book = db.get_book(book_id) or {}
            db.update_book(book_id, progress=68, stage="角色档案管理员审核中")
            agent = ExtractionAgent(
                book_title=book.get("title") or "",
                text=text,
                api_key=api_key,
                model=settings.extraction_model_name,
                max_steps=settings.agent_max_steps,
                enable_web=settings.agent_enable_web,
                enable_avatars=settings.agent_enable_avatars,
                base_params={
                    "min_mentions": settings.miner_min_mentions,
                    "max_characters": settings.max_characters,
                },
            )
            agent_result = await agent.run()
            characters = agent_result.get("characters") or characters
            avatars = agent_result.get("avatars") or {}
            diagnostic["agent"] = agent_result.get("report") or {}
            diagnostic["agent_trace"] = agent_result.get("trace") or []
            used_llm = True
            agent_log.decision(
                "管理员 Agent 审核完成",
                book_id=book_id,
                trace_id=trace_id,
                payload={
                    "characters": len(characters),
                    "steps": len(diagnostic["agent_trace"]),
                    "avatars": len(avatars),
                },
            )

        if characters and mode in ("hybrid", "agent") and api_key:
            # 补全只是"变得更好"，失败不该让整本书白抽。
            # enrich_characters 内部对每批都兜住了 LLM 异常，但取参考片段、
            # 写进度这类非 LLM 异常仍会冒出来——在这里整体兜一层。
            try:
                book = db.get_book(book_id) or {}
                db.update_book(book_id, progress=72, stage="模型校验与补全")
                excerpts = miner.pick_reference_excerpts(
                    chunks,
                    limit=4,
                    chars=max(200, settings.hybrid_reference_chars // 4),
                )
                characters, enrich_report = await extractor.enrich_characters(
                    characters,
                    api_key=api_key,
                    model=settings.extraction_model_name,
                    book_title=book.get("title") or "",
                    text=text,
                    reference_excerpts=excerpts,
                    quote_samples=settings.hybrid_quote_samples,
                    narrative_samples=settings.hybrid_narrative_samples,
                    batch_size=settings.hybrid_batch_size,
                )
                diagnostic["enrich"] = enrich_report
                used_llm = True
                agent_log.decision(
                    "模型校验与补全完成",
                    book_id=book_id,
                    trace_id=trace_id,
                    payload={"characters": len(characters), "report": enrich_report},
                )
            except Exception as exc:  # noqa: BLE001 - 保留算法结果
                logger.exception("模型补全失败，保留算法画像继续")
                diagnostic["enrich_error"] = str(exc)[:200]
                agent_log.decision(
                    "模型补全失败，保留算法画像",
                    book_id=book_id,
                    trace_id=trace_id,
                    severity="warn",
                    payload={"error": str(exc)[:300]},
                )

        if not characters:
            logger.warning("作品 %s 算法没挖到角色，回退到逐块抽取", book_id)
            agent_log.decision(
                "算法未挖到角色，回退逐块 LLM 抽取",
                book_id=book_id,
                trace_id=trace_id,
                severity="warn",
            )
            mode = "llm"

    if mode == "llm":
        if not api_key:
            raise extractor.ExtractionError(
                "没有识别到角色，且服务端未配置 DEEPSEEK_API_KEY"
            )

        def on_progress(done: int, total: int) -> None:
            db.update_book(
                book_id,
                progress=min(90, 10 + int(done / max(1, total) * 80)),
                stage=f"抽取角色 {done}/{total} 块",
            )

        characters = await extractor.extract_from_chunks(
            chunks,
            api_key=api_key,
            model=settings.extraction_model_name,
            on_progress=on_progress,
            per_chunk_characters=settings.characters_per_chunk,
        )
        used_llm = True
        diagnostic["mode"] = "llm"

    # 统一校验：名字必须在原文里真的出现，且不能是职位/组织/泛称
    if text:
        characters, rejected = miner.validate_characters(characters, text, min_mentions=1)
        if rejected:
            diagnostic["validate_rejected"] = [
                {"name": name, "reason": reason} for name, reason in rejected
            ]
        agent_log.decision(
            "人名逐字校验完成",
            book_id=book_id,
            trace_id=trace_id,
            payload={
                "kept": len(characters),
                "rejected": [name for name, _ in rejected][:20],
                "rejected_count": len(rejected),
            },
        )
        before = len(characters)
        characters = miner.merge_mutual_aliases(characters)
        if len(characters) != before:
            diagnostic["merged_duplicates"] = before - len(characters)
        diagnostic["characters_final"] = len(characters)

    return characters, used_llm, diagnostic, avatars


async def _sync_shelf_after_extraction(
    *,
    shelf_id: str,
    book_id: str,
    api_key: str = "",
    model: str = "",
    diagnostic: Optional[dict] = None,
) -> None:
    """抽取完成后执行书架事务：同步成员 → 给老角色补记忆 → 合并同一个人。

    以前只有 ``shelf.add_book``（手动"把书放进书架"）会做这些，
    而**上传时直接带 shelf_id** 的路径从不经过它——所以新书的同名角色一直没被合并。
    """
    import functools

    import anyio

    try:
        from services import merge as merge_mod
        from services import shelf as shelf_mod

        await anyio.to_thread.run_sync(shelf_mod.sync_members, shelf_id)
        await anyio.to_thread.run_sync(
            functools.partial(shelf_mod.supplement_memory, shelf_id, book_id)
        )

        # 判定器：名字不同但性格/关系相似时，问一次模型（有 Key 才问）
        verifier = (
            merge_mod.llm_verifier(api_key=api_key, model=model) if api_key else None
        )
        result = await anyio.to_thread.run_sync(
            functools.partial(merge_mod.merge_shelf, shelf_id, verifier=verifier)
        )
        if diagnostic is not None:
            diagnostic["shelf_sync"] = {
                "merged": len(result.get("merged") or []),
                "auto_merged": len(result.get("auto_merged") or []),
                "verified": len(result.get("verified") or []),
                "pending": len(result.get("pending") or []),
                "learned": len(result.get("learned") or []),
            }
        logger.info(
            "书架 %s 同步完成：合并 %d 个角色（高相似度自动 %d 个，模型确认 %d 个），待人工确认 %d 对",
            shelf_id,
            len(result.get("merged") or []),
            len(result.get("auto_merged") or []),
            len(result.get("verified") or []),
            len(result.get("pending") or []),
        )
    except Exception:  # noqa: BLE001 - 同步失败不能让抽取判死
        logger.exception("抽取完成后的书架同步失败（已忽略）")


async def _run_extraction(book_id: str) -> None:
    settings = get_settings()
    trace_id = agent_log.new_trace_id("extract")
    chunks = read_chunks(book_id)
    if not chunks:
        agent_log.log(
            "extraction_failed",
            book_id=book_id,
            trace_id=trace_id,
            severity="error",
            owner_type="manager",
            payload={"reason": "chunks_missing"},
        )
        db.update_book(
            book_id, status="failed", progress=100, stage="失败",
            error="原文块丢失（可能被手动删除），请删掉这本书后重新上传",
        )
        return

    mode = settings.extraction_mode_name
    api_key = settings.deepseek_api_key.strip()

    agent_log.log(
        "extraction_started",
        owner_type="manager",
        book_id=book_id,
        trace_id=trace_id,
        payload={"mode": mode, "chunks": len(chunks), "has_api_key": bool(api_key)},
    )

    if mode == "llm" and not api_key:
        agent_log.log(
            "extraction_failed",
            book_id=book_id,
            trace_id=trace_id,
            severity="error",
            owner_type="manager",
            payload={"reason": "missing_api_key", "mode": mode},
        )
        db.update_book(
            book_id, status="failed", progress=100, stage="失败",
            error="服务端未配置 DEEPSEEK_API_KEY；配置后点「重新抽取」即可",
        )
        return
    if mode == "hybrid" and not api_key:
        logger.warning("未配置 DEEPSEEK_API_KEY，hybrid 模式降级为纯算法画像")
        agent_log.decision(
            "未配置 API Key，hybrid 降级为纯算法",
            book_id=book_id,
            trace_id=trace_id,
            severity="warn",
        )

    start_stage = "纯算法识别角色" if mode == "algo" or not api_key else "识别角色"
    db.update_book(book_id, status="extracting", progress=2, stage=start_stage, error=None)
    try:
        characters, used_llm, diagnostic, avatars = await _extract_characters(
            book_id, chunks, mode, api_key, trace_id
        )

        db.update_book(book_id, progress=95, stage="整理角色档案")
        try:
            characters = extractor.attach_excerpts(
                characters,
                chunks,
                excerpt_chunks=settings.excerpt_chunks,
                excerpt_chars=settings.excerpt_chars,
            )[: settings.max_characters]
        except Exception as exc:  # noqa: BLE001 - 挑片段失败不该丢角色
            logger.exception("挑选原文片段失败，角色不带初始记忆继续")
            diagnostic["excerpt_error"] = str(exc)[:200]

        created = db.replace_novel_characters(book_id, characters)

        # 头像候选（agent 抓的）落到对应角色上，等用户挑
        avatar_applied = 0
        if avatars:
            for character in created:
                candidates = avatars.get(character["name"])
                if candidates:
                    db.update_character(character["id"], avatar_candidates=candidates)
                    avatar_applied += 1
        if avatar_applied:
            diagnostic["avatars_applied"] = avatar_applied

        # 重抽后向量必须跟着更新：原文片段被换掉了，旧 chunk 还指着老内容。
        # 单个角色失败不该让整本书失败，所以逐个 try。
        indexed = 0
        try:
            from services import rag

            for character in created:
                try:
                    rag.index_character(character["id"])
                    indexed += 1
                except Exception:  # noqa: BLE001
                    logger.warning("重建 %s 的索引失败（已跳过）", character["id"])
            diagnostic["reindexed"] = indexed
        except Exception:  # noqa: BLE001 - RAG 不可用时不影响抽取结果
            logger.exception("重抽后重建索引失败（已忽略）")

        # 抽取完成 = 这本书真正"进了书架"。必须跑一遍书架事务，否则：
        #   * 新角色不进群（要等前端调 members 接口才被动同步）
        #   * 老角色拿不到这本书里关于自己的片段
        #   * **同一个人不会被合并**（以前只有手动"加书"接口才会触发）
        shelf_id = str((db.get_book(book_id) or {}).get("shelf_id") or "")
        if shelf_id:
            await _sync_shelf_after_extraction(
                shelf_id=shelf_id,
                book_id=book_id,
                api_key=api_key,
                model=settings.extraction_model_name,
                diagnostic=diagnostic,
            )

        final_stage = "完成"
        if not used_llm:
            final_stage = "完成（纯算法画像，未使用模型）"
        diagnostic["characters_saved"] = len(characters)
        db.update_book(
            book_id,
            status="ready",
            progress=100,
            stage=final_stage,
            error=None,
            diagnostic=json.dumps(diagnostic, ensure_ascii=False)[:8000],
        )
        logger.info(
            "作品 %s 抽取完成：%d 个角色（%s 模式）", book_id, len(characters), mode
        )
        agent_log.log(
            "extraction_finished",
            owner_type="manager",
            book_id=book_id,
            trace_id=trace_id,
            payload={
                "characters": len(characters),
                "mode": mode,
                "used_llm": used_llm,
                "avatars_applied": avatar_applied,
            },
        )
    except asyncio.CancelledError:
        agent_log.log(
            "extraction_cancelled",
            owner_type="manager",
            book_id=book_id,
            trace_id=trace_id,
            severity="warn",
        )
        db.update_book(book_id, status="failed", progress=0, stage="已取消", error="抽取已取消")
        raise
    except Exception as exc:  # noqa: BLE001 - 统一落到 book.error 给前端看
        logger.exception("作品 %s 抽取失败", book_id)
        agent_log.log(
            "extraction_failed",
            book_id=book_id,
            trace_id=trace_id,
            severity="error",
            owner_type="manager",
            payload={"error": str(exc)[:500]},
        )
        db.update_book(
            book_id, status="failed", progress=100, stage="失败", error=str(exc)[:500]
        )


def schedule_extraction(book_id: str) -> None:
    existing = _tasks.get(book_id)
    if existing and not existing.done():
        return
    task = asyncio.create_task(_run_extraction(book_id))
    _tasks[book_id] = task
    task.add_done_callback(lambda _t, bid=book_id: _tasks.pop(bid, None))


def retry_extraction(book_id: str) -> None:
    db.update_book(book_id, status="extracting", progress=0, stage="排队中", error=None)
    schedule_extraction(book_id)


def extraction_running(book_id: str) -> bool:
    task = _tasks.get(book_id)
    return bool(task and not task.done())


def delete_task(book_id: str) -> None:
    """取消并移除该书的后台抽取任务（关服/删书时用）。"""
    task = _tasks.pop(book_id, None)
    if task and not task.done():
        task.cancel()


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
def create_character(
    book_id: str,
    *,
    name: str,
    role: str = "",
    personality: Optional[list[str]] = None,
    speech_style: str = "",
    background: str = "",
    avatar: str = "",
    is_user_persona: bool = False,
) -> dict:
    if not db.get_book(book_id):
        raise KeyError("作品不存在")

    name = (name or "").strip()
    if not name:
        raise ValueError("角色名不能为空")

    return db.insert_character(
        book_id=book_id,
        name=name[:32],
        role=(role or "").strip()[:60],
        personality=[str(p)[:40] for p in (personality or []) if str(p).strip()][:8],
        speech_style=(speech_style or "").strip()[:300],
        background=(background or "").strip()[:1500],
        avatar=avatar or pick_avatar(name),
        source="user",
        is_user_persona=is_user_persona,
        sort_order=-1,
        appearances=0,
    )
