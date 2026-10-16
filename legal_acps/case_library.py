"""真实案例库 —— 让「定期研判」跑的是真案子，而不是空 ping（#27，2026-10-05）。

存在理由
--------
本项目在平台上积累的**访问量是时间的函数**：停一天，就永久少一天。但"为了访问量
而定时空跑一轮"有两个说不通的地方：

  · **答不了辩**。"我们跑了 300 次"什么都证明不了；而空跑的输入永远一模一样，
    链路实际上只被验证过**一条**（而且是最容易的那一条）。
  · **测不出退化**。主控 → 事实核查 → 正/反辩手 → 裁定 → 证据鉴证这条链，
    其价值恰恰在于**输入变了它还得对**。

于是改成：**用公开的真实案例，定期跑通一次完整链路**。一轮同时做三件事 ——

  1. 真的产生平台访问（不是 ping，是带内容的完整研判）；
  2. 反复把整条链压到**不同的**真实输入上，任何一环退化都会被看见；
  3. 沉淀成**答辩素材**：每个案子都挂着公开出处，可以直接引用。

为什么案例必须**真实、且有出处**
--------------------------------
自编的案子没有正确答案，也没有公开出处 —— 一旦被问"你这个案子哪来的"就无话可说。
更要紧的是：**自编案子会悄悄适配我们自己的实现**（写案子的人天然知道系统能答什么、
不能答什么），于是它恰好测不出系统的盲区。所以本库每一条都取自**公开报道或官方
通报里的真实事件**，`source` 字段记明出处，`focus` 字段写明它想压住哪个法律争点。

轮换：**从台账现场派生**，不设游标文件
--------------------------------------
「下一个该跑哪个案子」这个问题，答案只能来自一个地方：**已经跑过什么**。

最自然的实现是写一个游标文件（`case_cursor.txt`），但它会引入**第二个事实源**：
台账 `runs.jsonl` 里如实记着每一轮，而游标文件是另抄的一份计数。两者一旦不同步
—— 手工补跑了一轮、游标文件写失败、或者有人从备份恢复了一份旧游标 —— 结果就是
**同一个案子连着跑两遍、另一个案子永远轮不到**，而且**没有任何东西会报错**。
（本项目纪律：**同一判定写两处必然漂移**，而漂移的那一半恰好是没人盯着的那一半。）

所以轮换**读台账现场算**：`plan_next()` 数出每个案子跑过几轮，取**跑得最少**的那个。
台账是 append-only 的流水账，本来就必须写；轮换不额外维护任何状态，也就不存在
"两个事实源对不上"这回事。

    ⚠️ 台账里**认得**的 id 与案例库里**收不收**的 id，必须现场读同一张表：
       台账里出现过、本库却没有的 id **不静默忽略**，而是从 `RotationPlan.unknown`
       里如实报出来 —— 删掉一个案子之后，它在历史轮次里留下的痕迹必须看得见。
       （本项目纪律：**「认得」与「收不收」必须现场读同一张表，且必须有负对照。**）

标签格式就是那座桥
------------------
一轮案例研判在台账里的 `label` 是 `case:<id>`（`CASE_ID_PREFIX` + `CaseDef.id`）。
`case_id_from_label()` 是**唯一的**反解实现：轮换、体检、自检三处都调它，**不各写
一份正则** —— 否则"改了格式忘了改解析"会让轮换**永远认不出任何一轮**，
而它的表现是"每个案子都是 0 轮"：**看上去像一切正常**。

"跑了几轮"与"这一轮成没成"是两件事
----------------------------------
轮换只问前者，所以它数的是**台账里记着的轮次**——准确地说，**不同 `run_id` 的个数**
（台账里一轮是开/收两行，按 `run_id` 折叠后算一轮；数行数会把每一轮都算成两轮）。
一轮被 kill 掉也算数：那一份案例材料**已经被喂进链路了**。
至于"这一轮有没有真的产出裁定书"，那是 `case_round` 的**四项体检**要回答的问题，
与本模块无关 —— 两个问题分开问，才不会用一个的答案去顶另一个。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .run_ledger import DEFAULT_ROLE, RunRecord, ledger_path, read_runs

__all__ = [
    "CASE_ID_PREFIX",
    "CASES",
    "MAX_TEXT_CHARS",
    "MIN_CATEGORIES",
    "MIN_SOURCE_CHARS",
    "MIN_TEXT_CHARS",
    "CaseDef",
    "RotationPlan",
    "case_id_from_label",
    "case_label",
    "categories",
    "get_case",
    "plan_next",
    "plan_next_from_ledger",
    "render_library",
    "render_plan",
    "self_check",
]

#: 案例轮次在台账里的 `label` 前缀。**唯一事实源**：写（`case_label`）与读
#: （`case_id_from_label`）都用它，`case_round` 与自检也引用它，不各写一份字面量。
CASE_ID_PREFIX = "case:"

#: 案例 id 的合法形态。**首字符必须是字母**，且**不许含冒号**（冒号是前缀分隔符）。
#: 之所以卡这么死：id 会直接拼进 `label`，而 label 会被写进 JSONL 与 shell 参数，
#: 一个宽松的 id（带空格、带引号、带 `/`）能在三个地方各自坏出一种样子。
CASE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,}$")

#: 输入文本的下限。低于它就不是一次真实研判，而是 ping —— 那正是本模块要消灭的东西。
MIN_TEXT_CHARS = 60

#: 输入文本的上限。防的是"把一整篇报道粘进 prompt"：主控的步数预算只有 14 步，
#: 输入里带上大段无关上下文只会让它在事实核查上更快地把预算烧光。
MAX_TEXT_CHARS = 1200

#: `source` 的下限。它必须能指名道姓（哪家通报、哪家报道、什么日期），
#: 一个 `"网警"` 这样的字符串等于没写。
MIN_SOURCE_CHARS = 12

#: 类别数下限。这是**多样性**的底线，不是对当前条数的描述 ——
#: 案例太少、或者全挤在同一个法律争点上，轮换就退化成"把同一条链路跑 N 遍"。
MIN_CATEGORIES = 5


@dataclass(frozen=True)
class CaseDef:
    """一条真实案例。**五个字段都不可省**，理由见模块头。"""

    #: 台账 label 里那一段（`case:<id>`）。稳定不变 —— 改了它等于把历史轮次弄丢。
    id: str
    #: 法律争点分类（用于看多样性与挑案子时的过滤）。
    category: str
    #: 一句话标题（人看的）。
    title: str
    #: **喂给主控的输入原文**（`run_agent leader -i <text>`）。这是本模块的交付物本体。
    text: str
    #: 这条案例想压住的**法律争点**。写在案子里，因为它是"为什么挑它"的理由。
    focus: str
    #: 公开出处。**不许含糊**：发布方 + 事件/通报名目 + 新闻源 + 日期。
    source: str


# ---------------------------------------------------------------------------
# 案例本体
# ---------------------------------------------------------------------------
#
# 取材原则（写新案子时照着做）：
#   · 只收**官方通报或主流媒体报道过**的事件，`source` 里写清是哪一家、哪一天；
#   · `text` 写成**当事人视角的提问**，而不是新闻摘要 —— 主控收到的是用户的咨询，
#     让它去核查"这条消息是不是真的"、再去做法律定性，才是它该干的活；
#   · `text` 里**只放传闻本身**，不放处罚结果 —— 结论要让链路自己跑出来，
#     把答案写进输入就等于把判据自己的期望塞进被测输入（本项目明令禁止）；
#   · `focus` 写**争点**，不写"正确答案"。
#
# ⚠️ 取材时**核对过原始报道**，不是凭印象写的。改文案时同样要核对，
#    否则 `source` 会变成一句"看上去有出处"的注释 —— 那比没有出处更糟。

CASES: tuple[CaseDef, ...] = (
    CaseDef(
        id="c-food-01",
        category="食品安全",
        title="矿泉水被造谣为「泡尸水」",
        text=(
            "前两天刷到一个视频，有人拍了某矿泉水厂的大门，配文说「这家矿泉水厂今年 3 月份"
            "死了一个人，6 月份又死了一个，那两个人捞上来的时候只剩一架骨头，那些水全被"
            "老百姓喝了」。评论区好几万条转发，还有人说本地超市已经下架了这款水。"
            "我想确认两件事：一是这话到底有没有依据；二是如果纯属编造，发视频的人要承担"
            "什么责任？我只是转发到家人群、没加任何评论，会不会也被追究？"
        ),
        focus=(
            "编造虚假食品安全信息的行政责任与刑事门槛；"
            "「仅转发未加评论」是否构成散布；被造谣企业的名誉权救济路径"
        ),
        source=(
            "公安部网安局通报《造谣某矿泉水为「泡尸水」，网警依法查处》"
            "（云南临沧耿马，曹某某被行政处罚）；中国新闻网 2025-08-29"
        ),
    ),
    CaseDef(
        id="c-food-02",
        category="食品安全",
        title="编造中学食堂致学生腹泻",
        text=(
            "家长群里在传一条消息，说我们县某中学食堂的饭菜让一批学生吃坏了身体、"
            "出现腹泻，还配了几张菜盆的照片，说是「后厨现场」。有家长已经准备去学校"
            "讨说法了。我想知道：这条消息如果查下来是假的，最初编造并发布的人要负"
            "什么法律责任？学校能不能追究他的名誉权？另外，我作为家长把这条消息转到"
            "群里，需要担责吗？"
        ),
        focus=(
            "校园食品安全类谣言的定性；「造成大量家长恐慌、破坏正常教学」如何折算为"
            "「扰乱公共秩序」；学校作为被侵权主体的救济；家长转发的责任边界"
        ),
        source=(
            "公安部网安局通报「校园食品安全问题不是可以随意造谣的」"
            "（甘肃天水武山县，王某编造「某中学食堂饭菜导致部分学生腹泻」被行政处罚）；"
            "南方都市报 / 南都N视频 2025-12-02"
        ),
    ),
    CaseDef(
        id="c-water-01",
        category="饮用水",
        title="自来水异味后伪造「警情通报」称粪水排污",
        text=(
            "我们这边前几天自来水有一股异味，官方说是气候原因导致藻类厌氧降解产生的"
            "硫醚类物质，已经切换水源、水质安全。但紧接着网上流传出一份「警情通报」，"
            "写着主城区有 13 处水路接驳了下水粪水排污管网。这份通报看起来格式很正规，"
            "还带着警徽。我想问：这份通报是不是伪造的？伪造并传播官方警情通报，在法律上"
            "怎么定性、后果有多重？"
        ),
        focus=(
            "在真实突发事件上伪造官方通报的定性（编造虚假警情 / 严重扰乱社会秩序）；"
            "「仿真度」与「官方权威被冒用」如何影响从重认定；真事件与假信息交织时的核查顺序"
        ),
        source=(
            "公安部网安局通报《编造虚假「警情通报」，捏造「粪水」谣言，邵某豪被刑事拘留》"
            "（浙江杭州余杭区，邵某豪被依法刑事拘留）；央广网 2025-07-22"
        ),
    ),
    CaseDef(
        id="c-quake-01",
        category="自然灾害",
        title="伪造院士名头预测大地震",
        text=(
            "家族群里转发了一张聊天截图，说某院士预测广东将发生 8 级以上大地震。"
            "截图里有院士的名字和他的头像，看起来像是在某个工作群里发出来的。"
            "好多亲戚已经开始收拾东西准备回老家了。我想确认：这个预测有没有科学依据？"
            "发布这张截图的人，和后面转发的人，分别要承担什么责任？"
        ),
        focus=(
            "假借真实专家名义编造地震预测的定性；"
            "**编造者与传播者的责任差异**（后者是「未核实真实情况即传播」）；"
            "被冒名的院士本人的名誉权与救济"
        ),
        source=(
            "公安部网安局通报《造谣传谣「院士预测广东将发生 8 级以上大地震」，4 人被依法查处》"
            "（罗某良编造，匡某、胡某成、张某传播，4 人均被行政处罚）；央广网 2025-07-11"
        ),
    ),
    CaseDef(
        id="c-quake-02",
        category="自然灾害",
        title="反复发布虚假「地震预警」",
        text=(
            "网上有人说「云南、四川今夜或明天有 5 级左右地震」，还有一条说"
            "「2025 年 4—6 月四川将发生 7.6 级地震」。我们这边好多人晚上不敢回家住。"
            "这种发布地震「预警」的行为算不算违法？如果发帖人只是「为了提醒大家注意安全」，"
            "主观上并不是恶意，还能不能免责？"
        ),
        focus=(
            "虚假地震预警的定性；**「动机善意」能否出罪**（主观目的与客观后果的关系）；"
            "同一人反复发布如何影响情节认定；权威预警信息的发布主体资格"
        ),
        source=(
            "公安部网安局通报（李某某为博取关注两次发布所谓「地震预警」信息，"
            "云南楚雄公安机关依法予以行政处罚）；中工网 / 中国新闻网 2025-07-11"
        ),
    ),
    CaseDef(
        id="c-blast-01",
        category="安全生产",
        title="用 AI 凭空生成「烟花厂爆炸 2 死 2 伤」",
        text=(
            "我在短视频平台刷到一条视频，画面是一片废墟、浓烟滚滚，字幕写着"
            "「江西萍乡上栗县发生烟花厂爆炸事故，造成 2 死 2 伤」。评论区一片恐慌。"
            "我打电话问了在上栗的朋友，他说当地一切正常。这条视频是什么来路？"
            "用 AI 工具生成这种虚假事故视频并发布，要承担什么法律后果？转发的人呢？"
        ),
        focus=(
            "AI 生成虚假事故内容的定性；**「有图有真相」错觉对传播量的影响**；"
            "生成工具使用者的注意义务与标识义务；虚假安全生产信息的公共安全风险"
        ),
        source=(
            "央视网《真相来了丨为博眼球使用 AI 造谣「烟花厂爆炸」 男子被处罚》"
            "（江西萍乡上栗县，阮某某用 AI 工具生成虚假事故视频被行政处罚）；"
            "人民公安报 2026-04-02，央广网 2026-03-22"
        ),
    ),
    CaseDef(
        id="c-blast-02",
        category="安全生产",
        title="以新闻口吻编造民房爆炸事故",
        text=(
            "看到一篇写得很像新闻的文章，标题是「某地一民房发生爆炸事故，4 人不幸遇难，"
            "官方紧急介入调查」，正文里说发现了 4 名遇难者，其中包括一名儿童。"
            "文章没有署名记者，也没有写明具体时间地点。这种以新闻报道口吻发布的"
            "爆炸事故消息，如果查证是编造的，法律责任是什么？和那种一眼就能看出"
            "是谣言的消息相比，处理上有区别吗？"
        ),
        focus=(
            "**冒充新闻报道体例**编造虚假信息如何影响定性与从重情节；"
            "「没有具体时间地点」对可核查性与危害后果认定的影响；"
            "平台对无署名、无信源内容的审核义务"
        ),
        source=(
            "公安部 2024-04-12 公布 10 起打击整治网络谣言违法犯罪典型案例"
            "（第八起：重庆网民康某余使用人工智能工具编造「某地一民房发生爆炸事故、"
            "4 人不幸遇难」谣言文章，被行政拘留 3 日）；中国新闻网 2024-04-12"
        ),
    ),
    CaseDef(
        id="c-police-01",
        category="公共秩序",
        title="真事故被配上假「警情通报」",
        text=(
            "一起真实的交通事故发生后，网上很快出现了一份「警情通报」，说这是驾车撞人的"
            "重大恶性案件，造成 131 人死亡、337 人受伤。后来交警部门发了真实通报，"
            "实际是 2 人死亡、4 人轻伤、5 人轻微伤。两份通报的差距太大了。我想问："
            "在一个真实事件上编造伤亡数字、并伪造官方通报，是不是比凭空造一个事故更严重？"
            "法律依据是什么？为什么伪造的通报表现在越来越多？"
        ),
        focus=(
            "**真实事件 + 虚假情节**与**纯虚构事件**在定性与情节上的差异；"
            "伪造官方通报的仿真度（PS / AI / 现成模板）对危害后果的放大；"
            "权威信息迟滞与谣言滋生的因果关系（不构成免责，但影响治理路径）"
        ),
        source=(
            "中国经济网《10 天内连发 3 起伪造警情通报事件 专家建议平台对自媒体发布"
            "官方通报应及时预警核实》（昆明交通事故，白某某、陈某某传播伪造通报被刑拘）；"
            "2025-08-01"
        ),
    ),
    CaseDef(
        id="c-bank-01",
        category="金融秩序",
        title="编造「某银行倒闭」引发储户挤兑",
        text=(
            "有人在几个微信群里发「某银行倒闭了」「钱趁早去取出来」，结果第二天网点门口"
            "排了很长的队，听说有 1500 多名储户去集中取款。后来这个人被行政拘留了。"
            "我想问：为什么编造银行倒闭的谣言，比一般的造谣处理得重？「扰乱公共秩序」"
            "在金融领域是怎么认定的？另外，如果我在群里看到这种消息，应该怎么核实？"
        ),
        focus=(
            "涉金融谣言的**后果认定**（挤兑、集中兑付如何折算为严重扰乱公共秩序）；"
            "行政责任与刑事责任的界分；金融机构与监管方的辟谣义务；"
            "普通人对金融类传闻的核实路径"
        ),
        source=(
            "公安部 2024-04-12 公布 10 起打击整治网络谣言违法犯罪典型案例"
            "（第九起：江西赣州网民廖某慧在多个微信群编造「某银行倒闭」等谣言，"
            "致 1500 余名储户集中兑付取款，被行政拘留 5 日）；凤凰网江西 2024-04-12"
        ),
    ),
    CaseDef(
        id="c-delivery-01",
        category="民生服务",
        title="组织兼职用 AI 批量生成「限制外卖配送」",
        text=(
            "我刷到一条消息，说「广州即将成为全国首个限制外卖配送的城市」，很快就上了热搜。"
            "后来才知道，是一个外省的网民组织了一批兼职的人，用人工智能工具批量生成不实文章"
            "来引流牟利。我想问：这种「组织 + 批量生成」的模式，和一个人随手发一条谣言，"
            "在法律定性上有什么不同？被雇来发文的人需要负责吗？平台要不要承担连带责任？"
        ),
        focus=(
            "**组织化、规模化造谣**与个体造谣的定性差异（共同犯罪 / 情节严重程度）；"
            "被招募的兼职人员的责任与「不知情」抗辩；AI 批量生成对传播量的放大效应；"
            "平台责任与「引流牟利」的违法所得认定"
        ),
        source=(
            "公安部 2024-04-12 公布 10 起打击整治网络谣言违法犯罪典型案例"
            "（第十起：四川德阳网民杨某组织招揽多名兼职人员用 AI 批量生成不实文章，"
            "编造「广州即将成为全国首个限制外卖配送的城市」，6 人被采取刑事强制措施）；"
            "中国新闻网 2024-04-12"
        ),
    ),
    CaseDef(
        id="c-deepfake-01",
        category="深度伪造",
        title="AI 换脸动态视频骗过人脸核验激活电话卡",
        text=(
            "我在网上看到有人在卖「AI 换脸」做出来的动态视频，说是可以让一张别人的身份证"
            "照片眨眼、张嘴，用来通过运营商的人脸核验、把电话卡激活。据说有个三人小团伙"
            "一年激活贩卖电话卡 368 张，造成的群众财产损失超过 600 万元，最后三个人都被判了刑。"
            "我不太理解：他们只是提供了「技术」，并没有亲自去骗人，为什么也要承担刑事责任？"
        ),
        focus=(
            "**技术帮助行为的独立可罚性**（帮助信息网络犯罪活动罪）；"
            "帮助行为与下游诈骗结果之间的关联性证明；人脸信息作为敏感个人信息的刑法保护；"
            "技术中立抗辩的边界"
        ),
        source=(
            "最高人民检察院《湖北南漳：查办利用「换脸」视频骗过人脸检验系统的新型犯罪》"
            "（陈某、肖某、秦某三人以帮助信息网络犯罪活动罪分别获刑一年二个月至一年八个月）；"
            "spp.gov.cn 2025-08-26，湖北日报 2025-08-11"
        ),
    ),
    CaseDef(
        id="c-deepfake-02",
        category="深度伪造",
        title="AI 换脸 + 声纹克隆冒充亲人骗走 50 万",
        text=(
            "看到一条提醒，说有位女士接到「儿子」的视频通话，对方让她转钱认购内部股权，"
            "画面里的样貌和声音跟她儿子一模一样，她没核实就转了 50 万元，事后才发现"
            "儿子的账号被盗、视频是用 AI 换脸加声纹克隆做出来的。我想知道：这类案件的"
            "追责和举证难点在哪里？被盗号的人、平台、转账的人各有什么责任？"
            "有没有普通人能用的办法判断一段视频是不是伪造的？"
        ),
        focus=(
            "深度伪造诈骗的追责与**举证难点**（伪造属性需要技术鉴伪）；"
            "被盗号者的责任边界；平台的账号安全义务；"
            "生物特征信息被滥用时的被害人过错认定；鉴伪在证据法上的地位"
        ),
        source=(
            "公安部网安局提示（AI 深度伪造视频通话、声纹克隆冒充亲属骗走 50 万元）；"
            "法治网 2026-03-19，人民网《当「眼见」不再「为实」，如何守住 AI 换脸安全防线》"
            "2026-05-30"
        ),
    ),
    CaseDef(
        id="c-staged-01",
        category="网络摆拍",
        title="「小学生在巴黎丢寒假作业」系自导自演",
        text=(
            "前段时间有一条特别火的视频，说是一个小学生在巴黎丢了寒假作业，全网都在帮忙找。"
            "后来警方通报说整件事是自导自演的摆拍，博主和背后的机构都被处罚了。"
            "我不太理解：摆拍本身不就是「创作」吗？既没有指名道姓骂人，也没有说哪里出了事故，"
            "为什么也算编造虚假信息？背后的 MCN 机构要负什么责任？"
        ),
        focus=(
            "**自导自演型虚假内容**的定性（没有被害人、没有险情，为什么仍违法）；"
            "「创作」与「编造虚假信息」的界限；MCN 机构作为组织者的责任；"
            "流量变现作为「牟利」情节的意义"
        ),
        source=(
            "公安部 2024-04-12 公布 10 起打击整治网络谣言违法犯罪典型案例"
            "（「秦朗寒假作业丢巴黎」系摆拍；同批另有江西南昌王某江经营 5 家 MCN 机构、"
            "运营 842 个账号用 AI 批量生成不实文章，被行政拘留 5 日）；中国新闻网 2024-04-12"
        ),
    ),
)


def categories(cases: Sequence[CaseDef] = CASES) -> list[str]:
    """库中出现过的分类，按首次出现顺序（**不是**排序后的字母序）。"""
    seen: list[str] = []
    for case in cases:
        if case.category not in seen:
            seen.append(case.category)
    return seen


def get_case(case_id: str, *, cases: Sequence[CaseDef] = CASES) -> CaseDef | None:
    """按 id 取案例；没有就返回 `None`（**不抛**，也不造一个出来）。"""
    for case in cases:
        if case.id == case_id:
            return case
    return None


def case_label(case_id: str) -> str:
    """案例 id → 台账 `label`。**写侧的唯一实现**。"""
    return f"{CASE_ID_PREFIX}{case_id}"


def case_id_from_label(label: str) -> str | None:
    """台账 `label` → 案例 id；不是案例轮次（或形态不对）就返回 `None`。

    ⚠️ **这是唯一的反解实现**（见模块头）。判据是「前缀完整匹配 + 剩下部分非空
       且不含冒号」，不是"看着差不多"的宽松匹配：`case:foo:bar` 不该被解析成
       一个 id 叫 `foo:bar` 的案例 —— id 里本来就不许有冒号（`self_check()` 盯着
       这一条）。宽松的反解会把一条**畸形**标签变成一条**查不到**的案例轮次，
       而"查不到"在轮换里表现为**静默少算一轮**：不报错，只是轮换慢慢错位。
    """
    if not isinstance(label, str) or not label.startswith(CASE_ID_PREFIX):
        return None
    case_id = label[len(CASE_ID_PREFIX) :]
    if not case_id or ":" in case_id:
        return None
    return case_id


# ---------------------------------------------------------------------------
# 轮换
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RotationPlan:
    """「下一个跑哪个案子」的现场答案，以及答案背后的全部事实。

    ⚠️ 这里刻意**把原始事实一起带出来**（每个案子几轮、圈数、异常），
       而不是只给一个 `next_case`：调用方（体检、答辩材料）经常要问
       "跑满几圈了""有没有轮次没收敛"，只给结论就得再算一遍 —— 而再算一遍
       就是"同一判定写两处"的起点。
    """

    #: 下一个该跑的案子。**库为空时是 `None`**（不是"第一个"）。
    case: CaseDef | None
    #: 每个案例 id → 台账里记着的轮次数。
    counts: dict[str, int]
    #: 已跑满的圈数 = `min(counts.values())`，即"每个案子都至少跑过 N 轮"。
    lap: int
    #: 台账里带 `case:` 前缀、但 id 不在本库里的（删过案子就会留下这些痕迹）。
    unknown: tuple[str, ...]
    #: 有「开」记录、没有「收」记录的轮次 `run_id`（被 kill 的那些）。
    unfinished: tuple[str, ...]
    #: 台账里**所有**案例轮次的条数（含 unfinished）。
    total_rounds: int
    #: 输入里**同 `run_id` 的重复行数**（按 `read_runs` 折叠过的输入，这里恒为 0）。
    #: 非 0 说明调用方把台账原始行直接喂了进来 —— 答案仍然是对的（见 `plan_next`），
    #: 但如实报出来，因为"我到底收的是什么形态的输入"不该靠猜。
    deduped_rows: int = 0

    @property
    def empty(self) -> bool:
        """库为空 ⇒ 轮换无从谈起。**不许**在这里兜一个默认案子出来。"""
        return self.case is None

    @property
    def clean(self) -> bool:
        """轮换的输入干净吗（没有认不出的 id、没有没收尾的轮次）。

        ⚠️ 它**不影响** `case` 的选取 —— 轮换不因为台账有瑕疵就罢工，
           但调用方可以据此决定"这一轮要不要先查清楚再跑"。
        """
        return not self.unknown and not self.unfinished


def plan_next(runs: Sequence[RunRecord], *, cases: Sequence[CaseDef] = CASES) -> RotationPlan:
    """从台账轮次现场算出下一个该跑的案子。**纯函数**（不读盘、不写盘）。

    选法：取**轮次数最少**的那个；并列时取**在库里靠前**的那个。
    于是空库（全 0 轮）第一轮跑 `CASES[0]`，之后一路按顺序铺满一圈，再回到开头。

    ⚠️ **轮次数 = 不同 `run_id` 的个数，不是记录条数。** 台账里一轮是**两行**
       （开一条、收一条），`read_runs()` 会按 `run_id` 折叠成一条。本函数的入参是
       `Sequence[RunRecord]`，调用方**完全可能**把原始行原样传进来 —— 写这个函数的
       人（2026-10-05）自己就踩了：把同一条记录传两次，数出来 2 轮。
    数错的方向是**静默**的：轮换会按一个偏大的计数挑案子，看上去照常运行，
    只是"该跑的案子永远排不到"。所以这里按 `run_id` 归组再数。

    归组只回答两个问题 —— **哪个案子的**、**收没收尾** —— 这是**幂等保护**，
    不是第二份折叠实现：标签在开/收两条里本来就一模一样（收尾记录是 `replace`
    出来的），"收没收到"取"只要有一行带 `finished_at` 就算收过"。
    真正"读台账"这件事仍然只有 `read_runs()` 一处；生产路径
    （`plan_next_from_ledger`）走的就是它，此时 `deduped_rows` 恒为 0。
    """
    counts = {case.id: 0 for case in cases}
    unknown: list[str] = []
    unfinished: list[str] = []
    total = 0
    deduped_rows = 0
    seen_unknown: set[str] = set()

    by_run: dict[str, list[RunRecord]] = {}
    order: list[str] = []
    for run in runs:
        if run.run_id in by_run:
            deduped_rows += 1
        else:
            by_run[run.run_id] = []
            order.append(run.run_id)
        by_run[run.run_id].append(run)

    for run_id in order:
        group = by_run[run_id]
        case_id = case_id_from_label(next((r.label for r in group if r.label), ""))
        if case_id is None:
            continue
        total += 1
        if case_id in counts:
            counts[case_id] += 1
        elif case_id not in seen_unknown:
            # 只记一次：同一条 id 跑了十轮不该在报告里刷十行。
            seen_unknown.add(case_id)
            unknown.append(case_id)
        if all(r.unfinished for r in group):
            unfinished.append(run_id)

    chosen: CaseDef | None = None
    for case in cases:
        if chosen is None or counts[case.id] < counts[chosen.id]:
            chosen = case

    lap = min(counts.values()) if counts else 0
    return RotationPlan(
        case=chosen,
        counts=counts,
        lap=lap,
        unknown=tuple(unknown),
        unfinished=tuple(unfinished),
        total_rounds=total,
        deduped_rows=deduped_rows,
    )


def plan_next_from_ledger(
    *,
    wit_dir: str | os.PathLike[str] | None = None,
    role: str = DEFAULT_ROLE,
    cases: Sequence[CaseDef] = CASES,
) -> RotationPlan:
    """读 `.wit/<role>/runs.jsonl` 再算。**只读**。"""
    return plan_next(read_runs(wit_dir=wit_dir, role=role), cases=cases)


# ---------------------------------------------------------------------------
# 自检：库自己的形态主张（check_local / 元验证都调它）
# ---------------------------------------------------------------------------

#: 文本比较前的归一化：压掉所有空白。用来判"两条案例是不是抄了同一段"。
_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS_RE.sub("", text)


def self_check(cases: Sequence[CaseDef] = CASES) -> list[str]:
    """检查案例库**自己**的形态，返回问题清单（空列表 = 通过）。

    ⚠️ 为什么是"返回清单"而不是"打印 + 返回 bool"：调用方（`check_local`、
       元验证）需要**拿到问题本身**去断言，而不是去解析 stdout。
       任何"靠读输出判断"的判据都是在 grep 源码，本项目明令禁止。

    ⚠️ 这里检查的是**形态**（唯一性、必填、长度），**不是内容对不对**。
       "这条案例是不是真实的"机器判不了 —— 它只能靠取材时核对原始报道，
       以及 `source` 字段必须写清发布方与日期这条形态要求。
       把判不了的事说成判过了，是本项目最忌讳的那类错。
    """
    problems: list[str] = []

    if len(cases) < 2:
        problems.append(f"案例只有 {len(cases)} 条：少于 2 条就谈不上「轮换」。")

    ids = [c.id for c in cases]
    dup_ids = sorted({i for i in ids if ids.count(i) > 1})
    if dup_ids:
        problems.append(f"id 重复：{dup_ids}（台账按 label 认轮次，重复 id 会把两案并成一案）")

    texts = [_norm(c.text) for c in cases]
    dup_texts = [c.id for c, t in zip(cases, texts) if texts.count(t) > 1]
    if dup_texts:
        problems.append(
            f"输入文本重复：{sorted(set(dup_texts))}"
            "（重复文本 ⇒ 轮换换了 id 但喂给链路的是同一段材料，等于没换案）"
        )

    cat_count = len(categories(cases))
    if cat_count < MIN_CATEGORIES:
        problems.append(
            f"分类只有 {cat_count} 类，少于下限 {MIN_CATEGORIES}："
            "案子全挤在同一个法律争点上 ⇒ 轮换只是把同一条路径跑 N 遍"
        )

    for case in cases:
        where = f"案例 {case.id or '(空 id)'}"
        if not CASE_ID_RE.match(case.id or ""):
            problems.append(
                f"{where} 的 id 形态非法（要求 {CASE_ID_RE.pattern}）："
                "它会原样拼进台账 label，越宽松的 id 越容易在 JSONL / shell 两处各坏一种样子"
            )
        for field in ("category", "title", "focus", "source"):
            if not (getattr(case, field) or "").strip():
                problems.append(f"{where} 缺 `{field}`")
        if len(case.source or "") < MIN_SOURCE_CHARS:
            problems.append(
                f"{where} 的 `source` 太短（{len(case.source or '')} < {MIN_SOURCE_CHARS}）："
                "它必须能指名道姓 —— 哪家通报、什么日期；写个「网警」等于没写"
            )
        if (case.source or "").strip() in {case.title.strip(), case.focus.strip()}:
            problems.append(f"{where} 的 `source` 与 `title`/`focus` 一模一样：那是抄了一遍，不是出处")
        n = len(case.text or "")
        if n < MIN_TEXT_CHARS:
            problems.append(
                f"{where} 的输入文本只有 {n} 字符（< {MIN_TEXT_CHARS}）："
                "这不是一次真实研判，而是 ping"
            )
        elif n > MAX_TEXT_CHARS:
            problems.append(
                f"{where} 的输入文本有 {n} 字符（> {MAX_TEXT_CHARS}）："
                "把整篇报道塞进 prompt 只会让主控更快地把步数预算烧在无关上下文上"
            )
        if case.text is not None and _norm(case.text) == _norm(case.title):
            problems.append(f"{where} 的输入文本与标题相同：标题是给人看的，文本是给链路的")
    return problems


# ---------------------------------------------------------------------------
# 渲染（人肉入口与 shell 包装共用，避免同一份显示写两处）
# ---------------------------------------------------------------------------


def render_library(cases: Sequence[CaseDef] = CASES) -> str:
    """把案例库摊开：一行一案。给人核对"库里到底有什么"。"""
    lines = [f"[cases] {len(cases)} 条真实案例，{len(categories(cases))} 类："]
    width = max((len(c.id) for c in cases), default=4)
    for case in cases:
        lines.append(f"  {case.id:<{width}}  [{case.category}] {case.title}")
        lines.append(f"  {' ' * width}  出处：{case.source}")
    return "\n".join(lines)


def render_plan(plan: RotationPlan, *, cases: Sequence[CaseDef] = CASES) -> str:
    """把轮换计划摊开。**下一个**那一行用 `→` 标出来。"""
    lines: list[str] = []
    nxt = plan.case.id if plan.case is not None else None
    lines.append(
        f"[rotation] 已跑 {plan.total_rounds} 轮案例研判，跑满 {plan.lap} 圈"
        f"（{len(cases)} 条案例）"
    )
    for case in cases:
        mark = "→" if case.id == nxt else " "
        lines.append(f"  {mark} {case.id:<14}{plan.counts.get(case.id, 0):>3} 轮  {case.title}")
    if nxt is None:
        lines.append("  ⚠️ 案例库为空 ⇒ 没有可跑的案子（**不猜一个出来**）。")
    else:
        lines.append(f"  下一个：{nxt}（{plan.case.category}）")
    if plan.unknown:
        # ⚠️ 用 `repr` 而不是裸串：认不出的 id 里**可能带着看不见的空白**（手工改过
        #    一行 `case:c-food-01 ` 就会这样），裸串打印出来与正常的 id 一模一样 ——
        #    "看上去没问题"比报错更难查。`repr` 让那个空格自己现形。
        shown = "、".join(repr(i) for i in plan.unknown)
        lines.append(
            f"  ⚠️ 台账里有 {len(plan.unknown)} 个 id 不在本库：{shown}"
            "  —— 删过案子、或有人手改过标签，都会留下这些痕迹；"
            "它们**不计入**任何案子的轮次。"
        )
    if plan.unfinished:
        lines.append(
            f"  ⚠️ 有 {len(plan.unfinished)} 轮案例研判「有开无收」："
            f"{'、'.join(plan.unfinished)}"
            "  —— 材料已经喂进链路，所以照算一轮；但它们**成没成**是另一回事，"
            "用 `python -m legal_acps.run_ledger` 看它们的进程在不在。"
        )
    if plan.deduped_rows:
        lines.append(
            f"  ⚠️ 输入里有 {plan.deduped_rows} 行是重复的 `run_id`"
            "（应当传 `read_runs()` 折叠后的结果）—— 计数已经按 `run_id` 归组过，"
            "答案是对的，但输入形态不是你以为的那一种。"
        )
    return "\n".join(lines)


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="查看真实案例库与轮换计划（只读）")
    parser.add_argument("--wit-dir", default=None, help="默认 <仓库根>/.wit")
    parser.add_argument("--role", default=DEFAULT_ROLE)
    parser.add_argument("--library", action="store_true", help="只列案例库，不算轮换")
    parser.add_argument("--check", action="store_true", help="只跑案例库自检")
    args = parser.parse_args(argv)

    problems = self_check()
    if args.check:
        if problems:
            print(f"[cases] 自检**未通过**（{len(problems)} 项）：")
            for p in problems:
                print(f"  ✗ {p}")
            return 1
        print(f"[cases] 自检通过：{len(CASES)} 条案例 / {len(categories())} 类")
        return 0

    print(render_library())
    if args.library:
        return 1 if problems else 0

    print()
    plan = plan_next_from_ledger(wit_dir=args.wit_dir, role=args.role)
    print(render_plan(plan))
    print(f"  （台账：{ledger_path(args.wit_dir, args.role)}）")

    if problems:
        print(f"\n[cases] ⚠️ 案例库自检有 {len(problems)} 项问题（`--check` 看详情）：")
        for p in problems:
            print(f"  ✗ {p}")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover - 人肉入口
    raise SystemExit(_main())
