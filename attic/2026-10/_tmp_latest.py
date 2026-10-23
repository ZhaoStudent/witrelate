import sqlite3

con = sqlite3.connect("file:.wit/leader/acps/aip-tasks.sqlite3?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute(
    "select task_ref,direction,session_id,remote_aic,state,created_at,updated_at "
    "from aip_tasks order by rowid desc limit 10"
).fetchall()
for r in rows:
    print(dict(r))
