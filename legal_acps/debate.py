"""辩论子会话的机制层 —— 终止判定、链深熔断、环检测。

这里**只有纯逻辑**，不依赖网络、不依赖模型，所以可以被完整单测。
Agent 的提示词与 capability 都调用这里的函数，避免"规则写在提示词里、
模型记不住"的经典失败。

════════════════════════════════════════════════════════════════════════
为什么必须用结构化信号，而不能靠自然语言
════════════════════════════════════════════════════════════════════════

"对方被说服了没有"这件事，从一段自然语言里是判不出来的 —— 只能再叫一个
模型去判，成本翻倍且不稳定。所以每轮强制交一个结构化信号：

    {"round": 3, "stance": "hold|concede", "confidence": 0.72,
     "key_point": "…", "rebuts": [1, 2]}

判停只需要读 `stance` 与 `confidence` 两个字段（见 should_terminate）。

════════════════════════════════════════════════════════════════════════
为什么还要链深熔断 + 环检测
════════════════════════════════════════════════════════════════════════

AAC 规范要求对委托链做深度限制、并检测环。实测 wit 走 mTLS 时**没有实现**
这一约束（协议空缺，见 docs/aicomp/收敛作战计划.md §1.3）。
缺了它，`A → B → A → B …` 这种环会静默地把模型额度烧光 ——
在演示现场表现为"跑到一半卡住，日志也不报错"。

所以本项目在自己这一层补上：一个 hop 表 + 深度计数，越界直接抛
`DebateChainError`，让它在工具调用处变成一个**结构化错误观测**，
模型看得见、能改，而不是死循环。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from wit import HookContext, WitHooks

from .contracts import (
    DEBATE_CONFIDENCE_FLOOR,
    DEBATE_DECLINE_PATIENCE,
    DEBATE_MAX_ROUNDS,
    MAX_CHAIN_DEPTH,
    DebateArgument,
    DebateStance,
)


# ---------------------------------------------------------------------------
# 一、终止判定（结构化信号 → 停 / 不停）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopDecision:
    """一次终止判定的结果。"""

    stop: bool
    reason: str

    @property
    def explanation(self) -> str:
        return self.reason or "未触发任何终止条件，可以继续下一轮"


def should_terminate(
    arguments: Sequence[DebateArgument],
    *,
    max_rounds: int = DEBATE_MAX_ROUNDS,
    patience: int = DEBATE_DECLINE_PATIENCE,
    floor: float = DEBATE_CONFIDENCE_FLOOR,
) -> StopDecision:
    """按三条终止条件判断辩论是否该停。

    参数 `arguments` 是**按发生顺序**排列的全部结构化信号（正反混排）。

    三条条件（任一满足即停）：
      ① 达到轮数上限；
      ② 最新一轮任一方的 stance == concede；
      ③ 连续 `patience` 轮 confidence 严格下降，且末值 < floor。

    同一轮里双方都交了信号时，"连续下降"按时间顺序逐条比较，
    因此既覆盖"某一方连续泄气"，也覆盖"双方一起泄气"。
    """
    if not arguments:
        return StopDecision(False, "")

    last = arguments[-1]

    if last.round >= max_rounds:
        return StopDecision(True, f"达到轮数上限 {max_rounds} 轮")

    if last.stance is DebateStance.CONCEDE:
        return StopDecision(
            True, f"第 {last.round} 轮一方认输（stance=concede）"
        )

    # 需要 patience+1 个点才能看出 patience 次下降
    window = patience + 1
    if len(arguments) >= window:
        tail = [max(0.0, min(1.0, float(a.confidence))) for a in arguments[-window:]]
        strictly_declining = all(b < a for a, b in zip(tail, tail[1:]))
        if strictly_declining and tail[-1] < floor:
            return StopDecision(
                True,
                f"连续 {patience} 轮置信度下降，末值 {tail[-1]:.2f} < {floor:g}",
            )

    return StopDecision(False, "")


def terminate_reason(arguments: Sequence[DebateArgument], **kwargs) -> str:
    """给记录用的终止原因；没触发条件时返回空串。"""
    return should_terminate(arguments, **kwargs).reason


# ---------------------------------------------------------------------------
# 二、委托链深度熔断 + 环检测
# ---------------------------------------------------------------------------


class DebateChainError(RuntimeError):
    """委托链越界（超深度，或出现环）。

    刻意不继承 ValueError：它要被 capability 捕获后转成**结构化错误观测**，
    与"模型填错了参数"区分开，排障时一眼能看出是谁的问题。
    """

    def __init__(
        self,
        message: str,
        *,
        chain: Sequence[str],
        target: str,
        hint: str | None = None,
        cause: str = "chain",
    ) -> None:
        super().__init__(message)
        self.chain = tuple(chain)
        self.target = target
        # ⚠️ `hint` 必须**跟着真因走**。2026-10-06 真机实测：目标为空时它仍旧写着
        #    「委托链已经到顶或出现环，请**不要**再向该 Agent 委托」——
        #    而真因是模型没填 `partner_aic`。模型照着这条提示走，
        #    就被推向"别再派了"这个**恰好相反**的方向（连派 9 次，次次如此）。
        #    ⇒ 一条**说错理由**的错误，比没有错误更糟：它把人（和模型）引去修别的。
        self.hint = hint
        self.cause = cause

    def to_error(self) -> dict[str, object]:
        return {
            "code": "debate_chain_rejected",
            "cause": self.cause,
            "message": str(self),
            "chain": list(self.chain),
            "target": self.target,
            "max_depth": MAX_CHAIN_DEPTH,
            "hint": self.hint
            or (
                "委托链已经到顶或出现环。请**不要**再向该 Agent 委托；"
                "把当前掌握的信息作为结论返回给调用方。"
            ),
        }


class ForeignPartnerError(RuntimeError):
    """委派目标**不属于本账号** —— 大概率是 discovery 捞到的外部团队 Agent。

    为什么需要这道闸门（2026-09-30 实测）：

        ADP 发现是**纯语义检索**，没有身份区分度。查「辩论 反驳」返回 7 条，
        其中只有 1 条是本项目的反方，其余是别的团队的辩论/裁决/核查智能体。
        裁判的提示词只说"用 acps_discover 查……拿到两位辩手的 aic"，
        **模型完全可能选中别人家的 Agent** —— 这不是模型的错，
        是发现服务给不出靠得住的区分依据。

        所以在**派发边界**上补一道确定性校验：目标 AIC 必须属于本账号。
        这也是「交互安全管控」（赛题鼓励方向③）的落点 ——
        不仅要有护栏，还要留下**可审计的选人证据**
        （见 `DebateChainGuardHooks.trust_rejections`）。

    刻意与 `DebateChainError` 分开：链越界是"走太远/绕回来"，
    身份不符是"本来就不该发给你"，两者的排障方向完全不同。
    """

    def __init__(
        self,
        message: str,
        *,
        target: str,
        expected_prefix: str,
        allowed: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.target = str(target or "")
        self.expected_prefix = str(expected_prefix or "")
        self.allowed = tuple(str(a) for a in allowed)

    def to_error(self) -> dict[str, object]:
        return {
            "code": "foreign_partner_rejected",
            "message": str(self),
            "target": self.target,
            "expected_account_prefix": self.expected_prefix,
            "allowlisted": list(self.allowed),
            "hint": (
                "该目标不属于本账号，极可能是 discovery 的语义检索捞到的外部团队 Agent。"
                "请改为委托**本项目**的伙伴（ACS 里 name 与本项目一致的那一个）。"
                "确实需要跨团队协作时，把对方 AIC 显式加进 allowed_aics 白名单再说。"
            ),
        }


def account_prefix_of(aic: str) -> str:
    """从 AIC 里取出「账号级前缀」；段数不足时返回空串（= 无法判定）。

    实测本平台 AIC 固定 **10 段**，逐段含义如下（数错一次，已订正）：

        1 . 2 . 156 . 3088 . 1 . BUPT  . 81M3FX . COUJXB . F9XC6K . 0J6D
        └──── OID 前缀 (5 段) ────┘└机构┘└ 账号 ┘└─── 智能体标识 (3 段) ───┘

        1 . 2 . 156 . 3088 . 1 . 0001  . 00001  . EZTK03 . PABX6D . 0JUV
                                 └别的机构┘└别的账号┘

    所以**账号级 = 前 7 段**（机构 + 账号）：

        本项目   1.2.156.3088.1.BUPT.81M3FX
        外部团队 1.2.156.3088.1.0001.00001

    同一账号注册的 Agent 共享这 7 段，它天然就是一个"自己人"白名单。
    段数不足 7 时**不敢猜**，返回空串 —— 调用方据此*不启用*校验，
    而不是把所有目标都判成外部（那会把演示链路直接掐死）。
    """
    parts = [p for p in str(aic or "").strip().split(".") if p]
    if len(parts) < 7:
        return ""
    return ".".join(parts[:7])


@dataclass(frozen=True)
class ChainHop:
    """一次被批准的委托。"""

    chain: tuple[str, ...]  # 含本次目标在内的完整链路
    depth: int              # 本次委托的深度（链长）

    @property
    def next_chain(self) -> tuple[str, ...]:
        return self.chain


def plan_next_hop(
    chain: Iterable[str],
    target_aic: str,
    *,
    limit: int = MAX_CHAIN_DEPTH,
    allow_repeat_last: bool = True,
) -> ChainHop:
    """决定"能不能把任务再委托给 target_aic"。

    chain        : 到目前为止的委托链（按顺序的 AIC 列表，不含本次目标）
    target_aic   : 本次想委托的目标
    limit        : 深度上限（含本次），默认 3
    allow_repeat_last
        是否把"再委托给链尾那个下游"当作**重试/追加要求**而不是环。默认 True。

    越界或成环时抛 `DebateChainError`。

    例：
        A→B 时 chain=("A",)，target="B" → 结果链 ("A","B")，depth=2 ✔
        B→A 时 chain=("A","B")，target="A" → 环，抛错 ✘
        链长已到 3 还想再委托 → 抛错 ✘
        重试 B→B（链尾就是 B）→ 放行，depth 不变 ✔

    ⚠️ 最后那条（重试）是补上去的。早期版本只判 `target in current`，
    于是"派发失败 → 模型重试同一目标"会被判成**环**，而且链已被写死，
    该目标在本轮 run 里被永久封禁 —— 一次网络抖动就能让整场辩论发不起来，
    报错还写着"检测到委托环"，把人往错的方向引。
    """
    current = tuple(str(a) for a in chain)
    target = str(target_aic or "").strip()

    if not target:
        raise DebateChainError(
            "委托目标为空，无法建立委托链",
            chain=current,
            target=target,
            cause="missing_target",
            hint=(
                "这次 `acps_delegate` **没有填 `partner_aic`**（或填了空串）——"
                "它**不是**链到顶、也**不是**成环。请先 `acps_discover` 找到目标 Agent，"
                "把它返回的 `aic` **原样填进 `partner_aic` 字段**；"
                "把名字或 AIC 写进请求正文**不算**，那个字段读不到。"
            ),
        )

    # 重试 / 追加要求：目标就是链尾那个下游。不算新的一跳，也不是环。
    if allow_repeat_last and current and target == current[-1]:
        return ChainHop(chain=current, depth=len(current))

    # 环检测优先于深度检测：成环的报错信息更有诊断价值
    if target in current:
        loop = " → ".join(current) + f" → {target}"
        raise DebateChainError(
            f"检测到委托环：{loop}。环会静默烧光模型额度，已拒绝。",
            chain=current,
            target=target,
        )

    depth = len(current) + 1
    if depth > limit:
        raise DebateChainError(
            f"委托链深度 {depth} 超过上限 {limit}（已走 {' → '.join(current)}）",
            chain=current,
            target=target,
        )

    return ChainHop(chain=current + (target,), depth=depth)


#: **载荷键名**（单一来源）：出站写入、入站读取、`chain_from_payload()` 三处共用。
#: ⚠️ 写第二处就会漂移 —— 而漂移的那一半恰好是"跨进程链深"这条链唯一的一根绳。
CHAIN_PAYLOAD_KEY = "debate_chain"


def chain_data_item(chain: Iterable[str]) -> dict[str, Any]:
    """委托链的**跨进程载荷形态**（工具层 JSON）。

    为什么偏偏是这个形状（2026-10-06 实测，别凭印象改）：

      · `acps_delegate` 的 `input_schema` 是
        `required: [partner_aic, request]` + **`additionalProperties: False`**
        ⇒ **加不了**顶层字段（`debate_chain=…` 会被 `ToolArgumentError` 顶回来）；
      · `request` 允许「字符串」或「内容项数组」，数组里允许
        `{"type": "data", "data": {…}}`（即 `StructuredDataItem`）
        ⇒ 这是**唯一**能把结构化元数据带过进程的通道；
      · 它在对端会变回 `DataPart`（`wit.acps.aip.data_item_to_content_part` 实测），
        而 `content_part_text([DataPart])` 是 **空串** ⇒ **不进模型的提示词**，
        只有入站 Hook 看得到。所以"给下游塞了个字段"不会污染下游的推理。

    出站/入站双向都实测过：`content_part_to_data_item(DataPart)` 又变回
    `{"type": "data", "data": {...}}` ⇒ 一个整圆。
    """
    return {"type": "data", "data": {CHAIN_PAYLOAD_KEY: [str(x) for x in chain]}}


def request_with_chain(request: Any, chain: Iterable[str]) -> tuple[Any, bool]:
    """把委托链并进 `acps_delegate` 的 `request` 参数。返回 `(新 request, 有没有并进去)`。

    ⚠️ 必须**保留原请求**：模型写的正文是这次任务的全部内容，
       为了带一个元数据把它丢掉，是拿"任务能做成"换"链深能算"。

    三种入参形态（工具层 schema 只认前两种）：
      · 字符串          → `[{type:text,text:原文}, 链标记]`
      · 内容项数组      → `[...原样..., 链标记]`（**先去掉**旧的链标记，避免重复累积）
      · 其它 / 空       → 原样返回，`injected=False` ⇒ **由调用方留痕**
        （⚠️ 静默返回等于"链没传过去而没人知道"，这正是本项目最忌讳的那种失败）
    """
    marker = chain_data_item(chain)

    if isinstance(request, str):
        items: list[Any] = []
        if request.strip():
            items.append({"type": "text", "text": request})
        items.append(marker)
        return items, True

    if isinstance(request, Sequence) and not isinstance(request, (str, bytes, bytearray)):
        items = [
            item
            for item in request
            if not (
                isinstance(item, Mapping)
                and item.get("type") == "data"
                and CHAIN_PAYLOAD_KEY in (item.get("data") or {})
            )
        ]
        items.append(marker)
        return items, True

    return request, False


def inbound_chain(parts: Iterable[Any]) -> tuple[str, ...]:
    """从**入站内容**里挑出上游带来的委托链（挑不到就空元组）。

    ⚠️ 只认 `DataPart.data[CHAIN_PAYLOAD_KEY]`，不认正文里的任何字样：
       正文是下游（乃至模型）都能写的东西，认它就等于把链深交给对方自报。
    """
    for part in parts or ():
        if type(part).__name__ != "DataPart":
            continue
        data = getattr(part, "data", None)
        if not isinstance(data, Mapping):
            continue
        raw = data.get(CHAIN_PAYLOAD_KEY)
        if raw is None:
            continue
        if isinstance(raw, str):
            return (raw,) if raw.strip() else ()
        if isinstance(raw, Iterable):
            return tuple(str(x) for x in raw if str(x).strip())
    return ()


def chain_from_payload(payload: object) -> tuple[str, ...]:
    """从上游派发的任务载荷里读出委托链。

    约定：请求体里带 `debate_chain`（字符串数组，按顺序的 AIC）。
    读不到就当作"我是第一跳"，即空链 —— 这样单跑某个 Agent 调试时也能工作。

    ✅ **2026-10-06 这条通路通了**（#123）：`DebateChainGuardHooks` 现在会在
    出站委派时把链写进任务载荷（`request_with_chain`），入站时
    `on_run_start` 把它挑回来写进 `AgentState.metadata` —— 于是**跨进程**的
    A→B→A 也挡得住了，不再只有本进程那一半。
    """
    if isinstance(payload, dict):
        raw = payload.get(CHAIN_PAYLOAD_KEY)
    else:
        raw = getattr(payload, CHAIN_PAYLOAD_KEY, None)
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if isinstance(raw, Iterable):
        return tuple(str(item) for item in raw if str(item).strip())
    return ()


# ---------------------------------------------------------------------------
# 三、熔断阀门：在 acps_delegate 这个边界上拦下来
# ---------------------------------------------------------------------------


class DebateChainGuardHooks(WitHooks):
    """在 `acps_delegate` 这个边界上做两件事：**身份校验** + **链深熔断**。

    ⚠️ 挂载位置很关键（2026-09-30 审计发现）：这个 Hook 只有挂到**会委派的
    Agent**（`legal-consult-leader` / `fact-judge`）身上才有效。挂到叶子节点
    （两位辩手）上，因为叶子根本没有 `acps_delegate` 这个工具，
    `wrap_tool_call` 永远不会被触发 —— 等于装了一个从不响的警报器。
    改造期间它一度只挂在辩手上，使「补上 AAC 链深约束」这个创新点
    **在真机上完全空转**（离线自检也测不出来，因为自检是自己构造调用）。

    两件事的分工：
      ① **身份校验** —— 目标 AIC 必须属于本账号（前缀白名单），否则拒绝。
         拦的是 discovery 语义检索捞到的**外部团队 Agent**（见 ForeignPartnerError）。
      ② **链深熔断 + 环检测** —— 拦 "A→B→A" 这类环与超深链，
         补 wit 未实现的 AAC 约束。

    为什么做成 Hook 而不是写在提示词里：提示词是**建议**，模型可以不听；
    而 `wrap_tool_call` 是**边界**，模型在这里没有选择 —— 越界的委派根本发不出去，
    它会拿到一个结构化错误观测
    （code=`debate_chain_rejected` / `foreign_partner_rejected`），
    从而知道"该收手了，回去汇报"，而不是死循环。

    链的来源（按优先级）：
      1. `AgentState.metadata["debate_chain"]` —— 上游派发任务时带进来的整条链，
         这样跨进程的 A→B→A 也能挡住（约定见 `chain_from_payload`）。
         ✅ **2026-10-06 已接通（#123）**：出站由 `request_with_chain()` 把链写进
         `acps_delegate` 的 `request`（走 `StructuredDataItem`），入站由本 Hook 的
         `on_run_start()` 把它挑回来写进 `metadata`。此前这条通路是空的
         （只读取、不写入）⇒ **跨进程的环挡不住**，生效的只有本进程那一半。
      2. 读不到载荷时，链 = `(self.self_aic,)` —— 即"我是这一跳的起点"。

    ⚠️ **链深只算嵌套层数，不算扇出宽度**（2026-09-30 真机修正，详见 `_base_path`）：
       同一个 Agent 连续委派 B、C、D 三个**同级**下游，三者 depth 都是 2；
       早期实现把它们累加成 4 层链，于是撞上 `MAX_CHAIN_DEPTH` 误杀第 3 个委派。
       `self.dispatched` 单独记台账用于溯源，**不参与**深度计算。

    注意：`self_aic` 可以直接给，也可以留空（留空时链首记为 "<self>"，
    只用于本地调试；上生产建议显式传入，报告里的链路溯源表会更好看）。
    """

    id = "legal.debate.chain-guard"
    priority = 50  # 比委派 Hook 更靠外层：先拦，再谈发不发

    def __init__(
        self,
        *,
        self_aic: str | None = None,
        max_depth: int = MAX_CHAIN_DEPTH,
        own_account_prefix: str | None = None,
        allowed_aics: Iterable[str] = (),
        verbose: bool = False,
    ) -> None:
        self.self_aic = (self_aic or "").strip() or "<self>"
        self.max_depth = max(1, int(max_depth))
        self.verbose = verbose
        self.rejections: list[dict[str, object]] = []
        # 归属校验的审计记录：谁被拒了、为什么。这就是"可审计的选人证据"。
        self.trust_rejections: list[dict[str, object]] = []
        # 台账：本轮**成功派发过**的下游（同级扇出会都记在这里，但**不**计入链深）。
        self.dispatched: list[str] = []
        #: 台账：**入站**挑回委托链的痕迹（跨进程那一半的证据）。
        #: ⚠️ 只记"有没有挑到"，不记链本身（链属于那一轮，属于任务库不是 Hook）。
        self.inbound_chain_trace: list[dict[str, object]] = []
        #: 台账：**出站**把链并进载荷的痕迹。`injected=False` 也要记 ——
        #:   那是"这次委派**没把链带过去**"，与"带了"必须分得开，
        #:   否则下游读不到链时无从判断是"上游没写"还是"下游没读"。
        self.outbound_chain_trace: list[dict[str, object]] = []
        self.allowed_aics = tuple(
            str(a).strip() for a in allowed_aics if str(a).strip()
        )
        # ⚠️ 拿不到本账号前缀时**不启用**归属校验（fail-open），而不是把所有目标
        #    都判成外部。理由：这是护栏，误杀会直接掐断合法委派；而"没启用"
        #    只是回到加固前的状态。宁可少一层保护，也不能把演示链路弄断。
        #    未注册 / `<self>` 这类占位串都落在这一支。
        self.own_account_prefix = (
            (own_account_prefix or "").strip() or account_prefix_of(self.self_aic)
        )

    # -- 内部 --------------------------------------------------------------

    def _base_path(self, ctx: HookContext) -> tuple[str, ...]:
        """**到达我自己**的那条链（祖先路径）—— 不含我派出去的下游。

        ⚠️ 2026-09-30 语义修正（真机端到端暴露）。此前这里维护的是"本轮派发过的
        **所有**目标"累加成的线性链，于是**扇出被当成了链深**：

            Leader 依次委派 3 个伙伴 → 链长变 4 → 撞上 MAX_CHAIN_DEPTH=3
            → **第 3 个委派被拒**。

        实测损失（第一次跑通两级嵌套时抓到）：主控的"事实核查"分支里
        网络裁判与煽动识别派出去了，**危害评估专家被拒**，leader 只能在结论里
        如实写"该系统判定委托链深度超限"；而 AAC 语义下这三位都是 depth=2 的
        **同级**，根本不是一条 4 层链。**把 breadth 当 depth 算，必然误杀。**

        现在只有"上游经 `debate_chain` 载荷传进来的祖先路径"算链，本进程内的
        扇出不改变它 —— 链深才等于真正的**嵌套层数**。环检测相应只针对**祖先**：
        A→B 之后 B 再派 A 会被拒（B 的祖先里有 A），而 A 连派 B/C/D 三个同级全部放行。

        返回：祖先路径；读不到载荷时退化为 `(self.self_aic,)`，即"我是第一跳"。
        """
        state = getattr(ctx, "private_state", None)
        if isinstance(state, dict):
            cached = state.get("debate_chain")
            if isinstance(cached, tuple):
                return cached
        inbound = chain_from_payload(getattr(getattr(ctx, "state", None), "metadata", None))
        chain = tuple(inbound) if inbound else (self.self_aic,)
        if isinstance(state, dict):
            # 缓存进 private_state 是为了**只读一次**载荷。
            # ⚠️ 委派成功后**绝不能再往这里追加** —— 追加就等于把扇出算成链深（见上）。
            state["debate_chain"] = chain
        return chain

    def _log_call(self, message: str) -> None:
        if self.verbose:
            print(f"[chain-guard] {message}", file=sys.stderr, flush=True)

    @staticmethod
    def _dispatch_failed(result: Any) -> bool:
        """这次委派是不是明确失败了（失败就不该计入委托链）。"""
        return isinstance(result, Mapping) and result.get("status") == "error"

    def _reject(self, tool_call: Any, exc: Any) -> dict[str, Any]:
        """把一次违规（链越界 / 身份不符）变成**结构化错误观测**。

        `exc` 只需要实现 `to_error()` —— 两类违规各有自己的错误码与提示，
        这里不关心是哪一类，只按 code 分筐留证。
        """
        payload = exc.to_error()
        if payload.get("code") == "foreign_partner_rejected":
            if len(self.trust_rejections) < 64:
                self.trust_rejections.append(payload)
        elif len(self.rejections) < 64:
            self.rejections.append(payload)
        if self.verbose:
            print(f"[chain-guard] 拒绝委派：{exc}", file=sys.stderr, flush=True)
        return {
            "toolCallId": getattr(tool_call, "id", ""),
            "tool": "acps.delegate",
            "capabilityId": "acps.delegate",
            "modelToolName": getattr(tool_call, "name", "acps_delegate"),
            "status": "error",
            "result": None,
            "error": payload,
            "durationMs": 0,
            "metadata": {"source": self.id},
        }

    def _check_partner_trust(self, target: str) -> ForeignPartnerError | None:
        """这个目标是不是"我们自己的"？返回 None 表示放行。

        三条放行理由，缺一不可地写清楚，避免将来被误读成"漏判"：
          ① 没拿到本账号前缀 → 不启用校验（fail-open，见 __init__ 注释）；
          ② 目标为空 → 放行，交给 `plan_next_hop` 去报"委托目标为空"，
             两件事职责分开，报错才不会互相掩盖；
          ③ 目标落在本账号前缀下，或显式出现在 `allowed_aics` 里。
        """
        if not self.own_account_prefix:
            return None
        if not target:
            return None
        if target in self.allowed_aics:
            return None
        prefix = self.own_account_prefix
        if target == prefix or target.startswith(prefix + "."):
            return None
        return ForeignPartnerError(
            f"委派目标 {target} 不属于本账号（本账号前缀 {prefix}），已拒绝派发。",
            target=target,
            expected_prefix=prefix,
            allowed=self.allowed_aics,
        )

    # -- Hook 接口 ---------------------------------------------------------

    def _record_inbound(self, chain: tuple[str, ...]) -> None:
        if len(self.inbound_chain_trace) < 64:
            self.inbound_chain_trace.append(
                {"event": "inbound_chain", "depth": len(chain), "found": bool(chain)}
            )

    def _with_chain_payload(
        self, tool_call: Any, arguments: Any, chain: tuple[str, ...]
    ) -> Any:
        """把链并进这次委派的 `request`（返回一个**新的** `ToolCall`）。

        ⚠️ `ToolCall` 是 frozen dataclass ⇒ 首选 `dataclasses.replace`（换一个新对象，
           调用链上游看到的入参与实际发出的**保持一致**）。
           ⚠️ 但**不能只认这一条路**：万一调用对象不是 dataclass（换框架版本、
           或某个中间层包了一层壳），`replace` 会抛错 —— 那时退一步去改
           `arguments` 这个 dict。**两条路都要能把链带出去**，否则会出现
           "换了条路就静默不传"，而判据跑在另一条路上全绿。
        ⚠️ 两条都失败时**必须留痕并照常发**：链深是**加分项**，它不该有能力
           掐断一次本来合法的委派（那一半归身份闸门与链深熔断管）。
        """
        if not isinstance(arguments, Mapping):
            self._note_outbound(chain, False, "arguments 不是 Mapping")
            return tool_call
        new_request, injected = request_with_chain(arguments.get("request"), chain)
        if not injected:
            self._note_outbound(chain, False, "request 形态认不出")
            return tool_call

        new_call = None
        try:
            new_call = replace(
                tool_call, arguments={**dict(arguments), "request": new_request}
            )
        except Exception:  # noqa: BLE001 - 不是 dataclass：走下面那条路
            new_call = None
        if new_call is not None:
            self._note_outbound(chain, True, "")
            return new_call

        try:
            arguments["request"] = new_request
        except Exception as exc:  # noqa: BLE001 - 换不了就照原样发，不许拖垮委派
            self._note_outbound(chain, False, f"改写 request 失败：{type(exc).__name__}")
            return tool_call
        self._note_outbound(chain, True, "")
        return tool_call

    def _note_outbound(self, chain: tuple[str, ...], injected: bool, why: str) -> None:
        if len(self.outbound_chain_trace) < 64:
            self.outbound_chain_trace.append(
                {
                    "event": "outbound_chain",
                    "depth": len(chain),
                    "injected": injected,
                    **({"why": why} if why else {}),
                }
            )
        if not injected:
            self._log_call(f"委托链**没能**并进载荷（{why}）⇒ 下游不会知道这条链")

    async def on_run_start(self, event: object) -> None:
        """**入站**那一半（#123）：把上游带来的委托链挑回 `AgentState.metadata`。

        ⚠️ 为什么必须在这里做、而不是让 `_base_path` 自己去扫消息：
           `_base_path` 是**每次委派前**都跑的，让它扫就得每次遍历全部消息；
           而入站链在**这一轮开始时**就已经确定了 —— 扫一次写进 metadata，
           既符合"读一次载荷"的既有口径，也让"链是谁带来的"有且仅有一个时刻。
           （它也不该被本轮后续的消息污染：下游交回的结论里不会带链。）

        ⚠️ **挑不到就什么都不写**：那表示"我是这一跳的起点"，由 `_base_path`
           退化成 `(self_aic,)` 去处理。**不许**写一个空链进 metadata ——
           那会让"没带链"与"带了空链"变成两件分不开的事。
        """
        ctx = getattr(event, "context", None)
        state = getattr(ctx, "state", None)
        metadata = getattr(state, "metadata", None)
        if not isinstance(metadata, dict):
            return
        parts: list[Any] = []
        for message in getattr(state, "messages", None) or ():
            parts.extend(getattr(message, "content", None) or ())
        chain = inbound_chain(parts)
        self._record_inbound(chain)
        if chain:
            metadata[CHAIN_PAYLOAD_KEY] = list(chain)
            self._log_call(f"入站委托链：{' → '.join(chain)}（深度 {len(chain)}）")

    async def wrap_tool_call(self, ctx: HookContext, tool_call: Any, handler: Any) -> dict[str, Any]:
        if getattr(tool_call, "name", "") != "acps_delegate":
            return await handler(tool_call)

        arguments = getattr(tool_call, "arguments", None) or {}
        target = ""
        if isinstance(arguments, Mapping):
            target = str(arguments.get("partner_aic") or "")

        # ① 先判身份，再判链 —— 顺序有讲究。
        #    "身份不符"是**这个目标根本不该发**；"链越界"是**这个目标发得太远/绕回来了**。
        #    先判身份才能给出方向正确的提示（选错人 vs 走太远），两者排障方向完全不同。
        foreign = self._check_partner_trust(target)
        if foreign is not None:
            return self._reject(tool_call, foreign)

        # ② 链深熔断 + 环检测
        #    `allow_repeat_last=False`：链尾现在是**我自己**，再"重复链尾"就等于
        #    自己派自己。重试/追加要求不需要靠这个开关了 —— 扇出不再增长 base_path，
        #    同一目标发第二次天然合法（"拿到上游结论再补一份要求"是允许的）。
        try:
            hop = plan_next_hop(
                self._base_path(ctx),
                target,
                limit=self.max_depth,
                allow_repeat_last=False,
            )
        except DebateChainError as exc:
            return self._reject(tool_call, exc)

        # ③ **出站**那一半（#123）：把"到达目标的这条链"写进委派载荷，
        #    下游才可能知道自己是被谁派来的 ⇒ 跨进程的 A→B→A 才挡得住。
        #    ⚠️ 必须在 `handler` 之前：下游读到什么，取决于这里送出去什么。
        tool_call = self._with_chain_payload(tool_call, arguments, hop.next_chain)

        # 只有**派发成功**才把这一跳记进台账 —— 这点很关键。
        # 早期版本在 await handler 之前就落状态，于是一次网络抖动（handler 抛异常
        # 或返回 error 观测）就会把目标永久留在链上，模型重试同一目标立刻被判成
        # "委托环"，辩论再也发不起来。handler 抛异常时不更新（异常原样冒泡）。
        result = await handler(tool_call)
        if not self._dispatch_failed(result):
            if target not in self.dispatched and len(self.dispatched) < 64:
                self.dispatched.append(target)
            state = getattr(ctx, "private_state", None)
            if isinstance(state, dict):
                # 仅供报告/溯源（谁派过谁）。⚠️ **不再**喂回 `debate_chain`：
                # 那正是"扇出变链深"的老 bug。
                state["debate_chain_last"] = hop.next_chain
        else:
            self._log_call(f"派发未成功（{target}），本跳不计入链，允许重试")
        return result


__all__ = [
    "CHAIN_PAYLOAD_KEY",
    "ChainHop",
    "DebateChainError",
    "DebateChainGuardHooks",
    "ForeignPartnerError",
    "MAX_CHAIN_DEPTH",
    "StopDecision",
    "account_prefix_of",
    "chain_data_item",
    "chain_from_payload",
    "inbound_chain",
    "plan_next_hop",
    "request_with_chain",
    "should_terminate",
    "terminate_reason",
]
