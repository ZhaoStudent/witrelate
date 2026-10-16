"""临时探针：打印一次真实 run 里每个任务的 products 原文（只读）。

目的：确定 verdict 的字段到底能从哪份产物里查到 —— 不许编映射。
"""
from __future__ import annotations

import json
import sqlite3
import sys

ROLES = ("leader", "fact-judge", "debate-pro", "debate-con")


def brief(obj, depth: int = 0):
    """把 JSON 压成一行摘要，保留键名与短值。"""
    if isinstance(obj, dict):
        return {k: brief(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) <= 3:
            return [brief(x, depth + 1) for x in obj]
        return [brief(obj[0], depth + 1), f"…共{len(obj)}项"]
    if isinstance(obj, str) and len(obj) > 90:
        return obj[:90] + "…"
    return obj


def dump(role: str, sess_tail: str) -> None:
    path = f".wit/{role}/acps/aip-tasks.sqlite3"
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    print("=" * 22, role)
    for r in con.execute(
        "SELECT task_ref,direction,session_id,task_id,state,products,"
        "last_result,local_error FROM aip_tasks WHERE session_id LIKE ? "
        "ORDER BY created_at", (f"%{sess_tail}%",)
    ):
        print(f"  {r['task_ref']}  {r['direction']}  state={r['state']}  "
              f"tid=…{r['task_id'][-14:]}")
        if r["local_error"]:
            print(f"      local_error={str(r['local_error'])[:150]}")
        if r["last_result"]:
            print(f"      last_result={str(r['last_result'])[:150]}")
        try:
            pj = json.loads(r["products"])
        except (ValueError, TypeError):
            print("      products=<解析失败>")
            continue
        if not pj:
            print("      products=[]")
        for item in pj:
            print("      ── 产物", json.dumps(brief(item), ensure_ascii=False)[:900])
    con.close()


if __name__ == "__main__":
    sess = sys.argv[1] if len(sys.argv) > 1 else "c285c8624158"
    for role in ROLES:
        try:
            dump(role, sess)
        except sqlite3.Error as exc:
            print(role, "打不开", exc)
