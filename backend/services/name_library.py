"""人名库：判断一个字符串是不是人名、是中国人名还是外国人名。

纯本地数据 + 规则，零网络依赖。给三处共用：
1. 算法挖掘（character_miner）做候选过滤；
2. 角色档案管理员 Agent 的 ``name_check`` 工具（可批量校验/分类）；
3. 抽取收口时做最终校验。

数据覆盖：
* 中国单姓 ~460、复姓 ~90；
* 中文名字常用字 ~520；
* 音译常用字 ~180（识别「恺撒」「芬格尔」这类外来名）；
* 常见外国人名的中文译名：名字 ~150、姓氏 ~90（约翰、玛丽、史密斯、加图索…）；
* 称谓/职位/泛称 ~200（教授、船长、掌门、黑衣人…），用来排除非人名。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 一、中国姓氏
# ---------------------------------------------------------------------------
SURNAMES_SINGLE: frozenset[str] = frozenset(
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
    "丰巢关蒯相查后荆红游竺权逯盖益桓楚岳郗邝郏郜郐鄢麦覃"
)

SURNAMES_COMPOUND: frozenset[str] = frozenset(
    """
    欧阳 太史 端木 上官 司马 东方 独孤 南宫 万俟 闻人 夏侯 诸葛 尉迟 公羊
    赫连 澹台 皇甫 宗政 濮阳 公冶 太叔 申屠 公孙 慕容 仲孙 钟离 长孙 宇文
    司徒 鲜于 司空 闾丘 子车 亓官 司寇 巫马 公西 颛孙 壤驷 公良 漆雕 乐正
    宰父 谷梁 拓跋 夹谷 轩辕 令狐 段干 百里 呼延 东郭 南门 羊舌 微生 梁丘
    左丘 东门 西门 第五 邯郸 成公 仲长 吾丘 于陵
    """.split()
)

# ---------------------------------------------------------------------------
# 二、名字用字（中文）
# ---------------------------------------------------------------------------
GIVEN_CHARS: frozenset[str] = frozenset(
    "伟刚勇毅俊峰强军平保东文辉力明永健世广志义兴良海山仁波宁贵福生龙"
    "元全胜学祥才发武新利清飞彬富顺信子杰涛昌成康星光天达安岩中茂进林"
    "有坚和彪博诚先敬震振壮会思群豪心邦承乐绍功松善厚庆磊民友裕河哲江"
    "超浩亮政谦亨奇固之轮翰朗伯宏言若鸣朋斌梁栋维启克伦翔旭鹏泽晨辰士"
    "以建家致树炎德行时泰盛雄琛钧冠策腾楠榕风航弘秀娟英华慧巧美娜静淑"
    "惠珠翠雅芝玉萍红娥玲芬芳燕彩春菊兰凤洁梅琳素云莲真环雪荣爱妹霞香"
    "月莺媛艳瑞凡佳嘉琼勤珍贞莉桂娣叶璧璐娅琦晶妍茜秋珊莎锦黛青倩婷姣"
    "婉娴瑾颖露瑶怡婵雁蓓纨仪荷丹蓉眉君琴蕊薇菁梦岚苑婕馨瑗琰韵融园艺"
    "咏卿聪澜纯毓悦昭冰爽琬茗羽希欣飘育滢馥筠柔竹霭凝晓欢霄枫芸菲寒"
    "伊亚宜可姬舒影荔枝丽阳妮辰逸寒暄临澈野舟砚白墨清溯淮珩珏屿"
    "焕炜烈灿然熠熙煦晖曜曦晗晟晏昱晋昭晃昂昆昊"
    "沐沛泓泉洲润淇湘溪瀚洋涌浪澄渊湛"
    "柯桐梓榆柏杉楚棠樱权柳槐檀"
    "珍珊琪瑾璇璎珞瑛琮瑄璟"
    "频颜颂飒"
    "蕾蔷蕴萱莹萦荃"
    "翎翡翠羽缨绣绮绫缇"
    "锋锐铠铮钰铖鑫钦铭钧"
    "淮浦淳泠"
)

# ---------------------------------------------------------------------------
# 三、音译用字（外国人名/地名转写常用）
# ---------------------------------------------------------------------------
TRANSLIT_CHARS: frozenset[str] = frozenset(
    "阿埃艾爱安昂奥巴拜班邦鲍贝本比彼毕宾波伯勃博布查达戴丹道德登迪蒂丁"
    "东杜敦多厄恩尔法凡菲芬弗福傅盖甘冈戈格根古瓜哈海汉豪赫亨胡华霍基吉"
    "加贾坚杰捷金卡凯坎康考柯科克肯库夸拉莱赖兰朗劳勒雷蕾里理丽利连林隆"
    "卢鲁路伦罗洛马玛迈麦曼芒梅门蒙米密缅明摩莫墨姆穆纳娜奈南内尼涅宁纽"
    "努诺欧帕潘佩彭皮珀普奇齐乔切钦琼丘萨塞桑瑟森沙莎山尚舍申圣施什史斯"
    "松苏索塔泰坦汤唐特提汀图托瓦威韦维温沃乌伍西希锡夏歇谢辛休亚扬耶伊"
    "衣以义尤于约泽扎詹朱兹佐堡黛薇"
    # 日式/西式译名常见字（用于「亚纪」「美雪」这类两字译名）
    "纪优樱美雪奈凉凛澪遥结惠香织绫乃叶花月星斗翔翼陆晴音琴莉纱"
)

#: 常见外国人名的中文译名（名）
FOREIGN_GIVEN: frozenset[str] = frozenset(
    """
    约翰 杰克 詹姆斯 乔治 查尔斯 亨利 威廉 爱德华 托马斯 理查德 罗伯特 迈克尔
    大卫 彼得 保罗 马克 卢克 马修 马丁 安东尼 帕特里克 尼古拉斯 亚历山大 安德鲁
    克里斯托弗 丹尼尔 唐纳德 道格拉斯 埃里克 弗兰克 加里 戈登 哈罗德 霍华德
    伊恩 杰弗里 杰里米 乔纳森 朱利安 凯文 劳伦斯 莱昂纳多 路易斯 马尔科姆 奈杰尔
    奥利弗 奥斯卡 雷蒙德 罗杰 罗纳德 塞缪尔 西蒙 斯蒂芬 斯图尔特 特伦斯 维克多
    文森特 沃尔特 沃伦 阿瑟 阿尔伯特 阿尔弗雷德 伯纳德 克莱夫 德斯蒙德 埃德加
    埃德蒙 弗雷德里克 杰拉尔德 吉尔伯特 赫伯特 霍勒斯 莱斯利 莱昂内尔 诺曼 珀西
    拉尔夫 雷金纳德 西德尼 斯坦利 西奥多 蒂莫西 特雷弗 威尔弗雷德
    玛丽 安娜 安妮 伊丽莎白 凯瑟琳 玛格丽特 简 埃米莉 艾丽丝 艾米 安吉拉 芭芭拉
    贝弗莉 布伦达 卡罗尔 克里斯蒂娜 戴安娜 多丽丝 多萝西 伊迪丝 埃莉诺 埃拉
    弗洛伦斯 弗朗西丝 格蕾丝 海伦 希拉里 艾琳 伊莎贝尔 杰奎琳 琼 朱迪思
    朱莉娅 凯 凯特 劳拉 莉莲 琳达 露西 玛丽亚 玛莎 莫德 梅布尔
    娜塔莉 妮娜 奥利维亚 帕梅拉 保拉 波琳 佩内洛普 菲莉丝 雷切尔 丽贝卡 丽塔
    罗莎 露丝 莎拉 塞尔玛 索菲亚 斯特拉 苏珊 西尔维亚 特蕾莎 瓦莱丽 维拉 维奥拉
    薇拉 温迪 伊冯娜 佐伊 卡门 艾达 阿加莎 阿黛尔 贝拉 克拉拉 黛西 艾玛
    范妮 弗洛拉 格雷塔 海蒂 伊芙 莉娜 洛拉 玛雅 米娅 诺拉 奥尔加 塔玛拉 蒂娜
    乌苏拉 维奥莱特 万达 泽尔达
    恺撒 凯撒 亚瑟 兰斯洛特 高文 梅林 维克托 谢尔盖 德米特里 伊万 米哈伊尔
    安德烈 鲍里斯 弗拉基米尔 格里高利 列夫 尼古拉 奥列格 帕维尔 尤里
    汉斯 弗里茨 卡尔 海因茨 赫尔曼 克劳斯 库尔特 路德维希 奥托 瓦尔特 沃尔夫冈
    迪特里希 恩斯特 京特 约阿希姆 曼弗雷德 赖因哈德 鲁道夫 齐格弗里德
    让 皮埃尔 米歇尔 弗朗索瓦 路易 雅克 夏尔 阿兰 贝尔纳 克洛德
    埃蒂安 加斯东 热拉尔 纪尧姆 居伊 洛朗 马塞尔 莫里斯
    奥利维耶 帕斯卡尔 菲利普 雷蒙 勒内 罗贝尔 罗歇 塞尔日 斯特凡 蒂埃里
    何塞 胡安 卡洛斯 米格尔 安东尼奥 弗朗西斯科 曼努埃尔 佩德罗 拉斐尔 圣地亚哥
    迭戈 费尔南多 阿方索 恩里克 哈维尔 巴勃罗 劳尔 塞尔吉奥
    乔瓦尼 朱塞佩 马尔科 卢卡 马泰奥 弗朗切斯科 达里奥 恩里科 法比奥
    洛伦佐 皮耶罗 里卡多 罗伯托 斯特凡诺 维托里奥
    """.split()
)

#: 常见外国人名的中文译姓
FOREIGN_SURNAMES: frozenset[str] = frozenset(
    """
    史密斯 约翰逊 威廉姆斯 布朗 琼斯 加西亚 米勒 戴维斯 罗德里格斯 马丁内斯
    埃尔南德斯 洛佩兹 冈萨雷斯 威尔逊 安德森 托马斯 泰勒 摩尔 杰克逊 马丁
    汤普森 怀特 哈里森 克拉克 刘易斯 罗宾逊 沃克 佩雷斯 霍尔 艾伦
    赖特 斯科特 格林 贝克 亚当斯 纳尔逊 希尔 坎贝尔 米切尔 罗伯茨 卡特
    菲利普斯 埃文斯 特纳 托雷斯 帕克 柯林斯 爱德华兹 斯图尔特 弗洛雷斯 莫里斯
    墨菲 库克 罗杰斯 摩根 彼得森 库珀 里德 贝利 贝尔 凯利 沃德
    考克斯 迪亚兹 理查森 伍德 沃森 布鲁克斯 贝内特 格雷 雷耶斯 克鲁兹
    休斯 普赖斯 迈尔斯 福斯特 桑德斯 罗斯 莫拉莱斯 鲍威尔 沙利文 拉塞尔
    奥尔蒂斯 詹金斯 加图索 龙德施泰特 施耐德 曼施坦因 古德里安 隆美尔 布吕歇尔
    俾斯麦 兴登堡 鲁登道夫 邓尼茨 凯塞林 莫德尔 克莱斯特 保卢斯 舍尔 施佩尔
    麦克阿瑟 艾森豪威尔 巴顿 蒙哥马利 朱可夫 科涅夫
    司各特 洛克菲勒 卡内基 福特 爱迪生 特斯拉 牛顿 达尔文 莎士比亚
    丘吉尔 罗斯福 林肯 华盛顿 杰斐逊 富兰克林 汉密尔顿 肯尼迪
    伏尔泰 卢梭 雨果 巴尔扎克 大仲马 小仲马 莫泊桑 福楼拜 左拉 普鲁斯特
    托尔斯泰 陀思妥耶夫斯基 契诃夫 屠格涅夫 高尔基 普希金 果戈理 肖洛霍夫
    歌德 席勒 海涅 卡夫卡 里尔克 黑塞 茨威格
    但丁 彼特拉克 薄伽丘 卡尔维诺 埃科 莫拉维亚
    塞万提斯 洛尔迦 博尔赫斯 马尔克斯 略萨 聂鲁达 劳恩斯
    """.split()
)

# ---------------------------------------------------------------------------
# 四、称谓 / 职位 / 泛称（出现即大概率不是"角色名"）
# ---------------------------------------------------------------------------
HONORIFICS: tuple[str, ...] = (
    "先生", "小姐", "女士", "夫人", "太太", "大人", "阁下", "殿下", "陛下",
    "教授", "副教授", "讲师", "博士", "硕士", "校长", "院长", "主任", "部长",
    "局长", "队长", "组长", "班长", "会长", "社长", "主席", "书记", "经理",
    "董事长", "总裁", "老板", "掌柜", "东家", "少爷", "公子", "姑娘", "老者",
    "前辈", "后生", "老师", "同学", "学长", "学姐", "学弟", "学妹", "师兄",
    "师姐", "师弟", "师妹", "师父", "师傅", "弟子", "长老", "掌门", "宗主",
    "帮主", "城主", "族长", "将军", "上校", "中校", "少校", "上尉", "中尉",
    "少尉", "军士", "士官", "船长", "舰长", "大副", "二副", "三副", "水手",
    "船员", "机长", "车长", "警长", "警官", "探长", "法官", "律师",
    "医生", "大夫", "护士", "神父", "牧师", "修士", "修女", "骑士", "爵士",
    "男爵", "子爵", "伯爵", "侯爵", "公爵", "亲王", "王子", "公主", "国王",
    "女王", "皇帝", "皇后", "太子", "贵妃", "总管", "管家", "侍卫",
    "护卫", "家丁", "侍女", "丫鬟", "小厮", "伙计", "小二", "店家",
)

IDENTITY_WORDS: frozenset[str] = frozenset(
    """
    掌门 长老 宗主 帮主 城主 族长 皇帝 太子 公主 王子 将军 士兵 弟子 师父
    师兄 师姐 师弟 师妹 公子 姑娘 小姐 少爷 老爷 夫人 大人 阁下 前辈 先生
    后生 少年 少女 老者 老丈 妇人 女子 男子 众人 掌柜 小二 丫鬟 侍女 侍卫
    护卫 家丁 管家 总管 大夫 郎中 书生 秀才 高手 强者 修士 道友 魔头 妖女
    教授 校长 院长 主任 导师 老师 同学 学长 学姐 学弟 学妹 部长 局长 队长
    组长 班长 会长 社长 主席 船长 舰长 大副 二副 经理 老板 董事长 秘书
    医生 护士 警官 警长 法官 律师 神父 牧师 骑士 佣兵 队员 教练 水手 船员
    男人 女人 男孩 女孩 孩子 小孩 老人 家伙 所有人 每个人 有人 别人
    黑衣人 蒙面人 白衣人 青衣人 灰衣人 店主 摊主 车夫 马夫 仆人 侍者
    龙王 龙族 魔族 妖族 神族 人族 精灵 恶魔 天使 学院 学校 公司 国家
    """.split()
)

#: 「老X / 小X / 阿X」里的常见非人名
NICK_STOPWORDS: frozenset[str] = frozenset(
    """
    老家 老人 老板 老师 老婆 老公 小孩 小心 小时 小伙 小弟 小妹 小姐 小队
    小组 小丑 小区 小费 老早 老远 老外 老总 老乡 老子
    """.split()
)

BAD_TAILS: frozenset[str] = frozenset("的地然着了们上下里中外边面前后间")
BAD_CHARS: frozenset[str] = frozenset("了的着是在有这那个和就都也我你他她它把被从对与并且但所之其")
_PRONOMINAL: frozenset[str] = frozenset(
    "我 你 他 她 它 我们 你们 他们 她们 它们 大家 自己 咱们".split()
)

#: 常见非人名词（虚词/常用词/地名/时间词）
STOP_WORDS: frozenset[str] = frozenset(
    """
    可以 不要 应该 或者 不知 不会 不能 只是 已经 自己 这样 那样 什么 怎么
    为什么 一个 一些 一切 可能 需要 所以 但是 而且 并且 只有 只要 虽然 如果
    因为 于是 然后 终于 立刻 马上 时候 小时 学生 眼睛 一次 一天 一年 一会
    地方 问题 事情 东西 感觉 样子 意思 办法 机会 时间 声音 名字 世界 城市
    学校 公司 国家 社会 中国 日本 美国 英国 法国 德国 俄国 欧洲 亚洲
    北京 上海 东京 纽约 地铁 电梯 手机 电脑 汽车 飞机 火车 医院 银行 房间
    教室 食堂 图书馆 办公室 宿舍 厨房 客厅 走廊 地下 通道 朋友 家人 父母
    老家 小心 玩笑 一愣 一下 一直 一样 一边 一点 一阵 一番 双手 水下 水流
    水面 通过 游戏 任何人 其他 其实 之后 以前 以后 刚才 一定 心里 眼前
    """.split()
)


# ---------------------------------------------------------------------------
# 五、判定
# ---------------------------------------------------------------------------
class NameVerdict:
    """一个名字的判定结果。"""

    def __init__(
        self,
        name: str,
        score: float,
        reasons: list[str],
        *,
        kind: str = "unknown",
        core: str = "",
        surname: str = "",
        given: str = "",
        honorific: str = "",
    ) -> None:
        self.name = name
        self.score = round(score, 3)
        self.reasons = reasons
        self.kind = kind          # cn | foreign | nickname | unknown
        self.core = core or name  # 去掉称谓后的核心名
        self.surname = surname
        self.given = given
        self.honorific = honorific

    @property
    def plausible(self) -> bool:
        return self.score >= 0.5

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "score": self.score,
            "plausible": self.plausible,
            "surname": self.surname,
            "given": self.given,
            "honorific": self.honorific,
            "core": self.core,
            "reasons": self.reasons,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NameVerdict {self.name} {self.kind} {self.score}>"


def strip_honorific(name: str) -> tuple[str, str]:
    """剥掉尾部的称谓：『王教授』-> (王, 教授)、『路明非同学』-> (路明非, 同学)。"""
    for honorific in sorted(HONORIFICS, key=len, reverse=True):
        if len(name) > len(honorific) and name.endswith(honorific):
            return name[: -len(honorific)], honorific
    return name, ""


def match_cn_surname(name: str) -> tuple[str, str]:
    """返回 (姓氏, 'compound'|'single'|'')。"""
    for surname in sorted(SURNAMES_COMPOUND, key=len, reverse=True):
        if name.startswith(surname) and len(name) > len(surname):
            return surname, "compound"
    if name and name[0] in SURNAMES_SINGLE and len(name) > 1:
        return name[0], "single"
    return "", ""


def translit_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for char in text if char in TRANSLIT_CHARS) / len(text)


def analyze(name: str, *, text: str = "") -> NameVerdict:
    """给名字打分并分类。score >= 0.5 视为"像人名"。

    kind:
      cn        中国人名（有姓）
      foreign   外国人名的中文译名（含间隔号 / 音译用字 / 在译名表里）
      nickname  老X、小X、阿X 这类称呼
      unknown   判断不出来
    """
    raw = (name or "").strip()
    reasons: list[str] = []

    if not raw:
        return NameVerdict(raw, 0.0, ["空字符串"])
    if raw in _PRONOMINAL or raw in IDENTITY_WORDS:
        return NameVerdict(raw, 0.0, ["代词/身份词/职位"])
    if len(raw) < 2:
        return NameVerdict(raw, 0.0, ["长度不足 2 字"])
    if len(raw) > 12:
        return NameVerdict(raw, 0.0, ["过长（>12 字）"])
    if raw in STOP_WORDS:
        return NameVerdict(raw, 0.0, ["常见非人名词"])
    if any(char in BAD_CHARS for char in raw):
        return NameVerdict(raw, 0.1, ["含虚词"])
    if raw[-1] in BAD_TAILS:
        return NameVerdict(raw, 0.15, ["以方位/副词结尾"])
    if re.fullmatch(r"[0-9A-Za-z·\-.]+", raw) and "·" not in raw:
        return NameVerdict(raw, 0.2, ["纯拉丁/数字"])

    core, honorific = strip_honorific(raw)
    if honorific:
        reasons.append(f"带称谓「{honorific}」")
    if core in IDENTITY_WORDS or core in _PRONOMINAL:
        return NameVerdict(raw, 0.0, ["称谓+身份词"], honorific=honorific, core=core)

    # ---------- 1. 带间隔号：外国人名 ----------
    if "·" in core:
        parts = [part for part in core.split("·") if part]
        # 允许「冯·施耐德」这种 1 字介词（冯/范/德），但至少要有一段像名字
        if (
            len(parts) >= 2
            and all(1 <= len(part) <= 8 for part in parts)
            and any(len(part) >= 3 for part in parts)
        ):
            score = 0.85 if len(parts) == 2 else 0.9
            reasons.append(f"音译名（{len(parts)} 段，含间隔号）")
            if any(part in FOREIGN_GIVEN or part in FOREIGN_SURNAMES for part in parts):
                reasons.append("名字在外国人名译名表里")
            return NameVerdict(
                raw, score, reasons, kind="foreign", core=core,
                surname=parts[-1], given=parts[0], honorific=honorific,
            )
        return NameVerdict(raw, 0.3, ["间隔号位置异常"], honorific=honorific, core=core)

    # ---------- 2. 整体命中外国人名译名表（优先于中国姓氏）----------
    # 「古德里安」以「古」开头，但它整体是德国姓 Guderian，应判为外国译名
    if core in FOREIGN_GIVEN or core in FOREIGN_SURNAMES:
        return NameVerdict(
            raw, 0.85, [*reasons, "在外国人名译名表里"], kind="foreign",
            core=core, given=core, honorific=honorific,
        )

    # ---------- 3. 中文姓氏 ----------
    surname, surname_kind = match_cn_surname(core)
    given = core[len(surname):] if surname else core

    # ---------- 4. 昵称形态（老X / 小X / 阿X）----------
    if (
        not surname
        and len(core) == 2
        and core[0] in "老小阿"
        and core not in NICK_STOPWORDS
        and core not in STOP_WORDS
    ):
        common_chars = sum(1 for char in given if char in GIVEN_CHARS)
        score = 0.3 + 0.2 + min(0.25, 0.12 * common_chars)
        reasons.append("老/小/阿 称呼形态")
        if common_chars:
            reasons.append(f"名字用字常见（{common_chars} 字）")
        return NameVerdict(
            raw, score, reasons, kind="nickname", core=core,
            given=given, honorific=honorific,
        )

    # ---------- 5. 音译外国名（无中国姓氏）----------
    translit = translit_ratio(core)
    if not surname and len(core) >= 3 and translit >= 0.8:
        return NameVerdict(
            raw, min(0.45 + 0.3 * translit, 0.95),
            [*reasons, f"音译用字占比 {translit:.0%}"],
            kind="foreign", core=core, given=core, honorific=honorific,
        )
    if not surname and len(core) == 2 and translit >= 1.0:
        return NameVerdict(
            raw, 0.55, [*reasons, "两字均为音译用字"],
            kind="foreign", core=core, given=core, honorific=honorific,
        )

    # ---------- 6. 中文人名评分 ----------
    score = 0.0
    if surname:
        score += 0.45 if surname_kind == "compound" else 0.35
        reasons.append(f"{'复姓' if surname_kind == 'compound' else '单姓'}「{surname}」")
    else:
        if translit >= 0.5 and len(core) >= 3:
            score += 0.2
            reasons.append(f"含音译用字（{translit:.0%}）")
        else:
            reasons.append("无常见姓氏")

    if 1 <= len(given) <= 2:
        score += 0.2
        reasons.append("名的长度正常")
    elif len(given) == 3:
        score += 0.05
        reasons.append("名为 3 字（较少见）")
    elif len(given) == 0:
        return NameVerdict(raw, 0.1, ["只有姓，没有名"], honorific=honorific, core=core)
    else:
        reasons.append("名字过长")

    common = sum(1 for char in given if char in GIVEN_CHARS)
    if common:
        score += min(0.25, 0.12 * common)
        reasons.append(f"名字用字常见（{common} 字）")

    # 原文出现次数（弱证据，由调用方传入 text 时才计算）
    if text:
        mentions = text.count(core)
        if mentions == 0:
            score -= 0.5
            reasons.append("原文中不存在")
        elif mentions >= 20:
            score += 0.1
            reasons.append(f"原文出现 {mentions} 次")
        elif mentions >= 5:
            score += 0.05

    score = max(0.0, min(1.0, score))
    kind = "cn" if surname else "unknown"
    return NameVerdict(
        raw, score, reasons, kind=kind, core=core,
        surname=surname, given=given, honorific=honorific,
    )


def lookup(name: str, *, text: str = "") -> dict:
    """给 Agent 用的富结果（除分数外还告诉它"为什么"）。"""
    verdict = analyze(name, text=text)
    return {
        **verdict.as_dict(),
        "in_cn_surname": verdict.surname or "",
        "in_foreign_given": verdict.core in FOREIGN_GIVEN,
        "in_foreign_surname": verdict.core in FOREIGN_SURNAMES,
        "translit_ratio": round(translit_ratio(verdict.core), 2),
    }


def batch_lookup(names: list[str], *, text: str = "") -> list[dict]:
    return [lookup(name, text=text) for name in names]


def is_plausible(name: str, *, text: str = "", threshold: float = 0.5) -> bool:
    verdict = analyze(name, text=text)
    return verdict.plausible and verdict.score >= threshold


def suggest_corrections(name: str) -> list[str]:
    """给一个名字提几种可能的"更像人名"的写法（修正切词/错字）。"""
    name = (name or "").strip()
    if not name:
        return []
    candidates: list[str] = []
    if len(name) > 2:
        candidates.extend([name[:-1], name[1:]])
    if len(name) > 3:
        candidates.append(name[:3])
    return [candidate for candidate in dict.fromkeys(candidates) if is_plausible(candidate)]


def library_stats() -> dict:
    """给人看的规模统计（也方便自检）。"""
    return {
        "cn_single_surnames": len(SURNAMES_SINGLE),
        "cn_compound_surnames": len(SURNAMES_COMPOUND),
        "given_chars": len(GIVEN_CHARS),
        "translit_chars": len(TRANSLIT_CHARS),
        "foreign_given_names": len(FOREIGN_GIVEN),
        "foreign_surnames": len(FOREIGN_SURNAMES),
        "honorifics": len(HONORIFICS),
        "identity_words": len(IDENTITY_WORDS),
    }