"""把角色的初始记忆压成一段摘要（可选走模型，不配 Key 就本地压缩）。

为什么要单独一层：``agent_memory.summarize`` 只负责"存哪、怎么替换"，
真正生成文字的活儿在这里。这样换模型、换 prompt 都不用动记忆层。
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"

SUMMARY_SYSTEM_PROMPT = """你在整理小说角色的「初始记忆摘要」。

输入第一行是「要总结的角色：X」，后面是该角色在原文里出现过的若干段片段，
可能来自同系列的多本书，每行以 - 开头。

**只总结 X 这一个人**。片段里一定还会出现别的人物——他们只是背景，不要把他们
写成摘要的主角，也不要花篇幅描述他们。

压成一段 200 字以内的中文摘要，要求：
- 只保留关于 X 的事实：身份、经历、与他人的关系、说话特点；
- 多本书里重复的信息合并成一条，不要罗列；
- 不要评价、不要编造、不要出现「片段」「摘要」「以上」这类词；
- 直接输出摘要正文，不要任何前缀或解释。"""


#: 对话蒸馏专用：提取**用户**的状态与偏好，不写角色说了什么。
#: 用角色简介那套 prompt 会把对话复述一遍（"X 劝对方先吃饭"），对"了解用户"没用。
MEMORY_SYSTEM_PROMPT = """你在整理「用户画像」。输入是一段用户和角色的聊天记录。

只提取**关于用户本人**的信息，按下面四类归并（没有的类别直接跳过，不要编）：

- 状态：情绪、处境、身体状况（例：最近分手，情绪低落；感冒乏力）
- 事实：工作、生活、计划、身边的人（例：明天要早起上班）
- 偏好：喜欢什么、讨厌什么、雷点（例：怕晚上一个人待着）
- 关系：和角色的信任程度、称呼习惯、相处方式

要求：
- 每条一行，以「· 」开头，最多 5 条；
- 用第二人称或中性陈述（"用户…"），不要写成"角色劝用户…"；
- 不写对话过程、不写角色做了什么；
- 直接输出条目，不要标题、不要解释。"""


def make_llm_summarizer(
    api_key: str,
    model: str = "deepseek-chat",
    system_prompt: str = SUMMARY_SYSTEM_PROMPT,
) -> Optional[Callable[[str], str]]:
    """构造一个同步摘要函数；没 Key / 没装 openai 就返回 None（自动走本地压缩）。"""
    if not api_key or not api_key.strip():
        return None
    try:
        from openai import OpenAI
    except Exception:  # noqa: BLE001
        logger.warning("没有 openai 客户端，记忆摘要回退本地压缩")
        return None

    client = OpenAI(api_key=api_key.strip(), base_url=DEEPSEEK_BASE_URL, timeout=60.0)

    def _summarize(text: str) -> str:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            max_tokens=600,
        )
        return (response.choices[0].message.content or "").strip()

    return _summarize