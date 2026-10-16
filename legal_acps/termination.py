"""把 Leader「出结论」这个动作，从"靠模型自觉"改成"系统结构上保证"。

════════════════════════════════════════════════════════════════════════
这个文件解决什么问题
════════════════════════════════════════════════════════════════════════

端到端复跑时实测到 Leader 的**终止行为不确定**，同一个输入会得到两种结果：

  (a) 两位专家都返回了，模型却直接输出一段自然语言就收尾
      → `status=completed`，但 `output/` 下**没有** verdict JSON。
  (b) 模型一直反复调工具，把 8 轮迭代预算烧完
      → `status=budget_exhausted`，最终文本为空，同样没有 verdict JSON。

两次运行的工具轨迹几乎相同（discover×3 / delegate×2 / task×5），
**唯一差别是最后一轮有没有调 `legal_issue_verdict`**。

根因：要不要出结论，完全由模型自己决定。演示时这就是"当场翻车"。

════════════════════════════════════════════════════════════════════════
怎么修的：五层，前四层把结论"逼出来"，第五层保证"一定有"
════════════════════════════════════════════════════════════════════════

**第 1 层（主）—— 结构性强制：把本轮可用工具收敛到只剩结论工具。**

  在 `wrap_model_call` 里把 `request.tools` 过滤成只剩 `legal_issue_verdict`。
  模型手里没有 `acps_delegate` / `acps_task` 可调，**不可能**再委派或轮询，
  只能在"交结论"和"输出一段文本"之间选 —— 这是结构性的，不是提示词层面的。
  （会先校验结论工具确实在本轮 tools 列表里，不在就跳过，免得指定了不存在的工具。）

  ⚠️ **这里刻意不用 `tool_choice`，原因见下面那条实测教训。**

**第 2 层 —— 同时往 system 提示里插一段"强制指令"。**

  被逼着调工具是一回事，这一步该写什么内容是另一回事。
  `context()` 钩子每个推理步都会被调用，且**先于**本步的模型调用
  （执行顺序：on_step_start → context() → 拼 system 提示 → wrap_model_call），
  所以我们可以在这个时机把"必须现在出结论、信息不全要如实说明"塞进 system 提示，
  让被强制的这次调用有内容可填。

**第 3 层 —— 迭代预算兜底。**

  如果专家还没返回、但步数只剩最后 1~2 步，也强制出结论（同样走第 1 层的工具收敛）。
  这时结论质量会下降，所以提示里明确要求"未拿到的部分如实写信息不完整，
  不要编造"。有结论 + 标注降级，远好过没有结论。

**第 4 层 —— 结论交付后强制收尾为纯文本。**

  结论一旦提交，后续再调用工具就毫无意义，而模型"停不下来"会把预算烧成
  `budget_exhausted`。所以在结论提交后的那次模型调用里，我们**清空
  `request.tools`** —— 模型手里没有工具可调，只能输出文本。
  于是 run 以 `status=completed` + 非空 `final_text` 正常收尾。

  （同样刻意**不用** `tool_choice="none"`：见下面那条实测教训。）

**第 5 层 —— 兜底：程序自己补齐一份结论（仅 Leader）。**

  万一前四层都失效（模型宁可输出一段文本也不交结论），
  在 `wrap_run` 里从两位专家**已经交回的结构化载荷**中直接组装一份
  `FinalVerdict` 落盘，并在 `consistency_note` 里明确标注"程序兜底生成"。
  质量低于模型亲笔，但保证 `output/` 一定拿到文件 —— 演示不会空手。

  ⚠️ 这一层**只对 Leader 生效**（`conclusion_tool == legal_issue_verdict`）。
  它拼的是 `FinalVerdict` 形状；网络裁判复用本 Hook 时交付物是裁定书 FactRuling，
  若也走这里就会往 `output/` 写一个"像结论的东西"，污染唯一的演示判据。

════════════════════════════════════════════════════════════════════════
⚠️ 实测教训：tool_choice 在推理模型上会直接 400（2026-09-29）
════════════════════════════════════════════════════════════════════════

wit 的模型 Provider（`providers/openai.py`）在拼 Chat Completions 参数时，
只把这 5 个键当作"保留键"自己填：
    {"model", "messages", "tools", "stream", "stream_options"}
`ModelRequest.settings` 里**其余任何键都会原样透传给 OpenAI SDK** ——
这一点是实测确认的。但 **"透传成功" ≠ "对方接受"**：

本项目生产模型是 `deepseek-v4-pro`（推理模型），它的 thinking 模式直接拒绝：

    HTTP 400 {"message": "Thinking mode does not support this tool_choice"}

后果比原来的 bug 更严重：整轮 run **抛异常崩掉**，连 `completed` 都拿不到。
（这是端到端真模型跑出来的，离线假模型测不出来 —— 假模型不会拒绝。）

所以强制手段改为"**收敛工具列表**"：效果等价（模型无从选择），
但它是纯本地行为，不经网关、不依赖 provider 特性，对任何 OpenAI 兼容服务零兼容风险。

若将来换成明确支持 `tool_choice` 的模型，可传 `force_tool_choice=True` 叠加这一层；
Hook 在遇到 provider 拒绝时会自动摘掉并降级，不会再崩。

════════════════════════════════════════════════════════════════════════
判定"专家都返回了"是怎么做的：不猜，直接读消息
════════════════════════════════════════════════════════════════════════

不引入任何额外状态，全部从 `AgentState.messages` 推导 —— 这样它是**可重入**的
（嵌套 run、重放、离线测试都得到同一个判断）。

每个工具调用的返回消息，wit 都写成一条 role=tool、name=<工具名> 的消息，
其内容是 `json.dumps(observation)`，observation 的键为：
    toolCallId / tool / capabilityId / modelToolName / status / result / error / durationMs

`acps_task` 的 `result` 就是 Partner 任务视图，含：
    task_ref / state / products / status_history ...

Partner 任务的 state 取值（读自 `wit.acps.aip`）：
    rejected / accepted / working / awaiting-input /
    awaiting-completion / completed / canceled / failed

其中 `awaiting-completion` 和 `completed` 表示**Partner 已经交回结构化结论**
（前者是等 Leader 收尾，后者已关闭）；`rejected / canceled / failed` 表示
**明确失败、不会再回来了**。所以：

    已交回结论的专家 = {task_ref : 最新 state ∈ {awaiting-completion, completed}}
    明确失败的委派   = {task_ref : 最新 state ∈ {rejected, canceled, failed}}

判定"能不能出结论"看的是两者之和（`LeaderProgress.resolved`），
**不是只看成功数** —— 否则只要有一位专家挂掉，计数就永远到不了预期值，
第 1 层永不触发，Leader 只能干等到预算见底。

注意按 `task_ref` **去重**：Leader 会多次 `acps_task(wait/refresh/complete)`
打同一个任务，不去重就会把一次委派数成好几次。
"""

from __future__ import annotations

import contextlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from wit import (
    ContextBlock,
    HookContext,
    ModelRequest,
    ModelResponse,
    StepStartEvent,
    WitHooks,
)

# ---------------------------------------------------------------------------
# 常量：工具名与状态集合
# ---------------------------------------------------------------------------

# 模型看到的工具名 = 能力 id 把点换成下划线（见 wit.kernel.capabilities）
VERDICT_TOOL = "legal_issue_verdict"
DELEGATE_TOOL = "acps_delegate"
TASK_TOOL = "acps_task"

# Partner 已交回结构化结论的两种状态
PARTNER_DONE_STATES = frozenset({"awaiting-completion", "completed"})

# 明确失败、不必再等的状态（用于统计"还有什么在跑"）
TASK_DEAD_STATES = frozenset({"rejected", "canceled", "failed"})


# ---------------------------------------------------------------------------
# 从消息里读出"现在到哪一步了"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaderProgress:
    """一次推理步开始前，Leader 的进度快照。"""

    delegations: int                 # 成功发起的 acps_delegate 次数
    partner_results: int             # 已交回结论的专家个数（按 task_ref 去重）
    pending_tasks: int               # 还在跑的委派任务数
    verdict_submitted: bool          # 最终结论是否已成功提交
    verdict_errors: int              # 最终结论提交失败的次数（参数不合 schema 等）
    dead_tasks: int = 0              # 明确失败/取消的委派任务数（rejected/canceled/failed）

    @property
    def resolved(self) -> int:
        """已到达**终态**的下游数：有结论的 + 明确失败的。

        判定"能不能出结论"要看这个，而不是只看 `partner_results` ——
        否则只要有一位专家挂掉，`partner_results` 就永远到不了预期值，
        `partners-ready` 永不触发，Leader 只能干等到预算见底。
        """
        return self.partner_results + self.dead_tasks

    @property
    def summary(self) -> str:
        return (
            f"委派 {self.delegations} 次 / 已回结论 {self.partner_results} 位 / "
            f"失败 {self.dead_tasks} 个 / 在跑 {self.pending_tasks} 个 / "
            f"结论已提交={self.verdict_submitted}"
        )


def _tool_name_of(tool: Any) -> str:
    """兼容 Tool 对象与 OpenAI 风格 dict 两种形态。"""
    name = getattr(tool, "name", None)
    if isinstance(name, str) and name:
        return name
    if isinstance(tool, Mapping):
        raw = tool.get("name")
        if isinstance(raw, str) and raw:
            return raw
        fn = tool.get("function")
        if isinstance(fn, Mapping) and isinstance(fn.get("name"), str):
            return fn["name"]
    return ""


def _decode_observation(message: Any) -> Mapping[str, Any] | None:
    """把一条 role=tool 的消息还原成 observation 字典。"""
    text_method = getattr(message, "text", None)
    if not callable(text_method):
        return None
    try:
        text = text_method() or ""
    except Exception:  # noqa: BLE001 - 解析失败不该影响主流程
        return None
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    # observation 一定带 status；没有它说明这不是工具观测
    if "status" not in payload:
        return None
    return payload


def survey(
    messages: Iterable[Any], *, verdict_tool: str = VERDICT_TOOL
) -> LeaderProgress:
    """扫描消息历史，算出 Leader 当前进度。纯函数，无副作用。

    `verdict_tool` 是**本轮 run 的交付物工具名**，默认 Leader 的
    `legal_issue_verdict`。网络裁判复用本模块时交付物是 `fact_issue_ruling`。

    ⚠️ 这个参数不是装饰性的：早期版本把它写死成 VERDICT_TOOL，结果是
    debate-pro 即使已成功提交交付物，`verdict_submitted` 依然恒为 False ——
    第 4 层（收尾）、retry 判定、`wrap_run` 的"已交付就跳过兜底"全部失效，
    而 `check_local` 只校验了 Hook 构造参数、没校验这个联动，所以没拦住。
    """
    delegations = 0
    verdict_submitted = False
    verdict_errors = 0
    # task_ref -> 该任务的最新 state
    task_states: dict[str, str] = {}
    # 没有 task_ref 可用时的兜底计数（正常不会走到）
    anonymous_done = 0

    for message in messages:
        if getattr(message, "role", None) != "tool":
            continue
        obs = _decode_observation(message)
        if obs is None:
            continue

        tool_name = obs.get("modelToolName") or getattr(message, "name", None) or ""
        status = obs.get("status")

        if status != "success":
            if tool_name == verdict_tool:
                verdict_errors += 1
            continue

        if tool_name == verdict_tool:
            verdict_submitted = True
            continue

        result = obs.get("result")

        if tool_name == DELEGATE_TOOL:
            delegations += 1
            if isinstance(result, Mapping):
                ref = result.get("task_ref") or result.get("taskRef")
                state = result.get("state")
                if isinstance(ref, str) and isinstance(state, str):
                    task_states[ref] = state
            continue

        if tool_name == TASK_TOOL and isinstance(result, Mapping):
            ref = result.get("task_ref") or result.get("taskRef")
            state = result.get("state")
            if isinstance(ref, str) and isinstance(state, str):
                task_states[ref] = state
                continue
            products = result.get("products")
            if isinstance(products, list) and products:
                anonymous_done += 1

    done = anonymous_done + sum(
        1 for s in task_states.values() if s in PARTNER_DONE_STATES
    )
    dead = sum(1 for s in task_states.values() if s in TASK_DEAD_STATES)
    pending = sum(
        1
        for s in task_states.values()
        if s not in PARTNER_DONE_STATES and s not in TASK_DEAD_STATES
    )
    return LeaderProgress(
        delegations=delegations,
        partner_results=done,
        pending_tasks=pending,
        verdict_submitted=verdict_submitted,
        verdict_errors=verdict_errors,
        dead_tasks=dead,
    )


# ---------------------------------------------------------------------------
# 第 5 层（兜底）：从专家载荷直接拼一份结论
#
# 只要 Provider 里能捞到"含 is_incitement / overall_truth_score / harm_index
# 且带 dimensions"的字典，就能在模型拒不交结论时程序化补齐，保证 output/ 有文件。
# ---------------------------------------------------------------------------


def _iter_result_dicts(value: Any) -> Iterable[Mapping[str, Any]]:
    """深度遍历任意观测结果，产出其中所有 dict（含嵌套 products 列表）。

    不假设 Partner 把交付物放在第几层 —— 实测里它可能被包在
    `result.products[i].artifact` 之类的结构里，逐层找比写死路径稳。
    """
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _iter_result_dicts(nested)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_result_dicts(item)


@dataclass(frozen=True)
class PartnerPayloads:
    """从消息里捞回来的专家交付物原始 JSON（找不到就是 None）。"""

    incitement: Mapping[str, Any] | None   # 含 is_incitement 的那份
    truth: Mapping[str, Any] | None        # 含 overall_truth_score 的那份
    harm: Mapping[str, Any] | None         # 含 harm_index + dimensions 的那份

    @property
    def empty(self) -> bool:
        return self.incitement is None and self.truth is None and self.harm is None


def collect_partner_payloads(messages: Iterable[Any]) -> PartnerPayloads:
    """扫描工具观测，找出两位专家交回的结构化结论。纯函数，无副作用。"""
    incitement: Mapping[str, Any] | None = None
    truth: Mapping[str, Any] | None = None
    harm: Mapping[str, Any] | None = None

    for message in messages:
        if getattr(message, "role", None) != "tool":
            continue
        obs = _decode_observation(message)
        if obs is None or obs.get("status") != "success":
            continue
        tool_name = obs.get("modelToolName") or getattr(message, "name", None) or ""
        # 只看"取回专家结果"的观测，不看 Leader 自己的结论提交
        if tool_name not in (DELEGATE_TOOL, TASK_TOOL):
            continue
        for candidate in _iter_result_dicts(obs.get("result")):
            if incitement is None and "is_incitement" in candidate:
                incitement = candidate
            if truth is None and "overall_truth_score" in candidate:
                truth = candidate
            if harm is None and "harm_index" in candidate and "dimensions" in candidate:
                harm = candidate

    return PartnerPayloads(incitement=incitement, truth=truth, harm=harm)


def _extract_subject(messages: Iterable[Any]) -> str:
    """取第一条 user 消息作为"被研判的原文"（找不到就返回占位符）。"""
    for message in messages:
        if getattr(message, "role", None) != "user":
            continue
        text_method = getattr(message, "text", None)
        if callable(text_method):
            with contextlib.suppress(Exception):
                text = text_method() or ""
                if text.strip():
                    return text.strip()
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    return "（未能从消息历史中还原被研判原文）"


#: 公开别名 —— 先例注入（`precedent.py`）与终止兜底共用**同一份**"取原文"口径。
extract_subject = _extract_subject


def build_fallback_verdict(
    *, subject: str, payloads: PartnerPayloads, reason: str
) -> dict[str, Any]:
    """用下游载荷拼一份合规的 `FinalVerdict`（第 5 层兜底）。

    刻意**不改 contracts 的 `FinalVerdict` 字段** —— 那个契约同时是 Leader 对外
    公布的 ACS 技能 schema。改它的字段**不会**动 ACS 指纹（2026-10-02 实测：
    ACS 的 `skills[]` 里**没有 input schema**），但会改变**对外的工具 schema**、
    下游调用方要跟着改。所以"这是兜底生成"这个信息仍然放在 `consistency_note`
    里，不新增字段。

    ⚠️ 措辞不能写死"两位专家"：2026-10-02 软废弃 `incitement` / `harm` 之后，
    主控的标准流程**不再委派任何人**，它根本没有"两位专家"可等。这里一律说
    "已交回的下游载荷" —— 对主控（可能一位都没派）与网络裁判（等的是正反双方）
    都成立。
    """
    from .contracts import band_of  # 延迟导入：避免与本模块形成循环依赖

    harm_index = 0
    if payloads.harm is not None:
        with contextlib.suppress(TypeError, ValueError):
            harm_index = int(payloads.harm.get("harm_index"))  # type: ignore[arg-type]
    harm_index = max(-100, min(100, harm_index))

    is_incitement = bool(
        payloads.incitement.get("is_incitement") if payloads.incitement else False
    )

    truth_score = 50
    if payloads.truth is not None:
        with contextlib.suppress(TypeError, ValueError):
            truth_score = int(payloads.truth.get("overall_truth_score"))  # type: ignore[arg-type]
    truth_score = max(0, min(100, truth_score))

    missing: list[str] = []
    if payloads.harm is None:
        missing.append("社会危害性评估")
    if payloads.incitement is None:
        missing.append("煽动性识别")
    if payloads.truth is None:
        missing.append("事实核查")

    note = (
        f"⚠️ 本结论由**程序兜底生成**，非模型亲笔撰写（触发原因：{reason}）。"
        "模型在依赖返回后未能提交结构化结论，系统只能按**已交回的下游载荷**"
        "直接汇总，因此缺少模型对冲突点的人工取舍说明。"
    )
    if missing:
        note += "未从下游取到的载荷：" + "、".join(missing) + "。"

    draft: dict[str, Any] = {
        "subject": subject,
        "harm_index": harm_index,
        "band": band_of(harm_index).value,
        "is_incitement": is_incitement,
        "truth_score": truth_score,
        "consistency_note": note,
        "conclusion": (
            f"该言论的社会危害性指数为 {harm_index}（{band_of(harm_index).value}），"
            f"{'构成' if is_incitement else '不构成'}煽动性言论，"
            f"整体为真程度约 {truth_score}/100。"
            "（此段为程序按专家载荷汇总，供链路自检与演示兜底使用。）"
        ),
        "action_advice": "建议人工复核：本条结论由系统兜底生成，未经过模型的统筹取舍。",
    }

    # 过一遍真契约再落盘：这样兜底文件与模型亲笔文件**字段完全一致**
    # （含 disclaimer 与 band 的自动推导）。手搓 dict 会漏字段，
    # 下游按 FinalVerdict 读就会 KeyError。校验失败则退回 draft。
    from .contracts import FinalVerdict

    with contextlib.suppress(Exception):
        draft = FinalVerdict.model_validate(draft).model_dump(mode="json")
    return draft


def write_fallback_verdict(
    *, messages: Iterable[Any], reason: str, output_dir: Path | None = None
) -> str | None:
    """把兜底结论写成 `output/verdict-*.json`，返回路径；无可汇总内容时返回 None。

    ⚠️ **软废弃（2026-10-02）之后，这一层对主控基本不再生效**：它只认
    `acps_delegate` / `acps_task` 观测里那三种形状的载荷（`is_incitement` /
    `overall_truth_score` / `harm_index`+`dimensions`），而主控改成自己研判后
    **不再委派任何人**（除非走事实核查分支）⇒ `payloads.empty` 恒为真 ⇒ 返回 None。

    这是**刻意的**，不是漏洞：没有载荷时唯一能拼出来的是一份
    `harm_index=0 / is_incitement=False / truth_score=50` 的空壳，那看起来像一份
    "中性"的结论，会被当成"链路成立"的证据 —— 正是本项目最忌讳的
    **静默给出安全结论**，会污染 `output/` 这个唯一判据。
    宁可**不产文件**（= 可检测的明确失败），也不产一份没有依据的空壳。
    """
    payloads = collect_partner_payloads(messages)
    if payloads.empty:
        return None

    from . import agents as agents_mod  # 延迟导入：agents 依赖本模块

    target = Path(output_dir) if output_dir is not None else agents_mod.OUTPUT_DIR
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = target / f"verdict-{stamp}.json"
    payload = build_fallback_verdict(
        subject=_extract_subject(messages), payloads=payloads, reason=reason
    )
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(path)


# ---------------------------------------------------------------------------
# 强制指令文案（第 2 / 3 层注入 system 提示的内容）
#
# 全部用模板函数生成，因为同一个 Hook 也被**网络裁判**复用
# （它的"结论工具"是 fact_issue_ruling，等的是正反双方而不是两位专家）。
# ---------------------------------------------------------------------------


def _force_partners_ready(
    *, tool: str, peers: str, dead: int = 0, note_field: str = "consistency_note"
) -> str:
    """下游全部到终态时的强制文案。

    `note_field` 是"把失败情况写进哪个字段"。它必须是**当前角色契约里真实存在**的字段：
    主控的 `FinalVerdict` 有 `consistency_note`，但网络裁判的 `FactRuling` 没有
    （那里叫 `reasoning`）。早期这里写死 `consistency_note`，裁判分支一旦被触发，
    系统就是在指示模型往一个不存在的字段里写东西 —— 模型要么困惑、要么写出脏字段。
    """
    if dead:
        head = (
            f"{peers}里已经有 {dead} 个**明确失败或取消**、没能交回结论，"
            "其余下游都已到达终态。"
        )
        extra = (
            f"\n- 失败的那部分请在 `{note_field}` 与相应字段中**如实写明"
            "「该下游未能返回结论」，不要编造、也不要假装信息完整**；"
        )
    else:
        head = f"{peers}的结论已经收齐。"
        extra = ""
    return f"""═══ 系统强制指令（不可跳过）═══
{head}
按流程，你现在**必须立刻调用 {tool}** 提交最终交付物。

- 系统已把本步的可调用工具**锁定为 {tool}**，你无法调用其他工具；
- 不要再用自然语言做收尾，也不要再发起任何新的委派；{extra}
- 该写的字段一个都不能少，缺信息就如实写明「未获取到」，不要编造。"""


def _force_budget_guard(*, tool: str, peers: str) -> str:
    """预算将尽时的强制文案。

    `peers` 为空串表示**本角色没有下游**（例如辩手这类叶子 Agent）。
    这时不能套用"下游的结论还没拿到"的措辞 —— 那会让模型去找一个根本不存在的东西。
    """
    missing = f"（{peers}的结论等）" if peers else "（尚未获取到的信息）"
    return f"""═══ 系统强制指令（迭代预算即将耗尽）═══
本轮推理步数即将用完，但最终交付物还没有提交。
无论目前掌握的信息是否完整，你现在**必须立刻调用 {tool}** 提交。

- 系统已把本步的可调用工具**锁定为 {tool}**；
- 尚未拿到的部分{missing}请在对应字段中如实写明
  「信息不完整 / 未能获取」，**不要编造，也不要给一个看似完整其实没有依据的内容**；
- 宁可交出一份标注了不确定性的结果，也不要什么都不交。"""


def _force_retry(*, tool: str) -> str:
    return f"""═══ 系统强制指令（上一次提交未通过）═══
你上一次调用 {tool} 没有成功（参数不符合契约）。
请**严格按工具的参数 schema 重新提交一次**：所有必填字段都要给，
数值范围要合法。不要再调用其他工具。"""


def _finish_after_conclusion(*, tool: str) -> str:
    return f"""═══ 收尾指令 ═══
最终交付物已经通过 {tool} 成功提交并写入文件，链路已闭环。
现在请**用一段简明中文向用户复述结论**，然后结束。
系统已收回本步的工具权限，你不需要也无法再调用任何工具。"""


@dataclass(frozen=True)
class _Decision:
    """一次强制决策。"""

    reason: str                  # partners-ready / budget-guard / retry / finish-after-verdict
    force_tool: str | None       # 要锁定的工具名；None 表示不锁工具
    clear_tools: bool            # True 表示清空本步工具列表（强制纯文本收尾）
    nudge: str | None            # 要注入 system 提示的文案


# ---------------------------------------------------------------------------
# Hook 本体
# ---------------------------------------------------------------------------


class LeaderTerminationHooks(WitHooks):
    """强制"有下游依赖的 Agent"在依赖返回后必须出交付物。

    默认配置就是 Leader：两位专家（煽动性识别 + 危害评估）返回后，
    强制调用 `legal_issue_verdict`。

    这个 Hook 同时也被**网络裁判**复用：它的"下游"是正反双方，
    "交付物"是 `fact_issue_ruling`。所以工具名与下游称呼都是参数。

    参数
    ----
    conclusion_tool
        最终交付物对应的**模型工具名**（能力 id 把点换成下划线）。
        默认 `legal_issue_verdict`；网络裁判传 `fact_issue_ruling`。
    partner_label
        提示词里对下游的称呼，纯文案。默认「两位专家」。
    expected_partners
        预期几位下游会交回结构化结论。默认 2（两位专家 / 正反双方）。
    reserve_steps
        距离迭代上限还剩几步时启动兜底强制。默认 1，
        即"最后两次模型调用"都会被强制出结论。

        ⚠️ **语义容易理解反：值越大 = 触发越早 = 越激进。**
        对于"流程长、且中途要等下游"的角色（如网络裁判，完整流程要 7 步），
        调大它会把"等待下游结果"的那一步直接劫持成强制出结论 ——
        表现为"反方的论点还没拿到就出裁定了"。这种角色应当传 0，
        只在真正的最后一步兜底。
    max_verdict_errors
        交付物提交连续失败多少次后放弃强制，改为让模型用自然语言交代失败原因。
        默认 2 —— 只是为了不让模型卡在一张填不对的表单上把预算烧光。
    note_field
        "下游失败时把情况写进哪个字段"的字段名，用于生成强制文案。
        默认 `consistency_note`（主控的 FinalVerdict 有它）。
        网络裁判的 FactRuling **没有**这个字段，所以传 `reasoning` ——
        写死会让系统指示模型往一个不存在的字段里写东西。
    finish_after_verdict
        交付物提交后是否清空工具、强制模型收尾成纯文本。默认 True。
    force_tool_choice
        **默认 False**。是否额外叠加 `settings["tool_choice"]` 锁定工具。
        默认关闭的原因：推理模型的 thinking 模式会直接拒绝
        （DeepSeek 实测 HTTP 400 "Thinking mode does not support this tool_choice"），
        一旦触发整轮 run 会崩。第 1 层的"收敛工具列表"已经能达到等价效果。
        若确认所用模型支持，可开启本项加强；provider 若拒绝会自动降级，不会再崩。
    fallback_on_failure
        前四层都没逼出交付物时，是否用专家载荷程序化补齐一份结论。默认 True。
    verbose
        把每次强制决策打到 stderr。
    """

    priority = 900  # 越大越靠内层；先让委派 Hook 干活，我们只做收尾
    # 用类属性而不是 property：wit 其它 Hook 都是类属性，
    # 框架里存在按类读取 hook.id 的写法。
    id = "legal.termination-guard"

    def __init__(
        self,
        *,
        conclusion_tool: str = VERDICT_TOOL,
        partner_label: str = "两位专家",
        expected_partners: int = 2,
        reserve_steps: int = 1,
        max_verdict_errors: int = 2,
        finish_after_verdict: bool = True,
        force_tool_choice: bool = False,
        fallback_on_failure: bool = True,
        note_field: str = "consistency_note",
        verbose: bool = False,
    ) -> None:
        self.conclusion_tool = str(conclusion_tool)
        self.partner_label = str(partner_label)
        self.expected_partners = max(1, int(expected_partners))
        self.reserve_steps = max(0, int(reserve_steps))
        self.max_verdict_errors = max(0, int(max_verdict_errors))
        self.finish_after_verdict = bool(finish_after_verdict)
        self.force_tool_choice = bool(force_tool_choice)
        self.fallback_on_failure = bool(fallback_on_failure)
        self.note_field = str(note_field or "reasoning")
        self.verbose = verbose
        # 供离线验证脚本 / 排障使用：记录每一次强制决策
        self.trace: list[dict[str, Any]] = []
        # 第 5 层兜底写出的文件路径（没有触发则为 None），便于外部断言
        self.fallback_path: str | None = None

    # -- 日志与可观测性 ----------------------------------------------------

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[termination] {message}", file=sys.stderr, flush=True)

    def record(self, decision: _Decision, progress: LeaderProgress) -> None:
        if len(self.trace) >= 64:
            return
        self.trace.append(
            {
                "reason": decision.reason,
                "force_tool": decision.force_tool,
                "clear_tools": decision.clear_tools,
                "delegations": progress.delegations,
                "partner_results": progress.partner_results,
                "dead_tasks": progress.dead_tasks,
                "pending_tasks": progress.pending_tasks,
                "verdict_submitted": progress.verdict_submitted,
                "verdict_errors": progress.verdict_errors,
            }
        )

    # -- 判定 --------------------------------------------------------------

    def _steps_left(self, ctx: HookContext) -> int | None:
        """距迭代上限还剩几步。拿不到步号时返回 None（那就只靠 partners-ready）。"""
        max_steps = getattr(getattr(ctx, "config", None), "max_steps", None)
        if not isinstance(max_steps, int) or max_steps <= 0:
            return None
        state = getattr(ctx, "private_state", None)
        step = state.get("step") if isinstance(state, dict) else None
        if not isinstance(step, int) or step < 0:
            return None
        return max(0, max_steps - (step + 1))

    def _peers_label(self, progress: LeaderProgress) -> str:
        """下游的称呼 —— 实际派出数量与预期不一致时必须改口。

        为什么不能直接用 `partner_label`：主控的「事实核查」附加分支会额外派
        一位（网络裁判），此时固定文案"两位专家的结论已经收齐"就成了一句**假话**,
        模型要么以为自己漏等了一方、要么以为信息已经全齐 —— 两种都会让汇总跑偏。
        数量对不上时如实换成"本轮派出的 N 位下游"。
        """
        if progress.delegations > self.expected_partners:
            return f"本轮派出的 {progress.delegations} 位下游"
        return self.partner_label

    def _decide(self, ctx: HookContext) -> tuple[_Decision, LeaderProgress] | None:
        """返回 (决策, 进度快照)；不需要干预时返回 None。"""
        messages = getattr(getattr(ctx, "state", None), "messages", None)
        if not messages:
            return None

        progress = survey(messages, verdict_tool=self.conclusion_tool)

        # 第 4 层：交付物已提交 —— 收回工具，逼它用文本收尾
        if progress.verdict_submitted:
            if not self.finish_after_verdict:
                return None
            return (
                _Decision(
                    reason="finish-after-verdict",
                    force_tool=None,
                    clear_tools=True,
                    nudge=_finish_after_conclusion(tool=self.conclusion_tool),
                ),
                progress,
            )

        # 第 1 层：下游都到达终态（有结论 或 明确失败）→ 立刻逼出交付物
        #
        # ⚠️ 这里必须用 `resolved`（= 已回结论 + 明确失败），不能只看
        #    `partner_results`。2026-09-29 真机日志里出现过"煽动性识别 Partner
        #    运行中失败、未产出任何产品"的情况：那种情况下只数成功数的话，
        #    计数永远到不了预期值，本层永不触发，
        #    Leader 只能干等到预算见底、由第 3 层兜底。
        #
        # ⚠️ 判据必须取 `max(expected_partners, delegations)`，**不能只用那个常量**。
        #    2026-09-30 真机逮到（离线复现一致）：主控的「事实核查」附加分支
        #    一轮里派出了 **3** 位（煽动识别 / 危害评估 / 网络裁判），
        #    而 `expected_partners` 只写了 2 —— 前两位一交回本层就触发，
        #    工具被锁定为交付物，**主控再也调不了 acps_task 去等第三位**；
        #    网络裁判的裁定书晚 9 秒才落盘，没能进入主控结论。
        #    `delegations` 是"本轮真正派出去了几个"，以它为准才是
        #    「我的依赖都回来了吗」的正确语义；`expected_partners` 退化为**下限**
        #    （防模型偷懒少派时提前收尾）。
        #    反面对照：若把本层写成"只等 delegations"，模型一次都没派时
        #    0 >= 0 会立刻触发 —— 那正是 `max(...)` 里那个下限要挡的。
        #
        # ⚠️ 这一层还必须排在"放弃强制"之前。早期版本把 `verdict_errors >= max`
        #    的放弃判定放在它前面，于是模型只要连续两次把表单填错，本模块就会
        #    彻底停止强制 —— 哪怕两位专家都已交回结论。那正好违背本模块的唯一承诺
        #    （"依赖回来 ⇒ 必须出交付物"），结果是只拿到第 5 层的程序兜底结论。
        target = max(self.expected_partners, progress.delegations)
        if progress.resolved >= target:
            return (
                _Decision(
                    "partners-ready",
                    self.conclusion_tool,
                    False,
                    _force_partners_ready(
                        tool=self.conclusion_tool,
                        peers=self._peers_label(progress),
                        dead=progress.dead_tasks,
                        note_field=self.note_field,
                    ),
                ),
                progress,
            )

        # 第 3 层：预算快没了 → 兜底逼出交付物（允许标注信息不完整）
        #
        # ⚠️ 本层必须排在下面那条"放弃强制"**之前**（2026-09-29 审计修正）。
        #    早期版本把"放弃强制"放在本层之前，于是模型连续两次把表单填错后
        #    直接 `return None`，**顺手把本层也一起跳过了**。两者的理由完全不同：
        #      · 放弃强制 = "表单填不对，别再逼它填了"
        #      · budget-guard = "步数要没了，宁可交一份标注不完整的也不要不交"
        #    让前者顺手取消后者，等于在最后一步主动放弃唯一的兜底机会 ——
        #    而"output/ 一定有文件"正是本模块对外的唯一承诺。
        steps_left = self._steps_left(ctx)
        if steps_left is not None and steps_left <= self.reserve_steps:
            return (
                _Decision(
                    "budget-guard",
                    self.conclusion_tool,
                    False,
                    _force_budget_guard(
                        tool=self.conclusion_tool, peers=self.partner_label
                    ),
                ),
                progress,
            )

        # 交付物表单反复填不对、且已无在跑的依赖 —— 不再重试提交（但仍受上面的预算兜底保护）。
        # 之所以要求 `pending_tasks == 0`：还有专家没回来时不能放手，
        # 否则模型会拿半份信息去交结论，或者干脆卡在预算上。
        if (
            progress.verdict_errors >= self.max_verdict_errors
            and progress.pending_tasks == 0
        ):
            return None

        # 提交失败过一次但还没到上限：再给一次机会。
        # 但**必须等依赖都落地**（`pending_tasks == 0`）—— 强制重试会把可用工具
        # 收敛到只剩交付物工具，此时若还有专家在跑，Leader 就再也调不了
        # `acps_task` 去取结果了，只能拿半份信息交结论。
        if progress.verdict_errors > 0 and progress.pending_tasks == 0:
            return (
                _Decision(
                    "retry",
                    self.conclusion_tool,
                    False,
                    _force_retry(tool=self.conclusion_tool),
                ),
                progress,
            )

        return None

    # -- 第 2 / 3 / 4 层：往 system 提示里插指令 ---------------------------

    def context(self, ctx: HookContext) -> list[ContextBlock]:
        try:
            decided = self._decide(ctx)
        except Exception as exc:  # noqa: BLE001 - 钩子出错不能拖垮整轮 run
            self._log(f"context() 判定异常，已跳过干预：{exc!r}")
            return []
        if decided is None:
            return []
        decision, progress = decided
        if not decision.nudge:
            return []
        self._log(f"注入提示 reason={decision.reason} | {progress.summary}")
        return [
            ContextBlock.text(
                title="终止约束（系统强制）",
                value=decision.nudge,
                metadata={"source": self.id, "reason": decision.reason},
            )
        ]

    # -- 第 1 / 3 / 4 层：真正动手改这一轮的请求 ---------------------------

    async def wrap_model_call(
        self,
        ctx: HookContext,
        request: ModelRequest,
        handler,
    ) -> ModelResponse:
        try:
            decided = self._decide(ctx)
        except Exception as exc:  # noqa: BLE001
            self._log(f"wrap_model_call() 判定异常，已放行：{exc!r}")
            return await handler(request)

        if decided is None:
            return await handler(request)

        decision, progress = decided

        if decision.clear_tools:
            # 收回工具 = 模型只能输出文本，run 会以 completed 正常收尾。
            # 比 tool_choice="none" 安全：不经过网关透传，零兼容风险。
            if getattr(request, "tools", None):
                request.tools = []
            self.record(decision, progress)
            self._log(f"收回工具强制收尾 reason={decision.reason} | {progress.summary}")
            return await handler(request)

        tool_name = decision.force_tool
        if not tool_name:
            return await handler(request)

        originals = list(getattr(request, "tools", None) or [])
        if not any(_tool_name_of(t) == tool_name for t in originals):
            # 结论工具不在本轮列表里。若强行收敛就会把工具清空、变成"纯文本收尾"，
            # 那不是我们要的效果，所以这种情况直接放行、不动请求。
            self._log(f"需要强制（{decision.reason}）但本轮 tools 里没有 {tool_name}，跳过")
            return await handler(request)

        # ── 第 1 层：把本轮可用工具收敛到只剩结论工具 ────────────────────
        # 这是纯本地行为：模型看不到 acps_delegate / acps_task，
        # 也就不可能再委派或轮询，只能在"交结论"和"输出文本"之间选。
        # 刻意不用 tool_choice —— 推理模型的 thinking 模式会直接 400（见模块 docstring）。
        keep = [t for t in originals if _tool_name_of(t) == tool_name]
        if len(keep) != len(originals):
            request.tools = keep

        # ── 可选加强层：仅当显式开启时才附 tool_choice ────────────────────
        settings = getattr(request, "settings", None)
        want_tool_choice = self.force_tool_choice and isinstance(settings, dict)
        if want_tool_choice:
            settings["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_name},
            }

        self.record(decision, progress)
        self._log(
            f"强制交付物={tool_name} reason={decision.reason} "
            f"（工具 {len(originals)}→{len(keep)}"
            f"{'，附 tool_choice' if want_tool_choice else ''}） | {progress.summary}"
        )

        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001 - 只拦 tool_choice 不兼容，其余原样抛出
            if want_tool_choice and "tool_choice" in str(exc):
                # provider 明确拒绝 tool_choice（如 DeepSeek thinking 模式）：
                # 摘掉它重试一次，并永久关闭这一层，避免每一步都崩。
                self.force_tool_choice = False
                settings.pop("tool_choice", None)
                self._log(f"provider 拒绝 tool_choice，已摘掉并降级重试：{exc}")
                return await handler(request)
            raise

    # -- 第 5 层：整轮跑完仍没交付物 → 程序化兜底 -------------------------

    async def wrap_run(self, ctx: HookContext, handler):
        """包住整轮 run，结束后检查"交付物到底交了没"。

        跑完才判定是**故意**的：前四层都在步内发力，万一仍没逼出来，
        这里用专家载荷程序化补齐一份，保证 output/ 一定有文件 —— 演示不空手。
        """
        result = await handler()

        if not self.fallback_on_failure:
            return result

        # 第 5 层只会拼装 `FinalVerdict`，它是 **Leader 专属**的交付物形状。
        # 网络裁判（conclusion_tool=fact_issue_ruling）复用了本 Hook，
        # 但它的交付物是辩论记录，不是结论 —— 若也走这里，就会往
        # output/ 里写一个"看起来像 Leader 结论"的文件，污染演示判据
        # （判据恰恰就是"output/ 里有没有 verdict-*.json"）。所以显式挡掉。
        if self.conclusion_tool != VERDICT_TOOL:
            self._log(
                f"第 5 层兜底只服务 Leader，当前交付物是 {self.conclusion_tool}，跳过"
            )
            return result

        try:
            messages = getattr(getattr(ctx, "state", None), "messages", None) or []
            if survey(messages, verdict_tool=self.conclusion_tool).verdict_submitted:
                return result  # 真交付了，不用兜底
            path = write_fallback_verdict(
                messages=messages, reason=f"整轮结束仍未提交（status={getattr(result, 'status', '?')}）"
            )
        except Exception as exc:  # noqa: BLE001 - 兜底失败也不能影响 run 结果
            self._log(f"第 5 层兜底失败，已忽略：{exc!r}")
            return result

        if path:
            self.fallback_path = path
            self._log(f"第 5 层兜底：模型未交付，已程序化补齐结论 -> {path}")
        else:
            self._log("第 5 层兜底：模型未交付，且消息里没有可汇总的专家载荷，跳过")
        return result

    # -- 记录当前步号，供 budget-guard 用 ----------------------------------

    async def on_step_start(self, event: StepStartEvent) -> None:
        state = getattr(event.context, "private_state", None)
        if isinstance(state, dict):
            state["step"] = event.step


__all__ = [
    "DELEGATE_TOOL",
    "LeaderProgress",
    "LeaderTerminationHooks",
    "PARTNER_DONE_STATES",
    "PartnerPayloads",
    "TASK_DEAD_STATES",
    "TASK_TOOL",
    "VERDICT_TOOL",
    "build_fallback_verdict",
    "collect_partner_payloads",
    # 🔴 **取「被研判原文」的唯一口径**。先例注入（`precedent.py`）要用同一份：
    #    两边各写一份正则，"改了格式忘了改解析"的表现是**永远取不到原文**，
    #    而"取不到"与"本来就没有先例"在结果上长得一模一样。
    "extract_subject",
    "survey",
    "write_fallback_verdict",
]
