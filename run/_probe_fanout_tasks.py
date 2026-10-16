"""临时探针：验证裁判并发扇出 —— 两个委派任务的创建时间差。"""
import sqlite3

con = sqlite3.connect(".wit/fact-judge/acps/aip-tasks.sqlite3")
con.row_factory = sqlite3.Row
cols = [r[1] for r in con.execute("PRAGMA table_info(aip_tasks)")]
print("aip_tasks 列：", cols)
rows = list(con.execute(
    "SELECT * FROM aip_tasks ORDER BY rowid DESC LIMIT 6"
))
for r in rows:
    d = dict(r)
    # 只打关键列，避免刷屏
    keys = [k for k in ("id", "task_id", "created_at", "updated_at",
                        "direction", "status", "callee_aic", "callee_name",
                        "partner_name", "payload", "request") if k in d]
    slim = {}
    for k in keys:
        v = d[k]
        if isinstance(v, str) and len(v) > 60:
            v = v[:60] + "…"
        slim[k] = v
    print(slim)
