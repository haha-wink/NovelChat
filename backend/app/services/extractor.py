"""角色抽取流水线（纯逻辑，不碰数据库/文件）。

流程：分块 -> 逐块让 LLM 输出 JSON 角色档案 -> 按名字/别名合并去重
     -> 按出场次数排序 -> 为每个角色截取原文片段作为「初始记忆」。
LLM 调用通过 :data:`call_llm_json` 注入，便于测试时替换成假实现。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

from core.config import get_settings
from agent.prompts import (
    build_enrichment_messages,
    build_extraction_messages,
    pick_avatar,
)

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"

#: (messages) -> 解析后的 JSON 字典
LLMJsonCaller = Callable[[list[dict[str, str]]], Awaitable[dict[str, Any]]]

_NAME_NOISE = re.compile(r"[\s·・.。\"'“”‘’（）()\[\]【】《》,，、:：;；!！?？]")


class ExtractionError(RuntimeError):
    """抽取失败（模型返回不可用 / 网络错误）。"""


def normalise_name(name: str) -> str:
    return _NAME_NOISE.sub("", (name or "").strip()).lower()


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------
def chunk_text(text: str, *, chunk_chars: int, overlap: int) -> list[str]:
    """按段落边界切分，尽量不切断句子；相邻块保留 overlap 字符的重叠。

    保证：每个块的长度都不超过 ``chunk_chars``。
    """
    text = (text or "").strip()
    if not text:
        return []
    chunk_chars = max(500, chunk_chars)
    overlap = max(0, min(overlap, chunk_chars // 2))

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for paragraph in paragraphs:
        # 段落本身比一块还长：按块长硬切，块间保留重叠
        while len(paragraph) > chunk_chars:
            flush()
            chunks.append(paragraph[:chunk_chars].strip())
            paragraph = paragraph[chunk_chars - overlap:] if overlap else paragraph[chunk_chars:]

        if not paragraph:
            continue

        if not current:
            current = paragraph
        elif len(current) + len(paragraph) + 2 <= chunk_chars:
            current = f"{current}\n\n{paragraph}"
        else:
            trailing = current[-overlap:] if overlap else ""
            flush()
            # 重叠尾巴只在装得下的时候才带，保证不超长
            if trailing and len(trailing) + len(paragraph) + 2 <= chunk_chars:
                current = f"{trailing}\n\n{paragraph}"
            else:
                current = paragraph

    flush()
    return chunks


def plan_chunks(text: str) -> tuple[list[str], int]:
    """按配置分块；块数超过上限时自动加大块长，保证调用次数可控。"""
    settings = get_settings()
    size = settings.chunk_chars
    overlap = settings.chunk_overlap

    chunks = chunk_text(text, chunk_chars=size, overlap=overlap)
    if len(chunks) <= settings.max_chunks:
        return chunks, size

    # 用「总字数 / 上限」反推块长，再留 10% 余量给段落边界
    needed = int(len(text) / max(1, settings.max_chunks) * 1.1)
    size = max(settings.chunk_chars, needed)
    chunks = chunk_text(text, chunk_chars=size, overlap=overlap)
    return chunks[: settings.max_chunks], size


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def _merge_relationships(target: list[dict], incoming: Iterable[Any]) -> None:
    seen = {(normalise_name(str(r.get("with"))), str(r.get("relation"))) for r in target if isinstance(r, dict)}
    for item in incoming or []:
        if not isinstance(item, dict):
            continue
        other = str(item.get("with") or "").strip()
        if not other:
            continue
        relation = str(item.get("relation") or "").strip()
        key = (normalise_name(other), relation)
        if key in seen:
            continue
        seen.add(key)
        target.append({"with": other, "relation": relation})


def _extend_unique(target: list[str], values: Iterable[Any], *, limit: int = 8) -> None:
    existing = {str(v).strip() for v in target}
    for value in values or []:
        text = str(value).strip()
        if not text or text in existing:
            continue
        target.append(text)
        existing.add(text)
        if len(target) >= limit:
            return


def _prefer_longer(current: str, candidate: Any) -> str:
    new = str(candidate or "").strip()
    return new if len(new) > len(current or "") else (current or "")


def merge_characters(records: Sequence[Sequence[dict]]) -> list[dict]:
    """把逐块抽取的结果合并成角色列表（同名/别名归并）。"""
    merged: list[dict] = []
    index: dict[str, dict] = {}

    for chunk_characters in records:
        for raw in chunk_characters or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue

            aliases = [str(a).strip() for a in (raw.get("aliases") or []) if str(a).strip()]
            keys = {k for k in (normalise_name(name), *(normalise_name(a) for a in aliases)) if k}

            target = next((index[k] for k in keys if k in index), None)
            if target is None:
                target = {
                    "name": name,
                    "aliases": [],
                    "role": "",
                    "personality": [],
                    "speech_style": "",
                    "background": "",
                    "relationships": [],
                    "quotes": [],
                    "appearances": 0,
                }
                merged.append(target)

            target["appearances"] += 1
            for alias in [name, *aliases]:
                if alias and normalise_name(alias) != normalise_name(target["name"]):
                    if alias not in target["aliases"]:
                        target["aliases"].append(alias)

            if not target["role"]:
                target["role"] = str(raw.get("role") or "").strip()
            target["speech_style"] = _prefer_longer(target["speech_style"], raw.get("speech_style"))
            target["background"] = _prefer_longer(target["background"], raw.get("background"))
            _extend_unique(target["personality"], raw.get("personality"), limit=6)
            _merge_relationships(target["relationships"], raw.get("relationships"))
            _extend_unique(target["quotes"], raw.get("quotes"), limit=4)

            for key in keys:
                index.setdefault(key, target)

    for character in merged:
        for alias in character["aliases"]:
            key = normalise_name(alias)
            if key:
                index.setdefault(key, character)
        character["aliases"] = character["aliases"][:6]

    merged.sort(key=lambda c: (-c["appearances"], c["name"]))
    return merged


# ---------------------------------------------------------------------------
# 原文片段（初始记忆）
# ---------------------------------------------------------------------------
def attach_excerpts(
    characters: list[dict],
    chunks: Sequence[str],
    *,
    excerpt_chunks: int,
    excerpt_chars: int,
) -> list[dict]:
    """给每个角色挑出出现次数最多的原文片段，作为「初始记忆」。"""
    lowered = [chunk.lower() for chunk in chunks]

    for character in characters:
        needles = [n for n in (normalise_name(character["name"]),
                               *(normalise_name(a) for a in character["aliases"])) if n]
        scored: list[tuple[int, int]] = []
        for index, text in enumerate(lowered):
            hits = sum(text.count(needle) for needle in needles)
            if hits:
                scored.append((hits, index))
        scored.sort(key=lambda item: (-item[0], item[1]))

        excerpts: list[str] = []
        for _, index in scored[:excerpt_chunks]:
            original = chunks[index]
            # 以首次出现位置为中心截一段上下文
            position = min(
                (original.lower().find(needle) for needle in needles if original.lower().find(needle) >= 0),
                default=0,
            )
            start = max(0, position - excerpt_chars // 3)
            snippet = original[start : start + excerpt_chars].strip()
            if snippet:
                excerpts.append(snippet)

        character["excerpts"] = excerpts
        character["avatar"] = pick_avatar(character["name"])

    return characters


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------
async def call_llm_json(
    messages: list[dict[str, str]],
    *,
    api_key: str,
    model: str,
    timeout: float = 120.0,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """调用 DeepSeek 的 JSON 输出模式。"""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=timeout)
    response = await client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        temperature=0.2,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    content = (response.choices[0].message.content or "").strip()
    if not content:
        raise ExtractionError("模型返回了空内容")
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content).strip()
    finish_reason = getattr(response.choices[0], "finish_reason", None)
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        hint = "（输出被 max_tokens 截断，请调小 HYBRID_BATCH_SIZE）" if finish_reason == "length" else ""
        raise ExtractionError(f"模型返回的不是合法 JSON{hint}：{content[:120]}") from exc
    if not isinstance(payload, dict):
        raise ExtractionError("模型返回的 JSON 结构不对")
    return payload


async def extract_from_chunks(
    chunks: Sequence[str],
    *,
    api_key: str,
    model: str,
    on_progress: Optional[Callable[[int, int], None]] = None,
    per_chunk_characters: int = 6,
    caller: Optional[LLMJsonCaller] = None,
) -> list[dict]:
    """逐块抽取并合并。on_progress(已完成块数, 总块数)。"""
    caller = caller or (
        lambda messages: call_llm_json(messages, api_key=api_key, model=model)  # noqa: E501
    )

    total = len(chunks)
    records: list[list[dict]] = []
    failures = 0

    for position, chunk in enumerate(chunks, start=1):
        messages = build_extraction_messages(
            chunk, index=position, total=total, max_characters=per_chunk_characters
        )
        try:
            payload = await caller(messages)
            characters = payload.get("characters") or []
            if isinstance(characters, list):
                records.append([c for c in characters if isinstance(c, dict)])
        except Exception as exc:  # noqa: BLE001 - 单块失败不放弃整本
            failures += 1
            logger.warning("第 %d/%d 块抽取失败：%s", position, total, exc)
            if failures >= 3 and not records:
                raise ExtractionError(f"连续 {failures} 块抽取失败，请检查 API Key 或稍后重试") from exc

        if on_progress:
            on_progress(position, total)

    if not records:
        raise ExtractionError("没有从原文中抽取到任何角色，请确认文档是小说正文")

    return merge_characters(records)


async def enrich_characters(
    mined: Sequence[dict],
    *,
    api_key: str,
    model: str,
    book_title: str,
    text: str = "",
    reference_excerpts: Optional[Sequence[str]] = None,
    quote_samples: int = 6,
    narrative_samples: int = 2,
    min_new_mentions: int = 2,
    batch_size: int = 10,
    caller: Optional[LLMJsonCaller] = None,
) -> tuple[list[dict], dict]:
    """hybrid 模式：算法骨架 + LLM 调用做「校验 / 规范化 / 补漏」。

    为什么分批：一次让模型输出 20+ 个角色的完整档案会超过 max_tokens 而被截断
    （截断后 JSON 不合法，整批补全就白做了）。按 batch_size 分批，每批
    输出可控，某批失败也只影响那一批的角色（保留算法画像）。

    三道防幻觉闸：
    1. 模型判定 ``is_person=false`` 的候选直接丢弃（职位、地名、组织名）；
    2. ``missing`` 里补出来的新角色，名字必须**逐字出现在原文里**且出现 >= min_new_mentions 次；
    3. 最后统一过 :func:`character_miner.validate_characters`（在 library 里调用）。
    """
    from services.character_miner import clean_alias, is_identity_like

    report: dict = {"dropped": [], "renamed": [], "added": [], "enriched": 0, "batches": 0, "errors": []}
    if not mined:
        return [], report

    caller = caller or (
        lambda messages: call_llm_json(  # noqa: E501
            messages, api_key=api_key, model=model, max_tokens=8192
        )
    )

    batch_size = max(1, batch_size)
    batches = [list(mined[i:i + batch_size]) for i in range(0, len(mined), batch_size)]

    returned: list = []
    missing: list = []
    for index, batch in enumerate(batches, start=1):
        messages = build_enrichment_messages(
            batch,
            book_title=book_title,
            quote_samples=quote_samples,
            narrative_samples=narrative_samples,
            reference_excerpts=reference_excerpts,
        )
        try:
            payload = await caller(messages)
        except Exception as exc:  # noqa: BLE001 - 单批失败不该让整本书失败
            logger.warning("第 %d/%d 批角色补全失败，保留算法画像：%s", index, len(batches), exc)
            report["errors"].append(f"批 {index}: {str(exc)[:120]}")
            continue

        report["batches"] += 1
        batch_characters = payload.get("characters") if isinstance(payload, dict) else None
        if isinstance(batch_characters, list):
            returned.extend(item for item in batch_characters if isinstance(item, dict))
        batch_missing = payload.get("missing") if isinstance(payload, dict) else None
        if isinstance(batch_missing, list):
            missing.extend(batch_missing)

    if not returned:
        logger.warning("补全结果里没有 characters，回退为算法画像")
        return [dict(item) for item in mined], report

    by_name: dict[str, dict] = {}
    for item in returned:
        key = normalise_name(str(item.get("name") or ""))
        if key:
            by_name[key] = item

    merged: list[dict] = []
    for record in mined:
        item = by_name.get(normalise_name(record["name"]))
        if item is None:
            merged.append(dict(record))
            continue

        # 闸 1：模型判定不是人物 -> 丢弃
        if item.get("is_person") is False:
            report["dropped"].append({"name": record["name"], "reason": "模型判定不是人物"})
            continue

        result = dict(record)

        # 规范化真名：只接受"原文里真的出现过"的名字
        canonical = str(item.get("canonical_name") or "").strip()
        if canonical and canonical != record["name"] and (not text or canonical in text):
            report["renamed"].append({"from": record["name"], "to": canonical})
            result["aliases"] = list(
                dict.fromkeys([record["name"], *list(record.get("aliases") or [])])
            )
            result["name"] = canonical[:32]

        for field in ("role", "speech_style", "background"):
            value = str(item.get(field) or "").strip()
            if value:
                result[field] = value[:1500]

        personality = [str(p).strip() for p in (item.get("personality") or []) if str(p).strip()]
        if personality:
            result["personality"] = personality[:6]

        relationships = [
            {"with": str(r.get("with")).strip(), "relation": str(r.get("relation") or "").strip()}
            for r in (item.get("relationships") or [])
            if isinstance(r, dict) and str(r.get("with") or "").strip()
        ]
        if relationships:
            result["relationships"] = relationships[:6]

        aliases = [clean_alias(a) for a in (item.get("aliases") or [])]
        aliases = [alias for alias in aliases if alias]
        if aliases:
            result["aliases"] = list(
                dict.fromkeys(list(result.get("aliases") or []) + aliases)
            )[:6]

        result["enriched"] = True
        report["enriched"] += 1
        merged.append(result)

    # 闸 2：补漏，名字必须真的在原文里
    known = {normalise_name(c["name"]) for c in merged}
    known |= {normalise_name(a) for c in merged for a in (c.get("aliases") or [])}
    if isinstance(missing, list):
        for raw in missing:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if len(name) < 2 or len(name) > 4:
                continue
            if normalise_name(name) in known or is_identity_like(name):
                continue
            if text:
                mentions = text.count(name)
                if mentions < min_new_mentions:
                    continue
            else:
                mentions = 0
            known.add(normalise_name(name))
            report["added"].append(name)
            merged.append(
                {
                    "name": name,
                    "aliases": [],
                    "role": "",
                    "personality": [],
                    "speech_style": "",
                    "background": str(raw.get("evidence") or "").strip()[:200],
                    "relationships": [],
                    "quotes": [],
                    "narration": [],
                    "excerpts": [],
                    "appearances": mentions,
                    "added_by_model": True,
                }
            )

    # 合并规范化后重名的记录
    deduped: dict[str, dict] = {}
    for character in merged:
        key = normalise_name(character["name"])
        if key in deduped:
            target = deduped[key]
            target["quotes"] = list(dict.fromkeys([*target.get("quotes", []), *character.get("quotes", [])]))[:8]
            target["aliases"] = list(
                dict.fromkeys([*target.get("aliases", []), *character.get("aliases", []), character["name"]])
            )[:6]
            target["appearances"] = max(
                int(target.get("appearances") or 0), int(character.get("appearances") or 0)
            )
            continue
        deduped[key] = character

    result_list = list(deduped.values())
    # 别名里去掉本名自身，避免「陈墨瞳 → 别名：诺诺、陈墨瞳」这种重复
    for character in result_list:
        aliases = [a for a in (character.get("aliases") or []) if a and a != character["name"]]
        character["aliases"] = list(dict.fromkeys(aliases))[:6]

    logger.info(
        "角色档案补全：%d/%d 由模型补充，丢弃 %d，改名 %d，新增 %d",
        report["enriched"], len(result_list), len(report["dropped"]),
        len(report["renamed"]), len(report["added"]),
    )
    return result_list, report
