"""RAG 记忆库：给角色记忆建索引，按"场景 + 语义"检索相关片段。

## 库在哪

不是另一套存储，而是 ``character_memory_items`` 的三列扩展：

```
content  原始文字
scene    场景标签（战斗/雨夜/校园/对话…）
tokens   BM25 用的切词结果（core/text.tokenize）
vector   bge-small-zh 的 512 维向量（float32 字节）
```

一条记忆写入时就把该算的都算好，**检索时只嵌入用户这一句话**，所以每轮的开销是一次
小模型前向（实测 10~20ms，见 ``stats()``）。向量在数据库里躺着，检索是纯 numpy 点积，
几百条记忆也就零点几毫秒。

## 混合打分

```
score = 0.65 * 余弦相似度 + 0.35 * BM25
        然后 ×1.30（场景一致） ×0.85（场景不同） ×1.05（摘要类）
```

为什么不全押向量：角色记忆里大量专有名词（人名、地名），词法命中更稳；而向量能补上
"说法不同但意思相关"的情况。两者混着用，比单独任何一个都稳。

## 降级

模型目录不存在、torch 没装、或者任何异常 -> 自动退化成纯 BM25。**聊天不能因为 RAG 挂掉**。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from core import db
from core.text import tokenize
from services import scene as scene_mod

logger = logging.getLogger(__name__)

#: 一块的字符数。原文片段普遍 800~1200 字，整段编码只能看到开头，
#: 中段信息全丢——按 220 字切块后，检索精度提升明显。
CHUNK_CHARS = 220
CHUNK_OVERLAP = 40

#: 记忆向量维度（bge-small-zh-v1.5 = 512）
EMBEDDING_DIM = 512

#: 混合权重
WEIGHT_VECTOR = 0.65
WEIGHT_LEXICAL = 0.35

#: 场景一致/不一致的乘数
SCENE_BOOST = 1.30
SCENE_PENALTY = 0.85

#: 摘要类记忆略加分：它本身就是压过的精华
SUMMARY_BOOST = 1.05

#: 默认返回条数
DEFAULT_TOP_K = 4

#: 低于这个分数的不进提示词（避免塞废话）
MIN_SCORE = 0.18

_model_lock = threading.Lock()
_embedder_state: dict[str, Any] = {"embedder": None, "error": None, "tried": False}

_query_cache: dict[str, list[float]] = {}
_QUERY_CACHE_LIMIT = 256

_stats = {"queries": 0, "embedding_ms": 0.0, "last_backend": "none"}


def model_path() -> Path:
    from core.config import get_settings

    configured = (get_settings().rag_model_path or "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "data" / "models" / "bge-small-zh-v1.5"


class LocalEmbedder:
    """bge-small-zh 的极简封装：只做编码，不引入 sentence-transformers。"""

    def __init__(self, path: Path, *, max_length: int = 128, threads: int = 0) -> None:
        import torch

        from core.hf_import import auto_classes

        # 走安全导入：并发的首次导入会拿到半初始化的 transformers（见 core/hf_import.py）
        AutoModel, AutoTokenizer = auto_classes()

        self._torch = torch
        if threads > 0:
            torch.set_num_threads(threads)
        self.tokenizer = AutoTokenizer.from_pretrained(str(path))
        self.model = AutoModel.from_pretrained(str(path))
        self.model.eval()
        self.max_length = max_length

    def encode(self, texts: list[str]) -> list[list[float]]:
        """平均池化 + L2 归一化。返回 python list（方便存库/缓存）。"""
        torch = self._torch
        with torch.no_grad():
            batch = self.tokenizer(
                texts,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            output = self.model(**batch)
            hidden = output.last_hidden_state
            mask = batch["attention_mask"].unsqueeze(-1).to(hidden.dtype)
            summed = (hidden * mask).sum(dim=1)
            counts = mask.sum(dim=1).clamp(min=1e-6)
            pooled = summed / counts
            normalized = torch.nn.functional.normalize(pooled, p=2, dim=1)
        return normalized.cpu().tolist()


def _load_embedder() -> Optional[LocalEmbedder]:
    """惰性加载模型；只尝试一次，失败后永远走词法降级。"""
    with _model_lock:
        if _embedder_state["tried"]:
            return _embedder_state["embedder"]
        _embedder_state["tried"] = True
        from core.config import get_settings

        settings = get_settings()
        if settings.rag_backend == "lexical":
            _embedder_state["error"] = "配置为词法模式"
            return None

        path = model_path()
        if not path.exists():
            _embedder_state["error"] = f"模型目录不存在：{path}"
            logger.warning("RAG 向量后端不可用（%s），降级为词法检索", _embedder_state["error"])
            return None
        try:
            started = time.perf_counter()
            embedder = LocalEmbedder(
                path,
                max_length=int(settings.rag_max_length or 128),
                threads=int(settings.rag_threads or 0),
            )
            logger.info("RAG 向量后端就绪（加载耗时 %.0f ms）", (time.perf_counter() - started) * 1000)
            _embedder_state["embedder"] = embedder
            return embedder
        except Exception as exc:  # noqa: BLE001 - 模型问题不能影响聊天
            _embedder_state["error"] = str(exc)[:200]
            logger.warning("RAG 向量后端加载失败（%s），降级为词法检索", _embedder_state["error"])
            return None


def backend() -> str:
    """当前实际使用的后端：``vector`` / ``lexical``。"""
    return "vector" if _embedder_state["embedder"] is not None else "lexical"


def retry_embedder() -> Optional["LocalEmbedder"]:
    """清掉「只尝试一次」的标记后重新加载。

    启动预热用：向量后端若因并发导入之类的偶发原因失败，
    :func:`_load_embedder` 会把失败永久缓存住，整个进程就一直是词法检索。
    这里允许显式重试一次，避免"悄悄降级"一直持续到下次重启。
    """
    if _embedder_state["embedder"] is None:
        _embedder_state["tried"] = False
    return _load_embedder()


def backend_status() -> dict:
    _load_embedder()
    return {
        "backend": backend(),
        "model_path": str(model_path()),
        "model_exists": model_path().exists(),
        "error": _embedder_state["error"],
        "stats": dict(_stats),
    }


def status_light() -> dict:
    """不触发模型加载的状态查询（给 /api/health 用，别让健康检查卡 7 秒）。"""
    return {
        "backend": backend(),
        "ready": _embedder_state["embedder"] is not None,
        "model_path": str(model_path()),
        "model_exists": model_path().exists(),
        "error": _embedder_state["error"],
        "stats": dict(_stats),
    }


def _embed(texts: list[str]) -> Optional[list[list[float]]]:
    embedder = _load_embedder()
    if embedder is None or not texts:
        return None
    try:
        return embedder.encode(texts)
    except Exception:  # noqa: BLE001
        logger.exception("向量编码失败，本次降级为词法检索")
        return None


# ---------------------------------------------------------------------------
# 写索引
# ---------------------------------------------------------------------------
def split_chunks(
    text: str, *, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """把长文本切成带重叠的小块，尽量在句末断句。"""
    content = " ".join(str(text or "").split())
    if not content:
        return []
    if len(content) <= size:
        return [content]

    chunks: list[str] = []
    start = 0
    while start < len(content):
        end = min(len(content), start + size)
        window = content[start:end]
        if end < len(content):
            for mark in ("。", "！", "？", "；", "，", " "):
                position = window.rfind(mark)
                if position >= size // 2:
                    end = start + position + 1
                    break
        chunks.append(content[start:end].strip())
        if end >= len(content):
            break
        start = max(end - overlap, start + 1)
    return [chunk for chunk in chunks if chunk]


def index_character(character_id: str, *, force: bool = False) -> int:
    """给该角色还没有块向量的记忆建索引。返回处理的记忆条数。"""
    items = db.items_without_chunks(character_id, force=force)
    if not items:
        return 0

    payload: list[tuple[str, list[str]]] = []
    flat: list[str] = []
    for item in items:
        chunks = split_chunks(item.get("content") or "")
        if not chunks:
            continue
        payload.append((item["id"], chunks))
        flat.extend(chunks)

    vectors = _embed(flat)
    if vectors is None:
        return 0

    cursor = 0
    for item_id, chunks in payload:
        piece = vectors[cursor : cursor + len(chunks)]
        cursor += len(chunks)
        db.replace_memory_chunks(
            item_id, character_id, list(zip(chunks, [_pack(vector) for vector in piece]))
        )
        # 整段向量取块均值，给"没有块"的兜底路径和调试用
        import numpy as np

        mean = np.asarray(piece, dtype="float32").mean(axis=0)
        norm = float(np.linalg.norm(mean)) or 1.0
        db.update_memory_vector(item_id, _pack((mean / norm).tolist()))
    return len(payload)


def index_all(*, force: bool = False) -> dict:
    """给所有角色建索引（首次启用或换模型后跑一次）。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT character_id FROM character_memory_items"
        ).fetchall()
    total = 0
    for row in rows:
        total += index_character(row["character_id"], force=force)
    with db.connect() as conn:
        chunks = conn.execute("SELECT COUNT(*) AS n FROM memory_chunks").fetchone()["n"]
    return {
        "characters": len(rows),
        "vectors": total,
        "chunks": int(chunks or 0),
        "backend": backend(),
    }


def _pack(vector: list[float]) -> bytes:
    import numpy as np

    return np.asarray(vector, dtype="float32").tobytes()


def _unpack(raw: Any):
    import numpy as np

    if not raw:
        return None
    try:
        array = np.frombuffer(raw, dtype="float32")
    except (TypeError, ValueError):
        return None
    if array.size != EMBEDDING_DIM:
        return None
    return array


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def _bm25(rows: list[dict], query_tokens: list[str]) -> list[float]:
    """标准 BM25，语料就是该角色的全部记忆。"""
    if not query_tokens:
        return [0.0] * len(rows)

    docs = [str(row.get("tokens") or "").split() for row in rows]
    lengths = [max(1, len(doc)) for doc in docs]
    average = sum(lengths) / max(1, len(lengths))
    total = len(docs)
    k1, b = 1.2, 0.75

    document_frequency: dict[str, int] = {}
    for doc in docs:
        for token in set(doc):
            document_frequency[token] = document_frequency.get(token, 0) + 1

    import math

    scores: list[float] = []
    for doc, length in zip(docs, lengths):
        counts: dict[str, int] = {}
        for token in doc:
            counts[token] = counts.get(token, 0) + 1
        score = 0.0
        for token in query_tokens:
            frequency = counts.get(token, 0)
            if not frequency:
                continue
            df = document_frequency.get(token, 0)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            score += idf * frequency * (k1 + 1) / (
                frequency + k1 * (1 - b + b * length / average)
            )
        scores.append(score)
    return scores


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return values
    peak = max(values)
    if peak <= 0:
        return [0.0] * len(values)
    return [value / peak for value in values]


def _query_vector(query: str) -> Optional[list[float]]:
    cached = _query_cache.get(query)
    if cached is not None:
        return cached
    started = time.perf_counter()
    vectors = _embed([query])
    elapsed = (time.perf_counter() - started) * 1000
    if vectors is None:
        return None
    _stats["queries"] += 1
    _stats["embedding_ms"] = elapsed
    _stats["last_backend"] = "vector"
    if len(_query_cache) >= _QUERY_CACHE_LIMIT:
        _query_cache.pop(next(iter(_query_cache)))
    _query_cache[query] = vectors[0]
    return vectors[0]


def search(
    character_id: str,
    query: str,
    *,
    scene: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = MIN_SCORE,
) -> list[dict]:
    """检索该角色与 ``query`` 最相关的记忆。"""
    rows = db.list_memory_items(character_id)
    if not rows or not str(query or "").strip():
        return []

    current_scene = scene if scene is not None else scene_mod.detect(query)

    lexical = _normalize(_bm25(rows, tokenize(query)))

    vector_scores: Optional[list[float]] = None
    query_vector = _query_vector(query)
    if query_vector is not None:
        import numpy as np

        # 优先用块级向量：一条记忆取"最像的那一块"的分数
        best: dict[str, float] = {}
        chunks = db.list_memory_chunks(character_id)
        if chunks:
            matrix = []
            owners = []
            for chunk in chunks:
                unpacked = _unpack(chunk.get("vector"))
                if unpacked is None:
                    continue
                matrix.append(unpacked)
                owners.append(chunk["item_id"])
            if matrix:
                scores = np.asarray(matrix) @ np.asarray(query_vector, dtype="float32")
                for position, owner in enumerate(owners):
                    value = float(max(0.0, min(1.0, scores[position])))
                    if value > best.get(owner, 0.0):
                        best[owner] = value

        # 没有块的老条目退回整段向量
        fallback_matrix = []
        fallback_owners = []
        for row in rows:
            if row["id"] in best:
                continue
            unpacked = _unpack(row.get("vector"))
            if unpacked is None:
                continue
            fallback_matrix.append(unpacked)
            fallback_owners.append(row["id"])
        if fallback_matrix:
            scores = np.asarray(fallback_matrix) @ np.asarray(query_vector, dtype="float32")
            for position, owner in enumerate(fallback_owners):
                best[owner] = float(max(0.0, min(1.0, scores[position])))

        if best:
            vector_scores = [best.get(row["id"], 0.0) for row in rows]

    results: list[dict] = []
    for index, row in enumerate(rows):
        if vector_scores is not None:
            score = WEIGHT_VECTOR * vector_scores[index] + WEIGHT_LEXICAL * lexical[index]
        else:
            _stats["last_backend"] = "lexical"
            score = lexical[index]

        row_scene = str(row.get("scene") or "")
        if row_scene and current_scene:
            score *= SCENE_BOOST if row_scene == current_scene else SCENE_PENALTY
        if row.get("kind") == "summary":
            score *= SUMMARY_BOOST

        if score < min_score:
            continue
        results.append(
            {
                "id": row["id"],
                "kind": row.get("kind"),
                "scene": row_scene,
                "content": row.get("content") or "",
                "score": round(float(score), 4),
                "source_book_id": row.get("source_book_id"),
            }
        )

    results.sort(key=lambda item: item["score"], reverse=True)
    return results[: max(1, int(top_k or DEFAULT_TOP_K))]


def recall_block(
    character_id: str,
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    scene: Optional[str] = None,
) -> str:
    """给提示词用的【相关记忆】块。检索不到就返回空串。"""
    try:
        hits = search(character_id, query, scene=scene, top_k=top_k)
    except Exception:  # noqa: BLE001 - RAG 失败不能影响回复
        logger.exception("记忆检索失败（已忽略）")
        return ""
    if not hits:
        return ""
    lines = []
    for hit in hits:
        tag = f"（{hit['scene']}）" if hit["scene"] else ""
        lines.append(f"- {tag}{hit['content'][:220]}")
    return "【与你这句话相关的记忆】\n" + "\n".join(lines)


def stats() -> dict:
    return dict(_stats)