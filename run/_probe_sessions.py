"""临时探针：leader 库的方向分布 / 会话表 / 有没有 inbound 行（只读）。

这决定 sidecar 能否从「平台自己写的行」里取 session_id —— 而不是靠时间猜。
"""
from __future__ import annotations

import sqlite3

for role in ("leader", "fact-judge", "debate-pro", "debate-con", "evidence-video"):
    path = f".wit/{role}/acps/aip-tasks.sqlite3"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"### {role}: 打不开 {exc}")
        continue
    con.row_factory = sqlite3.Row
    print(f"### {role}")
    for r in con.execute("SELECT direction, COUNT(*) n, MIN(created_at) t0, "
                         "MAX(created_at) t1 FROM aip_tasks GROUP BY direction"):
        print(f"    {r['direction']:<9} n={r['n']:<4} {r['t0'][:19]} .. {r['t1'][:19]}")
    try:
        rows = list(con.execute("SELECT * FROM aip_sessions ORDER BY created_at DESC LIMIT 3"))
        print(f"    aip_sessions 共 "
              f"{con.execute('SELECT COUNT(*) FROM aip_sessions').fetchone()[0]} 行；最近 3：")
        for r in rows:
            d = dict(r)
            print(f"      {d.get('session_id','')[-16:]}  owner=…{str(d.get('owner_aic'))[-14:]}"
                  f"  {str(d.get('created_at'))[:19]} .. {str(d.get('updated_at'))[:19]}")
    except sqlite3.Error as exc:
        print("    aip_sessions 读不到：", exc)
    con.close()
