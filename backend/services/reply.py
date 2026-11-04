"""回复后处理：去掉动作/神态描写。

提示词已经把"不要写动作"写进规则了，但模型仍会偶发：

```
从桌上抬起头来，揉了揉眼睛，把嘴角的纸巾扯下来
在、在的的在的——刚才谁喊我？
```

（第一行就是模型写的动作旁白，用户看着很出戏。）

这里是**第二道保险**：把整行都是动作的删掉、行内的 ``*动作*`` 摘掉。
只处理"整行/整段都是旁白"的情况，不碰正文里的括号补充说明。
"""

from __future__ import annotations

import re

#: 整行就是 *动作* 或 （动作）——短、且被成对符号包裹
_STAGE_LINE = re.compile(
    r"^\s*(?:[*＊][^*＊\n]{1,60}[*＊]|[（(][^（()）\n]{1,40}[)）])\s*$"
)

#: 行内的 *动作* 片段（负向断言避开 **加粗**）
_INLINE_STAGE = re.compile(r"(?<![*＊])[*＊](?!\*)([^*＊\n]{1,40})[*＊](?!\*)")

#: 行首的括号动作，后面还跟着正文
_LEADING_PAREN = re.compile(r"^\s*[（(][^（()）\n]{1,30}[)）]\s*")

#: 连续空白
_MULTI_SPACE = re.compile(r"[ \t\u3000]{2,}")
_MULTI_BLANK = re.compile(r"\n{3,}")


def has_stage_directions(text: str) -> bool:
    """这段回复里有没有动作旁白（排查用）。"""
    content = str(text or "")
    if not content.strip():
        return False
    return any(_STAGE_LINE.match(line) for line in content.split("\n")) or bool(
        _INLINE_STAGE.search(content)
    )


def strip_stage_directions(text: str) -> str:
    """删掉动作旁白，返回干净的对话文本。

    处理三件事：

    1. 整行都是 ``*……*`` 或 ``（……）`` 的行 -> 整行删；
    2. 行内的 ``*……*`` 片段 -> 摘掉；
    3. 行首的 ``（……）`` -> 摘掉（只摘短的，避免误伤正文括号）。
    """
    content = str(text or "")
    if not content.strip():
        return ""

    kept: list[str] = []
    for line in content.split("\n"):
        if not line.strip():
            kept.append("")
            continue
        if _STAGE_LINE.match(line):
            continue
        line = _INLINE_STAGE.sub("", line)
        line = _LEADING_PAREN.sub("", line)
        line = _MULTI_SPACE.sub(" ", line).rstrip()
        if line.strip():
            kept.append(line)

    result = "\n".join(kept)
    result = _MULTI_BLANK.sub("\n\n", result).strip()
    return result