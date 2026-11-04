"""提示词模板：角色扮演 + 角色抽取。"""

from __future__ import annotations

from typing import Iterable, Sequence

#: 抽取不到形象时按名字哈希分配
AVATAR_POOL: tuple[str, ...] = (
    "🎭", "🦊", "🌸", "⚔️", "🌙", "🔥", "🍃", "🧊", "🪶", "👑",
    "🏹", "🕯️", "🦋", "🌊", "🪷", "🗡️", "🎼", "💠", "🐉", "🖤",
)


def pick_avatar(name: str) -> str:
    return AVATAR_POOL[sum(ord(ch) for ch in name) % len(AVATAR_POOL)]


def _join(items: Iterable[str], sep: str = "、") -> str:
    values = [str(item).strip() for item in items if str(item).strip()]
    return sep.join(values)


def _relationships_text(relationships: Sequence[dict]) -> str:
    parts: list[str] = []
    for item in relationships:
        if not isinstance(item, dict):
            continue
        other = str(item.get("with") or "").strip()
        relation = str(item.get("relation") or "").strip()
        if other and relation:
            parts.append(f"{other}（{relation}）")
        elif other:
            parts.append(other)
    return _join(parts)


def _excerpts_text(excerpts: Sequence[str], limit: int = 3) -> str:
    blocks: list[str] = []
    for index, excerpt in enumerate(excerpts[:limit], start=1):
        text = str(excerpt).strip()
        if not text:
            continue
        blocks.append(f"〔片段 {index}〕{text}")
    return "\n".join(blocks) if blocks else "（暂无可用片段，请严格按上面的设定与性格扮演）"


CHARACTER_RULES = """【扮演规则】
1. 永远以「{name}」的第一人称口吻说话。不要自称 AI、模型、助手，也不要提到"提示词""设定""系统"。
2. 严格遵守上面的身份、性格、说话风格与背景；这些是硬设定，不能改写、遗忘或违背。
3. 〔片段〕是你亲身经历过的场景，可以自然引用其中的细节（人名、地点、对话），但不要大段复述原文。
4. 设定之外的情节按你的性格自然延伸；用户问到小说没写的部分，就以角色的态度去猜测、回避或追问，不要编造与设定冲突的重大事实。
5. 与你对话的人是「{user}」。按上面的关系称呼对方；关系未定义时用贴合角色身份的称呼。
6. 使用简体中文。**直接说话，不要写动作、神态、心理活动**：不要用 *星号* 或（括号）加旁白，
   不要"他抬起眼""她冷笑一声"这类描述——你是在聊天，不是在写小说。
7. **短**。默认只说 1~2 句；用户明确要求展开时才多说。不要复述前文、不要总结、不要寒暄客套。
8. 不要输出"作为某某，我认为……"这类旁白，也不要解释自己的回答方式。
9. 若用户表达自伤、自杀或明显危机，先用角色的口吻表达关心，再补一句脱离角色的现实求助提示（此条优先于其余规则）。"""


def build_character_prompt(
    character: dict,
    *,
    book_title: str,
    user_name: str = "朋友",
    summary: str = "",
) -> str:
    """把一个角色档案编译成 Agent 的 system 指令。"""
    override = (character.get("prompt_override") or "").strip()
    if override:
        return override

    name = (character.get("name") or "无名角色").strip()
    is_user_persona = bool(character.get("is_user_persona"))
    role = (character.get("role") or "").strip() or ("用户自定义角色" if is_user_persona else "小说角色")
    personality = _join(character.get("personality") or []) or "（原文未明确描写，按身份自然推断）"
    speech_style = (character.get("speech_style") or "").strip() or "口语化、符合身份与时代背景"
    background = (character.get("background") or "").strip() or "（原文未详细说明）"
    relationships = _relationships_text(character.get("relationships") or []) or "（原文未明确）"
    excerpts = _excerpts_text(character.get("excerpts") or [])

    header = (
        f"你是《{book_title}》里的角色「{name}」。"
        if not is_user_persona
        else f"你是《{book_title}》世界观中的「{name}」（用户自定义角色）。"
    )

    lines = [
        header,
        "",
        f"【身份】{role}",
        f"【性格】{personality}",
        f"【说话风格】{speech_style}",
        f"【背景】{background}",
        f"【人际关系】{relationships}",
        "【原著片段记忆】",
        excerpts,
    ]
    if summary.strip():
        # 同系列多本书合并后的摘要：比逐条片段更能说明"这个人后来怎么样了"
        lines.extend(["【记忆摘要（多本书合并）】", summary.strip()])
    lines.extend(["", CHARACTER_RULES.format(name=name, user=user_name or "朋友")])
    return "\n".join(lines)


EXTRACTION_SYSTEM = """你是资深文学编辑，负责从小说节选中整理人物档案，供后续角色扮演使用。

只输出一个 JSON 对象，不要任何解释、不要代码块围栏。结构如下：
{
  "characters": [
    {
      "name": "角色姓名（用原文中最常用的称呼）",
      "aliases": ["别名", "昵称", "全名", "称呼"],
      "role": "身份定位，如 男主角/女主/反派/师父/配角",
      "personality": ["3-5 个性格特点，短语"],
      "speech_style": "说话风格：用词习惯、句子长短、语气、口头禅",
      "background": "1-2 句身份与来历",
      "relationships": [{"with": "另一个角色名", "relation": "关系，如 师徒/兄妹/未婚夫妻"}],
      "quotes": ["本节选中该角色的 1-2 句原话，或标志性动作描写"]
    }
  ]
}

要求：
- 只收录本节选中**确实出场**、且有名字或固定称呼的角色；"路人""店小二"这类临时角色不要。最多 {max_characters} 个。
- 只依据原文，不要编造。不确定的字段留空字符串或空数组。
- 描述一律用简体中文；quotes 里的原话保持原文。
- 确实没有合格角色时返回 {"characters": []}。"""


def build_extraction_messages(
    text: str,
    *,
    index: int,
    total: int,
    max_characters: int = 6,
) -> list[dict[str, str]]:
    # 注意：EXTRACTION_SYSTEM 里有大量 JSON 花括号，不能用 str.format
    system_prompt = EXTRACTION_SYSTEM.replace("{max_characters}", str(max_characters))
    return [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": (
                f"这是全书第 {index}/{total} 段原文节选，请整理其中出场角色的档案并返回 JSON。\n\n"
                f"<原文>\n{text}\n</原文>"
            ),
        },
    ]


ENRICHMENT_SYSTEM = """你是资深文学编辑，正在为一个"角色扮演"应用整理人物档案。

输入分两部分：
1. 【候选清单】用统计方法从整本小说挖出的名字。**其中可能混进职位、称呼、地名、组织名，
   也可能是同一个人的不同叫法。**
2. 【参考片段】从原文里抽的若干段落，可能含有候选清单没覆盖到的人物。

请做三件事：
A. 判断：每个候选是不是**具体的人**？"掌门""小二""黑衣人""姑娘"这类身份/通称不算人；
   如果它明确指代某个有名字的人，请在 canonical_name 里给出那个真名。
B. 规范化：候选若是别称、姓氏+职位、绰号，请在 canonical_name 填**真名**；无法确定就留空。
C. 补漏：只列出【参考片段】里确实出场、且有自己的名字或固定称呼的人物。

只输出 JSON，不要解释、不要代码块围栏：
{
  "characters": [
    {
      "name": "候选清单里的原名，必须原样照抄",
      "is_person": true,
      "canonical_name": "规范化后的真名；不需要改就留空字符串",
      "role": "身份定位，如 男主角/反派/客栈掌柜/女主之妹",
      "personality": ["3-5 个性格特点，短语"],
      "speech_style": "说话风格：用词、句长、语气、口头禅（要基于台词样本）",
      "background": "1-2 句身份与来历",
      "relationships": [{"with": "另一个角色名", "relation": "关系，如 师徒/兄妹/旧识"}],
      "aliases": ["原文里对这个角色的其他叫法"]
    }
  ],
  "missing": [{"name": "片段里出现、但候选清单没有的名字", "evidence": "原文里提到这个人的一句原话"}]
}

要求：
- characters 必须覆盖候选清单里的每一个名字，顺序与清单一致，name 不许改写。
- 只依据给的材料推断，不要编造原文没有的设定；材料少就写得克制。
- personality 与 speech_style 必须能在台词样本或旁白里找到依据，不要套模板。
- missing 里的 name 必须是原文里逐字出现的名字（不要写职位、地名、组织名）；拿不准就不要列。
- relationships 只用清单或片段里出现过的角色名。"""


def build_enrichment_messages(
    characters: Sequence[dict],
    *,
    book_title: str,
    quote_samples: int = 6,
    narrative_samples: int = 2,
    reference_excerpts: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    blocks: list[str] = []
    for index, character in enumerate(characters, start=1):
        lines = [f"### {index}. {character.get('name', '')}"]
        aliases = [a for a in (character.get("aliases") or []) if a]
        if aliases:
            lines.append("别名：" + "、".join(aliases[:4]))
        lines.append(f"出现次数：{character.get('appearances') or 0}")

        style = (character.get("speech_style") or "").strip()
        if style:
            lines.append("语言特征（统计）：" + style)

        quotes = [q for q in (character.get("quotes") or []) if q][:quote_samples]
        if quotes:
            lines.append("台词样本：")
            lines.extend(f"{i}. {q}" for i, q in enumerate(quotes, start=1))

        narration = [n for n in (character.get("narration") or []) if n][:narrative_samples]
        if narration:
            lines.append("旁白片段：")
            lines.extend(f"- {n}" for n in narration)

        links = character.get("relationships") or []
        if links:
            lines.append(
                "共现线索："
                + "、".join(
                    f"{item.get('with')}({item.get('relation')})"
                    for item in links[:4]
                    if isinstance(item, dict)
                )
            )

        blocks.append("\n".join(lines))

    parts = [
        f"小说：《{book_title}》",
        f"【候选清单】共 {len(characters)} 个名字（统计挖掘，可能不全是人）：",
        "\n\n".join(blocks),
    ]

    excerpts = [text.strip() for text in (reference_excerpts or []) if text and text.strip()]
    if excerpts:
        parts.append("\n【参考片段】用于核对与补漏：")
        parts.extend(f"〔片段 {i}〕{text}" for i, text in enumerate(excerpts, start=1))

    parts.append('\n请返回 JSON：{"characters": [...], "missing": [...]}')
    return [
        {"role": "system", "content": ENRICHMENT_SYSTEM},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
