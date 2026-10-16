"""真机对跑：一轮完整人机辩论（功能③ · 2026-10-07）。

在**服务器**跑（本地无 .env ⇒ 没有真模型）：

    ./.venv/bin/python -m run._probe_human_debate

为什么必须真机跑（离线判据覆盖不了的）：
  · check_local ⑦-6 的假 invoke 证明的是**循环逻辑**（回放/记账/终止）；
    而「正方模型在多轮回放形态下真的交得出结构化论点」「步骤 0 的不登录
    运行形态在完整 CLI 链路上成立」只有真模型能证。
  · 走的是**完整 CLI 路径**（argparse → script_user_source → run_debate →
    _invoke_pro → 台账落盘），不是绕过 main() 的库调用 —— 「手工跑通」
    证明不了「入口跑通」。

判据（唯一硬判据 = 台账文件的真实字节，不信 stdout 的自说自话）：
  1. CLI 退出码 0（outcome ∈ {concede, user_exit, max_rounds}）；
  2. output/debates/<sid>.jsonl 存在且逐行可解析：open → ≥1 round → close；
  3. 每条 round 记录：用户原文非空 + 正方五字段（round/stance/confidence/
     key_point/rebuts）齐全；
  4. close 的 rounds 与 round 行数一致、outcome 与轮次对得上；
  5. round_failed 如实报告（有就列出，不当作失败 —— 但 outcome=error 判负）。

用户论点内嵌在脚本里（三轮，逐步收紧对矿泉水谣言的质疑）；正方立场 =
「该传闻属实」（分配立场，与真相无关 —— 它只负责把该立场的最强论证顶出来）。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EVENT = ("网传某矿泉水厂今年3月、6月各有一名员工落水身亡，尸体捞起时仅剩骨架，"
         "含尸体的生产用水被附近居民饮用。")

#: 用户（质疑方）三轮论点：①无源可查 ②法医学/工艺细节不成立 ③模板谣言可检索。
USER_ARGS = [
    "这个传闻连基本的时间、地点、人物都给不出——没有厂名、没有城市、没有逝者"
    "姓名、没有任何媒体的只字报道。两次死亡事故、两次打捞、两次家属追问，却"
    "没有任何一手信息源，这本身就是典型的无源谣言形态。你要大家相信它属实，"
    "请先拿出一个可核验的信源。",
    "从法医学看，「落水数月仅剩骨架」说不通：水中尸体的软组织腐败以月计，企业"
    "蓄水池条件下不可能短时间内白骨化。而且自来水生产要经过混凝、沉淀、过滤、"
    "消毒多道工序，一具尸体不可能整段进入成品水被居民「饮用」。这个细节的"
    "生理学与工艺学两头都不成立，更像是猎奇修辞而不是事实。",
    "这条文本是可检索的「模板谣言」：把厂名换掉的同款「员工落水、仅剩骨架、"
    "居民饮用尸水」在多个省份出现过，且都被当地有关部门辟谣。它只在家族群"
    "里流转，几年了没有一家媒体跟进、没有一个监管部门回应、没有一个家属"
    "发声。你坚持它属实，就必须解释：为什么一条「真事」能逃过所有信息渠道，"
    "只活在转发文本里？",
]

CLEAN_OUTCOMES = {"concede", "user_exit", "max_rounds"}
REQUIRED_PRO_FIELDS = ("round", "stance", "confidence", "key_point", "rebuts")


def main() -> int:
    with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8", newline="\n") as f:
        f.write("\n".join(USER_ARGS) + "\n")
        args_path = f.name

    try:
        r = subprocess.run(
            [sys.executable, "-m", "legal_acps.human_debate",
             "--event", EVENT, "--script", args_path, "--max-rounds", "4"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=1800)
    finally:
        Path(args_path).unlink(missing_ok=True)

    print(r.stdout)
    if r.stderr.strip():
        print("── stderr ──")
        print(r.stderr)

    failures: list[str] = []

    # ── (1) 退出码 ──────────────────────────────────────────────────
    if r.returncode != 0:
        failures.append(f"CLI 退出码 {r.returncode}（期望 0）")

    # ── (2) 定位台账（sid 从 stdout 取，取不到再按最新文件兜底）──────
    sid = None
    m = re.search(r"会话 (\d{8}T\d{6}Z)", r.stdout or "")
    if m:
        sid = m.group(1)
    ledger_dir = ROOT / "output" / "debates"
    ledger_path = (ledger_dir / f"{sid}.jsonl") if sid else None
    if ledger_path is None or not ledger_path.exists():
        if ledger_dir.exists():
            cands = sorted(ledger_dir.glob("*.jsonl"))
            ledger_path = cands[-1] if cands else None
    if ledger_path is None or not ledger_path.exists():
        print(f"[失败] 找不到会话台账（sid={sid}）—— 没有落盘就没有辩论记录")
        return 1
    print(f"── 台账：{ledger_path.name} ──")

    # ── (3) 逐行解析 + 形态断言 ─────────────────────────────────────
    lines: list[dict] = []
    for i, ln in enumerate(ledger_path.read_text(encoding="utf-8").splitlines(), 1):
        if not ln.strip():
            continue
        try:
            lines.append(json.loads(ln))
        except json.JSONDecodeError as exc:
            failures.append(f"第 {i} 行不是合法 JSON：{exc}")
    types = [l.get("type") for l in lines]
    if not types or types[0] != "session_open":
        failures.append(f"台账第一行不是 session_open（{types[:1]}）")
    if not types or types[-1] != "session_close":
        failures.append(f"台账最后一行不是 session_close（{types[-1:]}）")

    rounds = [l for l in lines if l.get("type") == "round"]
    failed = [l for l in lines if l.get("type") == "round_failed"]
    close = next((l for l in reversed(lines) if l.get("type") == "session_close"), {})

    if not rounds:
        failures.append("一条 round 记录都没有 —— 辩论等于没发生")
    for rec in rounds:
        n = rec.get("round")
        if not str(rec.get("user_argument") or "").strip():
            failures.append(f"第 {n} 轮：用户原文为空")
        pro = rec.get("pro") or {}
        for field in REQUIRED_PRO_FIELDS:
            if field not in pro:
                failures.append(f"第 {n} 轮：正方论点缺字段 {field}")
        if pro.get("stance") not in ("hold", "concede"):
            failures.append(f"第 {n} 轮：stance 异常（{pro.get('stance')!r}）")
    if failed:
        print(f"⚠️ 有 {len(failed)} 条 round_failed（如实报告，不算失败）：")
        for rec in failed:
            print(f"    · 第 {rec.get('round')} 轮 attempt={rec.get('attempt')}"
                  f"：{rec.get('reason')}")

    # ── (4) close 与轮次对账 ────────────────────────────────────────
    outcome = str(close.get("outcome") or "")
    if outcome not in CLEAN_OUTCOMES:
        failures.append(f"outcome = {outcome!r}（期望 ∈ {sorted(CLEAN_OUTCOMES)}）")
    if close.get("rounds") != len(rounds):
        failures.append(f"close.rounds = {close.get('rounds')}，"
                        f"round 行数 = {len(rounds)}（两处对不上）")
    if outcome == "max_rounds" and len(rounds) < 4:
        failures.append(f"outcome=max_rounds 但只辩了 {len(rounds)} 轮"
                        f"（上限 4 ⇒ 应恰好 4 轮，除非用户论点先用完）")

    # ── 结论 ────────────────────────────────────────────────────────
    if failures:
        for msg in failures:
            print(f"[失败] {msg}")
        return 1
    print(f"[通过] 真机人机辩论：{len(rounds)} 轮，outcome={outcome}，"
          f"正方立场=该传闻属实，台账={ledger_path.name}")
    for rec in rounds:
        pro = rec.get("pro") or {}
        print(f"    · 第 {rec.get('round')} 轮 stance={pro.get('stance')} "
              f"conf={pro.get('confidence')} "
              f"key_point={str(pro.get('key_point'))[:40]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
