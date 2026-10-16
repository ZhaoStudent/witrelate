"""**真机端到端 · 线索受理链** —— 主控「线索报送分支」在真机上会不会响。

它要回答的问题（上线 #148 的验收，只能被真机一轮回答）：

  1. 主控收到「转交看到的传闻、只登记不核查」类消息时，**线索报送分支**
     会不会真的把「线索受理官」委派出去（此前该分支只有离线断言，从未
     在真机上亮过 ——「装了 ≠ 会响」）。
  2. 受理官拿到委派后，会不会真的调 `lead.submit_lead` 把线索落进
     `.wit/leads/queue.jsonl`（受理是这条链**唯一**的交付物）。

它与 `run/e2e_leader_image.py` 同一条路：进程内 `runtime.run(parts)`
（用主控自己的身份，出站委派走真实 ACPs/mTLS），**不启动监听**。
⚠️ 跑之前**必须停掉**常驻的 `acps-leader.service`（`systemctl stop`，
不是 `serve.sh stop` —— 单元 `Restart=always` 会立刻拉起来抢工作区）；
**`acps-lead-intake.service` 必须在跑**（委派的目标就是它 —— 这一步
顺带就是 8454「真调一次」的实证，curl 探不了 mTLS）。

════════════════════════════════════════════════════════════════════════
判据（全部「从交付出来的值上唯一判定」，不认措辞类字符串）
════════════════════════════════════════════════════════════════════════

  A｜线索真的落进队列：跑完后 `queue.jsonl` **新增**一条受理记录，其
     `claim` 含本轮独有标记「临江路78号第三中学」。标记由本轮输入**现造**
     （不在任何提示词里）⇒ 只有把原文一路传到受理官、再由受理官写进
     `lead_submit_lead`，它才可能出现在队列里 —— 模型自己编不出这条地名。
     ⚠️ 先记基线（跑之前的 lead_id 集）：「没有新记录」必须显式判失败，
     不许借用历史产物（e2e_leader_image 的 B′ 同一条教训）。
     ⚠️ 基线为空（queue.jsonl 不存在）是**合法基线**（0 条），不算失败。

  B｜委派真的发给了线索受理官：主控任务库**新增**一条 outbound 任务，
     `remote_aic` == 线索受理官的 AIC（从 `.wit/lead-intake/acps/acs.final.json`
     **现读**，不写死 —— AIC 是平台分配的，写进探针就会漂移）。
     任务库是本项目「谁真的被调用过」的唯一权威（并发扇出 #25 同款判据）。

  T｜这一轮没有被 token 上限截断（`truncated_of` == 0 才算干净；
     截断是「空转」的唯一可观测信号）。

三条**全部进退出码**：A 自带基线对比（不借历史），B/T 无前置条件。

用法（在服务器的 witrelate 目录下；先 `systemctl stop acps-leader`）：
    ./.venv/bin/python -m run.e2e_lead_intake
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dotenv_lite import load_env

load_env()

from wit import RunConfig, TextPart, WitRuntime  # noqa: E402

from legal_acps import run_ledger  # noqa: E402
from legal_acps.acps_config import build_config  # noqa: E402
from legal_acps.agents import build_agent  # noqa: E402
from legal_acps.lead_store import queue_path  # noqa: E402
from legal_acps.llm import build_model  # noqa: E402
from legal_acps.run_agent import _active_aic, research_run_config  # noqa: E402
from legal_acps.token_budget import truncated_of  # noqa: E402

BAR = "─" * 70

#: 本轮独有标记：探针**现造**的地名组合，不在任何提示词 / 先例 / 案例库里。
#: 模型唯一能让它出现在 queue.jsonl 里的方式 = 把我发的原文传给受理官。
UNIQUE_MARK = "临江路78号第三中学"


def task_text() -> str:
    """触发文案必须**同时**满足主控提示词里线索报送分支的两个条件：
    ① 明确是「转交看到的传闻」（刷到 / 群里都在传 / 报个线索）；
    ② **没有**要求当场出核查结论（少这条会走核查分支 —— 那是边界
       规则定的正确行为，但这一轮就白跑了）。
    ⚠️ 文案同时是 claim 的原文来源：不得含定性词 / 处罚词（受理闸门
       会拦 —— 拦了 A 就红，而那次红证明的是文案错，不是链路坏）。
    """
    return (
        "我想给你们报个线索。今晚我们小区的几个业主群里都在传一条消息，"
        f"说{UNIQUE_MARK}检出教学楼结构有问题，明天上午全校停课搬迁，"
        "群里传得挺厉害的，我拿不准真假。"
        "现在不用帮我核实真假，请把这个情况登记进待核查的线索队列，"
        "希望你们后续跟进。"
    )


def read_queue_ids() -> set[str]:
    """基线/终态读队列：返回已有 lead_id 集合。文件不存在 = 空基线（合法）。"""
    p = queue_path()
    if not p.exists():
        return set()
    ids = set()
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            try:
                ids.add(json.loads(ln).get("lead_id", ""))
            except ValueError:
                pass  # 残行如实跳过；判据 A 比对新记录，不依赖旧行完好
    return ids


def read_new_records(before_ids: set[str]) -> list[dict]:
    """跑完后取**本轮新增**的受理记录（含历史行的文件里只挑新面孔）。"""
    p = queue_path()
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("lead_id") not in before_ids:
            out.append(rec)
    return out


def lead_intake_aic() -> str:
    """线索受理官的 AIC —— 从它自己的 `acs.final.json` **现读**（单一事实源）。

    读不到（未注册/未过审）= 本探针**不该被跑**：直接抛，让失败带着原因走。
    """
    p = Path(".wit/lead-intake/acps/acs.final.json")
    data = json.loads(p.read_text(encoding="utf-8"))
    aic = str(data.get("aic") or "")
    if not aic:
        raise RuntimeError(f"{p} 里没有 aic 字段 —— lead-intake 未完成注册？")
    return aic


def delegated_new_tasks(before_refs: set[str], target_aic: str) -> list[dict]:
    """判据 B：主控任务库里**本轮新增**、发给线索受理官的 outbound 任务。

    复用 `tools.metrics._read_tasks`（全项目唯一的任务库 reader —— 同一判定
    不写第二份）。tools 可以 import runtime 侧的模块，方向不反过来即可。
    """
    from tools.metrics import _read_tasks

    db = ".wit/leader/acps/aip-tasks.sqlite3"
    rows = _read_tasks(db)
    out = []
    for r in rows:
        if r.task_ref in before_refs:
            continue
        if r.direction == "outbound" and str(r.remote_aic) == target_aic:
            out.append(
                {"task_ref": r.task_ref, "state": r.state, "created_at": r.created_raw}
            )
    return out


def task_refs_snapshot() -> set[str]:
    from tools.metrics import _read_tasks

    db = Path(".wit/leader/acps/aip-tasks.sqlite3")
    if not db.exists():
        return set()
    return {r.task_ref for r in _read_tasks(str(db))}


async def run_probe(
    runtime: object,
    parts: list[object],
    task: str,
    config: RunConfig,
    *,
    role: str = "leader",
    wit_dir: str | None = None,
):
    """跑这一轮 —— **必须经发起轮台账**（与 `e2e_leader_image.run_probe` 同款）。

    做成可 import 的函数，`legal_acps.check_local` 3c 才能塞假 runtime 进来，
    行为断言「探针这一轮真的记了台账」。
    """
    return await run_ledger.run_ledgered(
        lambda: runtime.run(parts, config=config),
        text=task,
        label=run_ledger.PROBE_LABEL,
        role=role,
        wit_dir=wit_dir,
    )


def output_text(parts: list[object]) -> str:
    """只取 TextPart（ReasoningPart 是内部思考，不能当答复 —— 同款坑）。"""
    return "\n".join(
        text
        for part in parts
        if type(part).__name__ == "TextPart"
        for text in [getattr(part, "text", None)]
        if isinstance(text, str) and text
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", action="store_true", help="不打长文本，只看结构")
    args = parser.parse_args()

    target_aic = lead_intake_aic()
    task = task_text()
    base_ids = read_queue_ids()
    base_refs = task_refs_snapshot()

    print("=" * 70)
    print("  真机端到端 · 线索受理链（主控 → 线索受理官 → 队列落盘）")
    print("=" * 70)
    print(f"  目标 AIC  ：{target_aic}（现读自 acs.final.json）")
    print(f"  队列基线  ：{len(base_ids)} 条已有受理记录（跑完必须出现**新面孔**）")
    print(f"  任务基线  ：{len(base_refs)} 条已有任务（跑完必须出现发给受理官的新委派）")
    print(f"  独有标记  ：「{UNIQUE_MARK}」—— 只能来自本轮输入的一路传递")

    role = "leader"
    agent = build_agent(role, build_model(), self_aic=_active_aic(role))
    config = build_config(role=role)
    runtime = WitRuntime(agent, workspace=f".wit/{role}", acps=config)
    parts: list[object] = [TextPart(text=task)]

    print(f"\n{BAR}\n  正在跑……（预计 2~5 分钟；受理官只需一次往返，链深 1）\n{BAR}")
    try:
        result, ledger_run = await run_probe(
            runtime, parts, task, research_run_config(), role=role
        )
    except Exception as exc:  # noqa: BLE001 - 全真跑要把失败原样打出来给人看
        print(f"  ❌ run 抛异常：{type(exc).__name__}: {exc}")
        return 1

    print(
        f"  [台账] run_id = {ledger_run.run_id}  label = {ledger_run.label}"
        f"（.wit/{role}/runs.jsonl，只增不改）"
    )
    print(f"  status = {result.status}   迭代 = {result.iterations} 轮")

    print(f"\n{BAR}\n  工具调用轨迹\n{BAR}")
    for obs in result.observations or []:
        name = obs.get("tool") or obs.get("name") or "?"
        print(
            f"  · {name:<28} status={obs.get('status', '?'):<8} "
            f"{obs.get('duration_ms', '?')} ms"
        )

    body = output_text(result.output_parts)
    if not args.brief:
        print(f"\n{BAR}\n  主控最终文本\n{BAR}")
        print(body if body else "  （本次没有文字输出）")

    # ── 判据 ────────────────────────────────────────────────────────────
    new_records = read_new_records(base_ids)
    # 归一化比对：受理官转述 claim 可能丢标点/空格 ⇒ 两侧都剥掉再找标记。
    def _norm(s: str) -> str:
        return "".join(ch for ch in s if ch not in " \t，。、；：！？「」『』（）()")

    crit_a = any(
        _norm(UNIQUE_MARK) in _norm(str(rec.get("claim") or "")) for rec in new_records
    )
    new_tasks = delegated_new_tasks(base_refs, target_aic)
    crit_b = len(new_tasks) >= 1
    truncated = truncated_of(result)
    crit_t = truncated == 0

    print(f"\n{BAR}\n  判据\n{BAR}")
    if not new_records:
        print(
            "  [❌] A｜线索落进队列：本轮**没有新受理记录**（不得借用历史产物判通过）"
        )
    else:
        for rec in new_records:
            claim = str(rec.get("claim") or "")
            hit = _norm(UNIQUE_MARK) in _norm(claim)
            print(
                f"  [{'✅' if hit else '❌'}] A｜新受理记录 {rec.get('lead_id')} "
                f"category={rec.get('category')!r} "
                f"{'含' if hit else '**不含**'}独有标记"
            )
            if not args.brief:
                print(f"        claim：{claim}")
        crit_a = crit_a and True
    print(
        f"  [{'✅' if crit_b else '❌'}] B｜主控任务库新增发给受理官的委派："
        + (f"{len(new_tasks)} 条 {new_tasks}" if crit_b else "**一条都没有** ⇒ 分支没响")
    )
    print(
        f"  [{'✅' if crit_t else '❌'}] T｜未被 token 上限截断："
        + (
            "0 次 `finish_reason=length`"
            if truncated == 0
            else f"**被截断 {truncated} 次**"
            if truncated > 0
            else "**无法判定**（拿不到 result.state.metadata）"
        )
    )

    print(f"\n{BAR}\n  结论\n{BAR}")
    verdict = crit_a and crit_b and crit_t
    print(
        "  ✅ 线索受理链真机跑通：主控线索报送分支 → 线索受理官 → 队列落盘"
        if verdict
        else "  ❌ 未跑通 —— 按上面红的那条归因（文案错 / 分支没亮 / 被截断）"
    )
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
