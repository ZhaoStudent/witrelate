"""只读追查一处**跨源矛盾**：任务库说有 1 次外部访问，access 日志说外部 = 0。

为什么必须查
════════════
`tools/metrics.py` 的【指标 4】从任务库算出 debate-pro 被外部 AIC
`…0001.00001.EZTK03.PABX6D.0JUV` 访问 1 次；而同一角色的 access 日志里
「外部 = 0」。这是**同一个事实的两个来源给出了不同答案** —— 而"外部访问量"
恰恰是本项目最敏感、最容易被质疑的数字。

可能的解释（不猜，逐个量）：
  A) 那次调用发生得比 access 日志的起点还早（日志截断/轮转）
  B) 它不是 rpc 投递（delivery ≠ rpc）⇒ 不走 acps.direct.rpc，自然没 access 记录
  C) 任务建了但对端根本没连上（state 异常）⇒ 服务端没受理，不写 access
  D) access 日志里其实有这条，只是我的 caller 分类判错了（那分类就有洞）

只读：sqlite 用 mode=ro 打开。
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys

WIT = sys.argv[1] if len(sys.argv) > 1 else ".wit"
EXT = "1.2.156.3088.1.0001.00001.EZTK03.PABX6D.0JUV"


def main() -> int:
    for db in sorted(glob.glob(os.path.join(WIT, "*", "acps", "aip-tasks.sqlite3"))):
        role = os.path.basename(os.path.dirname(os.path.dirname(db)))
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT task_ref, direction, delivery, state, session_id, task_id, "
                "created_at, updated_at, local_error FROM aip_tasks "
                "WHERE remote_aic = ? ORDER BY created_at",
                (EXT,),
            ).fetchall()
        finally:
            con.close()
        if not rows:
            continue
        print(f"=== 角色 {role}：含该外部 AIC 的任务 {len(rows)} 条 ===")
        for r in rows:
            print(f"    task_ref={r['task_ref']}")
            print(f"      direction={r['direction']}  delivery={r['delivery']}  state={r['state']}")
            print(f"      created_at={r['created_at']}")
            print(f"      updated_at={r['updated_at']}")
            if r["local_error"]:
                print(f"      local_error={str(r['local_error'])[:200]}")

        # 同一个角色 access 日志的时间窗与 caller 集合
        accs = sorted(glob.glob(os.path.join(WIT, role, "logs", "amp_access_*.jsonl")))
        print(f"    access 文件 {len(accs)} 个")
        stamps: list[str] = []
        callers: dict[str, int] = {}
        routes: dict[str, int] = {}
        for fp in accs:
            with open(fp, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    ts = rec.get("timestamp")
                    if isinstance(ts, str):
                        stamps.append(ts)
                    body = rec.get("body") or {}
                    ca = str((body.get("caller") or {}).get("aic") or "") or "(缺失)"
                    callers[ca] = callers.get(ca, 0) + 1
                    rt = str((body.get("request") or {}).get("route") or "?")
                    routes[rt] = routes.get(rt, 0) + 1
        if stamps:
            stamps.sort()
            print(f"    access 时间窗 {stamps[0]} .. {stamps[-1]}")
        print(f"    access caller 集合：")
        for k, v in sorted(callers.items(), key=lambda kv: -kv[1]):
            print(f"        {k} × {v}")
        print(f"    access route 集合：{routes}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
