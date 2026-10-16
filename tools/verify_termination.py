"""离线验证：Leader 终止不确定性 —— 先复现两种翻车，再证明修复有效。

这个脚本**不联网、不需要 API key、不碰平台**：用假模型 + 假委派工具，
在真实 wit run 循环里跑 Leader，所以它验证的是真实代码路径。

用法（在 witrelate 目录下）：
    ./.venv/bin/python -m tools.verify_termination

════════════════════════════════════════════════════════════════════════
它到底在测什么
════════════════════════════════════════════════════════════════════════

把"假模型"当成一个**不听话的 Leader** 来用，复现实测到的六种形态：

  lazy     两位专家都返回了，模型直接输出一段自然语言收尾，不调 legal_issue_verdict
  loopy    模型停不下来，反复调 acps_task，把 8 轮迭代预算烧完
  starved  专家任务永不返回（模拟对方卡住），预算烧完才算完
  halfdead 一位专家明确失败（state=failed）、另一位正常返回 —— 终态即就绪
  refuse   连"工具被收敛到只剩结论工具"都不买账，仍只输出文本（验证第 5 层兜底）
  good     守规矩的模型（对照组，确认强制 Hook 不会破坏正常流程）

每个形态跑两遍：
  · 不挂 LeaderTerminationHooks → 期望"翻车"
  · 挂   LeaderTerminationHooks → 期望"必出结论"

判据只有一条，也是唯一可信的：**output 目录里有没有 verdict-*.json**。
"status=completed" 不足以证明链路成立 —— 这正是当初误判的根源。
"""

from __future__ import annotations

import dataclasses
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from wit import (
    AcpsDirect,
    ModelResponse,
    OpenAIChatProvider,
    Tool,
    ToolCall,
    WitAgent,
    WitRuntime,
)
from wit.kernel.hooks import RunStartEvent, WitHooks

from legal_acps import agents as agents_mod
from legal_acps.acps_config import build_config
from legal_acps.agents import LEADER_INSTRUCTIONS, submit_final_verdict
from legal_acps.stub_model import CANNED_ARGUMENTS
from legal_acps.termination import VERDICT_TOOL, LeaderTerminationHooks, survey

MAX_STEPS = 8
PARTNER_A = "aic://incitement-detector@wt.ioa.pub"
PARTNER_B = "aic://harm-assessor@wt.ioa.pub"

# 假模型返回的工具参数（沿用 stub_model 里那份能过 schema 的固定值）
VERDICT_ARGS: dict[str, Any] = dict(CANNED_ARGUMENTS["legal_issue_verdict"])

_EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "task_ref": {"type": "string"},
    },
    "required": ["action"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# 一、假委派工具：顶替 AipDelegationHooks，把网络边界换成查表
# ---------------------------------------------------------------------------


class FakeDelegationHooks(WitHooks):
    """注入 acps_discover / acps_delegate / acps_task 三个假工具。

    真 AipDelegationHooks 的 on_run_start 也是干这件事（往 ctx 里 add_tool），
    只是它背后接着 AIP 协调器。这里换成固定返回值，好让整轮 run 离线跑完。
    """

    id = "test.fake-delegation"
    priority = 100

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.calls: list[str] = []

    async def on_run_start(self, event: RunStartEvent) -> None:
        ctx = event.context
        ctx.add_tool(
            Tool(
                name="acps_discover",
                description="发现可用 Partner（离线假实现）",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=self._discover,
                capability_id="acps.discover",
            ),
            source="offline-verify",
        )
        ctx.add_tool(
            Tool(
                name="acps_delegate",
                description="向 Partner 派发任务（离线假实现）",
                input_schema={
                    "type": "object",
                    "properties": {
                        "partner_aic": {"type": "string"},
                        "request": {"type": "object"},
                    },
                    "required": ["partner_aic"],
                    "additionalProperties": True,
                },
                handler=self._delegate,
                capability_id="acps.delegate",
            ),
            source="offline-verify",
        )
        ctx.add_tool(
            Tool(
                name="acps_task",
                description="管理已派发的任务（离线假实现）",
                input_schema=_TASK_SCHEMA,
                handler=self._task,
                capability_id="acps.task",
            ),
            source="offline-verify",
        )

    # -- 三个假工具的行为 --------------------------------------------------

    async def _discover(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append("discover")
        return {
            "agents": [
                {"aic": PARTNER_A, "name": "煽动性识别 Partner", "available": True},
                {"aic": PARTNER_B, "name": "社会危害性评估 Partner", "available": True},
            ]
        }

    async def _delegate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append("delegate")
        aic = str(arguments.get("partner_aic") or "")
        ref = "task-A" if "incitement" in aic else "task-B"
        # 刚派发时状态是 working，还没有任何结论
        return {"task_ref": ref, "partner_aic": aic, "state": "working"}

    async def _task(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append("task")
        ref = str(arguments.get("task_ref") or "task-A")
        if self.behaviour == "starved":
            # 模拟"对方一直不返回"：永远 working、没有 products
            return {"task_ref": ref, "state": "working", "products": []}
        if self.behaviour == "halfdead" and ref == "task-B":
            # 模拟"一位专家明确失败"（真机日志里出现过：Partner 运行中失败、未产出产品）
            return {"task_ref": ref, "state": "failed", "products": []}
        return {
            "task_ref": ref,
            "state": "awaiting-completion",
            "products": [{"type": "data", "data": _partner_deliverable(ref)}],
        }


def _partner_deliverable(ref: str) -> dict[str, Any]:
    """两位专家交回的交付物（形状照抄真实契约，便于第 5 层兜底能真解析出来）。"""
    if ref == "task-A":  # 煽动性识别 + 真伪核查
        return {
            "is_incitement": True,
            "kinds": ["煽动地域歧视"],
            "severity": 62,
            "targets": ["外地务工人员"],
            "evidence_spans": ["那些外地来的打工的闹的", "他们都该滚出去"],
            "action_calls": ["他们都该滚出去"],
            "rationale": "以地域划界、号召排斥特定群体，属煽动地域歧视。",
            "claims": [],
            "overall_truth_score": 35,
            "notes": "整体判断为情绪化归因，缺乏事实依据。",
        }
    return {  # 社会危害性评估
        "harm_index": 58,
        "band": "中高风险",
        "dimensions": {
            "public_order": 70,
            "national_security": 0,
            "group_targeting": 80,
            "real_world_risk": 60,
            "spread_potential": 55,
            "expression_legitimacy": 20,
        },
        "legal_hints": ["《刑法》第249条"],
        "reasoning": "针对特定地域群体的排斥号召，具备现实风险。",
        "counter_view": "若仅为个人情绪宣泄、未号召行动，可减轻评价。",
    }


# ---------------------------------------------------------------------------
# 二、假模型：一个"不听话的 Leader"
# ---------------------------------------------------------------------------


class MisbehavingLeaderModel:
    """按脚本走，并**遵守系统的结构性强制**（真实模型被强制时就是这么做的）。

    两种强制手段都建模了：
      · `settings["tool_choice"]` —— 本项目默认不开启（推理模型会 400）
      · 可用工具被收敛到只剩结论工具 —— 默认路径
    另有 `refuse` 形态：连收敛工具列表都不买账，用来验证第 5 层程序化兜底。
    """

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self._real = OpenAIChatProvider(model="offline", api_key="offline")
        self.capabilities = dataclasses.replace(
            self._real.capabilities, provider="offline-verify", streaming=False
        )
        self.forced_calls = 0
        self.model_calls = 0

    # ModelProvider 协议 -------------------------------------------------

    def supports_content(self, part: Any, *, role: str) -> Any:
        return self._real.supports_content(part, role=role)

    async def aclose(self) -> None:
        return None

    async def complete(self, request: Any) -> ModelResponse:
        self.model_calls += 1

        available = {_tool_name(t) for t in getattr(request, "tools", None) or []}

        # —— 强制手段之一：tool_choice（本项目默认**不**开启，留作可选加强层）
        tool_choice = (getattr(request, "settings", None) or {}).get("tool_choice")
        if isinstance(tool_choice, Mapping):
            name = (tool_choice.get("function") or {}).get("name")
            if name:
                self.forced_calls += 1
                return ModelResponse.tools(
                    [
                        ToolCall(
                            id=f"forced-{self.model_calls}",
                            name=str(name),
                            arguments=VERDICT_ARGS,
                        )
                    ]
                )

        # —— 强制手段之二（默认路径）：可用工具被收敛到只剩结论工具。
        #    真实模型在这种情况下基本只能交出结论，这里照此建模。
        if available == {VERDICT_TOOL}:
            if self.behaviour == "refuse":
                # 极端不听话：宁可输出一段文本也不交结论 → 交给第 5 层程序化兜底
                return ModelResponse.final("（refuse）我就是不交结论，只写一段话。")
            self.forced_calls += 1
            return ModelResponse.tools(
                [
                    ToolCall(
                        id=f"forced-{self.model_calls}",
                        name=VERDICT_TOOL,
                        arguments=VERDICT_ARGS,
                    )
                ]
            )

        # —— 第 4 层：工具被完全收回 → 只能纯文本收尾
        if not available:
            return ModelResponse.final("（收尾）工具已被系统收回，研判结束。")

        tool_msgs = [m for m in request.messages if getattr(m, "role", None) == "tool"]
        seen = len(tool_msgs)

        # 第 0~4 次调用：正常走"发现 → 派发两位专家 → 等两条结果"
        script: list[tuple[str, dict[str, Any]]] = [
            ("acps_discover", {"query": "煽动性识别"}),
            ("acps_delegate", {"partner_aic": PARTNER_A, "request": {"text": "…"}}),
            ("acps_delegate", {"partner_aic": PARTNER_B, "request": {"text": "…"}}),
            ("acps_task", {"action": "wait", "task_ref": "task-A"}),
            ("acps_task", {"action": "wait", "task_ref": "task-B"}),
        ]
        if seen < len(script):
            name, args = script[seen]
            if name in available:
                return ModelResponse.tools(
                    [ToolCall(id=f"step{seen}", name=name, arguments=args)]
                )
            return ModelResponse.final("（假模型）工具没注入成功，退出。")

        # —— 从这里开始体现"不听话"
        if self.behaviour == "good":
            if "legal_issue_verdict" in available:
                return ModelResponse.tools(
                    [
                        ToolCall(
                            id="good-verdict",
                            name="legal_issue_verdict",
                            arguments=VERDICT_ARGS,
                        )
                    ]
                )
            return ModelResponse.final("（good）结论已提交，研判完成。")

        if self.behaviour in ("lazy", "halfdead"):
            # 翻车形态 a：直接自然语言收尾，绝不调用 legal_issue_verdict
            return ModelResponse.final("（lazy）我的判断是：这段话有问题。就这样。")

        if self.behaviour == "refuse":
            # 翻车形态 d：即便被强制（工具只剩结论工具）也拒不交结论
            return ModelResponse.final("（refuse）我就是不交结论，只写一段话。")

        # 翻车形态 b / c：停不下来，继续调工具，直到预算烧光
        if "acps_task" in available:
            return ModelResponse.tools(
                [
                    ToolCall(
                        id=f"loop-{self.model_calls}",
                        name="acps_task",
                        arguments={"action": "refresh", "task_ref": "task-A"},
                    )
                ]
            )
        return ModelResponse.final("（loopy）没有工具可调了。")


def _tool_name(tool: Any) -> str:
    name = getattr(tool, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(tool, Mapping):
        raw = tool.get("name")
        if isinstance(raw, str):
            return raw
        fn = tool.get("function")
        if isinstance(fn, Mapping) and isinstance(fn.get("name"), str):
            return fn["name"]
    return ""


# ---------------------------------------------------------------------------
# 三、跑一次
# ---------------------------------------------------------------------------


def build_leader(*, behaviour: str, with_guard: bool, model: Any) -> WitAgent:
    hooks: list[WitHooks] = [FakeDelegationHooks(behaviour)]
    if with_guard:
        hooks.append(LeaderTerminationHooks(expected_partners=2, verbose=False))
    return WitAgent(
        id="legal-consult-leader",
        display_name="法律咨询主控（离线验证）",
        description="离线验证 Leader 终止行为，不联网、不注册。",
        version="1.0.0",
        instructions=LEADER_INSTRUCTIONS,
        model=model,
        capabilities=[submit_final_verdict],
        hooks=hooks,
        protocols=[AcpsDirect(modes=("rpc",))],
    )


async def run_once(*, behaviour: str, with_guard: bool) -> dict[str, Any]:
    model = MisbehavingLeaderModel(behaviour)
    agent = build_leader(behaviour=behaviour, with_guard=with_guard, model=model)

    outdir = Path(tempfile.mkdtemp(prefix="verdict-out-"))
    original = agents_mod.OUTPUT_DIR
    agents_mod.OUTPUT_DIR = outdir  # submit_final_verdict 在调用时读这个全局
    try:
        runtime = WitRuntime(
            agent,
            workspace=".wit-t/termination-verify",
            acps=build_config(role="leader"),
        )
        run = await runtime.run(
            "请研判下面这段文本：\n\n"
            "最近治安这么差，还不都是那些外地来的打工的闹的。他们都该滚出去。",
            config=None,
        )
        verdicts = sorted(p.name for p in outdir.glob("verdict-*.json"))
        progress = survey(run.messages)
        guard = next(
            (h for h in agent.hooks if isinstance(h, LeaderTerminationHooks)), None
        )
        return {
            "status": run.status,
            "iterations": run.iterations,
            "final_text": (run.final_text or "").strip(),
            "model_calls": model.model_calls,
            "forced_calls": model.forced_calls,
            "verdict_files": verdicts,
            "verdict_submitted": progress.verdict_submitted,
            "partner_results": progress.partner_results,
            "guard_trace": list(guard.trace) if guard else [],
            "fallback_path": getattr(guard, "fallback_path", None),
        }
    finally:
        agents_mod.OUTPUT_DIR = original
        shutil.rmtree(outdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 四、主流程
# ---------------------------------------------------------------------------

CASES: list[tuple[str, str, bool]] = [
    # (说明, behaviour, 是否挂强制 Hook)
    ("lazy   · 不挂 Hook（复现翻车 a）", "lazy", False),
    ("lazy   · 挂 Hook（修复）", "lazy", True),
    ("loopy  · 不挂 Hook（复现翻车 b）", "loopy", False),
    ("loopy  · 挂 Hook（修复）", "loopy", True),
    ("starved· 不挂 Hook（复现翻车 c）", "starved", False),
    ("starved· 挂 Hook（修复）", "starved", True),
    ("halfdead· 不挂 Hook（复现翻车 e：一位专家失败）", "halfdead", False),
    ("halfdead· 挂 Hook（修复：终态即就绪）", "halfdead", True),
    ("refuse · 不挂 Hook（复现翻车 d）", "refuse", False),
    ("refuse · 挂 Hook（第 5 层兜底）", "refuse", True),
    ("good   · 挂 Hook（回归：不破坏正常流程）", "good", True),
]


def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


async def main() -> int:
    import asyncio

    _section("Leader 终止不确定性 —— 离线验证")
    print("说明：假模型只复现「不听话」这一件事；判据是 output/ 里有没有 verdict JSON。")

    results: list[tuple[str, bool, dict[str, Any]]] = []

    for label, behaviour, with_guard in CASES:
        _section(label)
        outcome = await run_once(behaviour=behaviour, with_guard=with_guard)
        produced = bool(outcome["verdict_files"])
        print(f"  status          = {outcome['status']}   迭代 {outcome['iterations']} 轮")
        print(f"  模型调用/被强制  = {outcome['model_calls']} / {outcome['forced_calls']}")
        print(f"  专家已回结论     = {outcome['partner_results']} 位")
        print(f"  verdict 已提交   = {outcome['verdict_submitted']}")
        print(f"  output 文件      = {outcome['verdict_files'] or '（无！）'}")
        if outcome["fallback_path"]:
            print(f"  第 5 层兜底      = {Path(outcome['fallback_path']).name}")
        print(f"  final_text       = {(outcome['final_text'] or '（空）')[:60]}")
        if outcome["guard_trace"]:
            print("  强制决策轨迹：")
            for entry in outcome["guard_trace"]:
                print(
                    "    · reason={reason:<22} 强制工具={force_tool} 收回工具={clear_tools} "
                    "专家已回={partner_results}".format(**entry)
                )
        results.append((label, produced, outcome))

    _section("结论")
    failed: list[str] = []
    for label, produced, outcome in results:
        expect_produced = "不挂 Hook" not in label
        ok = produced == expect_produced
        notes = [f"期望产出 verdict={expect_produced} / 实际={produced}"]
        if label.startswith("good"):
            healthy = outcome["status"] == "completed" and bool(outcome["final_text"])
            ok = ok and healthy
            notes.append(f"正常收尾 completed+非空文本={healthy}")
        if label.startswith("refuse") and "不挂" not in label:
            via_fallback = outcome["fallback_path"] is not None
            ok = ok and via_fallback
            notes.append(f"由第 5 层兜底补齐={via_fallback}")
        mark = "通过" if ok else "未达预期"
        if not ok:
            failed.append(label)
        print(f"  [{mark}] {label}")
        for note in notes:
            print(f"          {note}")

    print()
    if failed:
        print(f"有 {len(failed)} 个用例未达预期，请检查上面的轨迹：")
        for label in failed:
            print(f"  · {label}")
        return 1

    print("全部用例符合预期：")
    print("  · 不挂 Hook 时，五种失范都会让 verdict JSON 缺失（= 演示当场翻车）")
    print("  · 挂上 LeaderTerminationHooks 后，任何形态都必出 verdict JSON")
    print("  · 前四层靠「收敛可用工具 + 强制指令」，第 5 层在模型拒不交付时程序化补齐")
    print("  · 正常流程不受影响（good 组仍然是 completed + 非空 final_text）")
    return 0


if __name__ == "__main__":
    import asyncio

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(asyncio.run(main()))
