"""按环节的 token 预算 —— 以及「**被截断**」必须留痕。

2026-10-06 真机夹出，**全部是实测，不是推断**：

  · `llm.DEFAULT_MAX_TOKENS = 8192` 是**全局写死**的，不按环节区分；
  · 本部署的 `deepseek-v4-pro` 是**推理模型**：思考（`reasoning_content`）
    **也占** `max_tokens`；
  · 音频链那一跳实测：`finish_reason = length`、`completion_tokens = 8192`
    （**正好打满**）、`reasoning_tokens = 7256` ⇒ `content` 没有额度
    ⇒ 没有正文、也没有工具调用 ⇒ 主控判「这一轮说完了」
    ⇒ 1 轮即 `completed`、**零产物**。

🔴 **空转的一轮与跑完的一轮长得一模一样**：`status=completed`、无异常、
   台账两行齐全 —— 唯一区别是 `output/` 里没有新产物。所以本模块做**两件事**，
   而且**两件都要**（只做放大，下次换个环节照样静默空转）：

   1. **给重环节放大预算**：`wrap_model_call` 进站时写 `settings["max_tokens"]`；
   2. **被截断时必须留痕**：出站时看 `finish_reason`，写进 `ctx.state.metadata`
      ⇒ 真机探针据此**报红**，不许它继续显示成「跑完了」。

⚠️ 为什么**不许**复用 `experience/tool_scope.ToolScopeHooks`：
   它还带「只留一个工具」的收敛（`only_tool`），挂到法律角色上会把工具清单
   清掉 —— 那是**另一种**、且更难查的失败（run 照样 `completed`，只是
   「取不到东西」）。所以这里另起一个**只管预算**的 Hook。

⚠️ 本 Hook 挂在**角色**上（`agents.py`），不是挂在某一次 run 上：
   「重」是环节的**固有属性**（证据链的输入天生就长），不是某一次任务的偶然。
"""

from __future__ import annotations

from typing import Any

from wit import WitHooks

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 重环节的预算。依据：音频链实测需要 ≈18700 token（reasoning 31044 字），
#: 取 32768 是**留了余量**，不是拍脑袋。
#: ⚠️ 改这个数必须有实测依据 —— 它是**成本旋钮**，放多大就是多花钱。
HEAVY_MAX_TOKENS = 32_768

#: 哪些角色算「重」。按**实测**划，不按感觉划：
#: 本轮撞上限的是音频链；四条证据链的**输入形态同类**（降级后的测量报告 +
#: 长任务文本）⇒ 风险相近，一起算重；`leader` 要综合三份报告并写终稿。
#: ⚠️ 与 `check_local` 那条判据是**同一个集合**：判据按这里的定义**现场派生**，
#:    加一个角色忘了挂 ⇒ 判据立刻报出来（不许「登记表写死 = 静默漏项」）。
HEAVY_ROLES: tuple[str, ...] = (
    "leader",
    "evidence-leader",
    "evidence-image",
    "evidence-audio",
    "evidence-video",
)

#: 留痕用的键（写进 `state.metadata`）。
TRUNCATED_KEY = "token.finish_reason_length"
TRUNCATED_COUNT_KEY = "token.finish_reason_length.count"


def note_truncation(state: Any, response: Any) -> bool:
    """响应以 `finish_reason=length` 结束时，在 `state.metadata` 里留痕。

    返回 `True` 表示「截断**发生了且记到了**」。

    ⚠️ 「没截断」与「截断了但**记不下来**」都返回 `False` ——
    后者只在 `state` 是 `None` / `metadata` 不是 dict 时发生，而它会把
    「截断过」说成「没截断」，正是本项目最危险的失败形态
    （**静默给安全结论**）。
    ⇒ 所以**读**的那一边必须走 `truncated_of()`：它把「读不出来」报成 **-1**，
    与「判定过、没截断」（0）**分得开**。
    """
    if getattr(response, "finish_reason", None) != "length":
        return False
    metadata = getattr(state, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    metadata[TRUNCATED_KEY] = True
    metadata[TRUNCATED_COUNT_KEY] = int(metadata.get(TRUNCATED_COUNT_KEY, 0)) + 1
    return True


def truncated_of(result: Any) -> int:
    """从 run 结果里读出「这一轮被截断过几次」。

    ⚠️ **三态**，不许并成两态：

      · `0`   = 判定过，**没有**截断；
      · `n>0` = 截断过 n 次；
      · `-1`  = **无法判定**（拿不到 `result.state.metadata`）。
                「没得比」与「一样」必须分得开（本项目老规矩）。
    """
    state = getattr(result, "state", None)
    metadata = getattr(state, "metadata", None)
    if not isinstance(metadata, dict):
        return -1
    if not metadata.get(TRUNCATED_KEY):
        return 0
    n = metadata.get(TRUNCATED_COUNT_KEY, 0)
    return int(n) if isinstance(n, int) and n > 0 else 0


class TokenBudgetHooks(WitHooks):
    """只管 token 预算：**放大** + **截断留痕**，别的什么都不做。

    ⚠️ 刻意**不**碰 `request.tools`：那件事归 `experience/tool_scope` 管，
    而它在本项目里的语义是「只留一个工具」—— 混进来的后果是把工具清单清掉。
    """

    id = "legal.token-budget"

    def __init__(self, *, max_tokens: int = HEAVY_MAX_TOKENS) -> None:
        self._max_tokens = int(max_tokens) if max_tokens else 0

    @property
    def max_tokens(self) -> int:
        """本 Hook 会给请求设的预算；`0` = 不动（沿用 provider 的默认）。"""
        return self._max_tokens

    async def wrap_model_call(self, ctx: Any, request: Any, handler: Any) -> Any:
        self._apply_budget(request)
        response = await handler(request)
        # ⚠️ 留痕失败**不许**把这一轮顶掉：预算是成本问题，留痕是**可观测性**
        # 问题 —— 让它掀桌等于「旁路记录有权限顶掉主流程」，本项目明令禁止。
        note_truncation(getattr(ctx, "state", None), response)
        return response

    def _apply_budget(self, request: Any) -> None:
        if not self._max_tokens:
            return
        settings = getattr(request, "settings", None)
        # ⚠️ 只有 dict 才动：拿不到 dict 就放行 —— 预算是**优化**，不是正确性
        # 条件（与 `experience/tool_scope._apply_max_tokens` 同一处置）。
        if isinstance(settings, dict):
            settings["max_tokens"] = self._max_tokens


__all__ = [
    "HEAVY_MAX_TOKENS",
    "HEAVY_ROLES",
    "TRUNCATED_COUNT_KEY",
    "TRUNCATED_KEY",
    "TokenBudgetHooks",
    "note_truncation",
    "truncated_of",
]
