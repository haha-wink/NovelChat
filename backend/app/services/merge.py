"""同一书架里「同一个人」合并成一个 Agent。

## 场景

书架里放了同系列两本书，《龙族Ⅰ》和《龙族Ⅱ》各抽出一个「路明非」。不合并的话，
群里有两个路明非，记忆还分成两半——用户问"你还记得那晚吗"，两个分身各答一半。

## 合并流程

```
1. 归一化：名字/别名去掉 · 空格 标点，作为"身份键"
2. 分组：身份键有交集就算同一个人（并查集，支持传递合并）
3. 选本体：出场次数最多的（并列取更早入库的）
4. 迁移：其它角色的记忆条目整体搬到本体，**source_book_id 保持原样**
5. 标记：被合并的角色写 canonical_id，不再作为独立成员出现
6. 摘要：本体把跨书记忆压成一段（可注入 summarizer，缺省本地压缩）
```

## 删书为什么能回档

原始条目带来源，删书时按 `source_book_id` 回收 → 本体只剩另一本书的记忆 →
重新摘要即可。**不需要"反合并"记录**，也永远不会丢数据。

这条依赖 P1 的一个决定：记忆按条存储且带来源。如果当初把 excerpts 存成一个
JSON 数组、或者摘要直接覆盖原文，这一步就做不回来了。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from core import db
from services import agent_log
from services import agent_memory

logger = logging.getLogger(__name__)

#: 名字归一化时要抹掉的字符（间隔号、标点、空白）
_PUNCTUATION = re.compile(r"[\s·・.。\"'“”‘’（）()\[\]【】《》,，、:：;；!！?？\-—_/\\]+")

#: 归一化后太短的不当身份键（避免"我""他"这类单字误合并）
MIN_KEY_LENGTH = 2

#: 通用称呼不能当身份键。
#: 真实数据里「老大」既是赵孟华的别名，也是恺撒·加图索的别名——靠"别名相交"合并，
#: 会把两个完全不同的人并成一个。所以这类词既不参与别名匹配，也不参与主名匹配。
GENERIC_KEYS = frozenset(
    {
        "老大", "老二", "老三", "小哥", "大哥", "大姐", "小妹", "老板", "老师",
        "同学", "学长", "学姐", "队长", "班长", "掌柜", "师父", "师兄", "师姐",
        "主任", "经理", "医生", "警官", "教授", "校长", "院长", "公主", "王子",
        "大人", "前辈", "姑娘", "公子", "少爷", "小姐", "夫人", "老爷", "掌门",
        "长老", "宗主", "帮主", "城主", "族长", "兄弟", "丫头", "小家伙",
    }
)


def normalize(text: str) -> str:
    return _PUNCTUATION.sub("", str(text or "")).lower()


def _key(text: str) -> str:
    """归一化后能当身份键就返回，否则空串（太短或通用称呼）。"""
    key = normalize(text)
    if len(key) < MIN_KEY_LENGTH or key in GENERIC_KEYS:
        return ""
    return key


def primary_key(character: dict) -> str:
    """主名字的身份键——合并只认它，不认任意别名。"""
    return _key(character.get("name") or "")


def alias_keys(character: dict) -> set[str]:
    keys: set[str] = set()
    for alias in character.get("aliases") or []:
        key = _key(alias)
        if key:
            keys.add(key)
    return keys


def identity_keys(character: dict) -> set[str]:
    """该角色的全部可用身份键（主名 + 别名），给排查/测试用。"""
    keys = alias_keys(character)
    primary = primary_key(character)
    if primary:
        keys.add(primary)
    return keys


def same_person(left: dict, right: dict) -> bool:
    """是不是同一个人。

    **必须有一方的主名参与**：
      * 两个主名相同 —— 路明非 == 路明非
      * 一方主名出现在另一方别名里 —— 恺撒 与 恺撒·加图索（别名含"恺撒")

    只看"别名之间有交集"是不够的：不同的人可能共享同一个昵称（例如都叫"老大"）。
    """
    left_primary = primary_key(left)
    right_primary = primary_key(right)
    if left_primary and left_primary == right_primary:
        return True
    if left_primary and left_primary in alias_keys(right):
        return True
    if right_primary and right_primary in alias_keys(left):
        return True
    return False


def group_duplicates(
    characters: list[dict], *, extra_pairs: Optional[list[set[str]]] = None
) -> list[list[dict]]:
    """把"同一个人"分到一组（并查集，支持 A~B、B~C 推出 A~C）。

    ``extra_pairs`` 是"管理 Agent 以前确认过是同一人"的名字集合，
    例如 ``[{"诺诺", "陈墨瞳"}]``——名字规则认不出来，但经验里已经定过。
    """
    parent = {character["id"]: character["id"] for character in characters}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for index, left in enumerate(characters):
        for right in characters[index + 1 :]:
            if same_person(left, right):
                union(left["id"], right["id"])

    # 已确认的经验：名字集合命中两个及以上角色就并到一起
    for pair in extra_pairs or []:
        if len(pair) < 2:
            continue
        hit = [
            character["id"]
            for character in characters
            if identity_keys(character) & pair
        ]
        for other in hit[1:]:
            union(hit[0], other)

    buckets: dict[str, list[dict]] = {}
    for character in characters:
        buckets.setdefault(find(character["id"]), []).append(character)
    return [group for group in buckets.values() if len(group) > 1]


# ---------------------------------------------------------------------------
# 相似度：名字不同的情况下，判断"可能是同一个人"
# ---------------------------------------------------------------------------
#: 三个信号的权重（关系网与性格权重最高，身份描述只是辅助）
_W_CIRCLE = 0.30
_W_PERSONALITY = 0.45
_W_PROFILE = 0.25

#: 低于这个分数不进候选：既不合并也不去问模型，省得白花判定次数
SIMILARITY_FLOOR = 0.5

#: 高于这个分数**直接合并**，不再询问。
#: 0.7 意味着三个信号里至少两个高度吻合（例如同一圈子 + 同一套性格标签），
#: 已经足够可信；介于 FLOOR 与 AUTO 之间的才需要判定或人工确认。
AUTO_MERGE_SCORE = 0.7


def _personality_tags(character: dict) -> set[str]:
    return {
        normalize(tag)
        for tag in (character.get("personality") or [])
        if normalize(tag)
    }


def _relationship_names(character: dict) -> set[str]:
    names: set[str] = set()
    for relation in character.get("relationships") or []:
        if isinstance(relation, dict):
            target = relation.get("with") or relation.get("name") or relation.get("target") or ""
        else:
            target = relation
        key = normalize(str(target))
        if key and len(key) >= 2:
            names.add(key)
    return names


def _profile_tokens(character: dict) -> set[str]:
    """身份描述（role / background / speech_style）的字二元组，用来算粗糙的重合度。"""
    text = " ".join(
        str(character.get(field) or "")
        for field in ("role", "background", "speech_style")
    )
    cleaned = normalize(text)
    if len(cleaned) < 2:
        return set()
    return {cleaned[index: index + 2] for index in range(len(cleaned) - 1)}


def _jaccard(left: set[str], right: set[str]) -> Optional[float]:
    if not left or not right:
        return None
    return len(left & right) / len(left | right)


def similarity(left: dict, right: dict) -> float:
    """两个角色的相似度（0~1）。**只用来排候选**，不直接决定合并。"""
    if left.get("book_id") == right.get("book_id"):
        return 0.0  # 同一本书里的两个人：抽取阶段已经去过重，不再猜

    scores: list[float] = []
    circle = _jaccard(_relationship_names(left), _relationship_names(right))
    if circle is not None:
        scores.append(circle * _W_CIRCLE)
    personality = _jaccard(_personality_tags(left), _personality_tags(right))
    if personality is not None:
        scores.append(personality * _W_PERSONALITY)
    profile = _jaccard(_profile_tokens(left), _profile_tokens(right))
    if profile is not None:
        scores.append(profile * _W_PROFILE)

    if len(scores) < 2:
        # 只靠一个信号（比如都在同一个圈子）不算相似，太容易误判
        return 0.0
    return sum(scores) / (_W_CIRCLE + _W_PERSONALITY + _W_PROFILE)


def similarity_candidates(
    characters: list[dict], *, floor: float = SIMILARITY_FLOOR, limit: int = 8
) -> list[tuple[dict, dict, float]]:
    """列出"可能是同一个人"的候选对，按分数从高到低。"""
    pairs: list[tuple[dict, dict, float]] = []
    for index, left in enumerate(characters):
        for right in characters[index + 1:]:
            score = similarity(left, right)
            if score >= floor:
                pairs.append((left, right, round(score, 3)))
    pairs.sort(key=lambda item: -item[2])
    return pairs[:limit]


# ---------------------------------------------------------------------------
# 管理 Agent 的经验：确认过一次，下次直接用
# ---------------------------------------------------------------------------
def _learned_pairs(shelf_id: str) -> tuple[list[set[str]], list[set[str]]]:
    """从记忆里取出「确认是同一人」和「确认不是同一人」的名字集合。"""
    same: list[set[str]] = []
    different: list[set[str]] = []
    try:
        memories = db.list_manager_memories(shelf_id, limit=500)
    except Exception:  # noqa: BLE001 - 记忆读不到不影响合并
        logger.exception("读取管理 Agent 记忆失败（已忽略）")
        return [], []
    for memory in memories:
        names = {
            normalize(name)
            for name in (memory.get("payload") or {}).get("names") or []
            if normalize(str(name))
        }
        if len(names) < 2:
            continue
        (same if memory.get("kind") == "same_person" else different).append(names)
    return same, different


def remember_merge(
    shelf_id: str,
    names: list[str],
    canonical: str,
    *,
    source: str,
    reason: str = "",
) -> Optional[dict]:
    """把"这两个名字是同一个人"写进管理 Agent 的记忆库，下次抽取直接用。"""
    cleaned = [str(name).strip() for name in names if str(name or "").strip()]
    if len(cleaned) < 2:
        return None
    # 同名的重复项去掉，免得写成「路明非、路明非 是同一人」
    seen: set[str] = set()
    unique: list[str] = []
    for name in cleaned:
        key = normalize(name)
        if key and key not in seen:
            seen.add(key)
            unique.append(name)
    if len(unique) >= 2:
        content = f"{'、'.join(unique[:3])} 是同一人（本体：{canonical}）"
    else:
        content = f"「{unique[0] if unique else canonical}」跨书同名，视为同一人"
    if reason:
        content += f"｜依据：{reason}"
    try:
        return db.add_manager_memory(
            shelf_id,
            "same_person",
            content,
            {"names": cleaned, "canonical": canonical, "source": source, "reason": reason},
        )
    except Exception:  # noqa: BLE001 - 记不住不影响这次合并
        logger.exception("写入合并经验失败（已忽略）")
        return None


def remember_different(
    shelf_id: str, names: list[str], *, source: str, reason: str = ""
) -> Optional[dict]:
    """把"这两个名字不是同一个人"写进记忆，免得下次又去问一遍。"""
    cleaned = [str(name).strip() for name in names if str(name or "").strip()]
    if len(cleaned) < 2:
        return None
    content = f"{'、'.join(cleaned[:3])} 不是同一人"
    if reason:
        content += f"｜依据：{reason}"
    try:
        return db.add_manager_memory(
            shelf_id,
            "different_person",
            content,
            {"names": cleaned, "source": source, "reason": reason},
        )
    except Exception:  # noqa: BLE001
        logger.exception("写入排除经验失败（已忽略）")
        return None


def pick_canonical(group: list[dict]) -> dict:
    """出场最多的当本体；并列时取更早入库的（通常是系列第一本）。"""
    return sorted(
        group,
        key=lambda c: (-int(c.get("appearances") or 0), str(c.get("created_at") or ""), c["id"]),
    )[0]


def _shelf_characters(shelf_id: str, *, only_canonical: bool) -> list[dict]:
    collected: list[dict] = []
    for book in db.list_books(shelf_id):
        for character in db.list_characters(book["id"], include_merged=True):
            if character.get("is_user_persona"):
                continue
            if only_canonical and character.get("is_merged"):
                continue
            collected.append(character)
    return collected


def _merge_into(
    child: dict,
    target: dict,
    *,
    shelf_id: str,
    source: str,
    summarizer=None,
    reason: str = "",
) -> dict:
    """把 child 并进 target：迁移记忆、记别名、写 canonical 标记。"""
    moved = db.transfer_memory_items(child["id"], target["id"])
    db.set_canonical(child["id"], target["id"])
    _merge_aliases(target, child)
    agent_memory.summarize(target["id"], summarizer=summarizer)

    agent_log.log(
        "character_merged",
        owner_type="manager",
        book_id=target.get("book_id"),
        shelf_id=shelf_id,
        payload={
            "canonical": target.get("name"),
            "canonical_id": target["id"],
            "merged": child.get("name"),
            "merged_id": child["id"],
            "moved_items": moved,
            "source": source,
            "reason": reason,
        },
    )
    return {
        "merged_id": child["id"],
        "name": child.get("name") or "",
        "into": target.get("name") or "",
        "into_id": target["id"],
        "from_book": child.get("book_id"),
        "into_book": target.get("book_id"),
        "moved_items": moved,
        "source": source,
    }


def pending_pairs(
    shelf_id: str,
    *,
    floor: float = SIMILARITY_FLOOR,
    ceiling: float = AUTO_MERGE_SCORE,
    limit: int = 10,
) -> list[dict]:
    """列出「名字不同但可能是同一个人」的候选对。**只读**：不合并、不问模型。

    给界面用——用户看到 "A ↔ B 相似度 0.72" 自己决定要不要合。
    已经被判定为"不是同一人"的组合不会出现在这里。
    """
    if not db.get_shelf(shelf_id):
        raise ValueError("书架不存在")

    _, learned_different = _learned_pairs(shelf_id)
    pairs: list[dict] = []
    for left, right, score in similarity_candidates(
        _shelf_characters(shelf_id, only_canonical=True), floor=floor, limit=limit
    ):
        if score > ceiling:
            # 高于自动合并阈值的会被 merge_shelf 直接合掉，不必让用户确认
            continue
        keys = {primary_key(left), primary_key(right)} - {""}
        if any(keys and keys <= pair for pair in learned_different):
            continue
        pairs.append(
            {
                "left": {
                    "id": left["id"],
                    "name": left.get("name") or "",
                    "role": left.get("role") or "",
                    "book_id": left.get("book_id") or "",
                    "appearances": int(left.get("appearances") or 0),
                },
                "right": {
                    "id": right["id"],
                    "name": right.get("name") or "",
                    "role": right.get("role") or "",
                    "book_id": right.get("book_id") or "",
                    "appearances": int(right.get("appearances") or 0),
                },
                "score": score,
                "common": sorted(_relationship_names(left) & _relationship_names(right))[:6],
            }
        )
    return pairs


def merge_shelf(
    shelf_id: str,
    *,
    summarizer=None,
    verifier=None,
    learn: bool = True,
    max_verifications: int = 3,
) -> dict:
    """合并书架里所有"同一个人"，并刷新摘要。

    三个来源，按可信度从高到低：

    1. **名字/别名相同** —— 直接合并（确定的事）
    2. **管理 Agent 记忆里确认过的** —— 直接合并（上次已经拍板）
    3. **关系网 + 性格相似** —— 只当作候选：有 ``verifier`` 就交它判定，
       判 true 才合并并把结论写进记忆；判 false 也写进记忆（以后不再问）。
       没有 ``verifier`` 时放进 ``pending`` 等用户在界面上手动合并。

    ``verifier`` 是同步函数 ``(left, right, score) -> True/False/None``。
    """
    if not db.get_shelf(shelf_id):
        raise ValueError("书架不存在")

    learned_same, learned_different = _learned_pairs(shelf_id)
    merged: list[dict] = []
    touched: list[str] = []
    learned_now: list[str] = []

    # ---- 第 1、2 阶段：名字 + 已确认经验 ----
    candidates = _shelf_characters(shelf_id, only_canonical=True)
    for group in group_duplicates(candidates, extra_pairs=learned_same):
        canonical = pick_canonical(group)
        names = [canonical.get("name") or ""]
        for other in group:
            if other["id"] == canonical["id"]:
                continue
            merged.append(
                _merge_into(other, canonical, shelf_id=shelf_id, source="name", summarizer=summarizer)
            )
            names.append(other.get("name") or "")
        touched.append(canonical["id"])
        if learn:
            memory = remember_merge(
                shelf_id,
                [name for name in names if name],
                canonical.get("name") or "",
                source="name",
                reason="名字或别名相同",
            )
            if memory:
                learned_now.append(memory["content"])

    # ---- 第 3 阶段：相似度候选 → 自动合并 / 判定 / 待确认 ----
    pending: list[dict] = []
    verified: list[dict] = []
    rejected: list[dict] = []
    auto_merged: list[dict] = []
    used = 0
    already_merged: set[str] = set()

    for left, right, score in similarity_candidates(
        _shelf_characters(shelf_id, only_canonical=True)
    ):
        # 这一轮里已经把其中一个并掉了：跳过，否则会对已合并的角色再合一次
        if left["id"] in already_merged or right["id"] in already_merged:
            continue

        keys = {primary_key(left), primary_key(right)} - {""}
        if any(keys and keys <= pair for pair in learned_different):
            rejected.append(
                {"a": left.get("name"), "b": right.get("name"), "score": score, "source": "memory"}
            )
            continue

        entry = {"a": left.get("name"), "b": right.get("name"), "score": score}

        # 高相似度：直接合并，不问模型
        if score > AUTO_MERGE_SCORE:
            child, target = (
                (left, right)
                if int(left.get("appearances") or 0) <= int(right.get("appearances") or 0)
                else (right, left)
            )
            merged.append(
                _merge_into(
                    child, target, shelf_id=shelf_id, source="similarity",
                    summarizer=summarizer,
                    reason=f"相似度 {score} 超过阈值 {AUTO_MERGE_SCORE}",
                )
            )
            touched.append(target["id"])
            already_merged.add(child["id"])
            auto_merged.append({**entry, "into": target.get("name")})
            if learn:
                memory = remember_merge(
                    shelf_id,
                    [left.get("name") or "", right.get("name") or ""],
                    target.get("name") or "",
                    source="similarity",
                    reason=f"性格/关系高度相似（{score}）",
                )
                if memory:
                    learned_now.append(memory["content"])
            continue

        if verifier is None:
            pending.append({**entry, "reason": "需要人工确认（未启用判定）"})
            continue
        if used >= max_verifications:
            pending.append({**entry, "reason": "本次判定次数已用完"})
            continue

        used += 1
        try:
            verdict = verifier(left, right, score)
        except Exception:  # noqa: BLE001 - 判定失败就当没问
            logger.exception("合并判定失败（已忽略）")
            verdict = None

        if verdict is True:
            child, target = (left, right) if (left.get("appearances") or 0) <= (
                right.get("appearances") or 0
            ) else (right, left)
            merged.append(
                _merge_into(
                    child, target, shelf_id=shelf_id, source="similarity",
                    summarizer=summarizer, reason=f"相似度 {score}",
                )
            )
            touched.append(target["id"])
            already_merged.add(child["id"])
            verified.append({**entry, "into": target.get("name")})
            if learn:
                memory = remember_merge(
                    shelf_id,
                    [left.get("name") or "", right.get("name") or ""],
                    target.get("name") or "",
                    source="similarity",
                    reason=f"性格/关系相似（{score}）+ 模型判定",
                )
                if memory:
                    learned_now.append(memory["content"])
        elif verdict is False:
            rejected.append({**entry, "source": "verifier"})
            if learn:
                remember_different(
                    shelf_id,
                    [left.get("name") or "", right.get("name") or ""],
                    source="similarity",
                    reason=f"模型判定不是同一人（相似度 {score}）",
                )
        else:
            pending.append({**entry, "reason": "判定不确定"})

    for character_id in dict.fromkeys(touched):
        agent_memory.summarize(character_id, summarizer=summarizer)

    members = db.ensure_shelf_members(shelf_id)
    return {
        "groups": len({item["into_id"] for item in merged}),
        "merged": merged,
        "summarized": len(set(touched)),
        "auto_merged": auto_merged,
        "verified": verified,
        "rejected": rejected,
        "pending": pending,
        "learned": learned_now,
        "members": members,
    }


def _merge_aliases(target: dict, child: dict) -> None:
    """把被合并方的名字并进本体的别名，以后自动识别就不用再手动合并一次。"""
    aliases = list(target.get("aliases") or [])
    known = {normalize(name) for name in aliases}
    known.add(normalize(target.get("name")))
    for name in [child.get("name"), *(child.get("aliases") or [])]:
        key = normalize(name)
        if not key or key in known:
            continue
        aliases.append(str(name).strip())
        known.add(key)
    if aliases != (target.get("aliases") or []):
        db.update_character(target["id"], aliases=aliases)


def _shelf_of(character: dict) -> str:
    book = db.get_book(character.get("book_id") or "") or {}
    return str(book.get("shelf_id") or "")


def llm_verifier(
    *, api_key: str, model: str, timeout: float = 25.0, min_confidence: float = 0.7
):
    """造一个**同步**判定器：联网问模型"这两个名字是不是同一个人"。

    拿不准（置信度低或调用失败）时返回 ``None``——调用方会把这对放进
    ``pending`` 让用户自己决定，**绝不靠猜**。
    """

    def verify(left: dict, right: dict, score: float):
        if not api_key:
            return None

        from openai import OpenAI

        from services.extractor import DEEPSEEK_BASE_URL

        def describe(character: dict) -> str:
            relations = "、".join(
                f"{item.get('with')}（{item.get('relation')}）"
                for item in (character.get("relationships") or [])[:6]
                if isinstance(item, dict) and item.get("with")
            )
            return (
                f"名字：{character.get('name')}"
                f"｜别名：{'、'.join(character.get('aliases') or []) or '无'}"
                f"｜身份：{character.get('role') or '未知'}"
                f"｜性格：{'、'.join((character.get('personality') or [])[:6]) or '未知'}"
                f"｜关系网：{relations or '无'}"
                f"｜出处：《{(db.get_book(character.get('book_id') or '') or {}).get('title') or '未知'}》"
            )

        common = _relationship_names(left) & _relationship_names(right)
        prompt = (
            "判断下面两个角色是不是**同一个人物**（同一部作品里的同一个角色，只是叫法不同）。\n\n"
            f"A：{describe(left)}\n\n"
            f"B：{describe(right)}\n\n"
            f"线索：性格/关系相似度 {score}；共同关联的人：{'、'.join(sorted(common)[:6]) or '无'}\n\n"
            "判定标准：\n"
            "- 同一人的不同称呼（昵称 / 全名 / 译名差异）→ same=true\n"
            "- 不同人物，哪怕性格相近、圈子重叠 → same=false\n"
            "- 拿不准就把 confidence 写低（<0.7），不要硬猜\n\n"
            '只输出 JSON：{"same": true|false, "confidence": 0~1, "reason": "一句话"}'
        )

        try:
            client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=timeout)
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是角色档案管理员，只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=200,
                response_format={"type": "json_object"},
            )
            raw = (response.choices[0].message.content or "").strip()
            data = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001 - 判定失败不该影响合并
            logger.exception("合并判定调用失败（当作未确认）")
            return None

        try:
            confidence = float(data.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < min_confidence:
            return None
        return bool(data.get("same"))

    return verify


def merge_pair(child_id: str, target_id: str, *, summarizer=None) -> dict:
    """手动把 child 合并进 target —— 不靠名字猜，用户说了算。

    自动规则为了不误合并，只认"主名"参与；像「诺诺」和「陈墨瞳」这种同一人但
    别名数据没互相指向的情况就漏了。这个接口用来补那些漏网的。

    合并后会把被并入方的名字记为别名，下次自动合并也能认出来。
    """
    if not child_id or not target_id:
        raise ValueError("缺少角色 id")
    if child_id == target_id:
        raise ValueError("不能合并到自己")

    child = db.get_character(child_id)
    target = db.get_character(target_id)
    if not child:
        raise ValueError("要合并的角色不存在")
    if not target:
        raise ValueError("目标角色不存在")
    if child.get("is_user_persona"):
        raise ValueError("「我」是用户自己的角色，不能合并")
    if child.get("is_merged"):
        raise ValueError("这个角色已经并入别人了")

    shelf_id = _shelf_of(child)
    if not shelf_id or shelf_id != _shelf_of(target):
        raise ValueError("只能合并同一个书架里的角色")

    moved = db.transfer_memory_items(child_id, target_id)
    db.set_canonical(child_id, target_id)
    _merge_aliases(target, child)
    memory_summary = agent_memory.summarize(target_id, summarizer=summarizer)

    agent_log.log(
        "character_merged_manual",
        owner_type="manager",
        book_id=target.get("book_id"),
        shelf_id=shelf_id,
        payload={
            "child": child.get("name"),
            "child_id": child_id,
            "target": target.get("name"),
            "target_id": target_id,
            "moved_items": moved,
        },
    )
    # 用户拍板过的身份关系进管理 Agent 记忆：下次抽书自动合并，不用再来一次
    remember_merge(
        shelf_id,
        [child.get("name") or "", target.get("name") or ""],
        target.get("name") or "",
        source="manual",
        reason="用户手动合并",
    )
    return {
        "merged": {
            "merged_id": child_id,
            "name": child.get("name") or "",
            "into": target.get("name") or "",
            "into_id": target_id,
        },
        "moved_items": moved,
        "summary": memory_summary,
        "members": db.ensure_shelf_members(shelf_id),
    }


def unmerge(child_id: str, *, summarizer=None) -> dict:
    """撤销一次合并：把角色还原成独立角色，并取回属于它自己那本书的记忆。"""
    character = db.get_character(child_id)
    if not character:
        raise ValueError("角色不存在")
    canonical_id = character.get("canonical_id")
    if not canonical_id:
        raise ValueError("这个角色没有被合并")
    if not db.get_character(canonical_id):
        # 本体已经不在了，只清标记即可
        db.set_canonical(child_id, None)
        return {"restored": child_id, "target": None, "moved_items": 0}

    moved = db.restore_memory_items(child_id, canonical_id, character.get("book_id") or "")
    db.set_canonical(child_id, None)
    agent_memory.summarize(child_id, summarizer=summarizer)
    if db.get_character(canonical_id):
        agent_memory.summarize(canonical_id, summarizer=summarizer)

    shelf_id = _shelf_of(character)
    members = db.ensure_shelf_members(shelf_id) if shelf_id else []
    return {
        "restored": child_id,
        "target": canonical_id,
        "moved_items": moved,
        "members": members,
    }


def refresh_summaries(shelf_id: str, *, summarizer=None) -> int:
    """重新摘要书架里所有角色的记忆。

    删书后调用：原始条目已被按来源回收，重算一遍摘要就等于"回档"到
    「只剩现有这些书」的状态。
    """
    if not db.get_shelf(shelf_id):
        raise ValueError("书架不存在")
    count = 0
    for character in _shelf_characters(shelf_id, only_canonical=True):
        agent_memory.summarize(character["id"], summarizer=summarizer)
        count += 1
    db.ensure_shelf_members(shelf_id)
    return count