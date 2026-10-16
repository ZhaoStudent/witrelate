"""音频委派闸门 —— 带音频的轮次里，**委派出手之前不许出终稿**。

════════════════════════════════════════════════════════════════════════
为什么必须有它（2026-10-06 真机实测，当时判据是全绿的）
════════════════════════════════════════════════════════════════════════

`audio_modality` 把 `AudioPart` 降级成一段文本，而那段文本里带的是
**完整的技术测量报告**（`_measure()` 的逐项检测 + 综合线索等级）。
主控因此「手上已经有读数」，于是：

    用户问「这段录音有没有被剪过」
      → 主控读完自己上下文里的那份报告
      → **自己**出技术结论
      → 直接 `legal_issue_verdict`
      → 音频鉴伪分支一次都没走

那一轮 `status=completed`、台账齐全、零异常，而 `output/` 里**没有新的
`evidence-*.json`**：整条音频链被**静默绕过**。判据 A′/B 报红、C 反而报绿
—— 因为主控拿自己那份降级读数写进了终稿。

⚠️ 提示词里「这一步请在**出最终结论之前**做完」当时**已经写了**
   （`meta_verify_run_ledger` 用例 Y 守着它），真机上模型仍然不照做。
   ⇒ 「提示词改了 ≠ 模型照做」第二次被坐实 ⇒ 这里做的是**闸门**，
   不是又一句提示词。

════════════════════════════════════════════════════════════════════════
放行条件（三条，缺一不可）
════════════════════════════════════════════════════════════════════════

① 本轮**有音频字节可送** —— 判据与 `audio_modality` 里「能不能测量」同源
   （`data` 非空，或 `uri` 以 `inbox:` / `data:` 开头）。
   ⚠️ 只有 http 地址时**不拦**：那一侧取不到字节，派了也没用，
      提示词对它的处置是「如实回报请对方重发附件」。
② 本轮**还没有试过一次委派** —— 试过一次（成功或失败）**立即放行**。
   ⚠️ 这是「不卡死」的保证：闸门只负责**逼出第一次尝试**，不负责「委派必须成功」。
③ 只对**终稿工具**生效（工具名取自 `termination.VERDICT_TOOL`，单一来源）。

外加一条**兜底放行**：连拦 `max_blocks` 次仍不委派 ⇒ 放行。宁可丢一条委派，
也不能把一次本来能交付的研判变成零产物 —— 「闸门把 run 卡死」比「委派没走」
更糟，而后者至少还有判据报得出来。

════════════════════════════════════════════════════════════════════════
为什么只管音频
════════════════════════════════════════════════════════════════════════

因为这个缺陷的成因是**音频降级文本里带了完整测量报告**，主控才敢自证。
视频降级同样给报告，但那条链另有判据盯着；这里刻意**只做真机实测过的那一支**
—— 把没实测过的分支一起拦进去，就是拿没验证过的行为去改一条正在跑的链路。
"""

from __future__ import annotations

from typing import Any

from wit import WitHooks

from .termination import VERDICT_TOOL

#: 委派工具。**单一来源**：`evidence_hooks.EvidenceInboxHooks` 用的也是这个名字，
#: 两处必须指向同一个工具，否则「派过一次」会被记在一个没人用的名字上。
DELEGATE_TOOL = "acps_delegate"

#: 记账键：本轮**是否已经试过一次委派**。
DELEGATED_KEY = "legal.audio-gate.delegated"
#: 记账键：本轮**拦了几次**终稿（给探针与自检看：区分「闸门逼出来的」与「模型自觉派的」）。
BLOCKS_KEY = "legal.audio-gate.blocks"

#: 连拦这么多次仍不委派 ⇒ 放行（fail-open）。见模块 docstring 最后一段。
DEFAULT_MAX_BLOCKS = 2

#: 部件的**类名**。按类名认而不是 `isinstance`，是为了不在模块级 import
#: 具体的 Part（`check_local` 与自检都要能 import 本模块而不拉重依赖）。
AUDIO_PART_TYPE = "AudioPart"

#: 这两种 uri 形态**带得动字节** —— 与 `audio_modality._report_parts` 同口径：
#: 除了它们，其余 uri 在本项目里一律取不到（`audio/fetch.py` 刻意不出网）。
SENDABLE_URI_PREFIXES = ("inbox:", "data:")

#: 优先级。**越大越靠内层**（wit 的约定），取 90 ⇒ 比 `EvidenceInboxHooks`（100）
#: 更靠外 ⇒ 委派调用先经过本闸门记一笔，再交给收件箱 Hook 换字节。
GATE_PRIORITY = 90


def inbound_audio(state: Any) -> list[Any]:
    """本轮消息里**可派发的音频部件**（现场派生，不写死数量）。

    ⚠️ 认的是「有字节可送」，不是「提到音频」：只有 http 地址的部件不算 ——
       那一侧取不到字节，拦住它只会让 run 卡在一个注定失败的委派上。
    """
    found: list[Any] = []
    for message in getattr(state, "messages", None) or []:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            content = [content]
        for part in content:
            if part is None or type(part).__name__ != AUDIO_PART_TYPE:
                continue
            if getattr(part, "data", None):
                found.append(part)
                continue
            uri = str(getattr(part, "uri", "") or "")
            if uri.startswith(SENDABLE_URI_PREFIXES):
                found.append(part)
    return found


class AudioDelegationGateHooks(WitHooks):
    """拦在**终稿工具**前面：本轮有音频却一次都没派过 ⇒ 拒绝出终稿。

    拒绝不是一句「不行」，而是**带出路的**结构化错误（与
    `debate.DebateChainGuardHooks._reject` 同款形状），模型拿到后知道下一步做什么。
    """

    id = "legal.audio-gate"
    priority = GATE_PRIORITY

    def __init__(
        self,
        *,
        conclusion_tool: str = VERDICT_TOOL,
        delegate_tool: str = DELEGATE_TOOL,
        max_blocks: int = DEFAULT_MAX_BLOCKS,
    ) -> None:
        self.conclusion_tool = str(conclusion_tool)
        self.delegate_tool = str(delegate_tool)
        self.max_blocks = max(0, int(max_blocks))
        # 只在**拿不到任何每轮容器**时才用；见 `_bag()`。
        self._fallback: dict[int, dict[str, Any]] = {}

    # -- 记账 ------------------------------------------------------------

    def _bag(self, ctx: Any) -> dict[str, Any]:
        """每轮一个的记账袋。

        优先 `state.metadata`：它**每轮新建**（换一轮就是一个新 `AgentState`），
        而且探针能原样读回 —— 拦了几次因此是**可观测**的，不是只有闸门自己知道。
        ⚠️ 反过来用实例属性记账是错的：服务模式下 Hook 实例跨轮复用，
           「上一轮派过了」会漏到下一轮，闸门从此**再也不响**。
        """
        state = getattr(ctx, "state", None)
        for holder in (getattr(state, "metadata", None), getattr(ctx, "private_state", None)):
            if isinstance(holder, dict):
                return holder
        return self._fallback.setdefault(id(state), {})

    def _mark_delegated(self, ctx: Any) -> None:
        self._bag(ctx)[DELEGATED_KEY] = True

    def _count_block(self, ctx: Any) -> int:
        bag = self._bag(ctx)
        bag[BLOCKS_KEY] = int(bag.get(BLOCKS_KEY, 0)) + 1
        return int(bag[BLOCKS_KEY])

    # -- 判与拦 ----------------------------------------------------------

    def _should_block(self, ctx: Any) -> bool:
        bag = self._bag(ctx)
        if bag.get(DELEGATED_KEY):
            return False  # 试过一次就放行 —— 见模块 docstring 放行条件 ②
        if int(bag.get(BLOCKS_KEY, 0)) >= self.max_blocks:
            return False  # 兜底放行：不把 run 卡死
        return bool(inbound_audio(getattr(ctx, "state", None)))

    async def wrap_tool_call(self, ctx: Any, tool_call: Any, handler: Any) -> dict[str, Any]:
        name = getattr(tool_call, "name", "")
        if self.delegate_tool and name == self.delegate_tool:
            self._mark_delegated(ctx)
            return await handler(tool_call)
        if name != self.conclusion_tool or not self._should_block(ctx):
            return await handler(tool_call)
        return self._refuse(tool_call, self._count_block(ctx))

    def _refuse(self, tool_call: Any, blocks: int) -> dict[str, Any]:
        """拒一次终稿，并把**下一步该做什么**原样交给模型。

        ⚠️ 这里必须点破「上下文里那份测量报告不能当鉴证结论用」：
           它正是模型不去委派的原因，不说清楚，模型只会重试同一个调用。
        """
        message = (
            "本轮随任务进来了音频（字节已由系统收存，句柄见任务提示）。"
            "音频鉴伪**必须**先派给证据鉴证 Leader：用 `acps_discover` 查"
            "「证据鉴证 音频鉴伪」，再用 `acps_delegate` 把句柄原文写进请求。"
            "⚠️ 你上下文里那份「测量报告」是**降级产物**，不是鉴证结论 —— "
            "不能拿它出技术结论。"
        )
        return {
            "toolCallId": getattr(tool_call, "id", ""),
            "tool": self.conclusion_tool,
            "capabilityId": self.conclusion_tool,
            "modelToolName": self.conclusion_tool,
            "status": "error",
            "result": None,
            "error": {
                "code": "audio_delegation_required",
                "message": message,
                "how_to_fix": (
                    f"先调 {self.delegate_tool} 把音频交给证据鉴证 Leader，"
                    f"拿到鉴证报告之后再提交 {self.conclusion_tool}。"
                ),
                "blocks": blocks,
            },
            "durationMs": 0,
            "metadata": {"source": self.id},
        }


__all__ = [
    "AUDIO_PART_TYPE",
    "BLOCKS_KEY",
    "DELEGATE_TOOL",
    "DELEGATED_KEY",
    "DEFAULT_MAX_BLOCKS",
    "GATE_PRIORITY",
    "SENDABLE_URI_PREFIXES",
    "AudioDelegationGateHooks",
    "inbound_audio",
]
