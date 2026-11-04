"""正文清洗：去掉盗版站广告、水印、重复页眉页脚、分隔线。

## 为什么要单独一层

抽取出来的角色记忆里混着这些东西：

```
--------------------
格格书海：本书校对不易，如有乱码，错误的地方，请联系客服解决！
小店生存艰难，我们尽力做到最好，愿您不要轻易差评，谢谢您的理解！
```

它们有两个危害：

1. **污染 RAG 检索**——这类文本跟任何查询都有中等相似度，会把真正相关的片段挤下去；
2. **浪费 token**——进提示词时占位置，还诱导模型跑偏。

## 两层清洗

```
clean_text()     入库时清洗：source.txt / chunks / 抽取 全程受益
clean_memories() 存量清洗：已经抽好的记忆原地改写，不重抽、不动角色 id
```

第二层是关键：用户的角色已经调过、头像换过，**不能重抽**（会全变新 id）。
所以提供一条"只洗记忆、不重抽"的路径。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from core import db
from core.text import tokenize

logger = logging.getLogger(__name__)

#: 一行里命中这些就整行删掉（盗版站广告/求票/水印）
AD_LINE_PATTERNS = (
    r"格格书海",
    r"本书由.{0,12}(整理|提供|发布)",
    r"(更多|最新|精彩).{0,6}(章节|小说|内容).{0,10}(请|尽在|访问)",
    r"请记住(本站|本书|域名)",
    r"(手机|移动|wap|WAP).{0,6}(阅读|用户|站)",
    r"(无弹窗|全文阅读|免费阅读|txt下载|电子书下载)",
    r"(笔趣阁|顶点小说|新笔趣|书趣阁|八一中文|uu看书)",
    r"(求|跪求).{0,4}(月票|推荐票|收藏|订阅|打赏|好评)",
    r"(本章未完|点击下一页|翻页继续)",
    r"(校对|精校|手打|录入).{0,10}(不易|组|团队)",
    r"(客服|差评|小店生存|盗版|侵权).{0,20}(解决|理解|删除|联系)",
    r"www\.[a-z0-9\-]+\.(com|net|org|cc|info|cn)",
    r"https?://",
)

#: 行内广告片段：只删片段，不删整行（避免误伤正文）
INLINE_PATTERNS = (
    r"[（(【\[]?\s*本章完\s*[)）】\]]?",
    r"[（(【\[]?\s*(未完待续|待续)\s*[)）】\]]?",
    r"[（(【\[]?\s*请翻页[^）)】\]]{0,20}[)）】\]]?",
    r"[（(【\[]?\s*(手机|电脑)用户[^）)】\]]{0,20}[)）】\]]?",
    r"[（(【\[]?\s*字数[：:]\s*\d+\s*[)）】\]]?",
)

_AD_LINE = re.compile("|".join(AD_LINE_PATTERNS), re.IGNORECASE)
_INLINE = re.compile("|".join(INLINE_PATTERNS))

#: 只有这些字符组成的行 = 分隔线
_SEPARATOR = re.compile(r"^\s*[-=_*~·—+#\s]{4,}\s*$")
#: 零宽字符 / BOM
_INVISIBLE = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff]")
#: 全角空格连用
_MULTI_SPACE = re.compile(r"[ \t\u3000]{2,}")
#: 三个以上连续空行
_MULTI_BLANK = re.compile(r"\n{3,}")

#: 重复出现这么多次的短行，判为页眉页脚
REPEAT_THRESHOLD = 5
#: 判定为"短行"的长度上限
SHORT_LINE = 50


def looks_like_ad(line: str) -> bool:
    """单行是否像广告/水印。"""
    text = line.strip()
    if not text:
        return False
    return bool(_AD_LINE.search(text))


def normalize(text: str) -> str:
    """去不可见字符、统一换行、压缩空白。"""
    content = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    content = _INVISIBLE.sub("", content)
    content = content.replace("\u3000", " ")
    content = _MULTI_SPACE.sub(" ", content)
    return content


def _strip_inline(line: str) -> str:
    cleaned = _INLINE.sub("", line)
    return cleaned.rstrip()


def _strip_ads(line: str) -> tuple[str, bool]:
    """按**句子**剔除广告，返回 ``(剩下的内容, 是否整行都是广告)``。

    为什么不能整行删：盗版站的广告经常和正文粘在同一行里——

    ```
    路明非推开门，屋外下着大雨。格格书海：本书校对不易，请联系客服解决！
    ```

    整行删会把正文一起删掉（第一版就是这么错的）。按句号切开只丢广告那句。
    """
    pieces = re.split(r"(?<=[。！？!?])", line)
    kept: list[str] = []
    for piece in pieces:
        if not piece.strip():
            continue
        if _AD_LINE.search(piece):
            continue
        kept.append(piece)
    return "".join(kept), not kept


def clean_text(text: str, *, repeat_threshold: int = REPEAT_THRESHOLD) -> tuple[str, dict]:
    """清洗一段正文，返回 ``(清洗后文本, 统计)``。"""
    content = normalize(text)
    if not content.strip():
        return "", {
            "original_chars": len(text or ""),
            "cleaned_chars": 0,
            "removed_chars": len(text or ""),
            "removed_lines": 0,
            "deduped_lines": 0,
            "removed_ratio": 1.0,
        }

    lines = content.split("\n")
    original_chars = len(content)

    # 1) 统计"高频短行"——页眉页脚通常整本书反复出现
    counts: dict[str, int] = {}
    for line in lines:
        key = line.strip()
        if 0 < len(key) <= SHORT_LINE:
            counts[key] = counts.get(key, 0) + 1
    boilerplate = {
        key for key, count in counts.items()
        if count >= repeat_threshold and not looks_like_ad(key)
    }

    kept: list[str] = []
    removed_lines = 0
    deduped_lines = 0
    for line in lines:
        stripped = line.strip()

        # 空行先留着，最后统一折叠
        if not stripped:
            kept.append("")
            continue

        if _SEPARATOR.match(stripped):
            removed_lines += 1
            continue

        if stripped in boilerplate:
            deduped_lines += 1
            continue

        # 先按句删广告，再处理行内残留（本章完/待续这类）
        without_ads, all_ad = _strip_ads(line)
        if all_ad:
            removed_lines += 1
            continue

        cleaned = _strip_inline(without_ads)
        if not cleaned.strip():
            removed_lines += 1
            continue
        kept.append(cleaned)

    result = "\n".join(kept)
    result = _MULTI_BLANK.sub("\n\n", result).strip()

    cleaned_chars = len(result)
    return result, {
        "original_chars": original_chars,
        "cleaned_chars": cleaned_chars,
        "removed_chars": max(0, original_chars - cleaned_chars),
        "removed_lines": removed_lines,
        "deduped_lines": deduped_lines,
        "removed_ratio": round(
            max(0.0, (original_chars - cleaned_chars) / max(1, original_chars)), 4
        ),
    }


# ---------------------------------------------------------------------------
# 存量清洗：只洗记忆，不重抽（保住角色 id / 头像 / 用户改过的字段）
# ---------------------------------------------------------------------------
def clean_memories(
    *, dry_run: bool = True, min_removed_chars: int = 40, character_id: Optional[str] = None
) -> dict:
    """把已抽好的记忆条目原地清洗一遍。

    ``character_id`` 可以限定只洗某个角色——测试和"只修一个人"时用得上，
    不传才是全库。**真清洗会改数据，默认 dry_run**。

    改动了 content 的条目会把 ``vector`` 置空、块向量删掉，等 rag 重建索引——
    否则检索还在用旧内容的向量。
    """
    with db.connect() as conn:
        if character_id:
            rows = conn.execute(
                "SELECT id, character_id, content FROM character_memory_items "
                "WHERE character_id = ?",
                (character_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, character_id, content FROM character_memory_items"
            ).fetchall()

    changed = 0
    removed_chars = 0
    samples: list[dict] = []

    for row in rows:
        content = row["content"] or ""
        cleaned, stats = clean_text(content)
        if not cleaned or cleaned == content:
            continue
        # 变化太小的不动——避免为了几十个字重算向量
        if stats["removed_chars"] < min_removed_chars:
            continue

        changed += 1
        removed_chars += stats["removed_chars"]
        if len(samples) < 5:
            samples.append(
                {
                    "item_id": row["id"],
                    "before": content[:80],
                    "after": cleaned[:80],
                    "removed": stats["removed_chars"],
                }
            )
        if not dry_run:
            db.update_memory_content(row["id"], cleaned)

    return {
        "dry_run": dry_run,
        "scanned": len(rows),
        "changed": changed,
        "removed_chars": removed_chars,
        "samples": samples,
    }