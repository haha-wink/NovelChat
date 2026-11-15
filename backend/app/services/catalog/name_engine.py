"""人名识别引擎：优先调用现成的中文 NLP 库，装不上才回退内置规则。

为什么要现成的库：中文人名识别（尤其音译名 nrf、日文名 nrj）是成熟问题，
jieba 的词典 + HMM 比自己手写规则准得多。内置规则（services/name_library.py）
只作为兜底，保证「一个库都装不上」时功能仍然可用。

支持的后端（按优先级）：
* ``jieba``  —— 纯 Python、无需编译，最省事，首选
* ``hanlp``  —— 效果好但要下模型（体积大），可选
* ``builtin``—— 内置规则兜底

安装方式见 docs/nlp-engine.md，或直接跑：
    python scripts\\check_nlp.py
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from services.catalog import name_library

logger = logging.getLogger(__name__)

#: jieba 里表示人名的词性
NR_FLAGS = ("nr", "nr1", "nr2", "nrj", "nrf", "nrfg")
#: 其中这些表示"音译/外来人名"
FOREIGN_FLAGS = ("nrf", "nrj")

_lock = threading.Lock()
_cached_engine: Optional["Engine"] = None


class Backend:
    """后端接口。"""

    name = "builtin"

    def available(self) -> bool:
        return True

    def tag(self, text: str) -> list[tuple[str, str]]:
        """返回 [(词, 词性)]。"""
        return []

    def learn(self, names: list[str]) -> int:
        """把已知人名加入分词词典，返回实际加入的数量。"""
        return 0

    def detail(self) -> dict:
        return {"backend": self.name, "available": self.available()}


class BuiltinBackend(Backend):
    """兜底：没有 NLP 库时用内置规则。"""

    name = "builtin"

    def tag(self, text: str) -> list[tuple[str, str]]:
        # 内置规则不产出词性；detect() 会走另一条实现
        return []


class JiebaBackend(Backend):
    """jieba：posseg 给出 nr / nr1 / nr2 / nrj / nrf 词性。"""

    name = "jieba"

    def __init__(self) -> None:
        self._pseg: Any = None
        self._jieba: Any = None
        self._error: str = ""

    def _module(self) -> Any:
        if self._pseg is not None:
            return self._pseg
        import jieba  # noqa: PLC0415 - 延迟导入，没装也不影响其它功能
        import jieba.posseg as pseg  # noqa: PLC0415

        jieba.setLogLevel(logging.WARNING)
        self._jieba = jieba
        self._pseg = pseg
        return pseg

    def available(self) -> bool:
        # 不只是 import：真跑一次分词，确认 (word, flag) 契约可用
        try:
            pseg = self._module()
            sample = [(pair.word, pair.flag) for pair in pseg.cut("路明非说")]
            if not sample:
                raise RuntimeError("posseg.cut 没有返回结果")
            return True
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            return False

    def tag(self, text: str) -> list[tuple[str, str]]:
        pseg = self._module()
        return [(pair.word, pair.flag) for pair in pseg.cut(text)]

    def learn(self, names: list[str]) -> int:
        if not self.available():
            return 0
        added = 0
        for name in names:
            if len(name) < 2:
                continue
            try:
                self._jieba.add_word(name, freq=2000, tag="nr")
                added += 1
            except Exception:  # noqa: BLE001
                continue
        return added

    def detail(self) -> dict:
        info = {"backend": self.name, "available": self.available()}
        if self._error:
            info["error"] = self._error
        return info


class HanLPBackend(Backend):
    """HanLP：效果好但需要下载模型；加载失败就视作不可用。"""

    name = "hanlp"

    def __init__(self) -> None:
        self._pipeline: Any = None
        self._error: str = ""

    def _load(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        import hanlp  # noqa: PLC0415

        # 多任务小模型：分词 + 词性 + NER，第一次调用会下载模型
        self._pipeline = hanlp.load(
            hanlp.pretrained.mtl.CLOSE_TOK_POS_NER_SRL_DEP_SDP_CON_ELECTRA_SMALL_ZH
        )
        return self._pipeline

    def available(self) -> bool:
        try:
            self._load()
            return True
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {str(exc)[:120]}"
            return False

    def tag(self, text: str) -> list[tuple[str, str]]:
        pipeline = self._load()
        result = pipeline([text[:5000]])
        tokens = result.get("tok/fine") or result.get("tok/coarse") or []
        tags = result.get("pos/ctb") or result.get("pos/pku") or []
        if tokens and isinstance(tokens[0], list):
            tokens, tags = tokens[0], (tags[0] if tags and isinstance(tags[0], list) else tags)
        return [(str(word), str(flag)) for word, flag in zip(tokens, tags)]

    def detail(self) -> dict:
        info = {"backend": self.name, "available": self.available()}
        if self._error:
            info["error"] = self._error
        return info


def _make_backend(prefer: str) -> Backend:
    prefer = (prefer or "auto").strip().lower()
    candidates: list[Backend] = []
    if prefer in ("auto", "jieba"):
        candidates.append(JiebaBackend())
    if prefer in ("auto", "hanlp"):
        candidates.append(HanLPBackend())
    candidates.append(BuiltinBackend())

    for backend in candidates:
        if backend.available():
            return backend
    return BuiltinBackend()


class Engine:
    """统一入口：检测名单、判断单个名字。"""

    def __init__(self, backend: Optional[Backend] = None, *, preferred: str = "auto") -> None:
        self.backend = backend or _make_backend(preferred)
        self.preferred = (preferred or "auto").strip().lower()
        self._learned: set[str] = set()

    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.backend.name

    @property
    def is_nlp(self) -> bool:
        """是否用的是真正的 NLP 库（而不是内置规则）。"""
        return self.backend.name != "builtin"

    def info(self) -> dict:
        detail = self.backend.detail()
        detail["is_nlp"] = self.is_nlp
        detail["preferred"] = self.preferred
        detail["learned_names"] = len(self._learned)
        # 指定了 jieba/hanlp 却退到内置规则 -> 明确告诉用户"降级了"
        detail["degraded"] = self.preferred in ("jieba", "hanlp") and not self.is_nlp
        detail["hint"] = (
            ""
            if self.is_nlp
            else "未检测到 jieba/HanLP，正在用内置规则兜底；安装方法：python scripts\\check_nlp.py"
        )
        return detail

    # ------------------------------------------------------------------
    def learn(self, names: list[str]) -> int:
        """把已确认的人名喂进分词词典（只收内置规则认可的，避免污染）。"""
        fresh = [
            name
            for name in dict.fromkeys(names)
            if len(name) >= 2
            and name not in self._learned
            and name_library.analyze(name).score >= 0.4
        ]
        if not fresh:
            return 0
        added = self.backend.learn(fresh)
        self._learned.update(fresh[:added] or fresh)
        return added

    # ------------------------------------------------------------------
    def detect(self, text: str, *, limit: int = 60, min_count: int = 1) -> dict:
        """用 NLP 库扫全文，列出人名（按出现次数降序）。"""
        if not self.is_nlp:
            return {
                "engine": self.name,
                "names": [],
                "note": "未安装 NLP 库，无法扫描；请先安装 jieba（见 scripts/check_nlp.py）",
            }

        counts: dict[str, dict] = {}
        try:
            for word, flag in self.backend.tag(text):
                if not flag or not flag.startswith("nr"):
                    continue
                if flag == "nr1":  # 只是姓氏单字
                    continue
                if len(word) < 2 or len(word) > 12:
                    continue
                if word in name_library.IDENTITY_WORDS or word in name_library.STOP_WORDS:
                    continue
                if any(char in name_library.BAD_CHARS for char in word):
                    continue
                record = counts.setdefault(word, {"name": word, "count": 0, "tag": flag})
                record["count"] += 1
                if flag in FOREIGN_FLAGS:
                    record["tag"] = flag

        except Exception as exc:  # noqa: BLE001 - 库崩了也不能让主流程挂
            logger.warning("NLP 库扫描失败：%s", exc)
            return {"engine": self.name, "names": [], "note": f"扫描失败：{str(exc)[:120]}"}

        names = [item for item in counts.values() if item["count"] >= min_count]
        for item in names:
            base = name_library.analyze(item["name"], text=text)
            item["kind"] = "foreign" if item["tag"] in FOREIGN_FLAGS else (base.kind if base.kind != "unknown" else "cn")
            item["score"] = max(base.score, 0.7)
        names.sort(key=lambda item: (-item["count"], -item["score"]))
        return {
            "engine": self.name,
            "names": names[:limit],
            "total": len(names),
            "note": "nrf/nrj 是音译与日文人名；这是 NLP 库的独立结论，可与算法名单对照",
        }

    # ------------------------------------------------------------------
    def classify(self, name: str, *, text: str = "") -> dict:
        """判断单个名字：内置规则打底，NLP 库认出来就提升可信度/修正类型。"""
        base = name_library.lookup(name, text=text)
        base["engine"] = self.name
        base["engine_tag"] = ""
        if not self.is_nlp or len(name) < 2:
            return base

        tag = ""
        try:
            for word, flag in self.backend.tag(name):
                if word == name and flag.startswith("nr"):
                    tag = flag
                    break
        except Exception:  # noqa: BLE001
            return base
        if not tag:
            return base

        base["engine_tag"] = tag
        if tag in FOREIGN_FLAGS:
            base["kind"] = "foreign"
            base["score"] = max(base["score"], 0.8)
            base["plausible"] = True
            base["reasons"] = [*base["reasons"], f"NLP 库标注 {tag}（音译/日文人名）"]
        elif tag in ("nr", "nr2", "nrfg"):
            if base["kind"] == "unknown":
                base["kind"] = "cn"
            if not base["plausible"]:
                base["score"] = max(base["score"], 0.6)
                base["plausible"] = True
                base["reasons"] = [*base["reasons"], f"NLP 库标注 {tag}（人名）"]
        return base

    def classify_many(self, names: list[str], *, text: str = "") -> list[dict]:
        return [self.classify(name, text=text) for name in names]


def get_engine(prefer: Optional[str] = None) -> Engine:
    """进程内单例（jieba 加载词典较慢，缓存住）。"""
    global _cached_engine
    with _lock:
        if _cached_engine is None:
            if prefer is None:
                try:
                    from core.config import get_settings

                    prefer = get_settings().name_engine
                except Exception:  # noqa: BLE001 - 配置没就绪时用 auto
                    prefer = "auto"
            _cached_engine = Engine(_make_backend(prefer), preferred=prefer)
            info = _cached_engine.info()
            if info.get("degraded"):
                logger.warning(
                    "NAME_ENGINE=%s 但没装成功（%s），已降级为内置规则；"
                    "安装方法：python scripts\\check_nlp.py",
                    prefer,
                    info.get("error", "未安装"),
                )
            logger.info("人名引擎：%s", info)
        return _cached_engine


def reset_engine() -> None:
    """测试用：丢掉缓存的引擎。"""
    global _cached_engine
    with _lock:
        _cached_engine = None


def install_hint() -> str:
    return (
        "安装人名识别库（推荐 jieba，纯 Python 无需编译）：\n"
        "  python -m pip install jieba\n"
        "  国内镜像：python -m pip install jieba -i https://pypi.tuna.tsinghua.edu.cn/simple\n"
        "装完跑：python scripts\\check_nlp.py 验证；也可以把 wheel 下载下来离线装。"
    )