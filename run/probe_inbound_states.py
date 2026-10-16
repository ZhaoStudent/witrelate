"""只读测量：入站任务的终态分布 —— 「被访问次数」该不该把 canceled 算进去？

起因
════
2026-10-05 追查一处跨源矛盾时发现：任务库里**唯一一次**「外部真实访问」
（debate-pro ← 外部 AIC）的 `state` 是 **canceled**。
而 `tools/metrics.py` 的指标 4 目前数的是「所有 direction=inbound 的任务」，
**不看 state** ⇒ 它会把一次被取消的调用算成一次"平台访问"。

这正是"最敏感的数字宁可少报不可多报"要防的那类事。但**要不要排除 canceled
不能凭这一条样本下结论**：如果 canceled 在自家自测流量里也大量存在（比如
超时取消是常态），那"一律排除"又会走到另一个极端 —— 把真实受理的也算没了。

所以这里只做测量，把**每个角色入站任务的终态分布**打出来，让分布自己说话：
  · canceled 是只在外部那一条上出现，还是普遍存在？
  · 各终态的时间窗（canceled 是不是集中在某个早期时段）
"""

from __future__ import annotations

import glob
import os
import sqlite3
import sys
from collections import Counter

WIT = sys.argv[1] if len(sys.argv) > 1 else ".wit"


def main() -> int:
    total: Counter[str] = Counter()
    print(f"{'角色':<18}{'方向':<10}{'终态':<12}{'条数':>6}  {'最早':<32}{'最晚'}")
    print("-" * 100)
    for db in sorted(glob.glob(os.path.join(WIT, "*", "acps", "aip-tasks.sqlite3"))):
        role = os.path.basename(os.path.dirname(os.path.dirname(db)))
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT direction, COALESCE(state,'(NULL)') AS st, "
                "MIN(created_at), MAX(created_at), COUNT(*) "
                "FROM aip_tasks GROUP BY direction, st ORDER BY direction, st"
            ).fetchall()
        finally:
            con.close()
        for direction, st, t0, t1, n in rows:
            if direction != "inbound":
                continue
            total[st] += n
            print(f"{role:<18}{direction:<10}{st:<12}{n:>6}  "
                  f"{str(t0)[:30]:<32}{str(t1)[:30]}")
    print("-" * 100)
    print("入站任务终态合计：")
    for st, n in total.most_common():
        print(f"    {st:<12}{n:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
