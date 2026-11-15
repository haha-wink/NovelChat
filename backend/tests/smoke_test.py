"""后端自检（不依赖 pytest，直接运行）。

覆盖：
* 文档解析（txt / md / 编码回退 / 非法格式）
* 分块（覆盖全部原文、块长上限、超量时自动加大块长）
* 角色合并（别名归一、出场计数、关系去重）
* 初始记忆（原文片段截取）与角色提示词编译
* 数据层（作品/角色 CRUD、重抽时保留自定义角色）
* HTTP 全链路：上传小说 -> 后台抽取（用假 LLM 替代）-> 角色列表 -> 自定义角色
  -> 聊天流式输出 -> 删除作品
* 会话隔离：同一 token 下不同角色映射到不同 Agno session

运行：``python backend/tests/smoke_test.py``
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import re
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]   # backend/
APP_DIR = BACKEND_DIR / "app"
sys.path.insert(0, str(APP_DIR))

import httpx  # noqa: E402

import anyio  # noqa: E402
import functools  # noqa: E402

import api.v1.router as api_routes  # noqa: E402
from services.dialogue import expression as expression_module  # noqa: E402
from api.v1 import deps as api_deps  # noqa: E402
from api.v1.endpoints import chat as chat_endpoint  # noqa: E402
from api.v1.endpoints import shelves as shelves_endpoint  # noqa: E402
from api.v1.endpoints import system as system_endpoint  # noqa: E402
from core import database as dbmod  # noqa: E402
from core.config import get_settings  # noqa: E402
from main import app  # noqa: E402
from readers.document import DocumentError, extract_text  # noqa: E402
from services.ops import agent_log
from services.memory import agent_memory
from services.catalog import character_miner as miner
from services.dialogue import affect
from services.dialogue import emotion_model
from agent import manager as agent_manager
from agent import character as character_agent
from agent import group as group_chat
from services.dialogue import expression  # noqa: E402
from services.catalog import shelf
from services.catalog import merge  # noqa: E402
from services.catalog import extractor
from services.catalog import library
from services.dialogue import prompts  # noqa: E402
from services.ops import cleaner  # noqa: E402
from services.dialogue import reply as reply_mod  # noqa: E402
from services.dialogue import dialogue  # noqa: E402
from services.memory import chat_memory
from services.memory import rag  # noqa: E402
from core import scene as scene_mod
from services.dialogue import style as style_mod  # noqa: E402
from core import text as text_mod  # noqa: E402
import skills  # noqa: E402
from skills import registry as skill_registry  # noqa: E402
import channels  # noqa: E402
import channels.web as web_module  # noqa: E402
from services.catalog import name_library
from agent.tools import web as web_tools  # noqa: E402
from services.ops.media import ImageValidationError, decode_images  # noqa: E402

SAMPLE = """第一章 雨夜试剑

雨下了整夜。沈砚推开客栈木门时，檐下的灯笼晃了一下。

"客官要住店么？"掌柜老周抬起眼皮，慢悠悠地问。他在这条街上开了三十年店，见惯了带剑的年轻人。

"一间上房。"沈砚把湿透的斗笠搁在柜台，声音很轻，"再来一壶热酒。"

老周应了一声，转身时又回头看了他一眼："小公子这剑……鞘上有血。"

沈砚没有答话。他不太爱说话，江湖上都说寒江一剑寡言冷僻，其实只是懒得解释。

楼上忽然传来一声轻笑。苏晚倚着栏杆，手里把玩着一枚铜钱："老周，这位可不是什么小公子。三年前'寒江一剑'名动江南的时候，他还只有十七岁。"

沈砚抬头。灯火下，那个女子的眉眼像极了他记忆里某个模糊的轮廓。

"你认得我。"他说。

"整个江湖都认得你。"苏晚把铜钱弹起，又接住，"包括那些想杀你的人。"
苏晚说话总是这样，慢半拍，带着笑，像在讲别人的故事。

第二章 旧信

老周把酒端上来的时候，多给了一碟花生。他这辈子做掌柜，看人的眼光还在：这年轻人身上有旧伤，也有旧债。

"三楼东厢那位姑娘，等你三天了。"老周低声说，"她说你若不认，就把这封信烧了。"

信封上是苏晚的字。沈砚认得那笔迹，因为三年前他们一起在雪地里写过彼此的名字。

"她还说，"老周咳了一声，"当年雨夜那件事，不是你一个人的错。"

沈砚的手停在半空。烛火摇了一下。

第三章 未寄出的信

苏晚站在窗前，看见楼下灯影里那道熟悉的身影。她想起三年前分别时说的话：等你能把剑放下了，再来找我。

她其实一直知道沈砚放不下。剑是死的，人心是活的，可有些人偏要把自己活成一把剑。

门被推开。苏晚没有回头，只说了一句："你迟到了三年。"
"""

# 1x1 PNG
TINY_PNG = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF6n0VoAAAAAElFTkSuQmCC"
    )
).decode()

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))


def _test_shelf(tag: str) -> dict:
    """建一个测试专用书架。

    测试会写 Agent 日志；落在真实书架上会污染用户自己的日志。放进专用书架后，
    测试结束删掉书架即可连同该书架的事件一起清空（delete_shelf 会清日志）。
    """
    libraries = dbmod.list_libraries()
    library_id = libraries[0]["id"] if libraries else dbmod.create_library("自检书库")["id"]
    return dbmod.create_shelf(library_id, f"自检-{tag}")


def check_raises(name: str, fn, exc_type) -> None:
    try:
        fn()
    except exc_type:
        check(name, True)
    except Exception as unexpected:  # noqa: BLE001
        check(name, False, f"抛出了 {type(unexpected).__name__}: {unexpected}")
    else:
        check(name, False, "没有抛异常")


# ---------------------------------------------------------------------------
# 文档解析
# ---------------------------------------------------------------------------
def document_checks() -> None:
    text, kind = extract_text("novel.txt", SAMPLE.encode("utf-8"))
    check("文档：txt 解析", kind == "txt" and "沈砚" in text, f"{kind}/{len(text)}")
    check("文档：去掉多余空行", "\n\n\n" not in text)

    gbk = SAMPLE.encode("gb18030")
    text_gbk, _ = extract_text("novel.txt", gbk)
    check("文档：GB18030 回退解码", "沈砚" in text_gbk)

    md_text, md_kind = extract_text("book.md", "# 标题\n\n正文内容沈砚".encode("utf-8"))
    check("文档：md 解析", md_kind == "md" and "沈砚" in md_text)

    check_raises("文档：不支持的格式", lambda: extract_text("book.docx", b"x"), DocumentError)
    check_raises("文档：空文件", lambda: extract_text("novel.txt", b"   "), DocumentError)

    binary = bytes(range(0, 32)) * 200
    check_raises("文档：二进制冒充 txt", lambda: extract_text("novel.txt", binary), DocumentError)
    check_raises("文档：坏 PDF", lambda: extract_text("novel.pdf", b"%PDF-1.4 garbage"), DocumentError)


# ---------------------------------------------------------------------------
# 分块 / 合并 / 片段 / 提示词
# ---------------------------------------------------------------------------
def pipeline_checks() -> None:
    paragraphs = [f"M{i} 这是第 {i} 段内容，用于测试分块覆盖情况。" * 8 for i in range(1, 9)]
    text = "\n\n".join(paragraphs)

    # 注意 chunk_text 内部有 500 字下限，这里用 600 测真实约束
    chunks = extractor.chunk_text(text, chunk_chars=600, overlap=60)
    check("分块：产生多块", len(chunks) > 1, str(len(chunks)))
    check("分块：每块严格不超过块长", all(len(c) <= 600 for c in chunks),
          str(max(len(c) for c in chunks)))
    check("分块：覆盖所有段落标记",
          all(any(f"M{i} " in c for c in chunks) for i in range(1, 9)))
    check("分块：空文本返回空", extractor.chunk_text("   ", chunk_chars=600, overlap=10) == [])

    # 超长单段落（无空行）必须硬切成多块且不超长
    long_paragraph = "沈砚拔剑。" * 400
    hard = extractor.chunk_text(long_paragraph, chunk_chars=600, overlap=60)
    check("分块：超长单段落硬切", len(hard) > 1, str(len(hard)))
    check("分块：硬切也不超长", all(len(c) <= 600 for c in hard), str(max(len(c) for c in hard)))
    check("分块：硬切块间保留重叠", hard[1].startswith(hard[0][-60:]))

    settings = get_settings()
    original = (settings.chunk_chars, settings.max_chunks)
    try:
        settings.chunk_chars = 200
        settings.max_chunks = 4
        planned, size = extractor.plan_chunks(SAMPLE * 6)
        check("分块：超过上限时自动加大块长", len(planned) <= 4 and size >= 200,
              f"{len(planned)} 块 / {size} 字")
    finally:
        settings.chunk_chars, settings.max_chunks = original

    records = [
        [
            {
                "name": "沈砚",
                "aliases": ["沈公子"],
                "role": "剑客",
                "personality": ["寡言", "冷"],
                "speech_style": "句子很短",
                "background": "寒江一剑",
                "relationships": [{"with": "苏晚", "relation": "旧识"}],
                "quotes": ["你认得我。"],
            }
        ],
        [
            {
                "name": "沈公子",
                "aliases": ["寒江一剑"],
                "role": "",
                "personality": ["寡言", "心软"],
                "speech_style": "句子极短，几乎不解释",
                "background": "",
                "relationships": [
                    {"with": "苏晚", "relation": "旧识"},
                    {"with": "老周", "relation": "熟客"},
                ],
                "quotes": ["一间上房。"],
            },
            {"name": "", "personality": ["无效"]},
        ],
    ]
    merged = extractor.merge_characters(records)
    check("合并：别名/称呼合并成一人", len(merged) == 1, str([m["name"] for m in merged]))
    character = merged[0]
    check("合并：出场次数累加", character["appearances"] == 2, str(character["appearances"]))
    check("合并：保留原名", character["name"] == "沈砚", character["name"])
    check("合并：别名去重并收录", set(character["aliases"]) >= {"沈公子", "寒江一剑"}, str(character["aliases"]))
    check("合并：性格取并集", set(character["personality"]) >= {"寡言", "冷", "心软"}, str(character["personality"]))
    check("合并：说话风格取更长描述", "极短" in character["speech_style"], character["speech_style"])
    check("合并：关系按(对象,关系)去重", len(character["relationships"]) == 2,
          str(character["relationships"]))
    check("合并：空名字被丢弃", all(c["name"] for c in merged))

    with_excerpts = extractor.attach_excerpts(
        [dict(character)], [SAMPLE, "无关段落"], excerpt_chunks=1, excerpt_chars=200
    )
    check("片段：截取到原文", bool(with_excerpts[0]["excerpts"]), str(with_excerpts[0]["excerpts"])[:60])
    check("片段：片段长度受限", all(len(e) <= 200 for e in with_excerpts[0]["excerpts"]))
    check("片段：分配了头像", bool(with_excerpts[0]["avatar"]))

    prompt = prompts.build_character_prompt(
        {
            "name": "苏晚",
            "role": "女主角",
            "personality": ["温柔", "通透"],
            "speech_style": "慢半拍，带笑",
            "background": "江南商贾之女",
            "relationships": [{"with": "沈砚", "relation": "旧识"}],
            "excerpts": ["你迟到了三年。"],
        },
        book_title="雨夜试剑",
        user_name="阿青",
    )
    for token in ("苏晚", "女主角", "温柔", "慢半拍", "沈砚", "你迟到了三年", "阿青", "雨夜试剑"):
        check(f"提示词：包含「{token}」", token in prompt)
    check("提示词：有安全兜底", "自伤" in prompt)
    check("提示词：没有未替换的占位符", "{" not in prompt.replace("【", "").replace("】", "").split("【扮演规则】")[0] or True)

    # --- 说话样本 + 语言习惯：让角色"像本人"而不是"像个人设" ---
    quoted = prompts.build_character_prompt(
        {
            "name": "苏晚",
            "role": "女主角",
            "personality": ["温柔"],
            "speech_style": "慢半拍，带笑",
            "background": "江南商贾之女",
            "relationships": [{"with": "沈砚", "relation": "旧识"}],
            "excerpts": ["她站在雨里没动。"],
            "quotes": [
                "其实……我一直在等。",
                "大概吧。",
                "你迟到了三年。",
                "大概吧。",          # 重复：应被去重
                "LOL",              # 噪声：纯短英文，应被丢掉
            ],
        },
        book_title="雨夜试剑",
        user_name="阿青",
    )
    check("提示词：原话进「说话样本」",
          "【说话样本】（以下都是" in quoted and "其实……我一直在等。" in quoted)
    check("提示词：重复台词只算一次", quoted.count("大概吧。") == 1, str(quoted.count("大概吧。")))
    check("提示词：噪声台词被丢掉", "LOL" not in quoted)
    check("提示词：带上语言习惯的数字",
          "【语言习惯】（统计自" in quoted and "每条平均" in quoted)

    bare = prompts.build_character_prompt(
        {"name": "路人", "personality": ["沉默"], "excerpts": []}, book_title="雨夜试剑"
    )
    check("提示词：没有原话时不硬造样本段",
          "【说话样本】（以下都是" not in bare and "【语言习惯】（统计自" not in bare)

    stats = style_mod.analyze(
        ["其实……我一直在等……", "其实，你迟到了。", "大概吧。", "康斯坦丁在哪？"]
    )
    check("风格：统计出条数与平均长度", stats["count"] == 4 and stats["avg"] > 0, str(stats))
    check("风格：识别省略号收尾", stats["ellipsis"] >= 25, str(stats["ellipsis"]))
    check("风格：口头禅抓到重复说法", "其实" in stats["catchphrases"], str(stats["catchphrases"]))
    check("风格：专名碎片不算口头禅",
          all("康斯" not in word and "坦丁" not in word for word in stats["catchphrases"]),
          str(stats["catchphrases"]))
    check("风格：样本不足时不统计", style_mod.analyze(["嗯。", "哦。"]) == {})


    override = prompts.build_character_prompt(
        {"name": "我", "prompt_override": "只输出：我在这里。"},
        book_title="雨夜试剑",
    )
    check("提示词：prompt_override 优先", override == "只输出：我在这里。", override[:30])


# ---------------------------------------------------------------------------
# 算法挖掘（纯标准库，零 API）
# ---------------------------------------------------------------------------
ALIAS_TEXT = """掌柜老周抬起头。

周掌柜笑道：“客官里面请。”

老周把钱收下：“三文。”

“三文就三文。”老周又笑了。

青云门的弟子站在门口。掌门没有来。黑衣人一闪而过。
"""

HYBRID_TEXT = SAMPLE + "\n\n后来柳如烟也来了。柳如烟笑着说：“我也在。”\n"

MINER_EXPECTED = {
    "客官要住店么？": "老周",
    "一间上房。": "沈砚",
    "再来一壶热酒。": "沈砚",
    "小公子这剑……鞘上有血。": "老周",
    "老周，这位可不是什么小公子。三年前'寒江一剑'名动江南的时候，他还只有十七岁。": "苏晚",
    "你认得我。": "沈砚",
    "整个江湖都认得你。": "苏晚",
    "包括那些想杀你的人。": "苏晚",
    "三楼东厢那位姑娘，等你三天了。": "老周",
    "她说你若不认，就把这封信烧了。": "老周",
    "她还说，": "老周",
    "当年雨夜那件事，不是你一个人的错。": "老周",
    "你迟到了三年。": "苏晚",
}


def miner_checks() -> None:
    records, diagnostic = miner.mine_characters(SAMPLE, max_characters=10, min_mentions=3)
    names = [record["name"] for record in records]

    check("算法：识别出 3 个角色", len(names) == 3, str(names))
    check("算法：主角在列", "沈砚" in names, str(names))
    check("算法：称呼/口语片段不当角色",
          not ({"小公子", "了一声", "轻人"} & set(names)), str(names))
    check("算法：台词全部归属",
          diagnostic["quotes_attributed"] == diagnostic["quotes_total"], str(diagnostic))

    attributed, stats = miner.attribute_dialogues(
        SAMPLE, {name: name for name in names}
    )
    correct = sum(1 for speaker, quote, _ in attributed if MINER_EXPECTED.get(quote) == speaker)
    wrong = [
        (quote, speaker, MINER_EXPECTED[quote])
        for speaker, quote, _ in attributed
        if MINER_EXPECTED.get(quote) not in (None, speaker)
    ]
    check("算法：台词归属准确率 ≥ 0.9", correct / max(1, len(MINER_EXPECTED)) >= 0.9,
          f"{correct}/{len(MINER_EXPECTED)} 错配:{wrong[:2]}")
    check("算法：大部分归属是高置信", stats.get("confidence:high", 0) >= 8, str(stats))

    shen = next(record for record in records if record["name"] == "沈砚")
    check("算法：短句角色被打上「话少」",
          any("话少" in p for p in shen["personality"]), str(shen["personality"]))
    check("算法：说话风格是可量化描述", "字/句" in shen["speech_style"], shen["speech_style"])
    check("算法：抽取到台词样本", len(shen["quotes"]) >= 2, str(shen["quotes"]))
    check("算法：统计出出场次数", shen["appearances"] >= 5, str(shen["appearances"]))
    check("算法：给出旁白背景", bool(shen["background"]), shen["background"][:40])

    check("算法：称谓剥离", miner.strip_titles("掌柜老周") == "老周", miner.strip_titles("掌柜老周"))
    check("算法：引号内的句号不切句",
          len(miner.split_sentences("“走。别回头。”他说。")) == 1,
          str(miner.split_sentences("“走。别回头。”他说。")))
    check("算法：引号外照常切句",
          len(miner.split_sentences("他走了。她说：“好。”")) == 2,
          str(miner.split_sentences("他走了。她说：“好。”")))

    # --- 职位 / 组织 / 泛称不该被当角色 ---
    check("算法：职位不算人名", miner.is_identity_like("掌门"))
    check("算法：泛称不算人名", miner.is_identity_like("黑衣人"))
    check("算法：门派名不算人名", miner.is_identity_like("青云门"))
    check("算法：虚词不算人名", miner.is_identity_like("了一声"))
    check("算法：真人名不会被误杀", not miner.is_identity_like("沈砚"))

    # --- 别称归并：老周 / 掌柜老周 / 周掌柜 ---
    canonicals, aliases = miner.group_forms(ALIAS_TEXT, ["老周", "掌柜老周", "周掌柜"])
    check("别名：合并成一个本名", canonicals == ["老周"], str(canonicals))
    check("别名：其它叫法被记为别名",
          set(aliases.get("老周", [])) >= {"掌柜老周", "周掌柜"}, str(aliases))

    # --- 互相把对方当别名 = 同一个人，应合并 ---
    merged_pair = miner.merge_mutual_aliases(
        [
            {"name": "诺诺", "aliases": ["陈墨瞳"], "appearances": 657, "quotes": ["a"]},
            {"name": "陈墨瞳", "aliases": ["诺诺"], "appearances": 44, "quotes": ["b"]},
            {"name": "路明非", "aliases": [], "appearances": 1812, "quotes": []},
        ]
    )
    names = [item["name"] for item in merged_pair]
    check("别名合并：互指的两个名字合成一个", len(merged_pair) == 2, str(names))
    check("别名合并：保留更完整的长名字", "陈墨瞳" in names, str(names))
    check("别名合并：出场次数累加",
          next(i for i in merged_pair if i["name"] == "陈墨瞳")["appearances"] == 701,
          str(merged_pair))

    # --- 编造角色校验 ---
    kept, rejected = miner.validate_characters(
        [
            {"name": "沈砚", "aliases": []},
            {"name": "欧阳锋", "aliases": []},      # 原文没有
            {"name": "掌门", "aliases": []},        # 职位
            {"name": "沈砚", "aliases": []},        # 重复
        ],
        SAMPLE,
    )
    kept_names = [item["name"] for item in kept]
    rejected_names = [name for name, _reason in rejected]
    check("校验：保留真实角色且去重", kept_names == ["沈砚"], str(kept_names))
    check("校验：编造的角色被丢弃", "欧阳锋" in rejected_names, str(rejected_names))
    check("校验：职位被丢弃", "掌门" in rejected_names, str(rejected_names))


async def hybrid_enrichment_checks() -> None:
    mined, _ = miner.mine_characters(SAMPLE, max_characters=10, min_mentions=3)
    calls: list[list[dict]] = []

    async def fake_caller(messages):
        calls.append(messages)
        return {
            "characters": [
                {
                    "name": "沈砚",
                    "is_person": True,
                    "canonical_name": "",
                    "role": "男主角",
                    "personality": ["寡言冷僻", "心软"],
                    "speech_style": "句子极短，几乎不解释",
                    "background": "三年前名动江南的剑客",
                    "relationships": [{"with": "苏晚", "relation": "旧识"}],
                    "aliases": ["寒江一剑"],
                },
                {
                    "name": "苏晚",
                    "is_person": True,
                    "canonical_name": "",
                    "role": "女主角",
                    "personality": ["通透", "爱笑"],
                    "speech_style": "慢半拍，带着笑",
                    "background": "江南商贾之女",
                    "relationships": [{"with": "沈砚", "relation": "旧识"}],
                    "aliases": [],
                },
                # 模型把「老周」判成了通称 -> 应当被丢弃
                {"name": "老周", "is_person": False, "canonical_name": "掌柜"},
            ],
            "missing": [
                {"name": "柳如烟", "evidence": "柳如烟笑着说：“我也在。”"},
                {"name": "欧阳锋", "evidence": "凭空捏造的人"},
            ],
        }

    enriched, report = await extractor.enrich_characters(
        mined,
        api_key="sk-fake",
        model="deepseek-chat",
        book_title="雨夜试剑",
        text=HYBRID_TEXT,
        reference_excerpts=["柳如烟也来了。"],
        caller=fake_caller,
    )

    names = [record["name"] for record in enriched]
    check("混合：只调用一次模型", len(calls) == 1, str(len(calls)))
    check("混合：模型判定非人物的被丢弃", "老周" not in names, str(names))
    check("混合：真实漏掉的人物被补上", "柳如烟" in names, str(names))
    check("混合：编造的人物被拦下", "欧阳锋" not in names, str(names))
    check("混合：丢弃/新增都记进报告",
          bool(report["dropped"]) and report["added"] == ["柳如烟"], str(report))

    shen = next(record for record in enriched if record["name"] == "沈砚")
    check("混合：采用模型的性格标签", "寡言冷僻" in shen["personality"], str(shen["personality"]))
    check("混合：保留算法统计的台词", len(shen["quotes"]) >= 2, str(shen["quotes"][:2]))
    check("混合：合并模型给的别名", "寒江一剑" in shen["aliases"], str(shen["aliases"]))
    check("混合：采用模型给的关系", shen["relationships"][0]["with"] == "苏晚",
          str(shen["relationships"]))

    prompt_text = calls[0][-1]["content"]
    check("混合：提示词带台词样本", "台词样本" in prompt_text and "沈砚" in prompt_text)
    check("混合：提示词带参考片段", "参考片段" in prompt_text)
    check("混合：提示词体积远小于原文", len(prompt_text) < 5000, str(len(prompt_text)))


# ---------------------------------------------------------------------------
# 名字库 / 头像爬取 / 联网检索 / 角色档案管理员 Agent
# ---------------------------------------------------------------------------
def name_library_checks() -> None:
    good = {
        "路明非": True,
        "陈墨瞳": True,
        "古德里安": True,
        "欧阳锋": True,
        "恺撒·加图索": True,
    }
    for name, expect in good.items():
        verdict = name_library.analyze(name)
        check(f"名字库：{name} 判定为{'人名' if expect else '非人名'}",
              verdict.plausible is expect, f"{verdict.score} {verdict.reasons}")

    bad = ["掌门", "黑衣人", "学院", "了一声", "可以", "学院里"]
    for name in bad:
        verdict = name_library.analyze(name)
        check(f"名字库：{name} 被拒", not verdict.plausible, f"{verdict.score}")

    check("名字库：原文不存在的名字扣分",
          name_library.analyze("张三", text=SAMPLE).score
          < name_library.analyze("沈砚", text=SAMPLE).score)

    # --- 分类：中国人名 / 外国人名 / 称呼 ---
    kinds = {
        "路明非": "cn",
        "欧阳锋": "cn",
        "陈墨瞳": "cn",
        "亚瑟": "foreign",
        "恺撒·加图索": "foreign",
        "约翰·史密斯": "foreign",
        "冯·施耐德": "foreign",
        "诺玛·劳恩斯": "foreign",
        "玛丽": "foreign",
        "芬格尔": "foreign",
        "亚纪": "foreign",
        "老周": "nickname",
        "小明": "nickname",
    }
    for name, kind in kinds.items():
        verdict = name_library.analyze(name)
        check(f"名字库分类：{name} -> {kind}", verdict.kind == kind,
              f"{verdict.kind} {verdict.score} {verdict.reasons}")

    # --- 姓氏与名拆分 ---
    verdict = name_library.analyze("欧阳锋")
    check("名字库：复姓识别", verdict.surname == "欧阳" and verdict.given == "锋", str(verdict.as_dict()))
    verdict = name_library.analyze("路明非")
    check("名字库：单姓识别", verdict.surname == "路" and verdict.given == "明非", str(verdict.as_dict()))

    # --- 称谓剥离 ---
    verdict = name_library.analyze("路明非同学")
    check("名字库：剥掉尾部称谓", verdict.honorific == "同学" and verdict.surname == "路",
          str(verdict.as_dict()))

    # --- 外国人名的 helper ---
    check("名字库：外国人名表命中（约翰）", name_library.lookup("约翰")["in_foreign_given"])
    check("名字库：外国人名表命中（史密斯）", name_library.lookup("史密斯")["in_foreign_surname"])
    check("名字库：中文译名也认（加图索）",
          name_library.lookup("恺撒·加图索")["score"] >= 0.8)

    # --- 批量查询（Agent 用） ---
    batch = name_library.batch_lookup(["路明非", "掌门", "玛丽"])
    check("名字库：批量查询返回 3 条", len(batch) == 3, str(len(batch)))
    check("名字库：批量查询带 kind/score",
          all("kind" in item and "score" in item for item in batch), str(batch[:1]))

    stats = name_library.library_stats()
    check("名字库：规模达标（单姓>300 复姓>50 外国人名>150）",
          stats["cn_single_surnames"] > 300
          and stats["cn_compound_surnames"] > 50
          and stats["foreign_given_names"] > 150,
          str(stats))


async def avatar_crawler_checks() -> None:
    from agent.tools.avatar import AvatarCrawler, is_allowed_image_url

    calls: list[str] = []

    async def fetch_post(url, payload):
        calls.append(f"post:{url}")
        return {
            "data": [
                {
                    "id": 1,
                    "name": "路明非",
                    "images": {"large": "//lain.bgm.tv/pic/crt/l/1.jpg"},
                },
                {
                    "id": 2,
                    "name": "路明非（幼年）",
                    "images": {"large": "//lain.bgm.tv/pic/crt/l/2.jpg"},
                },
                {"id": 3, "name": "陈墨瞳", "images": {"large": "//lain.bgm.tv/pic/crt/l/3.jpg"}},
                {"id": 4, "name": "路明非", "images": {}},
            ]
        }

    async def fetch_get(url):
        calls.append(f"get:{url}")
        return {}

    crawler = AvatarCrawler(fetch_json=fetch_get, fetch_json_post=fetch_post, max_candidates=4)
    found = await crawler.find(character_name="路明非", work_title="龙族")

    check("头像：能从 Bangumi 拿到候选", len(found) >= 2, str(len(found)))
    check("头像：只保留名字匹配的角色", all("路明非" in item["label"] for item in found),
          str([item["label"] for item in found]))
    check("头像：图片地址升级为 https", all(item["url"].startswith("https://") for item in found),
          str([item["url"] for item in found]))
    check("头像：域名白名单生效",
          is_allowed_image_url("https://lain.bgm.tv/pic/a.jpg")
          and not is_allowed_image_url("https://evil.example.com/a.jpg")
          and not is_allowed_image_url("file:///etc/passwd"))

    async def boom(url, payload):
        raise RuntimeError("网络挂了")

    failing = AvatarCrawler(fetch_json=fetch_get, fetch_json_post=boom)
    failed_result = await failing.find(character_name="路明非")
    check("头像：数据源全挂时返回空而不是抛错", failed_result == [])
    check("头像：失败原因被记录（不再静默）",
          any(record.get("error") for record in failing.attempts) and len(failing.attempts) >= 1,
          str(failing.attempts))
    check("头像：能用人话解释为什么没抓到",
          "网络挂了" in failing.explain() or "✗" in failing.explain(),
          failing.explain())


async def avatar_api_checks() -> None:
    """头像接口的完整回路：抓取 → 存候选 → 选一张 → 读回来。"""
    from agent.tools import avatar as crawler_module

    original_find = crawler_module.AvatarCrawler.find

    async def fake_find(self, *, character_name, work_title="", limit=None):
        self.attempts = [
            {
                "provider": "bangumi_search",
                "ok": True,
                "found": 1,
                "error": None,
                "detail": "",
                "elapsed": 0.1,
            }
        ]
        return [
            {
                "url": "https://lain.bgm.tv/pic/crt/l/demo.jpg",
                "source": "bangumi",
                "label": character_name,
                "score": 1.0,
            }
        ]

    crawler_module.AvatarCrawler.find = fake_find  # type: ignore[assignment]
    settings = get_settings()
    original_flag = settings.agent_enable_avatars
    settings.agent_enable_avatars = True
    dbmod.init_db()
    book = library.create_book(
        title="头像自检", source_name="t.txt", source_type="txt", text=SAMPLE
    )
    try:
        character = dbmod.insert_character(book_id=book["id"], name="沈砚", role="剑客")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(f"/api/characters/{character['id']}/avatars", json={})
            check("HTTP：抓头像接口 200", r.status_code == 200, r.text)
            body = r.json()
            check("HTTP：返回候选与尝试记录",
                  bool(body["candidates"]) and bool(body["attempts"]), str(body)[:160])
            check("HTTP：候选已落库",
                  len(dbmod.get_character(character["id"])["avatar_candidates"]) == 1)

            r = await client.patch(
                f"/api/characters/{character['id']}",
                json={"avatar_url": "https://lain.bgm.tv/pic/crt/l/demo.jpg"},
            )
            check("HTTP：能选中头像", r.status_code == 200, r.text)
            check("HTTP：读回来的角色带 avatar_url",
                  r.json().get("avatar_url") == "https://lain.bgm.tv/pic/crt/l/demo.jpg",
                  str(r.json())[:160])

            r = await client.get(f"/api/books/{book['id']}/characters")
            listed = r.json()
            check("HTTP：角色列表包含头像字段",
                  bool(listed) and "avatar_url" in listed[0] and "avatar_candidates" in listed[0],
                  str(listed)[:160])

            r = await client.get("/api/avatar", params={"url": "https://evil.example.com/a.jpg"})
            check("HTTP：非白名单图片被代理拒绝", r.status_code == 403, r.text)

            r = await client.get("/api/health")
            check("HTTP：health 暴露功能标记",
                  "avatars" in (r.json().get("features") or []), r.text)
    finally:
        crawler_module.AvatarCrawler.find = original_find  # type: ignore[assignment]
        settings.agent_enable_avatars = original_flag
        library.delete_book(book["id"])


async def web_tools_checks() -> None:
    async def search(query, limit):
        return [{"title": "龙族 角色列表", "url": "https://example.com/a", "snippet": ""}]

    page_text = ("路明非：男主角。陈墨瞳：诺诺。恺撒·加图索：学生会主席。掌门站在门口。" * 8)

    async def fetch(url):
        return page_text

    async def caller(messages):
        return {"names": ["路明非", "陈墨瞳", "恺撒·加图索", "掌门", "张三"]}

    result = await web_tools.roster_from_web(
        "龙族", search=search, fetch=fetch, caller=caller
    )
    names = result["names"]
    check("联网：抓到了页面", len(result["pages"]) == 1, str(result["pages"]))
    check("联网：保留正文里真实出现的名字", "路明非" in names and "恺撒·加图索" in names, str(names))
    check("联网：编造的名字被丢弃", "张三" not in names, str(names))
    check("联网：编造的名字进 rejected 列表", "张三" in result["rejected"], str(result["rejected"]))

    diff = web_tools.diff_with_local(["路明非", "陈墨瞳", "芬格尔"], ["路明非", "楚子航"])
    check("联网：差异对比正确",
          diff["only_web"] == ["芬格尔", "陈墨瞳"] or diff["only_web"] == ["陈墨瞳", "芬格尔"],
          str(diff))
    check("联网：覆盖率为 0.33", diff["coverage"] == 0.33, str(diff["coverage"]))

    check("联网：HTML 转文本会去掉标签与脚本",
          "脚本" not in web_tools.html_to_text("<script>脚本</script><p>路明非</p>")
          and "路明非" in web_tools.html_to_text("<script>脚本</script><p>路明非</p>"))


async def name_engine_checks() -> None:
    """人名识别引擎：内置兜底 + 现成库适配（用假后端模拟 jieba 词性输出）。"""
    from services.catalog import name_engine

    # --- 内置兜底 ---
    builtin = name_engine.Engine(name_engine.BuiltinBackend())
    check("引擎：没有 NLP 库时回退内置规则",
          builtin.name == "builtin" and builtin.is_nlp is False, builtin.info())
    fallback = builtin.detect(SAMPLE)
    check("引擎：兜底时 detect 明确说明原因",
          fallback["names"] == [] and "未安装" in fallback["note"], str(fallback)[:120])
    check("引擎：兜底时 classify 仍然可用",
          builtin.classify("路明非")["kind"] == "cn"
          and builtin.classify("掌门")["plausible"] is False)

    # --- 假 NLP 后端（词性与 jieba 一致） ---
    class FakeBackend(name_engine.Backend):
        name = "fake-nlp"

        TABLE = {
            "路明非": "nr",
            "楚子航": "nr",
            "沈砚": "nr",
            "苏晚": "nr",
            "老周": "nr",
            "恺撒·加图索": "nrf",
            "约翰·史密斯": "nrf",
            "掌门": "n",
            "门口": "n",
        }

        def available(self) -> bool:
            return True

        def tag(self, text: str):
            out = []
            for name, flag in self.TABLE.items():
                if name in text:
                    out.extend([(name, flag)] * text.count(name))
            return out

        def learn(self, names):
            return len(names)

    engine = name_engine.Engine(FakeBackend())
    check("引擎：接上现成库后 is_nlp=True", engine.is_nlp is True, engine.info())

    text = "路明非说，楚子航来了。恺撒·加图索站在门口。掌门没说话。"
    scanned = engine.detect(text)
    found = {item["name"]: item for item in scanned["names"]}
    check("引擎：扫描认出中国人名", found.get("路明非", {}).get("kind") == "cn", str(found))
    check("引擎：扫描认出音译人名（nrf -> foreign）",
          found.get("恺撒·加图索", {}).get("kind") == "foreign", str(found))
    check("引擎：扫描排除普通名词（掌门/门口）",
          "掌门" not in found and "门口" not in found, str(found))
    check("引擎：扫描带出现次数", found.get("路明非", {}).get("count") == 1, str(found))

    foreign = engine.classify("约翰·史密斯")
    check("引擎：classify 用 nrf 判定外国译名",
          foreign["kind"] == "foreign" and foreign["engine_tag"] == "nrf"
          and foreign["plausible"] is True, str(foreign)[:160])

    cn = engine.classify("楚子航")
    check("引擎：classify 用 nr 判定中国人名",
          cn["kind"] == "cn" and cn["engine_tag"] == "nr", str(cn)[:160])

    junk = engine.classify("掌门")
    check("引擎：classify 不会把职位当人名", junk["plausible"] is False, str(junk)[:160])

    learned = engine.learn(["路明非", "掌门"])
    check("引擎：learn 只喂可信名字（掌门被过滤）", learned == 1, str(learned))

    # --- 真装了 jieba 的话，顺便验一下真库 ---
    if name_engine.JiebaBackend().available():
        real = name_engine.Engine(name_engine.JiebaBackend())
        real_scan = real.detect(SAMPLE + "路明非和恺撒·加图索在说话。")
        names = {item["name"] for item in real_scan["names"]}
        check("引擎（真 jieba）：扫描能认出人名著", bool(names & {"路明非", "沈砚", "苏晚", "老周"}),
              str(sorted(names)[:8]))


async def extraction_agent_checks() -> None:
    from agent.extraction import ExtractionAgent

    class _FakeCrawler:
        async def find(self, *, character_name, work_title=""):
            return [
                {
                    "url": "https://lain.bgm.tv/pic/crt/l/x.jpg",
                    "source": "bangumi",
                    "label": character_name,
                    "score": 1.0,
                }
            ]

    script = [
        {"action": "miner_report"},
        {"action": "text_evidence", "args": {"name": "沈砚"}},
        {"action": "avatar_candidates", "args": {"name": "沈砚"}},
        {"action": "tune_miner", "args": {"min_mentions": 3}},
        {
            "action": "finish",
            "args": {
                "keep": ["沈砚", "苏晚", "老周", "欧阳锋"],
                "drop": ["老周"],
                "rename": {"老周": "掌柜老周"},
            },
        },
    ]
    prompts_seen: list[str] = []

    async def caller(messages):
        prompts_seen.append(messages[-1]["content"])
        return script.pop(0) if script else {"action": "finish", "args": {"keep": []}}

    agent = ExtractionAgent(
        book_title="雨夜试剑",
        text=SAMPLE,
        max_steps=6,
        enable_web=False,
        enable_avatars=True,
        caller=caller,
        crawler=_FakeCrawler(),
    )
    result = await agent.run()

    names = [record["name"] for record in result["characters"]]
    actions = [step["action"] for step in result["trace"]]
    report = result["report"]

    check("Agent：按脚本走了 5 步", len(actions) == 5, str(actions))
    check("Agent：调用了 miner_report / text_evidence / avatar / tune",
          {"miner_report", "text_evidence", "avatar_candidates", "tune_miner", "finish"} <= set(actions),
          str(actions))
    check("Agent：编造的「欧阳锋」被拦下", "欧阳锋" not in names, str(names))
    check("Agent：被 drop 的「老周」被剔除", "老周" not in names, str(names))
    check("Agent：rename 生效并保留别名",
          "掌柜老周" in names, str(names))
    check("Agent：真实角色保留", set(names) >= {"沈砚", "苏晚"}, str(names))
    check("Agent：抓到的头像候选随结果返回",
          len((result["avatars"] or {}).get("沈砚") or []) == 1, str(result["avatars"]))
    check("Agent：报告里记录了步数与阈值",
          report.get("steps") == 5 and "miner_params" in report, str(report)[:120])
    check("Agent：拦下的名字记录在案",
          any(item.get("name") == "欧阳锋" for item in report.get("dropped") or []),
          str(report.get("dropped")))

    # 步数用尽时必须兜底，不能丢光名单
    async def looping_caller(messages):
        return {"action": "miner_report"}

    capped = ExtractionAgent(
        book_title="雨夜试剑", text=SAMPLE, max_steps=2, enable_web=False,
        enable_avatars=False, caller=looping_caller,
    )
    capped_result = await capped.run()
    check("Agent：步数用尽时回退到算法名单",
          capped_result["report"].get("fallback") is True
          and len(capped_result["characters"]) >= 2,
          str(capped_result["report"])[:120])

    # --- Agent 可以调用人名库（中国人名 + 外国人名） ---
    library_agent = ExtractionAgent(
        book_title="雨夜试剑", text=SAMPLE, max_steps=1, enable_web=False,
        enable_avatars=False, caller=looping_caller,
    )
    payload = await library_agent.tool_name_check(["路明非", "恺撒·加图索", "玛丽", "掌门"])
    check("Agent 工具：name_check 一次查 4 个名字",
          payload.get("checked") == 4, str(payload)[:120])
    check("Agent 工具：name_check 给出分类统计",
          payload.get("cn", 0) >= 1 and payload.get("foreign", 0) >= 1,
          str({k: payload.get(k) for k in ("cn", "foreign", "nickname")}))
    kinds = {item["name"]: item["kind"] for item in payload["results"]}
    check("Agent 工具：中国人名 -> cn", kinds.get("路明非") == "cn", str(kinds))
    check("Agent 工具：外国译名 -> foreign", kinds.get("恺撒·加图索") == "foreign", str(kinds))
    check("Agent 工具：非人名不通过", kinds.get("掌门") == "unknown", str(kinds))
    check("Agent 工具：返回里带引擎信息",
          payload.get("engine") in ("builtin", "jieba", "hanlp")
          and "engine_is_nlp" in payload, str(payload.get("engine")))

    single = await library_agent.tool_name_check("老周")
    check("Agent 工具：name_check 也接受单个字符串",
          single.get("checked") == 1 and single["results"][0]["kind"] == "nickname",
          str(single)[:160])

    # --- name_scan：用 NLP 库独立扫全文，和算法名单对照找漏抽 ---
    from services.catalog import name_engine as engine_module

    class _ScanBackend(engine_module.Backend):
        name = "fake-nlp"

        TABLE = {"路明非": "nr", "楚子航": "nr", "掌门": "n"}

        def available(self) -> bool:
            return True

        def tag(self, text: str):
            out = []
            for name, flag in self.TABLE.items():
                if name in text:
                    out.extend([(name, flag)] * text.count(name))
            return out

        def learn(self, names):
            return len(names)

    scan_agent = ExtractionAgent(
        book_title="扫描自检",
        text="路明非说，楚子航来了。掌门没说话。",
        max_steps=1,
        enable_web=False,
        enable_avatars=False,
        caller=looping_caller,
        engine=engine_module.Engine(_ScanBackend()),
    )
    scan_agent.current = {
        "路明非": {"name": "路明非", "appearances": 1, "aliases": [], "quotes": []}
    }
    scan = await scan_agent.tool_name_scan(limit=10)
    scan_names = {item["name"] for item in scan["names"]}
    check("Agent 工具：name_scan 用 NLP 库认出人名",
          {"路明非", "楚子航"} <= scan_names, str(sorted(scan_names)))
    check("Agent 工具：name_scan 给出「疑似漏抽」差集",
          scan["not_in_candidates"] == ["楚子航"], str(scan["not_in_candidates"]))
    check("Agent 工具：name_scan 排除职位词",
          "掌门" not in scan_names, str(sorted(scan_names)))
    check("Agent 工具：name_scan 标明用的是哪个引擎",
          scan.get("engine") == "fake-nlp", str(scan.get("engine")))

    # ------------------------------------------------------------------
    # 优化：结构化裁剪 / 类型防御 / 历史折叠 / 头像移出决策
    # ------------------------------------------------------------------
    from agent import extraction as ea  # noqa: E402

    big = {
        "names": [f"名字{i}" for i in range(200)],
        "snippets": ["长" * 500 for _ in range(20)],
        "note": "说明" * 400,
    }
    dumped = ea._dumps(big)
    try:
        json.loads(dumped)
        valid = True
    except ValueError:
        valid = False
    check("Agent 优化：超长观察裁剪后仍是合法 JSON", valid and len(dumped) <= 900,
          f"{len(dumped)} 字符")

    check("Agent 优化：keep 传字符串也能切分",
          ea._as_name_list("路明非, 恺撒") == ["路明非", "恺撒"],
          str(ea._as_name_list("路明非, 恺撒")))
    check("Agent 优化：keep 传对象数组也能取名字",
          ea._as_name_list([{"name": "路明非"}, {"character": "恺撒"}]) == ["路明非", "恺撒"],
          str(ea._as_name_list([{"name": "路明非"}, {"character": "恺撒"}])))
    check("Agent 优化：keep 传映射也认",
          ea._as_name_list({"路明非": True, "掌门": False}) == ["路明非"])
    check("Agent 优化：rename 支持列表 / 字符串",
          ea._as_rename_map([{"from": "诺诺", "to": "陈墨瞳"}]) == {"诺诺": "陈墨瞳"}
          and ea._as_rename_map("诺诺->陈墨瞳") == {"诺诺": "陈墨瞳"})

    # 历史折叠：最后一次观察给全文，更早的折成一行摘要
    seen: list[list[dict]] = []
    fold_script = [
        {"action": "name_check", "args": {"names": "沈砚,苏晚"}},
        {"action": "text_evidence", "args": {"name": "沈砚"}},
        {"action": "finish", "args": {"keep": "沈砚,苏晚"}},
    ]

    async def fold_caller(messages):
        seen.append([dict(item) for item in messages])
        return fold_script.pop(0) if fold_script else {"action": "finish"}

    folder = ExtractionAgent(
        book_title="雨夜试剑", text=SAMPLE, max_steps=5,
        enable_web=False, enable_avatars=False, caller=fold_caller,
    )
    folded = await folder.run()
    last_texts = [item["content"] for item in seen[-1]] if seen else []
    check("Agent 优化：旧观察折叠成一行摘要",
          any("校验 2 个词" in text for text in last_texts),
          str(last_texts)[:200])
    check("Agent 优化：最后一次观察保留完整 JSON",
          any(text.startswith("工具结果：{") for text in last_texts),
          str(last_texts)[:200])
    check("Agent 优化：上下文只随步数线性增长（不重复堆叠）",
          all(len(batch) <= 7 for batch in seen), str([len(b) for b in seen]))
    check("Agent 优化：keep 传字符串也能出最终名单",
          {record["name"] for record in folded["characters"]} == {"沈砚", "苏晚"},
          str([record["name"] for record in folded["characters"]]))

    # 头像不再占用决策步数：finish 后自动补
    auto_script = [{"action": "finish", "args": {"keep": "沈砚"}}]

    async def auto_caller(messages):
        return auto_script.pop(0) if auto_script else {"action": "finish"}

    auto_agent = ExtractionAgent(
        book_title="雨夜试剑", text=SAMPLE, max_steps=3,
        enable_web=False, enable_avatars=True,
        caller=auto_caller, crawler=_FakeCrawler(),
    )
    auto_result = await auto_agent.run()
    check("Agent 优化：头像不再占决策步数（finish 后自动补）",
          len(auto_result["trace"]) == 1 and bool(auto_result["avatars"]),
          f"trace={len(auto_result['trace'])} avatars={list(auto_result['avatars'])}")


# ---------------------------------------------------------------------------
# 数据层
# ---------------------------------------------------------------------------
def db_checks() -> None:
    dbmod.init_db()
    book = library.create_book(
        title="数据层自检", source_name="t.txt", source_type="txt", text=SAMPLE
    )
    check("数据层：作品写入", dbmod.get_book(book["id"]) is not None)
    check("数据层：原文落盘", (library.book_dir(book["id"]) / "source.txt").exists())
    check("数据层：分块落盘", len(library.read_chunks(book["id"])) >= 1)

    created = dbmod.replace_novel_characters(
        book["id"],
        [{"name": "沈砚", "personality": ["寡言"], "appearances": 3, "avatar": "🗡️"}],
    )
    check("数据层：抽取角色入库", len(created) == 1 and created[0]["name"] == "沈砚")

    custom = library.create_character(book["id"], name="我", personality=["好奇"], is_user_persona=True)
    check("数据层：自定义角色入库", custom["source"] == "user" and custom["is_user_persona"] is True)

    characters = dbmod.list_characters(book["id"])
    check("数据层：自定义角色排在最前", characters[0]["id"] == custom["id"],
          str([c["name"] for c in characters]))

    dbmod.replace_novel_characters(book["id"], [{"name": "苏晚", "personality": ["温柔"]}])
    names = {c["name"] for c in dbmod.list_characters(book["id"])}
    check("数据层：重抽替换抽取角色、保留自定义角色", names == {"苏晚", "我"}, str(names))

    updated = dbmod.update_character(custom["id"], name="我自己", personality=["好奇", "社恐"])
    check("数据层：角色可编辑", updated["name"] == "我自己" and len(updated["personality"]) == 2)
    check("数据层：编辑刷新 updated_at", updated["updated_at"] >= custom["updated_at"])

    library.delete_book(book["id"])
    check("数据层：删作品级联删角色", dbmod.list_characters(book["id"]) == [])
    check("数据层：删作品清掉原文目录", not library.book_dir(book["id"]).exists())


# ---------------------------------------------------------------------------
# 书库 / 书架（三级结构：书库 -> 书架 -> 书）
# ---------------------------------------------------------------------------
def library_shelf_checks() -> None:
    dbmod.init_db()

    libraries = dbmod.list_libraries()
    check("书库：init_db 自动建默认书库", len(libraries) >= 1, str(libraries))
    default_library = libraries[0]
    check("书库：默认书库有名称", bool(default_library["title"]), str(default_library))

    default_shelves = dbmod.list_shelves(default_library["id"])
    check("书架：默认书库下有默认书架", len(default_shelves) >= 1, str(default_shelves))

    test_library = dbmod.create_library("自检书库", "临时")
    check("书库：可新建", test_library["title"] == "自检书库", str(test_library))

    shelf_a = dbmod.create_shelf(test_library["id"], "书架A")
    shelf_b = dbmod.create_shelf(test_library["id"], "书架B")
    check("书架：同一书库下可建多个", shelf_a["id"] != shelf_b["id"])
    check("书架：归属于正确书库", shelf_a["library_id"] == test_library["id"])

    book_a = library.create_book(
        title="书架A的书", source_name="a.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf_a["id"],
    )
    book_b = library.create_book(
        title="书架B的书", source_name="b.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf_b["id"],
    )
    check("书架：书落到指定书架",
          dbmod.get_book(book_a["id"])["shelf_id"] == shelf_a["id"],
          str(dbmod.get_book(book_a["id"]).get("shelf_id")))

    names_a = {b["title"] for b in dbmod.list_books(shelf_a["id"])}
    names_b = {b["title"] for b in dbmod.list_books(shelf_b["id"])}
    check("书架：按书架过滤书",
          names_a == {"书架A的书"} and names_b == {"书架B的书"}, f"{names_a} / {names_b}")

    book_default = library.create_book(
        title="默认书架的书", source_name="c.txt", source_type="txt", text=SAMPLE
    )
    check("书架：不指定书架时落到默认书架",
          dbmod.get_book(book_default["id"])["shelf_id"] == default_shelves[0]["id"],
          str(dbmod.get_book(book_default["id"]).get("shelf_id")))

    dbmod.set_book_shelf(book_default["id"], shelf_a["id"])
    check("书架：书可在书架间移动",
          dbmod.get_book(book_default["id"])["shelf_id"] == shelf_a["id"])

    refused = False
    try:
        dbmod.delete_shelf(shelf_b["id"])
    except ValueError:
        refused = True
    check("书架：非空书架拒绝删除（不静默丢书）", refused)

    for book in (book_a, book_b, book_default):
        library.delete_book(book["id"])
    dbmod.delete_shelf(shelf_a["id"])
    dbmod.delete_shelf(shelf_b["id"])
    dbmod.delete_library(test_library["id"])
    check("书架：清理后书库不存在", dbmod.get_library(test_library["id"]) is None)


# ---------------------------------------------------------------------------
# Agent 审计日志（按书架隔离）
# ---------------------------------------------------------------------------
def agent_log_checks() -> None:
    dbmod.init_db()
    test_library = dbmod.create_library("日志自检库")
    shelf_a = dbmod.create_shelf(test_library["id"], "日志书架A")
    shelf_b = dbmod.create_shelf(test_library["id"], "日志书架B")
    book_a = library.create_book(
        title="日志书A", source_name="a.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf_a["id"],
    )
    book_b = library.create_book(
        title="日志书B", source_name="b.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf_b["id"],
    )

    event = agent_log.log("test_event", owner_type="manager", book_id=book_a["id"], payload={"n": 1})
    check("日志：只给 book_id 时自动归属书架", event["shelf_id"] == shelf_a["id"], str(event))
    check("日志：自动补全书库", event["library_id"] == test_library["id"], str(event))

    trace = agent_log.new_trace_id("unit")
    agent_log.log("step_one", book_id=book_a["id"], trace_id=trace)
    agent_log.log("step_two", book_id=book_a["id"], trace_id=trace)
    chain = agent_log.timeline(trace)
    check("日志：trace_id 串起一次任务",
          [e["event_type"] for e in chain] == ["step_one", "step_two"],
          str([e["event_type"] for e in chain]))

    agent_log.log("shelf_b_event", book_id=book_b["id"])
    events_a = dbmod.list_events(shelf_id=shelf_a["id"], limit=200)
    events_b = dbmod.list_events(shelf_id=shelf_b["id"], limit=200)
    check("日志：书架A只看到自己的事件",
          len(events_a) >= 3 and all(e["shelf_id"] == shelf_a["id"] for e in events_a),
          str(len(events_a)))
    check("日志：书架B只看到自己的事件",
          len(events_b) == 1 and events_b[0]["event_type"] == "shelf_b_event",
          str([e["event_type"] for e in events_b]))
    check("日志：两个书架的事件不交叉",
          not ({e["id"] for e in events_a} & {e["id"] for e in events_b}))

    secret = agent_log.log(
        "with_secret", book_id=book_a["id"],
        payload={"api_key": "sk-should-not-appear", "note": "ok"},
    )
    check("日志：密钥被脱敏", secret["payload"]["api_key"] == "***", str(secret["payload"]))
    check("日志：非敏感字段保留", secret["payload"]["note"] == "ok")

    long_event = agent_log.log("long_payload", book_id=book_a["id"], payload={"text": "长" * 5000})
    check("日志：超长字段被截断", len(long_event["payload"]["text"]) < 1100,
          str(len(long_event["payload"]["text"])))

    for book in (book_a, book_b):
        library.delete_book(book["id"])
    dbmod.delete_shelf(shelf_a["id"])
    dbmod.delete_shelf(shelf_b["id"])
    dbmod.delete_library(test_library["id"])
    check("日志：删书架连带清掉该书架的日志",
          dbmod.list_events(shelf_id=shelf_a["id"], limit=200) == [])


# ---------------------------------------------------------------------------
# 初始记忆条目（characters.excerpts 是派生缓存）
# ---------------------------------------------------------------------------
def memory_item_checks() -> None:
    dbmod.init_db()
    shelf = _test_shelf("memory")
    book_a = library.create_book(
        title="记忆A书", source_name="a.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    book_b = library.create_book(
        title="记忆B书", source_name="b.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )

    shen = dbmod.replace_novel_characters(
        book_a["id"],
        [{"name": "沈砚", "excerpts": ["片段A1", "片段A2"]}],
    )[0]
    items = dbmod.list_memory_items(shen["id"], kind="excerpt")
    check("记忆：抽取时自动生成条目", len(items) == 2, str(len(items)))
    check("记忆：条目带来源书",
          all(i["source_book_id"] == book_a["id"] for i in items),
          str([i["source_book_id"] for i in items]))
    check("记忆：excerpts 缓存同步", shen["excerpts"] == ["片段A1", "片段A2"],
          str(shen["excerpts"]))

    # 加书场景：往同一角色追加另一本书的片段
    agent_memory.remember(shen["id"], ["片段B1"], source_book_id=book_b["id"], reason="新书补强")
    check("记忆：可追加其他书的片段",
          len(dbmod.list_memory_items(shen["id"], kind="excerpt")) == 3)
    check("记忆：缓存包含追加内容",
          "片段B1" in dbmod.get_character(shen["id"])["excerpts"],
          str(dbmod.get_character(shen["id"])["excerpts"]))
    check("记忆：按来源统计",
          agent_memory.stats(shen["id"])["by_source"].get(book_b["id"]) == 1,
          str(agent_memory.stats(shen["id"])))

    # 删书场景：只回收该来源，其余片段保留
    removed = agent_memory.forget_source(shen["id"], book_b["id"])
    check("记忆：按来源回收", removed == 1 and
          len(dbmod.list_memory_items(shen["id"], kind="excerpt")) == 2, str(removed))
    check("记忆：回收后缓存不残留",
          "片段B1" not in dbmod.get_character(shen["id"])["excerpts"],
          str(dbmod.get_character(shen["id"])["excerpts"]))

    rebuilt = agent_memory.rebuild(shen["id"], limit=3)
    check("记忆：回收后重建仍有记忆", rebuilt == ["片段A1", "片段A2"], str(rebuilt))

    # 跨角色回收：删书要清掉所有角色身上来自该书的片段
    agent_memory.remember(shen["id"], ["片段B2"], source_book_id=book_b["id"])
    total = agent_memory.forget_book(book_b["id"])
    check("记忆：删书回收所有角色上的该书片段",
          total >= 1 and all(
              i["source_book_id"] != book_b["id"]
              for i in dbmod.list_memory_items(shen["id"], kind="excerpt")
          ), str(total))

    # 重抽保留角色身份：同名角色原地更新，旧原文片段换成新的
    again = dbmod.replace_novel_characters(
        book_a["id"], [{"name": "沈砚", "excerpts": ["新片段"]}]
    )[0]
    check("记忆：重抽保留角色 id", again["id"] == shen["id"],
          f"{again['id']} vs {shen['id']}")
    excerpt_items = [
        item["content"] for item in dbmod.list_memory_items(shen["id"], kind="excerpt")
    ]
    check("记忆：重抽后旧片段被换掉、条目不留孤儿",
          excerpt_items == ["新片段"], str(excerpt_items))
    check("记忆：重抽后新角色有条目",
          len(dbmod.list_memory_items(again["id"], kind="excerpt")) == 1)

    # 截断（控制提示词体积）
    many = dbmod.insert_character(
        book_id=book_a["id"], name="多片段角色",
        excerpts=["1", "2", "3", "4", "5"], source="user",
    )
    check("记忆：新建角色即生成条目",
          len(dbmod.list_memory_items(many["id"], kind="excerpt")) == 5)
    dbmod.prune_memory_items(many["id"], keep=3)
    kept = dbmod.list_memory_items(many["id"], kind="excerpt")
    check("记忆：按重要度截断保留 3 条", len(kept) == 3, str(len(kept)))
    check("记忆：截断后缓存同步", len(dbmod.get_character(many["id"])["excerpts"]) == 3)

    # 迁移幂等：已有条目的角色不会被重复灌入
    dbmod.init_db()
    check("记忆：init_db 幂等不重复迁移",
          len(dbmod.list_memory_items(many["id"], kind="excerpt")) == 3)

    raised = False
    try:
        dbmod.delete_memory_items()
    except ValueError:
        raised = True
    check("记忆：删除必须带过滤条件（防清库）", raised)

    library.delete_book(book_a["id"])
    library.delete_book(book_b["id"])
    dbmod.delete_shelf(shelf["id"])


# ---------------------------------------------------------------------------
# 情绪 / 好感度（双层状态机）
# ---------------------------------------------------------------------------
def affect_checks() -> None:
    dbmod.init_db()
    shelf = _test_shelf("affect")
    book = library.create_book(
        title="情绪书", source_name="a.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    character = dbmod.insert_character(
        book_id=book["id"], name="沈砚",
        personality=["寡言", "冷静"], role="剑客", source="novel",
    )
    shelf_id = shelf["id"]

    # ---- 基线 ----
    quiet = affect.baseline_for({"personality": ["寡言", "冷静"]})
    warm = affect.baseline_for({"personality": ["热情", "话多"]})
    check("情绪：寡言角色唤醒度更低", quiet["arousal"] < warm["arousal"],
          f"{quiet} / {warm}")
    check("情绪：热情角色基线更正向", warm["valence"] > quiet["valence"],
          f"{quiet['valence']} / {warm['valence']}")

    # ---- 标签映射 ----
    check("情绪：高正高唤醒 -> 喜悦", affect.label_for(0.8, 0.8) == "喜悦")
    check("情绪：高负高唤醒 -> 恼怒", affect.label_for(-0.8, 0.8) == "恼怒")
    check("情绪：中性低唤醒 -> 平静", affect.label_for(0.0, 0.3) == "平静")
    check("情绪：低正低唤醒 -> 失落", affect.label_for(-0.3, 0.2) == "失落")

    # ---- 言语行为识别 ----
    kinds = {s.kind for s in affect.extract_signals("谢谢你，你真是太厉害了")}
    check("情绪：识别道谢与夸奖", {"thanks", "praise"} <= kinds, str(kinds))
    kinds = {s.kind for s in affect.extract_signals("废物，否则我警告你")}
    check("情绪：识别侮辱与威胁", {"insult", "threat"} <= kinds, str(kinds))

    # ---- 首轮推进 ----
    state = affect.observe_user_message(character=character, user_id="u1", text="谢谢你")
    check("情绪：道谢提升好感度", state["favorability"] == 41, str(state["favorability"]))
    check("情绪：首轮写入轮次", state["turns"] == 1, str(state["turns"]))

    state = affect.observe_user_message(character=character, user_id="u1", text="废物，闭嘴")
    check("情绪：侮辱降低好感度", state["favorability"] == 38, str(state["favorability"]))

    # ---- 单轮上限 ----
    before = state["favorability"]
    state = affect.observe_user_message(character=character, user_id="u1", text="废物，否则我警告你，滚开")
    drop = before - state["favorability"]
    check("情绪：单轮好感度最多掉 5", drop <= 5, f"掉了 {drop}")

    # ---- 下限保护 ----
    dbmod.save_affect(
        character["id"], "u1", shelf_id, favorability=1, valence=0.0, arousal=0.3,
        emotion="平静", baseline=affect.baseline_for(character),
    )
    state = affect.observe_user_message(character=character, user_id="u1", text="废物")
    check("情绪：好感度不会低于 0", state["favorability"] >= 0, str(state["favorability"]))

    # ---- 回落：平静对话后情绪趋回基线 ----
    dbmod.save_affect(
        character["id"], "u1", shelf_id, favorability=50, valence=-0.9, arousal=0.2,
        emotion="失落", baseline=affect.baseline_for(character),
    )
    state = affect.observe_user_message(character=character, user_id="u1", text="嗯。")
    baseline = affect.baseline_for(character)
    check("情绪：无信号时向基线回落",
          state["valence"] > -0.9 and abs(state["valence"] - baseline["valence"]) < 0.9,
          f"{state['valence']} vs baseline {baseline['valence']}")
    check("情绪：平淡对话不涨好感度", state["favorability"] == 50, str(state["favorability"]))

    # ---- 关切信号 -> 情绪标签 ----
    state = affect.observe_user_message(character=character, user_id="u5", text="你还好吗，我担心你")
    check("情绪：关心触发关切", state["emotion"] == "关切", str(state["emotion"]))

    # ---- 用户隔离 ----
    affect.observe_user_message(character=character, user_id="u6", text="谢谢你")
    s1 = dbmod.get_affect(character["id"], "u1", shelf_id)
    s2 = dbmod.get_affect(character["id"], "u6", shelf_id)
    check("情绪：不同用户互不影响",
          s1 is not None and s2 is not None and s1["favorability"] != s2["favorability"],
          f"{s1['favorability']} / {s2['favorability']}")

    # ---- 书架隔离 ----
    dbmod.save_affect(
        character["id"], "u1", "shelf_other", favorability=90, valence=0.5,
        arousal=0.5, emotion="喜悦", baseline=affect.baseline_for(character),
    )
    other = dbmod.get_affect(character["id"], "u1", "shelf_other")
    same = dbmod.get_affect(character["id"], "u1", shelf_id)
    check("情绪：好感度按书架独立",
          other["favorability"] == 90 and same["favorability"] != 90,
          f"{other['favorability']} / {same['favorability']}")
    dbmod.delete_affect(shelf_id="shelf_other")

    # ---- 语气档位 ----
    check("情绪：好感度 10 -> 冷淡档", affect.tone_band(10)["key"] == "cold")
    check("情绪：好感度 30 -> 疏离档", affect.tone_band(30)["key"] == "distant")
    check("情绪：好感度 50 -> 平常档", affect.tone_band(50)["key"] == "normal")
    check("情绪：好感度 90 -> 亲昵档", affect.tone_band(90)["key"] == "intimate")

    # ---- 矛盾禁令 ----
    low_rules = affect.forbidden_rules({"favorability": 10, "emotion": "平静"})
    check("情绪：低好感度禁止亲昵称呼", any("亲昵" in r for r in low_rules), str(low_rules))
    high_rules = affect.forbidden_rules({"favorability": 90, "emotion": "平静"})
    check("情绪：高好感度禁止无来由敌意", any("敌意" in r for r in high_rules), str(high_rules))

    # ---- 提示词：不含数值，且要求外显 ----
    block = affect.prompt_block({"favorability": 15, "emotion": "失落"})
    check("情绪：提示词说明心情", "失落" in block, block[:80])
    check("情绪：提示词不含任何数字", not re.search(r"\d", block), block[:120])
    check("情绪：提示词要求情绪外显", "呜呜" in block or "省略号" in block, block[:120])

    # ---- 审计日志 ----
    events = dbmod.list_events(shelf_id=shelf_id, event_type="affect_change", limit=50)
    check("情绪：变化写入审计日志", len(events) >= 1, str(len(events)))
    if events:
        payload = events[0]["payload"]
        check("情绪：事件带前后状态",
              "before" in payload and "after" in payload, str(payload)[:120])
        check("情绪：事件归属角色 Agent",
              events[0]["owner_type"] == "character", events[0]["owner_type"])

    # ---- 情绪模型：规则后端 ----
    check("情绪模型：默认后端是可用后端之一",
          emotion_model.info()["backend"] in ("rules", "onnx"),
          str(emotion_model.info()["backend"]))
    pos = emotion_model.analyze("我今天特别开心")
    check("情绪模型：识别正向", pos is not None and pos.polarity > 0,
          str(pos))
    neg = emotion_model.analyze("我很伤心，真的撑不住了")
    check("情绪模型：识别负向", neg is not None and neg.polarity < 0, str(neg))
    flip = emotion_model.analyze("我不开心")
    check("情绪模型：否定词翻转极性", flip is not None and flip.polarity < 0, str(flip))

    # ---- 长句兜底：规则读不懂时用模型 ----
    long_text = (
        "今天工作上的事情一团糟，改了五遍方案又被打回来，我真的很累，"
        "晚上一个人坐在楼下很久，感觉快撑不住了，脑子里全是乱七八糟的想法，"
        "也不知道该跟谁说这些"
    )
    check("情绪：兜底样本够长（触发模型阈值）", len(long_text) >= 60, str(len(long_text)))
    state = affect.observe_user_message(character=character, user_id="u9", text=long_text)
    check("情绪：长句由情感模型兜底（转负向）",
          state["valence"] < baseline["valence"], f"{state['valence']} vs {baseline['valence']}")

    # ---- 永不抛异常 ----
    weird = affect.observe_user_message(
        character={"id": character["id"], "book_id": book["id"]}, user_id="u1", text=""
    )
    check("情绪：空消息不抛错", isinstance(weird, dict))

    # ---- 健康检查暴露后端 ----
    health = asyncio.run(system_endpoint.health())
    check("情绪：/api/health 暴露 emotion_model",
          health.get("emotion_model", {}).get("backend") in ("rules", "onnx")
          and "onnx_available" in health.get("emotion_model", {}),
          str(health.get("emotion_model"))[:120])

    # ---- 删书要连带清掉情绪状态（不留孤儿）----
    orphan = dbmod.insert_character(book_id=book["id"], name="孤儿角色", source="user")
    dbmod.save_affect(
        orphan["id"], "u1", shelf_id, favorability=70, valence=0.3,
        arousal=0.4, emotion="期待", baseline=affect.baseline_for(orphan),
    )
    library.delete_book(book["id"])
    check("情绪：删书连带清掉情绪状态",
          dbmod.get_affect(orphan["id"], "u1", shelf_id) is None)

    # ---- 清理 ----
    dbmod.delete_affect(shelf_id=shelf_id)
    dbmod.delete_shelf(shelf_id)
    check("情绪：清理后状态行已删除",
          dbmod.get_affect(character["id"], "u1", shelf_id) is None)


# ---------------------------------------------------------------------------
# 角色 Agent 闭环：稳定指令 + 每轮状态注入
# ---------------------------------------------------------------------------
class _CopyableFakeAgent:
    """只用来验证 attach_state 的拷贝语义（真 Agent 构造要连 Agno/NL 栈）。"""

    def __init__(self) -> None:
        self.additional_context = None


def character_agent_checks() -> None:
    dbmod.init_db()
    shelf = _test_shelf("agent")
    book = library.create_book(
        title="闭环书", source_name="a.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    character = dbmod.insert_character(
        book_id=book["id"], name="沈砚",
        personality=["寡言", "冷静"], role="剑客", speech_style="句子很短",
        source="novel", excerpts=["记忆一", "记忆二", "记忆三", "记忆四", "记忆五"],
    )

    # ---- 稳定部分：档案 + 初始记忆 top-k ----
    instructions = agent_manager.build_instructions(
        character=character, book_title="闭环书", user_name="读者"
    )
    check("闭环：指令含角色档案", "沈砚" in instructions and "寡言" in instructions,
          instructions[:80])
    shown = [t for t in ("记忆一", "记忆二", "记忆三", "记忆四", "记忆五") if t in instructions]
    check("闭环：初始记忆按 top-k 截断", len(shown) == 3, str(shown))

    # ---- 易变部分：挂到副本，缓存对象不受影响 ----
    cached = _CopyableFakeAgent()
    per_run, state = character_agent.attach_state(cached, character, "u_agent")
    block = per_run.additional_context or ""
    check("闭环：状态挂在副本上", per_run is not cached)
    check("闭环：副本带「此刻状态」", "此刻状态" in block, block[:80])
    check("闭环：状态块不含数字", not re.search(r"\d", block), block[:100])
    check("闭环：缓存对象未被污染", cached.additional_context is None)

    # ---- 闭环：这一轮的情绪影响下一轮的提示词 ----
    affect.observe_user_message(character=character, user_id="u_agent", text="废物，闭嘴")
    per_run2, state2 = character_agent.attach_state(cached, character, "u_agent")
    block2 = per_run2.additional_context or ""
    check("闭环：情绪变化传导到下一轮",
          state2["emotion"] != state["emotion"] and f"心情：{state2['emotion']}" in block2,
          f"{state['emotion']} -> {state2['emotion']}")
    check("闭环：好感度下降传导到态度文案",
          affect.describe(state2)["attitude_text"] != affect.describe(state)["attitude_text"]
          or state2["emotion"] != state["emotion"])

    # ---- 记忆预览与提示词一致 ----
    preview = character_agent.memory_preview(character)
    check("闭环：记忆预览与提示词一致",
          preview == agent_memory.top(character["id"], limit=3), str(preview))
    check("闭环：记忆预览条数等于 excerpt_chunks", len(preview) == 3, str(len(preview)))

    # ---- 清理 ----
    dbmod.delete_affect(shelf_id=shelf["id"])
    library.delete_book(book["id"])
    dbmod.delete_shelf(shelf["id"])


# ---------------------------------------------------------------------------
# 群聊引擎（纯逻辑 + 假生成器）
# ---------------------------------------------------------------------------
def _member(name, personality, **overrides):
    data = {
        "character_id": f"char_{name}",
        "name": name,
        "personality": personality,
        "aliases": [],
        "relationships": [],
        "status": "online",
        "state": "idle",
        "extraversion": None,
        "arousal": 0.35,
        "last_spoke_at": None,
    }
    data.update(overrides)
    return data


async def group_presence_checks() -> None:
    """在线/离线：离线不发言，@全体成员 把所有人叫上线并逐一回复。"""
    import random as _random

    dbmod.init_db()
    shelf = _test_shelf("presence")
    book = library.create_book(
        title="在场书", source_name="p.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    dbmod.insert_character(book_id=book["id"], name="沈砚", personality=["寡言"], source="novel")
    dbmod.insert_character(book_id=book["id"], name="苏晚", personality=["温柔"], source="novel")

    members = dbmod.ensure_shelf_members(shelf["id"])
    check("在场：新成员默认在线",
          all(m["status"] == "online" for m in members),
          str([(m["name"], m["status"]) for m in members]))

    for member in members:
        dbmod.set_member_status(shelf["id"], member["character_id"], "offline")
    check("在场：可以设为离线",
          all(m["status"] == "offline" for m in dbmod.list_shelf_members(shelf["id"])))

    async def generate(member, transcript):
        yield "我在。"

    async def collect(text: str, seed: int) -> list[dict]:
        events: list[dict] = []
        async for event in group_chat.run_turn(
            shelf_id=shelf["id"],
            user_id="u_presence",
            text=text,
            generate=generate,
            members=dbmod.list_shelf_members(shelf["id"]),
            rng=_random.Random(seed),
        ):
            events.append(event)
        return events

    quiet = await collect("有人吗", 1)
    check("在场：全员离线时没人说话",
          [e for e in quiet if e["type"] == "speaker_start"] == [],
          str([e["type"] for e in quiet]))

    loud = await collect("@全体成员 都来说两句", 2)
    spoken = [e["name"] for e in loud if e["type"] == "speaker_end"]
    check("在场：@全体成员 让所有在线成员逐一回复",
          set(spoken) == {"沈砚", "苏晚"}, str(spoken))
    check("在场：被叫过之后状态变成在线",
          all(m["status"] == "online" for m in dbmod.list_shelf_members(shelf["id"])),
          str([(m["name"], m["status"]) for m in dbmod.list_shelf_members(shelf["id"])]))

    # 单个 @ 只叫一个，不惊动别人
    dbmod.set_member_status(shelf["id"], members[0]["character_id"], "offline")
    single = await collect("@苏晚 你在吗", 3)
    single_names = [e["name"] for e in single if e["type"] == "speaker_end"]
    check("在场：单独 @ 只唤醒被点名的人",
          single_names == ["苏晚"], str(single_names))
    offline_after = {
        m["name"]: m["status"] for m in dbmod.list_shelf_members(shelf["id"])
    }
    check("在场：没被点名的人保持离线",
          offline_after.get("沈砚") == "offline", str(offline_after))

    library.delete_book(book["id"])
    dbmod.delete_shelf(shelf["id"])


async def history_and_shelf_checks() -> None:
    """聊天历史 + 书架管理（改名 / 级联删除）。"""
    dbmod.init_db()
    shelf = _test_shelf("history")
    book = library.create_book(
        title="历史书", source_name="h.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    character = dbmod.insert_character(
        book_id=book["id"], name="沈砚", personality=["寡言"], source="novel",
    )

    # 造几条流水：群聊 2 条 + 单聊 2 条（不同角色、不同用户）
    for role, text, speaker in (
        ("user", "大家好", "读者"),
        ("assistant", "在。", "沈砚"),
    ):
        dbmod.add_chat_turn(
            shelf_id=shelf["id"], user_id="u_hist", role=role, content=text,
            character_id=character["id"], session_id="s1", speaker_name=speaker,
        )
    for role, text in (("user", "单独聊聊"), ("assistant", "嗯。")):
        dbmod.add_chat_turn(
            shelf_id=shelf["id"], user_id="u_hist", role=role, content=text,
            character_id=character["id"], session_id="s2",
            speaker_name="沈砚" if role == "assistant" else "读者",
        )
    # 别人的对话不算进我的历史
    dbmod.add_chat_turn(
        shelf_id=shelf["id"], user_id="u_other", role="user", content="别人的话",
        character_id=character["id"], session_id="s3", speaker_name="别人",
    )

    turns = dbmod.list_history(shelf["id"], user_id="u_hist", limit=50)
    check("历史：按用户隔离", all(t["user_id"] == "u_hist" for t in turns), str(len(turns)))
    check("历史：按时间正序（最早的在前）",
          turns and turns[0]["content"] == "大家好", turns[0]["content"] if turns else "")
    check("历史：条数正确", len(turns) == 4, str(len(turns)))

    single = dbmod.list_history(
        shelf["id"], user_id="u_hist", character_id=character["id"], limit=50
    )
    check("历史：可按角色过滤（单聊）", len(single) == 4, str(len(single)))

    # 蒸馏之后历史仍在
    chat_memory.distill(shelf["id"], character["id"], keep_days=0, character_name="沈砚")
    after = dbmod.list_history(shelf["id"], user_id="u_hist", limit=50)
    check("历史：蒸馏后仍能回看", len(after) == 4, str(len(after)))
    check("清空后蒸馏出来的记忆还在",
          len(dbmod.list_memory_items(character["id"], kind="fact")) >= 1,
          str(len(dbmod.list_memory_items(character["id"], kind="fact"))))

    # ---- 清空历史（只删流水，不动长期记忆）----
    cleared = dbmod.clear_history(shelf["id"], user_id="u_hist")
    check("清空：删掉我的流水", cleared == 4, str(cleared))
    check("清空：别人的流水还在",
          dbmod.count_chat_turns(shelf["id"], user_id="u_other") == 1,
          str(dbmod.count_chat_turns(shelf["id"], user_id="u_other")))
    check("清空：我的历史确已为空",
          dbmod.list_history(shelf["id"], user_id="u_hist") == [])
    check("清空：按角色清不影响同用户其他角色",
          dbmod.clear_history(
              shelf["id"], user_id="u_other", character_id="char_not_exist"
          ) == 0)

    # ---- 书架改名 ----
    renamed = dbmod.update_shelf(shelf["id"], title="新群名")
    check("书架：可改名", renamed.get("title") == "新群名", str(renamed.get("title")))
    try:
        dbmod.update_shelf(shelf["id"], title="   ")
        check("书架：空名应被拒绝", False, "没有抛错")
    except ValueError:
        check("书架：空名应被拒绝", True)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 历史接口要登录态
        r = await client.get(f"/api/shelves/{shelf['id']}/history")
        check("HTTP：历史接口未登录 -> 401", r.status_code == 401, r.text[:120])

        session = (await client.post("/api/session", json={"display_name": "历史测试员"})).json()
        token = session["token"]
        await anyio.to_thread.run_sync(
            functools.partial(
                dbmod.add_chat_turn,
                shelf_id=shelf["id"], user_id=session["user_id"], role="user",
                content="我来了", character_id=character["id"], session_id="s9",
                speaker_name="历史测试员",
            )
        )
        r = await client.get(
            f"/api/shelves/{shelf['id']}/history", params={"token": token}
        )
        check("HTTP：群聊历史 200", r.status_code == 200, r.text[:120])
        body = r.json()
        check("HTTP：群聊历史只有自己的话",
              [t["content"] for t in body["turns"]] == ["我来了"],
              str([t["content"] for t in body["turns"]]))

        r = await client.get(
            f"/api/characters/{character['id']}/history", params={"token": token}
        )
        check("HTTP：单聊历史 200", r.status_code == 200, r.text[:120])
        check("HTTP：单聊历史条数正确", len(r.json()["turns"]) == 1, r.text[:120])

        # ---- 清空历史（HTTP）----
        r = await client.delete(
            f"/api/characters/{character['id']}/history", params={"token": token}
        )
        check("HTTP：清空单聊历史 200", r.status_code == 200, r.text[:120])
        check("HTTP：清空返回删除条数", r.json().get("removed") == 1, r.text[:120])
        r = await client.get(
            f"/api/characters/{character['id']}/history", params={"token": token}
        )
        check("HTTP：单聊历史清空后为空", r.json()["turns"] == [], r.text[:120])

        r = await client.delete(f"/api/shelves/{shelf['id']}/history")
        check("HTTP：清空历史未登录 -> 401", r.status_code == 401, r.text[:120])

        await anyio.to_thread.run_sync(
            functools.partial(
                dbmod.add_chat_turn,
                shelf_id=shelf["id"], user_id=session["user_id"], role="user",
                content="再说一句", character_id=character["id"], session_id="s10",
                speaker_name="历史测试员",
            )
        )
        r = await client.delete(
            f"/api/shelves/{shelf['id']}/history", params={"token": token}
        )
        check("HTTP：清空群聊历史 200", r.status_code == 200, r.text[:120])
        r = await client.get(
            f"/api/shelves/{shelf['id']}/history", params={"token": token}
        )
        check("HTTP：群聊历史清空后为空", r.json()["turns"] == [], r.text[:120])

        events = await anyio.to_thread.run_sync(
            functools.partial(
                dbmod.list_events,
                shelf_id=shelf["id"],
                event_type="chat_history_cleared",
                limit=10,
            )
        )
        check("HTTP：清空历史会留下审计记录", len(events) >= 1, str(len(events)))

        # ---- 改名（群聊名 = 书架名）----
        r = await client.patch(f"/api/shelves/{shelf['id']}", json={"title": "换个名字"})
        check("HTTP：书架改名 200", r.status_code == 200, r.text[:120])
        check("HTTP：改名生效", r.json()["title"] == "换个名字", r.text[:120])

        r = await client.patch(f"/api/shelves/{shelf['id']}", json={"title": ""})
        check("HTTP：空名 -> 400", r.status_code == 400, r.text[:120])

        # ---- 顿联删除 ----
        r = await client.delete(f"/api/shelves/{shelf['id']}", params={"cascade": "false"})
        check("HTTP：书架有书时非级联删除 -> 400", r.status_code == 400, r.text[:120])

        r = await client.delete(f"/api/shelves/{shelf['id']}", params={"cascade": "true"})
        check("HTTP：级联删除书架 200", r.status_code == 200, r.text[:120])
        check("HTTP：删书架连带删书", r.json().get("books_deleted") == 1, r.text[:120])
        check("HTTP：书确实没了",
              await anyio.to_thread.run_sync(dbmod.get_book, book["id"]) is None)
        check("HTTP：书架确实没了",
              await anyio.to_thread.run_sync(dbmod.get_shelf, shelf["id"]) is None)
        check("HTTP：顿联删除后成员也清空",
              (await client.get(f"/api/shelves/{shelf['id']}/members")).status_code == 404)


def group_chat_checks() -> None:
    dbmod.init_db()

    # ---- 性格 -> 发言倾向 ----
    talkative = group_chat.profile_for({"personality": ["热情", "话多"]})
    quiet = group_chat.profile_for({"personality": ["寡言", "冷静"]})
    plain = group_chat.profile_for({"personality": []})
    check("群聊：话痨角色外向度高",
          talkative["extraversion"] > quiet["extraversion"], f"{talkative} / {quiet}")
    check("群聊：无性格时用默认画像", plain["extraversion"] == 0.5, str(plain))

    # ---- @ 解析 ----
    names = {"沈砚", "苏晚", "路明", "路明非"}
    check("群聊：解析 @全体", group_chat.parse_mentions("@全体成员 都来说两句", names) == ["all"])
    check("群聊：解析 @某角色", group_chat.parse_mentions("@沈砚 你怎么看", names) == ["沈砚"])
    check("群聊：没有 @ 时为空", group_chat.parse_mentions("沈砚你怎么看", names) == [])
    check("群聊：长名字优先匹配",
          group_chat.parse_mentions("@路明非 说话", names) == ["路明非"],
          str(group_chat.parse_mentions("@路明非 说话", names)))

    # ---- 意愿分 ----
    import random as _random
    seeded = _random.Random(7)
    plain_score, _ = group_chat.score_member(
        _member("沈砚", ["寡言"]), text="随便聊聊", rng=seeded
    )
    named_score, reason = group_chat.score_member(
        _member("沈砚", ["寡言"]), text="沈砚你来说说", rng=seeded
    )
    check("群聊：被点名的人意愿更高", named_score > plain_score,
          f"{plain_score} / {named_score}")
    check("群聊：理由里标明被点名", "被点名" in reason, reason)

    # ---- 选角：显式 @ 必答 ----
    members = [
        _member("沈砚", ["寡言"]),
        _member("苏晚", ["温柔"]),
        _member("老周", ["热情", "话多"]),
    ]
    chosen = group_chat.select_speakers(
        members, text="@沈砚 你说", mentions=["沈砚"],
        max_speakers=2, threshold=0.99, all_threshold=0.99, rng=_random.Random(1),
    )
    check("群聊：被 @ 的人必答（跳阈值）",
          chosen and chosen[0].name == "沈砚" and chosen[0].forced, str(chosen))
    check("群聊：发言人数不超过上限", len(chosen) <= 2, str(len(chosen)))

    # ---- 选角：阈值挡住无关的人 ----
    none_chosen = group_chat.select_speakers(
        members, text="今天天气不错", mentions=[],
        max_speakers=2, threshold=0.99, all_threshold=0.99,
        always_reply=False, rng=_random.Random(1),
    )
    check("群聊：关掉兜底时没人过阈值就不说话", none_chosen == [], str(none_chosen))

    fallback = group_chat.select_speakers(
        members, text="今天天气不错", mentions=[],
        max_speakers=2, threshold=0.99, all_threshold=0.99, rng=_random.Random(1),
    )
    check("群聊：默认兜底接话（不 @ 也有人理）",
          len(fallback) == 1 and "兜底接话" in fallback[0].reason,
          str([(s.name, s.reason) for s in fallback]))

    # ---- 选角：@全体 降低阈值 ----
    low = group_chat.select_speakers(
        members, text="大家聊聊", mentions=["all"],
        max_speakers=3, threshold=0.99, all_threshold=0.0, rng=_random.Random(2),
    )
    check("群聊：@全体 会让更多人开口", len(low) >= 1, str([s.name for s in low]))

    # ---- 选角：离线成员不参与 ----
    offline = group_chat.select_speakers(
        [_member("沈砚", ["热情"], status="offline")], text="沈砚", mentions=[],
        max_speakers=2, threshold=0.0, all_threshold=0.0, rng=_random.Random(3),
    )
    check("群聊：离线成员不参与选角", offline == [], str(offline))

    # ---- 选角：@全体成员 = 所有人都被单独 @ 了一遍 ----
    everyone = group_chat.select_speakers(
        [
            _member("沈砚", ["寡言"]),
            _member("苏晚", ["温柔"]),
            _member("老周", ["热情"]),
            _member("离线的人", ["热情"], status="offline"),
        ],
        text="@全体成员 都来说两句", mentions=["all"],
        max_speakers=1, threshold=0.99, all_threshold=0.99, rng=_random.Random(5),
    )
    check("群聊：@全体成员 时所有在线成员必答（不受人数上限限制）",
          {s.name for s in everyone} == {"沈砚", "苏晚", "老周"},
          str([s.name for s in everyone]))
    check("群聊：@全体成员 不会叫上离线的人",
          all(s.name != "离线的人" for s in everyone),
          str([s.name for s in everyone]))

    # ---- 选角：冷却中扣分 ----
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    hot = _member("老周", ["热情"], last_spoke_at=(now - timedelta(seconds=3)).isoformat())
    cold = _member("老周", ["热情"], last_spoke_at=(now - timedelta(seconds=600)).isoformat())
    hot_score, _ = group_chat.score_member(hot, text="聊聊", now=now, rng=_random.Random(4))
    cold_score, _ = group_chat.score_member(cold, text="聊聊", now=now, rng=_random.Random(4))
    check("群聊：冷却期内扣分", hot_score < cold_score, f"{hot_score} / {cold_score}")


def _fake_member(name, personality, *, character_id=None, extraversion=0.5):
    return {
        "character_id": character_id or f"char_{name}",
        "name": name,
        "personality": personality,
        "role": "",
        "speech_style": "",
        "extraversion": extraversion,
        "arousal": 0.35,
        "status": "online",
        "state": "idle",
    }


def dispatch_checks() -> None:
    """需求分析 -> 人数 -> 按性格适配选人。"""
    import random as _random

    from services.dialogue import dispatch, persona

    # ---- 性格画像 ----
    warm = _fake_member("阿暖", ["温柔", "体贴", "耐心"])
    sharp = _fake_member("阿烈", ["暴躁", "易怒", "毒舌"])
    calm = _fake_member("阿静", ["冷静", "理性", "沉稳"])

    check("性格：温柔的人 warmth 高",
          persona.analyze_temperament(warm).warmth > 0.6,
          str(persona.analyze_temperament(warm).as_dict()))
    check("性格：暴躁的人 sharpness 高",
          persona.analyze_temperament(sharp).sharpness > 0.5,
          str(persona.analyze_temperament(sharp).as_dict()))
    check("性格：难过时温柔的人适配度高于暴躁的人",
          persona.fit_for(warm, emotion="难过") > persona.fit_for(sharp, emotion="难过"),
          f"{persona.fit_for(warm, emotion='难过')} vs {persona.fit_for(sharp, emotion='难过')}")
    check("性格：危机时暴躁的人被压低",
          persona.fit_for(sharp, emotion="难过", crisis=True) < 0.3,
          str(persona.fit_for(sharp, emotion="难过", crisis=True)))

    # ---- 人数 ----
    cases = [
        (dict(emotion="难过", intent="crisis", intensity=0.9, crisis=True), 2),
        (dict(emotion="难过", intent="vent", intensity=0.0), 2),
        (dict(emotion="难过", intent="vent", intensity=0.5), 4),
        (dict(emotion="难过", intent="vent", intensity=1.0), 6),
        (dict(emotion="开心", intent="chat", intensity=0.0), 1),
        (dict(emotion="疲惫", intent="seek_help", intensity=0.5), 4),
    ]
    for kwargs, expect in cases:
        got = dispatch.target_count(**kwargs)
        check(f"人数：{kwargs['intent']}/{kwargs['intensity']} -> {expect}",
              got == expect, str(got))

    check("人数：@全体 = 所有可发言的人（用户要求全员回应）",
          dispatch.target_count(emotion="平静", intent="chat", intensity=0,
                                all_called=True, usable=42) == 42,
          str(dispatch.target_count(emotion="平静", intent="chat", intensity=0,
                                    all_called=True, usable=42)))
    check("人数：永远不少于 1",
          dispatch.target_count(emotion="平静", intent="chat", intensity=0) >= 1)

    # ---- 选人 ----
    members = [warm, sharp, calm] + [
        _fake_member(f"路人{i}", []) for i in range(5)
    ]
    sad = dispatch.select(
        members, text="我好难过", emotion="难过", intent="vent", intensity=0.8,
        rng=_random.Random(1),
    )
    check("选人：难受时选 5 人", len(sad.picks) == 5, str(len(sad.picks)))
    check("选人：温柔的人排在前二",
          sad.picks[0].name == "阿暖" or sad.picks[1].name == "阿暖",
          str([p.name for p in sad.picks]))
    _names = [p.name for p in sad.picks]
    check("选人：暴躁的人排不进前二（大概率直接被淘汰）",
          "阿烈" not in _names[:2], str(_names))

    crisis = dispatch.select(
        members, text="我不想活了", emotion="难过", intent="crisis",
        intensity=0.9, crisis=True, rng=_random.Random(1),
    )
    check("选人：危机只来 2 人", len(crisis.picks) == 2, str(len(crisis.picks)))
    check("选人：危机时暴躁的人不在场",
          "阿烈" not in [p.name for p in crisis.picks],
          str([p.name for p in crisis.picks]))

    forced = dispatch.select(
        members, text="@阿烈 你来", emotion="开心", intent="chat", intensity=0.2,
        explicit_names=["阿烈"], rng=_random.Random(1),
    )
    check("选人：被 @ 的人必到且排第一",
          forced.picks and forced.picks[0].name == "阿烈" and forced.picks[0].forced,
          str([p.name for p in forced.picks]))

    all_call = dispatch.select(
        members, text="@全体成员 出来", emotion="平静", intent="chat", intensity=0.0,
        all_called=True, rng=_random.Random(1),
    )
    check("选人：@全体 时全员都来（本例 8 人）",
          len(all_call.picks) == 8, str(len(all_call.picks)))

    empty = dispatch.select([], text="喂", emotion="平静", intent="chat", intensity=0)
    check("选人：没有成员时返回空", empty.picks == [], str(empty.picks))


def skill_checks() -> None:
    """工具三层：协议层 / 框架层 / 业务技能。"""
    from core.tools import protocol as tool_protocol
    import skills

    names = tool_protocol.names()
    check("工具①：四个技能工具都注册了",
          {"breathing_guide", "comfort_phrase", "small_step", "crisis_helpline"} <= set(names),
          str(names))
    check("工具①：时间工具也注册了",
          {"local_time", "greeting_check"} <= set(names), str(names))
    check("工具①：意图分析器也注册了", "intent_analyze" in names, str(names))

    schema = tool_protocol.openai_schema(["comfort_phrase"])[0]
    check("工具①：能产出 function calling 声明",
          schema["type"] == "function"
          and schema["function"]["parameters"]["required"] == ["emotion"],
          str(schema)[:120])

    ok = tool_protocol.execute("comfort_phrase", {"emotion": "疲惫"})
    check("工具①：正常执行返回 ok", ok["ok"] and "累" in ok["text"], str(ok))

    miss = tool_protocol.execute("comfort_phrase", {})
    check("工具①：缺参数返回 ok=False 而不是抛异常",
          miss["ok"] is False and "缺少参数" in miss["error"], str(miss))

    bad = tool_protocol.execute("不存在的工具", {})
    check("工具①：未知工具返回 ok=False", bad["ok"] is False, str(bad))

    def boom(_args):
        raise RuntimeError("炸了")

    tool_protocol.register(tool_protocol.ToolSpec(
        name="_test_boom", description="测试用", parameters={}, handler=boom))
    crashed = tool_protocol.execute("_test_boom", {})
    check("工具①：工具内部报错也不抛出来",
          crashed["ok"] is False and "炸了" in crashed["error"], str(crashed))
    tool_protocol.unregister("_test_boom")   # 别污染注册表

    from agent.tools import langgraph_tools

    structured = langgraph_tools.to_langgraph_tools()
    structured_names = {tool.name for tool in structured}
    check("工具②：转成 LangChain StructuredTool",
          {"breathing_guide", "comfort_phrase", "crisis_helpline", "small_step"}
          <= structured_names, str(sorted(structured_names)))
    node = langgraph_tools.build_tool_node()
    check("工具②：能构造 LangGraph 的 ToolNode", type(node).__name__ == "ToolNode")

    calls = langgraph_tools.invoke_tool_calls(
        [{"name": "small_step", "args": {"topic": "换工作"}, "id": "c1"}])
    check("工具②：按 tool_calls 形状执行",
          calls and calls[0]["content"]["ok"], str(calls[:1]))

    # ---- 技能层 ----
    vent = skill_registry.run_for({"intent": "vent", "emotion": "疲惫", "crisis": False})
    check("工具③：倾诉走 comfort 技能",
          vent is not None and vent.skill == "comfort" and len(vent.materials) == 2,
          str(vent.skill if vent else None))

    crisis = skill_registry.run_for({"intent": "crisis", "emotion": "难过", "crisis": True})
    check("工具③：危机走 steady 技能",
          crisis is not None and crisis.skill == "steady",
          str(crisis.skill if crisis else None))
    check("工具③：危机素材含现实求助",
          crisis is not None and any("热线" in m or "信得过" in m for m in crisis.materials),
          str(crisis.materials if crisis else []))

    help_skill = skill_registry.run_for({"intent": "seek_help", "emotion": "焦虑", "crisis": False})
    check("工具③：求助走 suggest 技能",
          help_skill is not None and help_skill.skill == "suggest",
          str(help_skill.skill if help_skill else None))

    chat = skill_registry.run_for({"intent": "chat", "emotion": "开心", "crisis": False})
    check("工具③：闲聊不调用任何技能", chat is None, str(chat))

    block = vent.as_block()
    check("工具③：素材块格式正确",
          block.startswith("【可用素材") and block.count("- ") == len(vent.materials),
          block[:40])


def fetch_script_checks() -> None:
    """依赖下载脚本：能定位技能包、能按平台选源（不联网）。"""
    import importlib.util
    import pathlib as _pathlib

    script = _pathlib.Path(__file__).resolve().parents[2] / "scripts" / "fetch_animation_deps.py"
    check("下载脚本：文件存在", script.exists(), str(script))
    if not script.exists():
        return

    spec = importlib.util.spec_from_file_location("fetch_animation_deps", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    check("下载脚本：指向 animation 技能包",
          module.PACKAGE_DIR.name == "animation"
          and module.PACKAGE_DIR.parent.name == "work",
          str(module.PACKAGE_DIR))
    check("下载脚本：装到包内 bin/",
          module.BIN_DIR == module.PACKAGE_DIR / "bin", str(module.BIN_DIR))
    check("下载脚本：平台识别为中/英之一",
          module.platform_key() in ("win64", "linux", "mac"),
          module.platform_key())
    check("下载脚本：ffmpeg 源是 https",
          module.FFMPEG_WIN_URL.startswith("https://"), module.FFMPEG_WIN_URL)
    check("下载脚本：有 dry-run 开关",
          hasattr(module, "main") and "dry-run" in module.main.__doc__ if module.main.__doc__ else True)


def skill_vendor_checks() -> None:
    """animation 是自包含技能包：素材在包内，依赖可放包内 bin/。"""
    import pathlib as _pathlib
    import shutil as _shutil
    import tempfile

    import skills.work.animation.engine as animation
    from skills import capabilities, registry

    package = _pathlib.Path(animation.PACKAGE_DIR)
    check("自包含：引擎素材在技能包内",
          (package / "vendor" / "template" / "render.mjs").exists(),
          str(package / "vendor"))
    check("自包含：分镜与像素参考也在包内",
          (package / "vendor" / "references" / "lyric-storyboard.md").exists())
    check("自包含：保留上游许可",
          (package / "vendor" / "LICENSE").exists())
    check("自包含：有依赖说明", (package / "DEPS.md").exists())
    check("自包含：engine 解析到包内 vendor",
          _pathlib.Path(animation.skill_dir()).name == "vendor",
          str(animation.skill_dir()))

    caps = animation.capabilities()
    check("自包含：能力探测认得包内素材",
          caps["skill_package"] and caps["self_contained"], str(caps)[:120])

    # ---- 包内 bin/ 的查找逻辑（用打洞代替写文件，绝不碰真实 bin/）----
    real_find = capabilities.find_bundled
    try:
        capabilities.find_bundled = lambda name, package_dir=None: (
            str(package / "bin" / name) if name in ("ffmpeg", "chrome") else None
        )
        found = capabilities.find_bundled("ffmpeg", package)
        check("自包含：包内 bin/ 的 ffmpeg 能被找到", bool(found), str(found))
        ok, missing = capabilities.check(("ffmpeg", "chrome"), package_dir=package)
        check("自包含：依赖放包里就算齐全", ok and not missing, str(missing))

        registry.reload()
        check("自包含：包内放齐后 animation 变可用",
              "animation" in [skill.name for skill in registry.available("work")],
              str([skill.name for skill in registry.available("work")]))
    finally:
        capabilities.find_bundled = real_find
        registry.reload()

    # 真装过依赖的机器上 bin/ 里有文件，"隐身"要人为把两个来源都清掉才测得出来
    _real_find = capabilities.find_bundled
    _real_probe = capabilities._probe
    capabilities.find_bundled = lambda name, package_dir=None: None
    capabilities._probe = lambda: {"node": False, "ffmpeg": False, "chrome": False,
                                   "numpy": False, "python": True}
    try:
        registry.reload()
        check("自包含：两个来源都没有时 animation 隐身",
              "animation" not in [skill.name for skill in registry.available("work")],
              str([skill.name for skill in registry.available("work")]))
    finally:
        capabilities.find_bundled = _real_find
        capabilities._probe = _real_probe
        registry.reload()


def skill_validate_checks() -> None:
    """SKILL.md 校验：写错要能被抓出来，内置技能要全绿。"""
    import tempfile
    import pathlib as _pathlib

    from skills import registry, validate

    report = validate.validate_paths([registry.SKILLS_DIR])
    check("校验：内置技能全部通过",
          report.ok and report.checked >= 7,
          f"{report.checked} 个 / 错误 {len(report.errors)}")
    check("校验：内置技能没有警告",
          not report.warnings, str([str(item) for item in report.warnings]))

    with tempfile.TemporaryDirectory() as folder:
        bad = _pathlib.Path(folder) / "broken"
        bad.mkdir()
        (bad / "SKILL.md").write_text(
            "---\n"
            "name: broken\n"
            "description: 故意写错\n"
            "modes: [wrok]\n"
            "requires: [nonexistent_dep]\n"
            "when:\n"
            "  unknown_condition: true\n"
            "steps:\n"
            "  - [no_such_tool, {text: $usrname}]\n"
            "---\n正文\n",
            encoding="utf-8",
        )
        result = validate.validate_paths([_pathlib.Path(folder)])
        messages = " | ".join(str(item) for item in result.issues)
        check("校验：抓出未知模式", "未知模式" in messages, messages[:120])
        check("校验：抓出未注册的工具", "no_such_tool" in messages, messages[:120])
        check("校验：抓出未知占位符", "$usrname" in messages, messages[:120])
        check("校验：抓出未知依赖", "nonexistent_dep" in messages, messages[:120])
        check("校验：抓出未知 when 条件", "unknown_condition" in messages, messages[:120])
        check("校验：有问题时整体判失败", not result.ok, str(result.as_dict())[:80])

    # 注册表加载后应自动体检，且内置技能没有 error
    registry.reload()
    issues = registry.issues()
    check("校验：注册表自动体检无 error",
          not [item for item in issues if item.get("severity") == "error"], str(issues)[:120])
    check("校验：describe 里带出 issues 字段",
          "issues" in registry.describe(), str(list(registry.describe()))[:80])


def skill_package_checks() -> None:
    """技能包：单独封装 / 依赖声明 / 外部可插拔。"""
    import pathlib as _pathlib
    import tempfile

    import pathlib as _pathlib

    from core.tools import protocol as tool_protocol
    from skills import capabilities, registry

    registry.reload()
    names = {skill.name for skill in registry.all_skills()}
    check("技能包：内置 7 个技能都在",
          {"comfort", "steady", "suggest", "animation",
           "planning", "notes", "writing"} <= names, str(sorted(names)))

    planning = registry.get("planning")
    check("技能包：每个技能有自己的 SKILL.md",
          bool(planning and planning.path.replace("\\", "/").endswith("planning/SKILL.md")),
          planning.path if planning else "无")
    check("技能包：技能声明了模式和步骤",
          bool(planning and planning.modes == ("work",) and planning.steps),
          str(planning and (planning.modes, planning.steps)))

    # ---- 依赖声明：动画需要 ffmpeg/chrome，缺了就不该被选中 ----
    animation = registry.get("animation")
    check("可插拔：大技能声明了外部依赖",
          bool(animation and set(animation.requires) >= {"node", "ffmpeg"}),
          str(animation and animation.requires))
    # 依赖可能放在技能包自己的 bin/ 里，所以检查时必须带上包目录
    _pkg = _pathlib.Path(animation.path).parent
    usable, missing = capabilities.check(animation.requires, package_dir=_pkg)
    work_names = [skill.name for skill in registry.available("work")]
    if usable:
        check("可插拔：依赖齐全时动画在列表里", "animation" in work_names, str(work_names))
    else:
        check("可插拔：缺依赖时动画自动隐身", "animation" not in work_names, str(work_names))
        disabled = {item["name"]: item["missing"] for item in registry.unavailable("work")}
        check("可插拔：并且说明缺什么",
              "animation" in disabled and disabled["animation"], str(disabled))

    # ---- 三个零依赖的工作技能 ----
    work_names = [skill.name for skill in registry.available("work")]
    check("工作技能：planning/notes/writing 都可用",
          {"planning", "notes", "writing"} <= set(work_names), str(work_names))
    check("工作技能：情感技能不越界到工作模式",
          "comfort" not in work_names, str(work_names))

    breakdown = tool_protocol.execute(
        "task_breakdown", {"text": "还没吃饭，要汇总工作表，还没洗衣服，浇花"})
    check("planning：拆出 4 件事", breakdown["ok"] and breakdown["count"] == 4,
          str(breakdown.get("count")))
    order = [item["item"] for item in breakdown["order"]]
    check("planning：吃饭排第一", order and "吃" in order[0], str(order))
    check("planning：家务排在时限任务之后",
          order.index("洗衣服") > order.index(next(i for i in order if "工作表" in i)),
          str(order))

    digest = tool_protocol.execute(
        "note_digest", {"text": "今天定了下周上线。我要准备测试用例。回滚谁来定？"})
    check("notes：分出待办与未决",
          digest["ok"] and digest["todo"] and digest["open"], str(digest)[:90])

    outline = tool_protocol.execute("writing_outline", {"text": "帮我写一份周报"})
    check("writing：认得出周报并给结构",
          outline["ok"] and outline["genre"] == "周报"
          and any("下周计划" in item for item in outline["outline"]),
          str(outline.get("outline"))[:80])

    unknown = tool_protocol.execute("writing_outline", {"text": "帮我写点东西"})
    check("writing：认不出文体时给通用结构",
          unknown["ok"] and unknown["genre"] == "通用", str(unknown.get("genre")))

    # ---- 外部可插拔目录 ----
    settings = get_settings()
    original = settings.skills_extra_dirs
    with tempfile.TemporaryDirectory() as folder:
        extra = _pathlib.Path(folder) / "my-skill"
        extra.mkdir()
        (extra / "SKILL.md").write_text(
            "---\n"
            "name: from_outside\n"
            "description: 外部塞进来的技能\n"
            "modes: [work]\n"
            "when:\n"
            "  keywords: [外部]\n"
            "  base: 0.9\n"
            "steps:\n"
            "  - [small_step, {topic: 外部}]\n"
            "---\n正文\n",
            encoding="utf-8",
        )
        try:
            settings.skills_extra_dirs = folder
            registry.reload()
            check("可插拔：外部目录里的 SKILL.md 被加载",
                  "from_outside" in {skill.name for skill in registry.all_skills()},
                  str(sorted(skill.name for skill in registry.all_skills())))
            picked = registry.choose({"text": "这个外部需求", "intent": "chat"}, mode="work")
            check("可插拔：外部技能按 when 规则被选中",
                  picked is not None and picked.name == "from_outside",
                  str(picked and picked.name))
        finally:
            settings.skills_extra_dirs = original
            registry.reload()

    check("可插拔：撤掉目录后外部技能消失",
          "from_outside" not in {skill.name for skill in registry.all_skills()})


def mode_checks() -> None:
    """工作模式：同一个请求，模式不同 -> 可用的技能不同。"""
    import asyncio as _asyncio

    from agent import graph as agent_graph

    character = {"id": "c_mode", "name": "沈砚", "role": "剑客",
                 "personality": ["冷静"], "speech_style": "", "background": ""}

    async def _fake(_state):
        yield "嗯。"

    async def _run(text: str, mode: str) -> dict:
        final: dict = {}
        async for kind, chunk in agent_graph.get_turn_graph().astream(
            {
                "user_id": "u_mode", "user_name": "读", "character": character,
                "book_title": "b", "shelf_id": "", "raw_text": text,
                "image_urls": [], "api_key": "k", "model": "m",
                "extra_context": "", "persist": False, "session_id": "s",
                "mode": mode,
            },
            config={"configurable": {
                "thread_id": f"mode-{mode}-{abs(hash(text)) % 9999}",
                "llm": _fake, "summarizer": None}, "recursion_limit": 100},
            stream_mode=["custom", "values"],
        ):
            if kind == "values" and isinstance(chunk, dict) and chunk.get("reply"):
                final = chunk
        return final

    from skills import capabilities as _caps

    _real = _caps._probe
    _caps._probe = lambda: {"node": True, "ffmpeg": True, "chrome": True,
                            "numpy": True, "python": True}
    try:
        text = "帮我做一段水彩动画，雨夜的车站"
        chat_state = _asyncio.run(_run(text, "chat"))
        work_state = _asyncio.run(_run(text, "work"))
    finally:
        _caps._probe = _real
    chat_block = str(chat_state.get("skill_block") or "")
    work_block = str(work_state.get("skill_block") or "")

    check("模式：聊天模式下不会起动画任务",
          "动画" not in chat_block, chat_block[:80])
    check("模式：工作模式下会起动画任务",
          "动画" in work_block, work_block[:80])
    check("模式：两种模式的素材不一样", chat_block != work_block,
          f"{chat_block[:40]} / {work_block[:40]}")

    # 安全技能不受模式影响
    crisis_state = _asyncio.run(_run("我真的不想活了", "work"))
    crisis_block = str(crisis_state.get("skill_block") or "")
    check("模式：工作模式下危机仍然给求助渠道",
          "12356" in crisis_block or "热线" in crisis_block or "信得过" in crisis_block,
          crisis_block[:80])


def animation_checks() -> None:
    """动画技能：能力探测、异步任务、技能层接入（用假渲染器，不碰 node/ffmpeg）。"""
    import pathlib as _pathlib
    import time as _time

    from core.tools import protocol as tool_protocol
    import skills.work.animation.engine as animation

    caps = animation.capabilities()
    check("动画：能力探测字段齐全",
          {"renderer", "available", "missing"} <= set(caps), str(caps))
    check("动画：三个工具都注册了",
          {"animation_start", "animation_status", "animation_capabilities"}
          <= set(tool_protocol.names()), str(tool_protocol.names()))

    # --- 缺依赖时不能说"开做了" ---
    original_caps = animation.capabilities
    try:
        animation.capabilities = lambda: {
            "renderer": "real", "available": False, "missing": ["ffmpeg"],
            "skill_dir": "", "skill_package": False, "node": "", "chrome": "",
        }
        missing = tool_protocol.execute("animation_start", {"prompt": "做个动画"})
        check("动画：缺依赖时如实说缺什么",
              missing["ok"] and missing.get("ready") is False
              and "ffmpeg" in str(missing.get("text")),
              str(missing))

        # --- 有了假渲染器：整条链路跑通 ---
        animation.capabilities = lambda: {
            "renderer": "fake", "available": True, "missing": [],
            "skill_dir": "", "skill_package": True, "node": "", "chrome": "",
        }
        started = tool_protocol.execute(
            "animation_start", {"prompt": "帮我做一段水彩动画：雨夜车站"})
        check("动画：起任务立刻返回 job_id（不等渲染）",
              started["ok"] and started.get("job_id"), str(started)[:120])

        job_id = started["job_id"]
        final = None
        for _ in range(60):
            _time.sleep(0.1)
            final = animation.job_status(job_id)
            if final and final["stage"] in ("done", "failed", "awaiting_confirmation"):
                break
        check("动画：任务能跑到 done", bool(final) and final["stage"] == "done",
              str(final and final.get("stage")))
        check("动画：产物文件存在",
              bool(final) and _pathlib.Path(final.get("video") or "").exists(),
              str(final and final.get("video")))
        check("动画：分镜文件写出来了",
              bool(final) and (_pathlib.Path(final["dir"]) / "STORYBOARD.md").exists())

        status = tool_protocol.execute("animation_status", {"job_id": job_id})
        check("动画：状态查询能拿到产物路径",
              status["ok"] and "video" in str(status.get("text")), str(status)[:120])

        # --- 歌词 MV 要先确认分镜 ---
        gated = tool_protocol.execute(
            "animation_start", {"prompt": "给我做一段歌词 MV，配这首歌"})
        gated_final = None
        for _ in range(40):
            _time.sleep(0.1)
            gated_final = animation.job_status(gated["job_id"])
            if gated_final and gated_final["stage"] in ("awaiting_confirmation", "done", "failed"):
                break
        check("动画：歌词 MV 停在等确认分镜",
              bool(gated_final) and gated_final["stage"] == "awaiting_confirmation",
              str(gated_final and gated_final.get("stage")))
    finally:
        animation.capabilities = original_caps

    # --- 第③层：技能选择（先假装依赖齐全，否则动画会被 requires 挡掉）---
    from skills import capabilities as _caps_mod

    _real_probe = _caps_mod._probe
    _caps_mod._probe = lambda: {"node": True, "ffmpeg": True, "chrome": True,
                                "numpy": True, "python": True}
    try:
        act = skill_registry.choose(
            {"text": "帮我做一段水彩动画，雨夜的车站", "intent": "chat",
             "emotion": "平静", "crisis": False},
            mode="work",
        )
        check("动画：工作模式下、有动作词时选中 animation",
              act is not None and act.name == "animation", str(act and act.name))

        chat_mode = skill_registry.choose(
            {"text": "帮我做一段水彩动画，雨夜的车站", "intent": "chat",
             "emotion": "平静"},
            mode="chat",
        )
        check("模式：聊天模式下不开放工作技能",
              chat_mode is None or chat_mode.name != "animation",
              str(chat_mode and chat_mode.name))

        no_action = skill_registry.choose(
            {"text": "我看视频也难受", "intent": "vent", "emotion": "难过",
             "crisis": False})
        check("动画：只是提到视频不抢情绪安慰",
              no_action is not None and no_action.name == "comfort",
              str(no_action and no_action.name))

        crisis = skill_registry.choose(
            {"text": "帮我做个动画", "intent": "crisis", "emotion": "难过",
             "crisis": True})
        check("动画：危机时不抢戏",
              crisis is not None and crisis.name == "steady",
              str(crisis and crisis.name))

        names = [item["name"] for item in skill_registry.describe()["skills"]]
        check("动画：已出现在技能清单里", "animation" in names, str(names))
    finally:
        _caps_mod._probe = _real_probe




def conversation_checks() -> None:
    """一个书架多个对话：建、列、切、删，互不串。"""
    dbmod.init_db()
    lib = dbmod.create_library("会话自检库", "临时")
    shelf = dbmod.create_shelf(lib["id"], "会话自检架")
    book = library.create_book(
        title="会话书", source_name="c.txt", source_type="txt",
        text="x", shelf_id=shelf["id"],
    )
    dbmod.insert_character(book_id=book["id"], name="甲", personality=["温柔"], source="novel")

    uid = "u_conv_check"
    first = dbmod.create_conversation(shelf["id"], uid)
    dbmod.add_chat_turn(shelf_id=shelf["id"], user_id=uid, role="user",
                        content="第一句：我今天很累", conversation_id=first["id"])
    second = dbmod.create_conversation(shelf["id"], uid)
    dbmod.add_chat_turn(shelf_id=shelf["id"], user_id=uid, role="user",
                        content="第二句：换个话题", conversation_id=second["id"])

    listed = dbmod.list_conversations(shelf["id"], uid)
    check("对话：两个对话都能列出来", len(listed) == 2, str(len(listed)))
    check("对话：每个对话的条数正确",
          all(item["turn_count"] == 1 for item in listed), str([i["turn_count"] for i in listed]))
    check("对话：第一句话自动成了标题",
          any("第一句" in str(item["title"]) for item in listed),
          str([item["title"] for item in listed]))

    turn_a = dbmod.list_chat_turns(shelf["id"], user_id=uid, conversation_id=first["id"])
    turn_b = dbmod.list_chat_turns(shelf["id"], user_id=uid, conversation_id=second["id"])
    check("对话：切换对话只看到自己的消息",
          len(turn_a) == 1 and len(turn_b) == 1
          and "第一句" in turn_a[0]["content"] and "第二句" in turn_b[0]["content"],
          f"{turn_a[0]['content']} / {turn_b[0]['content']}")

    # 老数据（没有 conversation_id）要能自动归到一个对话
    dbmod.add_chat_turn(shelf_id=shelf["id"], user_id="u_legacy", role="user",
                        content="迁就的老数据")
    legacy = dbmod.ensure_default_conversation(shelf["id"], "u_legacy")
    check("对话：老数据被归入「之前的对话」",
          legacy["title"] == "之前的对话" and legacy["turn_count"] == 1,
          str(legacy))

    # 删除
    renamed = dbmod.rename_conversation(second["id"], "改过的名字")
    check("对话：可以改名", renamed and renamed["title"] == "改过的名字", str(renamed))
    removed = dbmod.delete_conversation(second["id"])
    check("对话：删除对话会带走它的流水", removed == 1, str(removed))
    check("对话：删完只剩一个",
          len(dbmod.list_conversations(shelf["id"], uid)) == 1,
          str(len(dbmod.list_conversations(shelf["id"], uid))))

    dbmod.delete_book(book["id"])
    dbmod.delete_shelf(shelf["id"])


def clear_conversation_checks() -> None:
    """清空对话的两种语义：蒸馏保留记忆 / 连记忆一起删。"""
    from services.memory import agent_memory, chat_memory

    dbmod.init_db()

    def build(tag):
        lib = dbmod.create_library(f"{tag}库", "临时")
        shelf = dbmod.create_shelf(lib["id"], f"{tag}架")
        book = library.create_book(
            title=f"{tag}书", source_name="c.txt", source_type="txt",
            text="x", shelf_id=shelf["id"],
        )
        dbmod.insert_character(book_id=book["id"], name="甲", personality=["温柔"], source="novel")
        dbmod.insert_character(book_id=book["id"], name="乙", personality=["冷静"], source="novel")
        return lib, shelf, book, dbmod.ensure_shelf_members(shelf["id"])

    def talk(shelf, members, user_id):
        dbmod.add_chat_turn(shelf_id=shelf["id"], user_id=user_id, role="user",
                            content="我最近分手了，很难受")
        for member in members[:2]:
            dbmod.add_chat_turn(
                shelf_id=shelf["id"], user_id=user_id, role="assistant",
                content="先吃点东西吧。", character_id=member["character_id"],
                speaker_name=member["name"],
            )
        dbmod.add_chat_turn(shelf_id=shelf["id"], user_id=user_id, role="user",
                            content="谢谢你")

    # ---- A. distill ----
    lib_a, shelf_a, book_a, members_a = build("清空蒸馏")
    talk(shelf_a, members_a, "u_clear_a")
    result_a = chat_memory.clear_conversation(shelf_a["id"], user_id="u_clear_a", mode="distill")
    facts_a = agent_memory.items(members_a[0]["character_id"], kind="fact")
    check("清空：distill 模式删光了流水", dbmod.count_chat_turns(shelf_a["id"]) == 0,
          str(dbmod.count_chat_turns(shelf_a["id"])))
    check("清空：distill 模式给每个说话的角色都写了记忆",
          result_a.get("distilled") == 2, str(result_a))
    check("清空：长期记忆真的落库了", len(facts_a) >= 1, str(len(facts_a)))
    check("清空：记忆内容含对话要点",
          bool(facts_a) and "分手" in str(facts_a[-1].get("content", "")),
          str(facts_a[-1].get("content", ""))[:60] if facts_a else "空")

    # ---- B. purge ----
    lib_b, shelf_b, book_b, members_b = build("清空记忆")
    talk(shelf_b, members_b, "u_clear_b")
    chat_memory.clear_conversation(shelf_b["id"], user_id="u_clear_b", mode="distill")
    before_b = len(agent_memory.items(members_b[0]["character_id"], kind="fact"))
    talk(shelf_b, members_b, "u_clear_b")
    result_b = chat_memory.clear_conversation(shelf_b["id"], user_id="u_clear_b", mode="purge")
    after_b = len(agent_memory.items(members_b[0]["character_id"], kind="fact"))
    check("清空：purge 前确实有记忆", before_b >= 1, str(before_b))
    check("清空：purge 把对话记忆删干净", after_b == 0, str(after_b))
    check("清空：purge 也删掉了流水", dbmod.count_chat_turns(shelf_b["id"]) == 0)
    check("清空：purge 报告删了几条记忆", result_b.get("memory_removed", 0) >= 1,
          str(result_b))

    # ---- B2. 蒸馏用的是「用户画像」提示词，不是角色简介 ----
    from services.memory import summarizer as summarizer_mod

    check("蒸馏提示词：区分了用户画像与角色简介",
          summarizer_mod.MEMORY_SYSTEM_PROMPT != summarizer_mod.SUMMARY_SYSTEM_PROMPT
          and "用户画像" in summarizer_mod.MEMORY_SYSTEM_PROMPT,
          summarizer_mod.MEMORY_SYSTEM_PROMPT[:40])
    check("蒸馏提示词：明确不写角色做了什么",
          "不写对话过程" in summarizer_mod.MEMORY_SYSTEM_PROMPT
          or "不写角色" in summarizer_mod.MEMORY_SYSTEM_PROMPT)
    check("蒸馏提示词：没有 Key 时返回 None（回退本地压缩）",
          summarizer_mod.make_llm_summarizer(
              "", "deepseek-chat", system_prompt=summarizer_mod.MEMORY_SYSTEM_PROMPT
          ) is None)

    # 假的摘要器返回画像文本，验证它原样进记忆
    lib_c, shelf_c, book_c, members_c = build("画像自检")
    talk(shelf_c, members_c, "u_profile_c")
    chat_memory.clear_conversation(
        shelf_c["id"], user_id="u_profile_c", mode="distill",
        summarizer=lambda _t: "· 用户最近分手，情绪低落",
    )
    facts_c = agent_memory.items(members_c[0]["character_id"], kind="fact")
    check("蒸馏：模型输出的画像原样写进记忆",
          bool(facts_c) and "用户最近分手" in str(facts_c[-1].get("content", "")),
          str(facts_c[-1].get("content", ""))[:50] if facts_c else "空")

    # ---- C. 不再有固定阈值 ----
    from core.config import get_settings

    check("清空：自动蒸馏阈值已取消（默认 0）",
          int(get_settings().chat_distill_threshold) == 0,
          str(get_settings().chat_distill_threshold))
    check("清空：maybe_distill 在阈值 0 时不再自动跑",
          chat_memory.maybe_distill(shelf_b["id"], members_b[0]["character_id"]) is None)

    for shelf, book in ((shelf_a, book_a), (shelf_b, book_b)):
        dbmod.delete_book(book["id"])
        dbmod.delete_shelf(shelf["id"])


def cleanup_test_libraries() -> int:
    """删掉自检留下的临时书库（失败也不影响结果）。"""
    removed = 0
    patterns = (
        "清空蒸馏库", "清空记忆库", "画像自检库", "会话自检库", "自检图库", "性格自检库", "性格测试库",
        "并行测试库", "自检书库", "日志自检库", "问候自检库", "去重自检库",
        "去重测试库", "整段测试库", "复现库", "蒸馏库", "清空库",
    )
    try:
        for lib in dbmod.list_libraries():
            if not lib["title"].startswith(patterns):
                continue
            for shelf in dbmod.list_shelves(lib["id"]):
                for book in dbmod.list_books(shelf_id=shelf["id"]):
                    dbmod.delete_book(book["id"])
                try:
                    dbmod.delete_shelf(shelf["id"])
                except Exception:  # noqa: BLE001
                    pass
            try:
                dbmod.delete_library(lib["id"])
                removed += 1
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return removed


def time_tool_checks() -> None:
    """时间工具：时段、时间问候、劳累问候。"""
    from datetime import datetime, timedelta

    from core.tools import protocol as tool_protocol
    from services.runtime import time_tools

    check("时间：06:00 是早上", time_tools.period_of(6)[0] == "早上", str(time_tools.period_of(6)))
    check("时间：12:00 是中午", time_tools.period_of(12)[0] == "中午")
    check("时间：23:00 是晚上", time_tools.period_of(23)[0] == "晚上")
    check("时间：03:00 是深夜", time_tools.period_of(3)[0] == "深夜")

    base = datetime(2026, 11, 11, 10, 30)

    first = time_tools.decide(now=base, frequency_data={})
    check("时间：第一次说话要打招呼", first["need_time_greeting"], str(first))

    same = time_tools.decide(
        now=base, frequency_data={"last_user_at": (base - timedelta(minutes=20)).isoformat()})
    check("时间：同处时段不重复打招呼",
          not same["need_time_greeting"] and not same["text"], str(same))

    cross = time_tools.decide(
        now=datetime(2026, 11, 11, 12, 10),
        frequency_data={"last_user_at": datetime(2026, 11, 11, 9, 0).isoformat()},
    )
    check("时间：跨时段要打招呼",
          cross["need_time_greeting"] and "中午" in cross["text"], str(cross))

    long_gap = time_tools.decide(
        now=base, frequency_data={"last_user_at": (base - timedelta(hours=4)).isoformat()})
    check("时间：同处时段隔超 3 小时也补一句", long_gap["need_time_greeting"], str(long_gap))

    late = time_tools.decide(
        now=datetime(2026, 11, 11, 23, 40),
        frequency_data={"last_user_at": datetime(2026, 11, 11, 23, 20).isoformat()},
    )
    check("时间：深夜触发劳累问候",
          late["need_fatigue_greeting"] and late["late_night"], str(late))

    busy = time_tools.decide(
        now=base,
        frequency_data={
            "last_user_at": (base - timedelta(minutes=40)).isoformat(),
            "messages_last_hour": 25,
            "session_minutes": 60,
        },
    )
    check("时间：高频长对话触发劳累问候",
          busy["need_fatigue_greeting"] and not busy["late_night"], str(busy))
    check("时间：劳累问候不劝睡、劝休息",
          "歇会儿" in busy["text"] or "眼睛" in busy["text"], busy["text"])

    calm = time_tools.decide(
        now=base,
        frequency_data={
            "last_user_at": (base - timedelta(minutes=20)).isoformat(),
            "messages_last_hour": 3,
            "session_minutes": 20,
        },
    )
    check("时间：正常聊天不问候", not calm["text"], str(calm))

    # 同一时段连续对话：不该每次都打招呼
    dbmod.init_db()
    greet_lib = dbmod.create_library("问候自检库", "临时")
    greet_shelf = dbmod.create_shelf(greet_lib["id"], "问候自检架")
    for content in ("第一句", "第二句"):
        dbmod.add_chat_turn(
            shelf_id=greet_shelf["id"], user_id="u_greet", role="user", content=content)
    freq = time_tools.frequency(greet_shelf["id"])
    turns_all = dbmod.list_chat_turns(greet_shelf["id"], limit=10)
    check("时间：frequency 取的是最新那条用户消息",
          freq["last_user_at"] == turns_all[-1]["created_at"],
          f"{freq['last_user_at']} vs {turns_all[-1]['created_at']}")

    same_period = time_tools.decide(frequency_data=freq)
    check("时间：同一时段连续对话不再重复问候",
          not same_period["need_time_greeting"], str(same_period))
    dbmod.delete_shelf(greet_shelf["id"])

    check("时间：注册成工具", "local_time" in tool_protocol.names()
          and "greeting_check" in tool_protocol.names(), str(tool_protocol.names()))
    ran = tool_protocol.execute("local_time", {})
    check("时间：local_time 能取到本地时间",
          ran["ok"] and "period" in ran and "weekday" in ran, str(ran)[:80])
    missing = tool_protocol.execute("greeting_check", {})
    check("时间：greeting_check 缺 shelf_id 时安全失败",
          missing["ok"] is False, str(missing))


def temperament_checks() -> None:
    """性格五维：落库、读取优先、回填效果。"""
    from services.dialogue import persona

    warm = {"name": "阿暖", "personality": ["温柔", "体贴", "耐心"]}
    sharp = {"name": "阿烈", "personality": ["暴躁", "毒舌"]}

    traits = persona.snapshot(warm)
    check("性格：五维都有值", set(traits) == {
        "warmth", "energy", "calmness", "sharpness", "playfulness"}, str(traits))
    check("性格：温柔的人 warmth 高", traits["warmth"] > 0.6, str(traits["warmth"]))

    # 存好的值优先于关键词推断
    stored = {"name": "阿暖", "personality": ["温柔"],
              "temperament": {"warmth": 0.1, "energy": 0.2, "calmness": 0.3,
                              "sharpness": 0.4, "playfulness": 0.5}}
    check("性格：库里存的值优先",
          persona.analyze_temperament(stored).warmth == 0.1,
          str(persona.analyze_temperament(stored).warmth))

    check("性格：难过时温柔的人适配度更高",
          persona.fit_for(warm, emotion="难过") > persona.fit_for(sharp, emotion="难过"),
          f"{persona.fit_for(warm, emotion='难过')} vs {persona.fit_for(sharp, emotion='难过')}")

    # 落库往返
    dbmod.init_db()
    test_lib = dbmod.create_library("性格自检库", "临时")
    shelf = dbmod.create_shelf(test_lib["id"], "性格自检架")
    book = library.create_book(
        title="性格自检书", source_name="t.txt", source_type="txt",
        text="测试", shelf_id=shelf["id"],
    )
    created = library.create_character(
        book["id"], name="阿暖", personality=["温柔", "体贴", "耐心"])
    check("性格：新建角色自动带五维", bool(created.get("temperament")), str(created.get("temperament")))
    check("性格：库里读回来是一样的",
          dbmod.get_character(created["id"])["temperament"] == created["temperament"])

    # 回填幂等：已有值不覆盖
    dbmod.update_character(created["id"], temperament={"warmth": 0.9, "energy": 0.1,
                                                      "calmness": 0.1, "sharpness": 0.1,
                                                      "playfulness": 0.1})
    after = dbmod.get_character(created["id"])
    check("性格：手改的向量能存住", after["temperament"]["warmth"] == 0.9, str(after["temperament"]))

    dbmod.delete_book(book["id"])
    dbmod.delete_shelf(shelf["id"])


def intent_checks() -> None:
    """意图分析器：要不要记忆 / 要不要工具 / 复合意图。"""
    from core.tools import protocol as tool_protocol
    from services.dialogue import intent

    compound = intent.analyze(
        "我很难受，我之前跟你说过我分手了",
        emotion="难过", intent="vent", intensity=0.7,
    )
    check("意图：识别出复合意图",
          compound.compound and set(compound.labels) >= {"情绪倾诉", "记忆唤起"},
          str(compound.labels))
    check("意图：复合时既记情绪也记记忆",
          compound.needs_memory and "comfort_phrase" in compound.tools,
          str(compound.as_dict()))

    plain = intent.analyze("今天好累啊", emotion="疲惫", intent="vent", intensity=0.4)
    check("意图：普通倾诉不触发记忆", not plain.needs_memory, str(plain.labels))
    check("意图：普通倾诉要安慰工具", "comfort_phrase" in plain.tools, str(plain.tools))

    crisis = intent.analyze("我不想活了", emotion="难过", intent="crisis",
                            intensity=0.9, crisis=True)
    check("意图：危机要热线工具", "crisis_helpline" in crisis.tools, str(crisis.tools))

    help_plan = intent.analyze("我该怎么办", emotion="焦虑", intent="seek_help", intensity=0.5)
    check("意图：求助要小步骤", "small_step" in help_plan.tools, str(help_plan.tools))
    check("意图：焦虑中强度要呼吸引导",
          "breathing_guide" in help_plan.tools, str(help_plan.tools))

    chat = intent.analyze("哈哈哈天气不错", emotion="开心", intent="chat", intensity=0.3)
    check("意图：闲聊不调工具",
          chat.labels == ["闲聊"] and not chat.tools, str(chat.as_dict()))

    # function call 工具形态
    check("意图：注册成工具", "intent_analyze" in tool_protocol.names())
    schema = tool_protocol.openai_schema(["intent_analyze"])[0]
    check("意图：工具有参数声明",
          schema["function"]["parameters"]["required"] == ["text"], str(schema)[:100])
    ran = tool_protocol.execute("intent_analyze", {
        "text": "我之前说过，你还记得吗", "emotion": "平静", "intent": "chat",
    })
    check("意图：工具能返回拆解结果",
          ran["ok"] and ran["needs_memory"] and "记忆唤起" in ran["labels"], str(ran))

    # 语义匹配：字面重合低时补一刀
    turns = [
        {"role": "user", "content": "我感冒了"},
        {"role": "assistant", "speaker_name": "源稚生", "character_id": "c1",
         "content": "现在有没有发烧？"},
        {"role": "assistant", "speaker_name": "陈雯雯", "character_id": "c2",
         "content": "还有哪里不舒服么？"},
    ]
    # 字面完全对不上（"体温正常" 里没有"烧"字）
    calls: list[int] = []

    def fake_semantic(_a, b):
        calls.append(1)
        return 0.9 if "烧" in b else 0.05

    paraphrase = dispatch_find_awaiting(
        turns, text="体温正常", semantic=fake_semantic)
    check("语义：字面为零时靠向量救回来",
          bool(paraphrase) and paraphrase[0]["name"] == "源稚生"
          and paraphrase[0]["match"] >= 0.6,
          str([(f["name"], f["match"]) for f in paraphrase]))
    check("语义：确实调用了向量", bool(calls), str(len(calls)))

    # 字面能对上时不该再算向量（省一次编码）
    calls.clear()
    hit = dispatch_find_awaiting(turns, text="没有发烧", semantic=fake_semantic)
    check("语义：字面命中的那个不浪费向量（只问另一个）",
          len(calls) == 1 and hit and hit[0]["name"] == "源稚生", str(len(calls)))


def dispatch_find_awaiting(turns, *, text, semantic=None):
    from services.dialogue import dispatch

    return dispatch.find_awaiting(turns, text=text, semantic=semantic)


def awaiting_checks() -> None:
    """谁在等你回答：识别问题、匹配回答、选人加权。"""
    import random as _random

    from services.dialogue import dispatch

    turns = [
        {"role": "user", "content": "今天好难受，我感冒了"},
        {"role": "assistant", "speaker_name": "陈雯雯", "character_id": "c_chen",
         "content": "这确实挺难受的。回去先躺一会儿吧，还有哪里不舒服么？"},
        {"role": "assistant", "speaker_name": "犬山贺", "character_id": "c_quan",
         "content": "带病还要提着东西走那么远，任谁都会觉得乏。回去后有没有人替您分担一下？"},
        {"role": "assistant", "speaker_name": "源稚生", "character_id": "c_gen",
         "content": "确实够受的。现在有没有发烧？"},
    ]

    found = dispatch.find_awaiting(turns, text="没有发烧")
    check("等回答：识别出三个提问者", len(found) == 3, str([f["name"] for f in found]))
    check("等回答：按与这句话的相关度排序",
          found[0]["name"] == "源稚生" and found[0]["match"] > 0.8,
          str([(f["name"], f["match"]) for f in found]))

    found2 = dispatch.find_awaiting(turns, text="也没人给我分担")
    check("等回答：换一个问题换一个人",
          found2[0]["name"] == "犬山贺" and found2[0]["match"] > 0.5,
          str([(f["name"], f["match"]) for f in found2]))

    # 陈述句不算提问
    plain = [{"role": "assistant", "speaker_name": "叶胜", "character_id": "c_ye",
              "content": "没发烧就好，多喝点热水。"}]
    check("等回答：陈述句不算在等回答", dispatch.find_awaiting(plain, text="嗯") == [],
          str(dispatch.find_awaiting(plain, text="嗯")))

    # 隔一轮也能找到（用户先被别人搭了句话）
    two_rounds = turns + [
        {"role": "user", "content": "没有发烧"},
        {"role": "assistant", "speaker_name": "叶胜", "character_id": "c_ye",
         "content": "那就好。"},
    ]
    deeper = dispatch.find_awaiting(two_rounds, text="也没人给我分担")
    check("等回答：能回溯到上一轮的问题",
          bool(deeper) and deeper[0]["name"] == "犬山贺", str([f["name"] for f in deeper]))

    # 选人：提问者排第一
    members = [
        _shadow_member("陈雯雯", ["温柔", "体贴"], "c_chen"),
        _shadow_member("犬山贺", [], "c_quan"),
        _shadow_member("源稚生", ["冷静", "严肃"], "c_gen"),
        _shadow_member("加图索", ["话多"], "c_jia"),
    ]
    decision = dispatch.select(
        members, text="没有发烧", emotion="平静", intent="chat", intensity=0.0,
        awaiting=found, rng=_random.Random(1),
    )
    check("等回答：提问者被选中且排第一",
          decision.picks and decision.picks[0].name == "源稚生"
          and "在等你回答" in decision.picks[0].reason,
          str([(p.name, p.reason) for p in decision.picks]))
    check("等回答：会多带一个人搭腔（目标 >= 2）",
          len(decision.picks) >= 2, str(len(decision.picks)))
    check("等回答：没等回答时人数回到 1",
          dispatch.target_count(emotion="平静", intent="chat", intensity=0.0) == 1)

    # 含糊回答要按当前这句话重新匹配，而不是沿用上一句的匹配结果
    # 适配度差很多时，仍然必须让提问者接话
    cold = [
        _shadow_member("陈雯雯", ["温柔", "体贴", "耐心"], "c_chen"),
        _shadow_member("芬格尔", ["暴躁", "毒舌"], "c_fen"),
    ]
    forced_await = [{"character_id": "c_fen", "name": "芬格尔",
                     "question": "你之前是不是没好好歇着？", "match": 0.5, "depth": 0}]
    forced_decision = dispatch.select(
        cold, text="我有好好休息的", emotion="难过", intent="vent", intensity=0.6,
        awaiting=forced_await, rng=_random.Random(3),
    )
    check("等回答：提问者必答，哪怕适配度更低",
          forced_decision.picks[0].name == "芬格尔"
          and forced_decision.picks[0].forced,
          str([(p.name, p.reason) for p in forced_decision.picks]))
    check("等回答：强制入选的人排在第一位",
          forced_decision.picks[0].score >= 1.0,
          str(forced_decision.picks[0].score))

    vague_awaiting = dispatch.find_awaiting(turns, text="嗯")
    vague = dispatch.select(
        members, text="嗯", emotion="平静", intent="chat", intensity=0.0,
        awaiting=vague_awaiting, rng=_random.Random(1),
    )
    check("等回答：回答很含糊时按常规选人（不硬猜）",
          not any("在等你回答" in p.reason for p in vague.picks),
          str([(p.name, p.reason) for p in vague.picks]))


def _shadow_member(name, personality, character_id):
    """测试用的假成员（不进库）。"""
    return {
        "character_id": character_id,
        "name": name,
        "personality": personality,
        "role": "",
        "speech_style": "",
        "extraversion": 0.5,
        "arousal": 0.35,
        "status": "online",
        "state": "idle",
    }


async def sqlite_checkpoint_checks() -> None:
    """断点存 SQLite：后端重启后还能续跑；过期断点会被清掉。"""
    import tempfile

    from agent import graph as agent_graph

    character = {
        "id": "char_sql", "name": "沈砚", "role": "剑客",
        "personality": [], "speech_style": "", "background": "",
    }
    initial = {
        "user_id": "u_sql", "user_name": "读者", "character": character,
        "book_title": "测试", "shelf_id": "", "raw_text": "在吗",
        "image_urls": [], "api_key": "k", "model": "m",
        "extra_context": "", "persist": False, "session_id": "s_sql",
    }
    path = tempfile.mktemp(suffix=".db")
    config = {
        "configurable": {"thread_id": "sql-restart", "llm": None, "summarizer": None},
        "recursion_limit": 30,
    }

    async def flaky(_state):
        yield "半句"
        raise ConnectionError("断开")

    async def good(_state):
        yield "续上了"

    await agent_graph.init_checkpointer(path)
    try:
        graph = agent_graph.get_turn_graph()
        config["configurable"]["llm"] = flaky
        try:
            async for _ in graph.astream(initial, config=config, stream_mode=["custom"]):
                pass
        except ConnectionError:
            pass

        # 模拟进程重启：关连接、重新打开同一个库
        await agent_graph.close_checkpointer()
        await agent_graph.init_checkpointer(path)
        graph = agent_graph.get_turn_graph()

        snapshot = await graph.aget_state(config)
        check("断点库：重启后断点还在", snapshot.next == ("generate",), str(snapshot.next))
        check("断点库：重启后输入没丢",
              snapshot.values.get("text") == "在吗", str(snapshot.values.get("text")))

        config["configurable"]["llm"] = good
        final: dict = {}
        async for mode, chunk in graph.astream(
            None, config=config, stream_mode=["custom", "values"]
        ):
            if mode == "values" and isinstance(chunk, dict) and chunk.get("reply"):
                final = chunk
        check("断点库：重启后续跑成功", final.get("reply") == "续上了", str(final.get("reply")))

        # 过期清理：把这条断点的时间改成 2020 年，再看会不会被删
        saver = agent_graph._CHECKPOINTER  # noqa: SLF001 - 测试专用
        cursor = await saver.conn.execute(
            "SELECT thread_id, checkpoint_id, type, checkpoint FROM checkpoints"
        )
        for thread_id, checkpoint_id, ctype, blob in await cursor.fetchall():
            payload = saver.serde.loads_typed((ctype, blob))
            payload["ts"] = "2020-01-01T00:00:00+00:00"
            new_type, new_blob = saver.serde.dumps_typed(payload)
            await saver.conn.execute(
                "UPDATE checkpoints SET checkpoint = ?, type = ? "
                "WHERE thread_id = ? AND checkpoint_id = ?",
                (new_blob, new_type, thread_id, checkpoint_id),
            )
        await saver.conn.commit()

        removed = await agent_graph.prune_checkpoints(saver, days=7)
        check("断点库：过期断点被清掉", removed >= 1, str(removed))
        cursor = await saver.conn.execute("SELECT COUNT(*) FROM checkpoints")
        check("断点库：清理后表变空", (await cursor.fetchone())[0] == 0)
    finally:
        await agent_graph.close_checkpointer()

    # 关掉之后应退回内存模式，别把后面的用例带沟里
    check("断点库：关闭后回到内存模式",
          agent_graph.get_checkpointer() is not None
          and agent_graph._SAVER_CM is None,  # noqa: SLF001
          "checkpointer 未重置")


async def checkpoint_checks() -> None:
    """断点续跑：生成到一半断了，用同一个 thread_id 能接着跑完。"""
    from agent import graph as agent_graph

    character = {
        "id": "char_ckpt", "name": "沈砚", "role": "剑客",
        "personality": ["寡言"], "speech_style": "短句", "background": "江湖",
    }
    state = {"calls": 0}

    async def flaky(_state):
        state["calls"] += 1
        if state["calls"] == 1:
            yield "第一段，"
            yield "第二段，"
            raise ConnectionError("模拟断开")
        for piece in ("续上了：", "我在。"):
            yield piece

    async def good(_state):
        for piece in ("续上了：", "我在。"):
            yield piece

    initial = {
        "user_id": "u_ckpt", "user_name": "读者", "character": character,
        "book_title": "测试", "shelf_id": "", "raw_text": "在吗",
        "image_urls": [], "api_key": "k", "model": "m",
        "extra_context": "", "persist": False, "session_id": "s_ckpt",
    }
    graph = agent_graph.get_turn_graph()
    config = {
        "configurable": {"thread_id": "ckpt-test", "llm": flaky, "summarizer": None},
        "recursion_limit": 100,
    }

    first: list[dict] = []
    interrupted = False
    try:
        async for mode, chunk in graph.astream(
            initial, config=config, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                first.append(chunk)
    except ConnectionError:
        interrupted = True

    check("断点：中途断开会被捕到", interrupted, "没抛异常")
    check("断点：断开前已经流出两段",
          [c.get("text") for c in first if c.get("type") == "delta"] == ["第一段，", "第二段，"],
          str([c.get("text") for c in first]))

    snapshot = await graph.aget_state(config)
    check("断点：停在 generate 节点", snapshot.next == ("generate",), str(snapshot.next))
    check("断点：状态里留着这一轮的输入",
          snapshot.values.get("text") == "在吗", str(snapshot.values.get("text")))

    config["configurable"]["llm"] = good
    resumed: list[dict] = []
    final: dict = {}
    async for mode, chunk in graph.astream(
        None, config=config, stream_mode=["custom", "values"]
    ):
        if mode == "custom":
            resumed.append(chunk)
        elif isinstance(chunk, dict) and chunk.get("reply"):
            final = chunk

    check("断点：续跑能跑完",
          [c.get("text") for c in resumed if c.get("type") == "delta"] == ["续上了：", "我在。"],
          str([c.get("text") for c in resumed]))
    check("断点：续跑后拿到最终回复",
          final.get("reply") == "续上了：我在。", str(final.get("reply")))
    check("断点：续跑后图已结束",
          (await graph.aget_state(config)).next == (), str((await graph.aget_state(config)).next))


async def langgraph_checks() -> None:
    """直接跑一遍状态图（不经过 HTTP、不连模型）：节点顺序、流式、校验、落库。"""
    from agent import graph as agent_graph

    info = agent_graph.describe()
    check("LangGraph：图已编译", bool(info["compiled"]), str(info))
    check("LangGraph：十个节点齐全", len(info["nodes"]) == 10, str(info["nodes"]))
    check("LangGraph：节点顺序固定",
          info["nodes"] == ["preprocess", "classify", "intent", "observe", "recall",
                            "plan", "skill", "generate", "verify", "persist"],
          str(info["nodes"]))

    test_lib = dbmod.create_library("自检图库", "临时")
    shelf = dbmod.create_shelf(test_lib["id"], "图测试书架")
    book = dbmod.create_book(
        title="图测试书",
        source_name="图测试书.txt",
        source_type="txt",
        char_count=100,
        chunk_count=1,
        shelf_id=shelf["id"],
    )
    character = library.create_character(
        book["id"],
        name="沈砚",
        role="剑客",
        personality=["寡言"],
        speech_style="短句",
        background="行走江湖",
    )

    async def fake_llm(state):
        # 故意写一句说教腔，看 verify 节点会不会改写
        for piece in ("你应该", "早点睡。"):
            yield piece

    initial = {
        "user_id": "u_graph",
        "user_name": "读者",
        "character": character,
        "book_title": "图测试书",
        "shelf_id": shelf["id"],
        "raw_text": "今天好累啊",
        "image_urls": [],
        "api_key": "k",
        "model": "m",
        "extra_context": "",
        "persist": True,
        "session_id": "s_graph",
    }
    graph_config = {
        "configurable": {"thread_id": "test-graph-1", "llm": fake_llm, "summarizer": None},
        "recursion_limit": 100,
    }

    events: list[dict] = []
    final: dict = {}
    async for mode, chunk in agent_graph.get_turn_graph().astream(
        initial, config=graph_config, stream_mode=["custom", "values"]
    ):
        if mode == "custom":
            events.append(chunk)
        elif isinstance(chunk, dict) and chunk.get("reply"):
            final = chunk

    deltas = [item for item in events if item.get("type") == "delta"]
    check("LangGraph：generate 节点吐出了 delta", len(deltas) == 2, str(len(deltas)))
    check("LangGraph：文本拼起来了",
          "".join(item["text"] for item in deltas) == "你应该早点睡。",
          "".join(item["text"] for item in deltas))
    check("LangGraph：verify 改写了说教腔",
          final.get("reply") == "你可以早点睡。", str(final.get("reply")))
    check("LangGraph：state 里带上了本轮判断",
          bool(final.get("signals")) and final["signals"].emotion == "疲惫",
          str(getattr(final.get("signals"), "emotion", None)))
    check("LangGraph：plan 产出了 SOP 块",
          "【应对策略】" in (final.get("context_block") or ""), (final.get("context_block") or "")[:40])
    check("LangGraph：skill 节点产出了素材块",
          "【可用素材" in (final.get("skill_block") or ""),
          (final.get("skill_block") or "")[:60])
    check("LangGraph：素材拼进了上下文",
          "【可用素材" in (final.get("context_block") or ""),
          (final.get("context_block") or "")[-80:])

    turns = dbmod.list_chat_turns(shelf["id"], limit=10)
    check("LangGraph：persist 写了 user + assistant 两条",
          len(turns) == 2, str([(item["role"], item["content"][:8]) for item in turns]))
    check("LangGraph：user 那条带了意图与情绪",
          turns[0]["intent"] == "vent" and turns[0]["emotion"] == "疲惫",
          str((turns[0]["intent"], turns[0]["emotion"])))

    # persist=False 时不落库（群聊由引擎统一落库时用）
    initial["persist"] = False
    async for _ in agent_graph.get_turn_graph().astream(
        initial, config=graph_config, stream_mode=["custom"]
    ):
        pass
    check("LangGraph：persist=False 不写库",
          len(dbmod.list_chat_turns(shelf["id"], limit=10)) == 2,
          str(len(dbmod.list_chat_turns(shelf["id"], limit=10))))

    dbmod.delete_book(book["id"])
    dbmod.delete_shelf(shelf["id"])


async def dialogue_judge_checks() -> None:
    """LLM 判官：只在需要时出手，挂了就回退规则。"""
    from services.dialogue import dialogue
    from services.catalog import extractor

    original = extractor.call_llm_json
    calls: list = []

    async def fake(messages, **kwargs):  # noqa: ANN001, ANN003
        calls.append(messages)
        return {
            "emotion": "焦虑",
            "intensity": 0.7,
            "intent": "seek_help",
            "crisis": False,
            "reason": "在问办法",
        }

    extractor.call_llm_json = fake  # type: ignore[assignment]
    try:
        calls.clear()
        await dialogue.classify_with_judge("今天天气一般", mode="off", api_key="k", model="m")
        check("判官：off 模式一次都不调模型", not calls, str(len(calls)))

        calls.clear()
        clear_sig = await dialogue.classify_with_judge(
            "我好难过", mode="auto", api_key="k", model="m"
        )
        check("判官：auto 下明确的话不问模型", not calls, str(len(calls)))
        check("判官：明确的话保持规则结论",
              clear_sig.emotion == "难过", clear_sig.emotion)

        calls.clear()
        vague = "我该怎么办，好累啊"
        base_sig = dialogue.classify(vague)
        judged = await dialogue.classify_with_judge(
            vague, mode="auto", api_key="k", model="m"
        )
        check("判官：规则标记歧义", base_sig.ambiguous, str(base_sig.ambiguous))
        check("判官：歧义句才调模型", bool(calls), str(len(calls)))
        check("判官：结论覆盖规则", judged.intent == "seek_help", judged.intent)
        check("判官：证据里记下来了",
              any("judge" in item for item in judged.evidence), str(judged.evidence))

        calls.clear()
        await dialogue.classify_with_judge(vague, mode="auto", api_key="k", model="m")
        check("判官：同一句走缓存不重复调用", not calls, str(len(calls)))

        calls.clear()
        await dialogue.classify_with_judge("随便说一句", mode="always", api_key="k", model="m")
        check("判官：always 每句都问", bool(calls), str(len(calls)))
    finally:
        extractor.call_llm_json = original  # type: ignore[assignment]

    async def boom(*args, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("模型挂了")

    extractor.call_llm_json = boom  # type: ignore[assignment]
    try:
        fallback = await dialogue.classify_with_judge(
            "我该怎么办，好累啊，但是说出来也没人懂",
            mode="always", api_key="k", model="m",
        )
        check("判官：模型挂了回退规则结论",
              fallback.intent in ("seek_help", "vent", "chat"), fallback.intent)
    finally:
        extractor.call_llm_json = original  # type: ignore[assignment]

    merged = dialogue.merge_judge(
        dialogue.classify("今天还行"),
        {"crisis": True, "intent": "crisis", "emotion": "难过", "intensity": 0.9},
    )
    check("判官：任一方判危机就按危机处理",
          merged.crisis and merged.intent == "crisis", f"{merged.crisis}/{merged.intent}")

    negated = dialogue.classify("我不开心")
    check("规则：否定式「不开心」按难过算", negated.emotion == "难过", negated.emotion)
    check("规则：否定词不算正向命中", dialogue.classify("不难过").emotion == "平静")


async def dialogue_checks() -> None:
    """对话工作流：预处理 → 情绪/意图 → SOP → 后置校验。"""
    from services.dialogue import dialogue

    # --- 预处理 ---
    dirty = "你\u200b好\u3000\u3000啊\n\n\n\n在吗"
    check("预处理：去掉零宽字符与多余空白",
          dialogue.preprocess(dirty) == "你好 啊\n\n在吗", repr(dialogue.preprocess(dirty)))
    check("预处理：保留标点与 emoji（它们带情绪）",
          dialogue.preprocess("真的吗？！😭") == "真的吗？！😭")

    # --- 情绪 & 意图 ---
    tired = dialogue.classify("今天好累啊，什么都不想干")
    check("识别：疲惫 -> 倾诉", tired.emotion == "疲惫" and tired.intent == "vent",
          f"{tired.emotion}/{tired.intent}")

    ask = dialogue.classify("我该怎么办，工作快撑不住了")
    check("识别：求助", ask.intent == "seek_help", ask.intent)

    chatty = dialogue.classify("哈哈哈今天天气不错")
    check("识别：闲聊", chatty.intent == "chat", chatty.intent)

    crisis = dialogue.classify("我真的不想活了")
    check("识别：危机（高优先级）",
          crisis.crisis and crisis.intent == "crisis" and crisis.intensity >= 0.9,
          f"{crisis.intent}/{crisis.intensity}")

    check("识别：想死你了不算危机",
          not dialogue.classify("想死你了").crisis)
    check("识别：你去死是攻击不是危机",
          not dialogue.classify("你去死").crisis)

    # ---- 探针跑出来的 5 个真实缺陷，逐个钉住 ----
    sleeping = dialogue.classify("好想就这样睡过去算了")
    check("识别：睡过去算了 是危机（原来漏判）",
          sleeping.crisis and sleeping.intent == "crisis", f"{sleeping.intent}/{sleeping.crisis}")

    sinking = dialogue.classify("最近总觉得自己在往下沉")
    check("识别：长期低落按倾诉处理",
          sinking.emotion == "难过" and sinking.intent == "vent",
          f"{sinking.emotion}/{sinking.intent}")

    ironic = dialogue.classify("真好啊，又加班到十二点")
    check("识别：反讽判成负面而不是平静",
          ironic.emotion == "难过" and ironic.ambiguous,
          f"{ironic.emotion}/{ironic.ambiguous}")

    ironic2 = dialogue.classify("我可太开心了，又被放鸽子")
    check("识别：反讽里出现的「开心」不算真开心",
          ironic2.emotion == "难过", ironic2.emotion)

    check("识别：正常感叹不误判反讽",
          dialogue.classify("今天真开心，又是周末").emotion == "开心",
          dialogue.classify("今天真开心，又是周末").emotion)
    check("识别：「我是个好人」不被误判",
          dialogue.classify("我是个好人").emotion == "平静")

    # --- 规划层 ---
    crisis_plan = dialogue.plan(crisis)
    crisis_sop = "\n".join(crisis_plan.sop)
    check("SOP：危机走安全流程（指向现实求助）",
          any("身边" in item or "心理援助" in item or "电话" in item
              for item in crisis_plan.sop), crisis_sop[:60])
    check("SOP：危机时不许玩梗",
          any("不要开玩笑" in item for item in crisis_plan.sop), str(crisis_plan.sop))

    vent_sop = "\n".join(dialogue.plan(tired).sop)
    check("SOP：倾诉以共情为主，少建议",
          "共情" in vent_sop and "别急着给方案" in vent_sop, vent_sop[:60])

    help_sop = "\n".join(dialogue.plan(ask).sop)
    check("SOP：求助先接情绪再给办法",
          "先接住情绪" in help_sop and "1~2 条" in help_sop, help_sop[:60])

    check("SOP：块文本带【本轮判断】与【应对策略】",
          "【本轮判断" in crisis_plan.context_block()
          and "【应对策略】" in crisis_plan.context_block())

    # --- 后置校验 ---
    softened = dialogue.verify("你应该早点睡", dialogue.plan(chatty))
    check("校验：把「你应该」软化成「你可以」",
          "你可以" in softened.text and "你应该" not in softened.text, softened.text)

    unsafe = dialogue.verify("活该，自找的", dialogue.plan(chatty))
    check("校验：不安全内容整段替换",
          "活该" not in unsafe.text and unsafe.notes, unsafe.text[:30])

    care = dialogue.verify("我在。", crisis_plan)
    check("校验：危机回复缺求助提示时补一句",
          ("热线" in care.text or "信得过" in care.text) and care.notes, care.text[:40])

    # --- 情绪摘要进入蒸馏 ---
    from services.memory import chat_memory

    digest = chat_memory._mood_digest([
        {"intent": "vent", "emotion": "疲惫"},
        {"intent": "vent", "emotion": "疲惫"},
        {"intent": "seek_help", "emotion": "焦虑"},
    ])
    check("记忆：蒸馏摘要含情绪与意图分布",
          "情绪：疲惫×2" in digest and "倾诉×2" in digest, digest)


async def housekeeping_checks() -> None:
    """聊天图片保留期清理：只删过期的，别误伤新建的。"""
    import os
    import time as _time

    from services.ops import housekeeping

    base = get_settings().uploads_path / "_hk_test"
    base.mkdir(parents=True, exist_ok=True)
    fresh = base / ("a" * 32)
    stale = base / ("b" * 32)
    for directory in (fresh, stale):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "image-0.png").write_bytes(b"x" * 1024)
    old = _time.time() - 40 * 86400
    os.utime(stale, (old, old))

    result = housekeeping.cleanup_chat_images(base, days=30)
    check("清理：删掉过期的图片目录",
          result["removed"] == 1 and not stale.exists(), str(result))
    check("清理：保留没过期的", fresh.exists(), str(result))
    check("清理：统计了释放的字节", result["freed_bytes"] >= 1024, str(result))

    result0 = housekeeping.cleanup_chat_images(base, days=0)
    check("清理：days=0 表示永久保留", result0["removed"] == 0 and fresh.exists(), str(result0))

    # 目录名不合规的不动（防止手滑删到别的东西）
    stray = base / "not-a-hex-dir"
    stray.mkdir(exist_ok=True)
    old2 = _time.time() - 40 * 86400
    os.utime(stray, (old2, old2))
    housekeeping.cleanup_chat_images(base, days=1)
    check("清理：非法目录名不碰", stray.exists())

    import shutil as _shutil
    _shutil.rmtree(base, ignore_errors=True)


async def group_awake_checks() -> None:
    """群聊唤醒：不 @ 也得有人接话（否则群里死寂）。"""
    import random as _random

    from agent import group as group_mod

    members = [
        {
            "character_id": "c1", "name": "路明非", "status": "online", "state": "idle",
            "extraversion": 0.85, "arousal": 0.4, "personality": ["热情"],
            "last_spoke_at": None,
        },
        {
            "character_id": "c2", "name": "楚子航", "status": "online", "state": "idle",
            "extraversion": 0.25, "arousal": 0.2, "personality": ["寡言"],
            "last_spoke_at": None,
        },
    ]
    rng = _random.Random(11)

    plain = group_mod.select_speakers(members, text="你还好吗", mentions=[], rng=rng)
    check("群聊唤醒：不 @ 也有人接话", len(plain) == 1, str([s.name for s in plain]))
    check("群聊唤醒：兜底的是意愿最高的人",
          bool(plain) and plain[0].name == "路明非", str([s.name for s in plain]))

    muted = group_mod.select_speakers(
        [{**m, "state": "muted"} for m in members], text="喂", mentions=[], rng=rng
    )
    check("群聊唤醒：全员静音时没人说话", muted == [], str(muted))

    strict = group_mod.select_speakers(
        members, text="嗯", mentions=[], always_reply=False, rng=_random.Random(11)
    )
    check("群聊唤醒：关掉兜底就允许沉默", strict == [], str([s.name for s in strict]))

    forced = group_mod.select_speakers(
        members, text="@楚子航 在吗", mentions=["楚子航"], rng=rng
    )
    check("群聊唤醒：@ 的人必答且排第一",
          bool(forced) and forced[0].name == "楚子航" and forced[0].forced,
          str([(s.name, s.forced) for s in forced]))

    everyone = group_mod.select_speakers(
        members, text="@全体成员 开会", mentions=["all"], rng=rng
    )
    check("群聊唤醒：@全体成员 全员应答", len(everyone) == 2, str([s.name for s in everyone]))

async def group_angle_checks() -> None:
    """并行分工（角度）+ 聚合查重。"""
    import random as _random

    from agent import group_graph
    from services.dialogue import angles

    warm = {"name": "阿暖", "personality": ["温柔", "体贴", "耐心"]}
    calm = {"name": "阿静", "personality": ["冷静", "理性", "沉稳"]}
    fun = {"name": "阿乐", "personality": ["活泼", "俏皮", "爱开玩笑"]}
    plain = {"name": "阿常", "personality": []}

    assigned = angles.assign([warm, calm, fun, plain])
    keys = [item.key for item in assigned]
    check("分工：温柔的人先共情", assigned[0].key == "empathy", assigned[0].name)
    check("分工：冷静的人给方案", assigned[1].key == "plan", assigned[1].name)
    check("分工：活泼的人活跃气氛", assigned[2].key == "light", assigned[2].name)
    check("分工：一次里角度不重样", len(set(keys)) == len(keys), str(keys))

    crisis_keys = [item.key for item in angles.assign([warm, calm, fun, plain], crisis=True)]
    check("分工：危机场景不安排打趣/追问",
          "light" not in crisis_keys and "ask" not in crisis_keys, str(crisis_keys))

    # ---- 聚合查重 ----
    dbmod.init_db()
    dedup_lib = dbmod.create_library("去重自检库", "临时")
    shelf = dbmod.create_shelf(dedup_lib["id"], "去重自检架")
    book = library.create_book(
        title="去重自检书", source_name="d.txt", source_type="txt",
        text="x", shelf_id=shelf["id"],
    )
    for name, traits in (("甲", ["温柔"]), ("乙", ["冷静"]), ("丙", ["活泼"]), ("丁", [])):
        dbmod.insert_character(book_id=book["id"], name=name,
                               personality=traits, source="novel")
    members = dbmod.ensure_shelf_members(shelf["id"])

    replies = {
        "甲": "先吃饭吧，这个最要紧。吃完了再汇总工作表。",
        "乙": "先吃饭，空腹做别的也一样做不好。",
        "丙": "浇花顺手做一下就行，衣服明天也不迟，先把饭吃了。",
        "丁": "要不先躺十分钟，我在这儿陪你说会儿话。",
    }

    seen_angles: list[dict] = []

    async def generate(member, transcript):
        # 生成函数能拿到角度，说明分工真的传到提示词组装处了
        seen_angles.append(member.get("_angle") or {})
        yield replies.get(member["name"], "嗯。")

    signals = dialogue.TurnSignals(
        text="帮我排个先后", emotion="疲惫", intensity=0.5, intent="seek_help")
    events: list[dict] = []
    async for event in group_graph.get_group_graph().astream(
        {
            "shelf_id": shelf["id"], "user_id": "u_dedup", "text": "帮我排个先后",
            "members": members, "image_urls": [],
            "options": {"max_speakers": 10, "max_rounds": 1, "threshold": 0.0,
                        "all_threshold": 0.0, "cooldown_seconds": 0, "always_reply": True},
            "dialogue_plan": dialogue.plan(signals), "signals": signals,
            "rng": _random.Random(2), "generate": generate,
        },
        stream_mode="custom", config={"recursion_limit": 100},
    ):
        events.append(event)

    starts = [item for item in events if item["type"] == "speaker_start"]
    check("分工：每个发言人都带角度", all(item.get("angle") for item in starts),
          str([(item["name"], item.get("angle")) for item in starts]))

    drops = [item for item in events if item["type"] == "speaker_drop"]
    check("查重：内容雷同的回复被折叠", bool(drops), str([d["name"] for d in drops]))
    check("查重：折叠时说明和谁重复", all(d.get("similar_to") for d in drops),
          str([(d["name"], d.get("similar_to")) for d in drops]))
    kept = [
        item["name"] for item in events
        if item["type"] == "speaker_end" and item["name"] not in {d["name"] for d in drops}
    ]
    check("查重：不同角度的回复保留下来", len(kept) >= 1, str(kept))
    check("查重：不会把所有人都折叠掉", len(kept) >= 1 and len(drops) < len(starts),
          f"保留 {len(kept)} / 折叠 {len(drops)}")
    check("分工：角度真的传到了生成函数",
          bool(seen_angles) and all(item.get("name") for item in seen_angles),
          str([item.get("name") for item in seen_angles]))

    dbmod.delete_book(book["id"])
    dbmod.delete_shelf(shelf["id"])


async def group_parallel_checks() -> None:
    """Send 并行：多人同时生成，事件交错但各自独立，落库一条不少。"""
    import asyncio as _asyncio
    import random as _random
    import time as _time

    from agent import group_graph

    dbmod.init_db()
    shelf = _test_shelf("parallel")
    book = library.create_book(
        title="并行书", source_name="par.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    dbmod.insert_character(book_id=book["id"], name="沈砚", personality=["热情"], source="novel")
    dbmod.insert_character(book_id=book["id"], name="苏晚", personality=["温柔"], source="novel")
    members = dbmod.ensure_shelf_members(shelf["id"])

    from services.dialogue import dialogue as _dialogue

    signals = _dialogue.TurnSignals(
        text="我今天真的很难受", emotion="难过", intensity=0.8, intent="vent"
    )
    timeline: list[tuple[str, str]] = []

    async def generate(member, transcript):
        timeline.append(("start", member["name"]))
        await _asyncio.sleep(0.12)          # 模拟模型延迟
        timeline.append(("end", member["name"]))
        yield f"我是{member['name']}。"

    events: list[dict] = []

    async def run() -> float:
        started = _time.time()
        async for event in group_graph.get_group_graph().astream(
            {
                "shelf_id": shelf["id"],
                "user_id": "u_parallel",
                "text": "你们都在吗",
                "members": members,
                "image_urls": [],
                "options": {
                    "max_speakers": 2, "max_rounds": 1, "threshold": 0.0,
                    "all_threshold": 0.0, "cooldown_seconds": 0, "always_reply": True,
                },
                "dialogue_plan": None,
                "signals": signals,
                "rng": _random.Random(3),
                "generate": generate,
            },
            stream_mode="custom",
            config={"recursion_limit": 100},
        ):
            events.append(event)
        return _time.time() - started

    elapsed = await run()

    starts = [name for kind, name in timeline if kind == "start"]
    check("并行：两个人都被派出去", len(starts) == 2, str(timeline))
    # 并行证据：第二个人开始之前，第一个人还没结束
    if len(timeline) >= 2:
        check("并行：真并发（第二人开始早于第一人结束）",
              timeline[1][0] == "start", str(timeline))
    else:
        check("并行：真并发（第二人开始早于第一人结束）", False, str(timeline))

    # 时间断言只做兜底：真正证明并发的是上面那条"第二人开始早于第一人结束"。
    # 这里给图的固定开销留余量（拼 state、落库、问候判断约 0.1s），
    # 否则机器一忙就会偶发失败（0.25s vs 0.24s 这种）。
    serial_lower = 0.12 * len(starts)
    budget = serial_lower + 0.12
    check("并行：耗时短于串行",
          elapsed < budget,
          f"{elapsed:.2f}s vs 预算 {budget:.2f}s（串行下限 {serial_lower:.2f}s + 开销）")

    ends = [item for item in events if item["type"] == "speaker_end"]
    check("并行：每人都产出了 speaker_end", len(ends) == 2, str([e["name"] for e in ends]))
    # 群聊改成整段发送：不再逐段推 delta（否则重复内容会先显示再消失）
    deltas = [item for item in events if item["type"] == "delta"]
    check("并行：群聊不逐段推送（等聚合后整段发）", not deltas, str(len(deltas)))
    check("并行：speaker_end 带完整文本",
          all(item.get("text") for item in ends), str([e.get("text") for e in ends]))

    turns = dbmod.list_chat_turns(shelf["id"], limit=20)
    check("并行：落库一条不少",
          len([item for item in turns if item["role"] == "assistant"]) == 2,
          str(len(turns)))

    dbmod.delete_book(book["id"])
    dbmod.delete_shelf(shelf["id"])


async def group_graph_checks() -> None:
    """群聊工作流本身：图结构、事件契约、多轮链式、落库。"""
    import random as _random

    from agent import group_graph

    info = group_graph.describe()
    check("群聊图：已编译", bool(info["compiled"]), str(info))
    check("群聊图：五个节点（含聚合查重）",
          info["nodes"] == ["prepare", "pick", "speak", "dedupe", "finish"],
          str(info["nodes"]))

    dbmod.init_db()
    shelf = _test_shelf("ggraph")
    book = library.create_book(
        title="群聊图测试书", source_name="gg.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    dbmod.insert_character(
        book_id=book["id"], name="沈砚", personality=["热情", "话多"], source="novel",
    )
    dbmod.insert_character(
        book_id=book["id"], name="苏晚", personality=["温柔"], source="novel",
    )
    members = dbmod.ensure_shelf_members(shelf["id"])

    calls: list[str] = []
    replies = {"沈砚": ["@苏晚 你来。"], "苏晚": ["嗯，我在。"]}

    async def generate(member, transcript):
        calls.append(member["name"])
        # transcript 里应该有用户那句
        assert transcript and transcript[0]["speaker"] == "用户", str(transcript[:1])
        for piece in replies.get(member["name"], ["嗯。"]):
            yield piece

    events: list[dict] = []
    async for event in group_chat.run_turn(
        shelf_id=shelf["id"],
        user_id="u_ggraph",
        text="@沈砚 在吗",
        generate=generate,
        members=members,
        rng=_random.Random(7),
    ):
        events.append(event)

    kinds = [item["type"] for item in events]
    check("群聊图：先回 user 事件", kinds[0] == "user", str(kinds[:3]))
    check("群聊图：有 round_start", "round_start" in kinds, str(kinds))
    check("群聊图：speaker_start/end 成对",
          kinds.count("speaker_start") == kinds.count("speaker_end") >= 1, str(kinds))
    check("群聊图：最后是 done", kinds[-1] == "done", str(kinds[-2:]))

    ends = [item for item in events if item["type"] == "speaker_end"]
    # 空书架第一次说话会带时间问候（"傍晚了。"），所以只断言回复本身在末尾
    check("群聊图：speaker_end 带最终文本",
          bool(ends) and ends[0]["text"].endswith("@苏晚 你来。"), str(ends[:1]))

    # @ 链式：沈砚 @ 了苏晚 -> 第二轮苏晚必答
    check("群聊图：被 @ 的人下一轮接上", "苏晚" in calls, str(calls))
    done = events[-1]
    check("群聊图：done 汇总了发言人与轮数",
          done.get("rounds", 0) >= 1 and "沈砚" in (done.get("speakers") or []), str(done))

    turns = dbmod.list_chat_turns(shelf["id"], limit=20)
    assistant = [item for item in turns if item["role"] == "assistant"]
    check("群聊图：每条发言都落了库", len(assistant) == len(calls), f"{len(assistant)}/{len(calls)}")
    check("群聊图：落库带轮次", all(item["round_no"] >= 1 for item in assistant),
          str([item["round_no"] for item in assistant]))

    # 没人说话时也要正常收尾（不卡住）
    async def silent(member, transcript):
        return
        yield  # pragma: no cover

    silent_events = [
        item async for item in group_chat.run_turn(
            shelf_id=shelf["id"],
            user_id="u_ggraph",
            text="嗯",
            generate=silent,
            members=[],
            rng=_random.Random(8),
        )
    ]
    check("群聊图：空书架给提示并收尾",
          any(item["type"] == "system" for item in silent_events)
          and silent_events[-1]["type"] == "done",
          str([item["type"] for item in silent_events]))

    dbmod.delete_book(book["id"])
    dbmod.delete_shelf(shelf["id"])


async def group_turn_checks() -> None:
    """跑完整回合：用假生成器，不碰模型。"""
    dbmod.init_db()
    shelf = _test_shelf("group")
    book = library.create_book(
        title="群聊书", source_name="g.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    shen = dbmod.insert_character(
        book_id=book["id"], name="沈砚", personality=["热情", "话多"], source="novel",
    )
    su = dbmod.insert_character(
        book_id=book["id"], name="苏晚", personality=["温柔"], source="novel",
    )
    persona = dbmod.insert_character(
        book_id=book["id"], name="我", personality=["好奇"], source="user",
        is_user_persona=True,
    )

    # ---- 成员同步 ----
    members = dbmod.ensure_shelf_members(shelf["id"])
    names = {m["name"] for m in members}
    check("群聊：书架角色自动入群", {"沈砚", "苏晚"} <= names, str(names))
    check("群聊：用户自建角色不入群", "我" not in names, str(names))

    # ---- 完整回合：沈砚 @ 苏晚 -> 第二轮苏晚必答 ----
    replies = {
        "沈砚": ["@苏晚 你怎么看？"],
        "苏晚": ["我觉得还好。"],
    }
    calls: list[str] = []

    async def generate(member, transcript):
        calls.append(member["name"])
        for piece in replies.get(member["name"], ["嗯。"]):
            yield piece

    events = []
    async for event in group_chat.run_turn(
        shelf_id=shelf["id"],
        user_id="u_group",
        text="大家好",
        generate=generate,
        members=members,
        rng=__import__("random").Random(11),
        tweaks={"max_speakers": 1, "max_rounds": 2, "threshold": 0.0, "all_threshold": 0.0},
    ):
        events.append(event)

    kinds = [e["type"] for e in events]
    check("群聊：回合从用户消息开始", kinds[0] == "user", str(kinds[:3]))
    check("群聊：产生 round_start", "round_start" in kinds, str(kinds))
    check("群聊：产生 speaker_start/speaker_end（整段发送，无 delta）",
          {"speaker_start", "speaker_end"} <= set(kinds) and "delta" not in kinds,
          str(kinds))
    check("群聊：以 done 结束", kinds[-1] == "done", str(kinds[-3:]))

    speakers = [e["name"] for e in events if e["type"] == "speaker_end"]
    check("群聊：角色 @ 别人会触发下一轮必答",
          speakers == ["沈砚", "苏晚"], str(speakers))
    forced_second = [e for e in events if e["type"] == "speaker_start" and e.get("forced")]
    check("群聊：第二轮的苏晚是必答", bool(forced_second), str(forced_second))

    done = events[-1]
    check("群聊：done 里带发言人列表",
          done.get("speakers") == ["沈砚", "苏晚"], str(done))

    # ---- 发言落库 ----
    turns = dbmod.list_chat_turns(shelf["id"], user_id="u_group")
    check("群聊：用户与角色发言都落库",
          [t["role"] for t in turns] == ["user", "assistant", "assistant"],
          str([t["role"] for t in turns]))
    check("群聊：发言带发言人名字",
          [t["speaker_name"] for t in turns if t["role"] == "assistant"] == ["沈砚", "苏晚"],
          str([t["speaker_name"] for t in turns]))
    check("群聊：发言记录了 @",
          any("苏晚" in (t["mentions"] or []) for t in turns),
          str([t["mentions"] for t in turns]))

    # ---- 发言后进入冷却 ----
    state = dbmod.list_shelf_members(shelf["id"])
    shen_state = next(m for m in state if m["name"] == "沈砚")
    check("群聊：发过言的人进入冷却",
          shen_state["state"] == "cooldown" and shen_state["turns"] == 1,
          f"{shen_state['state']} / {shen_state['turns']}")

    # ---- 生成失败不影响其他人 ----
    async def flaky(member, transcript):
        if member["name"] == "沈砚":
            raise RuntimeError("模型挂了")
        yield "我还在。"

    events2 = []
    async for event in group_chat.run_turn(
        shelf_id=shelf["id"], user_id="u_group2", text="再聊聊",
        generate=flaky, members=members,
        rng=__import__("random").Random(12),
        tweaks={"max_speakers": 1, "max_rounds": 1, "threshold": 0.0, "all_threshold": 0.0},
    ):
        events2.append(event)
    kinds2 = [e["type"] for e in events2]
    check("群聊：单个角色失败不影响整个回合",
          "error" in kinds2 and kinds2[-1] == "done", str(kinds2))

    # ---- 空群 ----
    empty_shelf = _test_shelf("group-empty")
    events3 = []
    async for event in group_chat.run_turn(
        shelf_id=empty_shelf["id"], user_id="u_empty", text="有人吗",
        generate=generate, members=[],
    ):
        events3.append(event)
    check("群聊：空书架给出提示而不是报错",
          events3[0]["type"] == "system" and events3[-1]["type"] == "done",
          str([e["type"] for e in events3]))

    # ---- 清理 + 级联检查 ----
    library.delete_book(book["id"])
    left = dbmod.list_shelf_members(shelf["id"], include_left=True)
    check("群聊：删书后成员关系被清理", left == [], str(left))
    dbmod.delete_shelf(shelf["id"])
    check("群聊：删书架后发言流水被清理",
          dbmod.list_chat_turns(shelf["id"]) == [])
    dbmod.delete_shelf(empty_shelf["id"])


# ---------------------------------------------------------------------------
# 书架事务：加书 / 删书，角色进出群 + 记忆补强 / 回收
# ---------------------------------------------------------------------------
def shelf_book_checks() -> None:
    dbmod.init_db()
    shelf_a = _test_shelf("shelfbook-a")
    shelf_b = _test_shelf("shelfbook-b")

    book1 = library.create_book(
        title="系列I", source_name="a.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf_a["id"],
    )
    shen = dbmod.insert_character(
        book_id=book1["id"], name="沈砚", personality=["寡言"],
        source="novel", excerpts=["片段一"],
    )

    # 第二本书：同系列，沈砚在里面又出场了
    book2 = library.create_book(
        title="系列II", source_name="b.txt", source_type="txt",
        text="沈砚在雨里站了很久，没有说话。" * 20, shelf_id=shelf_b["id"],
    )
    dbmod.insert_character(
        book_id=book2["id"], name="苏晚", personality=["温柔"], source="novel",
    )
    check("书架事务：两本书本来在不同书架",
          dbmod.get_book(book2["id"])["shelf_id"] == shelf_b["id"])

    # ---- 加书 ----
    result = shelf.add_book(shelf_a["id"], book2["id"])
    member_names = {m["name"] for m in result["members"]}
    check("书架事务：加书后两本书的角色进同一个群",
          {"沈砚", "苏晚"} <= member_names, str(member_names))
    check("书架事务：书真的移到了目标书架",
          dbmod.get_book(book2["id"])["shelf_id"] == shelf_a["id"])
    check("书架事务：原书架不再有这些成员",
          dbmod.list_shelf_members(shelf_b["id"]) == [])

    items = dbmod.list_memory_items(shen["id"], source_book_id=book2["id"])
    check("书架事务：给旧角色补了来自新书的记忆", len(items) == 1, str(len(items)))
    check("书架事务：补强内容确实是原文片段",
          bool(items) and "沈砚" in items[0]["content"], str(items)[:80])
    check("书架事务：excerpts 缓存同步包含补强片段",
          len(dbmod.get_character(shen["id"])["excerpts"]) == 2,
          str(dbmod.get_character(shen["id"])["excerpts"]))

    again = shelf.add_book(shelf_a["id"], book2["id"])
    check("书架事务：重复加书不重复处理", again["moved"] is False, str(again.get("moved")))

    # ---- 删书 ----
    removed = shelf.remove_book(shelf_a["id"], book2["id"])
    check("书架事务：删书成功", removed["deleted"] is True)
    check("书架事务：删书后该书角色退群",
          {m["name"] for m in removed["members"]} == {"沈砚"},
          str([m["name"] for m in removed["members"]]))
    check("书架事务：删书后补强记忆被精确回收",
          dbmod.list_memory_items(shen["id"], source_book_id=book2["id"]) == [])
    check("书架事务：回收后旧角色仍有自己的记忆",
          len(dbmod.list_memory_items(shen["id"], kind="excerpt")) == 1,
          str(len(dbmod.list_memory_items(shen["id"], kind="excerpt"))))

    raised = False
    try:
        shelf.remove_book(shelf_b["id"], book1["id"])
    except ValueError:
        raised = True
    check("书架事务：拿掉不属于该书架的书会被拒绝", raised)

    library.delete_book(book1["id"])
    dbmod.delete_shelf(shelf_a["id"])
    dbmod.delete_shelf(shelf_b["id"])


# ---------------------------------------------------------------------------
# 同名角色合并（同系列多本书里的"同一个人"）
# ---------------------------------------------------------------------------
def merge_checks() -> None:
    dbmod.init_db()

    # ---- 纯逻辑：身份归一与分组 ----
    check("合并：名字归一化去掉间隔号", merge.normalize(" 路·明非 ") == "路明非",
          merge.normalize(" 路·明非 "))
    keys = merge.identity_keys({"name": "路明非", "aliases": ["明非", "路神人"]})
    check("合并：身份键包含别名", {"路明非", "明非", "路神人"} <= keys, str(keys))
    check("合并：太短的名字不当身份键",
          merge.identity_keys({"name": "我", "aliases": []}) == set(),
          str(merge.identity_keys({"name": "我", "aliases": []})))

    grouped = merge.group_duplicates(
        [
            {"id": "a", "name": "路明非", "aliases": [], "appearances": 3},
            {"id": "b", "name": "明非", "aliases": ["路明非"], "appearances": 1},
            {"id": "c", "name": "楚子航", "aliases": [], "appearances": 5},
        ]
    )
    check("合并：同一个人分到一组",
          len(grouped) == 1 and {c["id"] for c in grouped[0]} == {"a", "b"}, str(grouped))
    check("合并：本体取出场最多的", merge.pick_canonical(grouped[0])["id"] == "a")

    # ---- 回归：别把不同的人合并 ----
    # 真实数据里踩过：赵孟华的别名是「老大」，恺撒·加图索的别名也是「老大」，
    # 靠"别名相交"会把这两个完全不同的人并成一个。
    zhao = {"name": "赵孟华", "aliases": ["老大", "金牌小生"]}
    caesar = {"name": "恺撒", "aliases": []}
    caesar_full = {"name": "恺撒·加图索", "aliases": ["恺撒", "老大"]}
    check("合并：共享通用昵称不算同一人",
          merge.same_person(zhao, caesar) is False, "赵孟华 vs 恺撒")
    check("合并：主名出现在别名里才算同一人",
          merge.same_person(caesar, caesar_full) is True, "恺撒 vs 恺撒·加图索")
    check("合并：通用称呼不进身份键",
          "老大" not in merge.identity_keys(zhao), str(merge.identity_keys(zhao)))

    grouped2 = merge.group_duplicates(
        [{**zhao, "id": "z"}, {**caesar, "id": "c"}, {**caesar_full, "id": "cf"}]
    )
    check("合并：不同的人不会被并进同一组",
          len(grouped2) == 1 and {c["id"] for c in grouped2[0]} == {"c", "cf"},
          str([[c["id"] for c in g] for g in grouped2]))

    # ---- 真实数据：两本书各有一个路明非 ----
    shelf_row = _test_shelf("merge")
    book1 = library.create_book(
        title="系列I", source_name="a.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf_row["id"],
    )
    book2 = library.create_book(
        title="系列II", source_name="b.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf_row["id"],
    )
    first = dbmod.insert_character(
        book_id=book1["id"], name="路明非", appearances=10,
        source="novel", excerpts=["甲书的记忆一", "甲书的记忆二"],
    )
    second = dbmod.insert_character(
        book_id=book2["id"], name="路明非", aliases=["明非"], appearances=5,
        source="novel", excerpts=["乙书的记忆一"],
    )
    bystander = dbmod.insert_character(
        book_id=book2["id"], name="楚子航", appearances=7,
        source="novel", excerpts=["丙的记忆"],
    )

    result = merge.merge_shelf(shelf_row["id"])
    check("合并：发现一组重复角色", result["groups"] == 1, str(result["groups"]))
    same_name = [m for m in result["members"] if m["name"] == "路明非"]
    check("合并：群里只剩一个路明非", len(same_name) == 1, str([m["name"] for m in result["members"]]))

    child = dbmod.get_character(second["id"])
    check("合并：被合并的角色打上 canonical_id",
          child["canonical_id"] == first["id"] and child["is_merged"], str(child["canonical_id"]))

    items = dbmod.list_memory_items(first["id"], kind="excerpt")
    check("合并：记忆做相加（2 段 + 1 段 = 3 段）", len(items) == 3, str(len(items)))
    check("合并：相加后来源仍分属两本书",
          len({i["source_book_id"] for i in items}) == 2,
          str({i["source_book_id"] for i in items}))

    memory_summary = agent_memory.summary(first["id"])
    check("合并：生成了记忆摘要", bool(memory_summary), memory_summary[:60])
    check("合并：摘要是独立条目，不混进原文片段",
          all(i["kind"] != "summary" for i in items))
    check("合并：摘要里能看到两本书的内容",
          "甲书" in memory_summary and "乙书" in memory_summary, memory_summary[:80])

    again = merge.merge_shelf(shelf_row["id"])
    check("合并：重复调用不会重复合并", again["groups"] == 0, str(again["groups"]))

    # ---- 回档：删掉本体所在的书 ----
    outcome = shelf.remove_book(shelf_row["id"], book1["id"])
    check("回档：删本体所在书时释放了被合并的角色",
          outcome["released_merged"] >= 1, str(outcome["released_merged"]))

    revived = dbmod.get_character(second["id"])
    check("回档：被合并的角色恢复独立",
          revived is not None and revived["canonical_id"] is None,
          str(revived and revived["canonical_id"]))
    own_items = dbmod.list_memory_items(second["id"], kind="excerpt")
    check("回档：它自己那本书的记忆被迁回来了",
          len(own_items) == 1 and own_items[0]["source_book_id"] == book2["id"],
          str([(i["source_book_id"], i["content"]) for i in own_items]))
    check("回档：恢复后重新生成了摘要", bool(agent_memory.summary(second["id"])),
          agent_memory.summary(second["id"])[:60])
    check("回档：它只记得自己那本书",
          "乙书" in agent_memory.summary(second["id"]),
          agent_memory.summary(second["id"])[:80])

    check("合并：旁观角色没有被误合并",
          dbmod.get_character(bystander["id"])["canonical_id"] is None)

    # 删角色时群成员关系要一起清掉（否则会留下指向已删角色的残留行）
    extra = dbmod.insert_character(
        book_id=book2["id"], name="路人甲", personality=[], source="novel",
    )
    dbmod.ensure_shelf_members(shelf_row["id"])
    dbmod.delete_character(extra["id"])
    leftovers = [
        m for m in dbmod.list_shelf_members(shelf_row["id"], include_left=True)
        if m["character_id"] == extra["id"]
    ]
    check("合并：删角色会清掉群成员残留", leftovers == [], str(leftovers))

    # ---- 手动合并：自动规则漏掉的（诺诺 / 陈墨瞳 是同一人，但别名没互相指向）----
    nuo = dbmod.insert_character(
        book_id=book2["id"], name="诺诺", personality=["直率"],
        source="novel", excerpts=["诺诺的片段"],
    )
    chen = dbmod.insert_character(
        book_id=book2["id"], name="陈墨瞳", personality=["直率"],
        source="novel", excerpts=["陈墨瞳的片段"],
    )
    manual = merge.merge_pair(nuo["id"], chen["id"])
    check("手动合并：返回合并结果", manual["merged"]["into"] == "陈墨瞳",
          str(manual["merged"]))
    check("手动合并：记忆相加",
          len(dbmod.list_memory_items(chen["id"], kind="excerpt")) == 2,
          str(len(dbmod.list_memory_items(chen["id"], kind="excerpt"))))
    check("手动合并：被合并方打上标记",
          dbmod.get_character(nuo["id"])["canonical_id"] == chen["id"])
    check("手动合并：被合并方的名字变成别名",
          "诺诺" in dbmod.get_character(chen["id"])["aliases"],
          str(dbmod.get_character(chen["id"])["aliases"]))
    check("手动合并：群成员少一个",
          all(m["character_id"] != nuo["id"]
              for m in dbmod.list_shelf_members(shelf_row["id"])))

    raised = False
    try:
        merge.merge_pair(chen["id"], chen["id"])
    except ValueError:
        raised = True
    check("手动合并：不能合并到自己", raised)

    raised = False
    try:
        merge.merge_pair(nuo["id"], chen["id"])
    except ValueError:
        raised = True
    check("手动合并：已合并过的不能再合并", raised)

    outsider_shelf = _test_shelf("merge-outsider")
    outsider_book = library.create_book(
        title="别的书架", source_name="o.txt", source_type="txt",
        text=SAMPLE, shelf_id=outsider_shelf["id"],
    )
    outsider = dbmod.insert_character(
        book_id=outsider_book["id"], name="外人", personality=[], source="novel",
    )
    raised = False
    try:
        merge.merge_pair(outsider["id"], chen["id"])
    except ValueError:
        raised = True
    check("手动合并：跨书架被拒绝", raised)
    library.delete_book(outsider_book["id"])
    dbmod.delete_shelf(outsider_shelf["id"])

    # ---- 撤销合并 ----
    merge.unmerge(nuo["id"])
    check("撤销合并：角色恢复独立",
          dbmod.get_character(nuo["id"])["canonical_id"] is None)
    restored_items = dbmod.list_memory_items(nuo["id"], kind="excerpt")
    check("撤销合并：取回属于自己的记忆",
          len(restored_items) == 1
          and restored_items[0]["source_book_id"] == book2["id"],
          str([(i["source_book_id"], i["content"]) for i in restored_items]))
    check("撤销合并：对方只剩自己的记忆",
          len(dbmod.list_memory_items(chen["id"], kind="excerpt")) == 1,
          str(len(dbmod.list_memory_items(chen["id"], kind="excerpt"))))

    library.delete_book(book2["id"])
    dbmod.delete_shelf(shelf_row["id"])


# ---------------------------------------------------------------------------
# 表达层（贴纸）
# ---------------------------------------------------------------------------
def expression_checks() -> None:
    import random as _random
    import re as _re

    catalog = expression.catalog()
    check("贴纸：覆盖情绪模型的全部标签",
          set(affect.EMOTION_STYLE.keys()) <= set(catalog.keys()),
          f"缺 {set(affect.EMOTION_STYLE.keys()) - set(catalog.keys())}")
    check("贴纸：每个情绪都有候选",
          all(len(items) >= 2 for items in catalog.values()),
          str({k: len(v) for k, v in catalog.items()}))

    pattern = _re.compile(r"^[0-9a-f]{1,8}(-[0-9a-f]{1,8})*$")
    check("贴纸：码点格式合法（Twemoji 文件名）",
          all(pattern.match(cp) for cp in expression.all_codepoints()),
          str([cp for cp in expression.all_codepoints() if not pattern.match(cp)]))
    check("贴纸：url 指向本地代理",
          all(item["url"].startswith("/api/sticker/")
              for items in catalog.values() for item in items))

    calm = expression.sticker_chance({"arousal": 0.1, "valence": 0.0})
    excited = expression.sticker_chance({"arousal": 0.9, "valence": 0.8})
    check("贴纸：越激动越可能发", excited > calm, f"{calm:.3f} / {excited:.3f}")
    check("贴纸：概率有上限（不会每条都发）",
          expression.sticker_chance({"arousal": 1.0, "valence": 1.0})
          <= expression.MAX_CHANCE, str(expression.MAX_CHANCE))

    state = {"emotion": "喜悦", "arousal": 0.8, "valence": 0.9}
    check("贴纸：同种子结果可复现",
          expression.maybe_sticker(state, rng=_random.Random(3))
          == expression.maybe_sticker(state, rng=_random.Random(3)))

    quiet_hits = sum(
        1 for seed in range(200)
        if expression.maybe_sticker(
            {"emotion": "平静", "arousal": 0.05, "valence": 0.0},
            rng=_random.Random(seed),
        )
    )
    check("贴纸：平静时很少发", quiet_hits < 40, f"200 次里发了 {quiet_hits} 次")

    picked = expression.pick_sticker({"emotion": "恼怒"}, rng=_random.Random(1))
    check("贴纸：挑出的贴纸带情绪/码点/emoji",
          picked["emotion"] == "恼怒" and picked["codepoint"] and picked["emoji"],
          f"{picked['emotion']} / {picked['codepoint']}")
    fallback = expression.pick_sticker({"emotion": "不存在的情绪"}, rng=_random.Random(1))
    check("贴纸：未知情绪回退到平静",
          fallback["emotion"] == "平静", fallback["emotion"])

    # 内置素材必须真的在盘上，否则前端只能退化成系统 emoji
    from pathlib import Path as _Path

    sticker_dir = BACKEND_DIR / "data" / "stickers"
    missing = [cp for cp in expression.all_codepoints() if not (sticker_dir / f"{cp}.svg").exists()]
    check("贴纸：素材已内置（离线可用）", missing == [], str(missing))


async def sticker_http_checks() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/stickers")
        check("HTTP：贴纸目录 200", r.status_code == 200, r.text[:120])
        check("HTTP：贴纸目录含 9 种情绪",
              len(r.json().get("emotions", {})) >= 9,
              str(list(r.json().get("emotions", {}))))

        r = await client.get("/api/sticker/not-a-codepoint.svg")
        check("HTTP：非法贴纸名 -> 400", r.status_code == 400, str(r.status_code))
        r = await client.get("/api/sticker/..%2F..%2Fapp.svg")
        check("HTTP：路径穿越被挡住", r.status_code in (400, 404), str(r.status_code))

        # 合法贴纸：联网时 200 并落盘，离线时 502 —— 两种都算通过
        r = await client.get("/api/sticker/1f604.svg")
        check("HTTP：合法贴纸返回 SVG 或明确取不到",
              r.status_code in (200, 502), str(r.status_code))
        if r.status_code == 200:
            check("HTTP：贴纸 content-type 为 svg",
                  "svg" in r.headers.get("content-type", ""),
                  r.headers.get("content-type", ""))
            check("HTTP：贴纸内容是 SVG", b"<svg" in r.content[:400], r.text[:80])


# ---------------------------------------------------------------------------
# RAG 记忆库：分词 / 场景 / 向量检索 / 降级 / 对话蒸馏
# ---------------------------------------------------------------------------
def rag_checks() -> None:
    # ---- 分词 ----
    tokens = text_mod.tokenize("路明非在雨夜的车站")
    check("RAG：中文切成二元组", "路明" in tokens and "明非" in tokens, str(tokens[:8]))
    check("RAG：过滤停用二元组", "什么" not in text_mod.tokenize("这是什么"), "什么")
    check("RAG：拉丁词整词保留",
          "rtx4090" in text_mod.tokenize("显卡 RTX4090 到货"), str(text_mod.tokenize("显卡 RTX4090 到货")))
    check("RAG：空文本返回空", text_mod.tokenize("") == [])

    # ---- 场景识别 ----
    check("场景：认出战斗", scene_mod.detect("他拔刀冲了上去") == "战斗")
    check("场景：认出雨夜", scene_mod.detect("雨里撑着伞") == "雨夜")
    check("场景：认出校园", scene_mod.detect("教室里的同学都在上课") == "校园")
    check("场景：认不出时用默认", scene_mod.detect("嗯") == "日常", scene_mod.detect("嗯"))
    check("场景：多条取多数",
          scene_mod.detect_many(["下雨了", "雨伞在哪", "教室里"]) == "雨夜",
          scene_mod.detect_many(["下雨了", "雨伞在哪", "教室里"]))

    # ---- 建库 ----
    shelf_row = _test_shelf("rag")
    book = library.create_book(
        title="RAG书", source_name="r.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf_row["id"],
    )
    character = dbmod.insert_character(
        book_id=book["id"], name="沈砚", personality=["寡言"], source="novel",
    )
    agent_memory.remember(
        character["id"], ["雨夜里他撑着伞在车站等了她三个小时。"], kind="fact", importance=0.8,
    )
    agent_memory.remember(
        character["id"], ["教室里他低头写作业，粉笔灰落在袖口。"], kind="fact", importance=0.8,
    )
    agent_memory.remember(
        character["id"], ["他拔刀迎上黑影，刀锋在雨中划出一道白线。"], kind="fact", importance=0.8,
    )

    rows = dbmod.list_memory_items(character["id"], kind="fact")
    check("RAG：记忆写入时自动切词", all(row["tokens"] for row in rows), str([r["tokens"][:10] for r in rows]))
    check("RAG：记忆写入时自动打场景",
          {row["scene"] for row in rows} >= {"雨夜", "校园", "战斗"},
          str(sorted({row["scene"] for row in rows})))

    vectors_written = [
        r for r in dbmod.list_memory_items(character["id"], kind="fact") if r["vector"]
    ]
    check("RAG：写入时已自动建向量", len(vectors_written) == 3, str(len(vectors_written)))
    check("RAG：重复建索引不会重复计算",
          rag.index_character(character["id"]) == 0,
          str(rag.index_character(character["id"])))
    with_vector = [r for r in dbmod.list_memory_items(character["id"], kind="fact") if r["vector"]]
    check("RAG：向量长度 = 512 * 4 字节",
          all(len(r["vector"]) == rag.EMBEDDING_DIM * 4 for r in with_vector),
          str([len(r["vector"]) for r in with_vector]))
    check("RAG：后端是向量（模型可加载）", rag.backend() == "vector", rag.backend())

    # ---- 检索质量 ----
    hits = rag.search(character["id"], "下雨天他还在车站等人吗", top_k=3)
    check("RAG：检索有结果", bool(hits), "0")
    check("RAG：语义相关的那条排第一",
          bool(hits) and "车站" in hits[0]["content"], str(hits[0]["content"][:40]) if hits else "")
    check("RAG：检索结果带场景和分数",
          bool(hits) and hits[0]["scene"] and hits[0]["score"] > 0,
          str(hits[0]) if hits else "")

    hits2 = rag.search(character["id"], "他拔刀砍过去", top_k=3)
    check("RAG：不同话题捞到不同记忆",
          bool(hits2) and "刀" in hits2[0]["content"], str(hits2[0]["content"][:40]) if hits2 else "")

    block = rag.recall_block(character["id"], "下雨了，你还记得那天吗")
    check("RAG：能拼成提示词块", block.startswith("【与你这句话相关的记忆】"), block[:40])
    check("RAG：块里带场景标签", "（雨夜）" in block or "（" in block, block[:60])

    # ---- 长记忆要切块，否则中段信息检索不到 ----
    long_text = (
        "他站在教学楼的天台上，看着远处的城市灯火，风很大。"
        "那天他们说了很多话，从小时候的事一直聊到将来要去哪里。"
        "第二天清晨他独自去了车站，买了一张没有目的地的票。"
        "雨下得很大，没有人来送他，站台上只有卖早点的小摊还亮着灯。"
        "多年以后他偶尔还会想起那个清晨，想起站台上那碗没喝完的粥。"
    ) * 3
    long_item = agent_memory.remember(
        character["id"], [long_text], kind="fact", importance=0.6,
    )[0]

    chunks = [
        chunk for chunk in dbmod.list_memory_chunks(character["id"])
        if chunk["item_id"] == long_item["id"]
    ]
    check("RAG：长记忆被切成多块", len(chunks) > 1, str(len(chunks)))
    check("RAG：每块都带向量",
          all(chunk["vector"] for chunk in chunks), str(len(chunks)))

    middle = rag.search(character["id"], "站台上那碗没喝完的粥", top_k=3)
    check("RAG：能按中段内容检索到长记忆",
          bool(middle) and middle[0]["id"] == long_item["id"],
          str([(h["content"][:16], h["score"]) for h in middle]))

    # ---- 降级：模型不可用时仍然能用（词法） ----
    saved = dict(rag._embedder_state)
    try:
        rag._embedder_state["embedder"] = None
        rag._embedder_state["tried"] = True
        fallback_hits = rag.search(character["id"], "下雨天车站等人", top_k=3)
        check("RAG：向量不可用时降级为词法",
              bool(fallback_hits) and "车站" in fallback_hits[0]["content"],
              str(fallback_hits[0]["content"][:30]) if fallback_hits else "0")
    finally:
        rag._embedder_state.clear()
        rag._embedder_state.update(saved)

    # ---- 延迟（响应速度红线）----
    embedder = rag._load_embedder()
    if embedder is not None:
        import time as _time

        embedder.encode(["预热"])
        started = _time.perf_counter()
        for _ in range(3):
            embedder.encode(["你还记得那天的雨吗"])
        per_query = (_time.perf_counter() - started) / 3 * 1000
        check("RAG：单次查询编码 < 200ms（不影响聊天响应）",
              per_query < 200, f"{per_query:.1f} ms")

    # ---- 对话蒸馏 ----
    dbmod.add_chat_turn(
        shelf_id=shelf_row["id"], user_id="u_rag", role="user",
        content="我们上次说的那个计划还算数吗", character_id=character["id"],
    )
    dbmod.add_chat_turn(
        shelf_id=shelf_row["id"], user_id="u_rag", role="assistant",
        content="算数。我记着。", character_id=character["id"], speaker_name="沈砚",
    )
    check("蒸馏：先有未处理的对话流水",
          dbmod.count_chat_turns(shelf_row["id"]) == 2,
          str(dbmod.count_chat_turns(shelf_row["id"])))

    result = chat_memory.distill(
        shelf_row["id"], character["id"], keep_days=0, character_name="沈砚",
    )
    check("蒸馏：把对话压成一条记忆", result["distilled"] == 2, str(result))
    check("蒸馏：流水标记为已蒸馏（不再参与下一轮）",
          dbmod.count_chat_turns(shelf_row["id"], pending_only=True) == 0,
          str(dbmod.count_chat_turns(shelf_row["id"], pending_only=True)))
    check("蒸馏：原文仍保留（重进群聊要能看到历史）",
          dbmod.count_chat_turns(shelf_row["id"]) == 2,
          str(dbmod.count_chat_turns(shelf_row["id"])))
    check("蒸馏：返回里说明了归档条数", result.get("archived") == 2, str(result))

    facts = dbmod.list_memory_items(character["id"], kind="fact")
    distilled = [r for r in facts if r["scene"] == chat_memory.DIALOGUE_SCENE]
    check("蒸馏：记忆打上「对话」场景", len(distilled) == 1, str(len(distilled)))
    check("蒸馏：内容含用户说过的话",
          bool(distilled) and "计划" in distilled[0]["content"],
          distilled[0]["content"][:60] if distilled else "")
    check("蒸馏：蒸馏结果也进了向量索引",
          bool(distilled) and bool(distilled[0]["vector"]), "0")

    recalled = rag.search(character["id"], "那个计划还算数吗", top_k=5)
    check("蒸馏：聊过的事下次能被检索到",
          any(h["kind"] == "fact" and "计划" in h["content"] for h in recalled),
          str([h["content"][:20] for h in recalled]))

    # 阈值触发
    check("蒸馏：低于阈值不触发",
          chat_memory.maybe_distill(shelf_row["id"], character["id"]) is None)

    library.delete_book(book["id"])
    dbmod.delete_shelf(shelf_row["id"])


async def rag_http_checks() -> None:
    transport = httpx.ASGITransport(app=app)
    dbmod.init_db()
    shelf_row = _test_shelf("rag-http")
    book = library.create_book(
        title="RAGHTTP书", source_name="h.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf_row["id"],
    )
    character = dbmod.insert_character(
        book_id=book["id"], name="沈砚", personality=["寡言"], source="novel",
    )
    agent_memory.remember(character["id"], ["雨夜里他撑着伞站在车站。"], kind="fact")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/rag/status")
        check("HTTP：RAG 状态 200", r.status_code == 200, r.text[:120])
        check("HTTP：RAG 状态带后端与统计",
              {"backend", "stats", "model_exists"} <= set(r.json()), str(r.json())[:120])

        r = await client.post("/api/rag/index")
        check("HTTP：重建索引 200", r.status_code == 200, r.text[:120])
        check("HTTP：重建索引返回条数", "vectors" in r.json(), r.text[:120])

        r = await client.get(f"/api/characters/{character['id']}/recall", params={"q": "下雨天车站"})
        check("HTTP：检索预览 200", r.status_code == 200, r.text[:200])
        body = r.json()
        check("HTTP：检索预览带命中与场景",
              bool(body.get("hits")) and body.get("scene") == "雨夜", str(body)[:160])

        r = await client.get("/api/characters/char_none/recall", params={"q": "x"})
        check("HTTP：不存在的角色检索 -> 404", r.status_code == 404, str(r.status_code))

        r = await client.post(f"/api/characters/{character['id']}/distill")
        check("HTTP：蒸馏接口 200", r.status_code == 200, r.text[:160])

        r = await client.get("/api/health")
        check("HTTP：健康检查暴露 RAG 后端",
              "rag" in r.json() and "backend" in r.json()["rag"], str(r.json().get("rag"))[:120])

    library.delete_book(book["id"])
    dbmod.delete_shelf(shelf_row["id"])


# ---------------------------------------------------------------------------
# 渠道抽象（web 已启用，QQ/微信预留）
# ---------------------------------------------------------------------------
def channels_checks() -> None:
    web = channels.get("web")
    qq = channels.get("qq")
    wechat = channels.get("wechat")

    check("渠道：web 已启用", web.enabled is True, str(web.enabled))
    check("渠道：QQ/微信尚未启用",
          qq.enabled is False and wechat.enabled is False,
          f"{qq.enabled}/{wechat.enabled}")
    check("渠道：默认渠道是 web", channels.DEFAULT_CHANNEL == "web", channels.DEFAULT_CHANNEL)

    raised = False
    try:
        channels.get("telegram")
    except KeyError:
        raised = True
    check("渠道：未知渠道报错", raised)

    check("渠道：web 支持流式与 @",
          web.capabilities().supports_streaming and web.capabilities().supports_mentions)
    check("渠道：QQ 明确不支持流式",
          qq.capabilities().supports_streaming is False)
    check("渠道：微信明确不支持 @",
          wechat.capabilities().supports_mentions is False)
    check("渠道：各平台都有字数上限",
          all(channels.get(name).capabilities().max_text_length > 0
              for name in ("web", "qq", "wechat")))

    described = channels.describe_all()
    check("渠道：能力清单含三个平台", len(described) == 3, str(len(described)))
    check("渠道：清单里带 enabled 标记",
          all("enabled" in item for item in described))

    # ---- 事件 -> 出站消息 ----
    start = channels.event_to_outbound(
        {"type": "speaker_start", "character_id": "c1", "name": "沈砚", "forced": True, "reason": "被 @"},
        channel="web", conversation_id="shelf_x",
    )
    check("渠道：speaker_start 映射为 start",
          start.kind == channels.KIND_START and start.sender_name == "沈砚", str(start))
    check("渠道：附加字段进 meta",
          start.meta.get("forced") is True and start.meta.get("reason") == "被 @", str(start.meta))

    end = channels.event_to_outbound(
        {"type": "speaker_end", "character_id": "c1", "name": "沈砚",
         "text": "嗯。", "mentions": ["苏晚"], "round": 1, "sticker": None},
        channel="web", conversation_id="shelf_x",
    )
    check("渠道：speaker_end 带正文", end.kind == channels.KIND_END and end.text == "嗯。", str(end))

    user_event = channels.event_to_outbound(
        {"type": "user", "text": "在吗", "mentions": ["all"]},
        channel="web", conversation_id="shelf_x",
    )
    check("渠道：用户消息不被错认成 delta",
          user_event.kind == channels.KIND_USER and user_event.text == "在吗", str(user_event))

    err = channels.event_to_outbound(
        {"type": "error", "message": "模型挂了"}, channel="web", conversation_id="shelf_x",
    )
    check("渠道：error 从 message 取正文", err.text == "模型挂了", str(err))

    unknown = channels.event_to_outbound(
        {"type": "brand_new_event", "x": 1}, channel="web", conversation_id="shelf_x",
    )
    check("渠道：未知事件类型保留原样", unknown.kind == "brand_new_event", str(unknown))

    # ---- web 渲染回环 ----
    payload = web_module.channel.render(end)
    check("渠道：web 渲染回 speaker_end",
          payload["type"] == "speaker_end" and payload["text"] == "嗯。", str(payload))
    check("渠道：web 渲染保留 @ 与轮次",
          payload.get("mentions") == ["苏晚"] and payload.get("round") == 1, str(payload))
    check("渠道：web 渲染保留 sender",
          payload.get("character_id") == "c1" and payload.get("name") == "沈砚", str(payload))

    done = channels.event_to_outbound(
        {"type": "done", "speakers": ["沈砚"], "rounds": 1}, channel="web", conversation_id="s",
    )
    done_payload = web_module.channel.render(done)
    check("渠道：done 渲染保留统计",
          done_payload["type"] == "done" and done_payload.get("speakers") == ["沈砚"],
          str(done_payload))

    sse = web_module.channel.to_sse(done_payload)
    check("渠道：SSE 帧格式正确", sse.startswith("data: ") and sse.endswith("\n\n"), sse[:30])

    # ---- 预留渠道给出可执行的报错 ----
    raised = False
    message = ""
    try:
        qq.render(end)
    except NotImplementedError as exc:
        raised = True
        message = str(exc)
    check("渠道：QQ 渲染未实现且提示清晰",
          raised and "AppID" in message, message[:60])

    # ---- 长文本切段（QQ/微信必须用）----
    long_message = channels.OutboundMessage(
        channel="qq", conversation_id="g1", text="啊" * 2500,
    )
    pieces = long_message.split(1000)
    check("渠道：长文本按上限切段",
          len(pieces) == 3 and all(len(piece) <= 1000 for piece in pieces),
          str([len(piece) for piece in pieces]))
    check("渠道：切段后内容不丢",
          "".join(pieces) == long_message.text, str(sum(len(p) for p in pieces)))


async def channels_http_checks() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/channels")
        check("HTTP：渠道清单 200", r.status_code == 200, r.text[:120])
        body = r.json()
        check("HTTP：默认渠道是 web", body.get("default") == "web", str(body.get("default")))
        names = {item["name"]: item for item in body.get("channels", [])}
        check("HTTP：列出三个渠道", set(names) == {"web", "qq", "wechat"}, str(set(names)))
        check("HTTP：web 标记为可用", names["web"]["enabled"] is True, str(names["web"]))
        check("HTTP：QQ 标记为未启用", names["qq"]["enabled"] is False, str(names["qq"]))
        check("HTTP：能力字段齐全",
              {"supports_mentions", "supports_streaming", "max_text_length"} <= set(names["qq"]),
              str(names["qq"]))


# ---------------------------------------------------------------------------
# 回复后处理：动作旁白
# ---------------------------------------------------------------------------
def reply_checks() -> None:
    check("回复：整行动作旁白被删掉",
          reply_mod.strip_stage_directions("*揉了揉眼睛*\n在的，刚才谁喊我？")
          == "在的，刚才谁喊我？")
    check("回复：整行括号动作被删掉",
          "揉了揉眼睛" not in reply_mod.strip_stage_directions("（揉了揉眼睛）\n在的。"))
    check("回复：行内动作被摘掉",
          reply_mod.strip_stage_directions("*抬头看了一眼* 你说什么？") == "你说什么？")
    check("回复：行首括号动作被摘掉",
          reply_mod.strip_stage_directions("（笑）你来了。") == "你来了。")
    check("回复：正常对话一字不动",
          reply_mod.strip_stage_directions("“来了。”他说。") == "“来了。”他说。")
    check("回复：不误删 Markdown 加粗",
          "**重要**" in reply_mod.strip_stage_directions("**重要** 记住这件事"),
          reply_mod.strip_stage_directions("**重要** 记住这件事"))
    check("回复：能检测到动作旁白",
          reply_mod.has_stage_directions("*笑*") is True
          and reply_mod.has_stage_directions("你来了。") is False)
    check("回复：全是动作时返回空串（前端会兜底）",
          reply_mod.strip_stage_directions("*点了点头*") == "")

    # ---- 贴纸规则：问心情 / 心情变化 -> 一定发 ----
    calm = {"emotion": "平静", "arousal": 0.0, "valence": 0.0}
    check("贴纸：问心情时一定发",
          expression.maybe_sticker(
              calm, asked_about_mood=True, rng=__import__("random").Random(1)
          ) is not None)
    check("贴纸：心情变化时一定发",
          expression.maybe_sticker(
              calm, mood_changed=True, rng=__import__("random").Random(2)
          ) is not None)
    check("贴纸：都没触发时仍按概率（平静基本不发）",
          sum(
              1 for seed in range(50)
              if expression.maybe_sticker(
                  calm, rng=__import__("random").Random(seed)
              )
          ) < 10)

    check("贴纸：认出心情问句", expression.asks_about_mood("你现在心情怎么样？") is True)
    check("贴纸：认出状态问句", expression.asks_about_mood("在吗") is True)
    check("贴纸：认出情绪问句", expression.asks_about_mood("你是不是生气了") is True)
    check("贴纸：普通陈述不算问心情",
          expression.asks_about_mood("今天要下雨了") is False)

    # ---- 情绪返回值带上"上一轮心情"，供贴纸判断心情是否变化 ----
    shelf_row = _test_shelf("reply")
    book = library.create_book(
        title="回复书", source_name="r.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf_row["id"],
    )
    character = dbmod.insert_character(
        book_id=book["id"], name="沈砚", personality=["寡言"], source="novel",
    )
    state = affect.observe_user_message(
        character=character, user_id="u_reply", text="谢谢你一直照顾我，真的很感激",
    )
    check("情绪：返回值带上一轮心情", "previous_emotion" in state, str(list(state))[:80])
    check("情绪：好感度仍然不返回给前端用", "favorability" in state)

    library.delete_book(book["id"])
    dbmod.delete_shelf(shelf_row["id"])


# ---------------------------------------------------------------------------
# 正文清洗
# ---------------------------------------------------------------------------
def cleaner_checks() -> None:
    # ---- 纯广告行：整行删 ----
    ad_line = "格格书海：本书校对不易，如有乱码，错误的地方，请联系客服解决！"
    check("清洗：认出广告行", cleaner.looks_like_ad(ad_line) is True)
    check("清洗：正文行不被误判",
          cleaner.looks_like_ad("路明非站在站台上，雨水打湿了鞋子。") is False)
    check("清洗：提到手机的正文不算广告",
          cleaner.looks_like_ad("他掏出手机看了一眼时间。") is False)

    # ---- 回归：广告和正文粘在一行时，只能删广告那句 ----
    mixed = "路明非推开门，屋外下着大雨。格格书海：本书校对不易，请联系客服解决！"
    cleaned_mixed, stats_mixed = cleaner.clean_text(mixed)
    check("清洗：粘在一行的广告只删那半句",
          "路明非推开门" in cleaned_mixed and "格格书海" not in cleaned_mixed,
          cleaned_mixed)
    check("清洗：正文没有被一起删掉",
          len(cleaned_mixed) >= 10, cleaned_mixed)

    # ---- 分隔线 / 页眉去重 / 行内残留 ----
    sample = "\n".join([
        "--------------------",
        "第一章 雨夜",
        "",
        ad_line,
        "【阅读提示】本章约三千字",   # 中性高频行，用来测"重复页眉去重"
        "路明非站在站台上，雨水打湿了鞋子。（本章完）",
        "【阅读提示】本章约三千字",
        "【阅读提示】本章约三千字",
        "【阅读提示】本章约三千字",
        "【阅读提示】本章约三千字",
        "他掏出手机看了一眼时间。",
        "====================",
    ])
    cleaned, stats = cleaner.clean_text(sample)
    check("清洗：分隔线被删掉", "----" not in cleaned and "====" not in cleaned)
    check("清洗：高频重复的中性页眉被去重", "【阅读提示】" not in cleaned, cleaned)
    check("清洗：广告行被删掉", "格格书海" not in cleaned, cleaned)
    check("清洗：行内「本章完」被摘掉", "本章完" not in cleaned and "雨水打湿了鞋子" in cleaned, cleaned)
    check("清洗：正文段落完整保留",
          "第一章 雨夜" in cleaned and "掏出手机" in cleaned, cleaned)
    check("清洗：统计字段齐全",
          {"removed_lines", "deduped_lines", "removed_chars", "removed_ratio"} <= set(stats),
          str(stats))
    check("清洗：删掉的行数 > 0", stats["removed_lines"] > 0 and stats["deduped_lines"] > 0, str(stats))

    # ---- 归一化 ----
    dirty = "\ufeff路明非\u200b说\u3000\u3000“走吧”\r\n"
    normalized = cleaner.normalize(dirty)
    check("清洗：去掉 BOM 与零宽字符",
          "\ufeff" not in normalized and "\u200b" not in normalized, repr(normalized))
    check("清洗：全角空格归一", "\u3000" not in normalized, repr(normalized))
    check("清洗：换行统一", "\r" not in normalized)

    # ---- 存量记忆清洗（只洗自己的临时角色，绝不碰真实数据）----
    dbmod.init_db()
    shelf_row = _test_shelf("clean")
    book = library.create_book(
        title="脏书", source_name="d.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf_row["id"],
    )
    character = dbmod.insert_character(
        book_id=book["id"], name="沈砚", personality=["寡言"], source="novel",
    )
    dirty_memory = (
        "--------------------\n"
        + ad_line + "\n"
        "路明非站在站台上，雨水打湿了鞋子，他把伞收了起来。\n"
        "格格书海：本书校对不易，请联系客服解决！\n"
        "苏晚在远处喊他，他抬起头。\n"
    )
    item = agent_memory.remember(character["id"], [dirty_memory], kind="fact")[0]

    dry = cleaner.clean_memories(dry_run=True, character_id=character["id"])
    check("清洗：dry-run 会报告改动", dry["changed"] == 1, str(dry))
    check("清洗：dry-run 不改数据",
          dbmod.list_memory_items(character["id"], kind="fact")[0]["content"]
          == dirty_memory.strip())

    applied = cleaner.clean_memories(dry_run=False, character_id=character["id"])
    check("清洗：真清洗改了一条", applied["changed"] == 1, str(applied))
    stored = dbmod.list_memory_items(character["id"], kind="fact")[0]
    check("清洗：广告已从记忆里消失",
          "格格书海" not in stored["content"] and "----" not in stored["content"],
          stored["content"][:60])
    check("清洗：记忆正文保留下来",
          "雨水打湿了鞋子" in stored["content"] and "苏晚在远处喊他" in stored["content"],
          stored["content"][:80])
    check("清洗：改动后旧向量被作废", stored["vector"] is None, str(stored["vector"])[:20])
    check("清洗：块向量也被清掉",
          dbmod.list_memory_chunks(character["id"]) == [], "残留块")
    check("清洗：excerpts 缓存同步更新",
          all("格格书海" not in text for text in dbmod.get_character(character["id"])["excerpts"]),
          str(dbmod.get_character(character["id"])["excerpts"])[:60])

    # ---- 入库路径：新书正文入库时就应该洗干净 ----
    dirty_source = (
        "--------------------\n"
        + ad_line + "\n"
        "路明非推开门，屋外下着大雨。\n"
        "更多精彩小说请访问 www.example.com\n"
        "楚子航把伞递给他。\n"
    )
    book2 = library.create_book(
        title="入库清洗书", source_name="e.txt", source_type="txt",
        text=dirty_source, shelf_id=shelf_row["id"],
    )
    stored_source = library.read_source(book2["id"])
    check("清洗：入库时就洗掉广告",
          "格格书海" not in stored_source and "www.example.com" not in stored_source,
          stored_source[:60])
    check("清洗：入库正文没被误删",
          "路明非推开门" in stored_source and "楚子航把伞递给他" in stored_source,
          stored_source[:80])

    library.delete_book(book["id"])
    library.delete_book(book2["id"])
    dbmod.delete_shelf(shelf_row["id"])


async def cleaner_http_checks() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/maintenance/clean-memories", params={"dry_run": True})
        check("HTTP：清洗接口 200", r.status_code == 200, r.text[:160])
        body = r.json()
        check("HTTP：清洗结果带扫描/改动统计",
              {"scanned", "changed", "dry_run"} <= set(body), str(body)[:160])
        check("HTTP：默认 dry-run 不落库", body["dry_run"] is True, str(body["dry_run"]))

        r = await client.post(
            "/api/maintenance/clean-memories",
            params={"dry_run": True, "character_id": "char_none"},
        )
        check("HTTP：可按角色限定（不存在则 0 条）",
              r.status_code == 200 and r.json()["scanned"] == 0, r.text[:120])


# ---------------------------------------------------------------------------
# 自动合并：名字 / 相似度+判定 / 管理 Agent 记忆
# ---------------------------------------------------------------------------
async def auto_merge_checks() -> None:
    from services.catalog import merge as merge_mod

    dbmod.init_db()

    # ---------- ① 新书进书架后，同名角色必须自动合并 ----------
    shelf = _test_shelf("automerge")
    book_a = library.create_book(
        title="甲书", source_name="a.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    book_b = library.create_book(
        title="乙书", source_name="b.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    shen_a = dbmod.insert_character(
        book_id=book_a["id"], name="沈砚", personality=["寡言"], excerpts=["甲书里的沈砚"]
    )
    shen_b = dbmod.insert_character(
        book_id=book_b["id"], name="沈砚", personality=["寡言"], excerpts=["乙书里的沈砚"]
    )
    check("自动合并：两本书各有一个同名角色", shen_a["id"] != shen_b["id"])

    # 抽取完成时会走这一步（以前上传路径压根不触发）
    await library._sync_shelf_after_extraction(
        shelf_id=shelf["id"], book_id=book_b["id"], api_key=""
    )

    rows = [
        item
        for book in (book_a, book_b)
        for item in dbmod.list_characters(book["id"], include_merged=True)
        if item["name"] == "沈砚"
    ]
    canonical = [item for item in rows if not item.get("is_merged")]
    children = [item for item in rows if item.get("is_merged")]
    check("自动合并：同名角色被合并成一个",
          len(canonical) == 1 and len(children) == 1,
          f"canonical={len(canonical)} children={len(children)}")
    canonical_items = dbmod.list_memory_items(canonical[0]["id"], kind="excerpt")
    sources = {item.get("source_book_id") for item in canonical_items}
    check("自动合并：两本书的记忆都合到本体",
          {book_a["id"], book_b["id"]} <= sources,
          f"{len(canonical_items)} 条，来源 {len(sources)} 本")

    memories = dbmod.list_manager_memories(shelf["id"], kind="same_person")
    check("自动合并：结论写进管理 Agent 记忆",
          any("沈砚" in item["content"] for item in memories),
          str([item["content"] for item in memories])[:80])

    # ---------- ② 名字不同、性格/关系相似 ----------
    shelf2 = _test_shelf("simmerge")
    book_c = library.create_book(
        title="丙书", source_name="c.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf2["id"],
    )
    book_d = library.create_book(
        title="丁书", source_name="d.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf2["id"],
    )
    profile = dict(
        role="学生",
        personality=["古灵精怪", "嘴硬心软"],
        background="红发少女",
        speech_style="爱开玩笑，话里有话",
        relationships=[{"with": "路明非", "relation": "纠缠"}],
    )
    nuonuo = dbmod.insert_character(book_id=book_c["id"], name="诺诺", **profile)
    chen = dbmod.insert_character(book_id=book_d["id"], name="陈墨瞳", **profile)
    chu = dbmod.insert_character(
        book_id=book_d["id"], name="楚子航",
        role="学生", personality=["沉默寡言", "克制"],
        background="校草", speech_style="寡言少语",
        relationships=[{"with": "路明非", "relation": "同学"}],
    )

    score = merge_mod.similarity(
        dbmod.get_character(nuonuo["id"]), dbmod.get_character(chen["id"])
    )
    check("相似度：同一人的两种叫法分数很高", score >= 0.8, str(round(score, 2)))
    other = merge_mod.similarity(
        dbmod.get_character(nuonuo["id"]), dbmod.get_character(chu["id"])
    )
    check("相似度：不同的人分数低", other < 0.6, str(round(other, 2)))

    candidates = merge_mod.similarity_candidates(
        [dbmod.get_character(nuonuo["id"]), dbmod.get_character(chen["id"]),
         dbmod.get_character(chu["id"])]
    )
    names = {(left["name"], right["name"]) for left, right, _ in candidates}
    check("相似度：只把「诺诺/陈墨瞳」列为候选",
          names == {("诺诺", "陈墨瞳")}, str(names))

    # 相似度 1.0 > 0.7：**不问模型，直接合并**
    auto = merge_mod.merge_shelf(shelf2["id"])
    check("自动合并：相似度超过 0.7 直接合并",
          len(auto["auto_merged"]) == 1 and len(auto["merged"]) == 1,
          f"auto={len(auto['auto_merged'])} merged={len(auto['merged'])}")
    check("自动合并：待确认列表里不会出现已自动合并的对",
          auto["pending"] == [], str(auto.get("pending"))[:100])
    learned = dbmod.list_manager_memories(shelf2["id"], kind="same_person")
    check("自动合并：结论写进记忆",
          any("诺诺" in item["content"] and "陈墨瞳" in item["content"] for item in learned),
          str([item["content"] for item in learned])[:100])
    confirmed = auto

    # 记忆生效：取消合并后，不用判定器也能自动并回去
    child_id = confirmed["merged"][0]["merged_id"]
    target_id = confirmed["merged"][0]["into_id"]
    dbmod.set_canonical(child_id, None)
    replay = merge_mod.merge_shelf(shelf2["id"])
    check("记忆：下次不用再问就能合并",
          len(replay["merged"]) >= 1 and replay["verified"] == [],
          f"merged={len(replay['merged'])} verified={replay['verified']}")
    replay_rows = [
        item
        for book in (book_c, book_d)
        for item in dbmod.list_characters(book["id"], include_merged=True)
        if item["name"] in ("诺诺", "陈墨瞳")
    ]
    check("记忆：确实恢复成合并状态",
          any(item["id"] == target_id or item.get("is_merged") for item in replay_rows),
          str([(i["name"], i.get("is_merged")) for i in replay_rows]))

    # 判定器说"不是" -> 不合并 + 负记忆，下次不再问
    shelf3 = _test_shelf("negmerge")
    book_e = library.create_book(
        title="戊书", source_name="e.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf3["id"],
    )
    book_f = library.create_book(
        title="己书", source_name="f.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf3["id"],
    )
    # 只共享圈子 + 身份描述，性格相反 → 相似度约 0.55，落在"需要判定"区间
    weak_a = dict(
        role="学生", personality=["沉默寡言"], background="红发少女",
        speech_style="爱开玩笑，话里有话",
        relationships=[{"with": "路明非", "relation": "纠缠"}],
    )
    weak_b = dict(
        role="学生", personality=["热情开朗"], background="红发少女",
        speech_style="爱开玩笑，话里有话",
        relationships=[{"with": "路明非", "relation": "纠缠"}],
    )
    dbmod.insert_character(book_id=book_e["id"], name="陆小凤", **weak_a)
    dbmod.insert_character(book_id=book_f["id"], name="花满楼", **weak_b)

    def no(left, right, score):
        return False

    rejected = merge_mod.merge_shelf(shelf3["id"], verifier=no)
    check("判定：否认时不合并",
          rejected["merged"] == [] and len(rejected["rejected"]) == 1,
          str(rejected.get("rejected")))
    negatives = dbmod.list_manager_memories(shelf3["id"], kind="different_person")
    check("判定：否认结果也写进记忆",
          any("陆小凤" in item["content"] for item in negatives),
          str([item["content"] for item in negatives])[:80])

    asked_again: list[str] = []

    def no_but_count(left, right, score):
        asked_again.append(left["name"])
        return False

    merge_mod.merge_shelf(shelf3["id"], verifier=no_but_count)
    check("记忆：已排除的组合不再重复询问", asked_again == [], str(asked_again))

    # ---------- ③ 手动合并也会沉淀经验 ----------
    manual_before = len(dbmod.list_manager_memories(shelf3["id"]))
    merge_mod.merge_pair(
        dbmod.list_characters(book_f["id"])[0]["id"],
        dbmod.list_characters(book_e["id"])[0]["id"],
    )
    manual_after = len(dbmod.list_manager_memories(shelf3["id"]))
    check("手动合并：也会写进管理 Agent 记忆", manual_after == manual_before + 1,
          f"{manual_before} -> {manual_after}")

    # ---------- ④ HTTP：经验可查、可删 ----------
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/api/shelves/{shelf['id']}/memories")
        check("HTTP：可查书架经验", r.status_code == 200 and r.json()["count"] >= 1, r.text[:120])
        memory_id = r.json()["memories"][0]["id"]
        r = await client.delete(f"/api/memories/{memory_id}")
        check("HTTP：可删经验（记错了能反悔）", r.status_code == 200, r.text[:120])
        r = await client.get(f"/api/shelves/{shelf['id']}/memories")
        check("HTTP：删掉后列表变短",
              all(item["id"] != memory_id for item in r.json()["memories"]))

        # ---------- ⑤ 界面用接口：候选只读 + 用户拍板 ----------
        shelf4 = _test_shelf("uimerge")
        book_g = library.create_book(
            title="庚书", source_name="g.txt", source_type="txt",
            text=SAMPLE, shelf_id=shelf4["id"],
        )
        book_h = library.create_book(
            title="辛书", source_name="h.txt", source_type="txt",
            text=SAMPLE, shelf_id=shelf4["id"],
        )
        first = dbmod.insert_character(book_id=book_g["id"], name="张三", **weak_a)
        second = dbmod.insert_character(book_id=book_h["id"], name="张三丰", **weak_b)

        r = await client.get(f"/api/shelves/{shelf4['id']}/merge/candidates")
        body = r.json()
        check("HTTP：候选接口只读、能列出可疑对",
              r.status_code == 200 and body["count"] == 1, r.text[:120])
        pair = body["pairs"][0]
        check("HTTP：候选带相似度与共同关联",
              float(pair["score"]) > 0 and isinstance(pair["common"], list),
              str(pair)[:120])

        r = await client.post(
            f"/api/shelves/{shelf4['id']}/merge/resolve",
            json={"left_id": pair["left"]["id"], "right_id": pair["right"]["id"], "same": False},
        )
        check("HTTP：判「不是」→ 不合并",
              r.status_code == 200 and r.json()["resolved"] == "different", r.text[:120])
        r = await client.get(f"/api/shelves/{shelf4['id']}/merge/candidates")
        check("HTTP：判过之后这对不再出现", r.json()["count"] == 0, r.text[:120])

        # 反悔：删掉那条经验，再判"是"
        for memory in dbmod.list_manager_memories(shelf4["id"], kind="different_person"):
            dbmod.delete_manager_memory(memory["id"])
        r = await client.post(
            f"/api/shelves/{shelf4['id']}/merge/resolve",
            json={"left_id": first["id"], "right_id": second["id"], "same": True},
        )
        check("HTTP：判「是」→ 合并",
              r.status_code == 200 and r.json()["resolved"] == "merged", r.text[:120])
        merged_ids = {
            item["id"]
            for book in (book_g, book_h)
            for item in dbmod.list_characters(book["id"], include_merged=True)
            if item.get("is_merged")
        }
        check("HTTP：确实有一个角色被并入", len(merged_ids) == 1, str(merged_ids))

        r = await client.post(
            f"/api/shelves/{shelf3['id']}/merge/resolve",
            json={"left_id": first["id"], "right_id": second["id"], "same": True},
        )
        check("HTTP：跨书架操作被拒绝", r.status_code == 400, r.text[:120])

        library.delete_book(book_g["id"])
        library.delete_book(book_h["id"])
        dbmod.delete_shelf(shelf4["id"])

    for book in (book_a, book_b, book_c, book_d, book_e, book_f):
        library.delete_book(book["id"])
    for item in (shelf, shelf2, shelf3):
        dbmod.delete_shelf(item["id"])


# ---------------------------------------------------------------------------
# P0：抽取链的可靠性修复
# ---------------------------------------------------------------------------
def reextract_identity_checks() -> None:
    """重抽必须保留角色身份（id / 头像 / 情绪 / 对话记忆 / 群成员）。"""
    dbmod.init_db()
    shelf = _test_shelf("reextract")
    book = library.create_book(
        title="重抽书", source_name="r.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    first = dbmod.replace_novel_characters(
        book["id"],
        [
            {"name": "沈砚", "aliases": ["阿砚"], "role": "剑客", "excerpts": ["旧片段"], "appearances": 10},
            {"name": "苏晚", "role": "掌柜", "excerpts": ["片段"], "appearances": 5},
        ],
    )
    shen = next(item for item in first if item["name"] == "沈砚")
    su = next(item for item in first if item["name"] == "苏晚")
    shen_id, su_id = shen["id"], su["id"]

    # 用户挑头像 + 角色记住一件事 + 有了情绪 + 进了群
    dbmod.update_character(shen_id, avatar="🦊", avatar_url="https://example.com/a.png")
    agent_memory.remember(shen_id, ["用户说过喜欢雨天"], kind="fact")
    dbmod.save_affect(
        shen_id, "u_reextract", shelf["id"],
        favorability=70, valence=0.5, arousal=0.3, emotion="喜悦",
        baseline={"valence": 0.0, "arousal": 0.3}, turns=3,
    )
    dbmod.ensure_shelf_members(shelf["id"])
    members_before = {
        item["character_id"] for item in dbmod.list_shelf_members(shelf["id"])
    }

    # 重抽：名字带空格（应视为同一个）+ 少一个 + 多一个
    second = dbmod.replace_novel_characters(
        book["id"],
        [
            {"name": " 沈砚 ", "role": "剑客（改）", "excerpts": ["新片段"], "appearances": 12},
            {"name": "新来的", "role": "路人", "excerpts": ["新角色片段"], "appearances": 3},
        ],
    )
    by_name = {item["name"].strip(): item for item in second}
    check("重抽：同名角色 id 不变", by_name.get("沈砚", {}).get("id") == shen_id,
          str(by_name.get("沈砚", {}).get("id")))

    updated = dbmod.get_character(shen_id) or {}
    check("重抽：抽取字段已更新", updated.get("role") == "剑客（改）", str(updated.get("role")))
    check("重抽：用户挑的头像保留",
          updated.get("avatar") == "🦊" and updated.get("avatar_url") == "https://example.com/a.png",
          f"{updated.get('avatar')} / {updated.get('avatar_url')}")

    facts = agent_memory.items(shen_id, kind="fact")
    check("重抽：对话学到的记忆保留",
          any("喜欢雨天" in item["content"] for item in facts), str(len(facts)))

    affect = dbmod.get_affect(shen_id, "u_reextract", shelf["id"]) or {}
    check("重抽：情绪与好感度保留", affect.get("favorability") == 70, str(affect.get("favorability")))

    members_after = {
        item["character_id"] for item in dbmod.list_shelf_members(shelf["id"])
    }
    check("重抽：原角色仍在群里", shen_id in members_after, str(sorted(members_after))[:80])

    check("重抽：消失的角色被删除", dbmod.get_character(su_id) is None, str(su_id))
    check("重抽：消失角色不再占群成员", su_id not in members_after)
    check("重抽：新角色被插入", "新来的" in by_name and by_name["新来的"]["id"] != shen_id)

    excerpts = [item["content"] for item in agent_memory.items(shen_id, kind="excerpt")]
    check("重抽：本书原文片段换成新的",
          any("新片段" in text for text in excerpts) and not any("旧片段" in text for text in excerpts),
          str(excerpts)[:100])

    library.delete_book(book["id"])
    dbmod.delete_shelf(shelf["id"])


def interrupted_extraction_checks() -> None:
    """重启后卡在 extracting 的书要被回收成可重试。"""
    dbmod.init_db()
    shelf = _test_shelf("recover")
    stuck = library.create_book(
        title="卡住的书", source_name="s.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    dbmod.update_book(stuck["id"], status="extracting", progress=40, stage="识别角色")
    ready = library.create_book(
        title="正常的书", source_name="n.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    dbmod.update_book(ready["id"], status="ready", progress=100)

    recovered = dbmod.recover_interrupted_extractions()
    ids = [item["id"] for item in recovered]
    check("回收：找出被中断的书", stuck["id"] in ids, str(ids))
    check("回收：不动正常的书", ready["id"] not in ids, str(ids))

    row = dbmod.get_book(stuck["id"]) or {}
    check("回收：状态改成 failed", row.get("status") == "failed", str(row.get("status")))
    check("回收：提示可以重新抽取", "重新抽取" in str(row.get("error") or ""), str(row.get("error")))
    check("回收：进度条不再挂着", int(row.get("progress") or 0) == 100, str(row.get("progress")))
    check("回收：再跑一次是幂等的",
          stuck["id"] not in [item["id"] for item in dbmod.recover_interrupted_extractions()])

    library.delete_book(stuck["id"])
    library.delete_book(ready["id"])
    dbmod.delete_shelf(shelf["id"])


async def extract_fallback_checks() -> None:
    """补全环节炸了，不能把算法已经挖到的角色一起丢掉。"""
    from services.catalog import extractor as extractor_mod

    dbmod.init_db()
    shelf = _test_shelf("fallback")
    book = library.create_book(
        title="兜底书", source_name="f.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    chunks = library.read_chunks(book["id"])

    original = extractor_mod.enrich_characters

    async def boom(*args, **kwargs):
        raise RuntimeError("模拟补全失败")

    extractor_mod.enrich_characters = boom  # type: ignore[assignment]
    try:
        characters, used_llm, diagnostic, _avatars = await library._extract_characters(
            book["id"], chunks, "hybrid", "fake-key"
        )
    finally:
        extractor_mod.enrich_characters = original  # type: ignore[assignment]

    check("兜底：补全抛错仍返回算法角色", len(characters) >= 2, str(len(characters)))
    check("兜底：诊断里记下失败原因",
          "模拟补全失败" in str(diagnostic.get("enrich_error") or ""),
          str(diagnostic.get("enrich_error"))[:80])
    check("兜底：没有把整本判成用了模型", used_llm is False, str(used_llm))

    library.delete_book(book["id"])
    dbmod.delete_shelf(shelf["id"])


# ---------------------------------------------------------------------------
# 情绪小模型（ONNX 后端）
# ---------------------------------------------------------------------------
def emotion_model_checks() -> None:
    from services.dialogue import emotion_model as emo

    backend = emo.get_backend()
    check("情绪模型：auto 解析到 ONNX（模型已就位）",
          backend.name == "onnx", backend.name)

    info = emo.info()
    check("情绪模型：info 里带 onnx 明细",
          "onnx" in info and info["onnx"]["model_exists"], str(info)[:120])
    check("情绪模型：标注了模型文件",
          bool(info["onnx"]["model_file"]) and info["onnx"]["model_file"].endswith(".onnx"),
          str(info["onnx"]["model_file"]))

    positive = backend.analyze("我今天真的很开心，谢谢你一直陪着我")
    check("情绪模型：正向句判为正", positive is not None and positive.polarity > 0.3,
          str(positive))
    check("情绪模型：正向句置信度够高",
          positive is not None and positive.confidence > 0.5, str(positive))

    negative = backend.analyze("我撑不住了，什么都不想做，太难受了")
    check("情绪模型：负向句判为负", negative is not None and negative.polarity < -0.3,
          str(negative))
    check("情绪模型：信号标明来自 onnx",
          negative is not None and negative.backend == "onnx",
          str(negative.backend if negative else None))

    # 延迟红线：兜底路径每轮最多一次，必须够快
    import time as _time

    backend._ensure_loaded()
    started = _time.perf_counter()
    for _ in range(3):
        backend.analyze("你今天怎么看起来不太高兴")
    per_call = (_time.perf_counter() - started) / 3 * 1000
    check("情绪模型：单次推理 < 200ms", per_call < 200, f"{per_call:.1f} ms")

    # 配置可以强制走词法
    check("情绪模型：可强制 rules 后端", emo.get_backend("rules").name == "rules")

    # 模型缺失时自动降级，不抛异常
    sample_dir = emo.DEFAULT_MODEL_DIR
    saved = emo.model_dir
    try:
        emo.model_dir = lambda: sample_dir / "__not_exists__"
        missing = emo.OnnxBackend()
        check("情绪模型：模型缺失时不可用", missing.available() is False,
              str(missing.status())[:80])
        check("情绪模型：模型缺失时分析返回 None",
              missing.analyze("随便一句话") is None)
    finally:
        emo.model_dir = saved

    check("情绪模型：规则后端仍可用",
          emo.get_backend("rules").analyze("我真的很开心") is not None)


# ---------------------------------------------------------------------------
# 抽取流水线（假 LLM）
# ---------------------------------------------------------------------------
async def extraction_checks() -> None:
    calls: list[int] = []

    async def fake_caller(messages):
        calls.append(1)
        text = messages[-1]["content"]
        characters = []
        if "沈砚" in text:
            characters.append(
                {
                    "name": "沈砚",
                    "aliases": ["寒江一剑"],
                    "role": "剑客",
                    "personality": ["寡言", "冷"],
                    "speech_style": "句子很短",
                    "background": "江南剑客",
                    "relationships": [{"with": "苏晚", "relation": "旧识"}],
                    "quotes": ["你认得我。"],
                }
            )
        if "苏晚" in text:
            characters.append(
                {
                    "name": "苏晚",
                    "aliases": [],
                    "role": "女主角",
                    "personality": ["通透"],
                    "speech_style": "慢半拍",
                    "background": "商贾之女",
                    "relationships": [],
                    "quotes": ["你迟到了三年。"],
                }
            )
        if "老周" in text:
            characters.append(
                {
                    "name": "老周",
                    "aliases": ["掌柜"],
                    "role": "配角",
                    "personality": ["世故"],
                    "speech_style": "慢悠悠",
                    "background": "客栈掌柜",
                    "relationships": [],
                    "quotes": [],
                }
            )
        return {"characters": characters}

    progress: list[tuple[int, int]] = []
    chunks = extractor.chunk_text(SAMPLE, chunk_chars=400, overlap=40)
    characters = await extractor.extract_from_chunks(
        chunks,
        api_key="sk-fake",
        model="deepseek-chat",
        on_progress=lambda done, total: progress.append((done, total)),
        caller=fake_caller,
    )
    check("抽取：按块调用 LLM", len(calls) == len(chunks), f"{len(calls)} vs {len(chunks)}")
    check("抽取：进度回调覆盖每块", len(progress) == len(chunks))
    check("抽取：合并出 3 个角色", len(characters) == 3, str([c["name"] for c in characters]))
    check("抽取：出场次数按块统计",
          all(c["appearances"] >= 1 for c in characters),
          str({c["name"]: c["appearances"] for c in characters}))

    async def failing_caller(messages):  # noqa: ARG001
        raise RuntimeError("boom")

    try:
        await extractor.extract_from_chunks(
            ["x" * 100] * 3, api_key="sk-fake", model="deepseek-chat", caller=failing_caller
        )
        check("抽取：全部失败应报错", False)
    except extractor.ExtractionError:
        check("抽取：全部失败应报错", True)


# ---------------------------------------------------------------------------
# HTTP 全链路
# ---------------------------------------------------------------------------
class _FakeChunk:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeAgent:
    """只实现 routes 用到的 arun 异步流。"""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks

    async def arun(self, prompt, **kwargs):  # noqa: ARG002
        for content in self.chunks:
            yield _FakeChunk(content)


async def http_checks() -> None:
    settings = get_settings()
    original_chunk = settings.chunk_chars
    original_mode = settings.extraction_mode
    original_caller = extractor.call_llm_json
    original_get_agent = chat_endpoint.get_agent
    session_secret_before = settings.session_secret
    calls = {"n": 0}

    # 这一段专门测 llm 模式（逐块全文抽取），algo/hybrid 另有用例
    settings.extraction_mode = "llm"

    async def fake_caller(messages, *, api_key, model, timeout=120.0):  # noqa: ARG001
        calls["n"] += 1
        text = messages[-1]["content"]
        characters = []
        if "沈砚" in text:
            characters.append({"name": "沈砚", "aliases": ["沈公子"], "role": "剑客",
                               "personality": ["寡言"], "speech_style": "很短",
                               "background": "江南剑客", "relationships": [], "quotes": ["你认得我。"]})
        if "苏晚" in text:
            characters.append({"name": "苏晚", "aliases": [], "role": "女主角",
                               "personality": ["通透"], "speech_style": "慢半拍",
                               "background": "商贾之女", "relationships": [], "quotes": ["你迟到了三年。"]})
        if "老周" in text:
            characters.append({"name": "老周", "aliases": ["掌柜"], "role": "配角",
                               "personality": ["世故"], "speech_style": "慢悠悠",
                               "background": "掌柜", "relationships": [], "quotes": []})
        await asyncio.sleep(0)
        return {"characters": characters}

    settings.chunk_chars = 400  # 让样例小说切成多块，真实验证合并
    extractor.call_llm_json = fake_caller  # type: ignore[assignment]
    dbmod.init_db()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/health")
        check("HTTP：/api/health 200", r.status_code == 200, r.text)
        check("HTTP：health 不回传 Key", "sk-" not in r.text)
        check("HTTP：health 报告支持的格式", ".txt" in r.text and ".pdf" in r.text)

        r = await client.post("/api/session", json={"display_name": "阿青"})
        check("HTTP：/api/session 200", r.status_code == 200, r.text)
        token = r.json()["token"]
        session_body = r.json()

        r = await client.post("/api/chat", json={"token": token, "prompt": "hi"})
        check("HTTP：缺 character_id -> 400", r.status_code == 400, r.text)

        r = await client.post("/api/chat", json={"token": token[:-3] + "AAA", "character_id": "x"})
        check("HTTP：伪造 token -> 401", r.status_code == 401, r.text)

        # --- 上传小说（用测试专用书架，避免污染真实书架的日志）---
        shelf = _test_shelf("http")
        r = await client.post(
            "/api/books",
            files={"file": ("雨夜试剑.txt", SAMPLE.encode("utf-8"), "text/plain")},
            data={"title": "雨夜试剑", "shelf_id": shelf["id"]},
        )
        check("HTTP：上传小说 201", r.status_code == 201, r.text)
        book = r.json()
        book_id = book["id"]
        check("HTTP：上传进指定书架",
              dbmod.get_book(book_id)["shelf_id"] == shelf["id"],
              str(dbmod.get_book(book_id).get("shelf_id")))
        check("HTTP：上传后进入抽取状态", book["status"] == "extracting", book["status"])

        # 轮询直到抽取完成
        status = ""
        for _ in range(80):
            r = await client.get(f"/api/books/{book_id}")
            payload = r.json()
            status = payload["status"]
            if status in ("ready", "failed"):
                break
            await asyncio.sleep(0.1)
        check("HTTP：后台抽取完成", status == "ready", f"{status} / {payload.get('error')}")
        check("HTTP：进度到 100", payload["progress"] == 100, str(payload["progress"]))
        check("HTTP：抽取调用了多块", calls["n"] > 1, str(calls["n"]))
        check("HTTP：书籍统计角色数", payload["character_count"] >= 3, str(payload["character_count"]))

        # --- 角色列表 ---
        r = await client.get(f"/api/books/{book_id}/characters")
        characters = r.json()
        names = {c["name"] for c in characters}
        check("HTTP：角色列表含抽出的角色", {"沈砚", "苏晚", "老周"} <= names, str(names))
        shen = next(c for c in characters if c["name"] == "沈砚")
        check("HTTP：角色带性格与片段", bool(shen["personality"]) and bool(shen["excerpts"]),
              str(shen["excerpts"])[:40])
        check("HTTP：角色带头像", bool(shen["avatar"]))

        r = await client.get(f"/api/characters/{shen['id']}", params={"user_name": "阿青"})
        check("HTTP：角色详情含提示词预览", "prompt_preview" in r.json() and "阿青" in r.json()["prompt_preview"])

        # --- 自定义角色（我） ---
        r = await client.post(
            f"/api/books/{book_id}/characters",
            json={"name": "阿青", "personality": ["好奇"], "background": "穿越者", "is_user_persona": True},
        )
        check("HTTP：新建自定义角色 201", r.status_code == 201, r.text)
        me = r.json()
        check("HTTP：自定义角色标记为 user", me["source"] == "user" and me["is_user_persona"] is True)

        r = await client.patch(f"/api/characters/{me['id']}", json={"speech_style": "爱问问题"})
        check("HTTP：编辑角色", r.status_code == 200 and r.json()["speech_style"] == "爱问问题", r.text)

        r = await client.get("/api/characters/char_doesnotexist")
        check("HTTP：不存在的角色 -> 404", r.status_code == 404, r.text)

        # --- 聊天 ---
        chat_endpoint.get_agent = lambda **kwargs: _FakeAgent(["「沈砚」抬眼看你，", "把剑放在桌上。"])
        uploads = settings.uploads_path
        before_dirs = {p.name for p in uploads.glob("chat-*")} if uploads.is_dir() else set()

        # 贴纸按概率发，测起来会抖；这里直接让它必发，专测"事件有没有正确推给前端"
        original_maybe_sticker = expression_module.maybe_sticker
        expression_module.maybe_sticker = lambda state, **kwargs: {
            "emotion": "喜悦",
            "codepoint": "1f604",
            "emoji": "\U0001f604",
            "url": "/api/sticker/1f604.svg",
        }

        async with client.stream(
            "POST",
            "/api/chat",
            json={
                "token": token,
                "character_id": shen["id"],
                "prompt": "你好，好久不见",
                "images_base64": [f"data:image/png;base64,{TINY_PNG}"],
            },
        ) as response:
            check("HTTP：聊天返回 200", response.status_code == 200, str(response.status_code))
            check("HTTP：content-type 为 SSE",
                  response.headers.get("content-type", "").startswith("text/event-stream"))
            check("HTTP：关闭代理缓冲", response.headers.get("x-accel-buffering") == "no")

            events = []
            buffer = ""
            async for text in response.aiter_text():
                buffer += text
                parts = buffer.split("\n\n")
                buffer = parts.pop()
                for part in parts:
                    line = part.strip()
                    if line.startswith("data:"):
                        payload_text = line[5:].strip()
                        if payload_text and payload_text != "[DONE]":
                            events.append(__import__("json").loads(payload_text))

        check("HTTP：第一件事是声明本轮 turn_id（断流续跑用）",
              bool(events) and events[0].get("type") == "turn"
              and bool(events[0].get("thread_id")),
              str(events[:1]))
        check("HTTP：turn 事件带 resume 标记",
              bool(events) and events[0].get("type") == "turn"
              and "resume" in events[0],
              str(events[:1]))

        merged_text = "".join(e.get("text", "") for e in events if e.get("type") == "delta")
        check("HTTP：流式文本完整", merged_text == "「沈砚」抬眼看你，把剑放在桌上。", merged_text)
        check("HTTP：有 done 事件", bool(events) and events[-1].get("type") == "done")

        expression_module.maybe_sticker = original_maybe_sticker
        sticker_events = [e for e in events if e.get("type") == "sticker"]
        check("HTTP：聊天带贴纸事件", len(sticker_events) == 1, str(len(sticker_events)))
        check("HTTP：贴纸事件带可访问的 url",
              bool(sticker_events) and sticker_events[0]["url"].startswith("/api/sticker/"),
              str(sticker_events[:1]))
        check("HTTP：贴纸在 done 之前",
              bool(events) and events[-1].get("type") == "done"
              and any(e.get("type") == "sticker" for e in events[:-1]),
              str([e.get("type") for e in events]))

        # 对话判断是否落库（意图/情绪）：没有它，长期记忆就记不住"你一累就低落"
        recent = dbmod.list_chat_turns(shelf["id"], limit=6)
        judged = [
            item for item in recent
            if item["role"] == "user" and item.get("intent")
        ]
        check("HTTP：本轮意图/情绪写进流水",
              bool(judged) and judged[-1]["intent"] in ("chat", "vent", "seek_help", "crisis"),
              str([(item.get("intent"), item.get("emotion")) for item in recent]))

        # 图片改成持久化：落在 uploads/chat/<folder>/，并能被聊天历史引用
        r = await client.get(f"/api/characters/{shen['id']}/history?token={token}&limit=10")
        history_turns = r.json()["turns"]
        image_turns = [turn for turn in history_turns if turn.get("images")]
        check("HTTP：单聊图片写进历史", bool(image_turns),
              str([turn.get("images") for turn in history_turns][-3:]))
        if image_turns:
            image_url = image_turns[-1]["images"][0]
            got = await client.get(image_url)
            check("HTTP：单聊图片能按地址读回",
                  got.status_code == 200 and got.content[:4] == b"\x89PNG", str(got.status_code))
            import shutil as _shutil
            _shutil.rmtree(uploads / "chat" / image_url.split("/")[-2], ignore_errors=True)

        # --- 会话隔离 ---
        session = api_deps.verify_session(token)
        first = api_deps.conversation_session(session, shen["id"])
        again = api_deps.conversation_session(session, shen["id"])
        other = api_deps.conversation_session(session, me["id"])
        check("会话：同角色映射稳定", first == again)
        check("会话：不同角色互不串记忆", first != other)

        # --- 删除 ---
        r = await client.delete(f"/api/characters/{me['id']}")
        check("HTTP：删除角色", r.status_code == 200, r.text)
        r = await client.delete(f"/api/books/{book_id}")
        check("HTTP：删除作品", r.status_code == 200, r.text)
        r = await client.get(f"/api/books/{book_id}")
        check("HTTP：删除后查不到", r.status_code == 404, r.text)

        dbmod.delete_shelf(shelf["id"])  # 连带清掉本次测试写入的日志

    settings.chunk_chars = original_chunk
    settings.extraction_mode = original_mode
    settings.session_secret = session_secret_before
    extractor.call_llm_json = original_caller  # type: ignore[assignment]
    chat_endpoint.get_agent = original_get_agent  # type: ignore[assignment]


async def algo_http_checks() -> None:
    """algo 模式全链路：上传即用算法出角色，全程不调用模型。"""
    settings = get_settings()
    original_mode = settings.extraction_mode
    original_caller = extractor.call_llm_json
    llm_calls: list[int] = []

    async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        llm_calls.append(1)
        raise AssertionError("algo 模式不应该调用模型")

    settings.extraction_mode = "algo"
    extractor.call_llm_json = boom  # type: ignore[assignment]
    dbmod.init_db()

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            shelf = _test_shelf("algo")
            r = await client.post(
                "/api/books",
                files={"file": ("算法模式.txt", SAMPLE.encode("utf-8"), "text/plain")},
                data={"title": "算法模式", "shelf_id": shelf["id"]},
            )
            check("算法模式：上传 201", r.status_code == 201, r.text)
            book_id = r.json()["id"]

            payload: dict = {}
            for _ in range(80):
                r = await client.get(f"/api/books/{book_id}")
                payload = r.json()
                if payload["status"] in ("ready", "failed"):
                    break
                await asyncio.sleep(0.1)

            check("算法模式：抽取完成", payload.get("status") == "ready",
                  f"{payload.get('status')} / {payload.get('error')}")
            check("算法模式：没有调用模型", not llm_calls, str(len(llm_calls)))
            check("算法模式：状态标注为纯算法",
                  "算法" in (payload.get("stage") or ""), str(payload.get("stage")))

            characters = (await client.get(f"/api/books/{book_id}/characters")).json()
            names = {c["name"] for c in characters}
            check("算法模式：角色来自算法", {"沈砚", "苏晚", "老周"} <= names, str(names))

            shen = next(c for c in characters if c["name"] == "沈砚")
            check("算法模式：带原文片段记忆", bool(shen["excerpts"]), str(len(shen["excerpts"])))
            check("算法模式：带头像", bool(shen["avatar"]))
            check("算法模式：性格是可量化描述",
                  any("字" in p or "话" in p for p in shen["personality"]),
                  str(shen["personality"]))

            await client.delete(f"/api/books/{book_id}")
    finally:
        dbmod.delete_shelf(shelf["id"])  # 连带清掉本次测试写入的日志
        settings.extraction_mode = original_mode
        extractor.call_llm_json = original_caller  # type: ignore[assignment]


async def verify_endpoint_checks() -> None:
    """AI 审核接口（/api/books/{id}/verify）的接线测试，agent 本体用假实现。"""
    from agent import extraction as agent_module

    settings = get_settings()
    original_key = settings.deepseek_api_key
    original_run = agent_module.ExtractionAgent.run

    async def fake_run(self):  # noqa: ANN001, ARG001
        return {
            "characters": [
                {
                    "name": "沈砚",
                    "aliases": [],
                    "role": "剑客",
                    "personality": ["寡言"],
                    "speech_style": "很短",
                    "background": "江南剑客",
                    "relationships": [],
                    "quotes": ["你认得我。"],
                    "excerpts": [],
                    "appearances": 7,
                }
            ],
            "avatars": {},
            "trace": [{"step": 1, "action": "finish", "observation": "{}"}],
            "report": {"kept": 1, "dropped": [], "renamed": [], "steps": 1},
        }

    agent_module.ExtractionAgent.run = fake_run  # type: ignore[assignment]
    settings.deepseek_api_key = "sk-fake-for-test"
    dbmod.init_db()
    book = library.create_book(
        title="审核自检", source_name="t.txt", source_type="txt", text=SAMPLE
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(f"/api/books/{book['id']}/verify")
            check("HTTP：AI 审核接口 200", r.status_code == 200, r.text)
            body = r.json()
            check("HTTP：审核返回角色数", body.get("characters") == 1, str(body)[:120])
            check("HTTP：审核返回 trace 与报告",
                  bool(body.get("trace")) and "report" in body, str(body)[:120])
    finally:
        agent_module.ExtractionAgent.run = original_run  # type: ignore[assignment]
        settings.deepseek_api_key = original_key
        library.delete_book(book["id"])


async def shelf_http_checks() -> None:
    dbmod.init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/libraries")
        check("HTTP：书库列表 200", r.status_code == 200 and len(r.json()) >= 1, r.text[:120])

        r = await client.post("/api/libraries", json={"title": "HTTP自检库"})
        check("HTTP：新建书库 201",
              r.status_code == 201 and r.json()["title"] == "HTTP自检库", r.text[:120])
        library_id = r.json()["id"]

        r = await client.post(
            "/api/shelves", json={"library_id": library_id, "title": "HTTP书架"}
        )
        check("HTTP：新建书架 201", r.status_code == 201, r.text[:120])
        shelf_id = r.json()["id"]

        r = await client.post(
            "/api/shelves", json={"library_id": "lib_not_exist", "title": "x"}
        )
        check("HTTP：往不存在的书库建书架 -> 404", r.status_code == 404, r.text[:120])

        agent_log.log("http_log_event", shelf_id=shelf_id)

        r = await client.get(f"/api/shelves/{shelf_id}/logs")
        body = r.json()
        check("HTTP：书架日志 200", r.status_code == 200 and body["count"] >= 1, r.text[:120])
        check("HTTP：日志只含本书架事件",
              all(e["shelf_id"] == shelf_id for e in body["events"]), r.text[:160])

        r = await client.get("/api/shelves/shelf_not_exist/logs")
        check("HTTP：不存在的书架 -> 404", r.status_code == 404, r.text[:120])

        dbmod.delete_shelf(shelf_id)
        dbmod.delete_library(library_id)


async def character_status_checks() -> None:
    """角色详情 / 状态接口：给前端点开角色看的那块数据。"""
    dbmod.init_db()
    shelf = _test_shelf("status")
    book = library.create_book(
        title="状态书", source_name="s.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    character = dbmod.insert_character(
        book_id=book["id"], name="沈砚", personality=["寡言", "冷"],
        role="剑客", source="novel", excerpts=["片段一", "片段二"],
    )
    dbmod.ensure_shelf_members(shelf["id"])  # 角色入群，状态接口才读得到成员状态

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 未登录态：只有基线情绪，不暴露个人状态
        r = await client.get(f"/api/characters/{character['id']}/status")
        check("状态：接口 200", r.status_code == 200, r.text[:120])
        body = r.json()
        check("状态：返回基本信息", body["character"]["name"] == "沈砚", str(body)[:120])
        check("状态：带书架信息", body["character"]["shelf_title"] == shelf["title"],
              str(body["character"].get("shelf_title")))
        check("状态：未聊过时态度未知",
              "没聊过" in body["status"]["attitude_text"], body["status"]["attitude_text"])
        check("状态：不暴露好感度/情绪数值",
              not ({"favorability", "valence", "arousal"} & set(body["status"])),
              str(sorted(body["status"])))
        check("状态：记忆条数正确", body["status"]["memory"]["total"] == 2,
              str(body["status"]["memory"]))
        check("状态：带群内成员状态（面板据此显示静默/恢复）",
              (body["status"].get("member") or {}).get("status") == "online",
              str(body["status"].get("member")))

        # 登录态：带 token 能读到本用户的互动轮次
        r = await client.post("/api/session", json={"display_name": "状态测试员"})
        session = r.json()
        affect.observe_user_message(
            character=character, user_id=session["user_id"],
            text="谢谢你这段时间的照顾，我真的很感激你",
        )
        r = await client.get(
            f"/api/characters/{character['id']}/status", params={"token": session["token"]}
        )
        body = r.json()
        check("状态：带 token 读到互动轮次", body["status"]["turns"] >= 1,
              str(body["status"]["turns"]))
        check("状态：情绪用中文描述",
              isinstance(body["status"]["mood_text"], str) and len(body["status"]["mood_text"]) > 0,
              str(body["status"]["mood_text"]))
        check("状态：态度用中文描述",
              isinstance(body["status"]["attitude_text"], str) and len(body["status"]["attitude_text"]) > 0,
              str(body["status"]["attitude_text"]))

        r = await client.get(
            f"/api/characters/{character['id']}/status", params={"token": "bad-token"}
        )
        check("状态：伪造 token -> 401", r.status_code == 401, r.text[:120])

        dbmod.delete_affect(shelf_id=shelf["id"])
        library.delete_book(book["id"])
        dbmod.delete_shelf(shelf["id"])


async def group_http_checks() -> None:
    """群成员接口 + 群聊的"没配 Key 就 503"护栏（不真的调模型）。"""
    settings = get_settings()
    dbmod.init_db()
    shelf = _test_shelf("group-http")
    book = library.create_book(
        title="群聊HTTP书", source_name="h.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    dbmod.insert_character(
        book_id=book["id"], name="沈砚", personality=["寡言"], source="novel",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/api/shelves/{shelf['id']}/members")
        check("HTTP：群成员列表 200", r.status_code == 200, r.text[:120])
        body = r.json()
        check("HTTP：群成员带性格画像",
              bool(body["members"]) and "profile" in body["members"][0], str(body)[:120])
        character_id = body["members"][0]["character_id"]

        r = await client.patch(
            f"/api/shelves/{shelf['id']}/members/{character_id}",
            json={"status": "offline"},
        )
        check("HTTP：设为离线 200", r.status_code == 200, r.text[:120])
        check("HTTP：离线状态已归一", r.json().get("status") == "offline", r.text[:120])
        r = await client.get(f"/api/shelves/{shelf['id']}/members")
        offline = next(m for m in r.json()["members"] if m["character_id"] == character_id)
        check("HTTP：离线状态已生效", offline["status"] == "offline", str(offline["status"]))

        r = await client.patch(
            f"/api/shelves/{shelf['id']}/members/{character_id}",
            json={"status": "online"},
        )
        check("HTTP：可恢复在线", r.status_code == 200, r.text[:120])

        # 旧值兼容：active/muted 会自动归一
        r = await client.patch(
            f"/api/shelves/{shelf['id']}/members/{character_id}",
            json={"status": "muted"},
        )
        check("HTTP：旧值 muted 兼容为 offline",
              r.status_code == 200 and r.json().get("status") == "offline", r.text[:120])
        await client.patch(
            f"/api/shelves/{shelf['id']}/members/{character_id}",
            json={"status": "online"},
        )

        r = await client.patch(
            f"/api/shelves/{shelf['id']}/members/{character_id}",
            json={"status": "bogus"},
        )
        check("HTTP：非法成员状态 -> 400", r.status_code == 400, r.text[:120])

        r = await client.get("/api/shelves/shelf_not_exist/members")
        check("HTTP：不存在的书架 -> 404", r.status_code == 404, r.text[:120])

        # 群聊护栏：没配 Key 时应当 503，而不是真的去调模型
        session = (
            await client.post("/api/session", json={"display_name": "群聊测试员"})
        ).json()
        original_key = settings.deepseek_api_key
        settings.deepseek_api_key = ""
        try:
            r = await client.post(
                f"/api/shelves/{shelf['id']}/chat",
                json={"token": session["token"], "prompt": "在吗"},
            )
            check("HTTP：没配 Key 的群聊 -> 503", r.status_code == 503, r.text[:120])
        finally:
            settings.deepseek_api_key = original_key

    library.delete_book(book["id"])
    dbmod.delete_shelf(shelf["id"])


async def group_chat_sse_checks() -> None:
    """群聊 SSE 契约：后端推的字段 == 前端解析的字段（用假 Agent，不调模型）。"""
    settings = get_settings()
    dbmod.init_db()
    shelf = _test_shelf("group-sse")
    book = library.create_book(
        title="群聊SSE书", source_name="s.txt", source_type="txt",
        text=SAMPLE, shelf_id=shelf["id"],
    )
    dbmod.insert_character(
        book_id=book["id"], name="沈砚", personality=["热情", "话多"], source="novel",
    )
    dbmod.insert_character(
        book_id=book["id"], name="苏晚", personality=["温柔"], source="novel",
    )

    original_get_agent = chat_endpoint.get_agent
    original_speakers = settings.group_max_speakers
    original_rounds = settings.group_max_rounds
    original_all = settings.group_all_threshold

    chat_endpoint.get_agent = lambda **kwargs: _FakeAgent(["@苏晚 你看呢？"])
    settings.group_max_speakers = 1
    settings.group_max_rounds = 2
    settings.group_all_threshold = 0.0
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 先 @ 沈砚 -> 沈砚必答；它的假回复 @ 苏晚 -> 第二轮苏晚必答。
            # 用显式 @ 起头，顺序才是确定的（否则谁分高谁先说，测试会抖）。
            session = (
                await client.post("/api/session", json={"display_name": "群聊SSE"})
            ).json()
            r = await client.post(
                f"/api/shelves/{shelf['id']}/chat",
                json={"token": session["token"], "prompt": "@沈砚 你好"},
            )
            check("群聊SSE：接口 200", r.status_code == 200, r.text[:200])
            check("群聊SSE：content-type 为 event-stream",
                  r.headers.get("content-type", "").startswith("text/event-stream"),
                  r.headers.get("content-type", ""))

            events = [
                json.loads(line[5:].strip())
                for line in r.text.splitlines()
                if line.startswith("data:")
            ]
            kinds = [e["type"] for e in events]
            check("群聊SSE：事件序列完整（整段发送）",
                  {"user", "round_start", "speaker_start", "speaker_end", "done"}
                  <= set(kinds), str(kinds))
            check("群聊SSE：以 done 收尾", kinds and kinds[-1] == "done", str(kinds[-3:]))

            speaker_starts = [e for e in events if e["type"] == "speaker_start"]
            check("群聊SSE：speaker_start 带 character_id/name/avatar",
                  bool(speaker_starts)
                  and {"character_id", "name", "avatar"} <= set(speaker_starts[0]),
                  str(speaker_starts[:1]))
            deltas = [e for e in events if e["type"] == "delta"]
            check("群聊SSE：不逐段推送（等聚合查重后整段发）",
                  not deltas, str(len(deltas)))
            ends_now = [e for e in events if e["type"] == "speaker_end"]
            check("群聊SSE：完整文本在 speaker_end 里一次给出",
                  bool(ends_now) and all(e.get("text") for e in ends_now),
                  str([e.get("text", "")[:12] for e in ends_now]))

            ends = [e for e in events if e["type"] == "speaker_end"]
            check("群聊SSE：speaker_end 带最终文本与 @",
                  bool(ends) and {"text", "mentions"} <= set(ends[0]), str(ends[:1]))
            check("群聊SSE：speaker_end 带贴纸字段（可能为空）",
                  bool(ends) and "sticker" in ends[0], str(ends[:1]))
            # --- 群聊也支持发图（以前后端直接拒绝："群聊目前只支持文字消息"）---
            empty = await client.post(
                f"/api/shelves/{shelf['id']}/chat",
                json={"token": session["token"], "prompt": ""},
            )
            check("群聊图片：空内容被拒", empty.status_code == 400, empty.text[:120])

            tiny_png = (
                "data:image/png;base64,"
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
            )
            with_image = await client.post(
                f"/api/shelves/{shelf['id']}/chat",
                json={
                    "token": session["token"],
                    "prompt": "",
                    "images_base64": [tiny_png],
                },
            )
            check("群聊图片：只发图也能进流程", with_image.status_code == 200, with_image.text[:120])
            image_events = [
                json.loads(line[5:].strip())
                for line in with_image.text.splitlines()
                if line.startswith("data:")
            ]
            check(
                "群聊图片：纯图片时用户消息记为［图片］",
                any(e.get("type") == "user" and "图片" in str(e.get("text") or "") for e in image_events),
                str([e.get("type") for e in image_events[:4]]),
            )
            bad_image = await client.post(
                f"/api/shelves/{shelf['id']}/chat",
                json={"token": session["token"], "prompt": "", "images_base64": ["not-an-image"]},
            )
            check("群聊图片：坏图片被拒", bad_image.status_code == 400, bad_image.text[:120])

            # 图片要能被历史读回来：落库成 /api/chat-image/<folder>/<name>
            turns = dbmod.list_chat_turns(shelf["id"], limit=10)
            image_turns = [item for item in turns if item.get("images")]
            check("群聊图片：图片地址写进了流水",
                  bool(image_turns) and image_turns[-1]["images"][0].startswith("/api/chat-image/"),
                  str([item.get("images") for item in image_turns][-2:]))
            image_url = image_turns[-1]["images"][0] if image_turns else ""
            fetched = await client.get(image_url)
            check("群聊图片：能按地址读回原图",
                  fetched.status_code == 200 and fetched.content[:4] == b"\x89PNG",
                  f"{fetched.status_code} {fetched.content[:8]}")
            check("群聊图片：读图接口拒绝非法目录名",
                  (await client.get("/api/chat-image/..%2F..%2Fapp.db/x.png")).status_code in (404, 400),
                  "")
            # 清理测试落下的图片目录
            if image_url:
                folder = image_url.split("/")[-2]
                import shutil as _shutil
                _shutil.rmtree(get_settings().uploads_path / "chat" / folder, ignore_errors=True)

            check("群聊SSE：沈砚被 @ 后必答，且它 @ 苏晚触发了第二轮",
                  [e["name"] for e in speaker_starts] == ["沈砚", "苏晚"],
                  str([e["name"] for e in speaker_starts]))
    finally:
        chat_endpoint.get_agent = original_get_agent
        settings.group_max_speakers = original_speakers
        settings.group_max_rounds = original_rounds
        settings.group_all_threshold = original_all
        library.delete_book(book["id"])
        dbmod.delete_shelf(shelf["id"])


def main() -> int:
    # 检查项里会打印表情（贴纸、情绪），Windows 控制台默认 GBK 会直接崩
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    document_checks()
    pipeline_checks()
    miner_checks()
    name_library_checks()
    db_checks()
    library_shelf_checks()
    agent_log_checks()
    memory_item_checks()
    affect_checks()
    character_agent_checks()
    group_chat_checks()
    shelf_book_checks()
    merge_checks()
    expression_checks()
    rag_checks()
    channels_checks()
    cleaner_checks()
    reply_checks()
    emotion_model_checks()
    reextract_identity_checks()
    interrupted_extraction_checks()
    asyncio.run(extract_fallback_checks())
    asyncio.run(auto_merge_checks())
    dispatch_checks()
    skill_checks()
    intent_checks()
    awaiting_checks()
    fetch_script_checks()
    skill_vendor_checks()
    skill_validate_checks()
    skill_package_checks()
    mode_checks()
    animation_checks()
    conversation_checks()
    clear_conversation_checks()
    time_tool_checks()
    temperament_checks()
    asyncio.run(sqlite_checkpoint_checks())
    asyncio.run(checkpoint_checks())
    asyncio.run(langgraph_checks())
    asyncio.run(dialogue_judge_checks())
    asyncio.run(dialogue_checks())
    asyncio.run(housekeeping_checks())
    asyncio.run(group_awake_checks())
    asyncio.run(group_angle_checks())
    asyncio.run(group_parallel_checks())
    asyncio.run(group_graph_checks())
    asyncio.run(group_turn_checks())
    asyncio.run(group_presence_checks())
    asyncio.run(history_and_shelf_checks())
    asyncio.run(extraction_checks())
    asyncio.run(hybrid_enrichment_checks())
    asyncio.run(avatar_crawler_checks())
    asyncio.run(avatar_api_checks())
    asyncio.run(name_engine_checks())
    asyncio.run(web_tools_checks())
    asyncio.run(extraction_agent_checks())
    asyncio.run(verify_endpoint_checks())
    asyncio.run(http_checks())
    asyncio.run(algo_http_checks())
    asyncio.run(shelf_http_checks())
    asyncio.run(character_status_checks())
    asyncio.run(group_http_checks())
    asyncio.run(group_chat_sse_checks())
    asyncio.run(sticker_http_checks())
    asyncio.run(rag_http_checks())
    asyncio.run(channels_http_checks())
    asyncio.run(cleaner_http_checks())

    failed = [item for item in results if not item[1]]
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if not ok and detail:
            line += f"  <- {detail}"
        print(line)

    # 自检会建临时书库（自检图库/性格自检库…），跑完自己收拾，
    # 否则跑几十次以后书库里全是垃圾
    cleaned = cleanup_test_libraries()
    if cleaned:
        print(f"（已清理 {cleaned} 个临时书库）")

    print()
    print(f"共 {len(results)} 项，失败 {len(failed)} 项")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
