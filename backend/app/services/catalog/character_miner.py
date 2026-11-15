"""纯算法角色挖掘（标准库实现，零 API 成本）。

四条产线：
1. **发现候选**：说话动词 / 动作主语 / 称谓前缀 / "名叫·唤作" 提名句式 / 姓氏锚定，
   再用出现次数与邻字多样性过滤。
2. **归并别称**：`老周`/`周掌柜`/`掌柜老周`、`沈砚`/`沈公子` 归到同一个本名；
   引号里的四字绰号（如「寒江一剑」）按共现挂到唯一主角身上。
3. **归属台词**：以句子为单位判断说话人（引号外名字 → 说话标记 → 代词回指 → 两人轮替），
   并把别称形式解析回本名。
4. **量化特征**：句长分布、标点习惯、自称/对称词、旁白背景、段落共现关系。

另外提供 :func:`validate_characters`：任何人名必须在原文里真的出现，
这是拦住"模型编造角色"的最后一道闸（hybrid / llm 模式都会用到）。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------
TITLE_WORDS: tuple[str, ...] = (
    # 古风/仙侠
    "掌柜", "师父", "师兄", "师姐", "师伯", "师叔", "弟子", "徒儿", "公子",
    "姑娘", "小姐", "少爷", "老爷", "夫人", "大人", "兄台", "前辈", "先生",
    "将军", "陛下", "殿下", "掌门", "长老", "宗主", "帮主", "城主", "族长",
    "公主", "王子", "老丈", "阁下", "少侠", "大侠", "总管", "管家", "护卫",
    "侍卫", "丫鬟", "嬷嬷", "道长", "大师", "居士",
    # 现代/校园/军事（真实小说里大量出现，缺了就会把「施耐德教授」切出「德教授」）
    "教授", "副校长", "校长", "院长", "主任", "导师", "老师", "同学", "学长",
    "学姐", "学弟", "学妹", "部长", "局长", "处长", "科长", "队长", "组长",
    "班长", "会长", "社长", "主席", "船长", "舰长", "大副", "二副", "机长",
    "经理", "老板", "董事长", "董事", "秘书", "助理", "医生", "护士", "警官",
    "警长", "法官", "律师", "神父", "牧师", "修士", "骑士", "佣兵", "队员",
)

#: 纯身份/职位/泛称词：出现这些基本不是具体角色的"名字"
IDENTITY_WORDS: frozenset[str] = frozenset(
    """
    掌门 长老 宗主 帮主 城主 族长 皇帝 太子 太后 皇后 贵妃 王爷 公主 王子
    师父 师兄 师姐 师弟 师妹 弟子 徒弟 徒儿 掌门人 老祖 真人 道君 上仙 仙子
    公子 姑娘 小姐 少爷 老爷 夫人 大人 阁下 前辈 先生 后生 少年 少女 老者
    老丈 老汉 妇人 女子 男子 众人 掌柜 小二 店小二 丫鬟 侍女 侍卫 护卫
    家丁 管家 总管 大夫 郎中 书生 秀才 将军 士兵 士卒 黑衣人 蒙面人
    白衣人 青衣人 高手 强者 修士 道友 魔头 妖女 圣子 圣女 家主
    教授 校长 副校长 院长 主任 导师 老师 同学 学长 学姐 学弟 学妹
    部长 局长 处长 科长 队长 组长 班长 会长 社长 主席 船长 舰长 大副 二副
    经理 老板 董事长 秘书 助理 医生 护士 警官 警长 法官 律师 神父 牧师
    骑士 佣兵 队员 教练 老板 二副 三副 水手 船员 司机 保安 前台
    龙王 龙族 魔族 妖族 神族 人族 精灵 矮人 巨人 兽人 亡灵 恶魔 天使
    男人 女人 男孩 女孩 孩子 小孩 老人 中年人 年轻人 少年人 家伙
    这小子 那家伙 这帮人 那些人 所有人 每个人 有人 别人 旁人 众人
    叔叔 阿姨 婶婶 舅舅 姑姑 爷爷 奶奶 爸爸 妈妈 哥哥 姐姐 弟弟 妹妹
    表哥 表姐 堂哥 堂姐 外公 外婆 岳父 岳母 师父 师母 干爹 干妈
    轻声 大声 低声 高声 小声 厉声 沉声 柔声 冷声 淡淡 冷冷 轻轻 慢慢
    缓缓 静静 默默 忽然 突然 立刻 马上 渐渐 随后 接着 于是 终于
    """.split()
)

#: 组织/地点结尾字：以这些字结尾的多半是门派、地名
ORG_SUFFIXES: tuple[str, ...] = (
    "门", "派", "帮", "宫", "阁", "堂", "宗", "府", "殿", "城", "山", "谷",
    "岛", "寺", "观", "楼", "庄", "村", "镇", "寨", "坊", "铺", "局", "会",
    "院", "校", "系", "科", "处", "署", "厅", "局",
    "堂口", "书院", "世家", "一族", "家族", "学院", "大学", "中学",
)

SPEECH_VERBS: tuple[str, ...] = (
    "说", "道", "问", "答", "笑", "喊", "叫", "骂", "应", "叹", "嘀咕", "开口",
    "冷笑", "低声道", "轻声道", "沉声道", "喝道", "问道", "答道", "笑道", "叹道",
    "插嘴", "嚷", "接口", "回答", "追问", "反问", "哂", "嗤",
)

ACTION_VERBS: tuple[str, ...] = (
    "推", "走", "站", "坐", "看", "望", "拿", "抬", "转", "握", "拔", "放",
    "端", "抱", "回头", "起身", "低头", "抬头", "点头", "摇头", "咳", "皱眉",
    "盯", "瞥", "跪", "扑", "冲", "抓", "揣", "收", "知", "想",
)

PRONOUNS: tuple[str, ...] = (
    "他", "她", "它", "他们", "她们", "我", "你", "您", "我们", "你们",
    "自己", "这人", "那人", "两人", "众人", "对方",
)

#: 明显不是人名的常见词
STOP_NAMES: frozenset[str] = frozenset(
    """
    一个 一些 一样 一边 一直 一起 一声 一句 一切 一手 一点 一片 一阵 一番
    这个 那个 这些 那些 这样 那样 这里 那里 这时 那时 此刻 此时 这种 那种
    什么 怎么 为什么 如何 因为 所以 但是 可是 只是 不过 于是 然后 接着
    已经 曾经 依然 仍然 依旧 忽然 突然 终于 立刻 马上 渐渐 慢慢 缓缓
    声音 目光 眼神 脸上 心里 手中 脚下 身后 面前 身上 眼中 嘴角 手里
    江湖 天下 世上 人间 客栈 酒楼 房间 门口 窗外 灯笼 铜钱 信封 房间
    年轻 年长 老人 女子 男子 少年 少女 孩子 第一 第二 第三 两年 三年
    十年 三天 一刻 半晌 今日 明日 昨日 当年 此时 之后 之前 后来 最后
    觉得 知道 看见 听见 明白 想起 认为 以为 说道 笑道 问道 答道 低声
    可以 不要 应该 或者 不知 还不知 不会 不能 只是 已经 自己 这样 那样
    什么 什幺 怎幺 怎么 为什么 这个是 那个是 一个 一些 一切 可能 需要 所以
    但是 而且 并且 只有 只要 虽然 如果 因为 于是 然后 终于 立刻 马上
    时候 小时 学生 眼睛 一次 一天 一年 一会 一天到晚 地方 问题 事情 东西
    感觉 样子 意思 办法 机会 时间 声音 名字 世界 城市 学校 公司 国家 社会
    中国 日本 美国 英国 法国 德国 俄国 欧洲 亚洲 北京 上海 东京 纽约
    地铁 电梯 手机 电脑 汽车 飞机 火车 医院 银行 房间 教室 食堂 图书馆
    办公室 宿舍 厨房 客厅 走廊 地下 通道 老板 同学 朋友 家人 父母 孩子
    老家 小心 开玩 玩笑 一愣 一下 一直 一样 一边 一点 一阵 一番
    双手 水下 水流 水面 水面上 水底 通过 游戏 任何人 后一 前后 四周 周围
    其他 其实 之后 以前 以后 刚才 一定 心里 眼前 环境 震惊 声音 突然
    学院 学校 校长 同时 作为 仍旧 前进 漂亮 抓住 觉得 耸耸肩 奖学金
    笑着 笑了 笑道 冷笑 点头 摇头 抬头 低头 回头 转身 站起 坐下 走开
    """.split()
)

#: 去掉这些字后仍以之结尾的，基本是组织/地点
_FUNCTION_CHARS = set(
    "了的着是在有这那个和就都也我你他她它们把被从对与并且但所之其"
    "不没无别太很更最又才只已刚正得"
)

#: 姓氏锚定（只在已知候选中加分，不单独造候选，避免"明天""国家"这类噪声）
SURNAME_CHARS = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛范彭鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑"
    "薛雷贺倪汤殷罗毕郝安常于傅齐康伍余元卜顾孟平黄穆萧尹姚邵汪祁毛狄"
    "米贝明藏计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危"
    "江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯管卢莫房裘解应宗"
    "丁宣邓郁单杭洪包诸左石崔吉龚程嵇邢裴陆荣翁荀羊惠甄曲家封芮储靳段"
    "富巫乌焦巴弓牧隗山谷车侯全班仰秋仲伊宫宁仇栾暴甘厉戎祖武符刘景詹"
    "束龙叶幸司韶黎薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥苍双闻莘"
    "党翟谭贡劳姬申扶堵冉宰郦雍桑桂濮牛寿通边扈燕冀浦尚农温别庄晏柴瞿"
    "阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄"
    "阙东欧殳沃利蔚越夔隆师巩聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须"
    "丰巢关蒯相查后荆红游竺权逯盖益桓"
)

_QUOTE_RE = re.compile(r"[“\"]([^“”\"]{1,600})[”\"]")
_PARA_SPLIT = re.compile(r"\n\s*\n")
_SENTENCE_END = set("。！？!?…\n")
_HAN = r"\u4e00-\u9fa5"

_SPEECH_ALT = "|".join(SPEECH_VERBS)
_ACTION_ALT = "|".join(ACTION_VERBS)
_TITLE_ALT = "|".join(TITLE_WORDS)

#: 名字与说话动词之间允许出现的一小撮副词（"沈砚低声说"）。
#: 用它替代"任意 0~4 字"的模糊匹配 —— 后者会把"江湖上都""老周低声"这类碎片当成名字。
_ADVERBS: tuple[str, ...] = (
    "低声", "轻声", "沉声", "冷声", "厉声", "柔声", "大声", "小声", "笑着", "苦笑着",
    "冷笑着", "淡淡", "冷冷", "微微", "缓缓", "慢慢", "轻轻", "又", "也", "便", "却",
    "就", "才", "这才", "随即", "接着", "忽然", "突然", "点头", "摇头", "皱眉",
)
_ADVERB_ALT = "|".join(_ADVERBS)

_SPEECH_TAG = re.compile(rf"([{_HAN}]{{2,3}})(?:(?:{_ADVERB_ALT}))?(?:{_SPEECH_ALT})")
_ACTION_TAG = re.compile(rf"([{_HAN}]{{2,3}})(?:{_ACTION_ALT})")
_TITLE_PREFIX = re.compile(rf"(?:{_TITLE_ALT})([{_HAN}]{{2,3}})")
_NOMINATION = re.compile(
    rf"(?:名叫|叫做|名为|唤作|人称|号称|自称|外号|绰号[是为：:]?)([{_HAN}]{{2,4}})"
)
_OLD_YOUNG = re.compile(rf"([老小阿][{_HAN}])")
_PRE_TAG = re.compile(
    rf"([{_HAN}]{{2,3}})(?:(?:{_ADVERB_ALT}))?(?:{_SPEECH_ALT})[，。：:]?\s*$"
)
_PRONOUN_TAG = re.compile(rf"([他她])(?:(?:{_ADVERB_ALT}))?(?:{_SPEECH_ALT})")

#: 「X说：“……”」—— 最像"有人开口说话"的证据（允许中间夹称谓，处理「曼施坦因教授说」）
_QUOTE_SPEAKER = re.compile(
    rf"([{_HAN}]{{2,4}})(?:(?:{_ADVERB_ALT}|{_TITLE_ALT}))?(?:{_SPEECH_ALT})[，。：:]?\s*[“\"]"
)
#: 「“……。”X说」
_QUOTE_SPEAKER_AFTER = re.compile(
    rf"[”\"]\s*([{_HAN}]{{2,4}})(?:(?:{_ADVERB_ALT}|{_TITLE_ALT}))?(?:{_SPEECH_ALT})"
)

#: 「X教授」「X掌柜」—— 名字在前、称谓在后（曼施坦因教授 / 施耐德教授）
_NAME_TITLE_SUFFIX = re.compile(rf"([{_HAN}]{{2,4}})(?:{_TITLE_ALT})")

#: 说话/动作动词的首尾字：候选以它们结尾基本是匹配碎片（沈砚抬、老周低）
_VERB_CHARS = {verb[0] for verb in SPEECH_VERBS + ACTION_VERBS} | {
    verb[-1] for verb in SPEECH_VERBS + ACTION_VERBS
}
#: 以这些字结尾的多半是副词/虚词
_ADVERB_TAILS = set("地然着了们")
#: 以这些字结尾的多半是方位/地点短语（水下、后一、水面上、心里）
_PLACE_TAILS = set("上下里中外边面前后间")


@dataclass
class MinedCharacter:
    name: str
    aliases: list[str] = field(default_factory=list)
    quotes: list[str] = field(default_factory=list)
    narration: list[str] = field(default_factory=list)
    appearances: int = 0
    score: float = 0.0
    partners: Counter = field(default_factory=Counter)

    def as_record(self) -> dict:
        stats = language_stats(self.quotes)
        return {
            "name": self.name,
            "aliases": self.aliases[:6],
            "role": "",
            "personality": behaviour_tags(stats, len(self.quotes), len(self.narration)),
            "speech_style": describe_style(stats),
            "background": narrative_background(self.narration),
            "relationships": [
                {"with": partner, "relation": f"同场 {count} 段"}
                for partner, count in self.partners.most_common(4)
            ],
            "quotes": self.quotes[:6],
            "narration": self.narration[:4],
            "excerpts": [],
            "appearances": self.appearances,
        }


# ---------------------------------------------------------------------------
# 文本切分
# ---------------------------------------------------------------------------
def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in _PARA_SPLIT.split(text or "") if p.strip()]


def split_sentences(paragraph: str) -> list[str]:
    """按句末标点切句；引号内的标点不切（保持台词完整）。"""
    sentences: list[str] = []
    buffer: list[str] = []
    in_quote = False
    for char in paragraph:
        if char in "“\"":
            in_quote = True
        elif char == "”":
            in_quote = False

        buffer.append(char)
        if char in _SENTENCE_END and not in_quote:
            sentence = "".join(buffer).strip()
            if sentence:
                sentences.append(sentence)
            buffer = []
    tail = "".join(buffer).strip()
    if tail:
        sentences.append(tail)
    return sentences


def split_sentences_text(text: str) -> list[str]:
    sentences: list[str] = []
    for paragraph in split_paragraphs(text):
        sentences.extend(split_sentences(paragraph))
    return sentences


def strip_titles(token: str) -> str:
    """去掉称谓前缀/后缀：掌柜老周 -> 老周；沈公子 -> 沈。"""
    previous = None
    current = token
    while previous != current:
        previous = current
        current = re.sub(rf"^(?:{_TITLE_ALT})", "", current)
        current = re.sub(rf"(?:{_TITLE_ALT})$", "", current)
    return current


def _outside_quotes(sentence: str) -> str:
    return _QUOTE_RE.sub(" ", sentence)


def is_identity_like(token: str) -> bool:
    """这个词像不像"职位/组织/泛称/动词碎片"而不是具体人名。"""
    token = (token or "").strip()
    if len(token) < 2:
        return True
    core = strip_titles(token)
    if not core or len(core) < 2:
        # 剥掉称谓后只剩 1 个字：多半是「施耐德教授」被切成「德教授」这类碎片
        return True
    if token in IDENTITY_WORDS or core in IDENTITY_WORDS:
        return True
    if token in STOP_NAMES or core in STOP_NAMES:
        return True
    if any(token.endswith(suffix) for suffix in ORG_SUFFIXES):
        return True
    if token[-1] in _VERB_CHARS:
        # 以动作/说话动词结尾：是「沈砚抬」这类匹配碎片
        return True
    if token[-1] in _ADVERB_TAILS:
        # 以「地/然/着/了/们」结尾：副词或虚词
        return True
    if token[-1] in _PLACE_TAILS:
        # 以「上/下/里/中/外/边/面/前/后/间」结尾：方位短语
        return True
    if any(char in _FUNCTION_CHARS for char in token):
        return True
    return False


# ---------------------------------------------------------------------------
# 1) 发现候选
# ---------------------------------------------------------------------------
def _context_diversity(text: str, name: str) -> tuple[int, int]:
    lefts: set[str] = set()
    rights: set[str] = set()
    for match in re.finditer(re.escape(name), text):
        if match.start() > 0:
            lefts.add(text[match.start() - 1])
        if match.end() < len(text):
            rights.add(text[match.end()])
    return len(lefts), len(rights)


def _expand_forms(token: str) -> list[str]:
    """匹配到的片段本身就是候选，不再额外切 2 字窗口（那会造出「德教」「施耐」这类碎片）。"""
    return [token] if 2 <= len(token) <= 4 else []


def discover_names(
    text: str, *, min_mentions: int = 3, max_names: int = 40
) -> tuple[list[tuple[str, float]], list[tuple[str, str]]]:
    """返回 (候选[(形式, 分数)], 被丢弃的[(形式, 原因)])。

    判定标准（按证据强度）：
    * ``quote``  ：「X说：“……”」这类直接对白标记 —— 最强
    * ``titled`` ：带称谓出现（「教授」「掌柜」）或提名句式（「名叫X」）
    * ``speech`` ：「X说/道/问」—— 但普通词也可能凑上（"小时说"），所以只凭它不够
    * ``action`` ：只有动作的主语 —— 最弱，仅对"姓氏开头"的名字采信

    这样既能保住原型（7 次出场、台词全走「他说」）这样的主角，
    又能刷掉「中国 / 仿佛 / 小时 / 一愣 / 小心 / 老家」这类高频普通词。
    """
    counters = {
        "speech": Counter(),
        "quote": Counter(),
        "action": Counter(),
        "titled": Counter(),
        "nick": Counter(),
    }

    def record(token: str, kind: str) -> None:
        for form in _expand_forms(token):
            counters[kind][form] += 1

    for match in _QUOTE_SPEAKER.finditer(text):
        record(match.group(1), "quote")
    for match in _QUOTE_SPEAKER_AFTER.finditer(text):
        record(match.group(1), "quote")
    for match in _SPEECH_TAG.finditer(text):
        record(match.group(1), "speech")
    for match in _PRE_TAG.finditer(text):
        record(match.group(1), "speech")
    for match in _NOMINATION.finditer(text):
        record(match.group(1), "titled")
    for match in _TITLE_PREFIX.finditer(text):
        record(match.group(1), "titled")
    for match in _NAME_TITLE_SUFFIX.finditer(text):
        record(match.group(1), "titled")
    for match in _OLD_YOUNG.finditer(text):
        record(match.group(1), "nick")
    for match in _ACTION_TAG.finditer(text):
        record(match.group(1), "action")

    # 引号紧邻的片段：只加分，不单独造候选
    weak: Counter = Counter()
    for match in _QUOTE_RE.finditer(text):
        for window in (
            text[max(0, match.start() - 8): match.start()],
            text[match.end(): match.end() + 8],
        ):
            for token in re.findall(rf"[{_HAN}]{{2,4}}", window):
                for form in _expand_forms(token):
                    weak[form] += 0.2

    candidates: set[str] = set()
    for counter in counters.values():
        candidates |= set(counter)

    ranked: list[tuple[str, float]] = []
    dropped: list[tuple[str, str]] = []

    #: 绝对频次门槛：长文里只出现十几次的词几乎都是普通词或碎片（短文本自动放宽）
    mention_floor = max(min_mentions, min(20, len(text) // 15000))

    #: 独立出现次数（扣掉"只作为更长候选的一部分"的出现）
    #  「学院」总共 265 次，其中 153 次来自「塞尔学院」-> 独立出现 112 次
    raw_counts = {token: text.count(token) for token in candidates}
    longer_hits: dict[str, int] = {}
    for token in candidates:
        covered = sum(
            count
            for other, count in raw_counts.items()
            if other != token and len(other) > len(token) and token in other
        )
        longer_hits[token] = covered
    standalone = {
        token: max(0, raw_counts[token] - longer_hits[token]) for token in candidates
    }

    for token in candidates:
        if token in PRONOUNS:
            continue
        if is_identity_like(token):
            dropped.append((token, "身份/组织/虚词/动词碎片"))
            continue

        mentions = standalone[token]
        if mentions < mention_floor:
            dropped.append((token, f"独立出现 {mentions} 次 < {mention_floor}"))
            continue

        speech = counters["speech"][token]
        quote = counters["quote"][token]
        titled = counters["titled"][token]
        action = counters["action"][token]
        nick = counters["nick"][token]
        surname = token[0] in SURNAME_CHARS
        quote_ratio = quote / max(1, mentions)

        if surname:
            # 姓氏开头：有说话 / 称谓 / 对白标记 / 动作主语证据即可
            keep = quote >= 1 or titled >= 1 or speech >= 2 or action >= 1
        else:
            # 音译名/昵称/外号：可复用称呼形态、带称谓，或稳定且占比较高的对白标记
            keep = nick >= 2 or titled >= 1 or (quote >= 3 and quote_ratio >= 0.05)
        if not keep:
            dropped.append(
                (token, f"不像名字（对白 {quote} 次 / 独立 {mentions} 次）")
            )
            continue

        lefts, rights = _context_diversity(text, token)
        if max(lefts, rights) < 2:
            dropped.append((token, "邻字过于单一"))
            continue

        score = (
            3.0 * speech + 4.0 * quote + 2.0 * titled + 0.6 * action
            + min(6.0, mentions * 0.5) + 0.2 * max(lefts, rights)
            + (0.8 if surname else 0.0) + weak.get(token, 0.0)
        )
        ranked.append((token, score))

    # 去掉互为碎片的候选：
    #   古德里安(221) 保留 -> 古德里 丢弃
    #   曼施坦因(160) 保留 -> 施坦因 / 曼施 丢弃（等频时取更长的）
    #   富山雅史(43) 保留 -> 富山雅 / 山雅史 丢弃
    #   路明非(1812) 保留 -> 明非一 / 明非心 丢弃（与更常见名字重叠 ≥2 字）
    ranked.sort(key=lambda item: (-item[1], -len(item[0])))
    counts = {token: text.count(token) for token, _ in ranked}

    def max_overlap(left: str, right: str) -> int:
        best = 0
        for start in range(len(left)):
            for end in range(len(left), start + best, -1):
                if left[start:end] in right:
                    best = max(best, end - start)
                    break
        return best

    absorbed: set[str] = set()

    # 1) 长名字吸收「几乎总是出现在它内部」的短名字：
    #    曼施坦因(160) 吞掉 曼施坦(161)/施坦因(160)；古德里安(221) 吞掉 古德里(221)
    ordered = sorted(ranked, key=lambda item: (-len(item[0]), -item[1]))
    for index, (long_name, _score) in enumerate(ordered):
        for short_name, _s in ordered[index + 1:]:
            if short_name in absorbed or long_name == short_name:
                continue
            if not (
                short_name in long_name
                or long_name.startswith(short_name)
                or long_name.endswith(short_name)
            ):
                continue
            if counts[long_name] >= counts[short_name] * 0.8:
                absorbed.add(short_name)

    # 2) 与「常见得多」的名字重叠 ≥2 字 -> 判定为碎片（路明非 -> 明非一/明非心）
    for token, _score in ranked:
        if token in absorbed:
            continue
        for other, _s in ranked:
            if other == token or other in absorbed:
                continue
            if counts[other] > counts[token] * 2 and max_overlap(token, other) >= 2:
                absorbed.add(token)
                break

    final = [(token, score) for token, score in ranked if token not in absorbed]
    return final[:max_names], dropped

    return final[:max_names], dropped


# ---------------------------------------------------------------------------
# 2) 归并别称
# ---------------------------------------------------------------------------
def group_forms(
    text: str, forms: Sequence[str]
) -> tuple[list[str], dict[str, list[str]]]:
    """把别称形式归到本名。返回 (本名列表, {本名: [其它形式]})。

    只做「同名不同叫法」的确定性归并：剥掉称谓后同核心的算一个，
    单字核心挂到唯一包含它的本名上。四字绰号不再靠统计去猜
    —— 之前那版会把「青铜计划」「灰锡溶液」当成别名，噪声太大，
    绰号交给 hybrid 模式的模型判断。
    """
    forms = list(dict.fromkeys(forms))
    cores = {form: strip_titles(form) for form in forms}
    groups: dict[str, list[str]] = defaultdict(list)

    multi = [form for form in forms if len(cores[form]) >= 2]
    for form in multi:
        groups[cores[form]].append(form)

    # 单字核心（沈公子 -> 沈）：挂到唯一包含该字的候选上
    for form in forms:
        core = cores[form]
        if len(core) != 1:
            continue
        # 按「核心」去重：老周 / 掌柜老周 的核心都是「老周」，算同一个归属
        owner_cores = {cores[other] for other in multi if core in cores[other]}
        if len(owner_cores) == 1:
            groups[next(iter(owner_cores))].append(form)

    canonicals: list[str] = []
    aliases: dict[str, list[str]] = defaultdict(list)
    for members in groups.values():
        best = max(members, key=lambda form: (text.count(form), len(cores[form]), -len(form)))
        canonicals.append(best)
        for member in members:
            if member != best:
                aliases[best].append(member)

    return canonicals, aliases


# ---------------------------------------------------------------------------
# 3) 台词归属
# ---------------------------------------------------------------------------
def _names_in(text: str, forms: Sequence[str]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for form in forms:
        start = 0
        while True:
            index = text.find(form, start)
            if index < 0:
                break
            found.append((index, form))
            start = index + len(form)
    return found


def attribute_dialogues(
    text: str,
    form_map: dict[str, str],
) -> tuple[list[tuple[str, str, str]], dict]:
    """返回 [(本名, 台词, 置信度)] 与置信度统计。

    form_map: 任意形式 -> 本名（本名映射到自己）。
    """
    forms = list(form_map)
    attributed: list[tuple[str, str, str]] = []
    stats = Counter()
    last_speaker = ""
    last_narrated = ""

    for paragraph in split_paragraphs(text):
        paragraph_narrated = {
            form_map[form] for _, form in _names_in(_outside_quotes(paragraph), forms)
        }
        for sentence in split_sentences(paragraph):
            quotes = [m.group(1).strip() for m in _QUOTE_RE.finditer(sentence)]
            outside = _outside_quotes(sentence)
            present = _names_in(outside, forms)
            present_canonicals = sorted({form_map[form] for _, form in present})

            if len(present_canonicals) == 1 and not quotes:
                last_narrated = present_canonicals[0]
            if not quotes:
                continue

            speaker = ""
            confidence = "unknown"

            if len(present_canonicals) == 1:
                speaker, confidence = present_canonicals[0], "high"
            elif present:
                first_quote = _QUOTE_RE.search(sentence)
                anchor = first_quote.start() if first_quote else len(outside)
                nearest = min(present, key=lambda item: abs(item[0] - anchor))
                speaker, confidence = form_map[nearest[1]], "medium"
            else:
                pronoun = _PRONOUN_TAG.search(outside)
                if pronoun:
                    others: list[str] = []
                    if len(paragraph_narrated) == 2:
                        others = [name for name in paragraph_narrated if name != last_speaker]
                    if len(others) != 1 and last_narrated and last_narrated != last_speaker:
                        others = [last_narrated]
                    if len(others) != 1:
                        recent = list(dict.fromkeys([s for s, _q, _c in attributed][-2:]))
                        if len(recent) == 2 and last_speaker in recent:
                            others = [name for name in recent if name != last_speaker]
                    if len(others) == 1:
                        speaker, confidence = others[0], "low"

            if not speaker:
                stats["unknown"] += 1
                last_speaker = ""
                continue

            for quote in quotes:
                attributed.append((speaker, quote, confidence))
                stats[f"confidence:{confidence}"] += 1
            last_speaker = speaker

    return attributed, dict(stats)


def _cooccurrence(text: str, form_map: dict[str, str]) -> dict[str, Counter]:
    forms = list(form_map)
    partners: dict[str, Counter] = defaultdict(Counter)
    for paragraph in split_paragraphs(text):
        present = sorted(
            {form_map[form] for _, form in _names_in(_outside_quotes(paragraph), forms)}
        )
        for name in present:
            for other in present:
                if other != name:
                    partners[name][other] += 1
    return partners


# ---------------------------------------------------------------------------
# 4) 语言特征
# ---------------------------------------------------------------------------
_SELF_REF = ("我", "在下", "晚辈", "本王", "朕", "老身", "小女子", "小生", "吾", "妾身")
_ADDRESS = ("你", "您", "阁下", "姑娘", "公子", "前辈", "师父", "大人", "兄台", "少侠")


def language_stats(quotes: Sequence[str]) -> dict:
    if not quotes:
        return {}
    lengths = [len(q) for q in quotes]
    joined = "".join(quotes)
    count = len(quotes)
    return {
        "count": count,
        "avg_len": round(sum(lengths) / count, 1),
        "median_len": round(sorted(lengths)[count // 2], 1),
        "max_len": max(lengths),
        "exclaim_rate": round(joined.count("！") / count, 2),
        "question_rate": round(joined.count("？") / count, 2),
        "ellipsis_rate": round((joined.count("…") + joined.count("...")) / count, 2),
        "self_refs": [w for w in _SELF_REF if w in joined],
        "address_terms": [w for w in _ADDRESS if w in joined],
    }


def behaviour_tags(stats: dict, quote_count: int, narration_count: int) -> list[str]:
    """把统计事实翻成可执行的性格描述。"""
    if not stats:
        return ["（原文几乎没有台词，只留下旁白线索）"] if narration_count else []

    tags: list[str] = []
    avg = stats["avg_len"]
    if avg <= 8:
        tags.append(f"话少，平均每句只有 {avg} 字")
    elif avg >= 26:
        tags.append(f"话多，动辄长篇（平均 {avg} 字/句）")
    if stats["exclaim_rate"] >= 0.3:
        tags.append("语气激烈，常带感叹")
    elif stats["exclaim_rate"] == 0:
        tags.append("语气平缓，几乎不用感叹号")
    if stats["question_rate"] >= 0.3:
        tags.append("爱用反问")
    if stats["ellipsis_rate"] >= 0.2:
        tags.append("常把话说一半（多用省略号）")
    if quote_count <= 2 and narration_count >= 3:
        tags.append("多以行动示人，台词极少")
    if not tags:
        tags.append("语气自然克制")
    return tags[:5]


def describe_style(stats: dict) -> str:
    if not stats:
        return ""
    parts = [f"平均 {stats['avg_len']} 字/句（最长 {stats['max_len']} 字）"]
    if stats["exclaim_rate"] == 0:
        parts.append("几乎不用感叹号")
    elif stats["exclaim_rate"] >= 0.3:
        parts.append("常用感叹号")
    if stats["ellipsis_rate"] >= 0.15:
        parts.append("频繁使用省略号")
    if stats["question_rate"] >= 0.25:
        parts.append("爱发问")
    if stats["self_refs"]:
        parts.append("自称：" + "、".join(stats["self_refs"][:3]))
    if stats["address_terms"]:
        parts.append("常称呼对方：" + "、".join(stats["address_terms"][:4]))
    return "；".join(parts)


def narrative_background(narration: Sequence[str], limit: int = 2, max_chars: int = 220) -> str:
    if not narration:
        return ""
    hints = ("是", "本是", "曾是", "原是", "身为", "自幼", "早年", "当年", "号称", "素有")
    scored = sorted(
        narration,
        key=lambda sentence: (-sum(1 for hint in hints if hint in sentence), -len(sentence)),
    )
    picked: list[str] = []
    total = 0
    for sentence in scored:
        if total + len(sentence) > max_chars:
            continue
        picked.append(sentence)
        total += len(sentence)
        if len(picked) >= limit:
            break
    return "".join(picked)[:max_chars]


# ---------------------------------------------------------------------------
# 校验（拦住编造角色）
# ---------------------------------------------------------------------------
def clean_alias(value: str) -> str:
    """别名必须是"名字"而不是描述性短语（「三楼东厢那位姑娘」不算）。"""
    alias = " ".join(str(value or "").split()).strip()
    if not (2 <= len(alias) <= 8):
        return ""
    if any(token in alias for token in ("的", "这", "那", "位", "个", "们", "他", "她")):
        return ""
    if is_identity_like(alias):
        return ""
    return alias


def merge_mutual_aliases(characters: Sequence[dict]) -> list[dict]:
    """两个角色互相把对方记成别名 —— 同一个人的两种叫法，合并成一个。

    只认「互相」引用（A 的别名里有 B，且 B 的别名里有 A），
    避免把独立昵称角色按单向别名误合并。
    """
    working = [dict(item) for item in characters]
    for _ in range(3):
        merged_any = False
        for index, left in enumerate(working):
            for right in list(working[index + 1:]):
                left_aliases = set(left.get("aliases") or [])
                right_aliases = set(right.get("aliases") or [])
                if not (right["name"] in left_aliases and left["name"] in right_aliases):
                    continue

                keep, drop = (left, right) if (
                    len(left["name"]), int(left.get("appearances") or 0)
                ) >= (len(right["name"]), int(right.get("appearances") or 0)) else (right, left)

                keep["aliases"] = list(
                    dict.fromkeys(
                        [
                            *(keep.get("aliases") or []),
                            *(drop.get("aliases") or []),
                            drop["name"],
                        ]
                    )
                )
                keep["aliases"] = [a for a in keep["aliases"] if a != keep["name"]][:6]
                keep["appearances"] = int(keep.get("appearances") or 0) + int(
                    drop.get("appearances") or 0
                )
                keep["quotes"] = list(
                    dict.fromkeys([*(keep.get("quotes") or []), *(drop.get("quotes") or [])])
                )[:8]
                working.remove(drop)
                merged_any = True
                break
            if merged_any:
                break
        if not merged_any:
            break
    return working


def validate_characters(
    characters: Sequence[dict],
    text: str,
    *,
    min_mentions: int = 1,
) -> tuple[list[dict], list[tuple[str, str]]]:
    """丢弃「不像人名」「原文里根本没出现」「出现次数太少」的角色。"""
    kept: list[dict] = []
    dropped: list[tuple[str, str]] = []
    seen: set[str] = set()

    for character in characters:
        name = str(character.get("name") or "").strip()
        if not name:
            continue
        if is_identity_like(name):
            dropped.append((name, "身份/组织词"))
            continue
        if text and name not in text:
            dropped.append((name, "原文中不存在（疑似编造）"))
            continue
        mentions = text.count(name) if text else 0
        if text and mentions < min_mentions:
            dropped.append((name, f"出现 {mentions} 次 < {min_mentions}"))
            continue

        aliases = [clean_alias(a) for a in (character.get("aliases") or [])]
        aliases = [alias for alias in aliases if alias]
        if text:
            aliases = [alias for alias in aliases if alias in text]
        character = dict(character)
        character["aliases"] = list(dict.fromkeys(aliases))[:6]
        character["appearances"] = mentions or character.get("appearances") or 0

        key = re.sub(r"\s", "", name).lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(character)

    return kept, dropped


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def mine_characters(
    text: str,
    *,
    max_characters: int = 20,
    min_mentions: int = 3,
    max_candidates: int = 40,
) -> tuple[list[dict], dict]:
    """从整本小说里挖角色。返回 (角色记录, 诊断信息)。"""
    text = (text or "").strip()
    if not text:
        return [], {"error": "空文本"}

    ranked, dropped = discover_names(
        text, min_mentions=min_mentions, max_names=max_candidates
    )
    if not ranked:
        return [], {"error": "没有发现任何角色名", "dropped": dropped[:20]}

    forms = [form for form, _score in ranked]
    canonicals, aliases = group_forms(text, forms)

    form_map: dict[str, str] = {}
    for canonical in canonicals:
        form_map[canonical] = canonical
        for alias in aliases.get(canonical, []):
            form_map[alias] = canonical

    attributed, stats = attribute_dialogues(text, form_map)
    partners = _cooccurrence(text, form_map)

    quotes_by: dict[str, list[str]] = defaultdict(list)
    for speaker, quote, _confidence in attributed:
        if len(quote) >= 2:
            quotes_by[speaker].append(quote)

    narration_by: dict[str, list[str]] = defaultdict(list)
    for sentence in split_sentences_text(text):
        cleaned = _QUOTE_RE.sub("", sentence).strip()
        if not cleaned or len(cleaned) > 180:
            continue
        for canonical in canonicals:
            names_here = {canonical, *aliases.get(canonical, [])}
            if any(name in cleaned for name in names_here):
                narration_by[canonical].append(cleaned)

    characters: list[MinedCharacter] = []
    for form, score in ranked:
        canonical = form_map.get(form, form)
        if any(character.name == canonical for character in characters):
            continue
        characters.append(
            MinedCharacter(
                name=canonical,
                aliases=[a for a in aliases.get(canonical, []) if a != canonical],
                quotes=quotes_by.get(canonical, [])[:8],
                narration=narration_by.get(canonical, [])[:6],
                appearances=text.count(canonical)
                + sum(text.count(a) for a in aliases.get(canonical, [])),
                score=round(score, 1),
                partners=partners.get(canonical, Counter()),
            )
        )

    characters.sort(key=lambda c: (-c.appearances, c.name))
    records = [character.as_record() for character in characters[:max_characters]]

    diagnostic = {
        "mode": "algo",
        "candidates": len(ranked),
        "characters": len(records),
        "quotes_total": len(_QUOTE_RE.findall(text)),
        "quotes_attributed": len(attributed),
        "attribution": stats,
        "aliases": {c["name"]: c["aliases"] for c in records if c["aliases"]},
        "dropped": [{"name": name, "reason": reason} for name, reason in dropped[:20]],
    }
    return records, diagnostic


def pick_reference_excerpts(
    chunks: Sequence[str], *, limit: int = 4, chars: int = 500
) -> list[str]:
    """从原文里均匀取几段，给 hybrid 模式做"补漏"参考。"""
    if not chunks:
        return []
    total = len(chunks)
    limit = max(1, min(limit, total))
    step = max(1, total // limit)
    picked: list[str] = []
    for index in range(0, total, step):
        snippet = chunks[index][:chars].strip()
        if snippet:
            picked.append(snippet)
        if len(picked) >= limit:
            break
    return picked
