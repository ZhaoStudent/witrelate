"""只读汇总：发起轮台账的全部轮次 + 常驻服务是否被 CLI 轮次影响过会话。

为什么
══════
#27 的约束写着「跑定期研判须与常驻服务错开，避免顶掉 Registry 会话」。
`serve.sh` / systemd 单元都记着「多个进程同时用同一账号登录 Registry，
后登录的会把先前会话顶掉」—— 而**每天确实会有一条真跑整轮的 CLI 轮次**
（原先挂在一条每 2 小时的自动化上；**2026-10-05 该自动化已收窄为「只复查 discovery 索引」、
不再跑整轮**，定期真链路改由 `acps-case-round.timer` 承担，见
`docs/上线操作手册.md` 第 14 步），所以必须实测：这些 CLI 轮次到底有没有影响常驻服务。

三条判据（都只读）：
  ① 台账里有哪些轮次、label 分别是什么、跑了多久 —— 定时轮次是否真的在跑；
  ② 每个常驻角色的**入站访问日志**（AMP access）在 CLI 轮次前后有没有断档；
  ③ `journalctl` 里有没有出现 blocked / Could not validate credentials。

⚠️ 判据说明：③ 只能证明"服务自己报过"。服务在没人调用它时不会去用那个会话，
   所以**没报错 ≠ 会话还在** —— 这一条要如实标注，不能当"没问题"的证据。
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime

WIT = sys.argv[1] if len(sys.argv) > 1 else ".wit"


def main() -> int:
    # ── ① 台账全部轮次 ──
    path = os.path.join(WIT, "leader", "runs.jsonl")
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue

    # 开/收两条同 run_id ⇒ 折叠成一轮
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        rid = r.get("run_id") or "?"
        if rid not in by_id:
            by_id[rid] = {}
            order.append(rid)
        # 后面的记录覆盖前面的（收尾记录更全）
        by_id[rid].update({k: v for k, v in r.items() if v is not None})

    print(f"台账 {path}：{len(rows)} 行 ⇒ {len(order)} 轮")
    print()
    print(f"{'run_id':<14}{'label':<16}{'started(UTC)':<24}{'status':<12}"
          f"{'迭代':>5}{'耗时s':>7}  输入前 24 字")
    print("-" * 110)
    labels: Counter[str] = Counter()
    for rid in order:
        r = by_id[rid]
        st = str(r.get("started_at") or "")
        fin = str(r.get("finished_at") or "")
        dur = ""
        if st and fin:
            try:
                d = (datetime.fromisoformat(fin) - datetime.fromisoformat(st))
                dur = f"{d.total_seconds():.0f}"
            except ValueError:
                dur = "?"
        labels[str(r.get("label"))] += 1
        head = str(r.get("text_head") or "").replace("\n", " ")[:24]
        print(f"{rid:<14}{str(r.get('label')):<16}{st[:23]:<24}"
              f"{str(r.get('status')):<12}{str(r.get('iterations')):>5}{dur:>7}  {head}")
    print()
    print("按 label 统计：")
    for k, v in labels.most_common():
        print(f"    {k:<16}{v} 轮")
    print()

    # ── ② 入站访问日志断档检查 ──
    print("=== 各角色 AMP access 日志：条目数 / 时间窗 ===")
    for role_dir in sorted(glob.glob(os.path.join(WIT, "*"))):
        role = os.path.basename(role_dir)
        files = sorted(glob.glob(os.path.join(role_dir, "logs", "amp_access_*.jsonl")))
        if not files:
            continue
        stamps: list[str] = []
        n = 0
        for fp in files:
            with open(fp, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    n += 1
                    try:
                        ts = json.loads(line).get("timestamp")
                    except ValueError:
                        continue
                    if isinstance(ts, str):
                        stamps.append(ts)
        stamps.sort()
        span = f"{stamps[0][:19]} .. {stamps[-1][:19]}" if stamps else "（无）"
        print(f"    {role:<18}{n:>5} 条   {span}")
    print()

    # ── ③ 常驻服务有没有报过凭证问题 ──
    print("=== journalctl 检索（近 3 天）===")
    units = subprocess.run(
        ["systemctl", "list-units", "acps-*", "--no-legend", "--plain"],
        capture_output=True, text=True, check=False,
    ).stdout
    names = sorted({
        l.split()[0] for l in units.splitlines() if l.strip()
    })
    hits = 0
    for u in names:
        res = subprocess.run(
            ["journalctl", "-u", u, "--since", "3 days ago", "--no-pager"],
            capture_output=True, text=True, check=False,
        ).stdout
        bad = [
            l for l in res.splitlines()
            if any(k in l for k in ("blocked", "Could not validate",
                                    "RegistryClientError", "顶掉", "401", "Unauthorized"))
        ]
        tag = f"{len(bad)} 条可疑" if bad else "无"
        print(f"    {u:<30}{tag}")
        for l in bad[:3]:
            print(f"        {l.strip()[:110]}")
        hits += len(bad)
    print()
    print(f"合计可疑行 {hits} 条。")
    if not hits:
        print("⚠️ 但这**不能**证明会话未被顶掉：常驻服务只在被调用时才会用到那个会话，")
        print("   自测流量都是自家 AIC 发起的，所以'没报错'与'会话还在'是两回事。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
