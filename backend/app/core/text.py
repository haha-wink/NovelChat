"""极小的中文分词底座：给 RAG 索引用，不依赖 jieba / torch / 任何模型。

## 为什么不直接上语义向量

真正的 RAG 用 embedding 做语义检索，但这条路上有两个硬约束：

1. **不能引 torch**——项目当初为了装包体验特意把 torch / sentence-transformers 去掉了；
2. **下不来模型**——HuggingFace / jsdelivr / GitHub 在这台机器上连接会被重置。

所以默认后端做成**词法检索（BM25）**：中文按"字二元组 + 拉丁词"切，完全离线、
结果可复现、零依赖。它在"同一个人的记忆里找相关片段"这个场景下并不弱——角色记忆
本来就短、专有名词多（人名/地名），词法命中比语义向量更稳。

``services/rag.py`` 留了 ``embedder`` 注入点：以后能下模型或接 embedding API 时，
换成向量检索即可，上层调用方式不变。
"""

from __future__ import annotations

import re

#: 中文取连续汉字段，再切成二元组
_HAN_RUN = re.compile(r"[\u4e00-\u9fff]+")
#: 拉丁字母/数字连续段当成一个词
_LATIN_RUN = re.compile(r"[a-zA-Z0-9_]+")

#: 高频虚词组成的二元组，留着只会稀释权重
STOP_BIGRAMS = {
    "的时", "时候", "什么", "怎么", "这个", "那个", "自己", "一个", "没有",
    "就是", "还是", "因为", "所以", "但是", "可以", "已经", "这样", "那样",
    "他们", "我们", "你们", "知道", "觉得", "不过", "如果", "然后", "真的",
}

#: 单字停用（做单字兜底时用）
STOP_CHARS = set("的了是在和就都而及与着或一个上不我你他她它这那")


def tokenize(text: str) -> list[str]:
    """把一段中文切成检索用的 token。

    * 中文：连续汉字段切成 **二元组**（"路明非" -> 路明 / 明非），单字段额外保留单字；
    * 拉丁/数字：整词保留并小写；
    * 过滤停用二元组与纯虚词。
    """
    text = str(text or "")
    if not text:
        return []

    tokens: list[str] = []
    for run in _HAN_RUN.findall(text):
        if len(run) == 1:
            if run not in STOP_CHARS:
                tokens.append(run)
            continue
        for index in range(len(run) - 1):
            bigram = run[index : index + 2]
            if bigram not in STOP_BIGRAMS:
                tokens.append(bigram)
        # 单字兜底：只对 2~3 字的短词补单字，长句补了噪声太大
        if len(run) <= 3:
            for char in run:
                if char not in STOP_CHARS:
                    tokens.append(char)

    for word in _LATIN_RUN.findall(text):
        lowered = word.lower()
        if len(lowered) >= 2:
            tokens.append(lowered)

    return tokens


def term_frequency(tokens: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return counts