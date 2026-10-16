"""验证 tools/metrics.py 的四项指标口径（离线、合成数据、不联网）。

为什么要单独验证
════════════════
指标的口径一旦算错，报告里的数字就是**编的** —— 而"看起来合理"的数字最难被发现。
所以这里不看真实数据，而是**构造已知答案的合成任务库**，断言 metrics.py 算出同样的答案。

判据只有一条：断言必须能在"口径被改坏"时失败。
本脚本末尾有一段**反向对照**，用刻意写错的口径算一遍，断言它与正确口径不同 ——
用来证明上面那些断言不是空转的。

用法
════
    ./.venv/bin/python -m tools.verify_metrics
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools import metrics as _M
from tools.metrics import (
    OWN_AIC_PREFIX,
    RETIRED_AICS,
    # 带下划线的是内部函数；故意拿来复算"错误口径"，好让反向对照真的跑一遍，
    # 而不是硬编码一个数字假装对照过。
    _NON_SUCCESS_INBOUND_STATES,
    _parse_ts,
    _read_tasks,
    build_report,
    compute_role,
    discover_roles,
    render_json,
    render_markdown,
    render_text,
)

UTC = timezone.utc
BASE = datetime(2026, 9, 29, 10, 0, 0, tzinfo=UTC)

_OWN = f"{OWN_AIC_PREFIX}.AAAAAAAA.AAAAAA.AAAA"      # 自家 AIC
_EXT = "1.2.156.3088.1.0001.00001.EZTK03.PABX6D.0JUV"  # 外部 AIC（平台/他队）
_PEER = f"{OWN_AIC_PREFIX}.BBBBBBBB.BBBBBB.BBBB"      # 自家伙伴
#: 一个**只有 access 日志、没有任务库**的角色 —— 用来证明 discover_roles 不漏角色
_ACC_SELF = f"{OWN_AIC_PREFIX}.CCCCCCC.CCCCCC.CCCC"
_ACC_ROLE = "acc-role"

_DDL = """
CREATE TABLE aip_tasks (
  task_ref text primary key,
  direction text not null check(direction in ('outbound', 'inbound')),
  session_id text not null,
  task_id text not null,
  local_aic text not null,
  remote_aic text not null,
  endpoint_url text,
  delivery text not null check(delivery in ('rpc', 'stream', 'notification')),
  state text,
  last_result text,
  local_error text,
  command_history text not null,
  products text not null,
  status_history text not null,
  agent_state text not null,
  created_at text not null,
  updated_at text not null,
  unique(direction, local_aic, remote_aic, session_id, task_id)
);
CREATE TABLE aip_task_events (
  task_ref text not null references aip_tasks(task_ref) on delete cascade,
  event_seq integer not null check(event_seq > 0),
  message_id text not null,
  event_type text not null,
  event_data text not null,
  created_at text not null,
  primary key(task_ref, event_seq),
  unique(task_ref, message_id)
);
"""


def ts(offset_ms: float) -> str:
    return (BASE + timedelta(milliseconds=offset_ms)).isoformat()


def cmd(name: str, sent_at_ms: float) -> dict:
    return {"type": "task-command", "command": name, "sentAt": ts(sent_at_ms)}


def make_db(
    root: str,
    role: str,
    *,
    tasks: list[dict],
    heartbeat_lines: int = 0,
) -> str:
    """按真实 schema 造一个角色的任务库（+ 可选 AMP 心跳）。"""
    d = Path(root) / role
    (d / "acps").mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(parents=True, exist_ok=True)
    db = d / "acps" / "aip-tasks.sqlite3"

    # 同一个角色可能在多个用例里重复建库 —— 先清干净，避免 "table already exists"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    con = sqlite3.connect(str(db))
    con.executescript(_DDL)
    for i, t in enumerate(tasks, start=1):
        con.execute(
            "INSERT INTO aip_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t["task_ref"],
                t["direction"],
                t.get("session_id", "session-1"),
                f"task-id-{i}",
                t.get("local_aic", _OWN),
                t["remote_aic"],
                None,
                "rpc",
                t.get("state"),
                None,
                t.get("local_error"),
                json.dumps(t.get("commands", [])),
                json.dumps(t.get("products", [])),
                json.dumps([]),
                json.dumps({"messages": [], "metadata": {}}),
                t["created_at"],
                t["updated_at"],
            ),
        )
        for seq, (et, at) in enumerate(t.get("events", []), start=1):
            con.execute(
                "INSERT INTO aip_task_events VALUES (?,?,?,?,?,?)",
                (t["task_ref"], seq, f"msg-{t['task_ref']}-{seq}", et,
                 json.dumps({"type": et}), at),
            )
    con.commit()
    con.close()

    if heartbeat_lines:
        hb = d / "logs" / f"amp_heartbeat_{_OWN}.jsonl"
        start = BASE
        with open(hb, "w", encoding="utf-8") as fh:
            for k in range(heartbeat_lines):
                fh.write(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "timestamp": (
                                start + timedelta(seconds=30 * k)
                            ).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
                            "aic": _OWN,
                            "log_type": "heartbeat",
                            "body": {"uptimeSeconds": 30.0 * (k + 1)},
                        }
                    )
                    + "\n"
                )
    return str(db)


# ── AMP 访问日志的合成构造 ──────────────────────────────────────────────────


def acc_rec(caller: str, callee: str, size: int, *, status: int = 200,
            dur: float = 3.0, i: int = 0) -> dict:
    """一条 access 日志（字段形态照抄服务器真实记录）。"""
    return {
        "schema_version": "1.0",
        "timestamp": ts(i * 100),
        "aic": callee,
        "log_type": "access",
        "log_id": f"log-{i}",
        "correlation_id": f"rpc-{i}",
        "severity_text": "INFO",
        "severity_number": 9,
        "body": {
            "durationMs": dur,
            "request": {
                "method": "POST",
                "url": "acps.direct.rpc",
                "route": "acps.direct.rpc",
                "bodySizeBytes": size,
            },
            "response": {"statusCode": status},
            "caller": {"aic": caller},
            "callee": {"aic": callee},
        },
        "resource": {"service.name": "synthetic"},
    }


def make_access(root: str, role: str, self_aic: str, entries: list[dict],
                *, malformed: int = 0) -> str:
    """写一个角色的 access 日志文件（文件名里带自身 AIC，与真实形态一致）。"""
    d = Path(root) / role
    (d / "logs").mkdir(parents=True, exist_ok=True)
    fp = d / "logs" / f"amp_access_{self_aic}.jsonl"
    with open(fp, "w", encoding="utf-8") as fh:
        for i, e in enumerate(entries):
            fh.write(json.dumps(e) + "\n")
        for _ in range(malformed):
            fh.write("{ 这不是合法 JSON\n")
    return str(fp)


# ── 断言小工具 ──────────────────────────────────────────────────────────────

_FAILS: list[str] = []
_PASS = 0


def check(label: str, got, want, tol: float = 1e-6) -> None:
    global _PASS
    ok = (
        abs(got - want) <= tol
        if isinstance(want, (int, float)) and isinstance(got, (int, float))
        else got == want
    )
    if ok:
        _PASS += 1
        print(f"  [通过] {label}  ->  {got}")
    else:
        _FAILS.append(label)
        print(f"  [失败] {label}  ->  实际 {got!r}，期望 {want!r}")


def section(name: str) -> None:
    print()
    print(f"── {name} " + "─" * max(0, 66 - len(name)))


def _naive_comm_ms(task) -> list[float]:
    """**故意写错**的通信时延口径：把 ``start`` 也算进去。

    存在的唯一目的是让反向对照真的执行一遍错误算法 —— 断言
    "错误口径的结果 ≠ 正确口径的结果"，才能证明正确口径的断言不是空转的。
    """
    cmds = sorted(
        (t, c.get("command"))
        for c in task.commands
        for t in [_parse_ts(c.get("sentAt"))]
        if t
    )
    evs = sorted(
        t for e in task.events for t in [_parse_ts(e.get("created_at"))] if t
    )
    out: list[float] = []
    for sent, _ in cmds:
        nxt = next((t for t in evs if t >= sent), None)
        if nxt is not None:
            out.append((nxt - sent).total_seconds() * 1000.0)
    return out


# ── 用例 ────────────────────────────────────────────────────────────────────


def case_latency(root: str) -> None:
    section("用例 A：时延口径（接入握手 vs 持续通信，两者样本集必须不同）")
    # start @0ms → 首个回执 @20ms        => 接入响应速度 20ms
    # get    @5000ms → 响应 @5008ms      => 通信时延 8ms
    # comple @9000ms → 响应 @9012ms      => 通信时延 12ms
    make_db(
        root,
        "leader",
        tasks=[
            {
                "task_ref": "t-lat",
                "direction": "outbound",
                "remote_aic": _PEER,
                "state": "completed",
                "created_at": ts(0),
                "updated_at": ts(9012),
                "commands": [cmd("start", 0), cmd("get", 5000), cmd("complete", 9000)],
                "events": [
                    ("task-result", ts(20)),
                    ("task-status-update", ts(5008)),
                    ("task-result", ts(9012)),
                ],
            }
        ],
    )
    m = compute_role(root, "leader")

    check("接入响应速度样本数 = 1（只数 start）", len(m.access_ms), 1)
    check("接入响应速度 = 20ms", m.access_ms[0], 20.0, tol=0.01)
    check("通信时延样本数 = 2（只数 get/complete）", len(m.comm_ms), 2)
    check("通信时延[get] = 8ms", m.comm_ms[0], 8.0, tol=0.01)
    check("通信时延[complete] = 12ms", m.comm_ms[1], 12.0, tol=0.01)

    # 反向对照：真的用**错误口径**复算一遍（把 start 也算进通信时延）。
    # 早先这里是 `naive = len([...])` 硬编码 3 —— 那种对照是空转的：
    # 它断言的是"我自己写的 3 不等于 2"，跟被测代码毫无关系。
    dbp = os.path.join(root, "leader", "acps", "aip-tasks.sqlite3")
    naive = _naive_comm_ms(_read_tasks(dbp)[0])
    check("反向对照：错误口径（start 也算）真的得到 3 个样本", len(naive), 3)
    check("反向对照：错误口径结果 ≠ 正确口径结果", len(naive) != len(m.comm_ms), True)


def case_concurrency(root: str) -> None:
    section("用例 B：并发协同数量（峰值重叠 + 伙伴去重）")
    make_db(
        root,
        "leader",
        tasks=[
            # 三条区间在 100~200ms 处同时活跃 → 峰值 3
            {
                "task_ref": "c1", "direction": "outbound", "remote_aic": _PEER,
                "state": "completed", "session_id": "s1",
                "created_at": ts(0), "updated_at": ts(300),
            },
            {
                "task_ref": "c2", "direction": "outbound", "remote_aic": _PEER,
                "state": "completed", "session_id": "s1",
                "created_at": ts(100), "updated_at": ts(400),
            },
            {
                "task_ref": "c3", "direction": "outbound", "remote_aic": _EXT,
                "state": "completed", "session_id": "s2",
                "created_at": ts(200), "updated_at": ts(500),
            },
            # 已退役的历史 demo 对端：仍在任务库里（删注册≠删历史），但不得计入伙伴数
            {
                "task_ref": "c4", "direction": "outbound",
                "remote_aic": sorted(RETIRED_AICS)[0],
                "state": "completed", "session_id": "s3",
                "created_at": ts(600), "updated_at": ts(700),
            },
            # ⚠️ 对端 AIC 缺失（空串）的脏任务：**不得**计入伙伴数，必须单列。
            #    2026-09-30 修 —— 入站侧早就做了「自家/外部/未知」三分类，
            #    出站侧却直接把 remote_aic 塞进 partners，一条空值就让
            #    「协同伙伴数」静默 +1；为 None 时还会让 sorted() 抛 TypeError。
            {
                "task_ref": "c5", "direction": "outbound", "remote_aic": "",
                "state": "completed", "session_id": "s3",
                "created_at": ts(800), "updated_at": ts(900),
            },
        ],
    )
    m = compute_role(root, "leader")
    check("发起方峰值并发任务数 = 3", m.out_peak_concurrency, 3)
    check("单会话(s1)最大并行 = 2", m.peak_partners_per_session, 2)
    check("伙伴数去重（同 AIC 出现两次只算 1）= 2", len(m.partners), 2)
    check("会话数 = 3", len(m.sessions), 3)
    check("已退役 demo 对端不计入伙伴数", len(m.retired_partners), 1)
    check("已退役对端被单列识别", sorted(RETIRED_AICS)[0] in m.retired_partners, True)
    check("对端 AIC 缺失（空串）**不**计入伙伴数", len(m.partners), 2)
    check("对端 AIC 缺失被单列计数 = 1", m.unknown_partners, 1)


def case_inbound_split(root: str) -> None:
    section("用例 C：访问次数必须拆开「自家自测」与「外部真实访问」")
    make_db(
        root,
        "incitement",
        tasks=[
            {"task_ref": "i1", "direction": "inbound", "remote_aic": _OWN,
             "state": "completed", "created_at": ts(0), "updated_at": ts(10)},
            {"task_ref": "i2", "direction": "inbound", "remote_aic": _OWN,
             "state": "completed", "created_at": ts(20), "updated_at": ts(30)},
            {"task_ref": "i3", "direction": "inbound", "remote_aic": _EXT,
             "state": "completed", "created_at": ts(40), "updated_at": ts(50)},
        ],
    )
    m = compute_role(root, "incitement")
    check("被访问总数 = 3", m.inbound_total, 3)
    check("来自自家 = 2", m.self_test_calls, 2)
    check("来自外部 = 1", m.external_calls, 1)
    check("外部调用方识别正确", [a for a, _ in m.external_detail()], [_EXT])


def case_robustness(root: str) -> None:
    section("用例 D：健壮性（失败任务 / 空库 / 缺列容忍）")
    make_db(
        root,
        "harm",
        tasks=[
            # 连不上对端：有 start 但**没有任何回执** → 不得进入时延样本
            {
                "task_ref": "h1", "direction": "outbound", "remote_aic": _PEER,
                "state": None, "local_error": '{"type":"AipError"}',
                "created_at": ts(0), "updated_at": ts(5),
                "commands": [cmd("start", 0)],
                "events": [],
            },
            # 回执早于命令（时钟异常 / 错序）→ 不得产生负时延
            {
                "task_ref": "h2", "direction": "outbound", "remote_aic": _PEER,
                "state": "completed",
                "created_at": ts(100), "updated_at": ts(200),
                "commands": [cmd("start", 150), cmd("get", 160)],
                "events": [("task-result", ts(100))],
            },
        ],
    )
    m = compute_role(root, "harm")
    check("无回执的任务不产生接入时延样本", len(m.access_ms), 0)
    check("回执早于命令时不产生负时延样本", len(m.comm_ms), 0)
    check("任务数仍然统计到 = 2", m.readings, 2)

    # 空库
    make_db(root, "debate-con", tasks=[])
    m2 = compute_role(root, "debate-con")
    check("空库：任务数 = 0", m2.readings, 0)
    check("空库：发起方峰值并发 = 0", m2.out_peak_concurrency, 0)
    check("空库：被调用峰值并发 = 0", m2.in_peak_concurrency, 0)
    check("空库：被访问 = 0", m2.inbound_total, 0)

    # 库不存在
    m3 = compute_role(root, "no-such-role")
    check("库不存在不抛异常，读数 = 0", m3.readings, 0)


def case_direction_semantics(root: str) -> None:
    section("用例 G：方向语义（inbound 任务绝不能进时延样本）")
    # ⚠️ 这条是真实数据踩出来的：inbound 库里 command_history 记录的是**对端发来的命令**、
    #    事件是**本端发出的状态**，配对算式量到的是处理/轮询时间而非通信时延
    #    （在服务器真实数据上会虚高到几十秒）。所以时延两项只认 outbound。
    make_db(
        root,
        "probe-role",
        tasks=[
            {
                "task_ref": "in-1",
                "direction": "inbound",
                "remote_aic": _OWN,
                "state": "completed",
                "created_at": ts(0),
                "updated_at": ts(30000),
                # 对端发来的命令（start + 一串 get 轮询）
                "commands": [cmd("start", 0), cmd("get", 1000), cmd("get", 2000)],
                # 本端发出的状态（首个回执很快，但后面隔了 29 秒才终态）
                "events": [("task-result", ts(5)), ("task-status-update", ts(29000))],
            }
        ],
    )
    m = compute_role(root, "probe-role")
    check("inbound 任务不产生接入响应样本", len(m.access_ms), 0)
    check("inbound 任务不产生通信时延样本", len(m.comm_ms), 0)
    check("inbound 仍正确计入被访问次数", m.inbound_total, 1)
    check("inbound 不进「协同伙伴」集合", len(m.partners), 0)
    check("inbound 计入被调用峰值并发 = 1", m.in_peak_concurrency, 1)
    check("inbound 不计入发起方峰值并发", m.out_peak_concurrency, 0)


def case_heartbeat(root: str) -> None:
    section("用例 E：AMP 心跳 → 在线时长佐证")
    make_db(root, "debate-pro", tasks=[], heartbeat_lines=5)  # 5 条 × 30s = 120s
    m = compute_role(root, "debate-pro")
    check("心跳样本数 = 5", (m.heartbeat or {}).get("samples"), 5)
    check("在线时长 = 0.03h（120 秒）", (m.heartbeat or {}).get("span_hours"), 0.03, tol=0.005)


def case_unknown_caller(root: str) -> None:
    section("用例 H：调用方 AIC 缺失必须单列「来源未知」，不得算成外部访问")
    # ⚠️ 这条是 2026-09-29 审计发现的真实口径漏洞：早期 `_is_own` 写成
    #    `startswith(自家前缀)`，空串判 False 就被归进"外部真实访问" ——
    #    一条来源不明的脏数据足以静默虚增最敏感的平台访问量。
    make_db(
        root,
        "unknown-role",
        tasks=[
            {"task_ref": "u1", "direction": "inbound", "remote_aic": "",
             "state": "completed", "created_at": ts(0), "updated_at": ts(10)},
            {"task_ref": "u2", "direction": "inbound", "remote_aic": "   ",
             "state": "completed", "created_at": ts(20), "updated_at": ts(30)},
            {"task_ref": "u3", "direction": "inbound", "remote_aic": _OWN,
             "state": "completed", "created_at": ts(40), "updated_at": ts(50)},
            {"task_ref": "u4", "direction": "inbound", "remote_aic": _EXT,
             "state": "completed", "created_at": ts(60), "updated_at": ts(70)},
        ],
    )
    m = compute_role(root, "unknown-role")
    check("被访问总数 = 4", m.inbound_total, 4)
    check("来自自家 = 1", m.self_test_calls, 1)
    check("来自外部 = 1（两条来源缺失的不得计入）", m.external_calls, 1)
    check("来源未知 = 2", m.unknown_calls, 2)
    check("外部明细里不出现空 AIC", [a for a, _ in m.external_detail()], [_EXT])


def case_first_event_any_type(root: str) -> None:
    section("用例 I：接入响应速度取「首条任意事件」，分片回流也算回执")
    # 执行方可以先回 product-chunk（边算边传），只认 task-result 会把
    # 「接入响应速度」量成「算完的时间」—— 那是另一个指标。
    make_db(
        root,
        "chunk-role",
        tasks=[
            {
                "task_ref": "p1", "direction": "outbound", "remote_aic": _PEER,
                "state": "completed", "created_at": ts(0), "updated_at": ts(9000),
                "commands": [cmd("start", 0)],
                "events": [("product-chunk", ts(15)), ("task-result", ts(8000))],
            }
        ],
    )
    m = compute_role(root, "chunk-role")
    check("样本数 = 1", len(m.access_ms), 1)
    check("取值 = 首条事件 15ms（而非 task-result 的 8000ms）",
          m.access_ms[0], 15.0, tol=0.01)


def case_heartbeat_timezone(root: str) -> None:
    section("用例 J：心跳时区写法混杂时，在线时长仍按真实时间算")
    # 构造"字符串序 ≠ 时间序"的一对时间戳：
    #   A = 20:00+08:00 → UTC 12:00
    #   B = 15:00Z      → UTC 15:00（真实上更晚）
    # 字符串序会把 A 排在 B 后面 → 旧口径算出 **负** 的在线时长。
    d = Path(root) / "tz-role"
    (d / "acps").mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(parents=True, exist_ok=True)
    hb = d / "logs" / "amp_heartbeat_tz.jsonl"
    with open(hb, "w", encoding="utf-8") as fh:
        for stamp in ("2026-09-29T20:00:00.000000+08:00", "2026-09-29T15:00:00.000000Z"):
            fh.write(json.dumps({"timestamp": stamp, "log_type": "heartbeat"}) + "\n")
    m = compute_role(root, "tz-role")
    check("在线时长 = 3.0h（按真实时间）", (m.heartbeat or {}).get("span_hours"), 3.0, tol=0.01)


def case_inbound_state(root: str) -> None:
    section("用例 N：「被访问」≠「成功受理」—— 终态必须分得开")
    # ⚠️ 真实踩到的坑：全项目入站 144 条里 canceled 只有 1 条，而它**恰恰就是**
    #    唯一那条外部访问。指标 4 原先不看 state ⇒ 一次被取消的调用被报成
    #    「外部真实访问 1 次」。数字是真的，结论是假的 —— 最危险的一类错。
    make_db(
        root,
        "state-role",
        tasks=[
            # 外部调用方：1 条正常受理 + 1 条 canceled（后者必须能被单独问出来）
            {"task_ref": "s1", "direction": "inbound", "remote_aic": _EXT,
             "state": "awaiting-completion",
             "created_at": ts(0), "updated_at": ts(10)},
            {"task_ref": "s2", "direction": "inbound", "remote_aic": _EXT,
             "state": "canceled",
             "created_at": ts(20), "updated_at": ts(30)},
            # 自家调用方：1 条 failed
            {"task_ref": "s3", "direction": "inbound", "remote_aic": _OWN,
             "state": "failed",
             "created_at": ts(40), "updated_at": ts(50)},
        ],
    )
    m = compute_role(root, "state-role")
    check("被访问总数 = 3（canceled/failed 也算打进来了）", m.inbound_total, 3)
    check("终态分桶正确", m.inbound_by_state,
          {"awaiting-completion": 1, "canceled": 1, "failed": 1})
    check("非成功合计 = 2", m.inbound_bad, 2)
    check("**外部**里的非成功 = 1（canceled 那条）", m.external_bad, 1)
    check("外部访问总数仍是 2（不因失败而隐匿）", m.external_calls, 2)
    check("成功外部访问 = 2 − 1 = 1", m.external_calls - m.external_bad, 1)
    # awaiting-completion **不得**被当成非成功（它占真实数据的 123/144）
    check("awaiting-completion 不算非成功",
          "awaiting-completion" in _NON_SUCCESS_INBOUND_STATES, False)


def case_access_log(root: str) -> None:
    section("用例 K：AMP 访问日志 —— 方向 / 轮询折算 / 三分类")
    # ⚠️ 三条性质都是**实测**出来的，不是读文档推的（见 metrics._read_access）：
    #    ① 文件里两个方向都记 ② 它是每次 RPC 不是每次任务 ③ 轮询报文大小恒定
    entries: list[dict] = []
    entries += [acc_rec(_OWN, _ACC_SELF, 499) for _ in range(20)]      # 自家轮询
    entries += [acc_rec(_OWN, _ACC_SELF, 1234) for _ in range(2)]      # 自家任务提交
    entries += [acc_rec(_EXT, _ACC_SELF, 999)]                         # 外部任务提交
    entries += [acc_rec("", _ACC_SELF, 888) for _ in range(2)]         # caller 整个缺失
    entries += [acc_rec("   ", _ACC_SELF, 777)]                        # caller 纯空白
    entries += [acc_rec(_ACC_SELF, _PEER, 499) for _ in range(15)]     # 本端外呼（轮询）
    entries = [dict(e, timestamp=ts(i * 100)) for i, e in enumerate(entries)]
    make_access(root, _ACC_ROLE, _ACC_SELF, entries, malformed=1)

    m = compute_role(root, _ACC_ROLE)
    acc = m.access or {}
    check("非空行数 = 42（含 1 条坏行）", acc.get("lines"), 42)
    check("解析成功 = 41", acc.get("parsed"), 41)
    check("解析失败 = 1（不得静默丢弃）", acc.get("unparsed"), 1)
    check("被调用 = 26（只数 callee==self）", acc.get("as_callee"), 26)
    check("我调人 = 15（自家外呼，不算被访问）", acc.get("as_caller"), 15)
    check("方向不明 = 0", acc.get("unclear"), 0)
    check("轮询大小**现场派生** = 499（不是写死的常量）", acc.get("poll_size"), 499)
    check("折算任务提交 = 6（26 被调用 − 20 轮询）", acc.get("task_submissions"), 6)
    check("自家 = 22", m.access_own, 22)
    check("外部 = 1", m.access_external, 1)
    check("未知 = 3（两条 caller 缺失 + 一条纯空白）", m.access_unknown, 3)
    check("状态码只出现 200", acc.get("status"), {"200": 41})
    # access 侧是 RPC 级的**处理耗时**，绝不能混进指标 1/2（那是通信时延）
    check("access 日志不进接入响应样本", len(m.access_ms), 0)
    check("access 日志不进通信时延样本", len(m.comm_ms), 0)
    check("access 日志不进任务库受理数", m.inbound_total, 0)
    check("durationMs 单独成表（不并入时延）",
          (acc.get("duration_ms") or {}).get("n"), 41)


def case_access_discovery(root: str) -> None:
    section("用例 L：只有 access 日志、没有任务库的角色不得被漏掉")
    # 反过来的情况真实存在（evidence-audio：有任务库、没有 access 文件）；
    # 这里测的是"只被调过、从没发起过任务"的角色 —— 只认任务库就会漏掉它。
    check("该角色被 discover_roles 发现（无任务库）",
          _ACC_ROLE in discover_roles(root), True)
    m = compute_role(root, _ACC_ROLE)
    check("没有任务库时读数 = 0", m.readings, 0)
    check("db_path = None 时不抛异常", m.db_path, None)
    check("但仍然统计到 access 条目 = 26", m.access_as_callee, 26)


def case_access_render(root: str) -> None:
    section("用例 M：三种渲染都带上 access 对账段，且写清「未发现上报通道」")
    rep = build_report(root, [_ACC_ROLE])
    txt = render_text(rep)
    md = render_markdown(rep)
    js = json.loads(render_json(rep))
    check("文本含 access 对账标题", "【对账】AMP 访问日志" in txt, True)
    check("文本写明不能直接当访问次数报", "直接当访问次数报" in txt, True)
    check("文本写明未发现上报通道", "未发现上报通道" in txt, True)
    check("Markdown 含对账表头", "轮询大小" in md, True)
    check("JSON 角色级有 access_log 台账",
          (js["roles"][0].get("access_log") or {}).get("as_callee"), 26)
    check("JSON 汇总含 access 合计", js["totals"]["access_log"]["as_callee"], 26)
    check("JSON 不明示已上报平台（= None）",
          js["totals"]["access_log"]["reported_to_platform"], None)


def reverse_control(root: str) -> None:
    section("反向对照：证明上面的断言不是空转的")
    # ① 旧口径：把「来源未知」当外部
    m = compute_role(root, "unknown-role")
    old_external = sum(
        n for a, n in m.inbound_by_caller.items()
        if not str(a).startswith(OWN_AIC_PREFIX)
    )
    check("旧口径会把来源未知算成外部（= 3），新口径为 1",
          old_external == m.external_calls, False)
    check("旧口径确实数到了 3", old_external, 3)

    # ② 旧口径：心跳按字符串排序 → 时区混杂时得到负值
    hb = compute_role(root, "tz-role").heartbeat
    raw = sorted(["2026-09-29T20:00:00.000000+08:00", "2026-09-29T15:00:00.000000Z"])
    naive_first = datetime.fromisoformat(raw[0])
    naive_last = datetime.fromisoformat(raw[-1])
    naive_span = round((naive_last - naive_first).total_seconds() / 3600.0, 2)
    check("旧口径（字符串序）会得到负的在线时长", naive_span < 0, True)
    check("新口径修掉了它", (hb or {}).get("span_hours"), 3.0, tol=0.01)
    # ③ 时延口径的反向对照在用例 A 里（用 _naive_comm_ms 真实复算），此处不重复。

    # —— 以下三条是 AMP 访问日志侧的反向对照 ——
    ma = compute_role(root, _ACC_ROLE)
    a = ma.access or {}

    # ④ 旧口径：直接数文件行数当「被访问」（实测会把自家外呼也算进去，翻倍）
    check("旧口径：数行数当被访问 = 42", a.get("lines"), 42)
    check("旧口径 ≠ 新口径（42 ≠ 26）", a.get("lines") != ma.access_as_callee, True)

    # ⑤ 旧口径：不区分方向（被调用 + 我调人 一起数）
    naive_both = ma.access_as_callee + ma.access_as_caller
    check("旧口径：不区分方向 = 41", naive_both, 41)
    check("旧口径 ≠ 新口径（41 ≠ 26）", naive_both != ma.access_as_callee, True)

    # ⑥ 旧口径：把「未知」当外部（与任务库侧同一个坑，换了个数据源再犯一次）
    naive_ext = sum(
        n for k, n in a.get("by_caller", {}).items()
        if not str(k).startswith(OWN_AIC_PREFIX)
    )
    check("旧口径会把 3 条未知算成外部 = 4", naive_ext, 4)
    check("旧口径 ≠ 新口径（4 ≠ 1）", naive_ext != ma.access_external, True)

    # ⑦ 哨兵：任务库侧与 access 侧**共用同一个**外部判定。
    #    「一致性断言」挡不住"两边抄得一字不差"，所以要**改一处看两处都动**。
    base_db = compute_role(root, "unknown-role").external_calls
    base_acc = compute_role(root, _ACC_ROLE).access_external
    _M.OWN_AIC_PREFIX = "SENTINEL-NOT-A-REAL-PREFIX"
    try:
        new_db = compute_role(root, "unknown-role").external_calls
        new_acc = compute_role(root, _ACC_ROLE).access_external
    finally:
        _M.OWN_AIC_PREFIX = OWN_AIC_PREFIX
    check("哨兵：改前缀后任务库侧外部数变了", new_db != base_db, True)
    check("哨兵：改前缀后 access 侧外部数也变了（同一份判定）",
          new_acc != base_acc, True)
    check("哨兵复原后任务库侧回到 1", compute_role(root, "unknown-role").external_calls, 1)
    check("哨兵复原后 access 侧回到 1", compute_role(root, _ACC_ROLE).access_external, 1)

    # ⑧ 旧口径：指标 4 不看终态 ⇒ 把 canceled 的外部调用也算成"成功访问"
    ms = compute_role(root, "state-role")
    naive_success = ms.external_calls          # 旧口径眼里"成功的外部访问"
    real_success = ms.external_calls - ms.external_bad
    check("旧口径会把 canceled 也算成成功外部访问 = 2", naive_success, 2)
    check("旧口径 ≠ 新口径（2 ≠ 1）", naive_success != real_success, True)


def case_render(root: str) -> None:
    section("用例 F：三种渲染都不崩，且数字出现在输出里")
    rep = build_report(root, ["leader", "incitement", "harm", "debate-con", "debate-pro"])
    txt = render_text(rep)
    md = render_markdown(rep)
    js = json.loads(render_json(rep))

    check("文本渲染含指标 1 标题", "【指标 1】智能体接入响应速度" in txt, True)
    check("文本渲染含指标 4 标题", "【指标 4】梧桐平台累计访问次数" in txt, True)
    check("文本渲染标记出外部访问", "外部真实访问明细" in txt, True)
    check("Markdown 含表格头", "| 指标 | 口径 |" in md, True)
    check("JSON 汇总：外部访问 = 1", js["totals"]["inbound_from_external"], 1)
    check("JSON 汇总：自测访问 = 2", js["totals"]["inbound_from_self"], 2)
    check("JSON 汇总：检出外部调用方", js["roles"][1]["access_count"]["by_caller"], {_OWN: 2, _EXT: 1})


# ── 主流程 ──────────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 74)
    print("tools/metrics.py 口径验证（合成数据，不联网）")
    print("=" * 74)

    # ⚠️ Windows 上 sqlite 关闭后文件句柄可能延迟释放，导致临时目录清理失败。
    #    这不影响验证结论，所以忽略清理错误（否则会掩盖真正的断言失败）。
    with tempfile.TemporaryDirectory(
        prefix="acps-metrics-", ignore_cleanup_errors=True
    ) as root:
        case_latency(root)
        case_concurrency(root)
        case_inbound_split(root)
        case_robustness(root)
        case_direction_semantics(root)
        case_heartbeat(root)
        case_unknown_caller(root)
        case_inbound_state(root)
        case_first_event_any_type(root)
        case_heartbeat_timezone(root)
        case_access_log(root)
        case_access_discovery(root)
        case_access_render(root)
        case_render(root)
        reverse_control(root)

    print()
    print("=" * 74)
    if _FAILS:
        print(f"未达预期 {len(_FAILS)} 项：")
        for f in _FAILS:
            print(f"  · {f}")
        return 1
    print(f"全部符合预期：{_PASS} 项断言通过。")
    print("  · 接入响应速度只取 start；通信时延只取非 start —— 两者样本集不重叠")
    print("  · **inbound 任务绝不进时延样本**（方向语义：那里的命令是别人发来的）")
    print("  · 无回执 / 负时延 都不进样本，失败任务不污染统计")
    print("  · 访问次数按 AIC 拆开：自家自测 ≠ 外部真实访问 ≠ 来源未知")
    print("  · 调用方 AIC 缺失一律单列「来源未知」，不得计成平台访问量")
    print("  · 接入响应速度取「首条任意事件」，分片回流（product-chunk）也算回执")
    print("  · 心跳在线时长按真实时间算，不依赖字符串排序")
    print("  · 空库 / 缺库都不抛异常")
    print("  · **access 日志分方向**：只数「被调用」，自家外呼不算被访问（否则翻倍）")
    print("  · 轮询大小**现场派生**，不是写死的魔数；派生不出就报「无法区分」而非 0")
    print("  · access 的三分类与任务库侧**共用同一份判定**（哨兵改一处、两处同动）")
    print("  · durationMs 单独成表，不混进指标 1/2 的通信时延")
    print("  · 反向对照：错误口径**真的复算过**，且结果确实与正确口径不同")
    return 0


if __name__ == "__main__":
    sys.exit(main())
