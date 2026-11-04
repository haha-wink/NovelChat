"""角色头像爬取。

把「作品名 + 角色名」变成一组**可选**的头像候选，让用户自己挑。

数据源（按优先级，逐个尝试，某个失败不影响其它）：
1. Bangumi v0 角色搜索（中文 ACG 作品角色立绘最全）
2. Bangumi 旧版条目搜索 → 条目角色表
3. 萌娘百科（MediaWiki API，角色页主图）
4. 维基百科（REST summary 缩略图）

关键设计：
* **每一次尝试都记录结果**（``self.attempts``），失败不静默 —— 否则用户
  只会看到"没找到"，根本不知道是断网、被墙、限流还是解析错；
* 网络调用可注入（``fetch_json`` / ``fetch_json_post``），离线可测；
* 只接受 http(s) 图片地址，下载/代理必须过域名白名单（防 SSRF）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "novel-roleplay/1.0 (avatar crawler; +https://github.com/local/novel-roleplay)"
)

#: 允许被下载/代理的图片域名（防止 SSRF）
ALLOWED_IMAGE_HOSTS: tuple[str, ...] = (
    "lain.bgm.tv",
    "bgm.tv",
    "api.bgm.tv",
    "upload.wikimedia.org",
    "zh.wikipedia.org",
    "img.moegirl.org.cn",
    "mzh.moegirl.org.cn",
    "moegirl.icu",
    "commons.wikimedia.org",
)

BANGUMI_CHARACTER_SEARCH = "https://api.bgm.tv/v0/search/characters"
BANGUMI_SUBJECT_SEARCH = (
    "https://api.bgm.tv/search/subject/{keyword}?type={type}&responseGroup=small&max_results=4"
)
BANGUMI_SUBJECT_CHARACTERS = "https://api.bgm.tv/v0/subjects/{subject_id}/characters"
MOEGIRL_API = "https://zh.moegirl.org.cn/api.php"
WIKI_SUMMARY = "https://zh.wikipedia.org/api/rest_v1/page/summary/{title}"

_NORMALISE = re.compile(r"[\s·・.\-—_()（）\[\]【】「」\"'“”]")


@dataclass
class AvatarCandidate:
    url: str
    source: str
    label: str = ""
    score: float = 0.0

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "source": self.source,
            "label": self.label,
            "score": round(self.score, 2),
        }


def _normalise(text: str) -> str:
    return _NORMALISE.sub("", (text or "").strip()).lower()


def is_allowed_image_url(url: str) -> bool:
    match = re.match(r"^https?://([^/]+)/", url or "")
    if not match:
        return False
    host = match.group(1).lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_IMAGE_HOSTS)


def _upgrade_to_https(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def _name_match_score(target: str, candidate: str) -> float:
    """角色名匹配度：完全一致 1.0，互相包含 0.7，否则 0。"""
    left, right = _normalise(target), _normalise(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.7
    return 0.0


def _describe_error(exc: BaseException) -> str:
    """把异常翻译成人能看懂的一句话（用户/日志都要能定位问题）。"""
    name = type(exc).__name__
    text = str(exc) or ""
    lower = text.lower()
    if "timeout" in lower or "timed out" in lower:
        return f"连接超时（{name}）：目标站点无响应或被网络策略拦截"
    if "name or service not known" in lower or "nodename" in lower or "getaddrinfo" in lower:
        return f"DNS 解析失败（{name}）：本机可能无法解析该域名"
    if "certificate" in lower or "ssl" in lower:
        return f"TLS 证书校验失败（{name}）：可能有代理/杀毒软件替换了证书"
    if "403" in text:
        return "HTTP 403：对方拒绝了请求（可能需要更完整的 User-Agent 或被墙）"
    if "429" in text:
        return "HTTP 429：请求太频繁被限流，稍后重试"
    if "404" in text:
        return "HTTP 404：接口路径不存在（对方改版或该名字没有对应页面）"
    if "proxy" in lower:
        return f"代理配置问题（{name}）：检查 HTTP_PROXY/HTTPS_PROXY"
    return f"{name}: {text[:160]}"


class AvatarCrawler:
    """按作品 + 角色名抓头像候选。"""

    def __init__(
        self,
        *,
        fetch_json: Optional[Callable[[str], Awaitable[Any]]] = None,
        fetch_json_post: Optional[Callable[[str, dict], Awaitable[Any]]] = None,
        timeout: float = 8.0,
        max_candidates: int = 6,
        per_provider_timeout: float = 12.0,
    ) -> None:
        self._fetch_json = fetch_json or self._default_fetch_json
        self._fetch_json_post = fetch_json_post or self._default_fetch_json_post
        self.timeout = timeout
        self.max_candidates = max_candidates
        self.per_provider_timeout = per_provider_timeout
        #: 每次 find() 的尝试记录，供接口/CLI 展示"为什么没找到"
        self.attempts: list[dict] = []

    # ------------------------------------------------------------------
    # 默认 HTTP 实现
    # ------------------------------------------------------------------
    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    async def _request_json(self, method: str, url: str, payload: Optional[dict] = None) -> Any:
        """带 1 次重试的 JSON 请求（只对网络类错误重试，4xx 不重试）。"""
        import httpx

        last_error: Optional[BaseException] = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    headers=self._headers(),
                    follow_redirects=True,
                ) as client:
                    if method == "POST":
                        response = await client.post(url, json=payload or {})
                    else:
                        response = await client.get(url)
                    if response.status_code in (403, 404, 429) or response.status_code >= 500:
                        raise RuntimeError(f"HTTP {response.status_code}")
                    response.raise_for_status()
                    return response.json()
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络类错误重试一次
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.6)
                    continue
                raise
        raise last_error or RuntimeError("请求失败")

    async def _default_fetch_json(self, url: str) -> Any:
        return await self._request_json("GET", url)

    async def _default_fetch_json_post(self, url: str, payload: dict) -> Any:
        return await self._request_json("POST", url, payload)

    # ------------------------------------------------------------------
    # 尝试记录
    # ------------------------------------------------------------------
    async def _attempt(
        self,
        provider: str,
        factory: Callable[[], Awaitable[list[AvatarCandidate]]],
        *,
        detail: str = "",
    ) -> list[AvatarCandidate]:
        started = time.monotonic()
        record: dict = {"provider": provider, "ok": False, "found": 0, "error": None, "detail": detail}
        try:
            found = await asyncio.wait_for(factory(), timeout=self.per_provider_timeout)
            record["ok"] = True
            record["found"] = len(found)
            if not found:
                record["error"] = "能连上，但没有匹配到同名角色"
            return found
        except asyncio.TimeoutError:
            record["error"] = f"整体超时（>{self.per_provider_timeout:.0f}s）"
            logger.warning("头像源 %s 超时", provider)
            return []
        except Exception as exc:  # noqa: BLE001 - 单个源失败不影响其它
            record["error"] = _describe_error(exc)
            logger.warning("头像源 %s 失败：%s", provider, record["error"])
            return []
        finally:
            record["elapsed"] = round(time.monotonic() - started, 2)
            self.attempts.append(record)

    # ------------------------------------------------------------------
    # 数据源
    # ------------------------------------------------------------------
    async def _from_bangumi_search(self, character_name: str) -> list[AvatarCandidate]:
        payload = await self._fetch_json_post(
            BANGUMI_CHARACTER_SEARCH, {"keyword": character_name}
        )
        found: list[AvatarCandidate] = []
        for item in (payload or {}).get("data") or []:
            match = _name_match_score(character_name, item.get("name") or "")
            if match <= 0:
                continue
            images = item.get("images") or {}
            url = images.get("large") or images.get("medium") or images.get("grid")
            if url:
                found.append(
                    AvatarCandidate(
                        url=_upgrade_to_https(url),
                        source="bangumi",
                        label=item.get("name") or character_name,
                        score=match,
                    )
                )
        return found

    async def _from_bangumi_subjects(
        self, character_name: str, work_title: str
    ) -> list[AvatarCandidate]:
        """旧版接口：先按书名找条目（书/动画各试一次），再拉条目角色表。"""
        found: list[AvatarCandidate] = []
        subject_ids: list[tuple[int, str]] = []
        for subject_type in (1, 2):  # 1=书籍 2=动画
            try:
                payload = await self._fetch_json(
                    BANGUMI_SUBJECT_SEARCH.format(
                        keyword=quote(work_title), type=subject_type
                    )
                )
            except Exception:  # noqa: BLE001
                continue
            for subject in ((payload or {}).get("list") or [])[:2]:
                subject_id = subject.get("id")
                if subject_id:
                    subject_ids.append((int(subject_id), subject.get("name") or ""))

        for subject_id, subject_name in subject_ids[:3]:
            try:
                characters = await self._fetch_json(
                    BANGUMI_SUBJECT_CHARACTERS.format(subject_id=subject_id)
                )
            except Exception:  # noqa: BLE001
                continue
            for item in characters or []:
                match = _name_match_score(character_name, item.get("name") or "")
                if match <= 0:
                    continue
                images = item.get("images") or {}
                url = images.get("large") or images.get("medium") or images.get("grid")
                if url:
                    found.append(
                        AvatarCandidate(
                            url=_upgrade_to_https(url),
                            source=f"bangumi:{subject_name}",
                            label=item.get("name") or character_name,
                            score=match,
                        )
                    )
        return found

    async def _from_moegirl(self, character_name: str) -> list[AvatarCandidate]:
        found: list[AvatarCandidate] = []
        search = await self._fetch_json(
            f"{MOEGIRL_API}?action=query&list=search&srsearch={quote(character_name)}"
            "&srlimit=3&format=json"
        )
        titles = [
            item.get("title", "")
            for item in ((search or {}).get("query") or {}).get("search") or []
        ]
        for title in titles:
            if _name_match_score(character_name, title) <= 0:
                continue
            info = await self._fetch_json(
                f"{MOEGIRL_API}?action=query&titles={quote(title)}"
                "&prop=pageimages&piprop=original|thumbnail&pithumbsize=512"
                "&redirects=1&format=json"
            )
            pages = ((info or {}).get("query") or {}).get("pages") or {}
            for page in pages.values():
                original = (page.get("original") or {}).get("source")
                thumb = (page.get("thumbnail") or {}).get("source")
                url = original or thumb
                if url:
                    found.append(
                        AvatarCandidate(
                            url=_upgrade_to_https(url),
                            source="moegirl",
                            label=page.get("title", character_name),
                            score=_name_match_score(character_name, page.get("title", "")),
                        )
                    )
        return found

    async def _from_wikipedia(self, character_name: str) -> list[AvatarCandidate]:
        payload = await self._fetch_json(WIKI_SUMMARY.format(title=quote(character_name)))
        thumb = ((payload or {}).get("thumbnail") or {}).get("source")
        title = (payload or {}).get("title") or character_name
        if thumb and _name_match_score(character_name, title) > 0:
            return [
                AvatarCandidate(
                    url=_upgrade_to_https(thumb),
                    source="wikipedia",
                    label=title,
                    score=_name_match_score(character_name, title) * 0.9,
                )
            ]
        return []

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    async def find(
        self, *, character_name: str, work_title: str = "", limit: Optional[int] = None
    ) -> list[dict]:
        """返回头像候选（已过滤非法域名、去重、按匹配度排序）。

        失败原因记录在 ``self.attempts`` 里，调用方可以原样返回给前端。
        """
        character_name = (character_name or "").strip()
        work_title = (work_title or "").strip()
        limit = limit or self.max_candidates
        self.attempts = []

        candidates: list[AvatarCandidate] = []
        candidates.extend(
            await self._attempt(
                "bangumi_search", lambda: self._from_bangumi_search(character_name)
            )
        )

        if len(candidates) < limit and work_title:
            candidates.extend(
                await self._attempt(
                    "bangumi_subjects",
                    lambda: self._from_bangumi_subjects(character_name, work_title),
                    detail=work_title,
                )
            )

        if len(candidates) < limit:
            candidates.extend(
                await self._attempt("moegirl", lambda: self._from_moegirl(character_name))
            )

        if len(candidates) < limit:
            candidates.extend(
                await self._attempt("wikipedia", lambda: self._from_wikipedia(character_name))
            )

        seen: set[str] = set()
        result: list[AvatarCandidate] = []
        for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
            url = _upgrade_to_https(candidate.url)
            if not is_allowed_image_url(url) or url in seen:
                continue
            seen.add(url)
            candidate.url = url
            result.append(candidate)
            if len(result) >= limit:
                break
        return [item.as_dict() for item in result]

    def explain(self) -> str:
        """把 attempts 拼成一句人话，用于日志/接口提示。"""
        if not self.attempts:
            return "没有发起任何抓取"
        parts = []
        for record in self.attempts:
            if record["ok"] and record["found"]:
                parts.append(f"{record['provider']} ✓ {record['found']} 张")
            else:
                parts.append(f"{record['provider']} ✗ {record['error'] or '无结果'}")
        return "；".join(parts)


async def download_avatar(url: str, *, max_bytes: int = 4 * 1024 * 1024) -> tuple[bytes, str]:
    """下载头像图片，返回 (数据, 后缀)。会校验域名与图片魔数。"""
    import httpx

    if not is_allowed_image_url(url):
        raise ValueError("该图片域名不在允许列表内")

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        # 部分图床会检查 Referer，带一个同域来源更稳
        "Referer": "https://bgm.tv/",
        "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
    }
    async with httpx.AsyncClient(
        timeout=20.0, headers=headers, follow_redirects=True
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.content
        if len(data) > max_bytes:
            raise ValueError("图片过大")

    suffix = "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = "png"
    elif data.startswith(b"GIF8"):
        suffix = "gif"
    elif len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        suffix = "webp"
    elif not data.startswith(b"\xff\xd8\xff"):
        raise ValueError("不是受支持的图片格式")
    return data, suffix