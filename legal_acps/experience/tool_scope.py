"""把一次模型调用约束成**只做这一件事**：只给这一个工具，并给足这一次的 token。

═══ 为什么需要它：真机跑出来的，不是设计推演出来的 ═══

两个都由 2026-10-01 夜在服务器上跑真模型（`deepseek-v4-pro`）暴露，
而且**离线都看不见**。

**① 工具：一轮里同时摆两条能力，模型会挑错（或干脆不挑）。**
裁判这一个 Agent 同时挂着 `case.open` 立题 / `case.reveal` 揭示。
假模型对 `request.tools` 里**每个**它认识的工具都返回调用（见 `stub_model.complete`），
所以"两条能力同时挂着"在假模型那里永远不出岔子 —— 它不会挑错。真模型会：

    受理 2 步 / 立题 2 步 / 诱导方每轮 2 步          ← 只挂一个工具的环节，稳
    揭示 5 步 · 3 步 · **一步都不调**                 ← 同时挂着两个工具的那一环

**② token：揭示是全链路上下文最长的一环，8192 会被思考过程吃光。**
`llm.py` 早就把这条失效模式写在模块 docstring 里了：

    「若 max_tokens 给小了，token 会被思考过程吃光，content 会是空字符串
      —— 表现为『模型没输出』。」

而 `DEFAULT_MAX_TOKENS = 8192` 是**全局写死的**，没有按环节区分。
揭示那一轮要通读全场交锋记录 + 话术 JSON，上下文远长于其它环节；
**5 轮交锋比 3 轮更长**，于是它成了唯一会偶发"空输出"的一环。
真机诊断把它钉死了（`session.py` 的 `last_run`）：

    {"tool": "case_reveal", "attempts": [
       {"status": "completed", "iterations": 1, "max_steps": 12, "tools_seen": []},
       {"status": "completed", "iterations": 1, "max_steps": 24, "tools_seen": []}]}

`iterations=1` + `tools_seen=[]` = 第一步就结束了、一个工具都没调 ——
**和步数预算毫无关系**（这一步把"预算不够"那个假设否掉了：`max_steps` 加到 24 也一样）。

═══ 手法与主链路同源 ═══

`legal_acps/termination.py` 已经用真机验证过：推理模型的 thinking 模式**拒绝
`tool_choice`**（DeepSeek 实测 `HTTP 400 Thinking mode does not support this
tool_choice`），所以"逼模型用某个工具"只能靠**收敛本轮的 `request.tools`** ——
纯本地行为，不经网关、不依赖任何 provider 特性。
`max_tokens` 同理：它不是 provider 的保留键，写进 `request.settings` 会原样透传。

所以这里复用它的判定方式，连取工具名都直接用它的函数：那是同一件事
（"从 `request.tools` 的元素里取出模型看到的工具名"），再写一份就是
本项目反复吃亏的"同一事实散在多处"。

═══ 为什么用 metadata 而不是构造参数 ═══

同一个裁判 Agent 的两条能力分属**两个环节**，所以"这一轮只留哪个工具、给多少 token"
是**每次调用**的属性，不是 Agent 的属性。`RunConfig.metadata` 正是"每次调用的属性"
该待的地方，Hook 从 `ctx.config.metadata` 读它，一个 Hook 实例就能服务所有环节。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from wit import HookContext, ModelRequest, ModelResponse, WitHooks

# 复用主链路的实现，不重写第二份。
from ..termination import _tool_name_of

#: `RunConfig.metadata` 里指定"本轮只暴露哪个工具"的键。
#: 值应当是**模型工具名**（能力 id 把点换成下划线，如 `case.open` → `case_open`）。
ONLY_TOOL_KEY = "experience.only_tool"

#: `RunConfig.metadata` 里指定"本轮给多少 token"的键。
#: 不指定就沿用 provider 构造时的值（`llm.DEFAULT_MAX_TOKENS`）。
MAX_TOKENS_KEY = "experience.max_tokens"


class ToolScopeHooks(WitHooks):
    """把每一轮的模型请求收敛成"只做这一件事"。

    两个动作都**只在调用方明确要求时**才做：

    · `ONLY_TOOL_KEY` → 把 `request.tools` 收敛到只剩那一个工具；
    · `MAX_TOKENS_KEY` → 覆盖本次请求的 `max_tokens`。

    没有要求时**完全不动请求** —— 它是"调用方要求收敛"才收敛，
    不是一个"自动只留第一个工具"的黑箱。默认放行这一点很重要：收敛是**调用方的
    意图**（"这一轮就是要交这个结论"），Hook 只负责执行，不替它猜。
    """

    # 用类属性而不是 property：wit 框架里存在按类读取 `hook.id` 的写法，
    # 其它 Hook（termination.py / chain_guard.py）也都是类属性。
    id = "experience.tool-scope"
    priority = 800  # 靠近内层，让别的 Hook 先决定请求长什么样

    async def wrap_model_call(
        self,
        ctx: HookContext,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        _apply_tool_scope(ctx, request)
        _apply_max_tokens(ctx, request)
        return await handler(request)


def _apply_tool_scope(ctx: Any, request: ModelRequest) -> None:
    only = only_tool_of(ctx)
    if not only:
        return

    originals = list(getattr(request, "tools", None) or [])
    keep = [tool for tool in originals if _tool_name_of(tool) == only]

    # ⚠️ `keep` 为空时**不动请求**。
    #    强行收敛会把工具清单清空，那就等于把"这一轮必须交结论"悄悄换成了
    #    "这一轮只能输出文本" —— 是另一种失败，而且更难查（run 会以
    #    completed 正常收尾，`_run` 只是取不到东西）。
    #    宁可原样放行，让调用方按它自己的方式处理。同一处置见 termination.py。
    if keep and len(keep) != len(originals):
        request.tools = keep


def _apply_max_tokens(ctx: Any, request: ModelRequest) -> None:
    wanted = max_tokens_of(ctx)
    if not wanted:
        return
    settings = getattr(request, "settings", None)
    # ⚠️ `settings` 必须是 dict 才动：`_settings_for()` 把非保留键原样透传给 SDK。
    #    拿不到 dict 就放行 —— 这只是一个**预算**，不是正确性条件。
    if isinstance(settings, dict):
        settings["max_tokens"] = wanted


def only_tool_of(ctx: Any) -> str:
    """从本轮上下文里读出"只留哪个工具"；没指定过就是空串。"""
    value = _metadata(ctx).get(ONLY_TOOL_KEY)
    return value if isinstance(value, str) else ""


def max_tokens_of(ctx: Any) -> int:
    """从本轮上下文里读出"给多少 token"；没指定过就是 0。"""
    value = _metadata(ctx).get(MAX_TOKENS_KEY)
    return int(value) if isinstance(value, int) and value > 0 else 0


def _metadata(ctx: Any) -> dict:
    metadata = getattr(getattr(ctx, "config", None), "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


__all__ = [
    "MAX_TOKENS_KEY",
    "ONLY_TOOL_KEY",
    "ToolScopeHooks",
    "max_tokens_of",
    "only_tool_of",
]

