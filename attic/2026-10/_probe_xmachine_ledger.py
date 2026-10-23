"""#125 跨端判据 2+3 取证（一次性）。

判据 2（第一台跑）：evidence-leader 任务库 outbound 行 endpoint_url 指向第二台 IP。
判据 3（第二台跑）：evidence-image 任务库 inbound 行 agent_state 含
                    debate_chain=[leader_AIC, evidence-leader_AIC]（#123 跨机终极实证）。

退出码：0=判据成立（正向判据，全部现场派生，不写死 AIC/IP 之外的任何值）；
        1=判据不成立；2=无法判定（表/列不存在、无行——「没查到」不许当通过）。

🔴 口径修正（首跑教训，2026-10-15）：判据 3 首版按任意列扫、只取第一个命中列、
   还把样例截断到 400 字符 ⇒ 自我截断误报红（command_history 命中但链在更深处、
   真正的口径是 agent_state 列）。本版只认 aip_tasks.agent_state、全文不截断。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

MODE = sys.argv[1] if len(sys.argv) > 1 else ""
ROLE = sys.argv[2] if len(sys.argv) > 2 else "evidence-image"  # inbound 模式可指定角色
WIT = Path(__file__).resolve().parent / ".wit"
REMOTE2_IP = "101.33.124.181"


def _conn(role: str) -> sqlite3.Connection:
    db = WIT / role / "acps" / "aip-tasks.sqlite3"
    if not db.exists():
        print(f"[无法判定] 任务库不存在: {db}")
        sys.exit(2)
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def probe_outbound() -> int:
    """判据 2：第一台 evidence-leader 的 outbound 委派打向第二台。"""
    con = _conn("evidence-leader")
    rows = con.execute("SELECT direction, endpoint_url FROM aip_tasks").fetchall()
    print(f"aip_tasks 共 {len(rows)} 行")
    if not rows:
        print("[无法判定] 0 行")
        return 2
    out_rows = [(d, e) for d, e in rows if e and REMOTE2_IP in str(e)]
    print(f"其中 endpoint 含 {REMOTE2_IP} 的 = {len(out_rows)}")
    for d, e in out_rows[-3:]:
        print(f"  direction={d} ep={e}")
    ok = len(out_rows) > 0
    print("判据2（outbound 指向第二台）:", "✅" if ok else "❌")
    return 0 if ok else 1


def _extract_chain(text: str) -> list | None:
    """从 agent_state 全文里解析 debate_chain 数组（不截断）。"""
    try:
        blob = json.loads(text)
    except Exception:  # noqa: BLE001
        blob = None
    if isinstance(blob, dict):
        chain = blob.get("debate_chain")
        if isinstance(chain, list):
            return chain
        st = blob.get("agent_state")
        if isinstance(st, str):
            try:
                chain = json.loads(st).get("debate_chain")
                if isinstance(chain, list):
                    return chain
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(st, dict) and isinstance(st.get("debate_chain"), list):
            return st["debate_chain"]
    # 全文级兜底：找 "debate_chain" 键后的数组字面量
    idx = text.find('"debate_chain"')
    if idx >= 0:
        seg = text[idx: idx + 4000]
        lb, rb = seg.find("["), seg.find("]")
        if lb >= 0 and rb > lb:
            try:
                chain = json.loads(seg[lb: rb + 1])
                if isinstance(chain, list):
                    return chain
            except Exception:  # noqa: BLE001
                pass
    return None


def probe_inbound_chain() -> int:
    """判据 3：第二台 evidence-image 的 inbound 行 agent_state 含跨机 debate_chain。"""
    con = _conn(ROLE)
    rows = con.execute(
        "SELECT direction, remote_aic, endpoint_url, agent_state FROM aip_tasks"
    ).fetchall()
    print(f"aip_tasks 共 {len(rows)} 行")
    if not rows:
        print("[无法判定] 0 行——没有入站记录，不许当通过")
        return 2
    ok = False
    for direction, remote_aic, endpoint, state in rows:
        print(f"-- direction={direction} remote={remote_aic} ep={endpoint}")
        if not state:
            print("   agent_state 为空")
            continue
        chain = _extract_chain(str(state))
        if isinstance(chain, list):
            aic_like = [x for x in chain if isinstance(x, str) and x.count(".") >= 4]
            print(f"   debate_chain = {chain}")
            print(f"   形如 AIC 的元素 = {len(aic_like)}/{len(chain)}")
            if len(chain) >= 2 and len(aic_like) == len(chain):
                ok = True
        else:
            print("   本行未解析出 debate_chain 数组")
    print("判据3（inbound agent_state 含跨机链≥2 且均为 AIC）:", "✅" if ok else "❌")
    return 0 if ok else 1


if MODE == "outbound":
    sys.exit(probe_outbound())
if MODE == "inbound":
    sys.exit(probe_inbound_chain())
print("用法: _probe_xmachine_ledger.py outbound|inbound [role]")
sys.exit(2)
