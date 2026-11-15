"""角色档案管理员 Agent。

在算法挖掘之后接手，负责把名单"审"干净，而不是只做一次补全：

    miner_report        看候选、证据、可疑点
    name_scan           用现成 NLP 库（jieba/HanLP）独立扫全文认人名，对照算法名单找漏抽
    name_check          校验名字：是否人名 / 中国人名 / 外国译名 / 姓氏与名
    text_evidence       某个名字在原文里的真实证据（出现次数 / 说话次数 / 上下文）
    web_roster          按书名联网找真实角色表，用来对照
    tune_miner          调整算法阈值 / 停用词 / 强制名单后重跑
    avatar_candidates   给角色抓头像（可多候选，交给用户挑）
    finish              提交最终名单

设计原则：**模型只做决策，不提供事实**。
每个动作的产出都经过确定性校验（名字必须逐字出现在原文、头像域名必须白名单），
模型无法把"编出来的角色"塞进结果里。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from core.config import get_settings
from services.catalog import character_miner as miner
from services.catalog import name_engine
from services.catalog import name_library
from agent.tools import web as web_tools

logger = logging.getLogger(__name__)

LLMJsonCaller = Callable[[list[dict[str, str]]], Awaitable[dict[str, Any]]]

#: 单次观察结果的上限（字符）。超了按**结构**裁剪，不切断 JSON。
MAX_OBSERVATION_CHARS = 900
#: 单个字符串字段的上限
MAX_STRING_CHARS = 200
#: 单个数组的条目上限
MAX_LIST_ITEMS = 20

#: 名字列表的分隔符（模型可能把 keep 写成 "路明非, 恺撒"）
_NAME_SPLIT = re.compile(r"[,，、;；/\s]+")
_RENAME_SPLIT = re.compile(r"\s*(?:->|→|=>)\s*")


def _clip(text: Any, limit: int = MAX_STRING_CHARS) -> str:
    content = "" if text is None else str(text)
    return content if len(content) <= limit else content[:limit] + "…"


def _shrink(value: Any, *, str_limit: int, list_limit: int, depth: int = 0) -> Any:
    """**结构式**裁剪：只截字符串和数组，返回值仍是合法 JSON 结构。

    为什么不直接切字符串——把 ``{"names": ["a","b","c"`` 这种半截 JSON 喂给模型，
    它要么解析失败，要么被误导，一步就白费了。
    """
    if depth > 3:
        return _clip(value, 60)
    if isinstance(value, str):
        return _clip(value, str_limit)
    if isinstance(value, dict):
        return {key: _shrink(item, str_limit=str_limit, list_limit=list_limit, depth=depth + 1)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        total = len(value)
        items = [
            _shrink(item, str_limit=str_limit, list_limit=list_limit, depth=depth + 1)
            for item in list(value)[:list_limit]
        ]
        if total > list_limit:
            items.append(f"…（共 {total} 项，省略后 {total - list_limit} 项）")
        return items
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clip(value, str_limit)


def _dumps(payload: Any, limit: int = MAX_OBSERVATION_CHARS) -> str:
    """序列化成**合法 JSON**；超长时逐步收紧结构，绝不产出半截 JSON。"""
    text = json.dumps(payload, ensure_ascii=False)
    for str_limit, list_limit in (
        (MAX_STRING_CHARS, MAX_LIST_ITEMS),
        (120, 12),
        (80, 8),
        (60, 5),
    ):
        if len(text) <= limit:
            break
        payload = _shrink(payload, str_limit=str_limit, list_limit=list_limit)
        text = json.dumps(payload, ensure_ascii=False)
    if len(text) > limit:
        keys = list(payload)[:10] if isinstance(payload, dict) else []
        text = json.dumps(
            {
                "truncated": True,
                "keys": keys,
                "note": "结果过长已省略，请用更精确的参数再查一次",
            },
            ensure_ascii=False,
        )
    return text


def _as_name_list(value: Any, *, limit: int = 120) -> list[str]:
    """把模型可能给出的各种形态归一成名字列表。

    模型经常不听话：``keep`` 可能是 ``"路明非, 恺撒"``、``["路明非"]``、
    ``[{"name": "路明非"}]``、``{"names": [...]}``、甚至 ``{"路明非": true}``。
    这里全部接受，去重、去空、限长——**永远返回 list[str]**。
    """
    if value is None:
        return []

    raw: list[Any]
    if isinstance(value, str):
        raw = _NAME_SPLIT.split(value)
    elif isinstance(value, dict):
        for key in ("names", "keep", "list", "items", "values", "characters"):
            nested = value.get(key)
            if isinstance(nested, (list, tuple, str, dict)):
                return _as_name_list(nested, limit=limit)
        if value.get("name"):
            raw = [value["name"]]
        else:  # {"路明非": true, "掌门": false}
            raw = [key for key, flag in value.items() if flag]
    elif isinstance(value, (list, tuple, set)):
        raw = []
        for item in value:
            if isinstance(item, dict):
                raw.append(item.get("name") or item.get("character") or item.get("value") or "")
            elif isinstance(item, str):
                raw.extend(_NAME_SPLIT.split(item))
            else:
                raw.append(item)
    else:
        raw = [value]

    out: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if not name or len(name) > 16:
            continue
        if name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def _as_int(value: Any, default: int, *, lo: int = 1, hi: int = 999) -> int:
    """模型常把数字写成字符串（"40"），也可能写 "四十"——统一兜住。"""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _as_rename_map(value: Any, *, limit: int = 40) -> dict[str, str]:
    """把 ``rename`` 归一成 ``{旧名: 新名}``。

    接受 ``dict`` / ``[{"from":..,"to":..}]`` / ``["A->B"]`` / ``"A->B,C->D"``。
    """
    pairs: list[tuple[Any, Any]] = []
    if isinstance(value, dict):
        pairs = list(value.items())
    elif isinstance(value, str):
        for chunk in re.split(r"[,，;；\n]+", value):
            if re.search(r"->|→|=>", chunk):
                parts = _RENAME_SPLIT.split(chunk, maxsplit=1)
                if len(parts) == 2:
                    pairs.append((parts[0], parts[1]))
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                pairs.append(
                    (
                        item.get("from") or item.get("src") or item.get("old") or item.get("昵称"),
                        item.get("to") or item.get("dst") or item.get("new") or item.get("正名"),
                    )
                )
            elif isinstance(item, str) and re.search(r"->|→|=>", item):
                parts = _RENAME_SPLIT.split(item, maxsplit=1)
                if len(parts) == 2:
                    pairs.append((parts[0], parts[1]))

    out: dict[str, str] = {}
    for src, dst in pairs:
        left, right = str(src or "").strip(), str(dst or "").strip()
        if left and right and left != right and len(left) <= 16 and len(right) <= 16:
            out[left] = right
        if len(out) >= limit:
            break
    return out

#: 姓名库分类的中文标签（给模型看的）
_KIND_LABEL = {
    "cn": "中国人名",
    "foreign": "外国译名",
    "nickname": "称呼",
    "unknown": "不像人名",
}


def _truncate(text: str, limit: int = MAX_OBSERVATION_CHARS) -> str:
    """仅用于**纯文本**摘要（如 trace 记录）。结构化数据请用 :func:`_dumps`。"""
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


SYSTEM_PROMPT = """你是「角色档案管理员」。你只做一件事：**判定候选名单里哪些是原文中真实存在的角色**。

## 三项判定（任一不过就剔除）
1. **是人**：地名、组织、门派、职位、泛称、招式、物品都不是角色。
2. **原文有**：名字必须逐字出现在正文里，并且有说话、动作或被他人提及的痕迹。
3. **不重复**：同一角色的昵称/全名/音译异体要合并成一个（用 rename）。

## 工作方式：观察 → 判断 → 行动
- **观察**：候选表已经给出出现次数、台词数、别名、姓名库评分。多数情况到这里就够判断了。
- **判断**：只有「疑似漏人」「疑似伪角色」「统计明显矛盾」三种情况才值得调工具。
- **行动**：每次只输出**一个** JSON 动作。拿不准的怀疑，不如直接 finish。

## 工具与代价
- `name_scan`（低）：让 NLP 库独立扫全文，看 not_in_candidates 里有没有漏掉的人。
- `name_check`（低）：判断一批词像不像人名、是中国人名还是外国译名。
- `text_evidence`（低，全程 ≤2 次）：查某个名字在原文里的戏份。
- `miner_report`（低）：看算法丢掉了什么、为什么丢。
- `tune_miner`（高）：改阈值后**重跑全文算法**，很慢。
- `web_roster`（很高）：联网查这本书的角色表，可能超时，谨慎使用。

## 省步数的铁律
- 候选表看着正常就**直接 finish**——这是最常见的正确做法。
- 不要在 finish 前反复确认。步数很紧，早交名单永远优于空转。
- 头像不归你管，不要为它花任何步骤。

## 输出格式（一次一个 JSON）
{"thought": "一句话判断，≤20 字", "action": "工具名", "args": {...}}
{"thought": "...", "action": "finish", "args": {"keep": ["真实角色"], "drop": ["伪角色"], "rename": {"昵称": "正名"}}}

- `keep`：你认可的最终名单，可以照抄候选表里的名字。
- `drop`：要剔除的（伪角色）。
- `rename`：要合并的，目标名必须是原文里出现过的**正式名**。
- 三者都接受字符串或数组；数组里的名字请用原文里的写法。"""


class ExtractionAgent:
    """ReAct 式有界循环：每步一个 LLM 调用 + 一个确定性工具。"""

    def __init__(
        self,
        *,
        book_title: str,
        text: str,
        api_key: str = "",
        model: str = "",
        max_steps: int = 8,
        enable_web: bool = True,
        enable_avatars: bool = True,
        caller: Optional[LLMJsonCaller] = None,
        search: Optional[Callable[..., Awaitable[list[dict]]]] = None,
        fetch: Optional[Callable[..., Awaitable[str]]] = None,
        crawler: Any = None,
        base_params: Optional[dict] = None,
        engine: Any = None,
    ) -> None:
        self.book_title = book_title
        self.text = text
        self.api_key = api_key
        self.model = model
        self.max_steps = max(1, max_steps)
        self.enable_web = enable_web
        self.enable_avatars = enable_avatars
        self._caller = caller
        self._search = search
        self._fetch = fetch
        self._crawler = crawler
        #: 人名识别引擎：优先 jieba / HanLP，装不上自动回退内置规则
        self.engine = engine if engine is not None else name_engine.get_engine()
        self.params = {
            "min_mentions": get_settings().miner_min_mentions,
            "max_characters": get_settings().max_characters,
            **(base_params or {}),
        }
        self.current: dict[str, dict] = {}
        self.dropped: list[tuple[str, str]] = []
        self.web: dict = {}
        self.avatars: dict[str, list[dict]] = {}
        self.trace: list[dict] = []
        self.notes: list[str] = []
        #: 姓名库判定缓存：候选表每步都会被重建，不缓存会把全文扫描重复 N 次
        self._verdicts: dict[str, Any] = {}

    def _verdict(self, name: str):
        """带缓存的姓名库判定（纯 CPU，但要对 30 万字正文做多次扫描）。"""
        key = str(name or "")
        cached = self._verdicts.get(key)
        if cached is None:
            cached = name_library.analyze(key, text=self.text)
            self._verdicts[key] = cached
        return cached

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    async def _llm_json(self, messages: list[dict[str, str]], *, max_tokens: int = 1024) -> dict:
        from services.catalog.extractor import call_llm_json

        if self._caller is not None:
            return await self._caller(messages)
        return await call_llm_json(
            messages, api_key=self.api_key, model=self.model, max_tokens=max_tokens
        )

    def _run_miner(self) -> None:
        records, diagnostic = miner.mine_characters(
            self.text,
            max_characters=int(self.params["max_characters"]),
            min_mentions=int(self.params["min_mentions"]),
        )
        self.current = {record["name"]: record for record in records}
        self.dropped = [
            (item.get("name", ""), item.get("reason", ""))
            for item in (diagnostic.get("dropped") or [])
        ]
        self.miner_diagnostic = diagnostic

    def _candidate_table(self, limit: int = 40) -> str:
        rows = []
        for index, record in enumerate(
            sorted(self.current.values(), key=lambda r: -r.get("appearances", 0))[:limit], start=1
        ):
            verdict = self._verdict(record["name"])
            rows.append(
                f"{index}. {record['name']}｜出现{record.get('appearances', 0)}次"
                f"｜台词{len(record.get('quotes') or [])}条"
                f"｜别名:{'/'.join(record.get('aliases') or []) or '-'}"
                f"｜姓名库:{verdict.score}({_KIND_LABEL.get(verdict.kind, verdict.kind)})"
            )
        return "\n".join(rows) if rows else "（当前没有候选）"

    async def tool_name_check(self, names: Any) -> dict:
        """批量校验名字：现成 NLP 库（jieba/HanLP）打底，内置规则兜底。"""
        wanted = _as_name_list(names, limit=30)
        if not wanted:
            return {"error": "names 不能为空（字符串或字符串数组）"}

        engine = self.engine
        results = []
        for name in wanted:
            record = engine.classify(name, text=self.text)
            record["in_current"] = name in self.current
            results.append(record)

        recognised = [item for item in results if item["plausible"]]
        # 只回必要字段：每个检查项都带全文级别的附加信息会把观察撑爆
        compact = [
            {
                "name": item.get("name"),
                "kind": item.get("kind"),
                "score": round(float(item.get("score") or 0.0), 2),
                "plausible": bool(item.get("plausible")),
                "in_current": bool(item.get("in_current")),
            }
            for item in results
        ]
        return {
            "engine": engine.name,
            "engine_is_nlp": engine.is_nlp,
            "checked": len(results),
            "recognised": len(recognised),
            "cn": sum(1 for item in results if item["kind"] == "cn"),
            "foreign": sum(1 for item in results if item["kind"] == "foreign"),
            "nickname": sum(1 for item in results if item["kind"] == "nickname"),
            "results": compact,
            "note": "kind=cn 中国人名 / foreign 外国译名 / nickname 老X小X / unknown 不像人名"
            + ("" if engine.is_nlp else "；当前用的是内置规则，装 jieba 会更准"),
        }

    async def tool_name_scan(self, limit: int = 40, min_count: int = 1) -> dict:
        """用 NLP 库独立扫一遍全文，列出它认出来的人名（用来对照算法名单、发现漏抽）。"""
        engine = self.engine
        # 先把已有名单喂进分词词典，避免有名字被切碎
        engine.learn(list(self.current))
        result = engine.detect(self.text, limit=max(1, min(int(limit or 40), 80)), min_count=min_count)
        names = [
            {"name": item.get("name"), "count": item.get("count")}
            for item in (result.get("names") or [])[:40]
        ]
        found = {item["name"] for item in result.get("names") or []}
        known = set(self.current)
        return {
            "engine": result.get("engine"),
            "checked_chars": result.get("checked_chars"),
            "names": names,
            "total": len(result.get("names") or []),
            "not_in_candidates": sorted(found - known)[:20],
            "candidates_not_found": sorted(known - found)[:20],
            "note": (result.get("note") or "")
            + "｜not_in_candidates 是库认出但算法没挖到的（可能漏抽）；"
            "candidates_not_found 是算法挖到但库没认出的（可能是昵称/生僻名）",
        }

    async def tool_miner_report(self) -> dict:
        # 候选表已经在首条消息里给过了，这里**不再重复**（省一半 token）
        dropped_rows = [
            _clip(f"{name}（{reason}）", 60) for name, reason in self.dropped[:12] if name
        ]
        return {
            "params": self.params,
            "candidates": len(self.current),
            "dropped_total": len(self.dropped),
            "dropped_sample": dropped_rows,
            "note": "候选明细见首条消息；dropped_sample 是被算法丢掉的词，可能含被错杀的角色",
        }

    async def tool_text_evidence(self, name: str) -> dict:
        name = str(name or "").strip()
        if not name:
            return {"error": "name 不能为空"}
        count = self.text.count(name)
        snippets: list[str] = []
        start = 0
        while len(snippets) < 2:
            index = self.text.find(name, start)
            if index < 0:
                break
            snippets.append(
                _clip(self.text[max(0, index - 30): index + 45].replace("\n", " "), 90)
            )
            start = index + len(name)
        verdict = self._verdict(name)
        return {
            "name": name,
            "in_text": count,
            "snippets": snippets,
            "name_verdict": {"kind": verdict.kind, "score": round(float(verdict.score or 0), 2)},
            "in_current": name in self.current,
        }

    async def tool_web_roster(self, query: str = "") -> dict:
        if not self.enable_web:
            return {"disabled": True, "note": "联网检索已被配置关闭"}
        if not self.api_key and self._caller is None:
            return {"error": "缺少 API Key，无法提炼网页里的角色名"}

        result = await web_tools.roster_from_web(
            self.book_title,
            api_key=self.api_key,
            model=self.model,
            extra_query=query or "",
            search=self._search,
            fetch=self._fetch,
            caller=self._llm_json,
        )
        self.web = result
        return {
            "names": (result.get("names") or [])[:20],
            "total": len(result.get("names") or []),
            "pages": [
                _clip(page.get("title") or page.get("url"), 60)
                for page in (result.get("pages") or [])[:5]
            ],
            "rejected": (result.get("rejected") or [])[:8],
            "note": _clip(result.get("note", ""), 120),
        }

    async def tool_tune_miner(
        self,
        min_mentions: Optional[int] = None,
        max_characters: Optional[int] = None,
        add_stop: Any = None,
        add_names: Any = None,
    ) -> dict:
        before = set(self.current)
        if min_mentions is not None:
            try:
                self.params["min_mentions"] = max(2, min(10, int(min_mentions)))
            except (TypeError, ValueError):
                pass
        if max_characters is not None:
            try:
                self.params["max_characters"] = max(5, min(60, int(max_characters)))
            except (TypeError, ValueError):
                pass
        self._run_miner()

        # 额外停用词（模型认为不是人名的词）——可能是字符串、数组、甚至 dict
        for word in _as_name_list(add_stop, limit=40):
            if word in self.current:
                self.current.pop(word)
                self.dropped.append((word, "agent 判定不是人名"))

        # 强制补充（必须真的在原文里，且通过名字库/身份词校验）
        added: list[str] = []
        for name in _as_name_list(add_names, limit=30):
            if name in self.current or len(name) < 2:
                continue
            if name not in self.text:
                continue
            verdict = self._verdict(name)
            if verdict.score < 0.4:
                continue
            self.current[name] = {
                "name": name,
                "aliases": [],
                "role": "",
                "personality": [],
                "speech_style": "",
                "background": "",
                "relationships": [],
                "quotes": [],
                "narration": [],
                "excerpts": [],
                "appearances": self.text.count(name),
            }
            added.append(name)

        return {
            "params": self.params,
            "before": len(before),
            "after": len(self.current),
            "removed": sorted(before - set(self.current))[:20],
            "added": added,
            "candidates": self._candidate_table(limit=15),
        }

    async def tool_avatar_candidates(self, name: str) -> dict:
        if not self.enable_avatars:
            return {"disabled": True, "note": "头像爬取已被配置关闭"}
        name = (name or "").strip()
        if not name:
            return {"error": "name 不能为空"}

        from agent.tools.avatar import AvatarCrawler

        crawler = self._crawler or AvatarCrawler()
        try:
            candidates = await crawler.find(character_name=name, work_title=self.book_title)
        except Exception as exc:  # noqa: BLE001 - 抓不到不影响主流程
            return {"error": f"抓取失败：{str(exc)[:120]}"}

        if candidates:
            self.avatars[name] = candidates
        return {
            "name": name,
            "found": len(candidates),
            "sources": [item.get("source") for item in candidates],
            "note": "这些头像会存进角色，供用户在界面上挑选" if candidates else "没找到可用头像",
        }

    def _finalise(self, keep: Any, drop: Any, rename: Any) -> tuple[list[dict], dict]:
        """确定性收口：只保留原文里真实存在的名字，并做别名合并。

        ``keep`` / ``drop`` / ``rename`` 一律按"模型可能乱传"处理：
        字符串 ``"路明非,恺撒"``、数组、``[{"name": ...}]``、``{"路明非": true}``、
        ``"A->B"`` 形式的改名都能吃下——类型防御在 :func:`_as_name_list` /
        :func:`_as_rename_map` 里，这里只关心业务规则。
        """
        report: dict = {"kept": 0, "dropped": [], "renamed": [], "added_from_web": []}

        drop_set = set(_as_name_list(drop))
        keep_list = _as_name_list(keep)
        rename_map = _as_rename_map(rename)

        # 模型有时 finish 时不带 keep：那就视为"接受当前名单"，只应用 drop / rename
        if not keep_list:
            keep_list = [name for name in self.current if name not in drop_set]
            report["kept_without_explicit_list"] = True

        # 1) 模型要保留的必须存在：当前候选 / 网络名册且原文可查
        records: dict[str, dict] = {}
        for name in keep_list:
            if name in self.current:
                records[name] = self.current[name]
                continue
            if name in drop_set:
                continue
            web_names = set(self.web.get("names") or [])
            if name in web_names and name in self.text:
                verdict = name_library.analyze(name, text=self.text)
                if verdict.score >= 0.4:
                    records[name] = {
                        "name": name,
                        "aliases": [],
                        "role": "",
                        "personality": [],
                        "speech_style": "",
                        "background": "",
                        "relationships": [],
                        "quotes": [],
                        "narration": [],
                        "excerpts": [],
                        "appearances": self.text.count(name),
                    }
                    report["added_from_web"].append(name)
                    continue
            report["dropped"].append({"name": name, "reason": "原文里查不到或不像人名"})

        if not records:
            # 兜底：模型没给可用名单时保留算法结果
            records = dict(self.current)
            report["fallback"] = True

        # 2) rename 合并（别名保留）
        merged: dict[str, dict] = {}
        for key, record in records.items():
            target = rename_map.get(key, key)
            if target != key and target not in self.text:
                target = key  # 目标名原文里没有 -> 不合并
            if target != key:
                report["renamed"].append({"from": key, "to": target})
            if target not in merged:
                merged[target] = {**record, "name": target, "aliases": list(record.get("aliases") or [])}
            else:
                existing = merged[target]
                existing["quotes"] = list(
                    dict.fromkeys([*(existing.get("quotes") or []), *(record.get("quotes") or [])])
                )[:8]
                existing["aliases"] = list(
                    dict.fromkeys([*(existing.get("aliases") or []), *(record.get("aliases") or []), key])
                )[:6]
                existing["appearances"] = int(existing.get("appearances") or 0) + int(
                    record.get("appearances") or 0
                )

        final: list[dict] = []
        for name, record in merged.items():
            if name in drop_set:
                report["dropped"].append({"name": name, "reason": "agent 判定不是角色"})
                continue
            if name not in self.text:
                report["dropped"].append({"name": name, "reason": "原文中不存在"})
                continue
            record["aliases"] = [a for a in (record.get("aliases") or []) if a != name][:6]
            final.append(record)

        final.sort(key=lambda r: -int(r.get("appearances") or 0))
        report["kept"] = len(final)
        return final, report

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def run(self) -> dict:
        self._run_miner()
        if not self.current:
            return {
                "characters": [],
                "avatars": {},
                "trace": [],
                "report": {"error": "算法没有挖到任何候选"},
            }

        system_msg = {"role": "system", "content": SYSTEM_PROMPT}
        opener = {
            "role": "user",
            "content": (
                f"作品：《{self.book_title}》｜正文 {len(self.text)} 字｜语言：简体中文｜"
                f"算法阈值：{json.dumps(self.params, ensure_ascii=False)}\n\n"
                f"当前候选（{len(self.current)} 个）：\n{self._candidate_table()}\n\n"
                f"被算法丢弃的样本：\n"
                + ("\n".join(f"- {_clip(name, 20)}（{_clip(reason, 20)}）"
                            for name, reason in self.dropped[:10]) or "- 无")
                + "\n\n请开始：输出一个 JSON 动作。"
            ),
        }

        #: 每轮的工具调用记录（结构化保存，messages 每步重新拼装以便折叠旧观察）
        rounds: list[dict] = []
        final_characters: Optional[list[dict]] = None
        report: dict = {}

        for step in range(1, self.max_steps + 1):
            messages = self._compose(system_msg, opener, rounds, last_step=(step == self.max_steps))
            try:
                # 输出的只是一个 JSON 动作，400 token 足够；比固定 800 省一半延迟
                payload = await self._llm_json(messages, max_tokens=400)
            except Exception as exc:  # noqa: BLE001 - 单步失败不该炸整本
                self.trace.append(
                    {"step": step, "action": "llm_error", "observation": _clip(str(exc), 200)}
                )
                break

            if not isinstance(payload, dict):
                payload = {"action": str(payload or "")}

            action = str(payload.get("action") or "").strip().lower()
            args = payload.get("args")
            if not isinstance(args, dict):
                # 模型偶尔把参数平铺到顶层：{"action":"name_scan","limit":40}
                args = {key: value for key, value in payload.items()
                        if key not in ("action", "thought", "why", "reason")}

            if action == "finish":
                final_characters, report = self._finalise(
                    args.get("keep"), args.get("drop"), args.get("rename")
                )
                self.trace.append(
                    {
                        "step": step,
                        "action": "finish",
                        "observation": json.dumps(
                            {"kept": report.get("kept"),
                             "dropped": len(report.get("dropped") or [])},
                            ensure_ascii=False,
                        ),
                    }
                )
                break

            observation = await self._dispatch(action, args)
            text = _dumps(observation)
            summary = self._summarize(action, observation)
            rounds.append(
                {"action": action or "unknown", "args": args, "text": text, "summary": summary}
            )
            self.trace.append(
                {"step": step, "action": action or "unknown", "observation": _clip(text, 400)}
            )

        if final_characters is None:
            final_characters = list(self.current.values())
            report = {
                "fallback": True,
                "reason": f"{self.max_steps} 步内没有 finish，已回退到算法名单",
                "kept": len(final_characters),
            }

        # 头像不再占用决策步数：finish 之后按出场次数顺手抓前几个
        if self.enable_avatars and final_characters:
            await self._collect_avatars(final_characters)

        report["steps"] = len(self.trace)
        report["miner_params"] = self.params
        report["web_names"] = list(self.web.get("names") or [])[:30]
        report["avatars"] = {name: len(items) for name, items in self.avatars.items()}
        return {
            "characters": final_characters,
            "avatars": self.avatars,
            "trace": self.trace,
            "report": report,
        }

    # ------------------------------------------------------------------
    # 上下文拼装（控制 token）
    # ------------------------------------------------------------------
    def _compose(
        self,
        system_msg: dict[str, str],
        opener: dict[str, str],
        rounds: list[dict],
        *,
        last_step: bool = False,
    ) -> list[dict[str, str]]:
        """拼这一轮的消息。

        两条控成本的规则：

        1. **只有最后一次观察给全文**，更早的折成一行摘要——观察每步可达 900 字，
           6 步就是 5.4k 字，折叠能省掉七成上下文。
        2. **不回显 thought**：模型自己想的理由留在它那一轮即可，写回历史只是重复计费。
        """
        messages = [system_msg, opener]
        last_index = len(rounds) - 1
        for index, record in enumerate(rounds):
            messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {"action": record["action"], "args": record["args"]},
                        ensure_ascii=False,
                    ),
                }
            )
            body = record["text"] if index == last_index else record["summary"]
            messages.append({"role": "user", "content": f"工具结果：{body}"})

        if last_step:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "这是最后一步。请立刻输出 finish：keep 给出你认可的完整名单"
                        "（可直接照抄候选表里的名字），drop 给出要剔除的。"
                    ),
                }
            )
        else:
            messages.append(
                {"role": "user", "content": "请输出下一个 JSON 动作（满意就 finish）。"}
            )
        return messages

    @staticmethod
    def _summarize(action: str, payload: dict) -> str:
        """把一次观察折成一行，供后续轮次使用（避免历史无限膨胀）。"""
        try:
            if action == "name_scan":
                new = payload.get("not_in_candidates") or []
                return f"扫描完成，疑似漏抽 {len(new)} 个：{'、'.join(map(str, new[:6])) or '无'}"
            if action == "name_check":
                total = int(payload.get("checked") or 0)
                humans = int(payload.get("cn") or 0) + int(payload.get("foreign") or 0)
                return f"校验 {total} 个词，其中人名 {humans} 个"
            if action == "text_evidence":
                return (
                    f"「{payload.get('name', '')}」在原文出现 {payload.get('in_text', 0)} 次，"
                    f"姓名库 {payload.get('name_verdict', {}).get('kind', '?')}"
                )
            if action == "miner_report":
                return f"候选 {payload.get('candidates', 0)} 个，丢弃 {payload.get('dropped_total', 0)} 个"
            if action == "tune_miner":
                return f"算法重跑：{payload.get('before')} → {payload.get('after')} 个候选"
            if action == "web_roster":
                return f"网络名册取到 {payload.get('total', 0)} 个名字"
            if action == "avatar_candidates":
                return "头像候选已记录"
        except Exception:  # noqa: BLE001 - 摘要失败不影响主流程
            pass
        return _clip(json.dumps(payload, ensure_ascii=False), 60)

    async def _collect_avatars(self, characters: list[dict], *, top: int = 2) -> None:
        """按出场次数给前几个角色抓头像（并发、失败即放弃）。

        头像不再进决策循环：它只影响展示，不值得占一次模型调用。
        """
        if self.avatars:
            return  # 已经通过工具抓过了，不重复花钱
        targets = [
            record.get("name")
            for record in sorted(
                characters, key=lambda r: -int(r.get("appearances") or 0)
            )[:top]
            if record.get("name")
        ]
        if not targets:
            return
        try:
            from agent.tools.avatar import AvatarCrawler

            crawler = self._crawler or AvatarCrawler()

            async def fetch(name: str):
                try:
                    return await asyncio.wait_for(
                        crawler.find(character_name=name, work_title=self.book_title),
                        timeout=8,
                    )
                except Exception:  # noqa: BLE001 - 抓不到不影响抽取
                    return []

            results = await asyncio.gather(*(fetch(name) for name in targets))
            for name, items in zip(targets, results):
                if items:
                    self.avatars[name] = items
        except Exception:  # noqa: BLE001
            logger.warning("头像抓取失败（已忽略）", exc_info=True)

    async def _dispatch(self, action: str, args: dict) -> dict:
        """执行一个工具，返回**结构化结果**（由调用方统一序列化/裁剪）。"""
        try:
            if action == "miner_report":
                return await self.tool_miner_report()
            if action == "name_check":
                return await self.tool_name_check(
                    args.get("names") or args.get("name") or args.get("words") or ""
                )
            if action == "name_scan":
                return await self.tool_name_scan(
                    limit=_as_int(args.get("limit"), 40),
                    min_count=_as_int(args.get("min_count"), 1),
                )
            if action == "text_evidence":
                names = _as_name_list(args.get("name") or args.get("names"), limit=1)
                return await self.tool_text_evidence(names[0] if names else "")
            if action == "web_roster":
                return await self.tool_web_roster(_clip(args.get("query") or "", 60))
            if action == "tune_miner":
                return await self.tool_tune_miner(
                    min_mentions=args.get("min_mentions"),
                    max_characters=args.get("max_characters"),
                    add_stop=args.get("add_stop"),
                    add_names=args.get("add_names") or args.get("names"),
                )
            if action == "avatar_candidates":
                # 保留旧动作：不再在提示词里宣传，但老脚本/测试仍可调用
                names = _as_name_list(args.get("name") or args.get("names"), limit=1)
                return await self.tool_avatar_candidates(names[0] if names else "")
            return {
                "error": f"未知动作 {action}",
                "available": [
                    "miner_report",
                    "name_check",
                    "name_scan",
                    "text_evidence",
                    "web_roster",
                    "tune_miner",
                    "finish",
                ],
            }
        except Exception as exc:  # noqa: BLE001 - 工具异常也作为观察返回给模型
            logger.warning("工具 %s 执行失败：%s", action, exc)
            return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
