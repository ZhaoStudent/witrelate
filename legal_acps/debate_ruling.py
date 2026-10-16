"""阶段 2：人机辩论台账 → 直跑裁判出**正式裁定书**（计入经验库，2026-10-07）。

阶段 1（`human_debate.py`）产出的辩词落在 `output/debates/`，刻意**不进**
经验库（正方立场是分配的、未经裁定的对抗内容 = 污染）。本模块是**唯一计入
路径**：把台账里那场辩论交给裁判（fact-judge）裁决，裁判用 `fact_issue_ruling`
落盘 `output/ruling-<stamp>.json` —— 有 verdict、经裁定 ⇒ 合法经验。

运行形态（2026-10-07 步骤 0 实证，`run/_probe_debate_ruling.py`）：
  · **直跑不登录**（传 `acps=build_config(role="fact-judge")`，不调
    up_until_ready/login/enroll）：实测 iterations=3 干净走完
    「受理 → 裁定」，没有死磕 discover/delegate；
  · 输入框架里**必须**写明「辩手已离线、不要 acps_discover / acps_delegate」
    —— 裁判 instructions 是「受理→discover→派发→裁定」的严格序列，没有
    这句它会试着找辩手（不登录必然失败，白烧步数、裁定书只剩兜底）；
  · 受理工具 `fact_arbitrate_claim` 纯本地（内查事实库案底）⇒ 不登录照样
    拿到 `official_materials`；实测裁定书 basis 第 1 条引用的就是案底
    （公安部网安局辟谣通报）—— 证据通路在直跑形态下同样闭环；
  · 与阶段 1 同理：不登录 ⇒ 不碰 Registry 会话 ⇒ 与常驻服务随时并行。

裁定书进库后会发生什么（如实说，A/B 的教训）：
  · 新裁定与库内同事件裁定由 `_fold_duplicates` 折进一组；
  · 折叠代表规则「**有标注轮优先**」⇒ 新轮无标注 ⇒ **校准台账不会自己变**
    —— 要让新轮参与校准，须由维护者把 `ground_truth.jsonl` 的 `source`
    重标到新轮（基准 = 事件官方定性，人重标合法）。

用法（仓库根目录；本地无 .env ⇒ 真模型在服务器跑）：

    ./.venv/bin/python -m legal_acps.debate_ruling 20261007T112657Z.jsonl
    ./.venv/bin/python -m legal_acps.debate_ruling            # 交互挑最新一场
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LEDGER_DIR = ROOT / "output" / "debates"
OUTPUT_DIR = ROOT / "output"
WORKSPACE_ROOT = ROOT / ".wit" / "debate-ruling"

JUDGE_ROLE = "fact-judge"
DEFAULT_MAX_STEPS = 12  # 受理 1 + 裁定 1 + 余量（模型若试图 discover 失败几次）


# ══════════════════════════════════════════════════════════════════════
# 纯函数：判据的锚（check_local ⑦-7 / run/meta_verify_debate_ruling.py）
# ══════════════════════════════════════════════════════════════════════


def load_debate(path: Path) -> tuple[str, list[dict]]:
    """读台账 → (事件, round 行列表)。

    坏台账必须**当场炸**（SystemExit），不许静默返回空 —— 「没有轮次」的
    辩论交裁判等于让裁判凭空裁定，而失败形态会伪装成「跑通但裁定书很空」。
    """
    try:
        lines = [json.loads(ln) for ln in
                 path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except FileNotFoundError:
        raise SystemExit(f"找不到台账：{path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"台账 {path.name} 有坏行（{exc}）—— append-only "
                         "文件不该有坏行，先人工核这一份再跑") from None
    head = next((l for l in lines if l.get("type") == "session_open"), None)
    rounds = [l for l in lines if l.get("type") == "round"]
    if head is None:
        raise SystemExit(f"台账 {path.name} 没有 session_open 行 —— 不是"
                         "人机辩论台账（阶段 2 只吃自己家的台账形态）")
    event = str(head.get("event") or "").strip()
    if not event:
        raise SystemExit(f"台账 {path.name} 的 session_open 没有事件描述")
    if not rounds:
        raise SystemExit(f"台账 {path.name} 一条 round 都没有 —— 没辩过就"
                         "没东西可裁（outcome=user_exit 且 0 轮的空场直接退出）")
    return event, rounds


def build_judge_input(event: str, rounds: list[dict]) -> str:
    """拼裁判输入：事件 + **全部**辩词 + 「辩论已完成、辩手离线」任务框架。

    三个不许缺的成分（每个都有对应的判据与退回用例）：
      1. 每一轮的质疑方原文与正方论点全文 —— 裁判裁决的是这场辩论，
         漏一轮它就当那轮没发生过；
      2. 「不要 acps_discover / acps_delegate」—— 不登录直跑的命门
         （见模块 docstring）；
      3. 裁定要求（verdict 四选一 / fallacies 逐字摘录）—— 与裁判自身
         instructions 同口径，防止「简写版任务」诱发简写版裁定。
    """
    replay = []
    for r in rounds:
        pro = r.get("pro") or {}
        replay.append(f"—— 第 {r.get('round')} 轮 ——")
        replay.append(f"【质疑方（人类）论点】\n{r.get('user_argument')}")
        replay.append(
            f"【正方论点】（立场 {pro.get('stance')}，信心 {pro.get('confidence')}，"
            f"反驳轮次 {pro.get('rebuts') or []}）\n{pro.get('key_point')}")
    return (
        f"【待裁定事件】\n{event.strip()}\n\n"
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


def new_rulings(before: set[str], after: set[str]) -> list[str]:
    """跑前跑后 output/ 里 ruling 文件名的差集 = 本轮新增裁定书。

    空差 = 没有交付（status=completed 不算数 —— 同 `_enrich_rulings` 的
    唯一硬判据）。为什么是集合差而不是「找最新文件」：重跑/并发时最新
    的那份未必是这次产的。
    """
    return sorted(after - before)


# ══════════════════════════════════════════════════════════════════════
# 裁判调用与 CLI
# ══════════════════════════════════════════════════════════════════════


async def run_judge(text: str, *, max_steps: int = DEFAULT_MAX_STEPS,
                    invoke=None) -> tuple[object, list[str]]:
    """直跑裁判一轮并返回 (run 对象, 新增裁定书文件名)。

    🔴 **不登录**（运行约束见模块 docstring）。invoke 可注入（check_local
    离线断言用）；workspace 每次独立 ⇒ 重跑不踩上一次的残局。
    """
    if invoke is not None:
        run = await invoke(text)
    else:
        from wit import RunConfig, WitRuntime

        from legal_acps.acps_config import build_config
        from legal_acps.agents import build_agent
        from legal_acps.llm import build_model
        from legal_acps.run_agent import _active_aic

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        agent = build_agent(JUDGE_ROLE, build_model(),
                            self_aic=_active_aic(JUDGE_ROLE))
        runtime = WitRuntime(
            agent, workspace=str(WORKSPACE_ROOT / stamp),
            acps=build_config(role=JUDGE_ROLE))
        before = {p.name for p in OUTPUT_DIR.glob("ruling-*.json")}
        run = await runtime.run(text, config=RunConfig(max_steps=max_steps))
        after = {p.name for p in OUTPUT_DIR.glob("ruling-*.json")}
        return run, new_rulings(before, after)
    # 注入路径下落盘由 invoke 的假实现负责，这里只透传 run（离线断言用）。
    return run, []


def _latest_ledger() -> Path:
    files = sorted(LEDGER_DIR.glob("*.jsonl"))
    if not files:
        raise SystemExit(f"{LEDGER_DIR} 下没有任何台账 —— 先跑一场人机辩论："
                         "./.venv/bin/python -m legal_acps.human_debate")
    return files[-1]


async def _cli() -> int:
    ap = argparse.ArgumentParser(
        description="阶段 2：人机辩论台账 → 直跑裁判出正式裁定书（计入经验库）")
    ap.add_argument("ledger", nargs="?",
                    help="台账文件名或路径（不给 = 挑最新一场）")
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                    help=f"裁判步数上限（默认 {DEFAULT_MAX_STEPS}）")
    args = ap.parse_args()

    p = Path(args.ledger) if args.ledger else _latest_ledger()
    if not p.is_absolute():
        cand = LEDGER_DIR / p.name
        p = cand if cand.exists() else p
    event, rounds = load_debate(p)
    text = build_judge_input(event, rounds)
    print(f"台账 {p.name}：{len(rounds)} 轮，事件 {event[:40]}…，"
          f"裁判输入 {len(text)} 字符（直跑不登录）")

    run, new = await run_judge(text, max_steps=args.max_steps)
    print(f"run() 完成：status={getattr(run, 'status', '?')} "
          f"iterations={getattr(run, 'iterations', '?')}")
    for o in getattr(run, "observations", None) or []:
        print(f"    · {o.get('tool') or o.get('name') or '?'}"
              f" ({o.get('status', '?')})")

    if not new:
        print("❌ 没有新增裁定书（唯一硬判据不成立）—— 裁判没走完"
             "「受理→裁定」，重试或检查输入框架")
        return 1
    ruling = json.loads((OUTPUT_DIR / new[-1]).read_text(encoding="utf-8"))
    print(f"✅ 新增裁定书 {new[-1]}：")
    print(f"    verdict={ruling.get('verdict')}  confidence={ruling.get('confidence')}")
    print(f"    basis {len(ruling.get('basis') or [])} 条 / "
          f"fallacies {len(ruling.get('fallacies') or [])} 条 / "
          f"disputed {len(ruling.get('disputed_points') or [])} 条")
    for f in (ruling.get("fallacies") or [])[:3]:
        if isinstance(f, dict):
            print(f"      · {f.get('kind')}（{str(f.get('excerpt'))[:60]}）")
    print("（已落 output/ ⇒ 进经验库；校准台账**不会自己变** —— 新轮无标注，"
          "要参与校准须人工重标 ground_truth 的 source 到新轮）")
    return 0


def main() -> int:
    from dotenv_lite import load_env

    load_env(ROOT / ".env")
    return asyncio.run(_cli())


if __name__ == "__main__":
    raise SystemExit(main())
