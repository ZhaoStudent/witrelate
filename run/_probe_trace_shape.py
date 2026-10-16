"""临时探针：把一次真实研判的任务库结构完整打出来（只读）。

目的：为 tools/trace.py 确认三件事，全部照实、不推断：
  ① 一跳两端靠哪个键配对（task_id / session_id 各自能跨几层）
  ② products 里到底有哪些 id（决定 verdict 字段 → 产物 的映射）
  ③ 一次真实 run 里各角色的 session 与时间窗关系
"""
from __future__ import annotations

import json
import sqlite3
import sys

ROLES = ("leader", "fact-judge", "debate-pro", "debate-con",
         "evidence-leader", "evidence-image", "evidence-audio", "evidence-video")


def dump(role: str, *, limit: int, since: str = "") -> list[dict]:
    path = f".wit/{role}/acps/aip-tasks.sqlite3"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"  （打不开 {role}：{exc}）")
        return []
    con.row_factory = sqlite3.Row
    sql = ("SELECT task_ref,direction,session_id,task_id,local_aic,remote_aic,"
           "state,created_at,updated_at,products FROM aip_tasks ")
    args: tuple = ()
    if since:
        sql += "WHERE created_at >= ? "
        args = (since,)
    sql += "ORDER BY created_at DESC LIMIT ?"
    args = args + (limit,)
    rows = []
    for r in con.execute(sql, args):
        prods = r["products"] or "[]"
        try:
            pj = json.loads(prods)
            ids = [x.get("id") for x in pj] if isinstance(pj, list) else ["<非数组>"]
        except (ValueError, TypeError):
            ids = ["<解析失败>"]
        rec = {
            "role": role, "task_ref": r["task_ref"], "direction": r["direction"],
            "session_id": r["session_id"], "task_id": r["task_id"],
            "local_aic": r["local_aic"], "remote_aic": r["remote_aic"],
            "state": r["state"], "created_at": r["created_at"],
            "updated_at": r["updated_at"], "product_ids": ids,
        }
        rows.append(rec)
        print(f"  {rec['task_ref']:<26} {rec['direction']:<8} "
              f"sess=…{rec['session_id'][-12:]:<14} "
              f"tid=…{rec['task_id'][-14:]:<16} "
              f"→…{rec['remote_aic'][-14:]:<16} {str(rec['state']):<22} "
              f"{rec['created_at'][:19]}")
        print(f"      产物 ids={ids}")
    con.close()
    return rows


def watch(since: str) -> None:
    for role in ROLES:
        print(f"===== {role} (>= {since}) =====")
        dump(role, limit=12, since=since)


def pairs() -> None:
    """全库统计：task_id 能在哪两方之间配上对（一跳两端）。"""
    idx: dict[str, list[tuple[str, str, str]]] = {}
    for role in ROLES:
        path = f".wit/{role}/acps/aip-tasks.sqlite3"
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        con.row_factory = sqlite3.Row
        for r in con.execute("SELECT task_id,direction,created_at FROM aip_tasks"):
            idx.setdefault(r["task_id"], []).append(
                (role, r["direction"], r["created_at"]))
        con.close()
    multi = {k: v for k, v in idx.items() if len({x[0] for x in v}) > 1}
    print(f"task_id 总数 {len(idx)}；**跨角色出现**的 {len(multi)} 个")
    for k, v in sorted(multi.items(), key=lambda kv: -len(kv[1]))[:8]:
        print(f"  …{k[-14:]}  " + "  ".join(f"{r}/{d}" for r, d, _ in v))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "pairs"
    if mode == "pairs":
        pairs()
    elif mode == "watch":
        watch(sys.argv[2] if len(sys.argv) > 2 else "")
