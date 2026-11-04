"""联网检索：搜索 → 取正文 → 从正文里提炼角色名单。

用于 agent 的「先查书名，再拿真实角色表来校验算法结果」这一环。

所有网络函数都可以注入（测试时用假实现），并且：
* 抽取出的名字必须**逐字出现在抓到的正文里**，否则丢弃（防幻觉）；
* 每个名字标注来源 URL，方便用户判断可信度。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ROSTER_QUERIES: tuple[str, ...] = ("{title} 角色列表", "{title} 人物介绍", "{title} 主要角色")

_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES = re.compile(r"\n{2,}")


def html_to_text(html: str, *, max_chars: int = 8000) -> str:
    text = _SCRIPT_STYLE.sub(" ", html or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", text, flags=re.I)
    text = _TAGS.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    text = _SPACES.sub(" ", text)
    text = _BLANK_LINES.sub("\n", text)
    return text.strip()[:max_chars]


async def default_search(query: str, *, max_results: int = 5) -> list[dict[str, str]]:
    """DuckDuckGo 搜索（ddgs 是同步库，放线程里跑）。"""
    import anyio

    def _run() -> list[dict[str, str]]:
        from ddgs import DDGS

        results: list[dict[str, str]] = []
        with DDGS() as client:
            for item in client.text(query, max_results=max_results):
                results.append(
                    {
                        "title": str(item.get("title") or ""),
                        "url": str(item.get("href") or item.get("url") or ""),
                        "snippet": str(item.get("body") or item.get("snippet") or ""),
                    }
                )
        return results

    try:
        return await anyio.to_thread.run_sync(_run)
    except Exception as exc:  # noqa: BLE001 - 搜索失败降级为空
        logger.warning("搜索失败：%s", exc)
        return []


async def default_fetch(url: str, *, timeout: float = 12.0, max_chars: int = 8000) -> str:
    import httpx

    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            return ""
        return html_to_text(response.text, max_chars=max_chars)


async def roster_from_web(
    book_title: str,
    *,
    api_key: str = "",
    model: str = "",
    extra_query: str = "",
    max_pages: int = 2,
    max_chars: int = 7000,
    search: Optional[Callable[[str, int], Awaitable[list[dict[str, str]]]]] = None,
    fetch: Optional[Callable[[str], Awaitable[str]]] = None,
    caller: Optional[Callable[[list[dict[str, str]]], Awaitable[dict[str, Any]]]] = None,
) -> dict:
    """搜索书名 → 抓正文 → 让模型列出角色名（并用正文做逐字校验）。"""
    search = search or (lambda query, limit: default_search(query, max_results=limit))
    fetch = fetch or default_fetch

    queries = ([extra_query] if extra_query else []) + [
        template.format(title=book_title) for template in ROSTER_QUERIES
    ]

    hits: list[dict[str, str]] = []
    for query in queries:
        for item in await search(query, 5):
            if item.get("url") and item not in hits:
                hits.append(item)
        if len(hits) >= 6:
            break

    pages: list[dict[str, str]] = []
    for hit in hits[:max_pages]:
        try:
            text = await fetch(hit["url"])
        except Exception as exc:  # noqa: BLE001
            logger.info("抓取失败 %s：%s", hit.get("url"), exc)
            continue
        if text and len(text) >= 200:
            pages.append({"url": hit.get("url", ""), "title": hit.get("title", ""), "text": text})
        if len(pages) >= max_pages:
            break

    if not pages:
        return {"names": [], "pages": [], "verified": [], "rejected": [], "note": "没有抓到可用页面"}

    combined = "\n\n".join(
        f"<页面 {index} 来源={page['title']}>\n{page['text']}" for index, page in enumerate(pages, 1)
    )[:max_chars]

    raw_names: list[str] = []
    if caller is not None:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是中文小说资料整理助手。请从给定网页正文里列出**这部作品的角色名**。\n"
                    '只输出 JSON：{"names": ["角色名", ...]}\n'
                    "要求：必须是正文里逐字出现的名字（不要职位、地名、组织名）；"
                    "同一角色的别名也列出来；最多 40 个。"
                ),
            },
            {
                "role": "user",
                "content": f"作品：《{book_title}》\n\n{combined}",
            },
        ]
        try:
            payload = await caller(messages)
            if isinstance(payload, dict):
                raw_names = [str(n).strip() for n in (payload.get("names") or []) if str(n).strip()]
        except Exception as exc:  # noqa: BLE001
            logger.warning("从网页提炼角色名失败：%s", exc)

    verified: list[str] = []
    rejected: list[str] = []
    for name in raw_names:
        if name in combined and 2 <= len(name) <= 6:
            if name not in verified:
                verified.append(name)
        else:
            rejected.append(name)

    return {
        "names": verified,
        "pages": [{"url": page["url"], "title": page["title"]} for page in pages],
        "verified": verified,
        "rejected": rejected,
        "note": "" if verified else "模型没有给出可校验的角色名",
    }


def diff_with_local(web_names: Sequence[str], local_names: Sequence[str]) -> dict:
    """对比网络名册与算法结果。"""
    web = {name.strip() for name in web_names if name.strip()}
    local = {name.strip() for name in local_names if name.strip()}
    only_web = sorted(web - local)
    only_local = sorted(local - web)
    return {
        "both": sorted(web & local),
        "only_web": only_web,
        "only_local": only_local,
        "coverage": round(len(web & local) / max(1, len(web)), 2),
    }
