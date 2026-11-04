"""情绪极性模型：规则后端 + 本地小模型后端（可插拔）。

为什么需要这一层，而不是全靠 ``affect.py`` 的词表：

``affect.py`` 识别的是**言语行为**（道谢、威胁、失约……），取值离散、命中即确定；
但真实对话里大量是「含蓄」「反讽」「长句」，词表读不懂。这时才需要一层
**情绪极性**判断来兜底。

三层漏斗：
    ① 规则后端（永远可用，零依赖）—— ``RulesBackend``
    ② 本地小模型（可选）—— ``OnnxBackend``，ONNX Runtime 纯 CPU 推理
    ③ 都没有结论 —— 调用方不做任何改动（只回落到基线）

**为什么是 ONNX 而不是再上一个 torch 分类头**：torch 虽然为了 RAG 已经装上了，
但情绪判断是**每轮都要跑**的热路径。ONNX Runtime + 量化后的 int8 模型内存占用只有
torch 的几分之一、单次推理十几毫秒，且不占 torch 的线程池，不会和嵌入模型抢 CPU。

用法::

    from services import emotion_model

    backend = emotion_model.get_backend()      # auto: onnx 可用则 onnx，否则 rules
    signal = backend.analyze("我今天真的撑不住了")
    if signal:
        print(signal.polarity, signal.confidence)

配置：``EMOTION_MODEL = auto | onnx | rules``（默认 auto），``EMOTION_ONNX_DIR`` 可指定模型目录。
模型文件缺失时自动降级，不报错——和 ``name_engine`` 的 jieba/HanLP/builtin 完全同构。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: 正向情绪词（情绪本身，不是"言语行为"）
POSITIVE_WORDS: dict[str, float] = {
    "开心": 1.0, "高兴": 1.0, "快乐": 1.0, "幸福": 1.0, "满足": 0.8,
    "喜欢": 0.9, "爱": 0.9, "温暖": 0.8, "舒服": 0.7, "安心": 0.8,
    "期待": 0.8, "兴奋": 0.9, "激动": 0.8, "感动": 0.9, "欣慰": 0.8,
    "谢谢": 0.6, "感谢": 0.7, "厉害": 0.6, "佩服": 0.7, "可爱": 0.8,
    "好听": 0.6, "好看": 0.6, "美好": 0.7, "希望": 0.6, "放心": 0.6,
}

#: 负向情绪词
NEGATIVE_WORDS: dict[str, float] = {
    "难过": 1.0, "伤心": 1.0, "痛苦": 1.0, "绝望": 1.0, "崩溃": 1.0,
    "生气": 0.9, "愤怒": 1.0, "恼火": 0.9, "烦": 0.7, "烦躁": 0.8,
    "害怕": 0.9, "恐惧": 0.9, "不安": 0.7, "紧张": 0.7, "焦虑": 0.8,
    "讨厌": 0.9, "恶心": 0.9, "失望": 0.9, "委屈": 0.85, "孤独": 0.8,
    "累": 0.6, "疲惫": 0.7, "撑不住": 1.0, "难受": 0.8, "沮丧": 0.9,
}

#: 程度副词：命中后情绪强度放大
INTENSIFIERS = ("很", "非常", "特别", "太", "真的", "超级", "极其", "十分", "极其")

#: 否定词：出现在情绪词前时反转极性。
#: 注意**不要**放单字「别」——它会命中「特别」里的「别」，把"特别开心"判成负向。
NEGATIONS = ("不", "没", "没有", "不要", "并不", "从不", "不太", "毫无")

#: 一段文本里看多少个字符来判定"否定/程度"
_CONTEXT_WINDOW = 3


@dataclass(frozen=True)
class EmotionSignal:
    """一次情绪极性判断的结果。"""

    polarity: float      # -1（极负）~ 1（极正）
    intensity: float     # 0~1，情绪的强烈程度
    confidence: float    # 0~1，判断的可信度（命中越多越高）
    backend: str = "rules"
    evidence: tuple[str, ...] = ()


class Backend:
    """后端接口。"""

    name = "rules"

    def available(self) -> bool:
        return True

    def analyze(self, text: str) -> Optional[EmotionSignal]:
        return None


class RulesBackend(Backend):
    """词典 + 程度修饰 + 否定翻转。零依赖、毫秒级。"""

    name = "rules"

    def analyze(self, text: str) -> Optional[EmotionSignal]:
        text = (text or "").strip()
        if not text:
            return None

        positive = 0.0
        negative = 0.0
        hits: list[str] = []

        for word, weight in POSITIVE_WORDS.items():
            count = text.count(word)
            if count:
                positive += weight * count * self._modifier(text, word)
                hits.append(word)

        for word, weight in NEGATIVE_WORDS.items():
            count = text.count(word)
            if count:
                negative += weight * count * self._modifier(text, word)
                hits.append(word)

        # 被否定的正向词会变成负数；用绝对值做分母，避免正负抵消后误判为"没有情绪"
        denominator = abs(positive) + abs(negative)
        if denominator <= 0:
            return None

        polarity = (positive - negative) / denominator
        intensity = min(1.0, denominator / 3.0)
        confidence = min(1.0, len(hits) / 2.0)
        return EmotionSignal(
            polarity=round(polarity, 3),
            intensity=round(intensity, 3),
            confidence=round(confidence, 3),
            backend=self.name,
            evidence=tuple(hits[:6]),
        )

    @staticmethod
    def _modifier(text: str, word: str) -> float:
        """看情绪词前面几个字：有否定词则反号，有程度副词则放大。"""
        index = text.find(word)
        if index <= 0:
            return 1.0
        context = text[max(0, index - _CONTEXT_WINDOW) : index]
        factor = 1.0
        if any(neg in context for neg in NEGATIONS):
            factor = -1.0
        if any(prefix in context for prefix in INTENSIFIERS):
            factor *= 1.6
        return factor


#: 默认模型目录（scripts/fetch_emotion_model.py 下载并导出到这里）
DEFAULT_MODEL_DIR = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "models"
    / "erlangshen-sentiment"
)

#: 输入截断长度。对话消息都很短，128 足够，也省时间
MAX_TOKENS = 128


def model_dir() -> Path:
    """模型目录：优先环境变量/配置，其次默认位置。"""
    configured = (os.environ.get("EMOTION_ONNX_DIR") or "").strip()
    if not configured:
        try:
            from core.config import get_settings

            configured = (getattr(get_settings(), "emotion_onnx_dir", "") or "").strip()
        except Exception:  # noqa: BLE001 - 配置读不到就用默认目录
            configured = ""
    return Path(configured) if configured else DEFAULT_MODEL_DIR


def _preferred_mode() -> str:
    """后端选择：配置优先，其次环境变量，最后 auto。"""
    try:
        from core.config import get_settings

        mode = (getattr(get_settings(), "emotion_model", "") or "").strip().lower()
        if mode:
            return mode
    except Exception:  # noqa: BLE001
        pass
    return (os.environ.get("EMOTION_MODEL") or "auto").strip().lower()


def _softmax(logits: list[float]) -> list[float]:
    import math

    peak = max(logits) if logits else 0.0
    exponents = [math.exp(value - peak) for value in logits]
    total = sum(exponents) or 1.0
    return [value / total for value in exponents]


class OnnxBackend(Backend):
    """本地情感分类小模型（ONNX Runtime，纯 CPU）。

    模型：``lxyuan/distilbert-base-multilingual-cased-sentiments-student``
    （多语种情感分类，含中文；量化后约 130MB）。

    输出三类概率（negative / neutral / positive），折算成 ``EmotionSignal``：

    ``polarity``  = P(正) - P(负)
    ``intensity`` = 1 - P(中性)      —— 越不中性越强烈
    ``confidence``= 最大类别概率

    **只在规则后端没把握时才被调用**（见 ``affect._needs_model``），
    所以正常闲聊根本不会走到这里，热路径的延迟不受影响。
    """

    name = "onnx"

    def __init__(self) -> None:
        self._session = None
        self._tokenizer = None
        self._labels: dict[int, str] = {0: "negative", 1: "neutral", 2: "positive"}
        self._input_names: tuple[str, ...] = ()
        self._error: Optional[str] = None
        self._tried = False
        self._lock = threading.Lock()
        self._calls = 0
        self._total_ms = 0.0
        self._last_ms = 0.0

    # -- 文件 / 可用性 ----------------------------------------------------
    def model_file(self) -> Optional[Path]:
        base = model_dir() / "onnx"
        for name in ("model_quantized.onnx", "model.onnx"):
            candidate = base / name
            if candidate.exists():
                return candidate
        return None

    def available(self) -> bool:
        try:
            import onnxruntime  # noqa: F401
        except Exception:  # noqa: BLE001
            self._error = "没有安装 onnxruntime"
            return False
        if self.model_file() is None:
            self._error = f"模型文件不存在：{model_dir() / 'onnx'}"
            return False
        return True

    # -- 加载 -------------------------------------------------------------
    def _ensure_loaded(self) -> bool:
        if self._session is not None:
            return True
        if self._tried:
            return False
        with self._lock:
            if self._session is not None:
                return True
            if self._tried:
                return False
            self._tried = True
            try:
                import onnxruntime as ort

                from core.hf_import import load as load_hf

                AutoTokenizer = load_hf()["AutoTokenizer"]

                started = time.perf_counter()
                path = self.model_file()
                if path is None:
                    self._error = "模型文件不存在"
                    return False

                options = ort.SessionOptions()
                options.intra_op_num_threads = 2  # 别和嵌入模型抢线程
                self._session = ort.InferenceSession(
                    str(path), options, providers=["CPUExecutionProvider"]
                )
                self._input_names = tuple(i.name for i in self._session.get_inputs())
                self._tokenizer = AutoTokenizer.from_pretrained(str(model_dir()))
                self._labels = self._read_labels()
                logger.info(
                    "情绪模型（ONNX）就绪：%s，加载 %.0f ms",
                    path.name,
                    (time.perf_counter() - started) * 1000,
                )
                return True
            except Exception as exc:  # noqa: BLE001 - 模型问题不能影响聊天
                self._error = str(exc)[:200]
                logger.warning("情绪模型（ONNX）加载失败，回退规则后端：%s", self._error)
                self._session = None
                return False

    def _read_labels(self) -> dict[int, str]:
        """从 config.json 读 id2label；读不到就用 negative/neutral/positive。"""
        import json

        for candidate in (model_dir() / "onnx" / "config.json", model_dir() / "config.json"):
            if not candidate.exists():
                continue
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            mapping = raw.get("id2label") or {}
            if mapping:
                return {int(key): str(value).lower() for key, value in mapping.items()}
        return {0: "negative", 1: "neutral", 2: "positive"}

    # -- 推理 -------------------------------------------------------------
    def analyze(self, text: str) -> Optional[EmotionSignal]:
        content = (text or "").strip()
        if not content:
            return None
        if not self._ensure_loaded():
            return None

        try:
            started = time.perf_counter()
            batch = self._tokenizer(
                content,
                truncation=True,
                max_length=MAX_TOKENS,
                return_tensors="np",
            )
            feeds = {
                name: batch[name]
                for name in self._input_names
                if name in batch
            }
            logits = self._session.run(None, feeds)[0][0]
            probabilities = _softmax([float(value) for value in logits])

            positive = 0.0
            negative = 0.0
            for index, probability in enumerate(probabilities):
                label = self._labels.get(index, str(index)).lower()
                if "pos" in label:
                    positive += probability
                elif "neg" in label:
                    negative += probability

            elapsed = (time.perf_counter() - started) * 1000
            self._calls += 1
            self._last_ms = elapsed
            self._total_ms += elapsed

            top_label = max(
                (
                    (self._labels.get(index, str(index)).lower(), probability)
                    for index, probability in enumerate(probabilities)
                ),
                key=lambda item: item[1],
            )[0]
            return EmotionSignal(
                polarity=round(max(-1.0, min(1.0, positive - negative)), 3),
                # 二分类没有"中性"类，用主导类概率当强度；三分类也能表达"有多不确定"
                intensity=round(max(0.0, min(1.0, max(positive, negative))), 3),
                confidence=round(max(probabilities) if probabilities else 0.0, 3),
                backend=self.name,
                evidence=(top_label,),
            )
        except Exception:  # noqa: BLE001 - 推理失败绝不能影响聊天
            logger.exception("情绪模型推理失败（已忽略）")
            return None

    def stats(self) -> dict:
        average = (self._total_ms / self._calls) if self._calls else 0.0
        return {
            "calls": self._calls,
            "last_ms": round(self._last_ms, 2),
            "avg_ms": round(average, 2),
        }

    def status(self) -> dict:
        path = self.model_file()
        return {
            "model_file": str(path) if path else None,
            "model_exists": path is not None,
            "loaded": self._session is not None,
            "error": self._error,
            "labels": sorted(set(self._labels.values())),
            "stats": self.stats(),
        }


_rules_backend = RulesBackend()
_onnx_backend = OnnxBackend()
_lock = threading.Lock()
_resolved: Optional[Backend] = None


def get_backend(preferred: Optional[str] = None) -> Backend:
    """按配置选后端。``auto`` 时能用 onnx 就用，否则 rules。"""
    global _resolved
    mode = (preferred or _preferred_mode() or "auto").strip().lower()

    with _lock:
        if mode == "rules":
            return _rules_backend
        if mode == "onnx":
            if _onnx_backend.available():
                return _onnx_backend
            logger.warning("EMOTION_MODEL=onnx 但本地模型不可用，回退 rules")
            return _rules_backend
        # auto：只解析一次
        if _resolved is None:
            _resolved = _onnx_backend if _onnx_backend.available() else _rules_backend
            logger.info("情绪模型后端：%s", _resolved.name)
        return _resolved


def analyze(text: str, *, preferred: Optional[str] = None) -> Optional[EmotionSignal]:
    """便利函数：直接用当前后端分析一段文本。"""
    return get_backend(preferred).analyze(text)


def info() -> dict:
    """给 ``/api/health`` 用（照抄 name_engine 的做法）。"""
    backend = get_backend()
    return {
        "backend": backend.name,
        "onnx_available": _onnx_backend.available(),
        "rules_available": True,
        "onnx": _onnx_backend.status(),
    }