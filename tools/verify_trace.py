"""离线断言：结论溯源表（`tools/trace.py` + `tools/aip_sessions.py`）。

造**假任务库**跑，不碰真实数据、不连网。每条断言都落在**交付出去的值**上。

本文件重点守的几件事（都是实测踩过的）
════════════════════════════════════════
1. 🔴 **定局不许认错会话**。踩过：把所有角色的会话混在一起找，
   `fact-judge` 的**内层**会话窗口也罩住同一时刻 ⇒ 认领到内层 session，
   整条链凭空少一层，而输出**照样像模像样**。用例 B 把这条钉死。
2. 🔴 **假值不许被抹掉**。`_preview(False)` / `_preview(0)` 曾经因为写成
   `text or ""` 而返回空串 —— `is_incitement=False` 与 `harm_index=0`
   会显示成空白，读起来像"这一项没有值"。用例 D 把这条钉死。
3. 🔴 **gaps 非空必须渲染成灰色行**，不许留空（留空会被读成"没问题"）。用例 F。
4. 🔴 **请求原文只取正文**（`request_excerpt` 曾把 `{"type":"text"}` 里的
   字面 `text` 也拼进去 —— 那行是假证据）。用例 I。
5. 🔴 **一跳不许被造两遍**、**不许出现"自己所属会话是自己的子会话"这种自指边**，
   且两个父跳争同一个子会话时必须标 `ambiguous` + 告警。用例 J。
6. 🔴 **不许出现假硬溯源**：字段名不许靠**子串**命中（`band` ← `abandon`）、
   不许命中产物的**外壳**（`name` / `id`）。用例 K。
7. 🔴 **某个角色的库读不出来 ⇒ 必须告警点名**，不许静默少一层、更不许把原因
   说成"配不到对端的入站行"。用例 L。
8. 🔴 **sidecar 取会话不许静默放宽**（owner 未知 / 两局窗口重叠 ⇒ 拒绝认领）。用例 M。
9. 🔴 **渲染层不许过度声称**：`硬` 必须在**三种渲染里都写明**它只表示"字段名出现在
   下游返回的产物正文里"，**不是平台级认证**，并且必须把命中片段摆出来供复核。
   踩过：`_field_span` 的 docstring 早就写了这条要求，渲染层却一直没做 ——
   **"说了但没做"比不说更危险**，因为代码里留着"已经处理过"的印象。用例 N。
10. 🔴 **HTML 里不许混入 markdown 标记**（`**`、反引号）：它们会被浏览器**原样显示**。
    踩过：gaps 提示与页脚各留了一处 `**…**`，证据抽屉里留了一处反引号。用例 N。
11. 🔴 **Markdown 表格单元格必须转义竖线**：值里一个 `|` 就把这一行拆成两行。
    ⚠️ 判据要按**渲染器的口径**（只按未转义的 `|` 切分）算，不能数"字符串有几行"
    —— 未转义时字符串同样只有一行，那种写法是一条恒为真的假断言。用例 N。
12. 🔴 **取最早/最晚时刻必须按时间比，不许按字典序比**（`sent_at` 是字符串）。
    平台眼下统一写 `+00:00` 所以字典序恰好对得上，那只是**巧合**。用例 N 第 ⑦ 条。
13. 🔴 **越界值必须夹住并且说出来**：`harm_index` 超出 -100..100 时 pin 不许画到色条
    外面，也不许静默夹取。用例 N。

用法：
    ./.venv/bin/python -m tools.verify_trace        # 退出码 0 = 全绿
"""

from __future__ import annotations

import contextlib
import hashlib
import html as _html
import io
import json
import re as _re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.aip_sessions import (
    SESSIONS_DDL,
    SessionRef,
    pick_live_session,
    read_sessions,
    role_db_path,
    sessions_covering,
)
from tools.trace import (
    _md_cell,
    _preview,
    _read_tasks,
    _request_excerpt,
    _text_payload,
    build_trace,
    main,
    render_html,
    render_json,
    render_markdown,
    render_text,
)
from tools.verify_metrics import _DDL

# ── 固定事实 ────────────────────────────────────────────────────────────────

BASE = datetime(2026, 10, 3, 8, 0, 0, tzinfo=timezone.utc)

LEADER = "1.2.156.3088.1.BUPT.81M3FX.AAAAAAAA.LEADR"
INCITEMENT = "1.2.156.3088.1.BUPT.81M3FX.BBBBBBBB.INCIT"
JUDGE = "1.2.156.3088.1.BUPT.81M3FX.CCCCCCCC.JUDGE"
PRO = "1.2.156.3088.1.BUPT.81M3FX.DDDDDDDD.PROXX"
CON = "1.2.156.3088.1.BUPT.81M3FX.EEEEEEEE.CONXX"

OUTER = "session-outer-0000-0000-0000-aaaaaaaaaaaa"
INNER = "session-inner-0000-0000-0000-bbbbbbbbbbbb"

TID1, TID2, TID3, TID4 = "task-id-1", "task-id-2", "task-id-3", "task-id-4"
VERDICT_STAMP = "20261003T080100Z"  # 08:01:00Z —— **同时落在外层与内层窗口内**

_FAILS: list[str] = []
_PASSES = 0


def check(label: str, got, want, note: str = "") -> None:
    global _PASSES
    if got == want:
        _PASSES += 1
        print(f"  [通过] {label}")
    else:
        _FAILS.append(label)
        print(f"  [失败] {label}\n         实得 = {got!r}\n         期望 = {want!r}")
        if note:
            print(f"         注：{note}")


def check_true(label: str, cond: bool, note: str = "") -> None:
    check(label, bool(cond), True, note)


def section(title: str) -> None:
    print()
    print("─" * 78)
    print(f"  {title}")
    print("─" * 78)


# ── 造假库 ──────────────────────────────────────────────────────────────────


def ts(sec: float) -> str:
    return (BASE + timedelta(seconds=sec)).isoformat()


def cmd(name: str, sent_sec: float, *, text: str | None = None) -> dict:
    d = {
        "type": "task-command",
        "id": f"command-{name}-{sent_sec}",
        "sentAt": ts(sent_sec),
        "senderRole": "leader",
        "command": name,
    }
    if text is None:
        d["commandParams"] = {"lastCommandSentAt": None}
    else:
        d["dataItems"] = [{"type": "text", "text": text}]
    return d


def make_db(
    root: str,
    role: str,
    *,
    tasks: list[dict],
    sessions: list[dict] | None = None,
) -> str:
    """按真实 schema 造一个角色的任务库（+ 可选会话表）。

    时间参数既可以是相对 `BASE` 的秒数（常规用法），也可以是**现成的 ISO 字符串**
    —— 后者用来构造"偏移形态不一致"的样本（见用例 N 第 ⑦ 条）。
    """
    def _t(v):
        return v if isinstance(v, str) else ts(v)

    d = Path(root) / role
    (d / "acps").mkdir(parents=True, exist_ok=True)
    db = d / "acps" / "aip-tasks.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()

    con = sqlite3.connect(str(db))
    con.executescript(_DDL)
    con.executescript(SESSIONS_DDL)
    for t in tasks:
        con.execute(
            "INSERT INTO aip_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t["task_ref"], t["direction"], t["session_id"], t["task_id"],
                t["local_aic"], t["remote_aic"], None, "rpc", t.get("state"),
                None, t.get("local_error"),
                json.dumps(t.get("commands", [])),
                json.dumps(t.get("products", [])),
                json.dumps([]), json.dumps({"messages": [], "metadata": {}}),
                _t(t["created"]), _t(t["updated"]),
            ),
        )
        for seq, (etype, at) in enumerate(t.get("events", []), start=1):
            con.execute(
                "INSERT INTO aip_task_events VALUES (?,?,?,?,?,?)",
                (t["task_ref"], seq, f"msg-{t['task_ref']}-{seq}", etype,
                 json.dumps({"type": etype}), _t(at)),
            )
    for sid, owner, c0, c1 in sessions or []:
        con.execute("INSERT INTO aip_sessions VALUES (?,?,?,?)",
                    (sid, owner, _t(c0), _t(c1)))
    con.commit()
    con.close()
    return str(db)


def _text_product(pid: str, text: str) -> dict:
    return {"id": pid, "name": "Agent response",
            "dataItems": [{"type": "text", "text": text}]}


def build_fixture(root: str, *, verdict=True, sidecar=True,
                  judge_inner_sessions=(("session-inner-0000-0000-0000-bbbbbbbbbbbb", 40, 80),),
                  extra_judge_outbound=0, drop_products=False,
                  unmatched_hop=False) -> str:
    """造一套标准场景。返回 fake `wit` 目录（`<root>/.wit`）。"""
    wit = str(Path(root) / ".wit")

    def prods(x):
        return [] if drop_products else x

    make_db(wit, "leader", tasks=[
        {   # ① 主控 → 煽动识别（配对得上）
            "task_ref": "task_L1", "direction": "outbound", "session_id": OUTER,
            "task_id": TID1, "local_aic": LEADER, "remote_aic": INCITEMENT,
            "state": "awaiting-completion", "created": 0, "updated": 30,
            "commands": [cmd("start", 0, text="请判定这段文本是否构成煽动性言论。"),
                         cmd("get", 10)],
            "events": [("task-result", 30)],
            "products": prods([_text_product(
                "product-inc", "两项结论：is_incitement = false；truth_score = 50")]),
        },
        {   # ② 主控 → 网络裁判（配对得上；它是内层链的父）
            "task_ref": "task_L2", "direction": "outbound", "session_id": OUTER,
            "task_id": TID2, "local_aic": LEADER, "remote_aic": JUDGE,
            "state": "awaiting-completion", "created": 0, "updated": 120,
            "commands": [cmd("start", 0, text="受理事实核查并组织正反辩论。")],
            "events": [("task-result", 120)],
            "products": prods([_text_product("product-ruling", "裁定：无法判定")]),
        },
    ], sessions=[(OUTER, LEADER, -5, 125)])

    make_db(wit, "incitement", tasks=[
        {   # 对端入站行：task_id 与 L1 一致 ⇒ 硬配对
            "task_ref": "task_I1", "direction": "inbound", "session_id": OUTER,
            "task_id": TID1, "local_aic": INCITEMENT, "remote_aic": LEADER,
            "state": "awaiting-completion", "created": 0, "updated": 30,
            "commands": [], "events": [("task-result", 30)], "products": [],
        },
    ])

    judge_out = []
    for i, (sess, c0, c1) in enumerate(judge_inner_sessions):
        judge_out.append({
            "task_ref": f"task_J{i}_pro", "direction": "outbound", "session_id": sess,
            "task_id": TID3, "local_aic": JUDGE, "remote_aic": PRO,
            "state": "awaiting-completion", "created": c0, "updated": c1,
            "commands": [cmd("start", c0, text="为正方立场构建论证。")],
            "events": [("task-result", c1)],
            "products": prods([_text_product(f"product-pro-{i}", "正方论证正文")]),
        })
        judge_out.append({
            "task_ref": f"task_J{i}_con", "direction": "outbound", "session_id": sess,
            "task_id": TID4, "local_aic": JUDGE, "remote_aic": CON,
            "state": "awaiting-completion", "created": c0 + 5, "updated": c1,
            "commands": [cmd("start", c0 + 5, text="为反方立场构建反驳。")],
            "events": [("task-result", c1)],
            "products": prods([_text_product(f"product-con-{i}", "反方论证正文")]),
        })
    for i in range(extra_judge_outbound):
        judge_out.append({
            "task_ref": f"task_JX{i}", "direction": "outbound", "session_id": INNER,
            "task_id": f"task-id-x{i}", "local_aic": JUDGE, "remote_aic": PRO,
            "state": "awaiting-completion", "created": 50, "updated": 60,
            "commands": [cmd("start", 50, text="额外一趟。")],
            "events": [("task-result", 60)], "products": prods([]),
        })
    if unmatched_hop:
        judge_out.append({
            "task_ref": "task_LONE", "direction": "outbound", "session_id": OUTER,
            "task_id": "task-id-nobody", "local_aic": LEADER, "remote_aic": CON,
            "state": "failed", "created": 0, "updated": 20,
            "commands": [cmd("start", 0, text="这趟对端没有落库。")],
            "events": [("task-result", 20)], "products": [],
        })

    make_db(wit, "fact-judge", tasks=[
        {   # 内层父的入站行（与 L2 的 task_id 一致）
            "task_ref": "task_J0", "direction": "inbound", "session_id": OUTER,
            "task_id": TID2, "local_aic": JUDGE, "remote_aic": LEADER,
            "state": "awaiting-completion", "created": 0, "updated": 120,
            "commands": [], "events": [("task-result", 120)], "products": [],
        },
        *judge_out,
    ], sessions=[(s, JUDGE, c0, c1) for (s, c0, c1) in judge_inner_sessions])

    make_db(wit, "debate-pro", tasks=[
        {"task_ref": "task_P1", "direction": "inbound", "session_id": INNER,
         "task_id": TID3, "local_aic": PRO, "remote_aic": JUDGE,
         "state": "awaiting-completion", "created": 40, "updated": 70,
         "commands": [], "events": [("task-result", 70)], "products": []},
    ])
    make_db(wit, "debate-con", tasks=[
        {"task_ref": "task_C1", "direction": "inbound", "session_id": INNER,
         "task_id": TID4, "local_aic": CON, "remote_aic": JUDGE,
         "state": "awaiting-completion", "created": 45, "updated": 80,
         "commands": [], "events": [("task-result", 80)], "products": []},
    ])

    out = Path(root) / "output"
    out.mkdir(parents=True, exist_ok=True)
    if verdict:
        (out / f"verdict-{VERDICT_STAMP}.json").write_text(json.dumps({
            "subject": "某校食堂传闻（离线样本）",
            "harm_index": -25, "band": "正当批评",
            "is_incitement": False, "truth_score": 50,
            "conclusion": "属转述性表述。", "action_advice": "核查来源。",
            "disclaimer": "AI 辅助生成。",
        }, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    if verdict and sidecar:
        (out / f"verdict-{VERDICT_STAMP}.meta.json").write_text(json.dumps({
            "session_id": OUTER, "role": "leader", "aic": LEADER,
            "started_at": ts(-5), "finished_at": ts(125),
        }, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return wit


def tree_digest(root: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            out[str(p)] = hashlib.md5(p.read_bytes()).hexdigest()
    return out


# ── 用例 ────────────────────────────────────────────────────────────────────


def case_a_normal(tmp: str) -> None:
    section("用例 A：正常一局（有 sidecar）——链路 / 配对 / 归因 / 跨层标注")
    wit = build_fixture(tmp)

    # —— 回归：`verdict-*.json` 的 glob 会把 sidecar 一起捞进来（它也以 .json 结尾）——
    from tools.trace import _verdict_files
    vfiles = _verdict_files(wit)
    check("verdict 清单只含 verdict 本体（排掉了 *.meta.json）", len(vfiles), 1)
    check_true("清单里没有 sidecar",
               all(not f.endswith(".meta.json") for f in vfiles))

    rep = build_trace(wit)
    check_true("能定出这一局", rep is not None)
    assert rep is not None

    check("定局走 sidecar", rep.run.resolution, "sidecar")
    check("局 = 外层 session", rep.run.session_id, OUTER)
    check("参与跳数 = 4（2 条 depth0 + 2 条 depth1）", len(rep.hops), 4)
    check("depth0 跳数", len([h for h in rep.hops if h.depth == 0]), 2)
    check("depth1 跳数", len([h for h in rep.hops if h.depth == 1]), 2)
    check("全部完成硬配对（task_id 一跳两端一致）",
          sorted({h.pairing for h in rep.hops}), ["hard"])
    recv = {h.task_ref: h.to_role for h in rep.hops}
    check("L1 的接收方识别为 incitement", recv.get("task_L1"), "incitement")
    check("L2 的接收方识别为 fact-judge", recv.get("task_L2"), "fact-judge")
    check("内层跳的发起方识别为 fact-judge",
          sorted({h.from_role for h in rep.hops if h.depth == 1}), ["fact-judge"])
    check("跨层归属只有 1 条", len(rep.chain_edges), 1)
    edge = rep.chain_edges[0]
    check("跨层边指向内层会话", edge.to_session_id, INNER)
    check("跨层边**永远是推断**（实测无协议级硬键）", edge.evidence, "inferred")
    check_true("跨层边带 why（不许有无 why 的 inferred）", bool(edge.why.strip()))
    check("跨层边不歧义", edge.ambiguous, False)

    hard = {f.field: f for f in rep.verdict_fields if f.evidence == "hard"}
    check("硬溯源字段集合", sorted(hard), ["is_incitement", "truth_score"])
    check("is_incitement 挂到煽动识别那跳",
          hard["is_incitement"].source["task_ref"], "task_L1")
    check("is_incitement 挂到煽动识别的产物",
          hard["is_incitement"].source["product_id"], "product-inc")
    check("harm_index 找不到下游来源 ⇒ 推导",
          next(f for f in rep.verdict_fields if f.field == "harm_index").evidence,
          "derived")
    check("gaps 覆盖全部推导字段",
          sorted(g["field"] for g in rep.gaps),
          ["action_advice", "band", "conclusion", "disclaimer", "harm_index", "subject"])
    check_true("gaps 每项都带原因", all(g.get("reason") for g in rep.gaps))

    d = json.loads(render_json(rep))
    check("JSON schema 版本", d["schema"], "legal-acps/trace@1")
    check_true("JSON 里每跳都有 task_ref 与 session_id",
               all(h.get("task_ref") and h.get("session_id") for h in d["hops"]))
    check_true("数据来源列出了任务库文件",
               any("aip-tasks.sqlite3" in f for f in d["source_files"]))
    check_true("文本渲染含「复算」指引", "复算" in render_text(rep))


def case_b_no_sidecar(tmp: str) -> None:
    section("用例 B：🔴 缺 sidecar —— 必须认领**外层**会话，不许认成内层")
    wit = build_fixture(tmp, sidecar=False)

    # 先证明这条断言"会咬"：内层窗口**确实**也罩住 verdict 时刻。
    judge_sessions = read_sessions(role_db_path(wit, "fact-judge"))
    stamp = datetime.strptime(VERDICT_STAMP, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    inner_hit = [s.session_id for s in sessions_covering(judge_sessions, stamp, tol_s=0.0)]
    check("前提：内层会话窗口也覆盖 verdict 时刻（所以混着找必然认错）",
          inner_hit, [INNER])

    rep = build_trace(wit)
    assert rep is not None
    check("缺 sidecar 时降级为推断", rep.run.resolution, "inferred")
    check("🔴 认领到的是**外层/主控**会话", rep.run.session_id, OUTER)
    check_true("降级必须打 WARNING", any("sidecar" in w for w in rep.warnings))
    check_true("WARNING 里写明「靠推断、请核对」",
               any("推断" in w and "核对" in w for w in rep.warnings))
    check("降级后链路仍然是 4 跳（没有因为认错会话少一层）", len(rep.hops), 4)


def case_c_ambiguous(tmp: str) -> None:
    section("用例 C：内层有两个候选会话 ⇒ 必须标 ambiguous 并进 warnings")
    wit = build_fixture(tmp, judge_inner_sessions=(
        ("session-inner-0000-0000-0000-bbbbbbbbbbbb", 40, 80),
        ("session-inner-0000-0000-0000-cccccccccccc", 45, 75),
    ))
    rep = build_trace(wit)
    assert rep is not None
    check("两个候选会话各生成一条跨层边", len(rep.chain_edges), 2)
    check_true("两条边都标 ambiguous", all(e.ambiguous for e in rep.chain_edges))
    check_true("warnings 里点名「候选会话」",
               any("候选会话" in w for w in rep.warnings))
    check_true("两条边都带 why", all(e.why.strip() for e in rep.chain_edges))


def case_d_no_products(tmp: str) -> None:
    section("用例 D：🔴 全无产物 → 全部推导；假值不许被抹掉")
    wit = build_fixture(tmp, drop_products=True)
    rep = build_trace(wit)
    assert rep is not None
    check_true("没有任何字段能挂到下游产物",
               all(f.evidence == "derived" for f in rep.verdict_fields))
    check("gaps 覆盖全部字段", len(rep.gaps), len(rep.verdict_fields))
    check_true("gaps 原因写明「没有任何下游产物返回」",
               all("没有任何" in g["reason"] for g in rep.gaps))

    # —— 回归：假值不许被 `text or ""` 吃掉 ——
    check("_preview(False) 必须是 'False' 而不是空串", _preview(False), "False")
    check("_preview(0) 必须是 '0' 而不是空串", _preview(0), "0")
    check("_preview(None) 才是空串", _preview(None), "")
    fields = {f.field: f.value_preview for f in rep.verdict_fields}
    check("verdict 里的 False 如实显示", fields.get("is_incitement"), "False")
    check("verdict 里的 0/负数如实显示", fields.get("harm_index"), "-25")


def case_e_unmatched(tmp: str) -> None:
    section("用例 E：配不上对端入站行 ⇒ 如实标 unmatched + 警告")
    wit = build_fixture(tmp, unmatched_hop=True)
    rep = build_trace(wit)
    assert rep is not None
    lone = [h for h in rep.hops if h.task_ref == "task_LONE"]
    check("能重建出这条跳", len(lone), 1)
    check("它被标为 unmatched", lone[0].pairing, "unmatched")
    check_true("warnings 点名「配不到对端」",
               any("配不到对端" in w for w in rep.warnings))
    check_true("它的接收方角色仍按 AIC 反查出来（标注为推断的口径写在 warnings）",
               lone[0].to_role == "debate-con")


def case_f_html(tmp: str) -> None:
    section("用例 F：HTML 自包含 + 🔴 gaps 必须渲染成灰色行")
    wit = build_fixture(tmp)
    rep = build_trace(wit)
    assert rep is not None
    page = render_html(rep)
    check_true("是完整 HTML 文档", page.lstrip().startswith("<!DOCTYPE html>"))
    check("零外部引用（自包含）",
          ("<script" not in page, "http://" not in page, "https://" not in page),
          (True, True, True))
    check_true("gaps 字段渲染成灰色行（class='gap'）", "class='gap'" in page)
    check_true("每个 gap 字段名都出现在页面上",
               all(g["field"] in page for g in rep.gaps))
    check_true("硬溯源字段渲染成 class='hard'", "class='hard'" in page)
    check_true("页面写明「不许留空」的理由", "不留空" in page)
    check_true("页面含复算指引命令", "tools.trace" in page)
    check_true("页面声明生成器不写入任务库", "不写入任何任务库" in page)


def case_g_exit_codes(tmp: str) -> None:
    section("用例 G：退出码 —— 定不出这一局必须 2（不产半成品）")
    buf = io.StringIO()
    # ⚠️ 数据目录与空目录要**互为兄弟**：`_verdict_files` 会解析 `<wit>/../output`，
    #    若把空目录放进有 output 的目录里，它就"继承"到了 verdict，用例形同虚设。
    ok_wit = build_fixture(str(Path(tmp) / "ok"))

    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code_ok = main(["--wit", ok_wit, "--json"])
    check("有数据时 exit 0", code_ok, 0)

    empty = Path(tmp) / "empty" / ".wit"
    empty.mkdir(parents=True, exist_ok=True)
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code_empty = main(["--wit", str(empty)])
    check("空目录 exit 2", code_empty, 2)

    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code_bad_sess = main(["--wit", ok_wit, "--session", "session-nope"])
    check("🔴 指定了不存在的 session ⇒ exit 2（不许给一份看似成功的空表）",
          code_bad_sess, 2)

    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code_good_sess = main(["--wit", ok_wit, "--session", OUTER, "--json"])
    check("指定真实存在的 session ⇒ exit 0", code_good_sess, 0)


def case_h_readonly(tmp: str) -> None:
    section("用例 H：只读 —— 生成溯源表不许改动任何任务库")
    wit = build_fixture(tmp)
    before = tree_digest(wit)
    for renderer in (render_text, render_markdown, render_json, render_html):
        rep = build_trace(wit)
        assert rep is not None
        renderer(rep)
    after = tree_digest(wit)
    check("任务库与 sidecar 逐字节未变", after, before)


def _one_hop_run(tmp: str, verdict: dict, product_text: str) -> "object":
    """造「一跳 + 一个产物」的最小场景，返回 `TraceReport`。

    产物的形态**照抄真实平台**：`{"id":…, "name":"Agent response", "dataItems":[…]}`。
    外壳里的 `name` 键正是"假硬溯源"的来源，所以这里必须带上它。
    """
    wit = str(Path(tmp) / ".wit")
    sid = "session-k-0000-0000-0000-00000000000k"
    make_db(wit, "leader", tasks=[
        {"task_ref": "task_K1", "direction": "outbound", "session_id": sid,
         "task_id": "task-id-k1", "local_aic": LEADER, "remote_aic": JUDGE,
         "state": "awaiting-completion", "created": 0, "updated": 60,
         "commands": [cmd("start", 0, text="请给出结论。")],
         "events": [("task-result", 60)],
         "products": [_text_product("product-k", product_text)]},
    ], sessions=[(sid, LEADER, -5, 65)])
    out = Path(tmp) / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"verdict-{VERDICT_STAMP}.json").write_bytes(
        json.dumps(verdict, ensure_ascii=False).encode("utf-8"))
    (out / f"verdict-{VERDICT_STAMP}.meta.json").write_bytes(
        json.dumps({"session_id": sid, "role": "leader", "aic": LEADER},
                   ensure_ascii=False).encode("utf-8"))
    rep = build_trace(wit)
    assert rep is not None, "最小场景应当能定局"
    return rep


def case_i_request_excerpt(tmp: str) -> None:
    section("用例 I：🔴 请求原文只取正文（不许掺入 `{\"type\":\"text\"}` 的字面 text）")
    body = "请判定这段文本是否构成煽动性言论。"
    wit = build_fixture(tmp)
    l1 = next(t for t in _read_tasks(role_db_path(wit, "leader"))
              if t.task_ref == "task_L1")
    got = _request_excerpt(l1)
    check("请求原文与原始正文逐字相同", got, body)
    check_true("不以字面 'text' 开头（曾经的形态是 'text\\n正文'）",
               not got.startswith("text"), f"实得开头：{got[:12]!r}")
    check("只在 text 键上取值", _text_payload(
        [{"type": "text", "text": body, "note": "不该被收进来"}]), body)
    check("没有文本项时如实返回空串（不拿别的字段凑）",
          _text_payload([{"type": "text"}]), "")
    check("dataItems 不是列表时也返回空串", _text_payload("不是列表"), "")
    check("退化形态：项本身就是字符串", _text_payload([body]), body)


def _two_parent_fixture(tmp: str) -> str:
    """🔴 双父夹具：**两个**父跳的窗口都罩住**同一个**内层会话。返回 `wit` 目录。

    这个形态一个夹具管两件事：
      · 跨层「跨父争抢」⇒ 两条边都必须标 `ambiguous` + 告警（用例 J）；
      · `task_Jp` 的对端是正方，于是第 2 层会到 `debate-pro` 里找候选，而
        `task_P2` **落在 INNER 里**（== 父跳自己所在的会话）、窗口也在父跳窗口内
        —— 少了「不许把父跳自己的会话当作它的子会话」那道闸，
        `task_Jp → INNER` 这条**自指边**就会被造出来（还是用例 J）。

    ⚠️ 为什么要抽成共用函数：用例 J 与用例 N 都要用它，各写一份必然漂移
    （"同一事实散在多处 ⇒ 加角色必漏"）。
    """
    wit = str(Path(tmp) / ".wit")
    make_db(wit, "leader", tasks=[
        {"task_ref": "task_L1", "direction": "outbound", "session_id": OUTER,
         "task_id": TID1, "local_aic": LEADER, "remote_aic": JUDGE,
         "state": "awaiting-completion", "created": 0, "updated": 150,
         "commands": [cmd("start", 0, text="第一趟")],
         "events": [("task-result", 150)],
         "products": [_text_product("p-L1", "第一趟的裁定")]},
        {"task_ref": "task_L2", "direction": "outbound", "session_id": OUTER,
         "task_id": TID2, "local_aic": LEADER, "remote_aic": JUDGE,
         "state": "awaiting-completion", "created": 5, "updated": 145,
         "commands": [cmd("start", 5, text="第二趟")],
         "events": [("task-result", 145)],
         "products": [_text_product("p-L2", "第二趟的裁定")]},
    ], sessions=[(OUTER, LEADER, -5, 160)])
    make_db(wit, "fact-judge", tasks=[
        {"task_ref": "task_J2", "direction": "inbound", "session_id": OUTER,
         "task_id": TID2, "local_aic": JUDGE, "remote_aic": LEADER,
         "state": "awaiting-completion", "created": 5, "updated": 145,
         "commands": [], "events": [("task-result", 145)], "products": []},
        {"task_ref": "task_Jp", "direction": "outbound", "session_id": INNER,
         "task_id": TID3, "local_aic": JUDGE, "remote_aic": PRO,
         "state": "awaiting-completion", "created": 40, "updated": 80,
         "commands": [cmd("start", 40, text="内层一趟")],
         "events": [("task-result", 80)],
         "products": [_text_product("p-Jp", "内层产物")]},
    ], sessions=[(INNER, JUDGE, 35, 85)])
    # ⚠️ `task_P2` 不是装饰：它**落在 INNER 里**（== 父跳自己所在的会话），窗口也在
    #    父跳窗口之内 —— 少了「不许把父跳自己的会话当作它的子会话」那道闸，
    #    `task_Jp → INNER` 这条**自指边**就会被造出来。
    #    `task_P1` 是入站行（会被 `not is_inbound` 排掉），所以真正可能被误记的
    #    只有 `task_P2`。缺了它，自指边断言就是**空转**的。
    make_db(wit, "debate-pro", tasks=[
        {"task_ref": "task_P1", "direction": "inbound", "session_id": INNER,
         "task_id": TID3, "local_aic": PRO, "remote_aic": JUDGE,
         "state": "awaiting-completion", "created": 45, "updated": 75,
         "commands": [], "events": [("task-result", 75)], "products": []},
        {"task_ref": "task_P2", "direction": "outbound", "session_id": INNER,
         "task_id": "task-id-p2", "local_aic": PRO, "remote_aic": JUDGE,
         "state": "awaiting-completion", "created": 45, "updated": 75,
         "commands": [cmd("start", 45, text="被误记成内层子会话的那行")],
         "events": [("task-result", 75)], "products": [_text_product("p-P2", "正方产物")]},
    ])
    out = Path(tmp) / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"verdict-{VERDICT_STAMP}.json").write_bytes(b'{"subject":"x"}')
    (out / f"verdict-{VERDICT_STAMP}.meta.json").write_bytes(
        json.dumps({"session_id": OUTER}).encode("utf-8"))
    return wit


def case_j_no_dup_hops(tmp: str) -> None:
    section("用例 J：🔴 一跳不许造两遍；不许自指边；跨父争抢必须标 ambiguous")
    wit = _two_parent_fixture(tmp)
    rep = build_trace(wit)
    assert rep is not None
    refs = [h.task_ref for h in rep.hops]
    check("跳数 = 3（两趟父 + 一条内层，内层不重复）", len(refs), 3)
    check("没有重复的 task_ref", len(refs), len(set(refs)))
    check("层号没有虚增到 MAX_DEPTH", max(h.depth for h in rep.hops), 1)
    sess_of = {h.task_ref: h.session_id for h in rep.hops}
    selfloops = [e.from_task_ref for e in rep.chain_edges
                 if sess_of.get(e.from_task_ref) == e.to_session_id]
    check("没有'自己所属会话是自己的子会话'这种自指边", selfloops, [])
    # ⚠️ 上面那条若夹具不当就是**空转**的（恒为空）。这里正面证明夹具里**确实**
    #    存在"落在父跳自己会话内、且窗口也在父跳窗口内"的候选 —— 也就是说，
    #    少了那道闸，`task_Jp → INNER` 这条自指边**一定会**被造出来。
    inner_outs = [t for t in _read_tasks(role_db_path(wit, "debate-pro"))
                  if not t.is_inbound and t.session_id == INNER]
    check("夹具里存在'落在父跳自己会话内'的候选（否则自指边断言是空转的）",
          len(inner_outs), 1)
    check_true("该候选的窗口也落在父跳窗口 [40s, 80s] 内（所以才可能被误记）",
               all(t.created_at and t.updated_at
                   and t.created_at >= datetime.fromisoformat(ts(40))
                   and t.updated_at <= datetime.fromisoformat(ts(80))
                   for t in inner_outs))
    inner_edges = [e for e in rep.chain_edges if e.to_session_id == INNER]
    check("指向内层的边 = 2（两个父各一条）", len(inner_edges), 2)
    check_true("两条边**都**标了 ambiguous（不许一个标一个不标）",
               all(e.ambiguous for e in inner_edges))
    check_true("两条边的 why 都点名了冲突",
               all("其它" in e.why or "同时落在" in e.why for e in inner_edges))
    check_true("warnings 点名「多个父跳」",
               any("多个" in w and "父跳" in w for w in rep.warnings))


def case_k_no_false_hard(tmp: str) -> None:
    section("用例 K：🔴 不许出现假硬溯源（子串命中 / 命中产物外壳）")
    # ① 子串：`band` 不该命中 `abandon`
    rep1 = _one_hop_run(str(Path(tmp) / "a"), {"band": "正当批评"},
                        "We should abandon the claim entirely.")
    f1 = {f.field: f for f in rep1.verdict_fields}["band"]
    check("`band` 不许命中 `abandon`（子串）", f1.evidence, "derived")
    check("它落进 gaps", [g["field"] for g in rep1.gaps], ["band"])

    # ② 外壳：`name` 不该命中产物**外壳**的 "name": "Agent response"
    rep2 = _one_hop_run(str(Path(tmp) / "b"), {"name": "某校食堂传闻"},
                        "正文里完全没提到那个字段名。")
    f2 = {f.field: f for f in rep2.verdict_fields}["name"]
    check("`name` 不许命中产物外壳", f2.evidence, "derived")

    # ③ **正向判据**（必须有，否则"一律判 derived"也能骗过上面两条）：
    #    真机上的真实命中形态是 `字段名 = 值` / `**…字段名 = 值…**`
    rep3 = _one_hop_run(
        str(Path(tmp) / "c"), {"harm_index": -10, "is_incitement": False},
        "**综合指数 harm_index = -10（正当批评区间）**\n"
        "- is_incitement = false，severity = 0",
    )
    hard = sorted(f.field for f in rep3.verdict_fields if f.evidence == "hard")
    check("真命中（词边界 + 空格）仍然认得出来", hard,
          ["harm_index", "is_incitement"])
    check("真命中留有可复核的片段",
          all(f.source.get("excerpt") for f in rep3.verdict_fields), True)
    check("命中的片段里确实出现过该字段名",
          all(f.field in (f.source.get("excerpt") or "")
              for f in rep3.verdict_fields if f.evidence == "hard"), True)


def case_l_broken_db(tmp: str) -> None:
    section("用例 L：🔴 某个角色的库读不出来 ⇒ 必须告警点名，不许静默少一层")
    wit = str(Path(tmp) / ".wit")
    make_db(wit, "leader", tasks=[
        {"task_ref": "task_L1", "direction": "outbound", "session_id": OUTER,
         "task_id": TID1, "local_aic": LEADER, "remote_aic": JUDGE,
         "state": "awaiting-completion", "created": 0, "updated": 120,
         "commands": [cmd("start", 0, text="委托网络裁判")],
         "events": [("task-result", 120)],
         "products": [_text_product("p-L1", "裁定：无法判定")]},
    ], sessions=[(OUTER, LEADER, -5, 125)])
    make_db(wit, "fact-judge", tasks=[
        {"task_ref": "task_J0", "direction": "inbound", "session_id": OUTER,
         "task_id": TID1, "local_aic": JUDGE, "remote_aic": LEADER,
         "state": "awaiting-completion", "created": 0, "updated": 120,
         "commands": [], "events": [("task-result", 120)], "products": []},
    ])
    out = Path(tmp) / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"verdict-{VERDICT_STAMP}.json").write_bytes(b'{"subject":"x"}')
    (out / f"verdict-{VERDICT_STAMP}.meta.json").write_bytes(
        json.dumps({"session_id": OUTER}).encode("utf-8"))

    # 先确认基线：库完好时没有"读不出来"的告警
    ok = build_trace(wit)
    assert ok is not None
    check_true("基线（库完好）没有'读不出来'告警",
               not any("读不出来" in w for w in ok.warnings))

    # 把内层角色的库写坏
    Path(role_db_path(wit, "fact-judge")).write_bytes(b"not a sqlite database" * 30)
    rep = build_trace(wit)
    assert rep is not None
    hit = [w for w in rep.warnings if "fact-judge" in w and "读不出来" in w]
    check("告警点名了那个角色", len(hit), 1)
    check_true("告警说明'缺少该角色的全部记录'",
               any("缺少该角色的全部记录" in w for w in hit))
    check_true("告警把'配不到对端'指成可能的原因（不让读者查错方向）",
               any("很可能就是这个原因" in w for w in hit))
    check_true("读库失败的告警排在 warnings 最前（它是根因）",
               "读不出来" in rep.warnings[0])
    check_true("坏库不出现在 source_files 里",
               not any("fact-judge" in f for f in rep.source_files))

    # 反向：角色**从没跑过**（库文件不存在）不算失败 —— 否则真告警会被淹没
    empty = str(Path(tmp) / "never-ran" / ".wit")
    (Path(empty) / "ghost-role").mkdir(parents=True, exist_ok=True)
    _, files, errors = __import__("tools.trace", fromlist=["_load_all"])._load_all(empty)
    check("库文件不存在的角色不报错", errors, [])
    check("库文件不存在的角色也不进 source_files", files, [])


def case_m_pick_live_session(tmp: str) -> None:
    section("用例 M：🔴 sidecar 取会话不许静默放宽")
    now = datetime(2026, 10, 3, 10, 0, 0, tzinfo=timezone.utc)
    me, other = "aic-me", "aic-someone-else"

    def s(sid: str, owner: str, age: float, dur: float = 120.0,
          start: float | None = None, end: float | None = None) -> SessionRef:
        lo = now - timedelta(seconds=start) if start is not None \
            else now - timedelta(seconds=age + dur)
        hi = now - timedelta(seconds=end) if end is not None else now - timedelta(seconds=age)
        return SessionRef(sid, owner, lo, hi, "", "")

    got, why = pick_live_session([s("s-1", me, 200), s("s-2", me, 30)], now=now, owner=me)
    check("顺序两局（窗口不重叠）⇒ 取最新那局", got.session_id if got else None, "s-2")
    check_true("原因里说明了另有旧会话且不重叠", "不重叠" in why)

    overlap = [s("s-A", me, 0, start=300, end=40), s("s-B", me, 0, start=200, end=30)]
    got2, why2 = pick_live_session(overlap, now=now, owner=me)
    check("两局窗口重叠 ⇒ 拒绝认领（宁缺勿错）", got2, None)
    check_true("原因写明'互相重叠'", "重叠" in why2)

    got3, why3 = pick_live_session([s("s-X", other, 10)], now=now, owner="")
    check("owner 未知 ⇒ 拒绝认领（不再静默跳过 owner 过滤）", got3, None)
    check_true("原因串不许谎称'owner 匹配'", "owner 匹配" not in why3)

    got4, why4 = pick_live_session([s("s-old", me, 900)], now=now, owner=me)
    check("都不新鲜 ⇒ 拒绝认领", got4, None)
    check_true("原因写明了超时", "超过" in why4)

    got5, _ = pick_live_session([], now=now, owner=me)
    check("平台上没有任何会话 ⇒ 拒绝", got5, None)

    # 时区混用不许抛 TypeError（曾用 naive/aware 直接相减）。
    #
    # ⚠️ 这条**必须有咬合力**，否则是空转的假断言：本机若就是 UTC，
    #    「给 naive 挂 UTC」与「按本地时区解释 naive」是**同一个函数**，
    #    两种实现结果逐位相同，断言永远通过却什么都没守住。
    #    这里把本机偏移现算出来，θ==0 时**响亮地说明本机空转**（不静默放过）。
    naive_now = datetime(2026, 10, 3, 10, 0, 0)
    off_s = naive_now.astimezone().utcoffset().total_seconds()
    got6, why6 = pick_live_session(
        [SessionRef("s-n", me, naive_now - timedelta(seconds=200),
                    naive_now - timedelta(seconds=10), "", "")],
        now=now, owner=me)
    check("naive 会话 + aware now 不炸（按绝对时刻对齐，不按本地时区换算）",
          got6.session_id if got6 else None, "s-n")
    if off_s == 0.0:
        print("  [注意] 本机时区为 UTC ⇒ 上面这条在本机**空转**："
              "『挂 UTC』与『按本地时区换算』此刻是同一个函数。"
              "要真正验它，需在非 UTC 主机（如服务器）上跑。")
    else:
        # θ≠0 时才有判别力：坏实现会把 naive 当**本地时刻**换算，
        # 于是这条会话会因超出新鲜窗口而被拒 —— 上面那条断言当场变红。
        print(f"  [注意] 本机偏移 {off_s/3600:+.1f}h ⇒ 上一条有判别力"
              "（坏实现会把它算成负年龄而误拒）")


def case_n_render_claims(tmp: str) -> None:
    section("用例 N：🔴 渲染层不许过度声称 / 不许漏 markdown 标记 / 不许拆烂表格")
    rep = build_trace(build_fixture(tmp))
    assert rep is not None
    hard = [f for f in rep.verdict_fields if f.evidence == "hard"]
    check_true("夹具里确实有硬溯源字段（否则下面几条是空转的）", bool(hard))
    # ⚠️ 先钉死"夹具的数据里没有 markdown 字符"。否则"页面里没有 `**`"这条
    #    可能只是"数据碰巧没带"，而不是"模板干净"—— 那就成了一条会骗人的通过。
    free = ([rep.run.subject] + [h.request_excerpt for h in rep.hops]
            + [str(f.value_preview) for f in rep.verdict_fields])
    check("夹具数据里不含 `**` 与反引号（下面两条断言才不是空转的）",
          [x for x in free if "**" in x or "`" in x], [])

    html = render_html(rep)
    # 拿**去标签后的明文**来判语义：模板里为了排版会插 <b>/</b>，
    # 直接匹配整句会被标签劈开（曾经差点因此写成一条永远为假的断言）。
    body = _re.sub(r"<style>.*?</style>", "", html, flags=_re.S)
    plain = _re.sub(r"<[^>]+>", "", body)

    # ① 「硬」必须被解释，而且必须**否认**平台级认证
    check_true("页面明文写明了『硬』的含义（字段名出现在下游产物正文里）",
               "下游返回的产物正文" in plain)
    check_true("页面明文写明了『硬』不是平台级认证（不许过度声称）",
               _re.search(r"不是\s*平台级认证", plain) is not None)
    # ② 硬字段必须附命中片段，否则「硬」只是一句无法复核的断言
    for f in hard:
        ex = str(f.source.get("excerpt") or "")
        check_true(f"HTML 渲染了 `{f.field}` 的命中片段",
                   bool(ex) and _html.escape(ex, quote=True) in html)
    # ③ 我们自己写的文案里不许混入 markdown 标记（在浏览器里会**原样显示**）
    #    🔴 范围必须**只圈"我们自己写的"**：命中片段是**下游产物原文**，里面本来就
    #       可能带 `**`（实测旗舰样本单页里就有），那是**证据**，必须原样保留。
    #       一刀切地要求"整页没有 `**`"既会把真人数据判成失败，也会诱使人去改证据。
    #    ⚠️ 这里**不写"实测有 N 处"**这种数字：样本一换它就过期，而过期的数字比
    #       没有数字更容易骗人（本项目那条"写死的数字比文案危险"）。
    def _own_md(rep_: "object", label: str) -> None:
        bad = [("warning", w) for w in rep_.warnings if "**" in w]
        bad += [("gap", g["reason"]) for g in rep_.gaps if "**" in g["reason"]]
        bad += [("edge.why", e.why) for e in rep_.chain_edges if "**" in e.why]
        check(f"{label}：我们自己的文案里没有 markdown 标记", bad, [])

    _own_md(rep, "主场景（无告警）")
    check("HTML 明文里没有 markdown 加粗标记 `**`", plain.count("**"), 0)
    check("HTML 里没有反引号（应改用 <code>）", html.count("`"), 0)
    check_true("HTML 确实在用 <code> / <b>（正面判据：不是靠『一律不加样式』过关）",
               "<code>" in html and "<b>" in html)

    # ③b ⚠️ 光在主场景上判"没有 `**`"是**不够**的：主场景恰好没走到那些文案。
    #     下面把每一条会带标记的**分支**都踩一遍，且每条都先证明"确实走到了"。
    r_drop = build_trace(build_fixture(str(Path(tmp) / "n2"), drop_products=True))
    assert r_drop is not None
    check_true("无下游产物：确实落进了 gaps（否则那条分支没被走到）",
               any("没有任何下游产物返回" in g["reason"] for g in r_drop.gaps))
    _own_md(r_drop, "无下游产物")

    r_unm = build_trace(build_fixture(str(Path(tmp) / "n3"), unmatched_hop=True))
    assert r_unm is not None
    check_true("有配不到对端的跳：确实产生了那条告警",
               any("配不到对端" in w for w in r_unm.warnings))
    _own_md(r_unm, "配不到对端")

    r_amb = build_trace(_two_parent_fixture(str(Path(tmp) / "n4")))
    assert r_amb is not None
    check_true("跨父争抢：确实标了 ambiguous 且 why 点名了冲突",
               any(e.ambiguous and "其它" in e.why for e in r_amb.chain_edges))
    _own_md(r_amb, "跨父争抢")

    # 某角色的库读不出来（`_load_all` 那条告警）
    w_bad = build_fixture(str(Path(tmp) / "n5"))
    Path(role_db_path(w_bad, "fact-judge")).write_bytes(b"not a sqlite database" * 30)
    r_bad = build_trace(w_bad)
    assert r_bad is not None
    check_true("坏库：确实点名了那个角色", any("读不出来" in w for w in r_bad.warnings))
    _own_md(r_bad, "坏库")

    # 本局没有任何 outbound 行（`build_trace` 那条告警）
    w_none = str(Path(tmp) / "n6" / ".wit")
    make_db(w_none, "leader", tasks=[
        {"task_ref": "task_ONLYIN", "direction": "inbound", "session_id": OUTER,
         "task_id": "tid-onlyin", "local_aic": LEADER, "remote_aic": JUDGE,
         "state": "awaiting-completion", "created": 0, "updated": 10,
         "commands": [], "events": [], "products": []},
    ], sessions=[(OUTER, LEADER, -5, 15)])
    o_none = Path(tmp) / "n6" / "output"
    o_none.mkdir(parents=True, exist_ok=True)
    (o_none / f"verdict-{VERDICT_STAMP}.json").write_bytes(b'{"subject":"x"}')
    (o_none / f"verdict-{VERDICT_STAMP}.meta.json").write_bytes(
        json.dumps({"session_id": OUTER}).encode("utf-8"))
    r_none = build_trace(w_none)
    assert r_none is not None
    check_true("没有任何 outbound 行：确实产生了那条告警",
               any("没有任何 outbound 行" in w for w in r_none.warnings))
    _own_md(r_none, "没有 outbound 行")

    # 缺 sidecar、按 verdict 时刻认领一局（`_resolve_run` 那条告警）
    # ⚠️ 这一段**不是凑数**：2026-10-03 修前重新生成的真机样本里，单页 HTML 上
    #    原样出现过 `**本表把 verdict 归到哪一局靠推断**` —— 泄漏的**正是这一条**。
    #    而上面五条分支当时都踩了、偏偏漏了它 ⇒ 那条断言对"真正回归过的分支"
    #    恰好是空转的。所以这里既要点名"确实走到了这条分支"，也要过同一把尺子。
    w_nosc = build_fixture(str(Path(tmp) / "n7"), sidecar=False)
    r_nosc = build_trace(w_nosc)
    assert r_nosc is not None
    check("缺 sidecar：确实降级成「按 verdict 时刻认领」", r_nosc.run.resolution, "inferred")
    check_true("缺 sidecar：确实打出了「归到哪一局靠推断」那条告警",
               any("归到哪一局" in w for w in r_nosc.warnings))
    _own_md(r_nosc, "缺 sidecar 降级认领")

    # ④ 终端与 Markdown 也必须写明口径 + 附命中片段
    tx = render_text(rep)
    check_true("终端树写明了『硬』的含义", "下游返回的产物正文" in tx)
    check_true("终端树写明了『硬』不是平台级认证", "不是平台级认证" in tx)
    check_true("终端树渲染了命中片段",
               all(str(f.source.get("excerpt") or "") in tx for f in hard))
    md = render_markdown(rep)
    check_true("Markdown 写明了『硬』的含义", "下游返回的产物正文" in md)
    check_true("Markdown 写明了『硬』不是平台级认证", "它不是平台级认证" in md)
    check_true("Markdown 渲染了命中片段",
               all(_md_cell(f.source["excerpt"], 160) in md for f in hard))

    # ⑤ 🔴 值里有竖线时，表格不许被**拆成两行**
    #    ⚠️ 判据必须按 **markdown 渲染器的口径**（只按未转义的 `|` 切分）来算，
    #       不能数"字符串里有几行" —— 未转义时字符串同样只有一行，
    #       那种写法会得到一条**恒为真**的假断言。
    def _cells(row: str) -> int:
        return len([c for c in _re.split(r"(?<!\\)\|", row)]) - 2  # 掐掉首尾两个空段

    rep2 = _one_hop_run(str(Path(tmp) / "pipe"), {"band": "正当批评 | 用词过激"},
                        "band = 正当批评")
    md2 = render_markdown(rep2)
    rows = [l for l in md2.splitlines() if l.startswith("| `band`")]
    check("含竖线的字段值仍是**一行四格**", [len(rows), _cells(rows[0])] if rows else [], [1, 4])
    check_true("竖线在单元格内被转义成 \\|", bool(rows) and "\\|" in rows[0])
    check("反面：把转义撤掉就会切成五格（证明上一条有判别力）",
          _cells(rows[0].replace("\\|", "|")) if rows else None, 5)

    rep3 = _one_hop_run(str(Path(tmp) / "pipe2"), {"subject": "甲 | 乙"}, "无关正文")
    md3 = render_markdown(rep3)
    rows3 = [l for l in md3.splitlines() if l.startswith("| 输入原文")]
    check("输入原文含竖线时仍是**一行两格**",
          [len(rows3), _cells(rows3[0])] if rows3 else [], [1, 2])
    check_true("输入原文的竖线也被转义", bool(rows3) and "\\|" in rows3[0])

    # ⑥ 🔴 危害指数越界时 pin 必须被夹在标尺内，而且**说出来**
    rep4 = _one_hop_run(str(Path(tmp) / "scale"), {"harm_index": 250}, "无关正文")
    h4 = render_html(rep4)
    m = _re.search(r"class='pin' style='left:([0-9.]+)%'", h4)
    check_true("越界值仍然画出了 pin", bool(m))
    check("pin 被夹在 100% 以内（不画到色条外面）",
          bool(m) and float(m.group(1)) <= 100.0, True)
    h4_plain = _re.sub(r"<[^>]+>", "", _re.sub(r"<style>.*?</style>", "", h4, flags=_re.S))
    check_true("页面**说明了**它被夹过（不许静默）",
               "超出 -100..100 标尺" in h4_plain)
    # 反面：不越界时**不许**出现那句提示（否则它就成了永远贴着的噪声）
    h5 = render_html(_one_hop_run(str(Path(tmp) / "scale2"),
                                  {"harm_index": 50}, "无关正文"))
    check_true("不越界时不出现夹取提示",
               "超出 -100..100 标尺" not in h5)

    # ⑦ 🔴 取"最早/最晚时刻"必须按**时间**比，不许按**字典序**比
    #    ⚠️ 平台眼下统一写 `+00:00`，字典序恰好等于时间序 —— 那只是巧合。
    #       这里故意让两跳的偏移形态不同：`…T09:00:00+00:00` 与
    #       `…T16:05:00+08:00`（= 08:05Z，**更早**）。字典序会把前者判成更早，
    #       于是"本局开始时刻"悄悄错 55 分钟，而页面照样长得正常。
    wit7 = str(Path(tmp) / "tz" / ".wit")
    make_db(wit7, "leader", tasks=[
        # 16:05+08:00 == 08:05Z ⇒ **最早**；但字典序里 "16…" 比 "09…" **大**
        {"task_ref": "task_TZ1", "direction": "outbound", "session_id": OUTER,
         "task_id": TID1, "local_aic": LEADER, "remote_aic": JUDGE,
         "state": "awaiting-completion",
         "created": "2026-10-03T16:05:00+08:00",
         "updated": "2026-10-03T16:30:00+08:00",
         "commands": [], "events": [("task-result", "2026-10-03T16:30:00+08:00")],
         "products": []},
        # 09:00Z / 09:10Z ⇒ 真正的最晚
        {"task_ref": "task_TZ2", "direction": "outbound", "session_id": OUTER,
         "task_id": TID2, "local_aic": LEADER, "remote_aic": JUDGE,
         "state": "awaiting-completion",
         "created": "2026-10-03T09:00:00+00:00",
         "updated": "2026-10-03T09:10:00+00:00",
         "commands": [], "events": [("task-result", "2026-10-03T09:10:00+00:00")],
         "products": []},
    ], sessions=[(OUTER, LEADER, "2026-10-03T08:00:00+00:00", "2026-10-03T09:10:00+00:00")])
    out7 = Path(tmp) / "tz" / "output"
    out7.mkdir(parents=True, exist_ok=True)
    (out7 / f"verdict-{VERDICT_STAMP}.json").write_bytes(b'{"subject":"tz"}')
    (out7 / f"verdict-{VERDICT_STAMP}.meta.json").write_bytes(
        json.dumps({"session_id": OUTER}).encode("utf-8"))
    rep7 = build_trace(wit7)
    assert rep7 is not None
    check("最早时刻按『绝对时刻』取：16:05+08:00 == 08:05Z，早于 09:00Z",
          rep7.run.started_at, "2026-10-03T08:05:00+00:00")
    check("最晚时刻同理取 09:10Z（字典序会错取 16:30+08:00）",
          rep7.run.finished_at, "2026-10-03T09:10:00+00:00")
    check("总耗时 = 65 分钟（字典序比较会算出负数或巨值）",
          rep7.run.wall_ms, 65 * 60 * 1000)


def main_verify() -> int:
    print("=" * 78)
    print("  离线断言 · 结论溯源表（tools/trace.py）")
    print("=" * 78)
    with tempfile.TemporaryDirectory() as tmp:
        case_a_normal(tmp)
        case_b_no_sidecar(str(Path(tmp) / "b"))
        case_c_ambiguous(str(Path(tmp) / "c"))
        case_d_no_products(str(Path(tmp) / "d"))
        case_e_unmatched(str(Path(tmp) / "e"))
        case_f_html(str(Path(tmp) / "f"))
        case_g_exit_codes(str(Path(tmp) / "g"))
        case_h_readonly(str(Path(tmp) / "h"))
        case_i_request_excerpt(str(Path(tmp) / "i"))
        case_j_no_dup_hops(str(Path(tmp) / "j"))
        case_k_no_false_hard(str(Path(tmp) / "k"))
        case_l_broken_db(str(Path(tmp) / "l"))
        case_m_pick_live_session(str(Path(tmp) / "m"))
        case_n_render_claims(str(Path(tmp) / "n"))

    print()
    print("=" * 78)
    print(f"  通过 {_PASSES} 项，失败 {len(_FAILS)} 项")
    if _FAILS:
        for label in _FAILS:
            print(f"    ✗ {label}")
        print("  结论：不通过")
        print("=" * 78)
        return 1
    print("  结论：全部通过")
    print("  本文件守的几条：")
    print("  · 一跳两端按 task_id 硬配对；跨层无硬键 ⇒ 一律标 inferred 且必带 why")
    print("  · 🔴 缺 sidecar 时只认主控的会话表 —— 不许认成内层会话（会凭空少一层）")
    print("  · 🔴 假值（False / 0）不许被预览函数抹成空串")
    print("  · 🔴 gaps 是必需字段：找不到来源就如实列，且 HTML 里必须渲染成灰色行")
    print("  · 🔴 请求原文只取 text 键正文，不许掺入 type 字段的字面 'text'")
    print("  · 🔴 一跳不许造两遍；不许自指边；两个父争同一子会话必须标 ambiguous")
    print("  · 🔴 不许假硬溯源：字段名不许子串命中（band↛abandon）、不许命中产物外壳")
    print("  · 🔴 某角色库读不出来 ⇒ 告警点名该角色，并把它列为'配不到对端'的根因")
    print("  · 🔴 sidecar 取会话不许静默放宽：owner 未知 / 两局窗口重叠 ⇒ 拒绝认领")
    print("  · 🔴 渲染层不许过度声称：`硬` 必须写明含义（不是平台级认证）并附命中片段")
    print("  · 🔴 HTML 里不许混入 markdown 标记；Markdown 单元格必须转义竖线")
    print("  · 🔴 取最早/最晚时刻按时间比，不按字典序；越界值夹住并且说出来")
    print("  · 定不出这一局就 exit 2，不产半成品；生成器全程只读")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main_verify())
