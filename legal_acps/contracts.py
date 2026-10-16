"""数据契约与评分量表 —— 本项目所有 Agent 之间的"共同语言"。

设计原则：**先定口径，再写代码**。多智能体项目最容易失败的地方不是模型不够强，
而是几个 Agent 各自表述、格式对不上，Leader 汇总时被迫靠自然语言瞎猜。

所以这里把三件事的输入输出全部钉死：

    1. 煽动性识别   IncitementReport   —— 是不是煽动、哪一类、多强
    2. 真伪判定     ClaimCheckReport   —— 逐条断言 + 整体为真程度 0..100
    3. 社会危害评估 HarmReport         —— harm_index -100..100（项目主指标）

说明：这里的 severity(0..100) 与 harm_index(-100..100) 是两个不同刻度，
前者只回答"这句话有多像煽动"，后者回答"综合考虑后该支持还是该反对、程度多少"。
把它们分开，是为了避免"是煽动但危害低"（比如极端但无实际危险的口号）
和"不是煽动但危害高"（比如编造灾情引发恐慌）这两类被强行压成一个数。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, Field, field_validator, model_validator

# ⚠️ 只从 `evidence.contracts` 取**纯 pydantic** 的类型（它只 import pydantic）。
#    那个包的重依赖（Pillow / numpy）靠 PEP 562 惰性导入挡在
#    `evidence/__init__.py` 里，所以这一行**不会**把图像库拉进主链路 ——
#    否则"服务器上没装 Pillow"会升级成"所有已过审 Agent 起不来"。
from .evidence.contracts import Finding, SuspicionLevel, UpgradePath

# ---------------------------------------------------------------------------
# 一、社会危害性量表（-100 .. +100）
#
# 用户口径：数值越高 = 越有问题、越不应支持；数值越低 = 越应支持。
# 为了让打分可复现、可答辩，必须给出**分档锚点**，否则不同人打出来的分没法比。
# ---------------------------------------------------------------------------


class HarmBand(str, Enum):
    """harm_index 的分档锚点。数值越高越应反对。"""

    PROTECTED = "完全正当"      # -100 .. -61
    LAWFUL_VOICE = "依法表达"   #  -60 .. -21
    CRITICISM = "正当批评"      #  -20 ..  -1
    NEUTRAL = "中性"            #    0
    LOW = "低风险"              #    1 ..  20
    RISING = "风险上升"         #   21 ..  40
    MEDIUM = "中高风险"         #   41 ..  60
    HIGH = "高风险"             #   61 ..  80
    SEVERE = "涉嫌违法"         #   81 .. 100


HARM_BAND_TABLE: tuple[tuple[int, int, HarmBand, str], ...] = (
    (-100, -61, HarmBand.PROTECTED, "依法维权、公益监督、学术讨论等受宪法和法律保护的言论"),
    (-60, -21, HarmBand.LAWFUL_VOICE, "依法表达诉求、对公共事务提出异议"),
    (-20, -1, HarmBand.CRITICISM, "情绪化但属正常批评，无明显事实错误"),
    (0, 0, HarmBand.NEUTRAL, "纯事实陈述或与公共议题无关"),
    (1, 20, HarmBand.LOW, "用词偏激、片面，但未指向具体危害后果"),
    (21, 40, HarmBand.RISING, "带有群体指向的负面标签化，存在扩散风险"),
    (41, 60, HarmBand.MEDIUM, "煽动性明显，可能违反治安管理或平台规范"),
    (61, 80, HarmBand.HIGH, "明确号召违法行为或制造群体对立，应追究责任"),
    (81, 100, HarmBand.SEVERE, "涉嫌刑事犯罪（如煽动分裂、煽动民族仇恨、编造传播虚假信息等）"),
)


def band_of(harm_index: int) -> HarmBand:
    """把 -100..100 的整数映射到分档。"""
    for lo, hi, band, _ in HARM_BAND_TABLE:
        if lo <= harm_index <= hi:
            return band
    raise ValueError(f"harm_index 必须在 -100..100 之间，收到 {harm_index}")


def band_rationale(harm_index: int) -> str:
    for lo, hi, band, why in HARM_BAND_TABLE:
        if lo <= harm_index <= hi:
            return why
    raise ValueError(f"harm_index 必须在 -100..100 之间，收到 {harm_index}")


# ---------------------------------------------------------------------------
# 二、煽动性识别
# ---------------------------------------------------------------------------


class IncitementKind(str, Enum):
    """煽动指向的对象。法律评价差异很大，必须分类而不能只给一个布尔值。"""

    NONE = "无煽动"
    VIOLENCE = "煽动暴力"
    ETHNIC_HATRED = "煽动民族仇恨"
    RELIGION = "煽动宗教对立"
    SEPARATISM = "煽动分裂国家"
    REGIONAL = "煽动地域歧视"
    GENDER = "煽动性别对立"
    OCCUPATION = "煽动职业/阶层对立"
    INSULT = "侮辱诽谤"
    RUMOR = "编造传播虚假信息"


class IncitementReport(BaseModel):
    """Partner A 的第一项输出。"""

    is_incitement: bool = Field(description="是否构成煽动性言论")
    kinds: list[IncitementKind] = Field(
        default_factory=list, description="煽动指向的类别，可多选；无煽动时为空"
    )
    severity: int = Field(
        ge=0, le=100, description="煽动强度 0..100。只衡量'像不像煽动'，不含危害后果"
    )
    targets: list[str] = Field(
        default_factory=list, description="被针对的群体、个人或机构，逐条列出"
    )
    evidence_spans: list[str] = Field(
        default_factory=list, description="支撑判断的原文片段，必须逐字摘录，不得改写"
    )
    action_calls: list[str] = Field(
        default_factory=list, description="其中属于'号召行动'的部分，这类是危害升级的关键信号"
    )
    rationale: str = Field(description="判断理由，须引用 evidence_spans 中的片段")

    @field_validator("evidence_spans", "action_calls")
    @classmethod
    def _strip_empty(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s and s.strip()]


# ---------------------------------------------------------------------------
# 三、真伪判定
# ---------------------------------------------------------------------------


class ClaimVerdict(str, Enum):
    TRUE = "属实"
    MOSTLY_TRUE = "基本属实"
    MIXED = "真伪混杂"
    MOSTLY_FALSE = "大部分不实"
    FALSE = "不实"
    UNVERIFIABLE = "无法核实"


class ClaimCheck(BaseModel):
    """对单条可核查断言的判定。"""

    claim: str = Field(description="从原文中拆出的一条可核查的事实性断言")
    verdict: ClaimVerdict = Field(description="该断言的判定结论")
    truth_score: int = Field(
        ge=0, le=100, description="该断言为真的程度 0..100；无法核实时请给 50 并在 notes 说明"
    )
    basis: list[str] = Field(
        default_factory=list, description="判断依据：法条、公开资料、常识或指出其逻辑漏洞"
    )


class ClaimCheckReport(BaseModel):
    """Partner A 的第二项输出。"""

    claims: list[ClaimCheck] = Field(
        default_factory=list, description="逐条拆解出的断言；纯情绪表达可不拆"
    )
    overall_truth_score: int = Field(
        ge=0, le=100, description="整体为真程度 0..100，应按断言重要性加权，而非简单平均"
    )
    notes: str = Field(default="", description="拆解口径、无法核实的部分及原因")


# ---------------------------------------------------------------------------
# 四、社会危害评估
# ---------------------------------------------------------------------------


class HarmDimensions(BaseModel):
    """六维分解。给出分项是为了让最终那一个数字可解释、可质疑。"""

    public_order: int = Field(ge=0, le=100, description="扰乱公共秩序的风险")
    national_security: int = Field(ge=0, le=100, description="涉及国家安全的风险")
    group_targeting: int = Field(ge=0, le=100, description="针对特定群体的攻击性")
    real_world_risk: int = Field(
        ge=0, le=100, description="转化为现实危害（聚集、冲突、人身伤害）的可能性"
    )
    spread_potential: int = Field(ge=0, le=100, description="传播扩散的潜力")
    expression_legitimacy: int = Field(
        ge=0, le=100, description="作为正常表达受保护的程度，越高越应减分"
    )


class HarmReport(BaseModel):
    """Partner B 的输出，也是整个项目的主指标。"""

    harm_index: int = Field(
        ge=-100,
        le=100,
        description="社会危害性综合指数：正数表示有问题、不应支持，负数表示正当、应予支持",
    )
    band: str = Field(
        default="",
        # 关键：Pydantic v2 默认 **不校验默认值**（validate_default=False），
        # 于是模型不填 band 时，下面那个自动补全的 validator 根本不会被调用。
        # 实测症状：输出里 "band": ""。必须显式打开这一项。
        validate_default=True,
        description="由 harm_index 映射出的分档名称，程序会自动补全",
    )
    dimensions: HarmDimensions = Field(description="六维分解")
    legal_hints: list[str] = Field(
        default_factory=list,
        description="可能相关的中国法律条文，须写明法律名称与条号，如《刑法》第249条",
    )
    reasoning: str = Field(description="给出该分数的推理链条，必须回应上游的煽动性与真伪结论")
    counter_view: str = Field(
        default="", description="反方视角：这段言论在什么情况下可以被理解为正当"
    )

    @field_validator("band", mode="before")
    @classmethod
    def _fill_band(cls, v: object, info) -> str:
        # band 允许模型留空，由 harm_index 自动推导，避免模型自己算错分档
        if v:
            return str(v)
        idx = info.data.get("harm_index")
        return band_of(int(idx)).value if idx is not None else ""


# ---------------------------------------------------------------------------
# 五、Leader 汇总
# ---------------------------------------------------------------------------


class FinalVerdict(BaseModel):
    """主控 Agent 的最终交付物。

    ⚠️ 2026-10-07：煽动性 / 危害性判定**整体停用**（原依赖的煽动识别、
    危害评估两个专家已软废弃）—— `is_incitement` / `harm_index` 保留字段
    只为契约兼容（下游还在读这两个键），一律取默认值，**不许再当判定输出用**。
    """

    subject: str = Field(description="被研判的原文，逐字引用")
    # 🔴 已停用（2026-10-07）：固定 False。字段保留 = 契约兼容，不是"还在判"。
    is_incitement: bool = Field(
        default=False,
        description="已停用：煽动性识别专家已软废弃，固定填 false，"
                    "输出文字不得出现煽动性相关表述",
    )
    # 🔴 已停用（2026-10-07）：固定 0。band 随之恒定，同样不再是判定输出。
    harm_index: int = Field(
        default=0, ge=-100, le=100,
        description="已停用：危害评估专家已软废弃，固定填 0，"
                    "输出文字不得出现危害性相关表述",
    )
    band: str = Field(
        default="",
        validate_default=True,  # 同上：不打开就永远是空串
        description="分档名称，由 harm_index 自动推导（harm_index 已停用 ⇒ 恒定）",
    )
    truth_score: int = Field(ge=0, le=100, description="最终认定的为真程度")
    consistency_note: str = Field(
        default="",
        description="判定依据与不确定性；走裁判分支时写裁定结论与命中话术",
    )
    conclusion: str = Field(description="面向普通用户的结论，通俗中文")
    action_advice: str = Field(description="建议：如'可依法表达''建议平台核查''建议向公安机关举报'")
    disclaimer: str = Field(
        default="本结论由人工智能辅助生成，仅供法律学习与研究参考，不构成法律意见，"
        "不能替代律师或司法机关的认定。",
        description="免责声明",
    )

    @field_validator("band", mode="before")
    @classmethod
    def _fill_band(cls, v: object, info) -> str:
        # 与 HarmReport 同样的策略：模型算分、程序算档，避免模型自己分错档
        if v:
            return str(v)
        idx = info.data.get("harm_index")
        return band_of(int(idx)).value if idx is not None else ""


# ---------------------------------------------------------------------------
# 六、辩论子会话（功能一：事实核查与舆论甄别）
#
# 设计要点（详见 docs/项目设计提示词.md §3.2–3.4）：
#   1. **信息隔离** —— 辩论双方只拿到「可核验事实清单」（不含任何真假判断），
#      真值由裁判在裁定封存后才揭示。否则辩论就是演一场已知结局的戏。
#   2. **立场随机分配** —— 立场与真相无关，防的是模型的一致性偏差。
#   3. **结构化终止信号** —— 自然语言判断不了"对方被说服了"，
#      所以每轮必须交一个结构化信号（round/stance/confidence/key_point/rebuts）。
#   4. **链深熔断 + 环检测** —— wit 走 mTLS 时未实现 AAC 要求的委托链深度限制，
#      由本项目的 debate.py 自行补上（同时也是防额度烧光的护栏）。
# ---------------------------------------------------------------------------


class DebateStance(str, Enum):
    """一方在当前轮次的态度。"""

    HOLD = "hold"        # 继续坚持（默认）
    CONCEDE = "concede"  # 认输：这是给裁判的有效信号，不是失败


class SourceLevel(str, Enum):
    """信息源等级。只有一手材料能支撑事实判断。"""

    PRIMARY = "一手材料"      # 原始视频 / 通报 / 文件
    SECONDARY = "二手转述"    # 媒体报道、转述
    OPINION = "观点评论"      # 评论、解读
    UNKNOWN = "来源不明"


class FactItem(BaseModel):
    """一条**可被外部验证**的客观信息。严禁写入判断性内容。"""

    item: str = Field(description="客观信息本身：时间 / 地点 / 人物 / 数据")
    source_level: SourceLevel = Field(description="信息源等级")
    source: str = Field(description="出处：链接、机构名或文件号；拿不到就写'未获取到'")
    verbatim: str = Field(
        default="", description="若引用原文，必须逐字摘录，不得改写或概括"
    )


class FactSheet(BaseModel):
    """采证阶段的产出：可核验事实清单。**不含任何"是真是假"的结论**。"""

    event: str = Field(description="被核查的事件，一句话说清")
    items: list[FactItem] = Field(
        default_factory=list, description="逐条列出的可核验事实"
    )
    missing: list[str] = Field(
        default_factory=list, description="想要但没有获取到的信息项，如实列出，不要用推测填补"
    )
    notes: str = Field(default="", description="采证口径说明、来源分级理由")


class DebateArgument(BaseModel):
    """一轮辩论提交的结构化信号（正反双方共用同一契约）。"""

    round: int = Field(ge=1, description="第几轮，从 1 开始")
    stance: DebateStance = Field(description="hold=继续坚持；concede=认输")
    confidence: float = Field(
        ge=0.0, le=1.0, description="对自己当前立场成立信心 0.0~1.0"
    )
    key_point: str = Field(description="本轮核心论点")
    rebuts: list[int] = Field(
        default_factory=list, description="本轮反驳的是对方第几轮的论点编号"
    )


class DebateRound(BaseModel):
    """一轮辩论的完整记录。"""

    round: int = Field(ge=1)
    speaker: str = Field(description="'pro' 正方 或 'con' 反方")
    argument: DebateArgument


class DebateRecord(BaseModel):
    """一场辩论的完整记录 —— 交回裁判的唯一交付物。"""

    event: str = Field(description="被辩论的事件")
    pro_position: str = Field(description="分配给正方的立场（随机分配，与真相无关）")
    con_position: str = Field(description="分配给反方的立场（与正方相反）")
    rounds: list[DebateRound] = Field(default_factory=list, description="逐轮记录")
    termination_reason: str = Field(description="为什么停下来：轮数上限 / 认输 / 置信度塌陷")
    chain_depth: int = Field(
        default=1, ge=1, description="本次委托链深度（用于熔断，见 debate.py）"
    )


class DebateOpening(BaseModel):
    """正方 Agent 交回裁判的完整交付物。

    ⚠️ 注意这里面**没有**"这段说法是真是假"的字段 —— 那是裁判的职责。
    正方一旦写下结论，后面的辩论就变成演戏。
    """

    fact_sheet: FactSheet = Field(description="第一段：可核验事实清单")
    record: DebateRecord = Field(description="第二段：完整辩论记录")


# ---------------------------------------------------------------------------
# 七、网络裁判（事实核查的裁定环节）
#
# 结构（2026-09-29 改造）：
#
#     法律咨询主控  (Leader)
#        └─ 网络裁判  (Partner ＋ 辩论子会话 Leader)
#              ├─ 正方  (Partner)
#              └─ 反方  (Partner)
#
# 两层嵌套的 Leader–Partner 关系：裁判同时是「法律主控的 Partner」与
# 「辩论子会话的 Leader」。旧结构里正方兼任子会话 Leader，等于**既当运动员
# 又当裁判**；现在裁判独立成 Agent，正反双方都是它的直接 Partner ——
# 符合 AIP §3.2「Leader 与每个 Partner 直接交互，Partner 之间不直接交互」，
# 辩论由裁判居中转达。
# ---------------------------------------------------------------------------


class FallacyKind(str, Enum):
    """误导性话术。**分两层** —— 这分层是教学与判定的骨架，不是分类癖。

    ═══ 证据层（4 类）═══════════════════════════════════════════════════
    回答「**材料被动过没有**」。四类都有客观依据可查（元数据、原始出处、
    原始数据），所以它既是"反诈辨识"的原料，也是**能拿去要求对方举证的抓手**。

    ═══ 修辞层（6 类）═══════════════════════════════════════════════════
    回答「**他没动材料，但他动了你的脑子**」。这类话术**根本不碰事实**，
    因此没有"原始出处"可以对 —— 也最难防，网上带节奏绝大多数用的是这些。
    单独列一层，是因为它对当事人的要求完全不同：证据层靠**查**，修辞层靠**认**。

    ⚠️ 判定上的纪律（两层通用）：命中的必须逐字摘录原话，
    不能拿"他语气不太对"当命中。宁可漏报，不要凑数 ——
    凑出来的 `traps` 会让复盘教给用户错的东西。
    """

    # —— 证据层：材料被动过 ——
    TIME_MISMATCH = "时间错配"       # 旧画面配新事件
    OUT_OF_CONTEXT = "断章取义"      # 截取片段、去掉限定条件，改变原意
    MISATTRIBUTION = "张冠李戴"      # 甲地/甲主体的事安到乙头上
    NUMBER_DISTORTION = "数字夸张"   # 原始数据被放大、缩小或换种说法

    # —— 修辞层：材料没动，动的是你的脑子 ——
    EMOTIONAL_APPEAL = "诉诸情绪"    # 用愤怒/恐惧/同情替代论证本身
    SLIPPERY_SLOPE = "滑坡推论"      # 从 A 一路推到必然 Z，中间全跳过
    FALSE_DILEMMA = "虚假二分"       # 明明有多种可能，硬说只剩两个选项
    STRAW_MAN = "稻草人"             # 先歪曲对方的观点，再打那个歪曲版
    APPEAL_TO_AUTHORITY = "诉诸权威"  # "专家都说了"，但给不出可核对的出处
    BURDEN_SHIFT = "举证倒置"        # 不证明自己的主张，改要求对方自证清白


#: 层名。**只在这里定义一次** —— 提示词、自检、前端分组都引它，避免"四处各写一份"。
FALLACY_LAYER_EVIDENCE = "证据层"
FALLACY_LAYER_RHETORIC = "修辞层"

#: 认不出层的话术落到这里。**必须显式存在** —— 见 `group_fallacies` 的说明：
#: 认不出就丢掉是最坏的处理（链路全绿，用户只是"少看到一条"）。
FALLACY_LAYER_UNKNOWN = "未归类"

FALLACY_EVIDENCE_KINDS: tuple[FallacyKind, ...] = (
    FallacyKind.TIME_MISMATCH,
    FallacyKind.OUT_OF_CONTEXT,
    FallacyKind.MISATTRIBUTION,
    FallacyKind.NUMBER_DISTORTION,
)

FALLACY_RHETORIC_KINDS: tuple[FallacyKind, ...] = (
    FallacyKind.EMOTIONAL_APPEAL,
    FallacyKind.SLIPPERY_SLOPE,
    FallacyKind.FALSE_DILEMMA,
    FallacyKind.STRAW_MAN,
    FallacyKind.APPEAL_TO_AUTHORITY,
    FallacyKind.BURDEN_SHIFT,
)

FALLACY_LAYER_OF: dict[FallacyKind, str] = {
    **{k: FALLACY_LAYER_EVIDENCE for k in FALLACY_EVIDENCE_KINDS},
    **{k: FALLACY_LAYER_RHETORIC for k in FALLACY_RHETORIC_KINDS},
}


def fallacy_layer(kind: FallacyKind | str) -> str:
    """这条话术属于哪一层。未知类型返回空串（**不猜** —— 猜错会把课教歪）。"""
    if isinstance(kind, FallacyKind):
        return FALLACY_LAYER_OF.get(kind, "")
    for member in FallacyKind:
        if member.value == kind:
            return FALLACY_LAYER_OF.get(member, "")
    return ""


def _fallacy_kind_of(item: Any) -> Any:
    """取出条目里的 `kind` —— **同时支持模型对象与 dict 两种形态**。

    两种形态都会流到这里：模型自曝时给的是 `FallacyHit`（有 `.kind`），
    而编排层/接口层拿到的是已经 `model_dump` 过的 dict（`["kind"]`）。

    ⚠️ 旧实现只认 `.kind`（`getattr(hit, "kind", hit)`），对 dict 取不到 →
    `fallacy_layer()` 返回空串 → **每一条都被静默归到"未归类"**。
    这不会报错：链路全绿，用户只是看到复盘页里所有话术都挤在同一个组下面。
    这也正是它此前"接不上线"、只能当死代码的根因 —— 不是没人想用，
    是**用不了**（接上去就等于把所有条目打成未归类）。
    """
    kind = getattr(item, "kind", None)
    if kind is None and isinstance(item, Mapping):
        kind = item.get("kind")
    return kind


def group_fallacies(items) -> dict[str, list]:
    """把话术条目按层分组，**保持原顺序**。

    返回固定含三个键（证据层 / 修辞层 / 未归类），层内为空时是**空列表** ——
    调用方不必判 `None`，也不会因为"这个层名没认出来"而**静默丢条目**。

    ⚠️ 未归类的单独成组、显式暴露，既不并入任何一层，也不丢掉。
    丢掉是最坏的处理：链路全绿，用户只是"少看到一条"。
    """
    grouped: dict[str, list] = {
        FALLACY_LAYER_EVIDENCE: [],
        FALLACY_LAYER_RHETORIC: [],
        FALLACY_LAYER_UNKNOWN: [],
    }
    for hit in items or []:
        layer = fallacy_layer(_fallacy_kind_of(hit)) or FALLACY_LAYER_UNKNOWN
        grouped.setdefault(layer, []).append(hit)
    return grouped


#: 每类话术的一句话解释。**只在这里写一次** —— 提示词清单由它派生。
FALLACY_GLOSS: dict[FallacyKind, str] = {
    FallacyKind.TIME_MISMATCH: "拿旧画面 / 旧数据配当下的事件",
    FallacyKind.OUT_OF_CONTEXT: "截取片段、去掉限定条件，改变原意",
    FallacyKind.MISATTRIBUTION: "把甲地 / 甲主体的事安到乙头上",
    FallacyKind.NUMBER_DISTORTION: "把原始数据放大、缩小或换成更好听的表述",
    FallacyKind.EMOTIONAL_APPEAL: "用愤怒 / 恐惧 / 同情替代论证本身",
    FallacyKind.SLIPPERY_SLOPE: "从 A 一路推到必然 Z，中间全跳过",
    FallacyKind.FALSE_DILEMMA: "明明有多种可能，硬说只剩两个选项",
    FallacyKind.STRAW_MAN: "先歪曲对方的观点，再打那个歪曲版",
    FallacyKind.APPEAL_TO_AUTHORITY: "「专家都说了」，但给不出可核对的出处",
    FallacyKind.BURDEN_SHIFT: "不证明自己的主张，改要求对方自证清白",
}

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _render_layer(kinds: tuple[FallacyKind, ...]) -> str:
    lines = []
    for index, kind in enumerate(kinds):
        mark = _CIRCLED[index] if index < len(_CIRCLED) else f"{index + 1}."
        lines.append(f"  {mark} `{kind.value}` —— {FALLACY_GLOSS.get(kind, '')}")
    return "\n".join(lines)


#: 供各 Agent 的 instructions 直接引用的两层话术清单。
#:
#: ⚠️ **必须从枚举派生，不能手写**。手写就会出现"枚举里加了、提示词里没有"
#: （模型永远不用新那一类）或反过来的幽灵条目 —— 而这类不一致在离线自检里
#: 是看不见的：提示词是自由文本，没有 schema 可校。
FALLACY_LAYERS_BRIEF = f"""证据层（**材料被动过** —— 有客观依据可查，可以要求对方举证）：
{_render_layer(FALLACY_EVIDENCE_KINDS)}
修辞层（**材料没动，动的是你的认知** —— 不碰事实，所以没有原始出处可以核对）：
{_render_layer(FALLACY_RHETORIC_KINDS)}"""


class FallacyHit(BaseModel):
    """一条被判定命中的话术。这是「反诈辨识」环节的原料。"""

    kind: FallacyKind = Field(description="命中哪一类话术")
    excerpt: str = Field(description="命中的原文片段，逐字摘录，不得改写")
    speaker: str = Field(
        description="谁使用的：'pro' 正方 / 'con' 反方 / 'user' 用户"
    )
    why: str = Field(description="为什么算这一类，须指出它具体如何误导读者")


class ArbitrationCase(BaseModel):
    """裁判受理阶段的结构化产出。

    把「一段含混的传闻」加工成「一个可判真假的命题 + 一份可核验事实清单」，
    再据此给双方分配立场。**这一步不写任何真假结论** —— 那是裁定阶段的事。
    """

    event: str = Field(description="被核查的事件，一句话说清")
    proposition: str = Field(
        description="待裁定的核心命题，必须是可判真假的陈述句，一句话"
    )
    fact_sheet: FactSheet = Field(description="可核验事实清单（不含真假判断）")
    pro_position: str = Field(description="分配给正方的立场，一句话")
    con_position: str = Field(description="分配给反方的立场，与正方相反")


class RulingVerdict(str, Enum):
    """裁定结论。⚠️「无法判定」是合法且经常正确的答案 —— 证据不足时给结论比给错更糟。

    🔴 2026-10-07 补「不实」（值域缺口，未决 #16，实测发现）：官方证伪的谣言
    （占本项目主要案件类型——基准标注 13 条里 11 条）在旧三态值域下**结构性
    不可能判对**：「确有其事/断章取义」都不对（它不是真实事件、也不只是断章），
    「无法判定」也不对（证据充分，结论明确）。三态缺的正是「编造/虚构」这一档。
    ⚠️ 判定边界：**「不实」= 有权威信源证伪**（官方通报、当事方承认等）；
    证据不足以证伪也不足以证实时仍是「无法判定」—— 诚实优先。
    """

    TRUE = "确有其事"
    FALSE = "不实"
    DISTORTED = "断章取义"
    UNDECIDABLE = "无法判定"


class FactRuling(BaseModel):
    """网络裁判的最终交付物（裁定书）。"""

    event: str = Field(description="被裁定的事件")
    verdict: RulingVerdict = Field(description="裁定结论")
    confidence: float = Field(
        ge=0.0, le=1.0, description="证据强度，诚实反映；来源互相矛盾时它就是上不去的"
    )
    basis: list[str] = Field(
        default_factory=list, description="裁定依据，每条都要能指回具体来源"
    )
    disputed_points: list[str] = Field(
        default_factory=list, description="双方争议的真正焦点，供复盘"
    )
    fallacies: list[FallacyHit] = Field(
        default_factory=list, description="命中的误导性话术；未命中则为空"
    )
    reasoning: str = Field(description="从证据到结论的推理链条")
    disclaimer: str = Field(
        default="本裁定由自动化流程辅助生成，仅供学习与研究参考，"
        "不具备司法鉴定效力，不能替代司法机关的认定。",
        description="免责声明",
    )


class CitedArticle(BaseModel):
    """建议书里引用的一条法条。

    模型只填「法名 / 条号 / 逐字摘录」三个字段——出处与生效日期由
    `law.submit_advice` 工具**校验并从库内补全**（模型填的溯源字段
    不可信：它可能凭记忆写出不存在的 URL）。
    """

    law_name: str = Field(description="法律名称，如《中华人民共和国民法典》")
    article_number: str = Field(description="条文编号，如第五百八十四条")
    excerpt: str = Field(description="条文原文的逐字摘录（不得改写、概括）")


class LegalAdvice(BaseModel):
    """law-advisor 的交付物：法律纠纷**初期**的咨询建议书。

    定位纪律（与提示词同口径，两处一起改）：
      · 只做初期建议（梳理事实 / 指出可能的法定权利 / 下一步行动方向）；
      · 不预测判决结果、不代写法律文书、不构成法律意见；
      · 能力审视是**交付物的一部分**：`applicable=False` 时如实说
        「超出我的能力范围」并指明缺哪部法律，不许硬撑。
    """

    situation_summary: str = Field(
        description="对用户纠纷的理解复述（让用户能发现理解偏差）"
    )
    applicable: bool = Field(
        description="库内八部法律是否覆盖该咨询领域；False=能力范围外"
    )
    advice: list[str] = Field(
        default_factory=list,
        description="建议行动项（初期方向性建议，如协商/投诉/报警/起诉/找律师）",
    )
    cited_articles: list[CitedArticle] = Field(
        default_factory=list,
        description="引用的法条；只能引用 lookup 查到的条文，逐字摘录",
    )
    capability_note: str = Field(
        default="",
        description="能力边界声明：覆盖哪些法律、缺什么、为什么给不出更具体建议",
    )
    reasoning: str = Field(description="从法条到建议的推理链条")
    disclaimer: str = Field(
        default="本建议由自动化流程辅助生成，仅供参考，不构成法律意见；"
                "正式处理请咨询执业律师。",
        description="免责声明",
    )


class LeadSubmission(BaseModel):
    """lead-intake 的交付物：一条**已规范化**的待核查线索。

    定位纪律（与提示词同口径，两处一起改）：
      · 这是「群众来信」的受理记录，不是核查结论——claim 必须保持
        **求证口吻**，不得夹带官方定性（"这是谣言"）或处罚结果
        （把答案的后半段喂给下游裁判 = 基准污染，gt 扩充同一条纪律）；
      · 溯源字段（platform / virality / observed_at）**给不出就留空**，
        不许靠猜补齐（离线抓取器同款纪律）。
    """

    claim: str = Field(
        description=(
            "待核查传闻的原文转述（求证口吻，如「网上都在传……这是真的吗」）。"
            "不得包含官方定性词（谣言/不实/辟谣等）或处罚结果。"
        )
    )
    reporter_type: str = Field(
        default="",
        description="报送人身份：当事人 / 旁观转述 / 其他（给不出留空）",
    )
    platform: str = Field(
        default="",
        description="在哪里看到的：微信群 / 微博 / 抖音 / 短信等（给不出留空）",
    )
    virality: str = Field(
        default="",
        description="热度线索：转发量 / 评论数 / 群内刷屏程度（给不出留空）",
    )
    observed_at: str = Field(
        default="",
        description="什么时候看到的（日期或大致时段；给不出留空）",
    )
    category: str = Field(
        description=(
            "线索类别。从 lead_submit_lead 返回的 categories 清单里挑"
            "（清单由案例库现场派生）；实在不合适才填「其他」。"
        )
    )
    sensitive: bool = Field(
        default=False,
        description="是否涉及可识别的自然人/机构名誉（涉则 True，受理时须告知报送人核查状态不公开）",
    )
    note: str = Field(
        default="",
        description="其他线索说明（如附带材料、上下文）",
    )


# ---------------------------------------------------------------------------
# 七之二、证据鉴证子会话（加分项：图像鉴伪的汇总层）
#
# 与「事实核查」那条链**同形**，只是被汇总的对象换了：
#
#     legal-consult-leader  (S1 Leader)
#        ├─ fact-judge      (S1 Partner ＋ S2 Leader)  → 事实核查
#        └─ evidence-leader (S1 Partner ＋ S2 Leader)  → 证据鉴证   ← 新增
#              └─ evidence-image (S2 Partner)          → 图像鉴伪
#
# 为什么不让主控直接调 `evidence-image`，而要多这一层：
#   1. **步数预算**。服务模式下 `max_steps=8` 是钉死的（`RunConfig` frozen，
#      Agent 侧改不了）。主控要自己扛"每个模态一次发现 + 一次派发"，
#      模态一多就装不下；多一层就把这段编排挪进 `evidence-leader` 自己的预算里。
#   2. **扩展性**。以后加 audio / video 只挂在它下面，主控与其它调用方**零改动**。
#   3. **可复用**。任何 Agent 只要发现 `evidence-leader` 就能用上全部模态；
#      否则每个调用方都要自己写一遍"发现 + 派发"，模态一变全部返工。
#
# ⚠️ 契约纪律（与检测层同源，别在这里破）：
#   · `suspicion` 的取值域**从 `evidence.contracts` 复用**（`SuspicionLevel`），
#     不写第二份 —— 写第二份就会出现"检测层新增一档、汇总层还是老三样"；
#   · 每条线索自带 `caveat`（它**不能**证明什么），这是 `Finding` 的必填字段；
#   · `not_run` 必须显式列出"本次没跑成的检查项"——
#     「测不了」与「没问题」是两件事，混在一起正是本项目最忌讳的静默安全结论；
#   · 全文**没有**"真 / 假"这一对取值，`conclusion` 也不许出现断言性措辞。
# ---------------------------------------------------------------------------


class EvidenceModality(str, Enum):
    """证据的模态。图像 / 音频 / 视频三个都已开。

    ⚠️ 这个枚举是**模态编排的单一事实源**：`EvidenceCase.modality` 由鉴证 Leader
    在受理时定下，之后决定派给哪个 Partner（`图像` → evidence-image、
    `音频` → evidence-audio、`视频` → evidence-video）。所以加一个模态 = 在这里加一项
    + 在 `agents.EVIDENCE_LEADER_INSTRUCTIONS` 里加一条派发分支 ——
    **两处必须同时改**，只改一处会让 Leader 收到一个它无从派发的模态。

    ⚠️ `check_local` 把"枚举里的成员"与"Leader 提示词里点名的 Partner 名"
    做成**成对断言**：加一个模态却忘了改提示词，绿会变红。
    """

    IMAGE = "图像"
    AUDIO = "音频"
    #: 2026-10-02 新增：视频鉴伪。**与图像 / 音频平级**（证据链的第三级）——
    #: 链深上限是 3，把它挂在 evidence-image 下面会直接撞墙。
    VIDEO = "视频"

    @property
    def kind(self) -> str:
        """该模态在**代码侧**的英文键。

        同一件事本来有三个写法，散在三处就一定会漂：

            `EvidenceModality.VIDEO`（本枚举，给模型看的是中文「视频」）
            `evidence_hooks.KIND_PROFILES["video"]`（画像表的键）
            `EvidenceInboxHooks(kinds=("video",))`（收件箱要收哪些模态）

        最容易漏的正是**第三处** —— 加一个模态却忘了把它列进 `kinds`，
        后果是那种附件收不进来 ⇒ 模型看不到句柄 ⇒ 派发时无字节可带 ⇒
        下游回一句"没有材料可看"。**而链路上每一环看起来都是成功的。**

        把映射放在枚举上以后，`check_local` 可以**现场派生**地断言
        「`kinds` ⊇ 全部模态」，而不必再手抄一份清单（手抄的那份就是下
        一个会漂的副本）。角色 id 也按同一个约定派生：`evidence-<kind>`。
        """
        return _KIND_BY_MODALITY[self]


#: 模态 → 代码侧英文键。**这是唯一一份映射**（见 `EvidenceModality.kind`）。
_KIND_BY_MODALITY: dict[EvidenceModality, str] = {
    EvidenceModality.IMAGE: "image",
    EvidenceModality.AUDIO: "audio",
    EvidenceModality.VIDEO: "video",
}


class EvidenceCase(BaseModel):
    """鉴证 Leader 的**受理**产出：把材料加工成一份可执行的鉴证委托单。

    **这一步不下任何真伪结论** —— 那是鉴伪专家与汇总阶段的事。
    它存在的意义是：把"要查什么"从自然语言里拎出来，顺便把**材料引用**
    （句柄或地址）**原样固定下来** —— 后面派发时模型必须照抄它，
    漏掉就等于没把材料送出去。
    """

    subject: str = Field(description="待鉴证材料的说明：这是什么、从哪来")
    modality: EvidenceModality = Field(description="材料属于哪种模态")
    question: str = Field(
        description="本次要回答的问题，一句话。必须是**可检验的技术问题**"
                    "（如「这张图是否存在拼接或重压缩痕迹」），不要写成法律定性问题"
    )
    context_note: str = Field(
        default="", description="案情说明（原样带下去，仅用于措辞，不参与判定）"
    )
    source_refs: list[str] = Field(
        default_factory=list,
        description="本次任务自带材料的引用（inbox:<sha256> 句柄 或 材料地址），"
                    "**原样照抄**，不得改写、不得拼造、不得写成文件路径",
    )


class EvidenceRuling(BaseModel):
    """鉴证 Leader 的最终交付物（鉴证报告）。

    ⚠️ 与 `FinalVerdict` / `FactRuling` 一样，这是**对外可见的契约**：
    它同时是 `evidence.issue_report` 这个模型的工具 schema。
    """

    subject: str = Field(description="被鉴证材料的说明")
    modality: EvidenceModality = Field(description="材料模态")
    suspicion: SuspicionLevel = Field(
        description="综合线索等级 —— **不是**伪造概率。"
                    "「无法判定」是合法且经常正确的答案：证据不足时给结论比给错更糟"
    )
    findings: list[Finding] = Field(
        default_factory=list,
        description="逐项技术线索。每一条都**必须连它自己的 caveat 一起**转述",
    )
    not_run: list[str] = Field(
        default_factory=list,
        description="本次**没能跑成**的检查项及各自原因（如「样本为大片纯色，"
                    "噪声类检查的前提不成立」）。"
                    "**必须与「跑了且未提示异常」分开列** —— 否则读的人会把"
                    "「没检查」当成「没问题」，那正是本能力最危险的失败形态",
    )
    sha256: str = Field(
        default="", description="原文件 SHA-256（证据保全用，**事后无法补算**）"
    )
    upgrades: list[UpgradePath] = Field(
        default_factory=list,
        description="更准但更贵的复检路径，按成本递增。**每一档都要给全**，"
                    "不得按线索等级裁剪 —— 末档（线下司法鉴定）是唯一有法律证明力的路径",
    )
    source_uri: str = Field(
        default="", description="本次实际取用的材料引用（原文照抄，便于复核）"
    )
    fetched_from: str = Field(
        default="",
        description="字节来源的**溯源字段**：inbox / inline_base64 / network / local_file。"
                    "不许含糊 —— 本机读到的写成 network 会误导复核",
    )
    conclusion: str = Field(
        description="面向用户的结论，通俗中文。**不允许出现「真」「假」「伪造」"
                    "「P 过」这类断言性结论**，只能说「存在与 X 一致的线索」"
                    "「某项提示异常」「无法判定」"
    )
    reasoning: str = Field(
        description="从线索到结论的链条，并写明不确定性：哪些项支持、哪些项没跑、"
                    "因此这个等级是怎么定下来的"
    )
    disclaimer: str = Field(
        default="本报告是**技术线索**，不是司法鉴定意见，不具备司法鉴定效力。"
        "涉及法律责任时，请通过有资质的司法鉴定机构办理，并保全原始载体"
        "（原始存储卡 / 手机 / 只读镜像），事后无法补。",
        description="效力声明",
    )

    @model_validator(mode="after")
    def _undecidable_needs_reasons(self) -> "EvidenceRuling":
        """「无法判定」必须说清**是哪几项没跑**。

        这是本项目最难发现的一类缺陷的代码化：`evidence-image` 曾经把一张
        92% 纯色的图报成「综合线索等级：低」—— 而真相是**内容层的检查一项都没跑**
        （`ela` / `noise_residual` / `copy_move` 因"非自然摄影内容"整项弃权），
        只剩封装层还能出结论，而封装层从来就不检测画面内容。
        断言全绿、代码跑得通、结论是错的。

        ⇒ 判据是「**不是"没发现问题"，而是"根本没检查"**」。
        这里把它钉在契约上：说"无法判定"就必须点名没跑的项，不能给一句光秃秃的结论。
        （反向不成立：有正向触发的同时也可以有没跑的项，那时等级照样可以是低/中/高。）
        """
        if self.suspicion == "无法判定" and not self.not_run:
            raise ValueError(
                "suspicion=「无法判定」时必须用 not_run 列出**是哪几项没跑** —— "
                "不给理由的「无法判定」等于把'没检查'说成了'没问题'"
            )
        return self

    @model_validator(mode="after")
    def _level_needs_effective_findings(self) -> "EvidenceRuling":
        """给出「低 / 中 / 高」时，必须至少有一条**给出了有效结论**的线索。

        ⚠️ 上面那条只挡住了"无法判定却不给理由"**这一半**；另一半是它的镜像：
        `suspicion="低"` + `findings=[]`（**一项都没检查**）照样会被接受 ——
        那同样是"静默的安全结论"，而且更隐蔽：它连一句"我没查"都没有。

        判据**照抄检测层 `forensics.summarize()` 的不变式**，不是新发明的规矩：
        那边只有在"至少存在一条非弃权线索"时才可能给出低 / 中 / 高
        （`if not decisive: return "无法判定"`）。所以这里要求"**同一条不变式**"
        在汇总层也成立 —— 一份诚实的转述必然满足它，**不会误杀任何正常报告**；
        会被挡下的只有"什么都没跑却给了个让人安心的档位"。

        ⚠️ 用的是"**有一条 `verdict != "无法判定"` 的线索**"而不是"`findings` 非空"：
        整项弃权的检查项**也会**进 `findings`（这是刻意的，见第 7 节元验证），
        所以只判"非空"根本挡不住"全部弃权却报低"。

        反向不成立（不许矫枉过正）：有未适用项、同时也有有效线索 ⇒ 低 / 中 / 高照常合法。
        """
        if self.suspicion == "无法判定":
            return self
        if not any(f.verdict != "无法判定" for f in self.findings):
            raise ValueError(
                f"suspicion=「{self.suspicion}」时，findings 里必须至少有一条给出了有效结论"
                "（`verdict` 不是「无法判定」）。现在是一条都没有 —— "
                "这等于**一项都没检查**却给出了一个让人安心的档位。"
                "如果本次确实没能得到任何有效结论，请把 suspicion 改成「无法判定」"
                "并用 not_run 列出是哪几项没跑。"
            )
        return self


# ---------------------------------------------------------------------------
# 八、辩论机制常量（被 agents.py / debate.py / 提示词共同引用，单一事实源）
# ---------------------------------------------------------------------------

DEBATE_MAX_ROUNDS = 6          # 轮数上限：DeepSeek 推理模型单轮成本不低，6 轮够覆盖主论点
DEBATE_CONFIDENCE_FLOOR = 0.3  # 置信度下限
DEBATE_DECLINE_PATIENCE = 2    # 连续下降几轮后触发终止
MAX_CHAIN_DEPTH = 3            # 委托链深度上限（补 wit 未实现的 AAC 约束）

DEBATE_RULES_BRIEF = f"""辩论规则（必须遵守）：
- 每轮调用对应工具提交结构化信号：round / stance / confidence / key_point / rebuts。
- 终止条件（任一满足立即停止，不要恋战）：
  ① 达到 {DEBATE_MAX_ROUNDS} 轮；
  ② 任一方 stance = concede；
  ③ 连续 {DEBATE_DECLINE_PATIENCE} 轮 confidence 下降且末值 < {DEBATE_CONFIDENCE_FLOOR}。
- 立场是**随机分配**的，与事实真相无关。你的任务是为该立场构建尽可能强的论证，
  而不是"说出你认为的真相"。敷衍放水会让整场辩论失去信息量。
- 委托链深度上限 {MAX_CHAIN_DEPTH} 层，且禁止出现环（A→B→A）；
  你已经收到的调用方不得再次被委托。"""


# ---------------------------------------------------------------------------
# 供 Agent instructions 复用的量表说明（单一事实源，避免多处硬编码不一致）
# ---------------------------------------------------------------------------

HARM_SCALE_BRIEF = """社会危害性指数 harm_index 的取值范围与含义：
  +81 ~ +100  涉嫌违法：涉嫌刑事犯罪（如煽动分裂国家、煽动民族仇恨、编造传播虚假信息等）
  +61 ~  +80  高风险：明确号召违法行为或制造群体对立，应追究责任
  +41 ~  +60  中高风险：煽动性明显，可能违反治安管理或平台规范
  +21 ~  +40  风险上升：群体标签化，存在扩散风险
   +1 ~  +20  低风险：用词偏激片面，但未指向具体危害后果
         0   中性：纯事实陈述或与公共议题无关
  -20 ~   -1  正当批评：情绪化但属正常批评
  -60 ~  -21  依法表达：依法表达诉求或对公共事务提出异议
 -100 ~  -61  完全正当：依法维权、公益监督、学术讨论等受宪法和法律保护的言论
判分必须与 six 维度分项自洽：分项越高，harm_index 应越偏向正数。"""
