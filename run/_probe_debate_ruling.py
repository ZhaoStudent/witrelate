"""步骤 0 探针：人机辩论台账 → 直跑裁判（不登录）能否出正式裁定书（阶段 2，2026-10-07）。

要实证的问题（决定阶段 2 的形态，别预设答案）：
  1. fact-judge **不登录**（传 acps= 不调 up_until_ready）run() 能否完成——
     步骤 0 已证**叶子**（debate-pro）可行；fact-judge 更复杂（挂
     AipDelegationHooks，instructions 是「受理→discover→派发→裁定」的
     严格序列），不登录时它若死磕 discover/delegate，步数会被烧光、
     裁定书只剩终止 Hook 兜底——那就得退回「登录完整链路 + 辩词参考」形态。
  2. 「辩论已完成、辩手离线」的输入框架能否让它走 **受理 → 直接裁定**
     （受理工具 fact_arbitrate_claim 纯本地：内查案底 fact_store）。
  3. 唯一硬判据 = output/ 新增 ruling-*.json（status=completed 不算数）。
  4. 裁定书质量可读：event 是否对得上辩论事件、basis/fallacies 是否
     真的引用了辩词与案底（而不是空转兜底）。

用法（服务器）：

    ./.venv/bin/python -m run._probe_debate_ruling [台账文件名（默认昨天那场）]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path("/home/ubuntu/witrelate")
sys.path.insert(0, str(ROOT))

from dotenv_lite import load_env  # noqa: E402

load_env(ROOT / ".env")

from wit import RunConfig, WitRuntime  # noqa: E402

from legal_acps.acps_config import build_config  # noqa: E402
from legal_acps.agents import build_agent  # noqa: E402
from legal_acps.llm import build_model  # noqa: E402
from legal_acps.run_agent import _active_aic  # noqa: E402

DEFAULT_LEDGER = "20261007T112657Z.jsonl"
MAX_STEPS = 12  # 受理 1 + 裁定 1 + 余量（模型若试图 discover 失败几次）


def load_debate(ledger_name: str) -> tuple[str, list[dict]]:
    """读台账 → (事件, round 行列表)。缺 event / 缺轮次 = 探针前置不成立。"""
    p = ROOT / "output" / "debates" / ledger_name
    lines = [json.loads(ln) for ln in
             p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    head = next((l for l in lines if l.get("type") == "session_open"), None)
    rounds = [l for l in lines if l.get("type") == "round"]
    if not head or not rounds:
        raise SystemExit(f"台账形态不对：open={bool(head)} rounds={len(rounds)}")
    return str(head.get("event") or ""), rounds


def build_judge_input(event: str, rounds: list[dict]) -> str:
    """裁判输入：事件 + 辩词全文 + 「辩论已完成、辩手离线」任务框架。"""
    replay = []
    for r in rounds:
        pro = r.get("pro") or {}
        replay.append(f"—— 第 {r.get('round')} 轮 ——")
        replay.append(f"【质疑方（人类）论点】\n{r.get('user_argument')}")
        replay.append(
            f"【正方论点】（立场 {pro.get('stance')}，信心 {pro.get('confidence')}，"
            f"反驳轮次 {pro.get('rebuts') or []}）\n{pro.get('key_point')}")
    return (
        f"【待裁定事件】\n{event}\n\n"
        f"【已完成的人机辩论记录】\n（正方 = 持「该传闻属实」立场的辩手；"
        f"质疑方 = 人类提问者）\n" + "\n".join(replay) + "\n\n"
        "【任务】\n"
        "这场辩论已经完成，两位辩手均已离线（**不要 acps_discover、"
        "不要 acps_delegate** —— 派发不会成功，纯烧步数）。请：\n"
        "① 调用 fact_arbitrate_claim 受理立题：把事件整理成可判真假的命题 +"
        " 可核验事实清单 + 双方立场（受理返回的 official_materials 是本地"
        "事实库的案底，命中即权威依据）；\n"
        "② 结合案底与上面的辩论记录，按裁定要求逐项检查证据层/修辞层"
        "误导性话术；\n"
        "③ 调用 fact_issue_ruling 出具裁定书（verdict 四选一；fallacies"
        " 逐条列出命中话术并**逐字摘录**原话）。"
    )


async def main() -> int:
    ledger_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LEDGER
    event, rounds = load_debate(ledger_name)
    text = build_judge_input(event, rounds)
    print(f"[0] 台账 {ledger_name}：{len(rounds)} 轮，事件 {event[:40]}…")
    print(f"    裁判输入 {len(text)} 字符")

    agent = build_agent("fact-judge", build_model(),
                        self_aic=_active_aic("fact-judge"))
    runtime = WitRuntime(agent, workspace=".wit/fact-judge-debate-ruling",
                         acps=build_config(role="fact-judge"))
    print("[1] WitRuntime 构造完成（传 acps=、未登录）")

    out_dir = ROOT / "output"
    before = set(p.name for p in out_dir.glob("ruling-*.json"))
    run = await runtime.run(text, config=RunConfig(max_steps=MAX_STEPS))
    print(f"[2] run() 完成：status={run.status} iterations={run.iterations}")
    for o in run.observations or []:
        print(f"    · {o.get('tool') or o.get('name') or '?'}"
              f" ({o.get('status', '?')})")

    new = sorted(set(p.name for p in out_dir.glob("ruling-*.json")) - before)
    print(f"[3] 新增裁定书：{new or '无'}")
    if not new:
        print("[4] ❌ 没有落盘 = 阶段 2 直跑形态不成立（考虑退回登录完整链路）")
        return 1
    ruling = json.loads((out_dir / new[-1]).read_text(encoding="utf-8"))
    print(f"[4] 裁定书 {new[-1]}：")
    print(f"    event     = {ruling.get('event')}")
    print(f"    verdict   = {ruling.get('verdict')}  "
          f"confidence = {ruling.get('confidence')}")
    print(f"    basis     = {len(ruling.get('basis') or [])} 条")
    for b in (ruling.get("basis") or [])[:3]:
        print(f"      · {str(b)[:80]}")
    print(f"    fallacies = {len(ruling.get('fallacies') or [])} 条")
    for f in (ruling.get("fallacies") or [])[:3]:
        if isinstance(f, dict):
            print(f"      · {f.get('kind')}（{str(f.get('excerpt'))[:60]}）")
    print(f"    disputed  = {len(ruling.get('disputed_points') or [])} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
