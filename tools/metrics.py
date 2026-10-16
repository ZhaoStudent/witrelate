"""从任务库与 AMP 日志导出附件2 §五.2 点名的四项量化指标。

用途
════
大赛附件2「五、应用成效 · 2. 量化成效分析」要求提供可量化运行指标：

    智能体接入响应速度、通信时延、并发协同数量、梧桐平台累计访问次数

这四项的原料**已经全在服务器上**，不需要额外埋点采集：

  · ``.wit/<角色>/acps/aip-tasks.sqlite3``
      ``aip_tasks``        —— 每次互联任务一行。关键列：
                              ``direction``(inbound/outbound) / ``local_aic`` /
                              ``remote_aic`` / ``created_at`` / ``updated_at`` /
                              ``command_history``(JSON) / ``state``
      ``aip_task_events``  —— 对端发回的每条事件。关键列：
                              ``event_type`` / ``created_at``(本端入库时刻) / ``event_data``(JSON)
  · ``.wit/<角色>/logs/amp_heartbeat_*.jsonl`` —— AMP 心跳，可证明在线时长
  · ``.wit/<角色>/logs/amp_access_*.jsonl``     —— AMP 访问台账（OTel 形态，
                                                   一次 RPC 一条）。**只作对账**，
                                                   不替代指标 4，理由见 ``_read_access``。

本文件把它们变成可直接贴进技术报告的表格。

四项指标的口径（**本文件最要紧的部分 —— 口径写不清就是在编数**）
════════════════════════════════════════════════════════════════════
⚠️ 先记住一条实读真实库才发现的**方向语义**，指标 1/2 全靠它：

   · ``direction='outbound'`` 的库（本端是发起方）
       ``command_history`` = **我发出去的**命令；事件 = **我收到的**响应
       → 「命令时刻 → 其后首条事件」= 真实往返 RTT ✔
   · ``direction='inbound'`` 的库（本端是被调用方）
       ``command_history`` = **对端发来的**命令（``senderRole`` 是对端）；
       事件 = **本端发出的**状态更新
       → 同一个算式量到的是「对端命令到达 → 本端发状态」，
         夹着大量 ``get`` 轮询间隔与处理时间，**不是通信时延**
         （实测会把中位数虚高到几十秒）

   所以指标 1/2 **只统计 outbound**。被调用方的时延要由**调用方**在自己的库里测，
   或两边都做过 NTP 校时之后再说 —— 混进来就是编数。

1) 智能体接入响应速度
   定义：本端作为**发起方**，从发出 ``start`` 命令到收到对端**首个回执**的耗时。
   算式：本端**最早入库的那条事件**（`aip_task_events` 里 `created_at` 最小的一条）
       的入库时刻 − ``start`` 命令的 ``sentAt``
   注 1：**只用本端时钟**。对端的 ``stateChangedAt`` 属于对端时钟，跨机不可比，
       所以刻意不用它。
   注 2：「回执」**不限于** ``task-result`` —— 执行方可以先回流 ``product-chunk``
       （边算边传的分片）或 ``task-status-update``，这些都算"对端已经应答"。
       只认 ``task-result`` 会把接入响应速度量成"对方算完的时间"，那是另一个指标。
       （实测 2026-09-29：leader 库 13 条有回执的 outbound 任务首事件全是
       ``task-result``；而 incitement/harm 的 inbound 库里确实存在
       ``product-chunk``。口径写成"任意类型"才是稳的。）

2) 通信时延
   定义：本端作为**发起方**，稳态通信阶段每条命令（``get`` / ``complete`` 等）
       发出到收到响应的耗时。
   算式：该命令 ``sentAt`` → 其后第一条事件的**本端入库时刻**
   注：与指标 1 的差别是**样本集**，不是算法 ——
       指标 1 只取 ``start``（接入握手），指标 2 只取非 ``start`` 命令（持续通信）。
       两者**不重叠、不加总**。

3) 并发协同数量
   四个数字：
     · 协同伙伴数   = 本端 outbound 任务里出现过的不同 ``remote_aic`` 个数
     · 峰值并发任务 = 全部任务时间区间 ``[created_at, updated_at]`` 的最大重叠数
     · 会话数       = 不同 ``session_id`` 个数
     · 单会话最大并行伙伴 = 按 ``session_id`` 分组后组内的最大重叠数

4) 梧桐平台累计访问次数
   定义：本端作为**被调用方**受理的任务数。
   算式：``direction='inbound'`` 的任务计数
   注 1：**必须按调用方 AIC 拆开**「自家自测」与「外部真实访问」。
       混在一起就等于把"自己调自己"算成"平台访问"，那是编数。
   注 2：调用方 AIC **缺失/空白**的入站任务单列为「来源未知」，**既不算自家也不算外部**。
       早期版本直接按 ``startswith(自家前缀)`` 判，空串判 False 就被归进了"外部" ——
       一条脏数据足以静默虚增"平台访问量"。这是最敏感的数字，宁可少报不可多报。
   注 3：**「被访问」不等于「成功受理」**。入站任务按终态分桶，failed / canceled
       单列为「非成功」（见 ``_NON_SUCCESS_INBOUND_STATES``）。2026-10-05 实测：
       全项目入站 144 条，其中 canceled **只有 1 条**，而它**恰恰就是唯一那条外部
       访问** —— 不看终态就会把一次被取消的调用报成"外部真实访问 1 次"。
       ⚠️ 反过来也别走极端：``awaiting-completion`` 占 123/144，是这套状态机下
       产出结果后的常态终态，**不能**因为它不叫 completed 就排除掉（那会把 144
       报成 7 —— 少报到失真，是另一种编数）。

输出
════
    ./.venv/bin/python -m tools.metrics                # 人读表格
    ./.venv/bin/python -m tools.metrics --role leader  # 只看一个角色
    ./.venv/bin/python -m tools.metrics --md           # Markdown，直接贴报告
    ./.venv/bin/python -m tools.metrics --json --out metrics.json

本文件**只读**：sqlite 一律用 ``mode=ro`` 打开，不会干扰正在跑的服务。
也**不依赖** wit / .env —— 纯标准库，任何装了 Python 3.11+ 的地方都能跑。
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

__all__ = [
    "OWN_AIC_PREFIX",
    "ROLES",
    "build_report",
    "discover_roles",
    "main",
    "render_markdown",
    "render_text",
]

# ── 配置 ────────────────────────────────────────────────────────────────────

#: 已知角色，仅用于**排序**（不认识的角色会自动追加，不会漏）。
ROLES: tuple[str, ...] = ("incitement", "harm", "leader", "debate-pro", "debate-con")

#: 本项目的 AIC 前缀。用来区分「自家自测流量」与「外部真实访问」——
#: 这个区分最容易做错，也最影响结论可信度。
OWN_AIC_PREFIX = "1.2.156.3088.1.BUPT.81M3FX"

#: 历史上做过端到端测试、后已从平台删除的 demo Agent。
#: ⚠️ 删注册**不会**删掉历史任务记录，所以它们仍会出现在任务库里。
#: 报「协同伙伴数」时必须剔除，否则数字虚高（会把自己删掉的 demo 算成协同对象）。
#: 与 ``tools/check_discovery_pollution.py`` 的清单保持一致。
RETIRED_AICS = frozenset(
    {
        "1.2.156.3088.1.BUPT.81M3FX.YB1DAS.F56BVO.0QYM",
        "1.2.156.3088.1.BUPT.81M3FX.8R0Y7E.IYKGM1.0X32",
        "1.2.156.3088.1.BUPT.81M3FX.B7G6IE.9D5VMV.1DZ8",
    }
)

# ── 入站任务的「非成功终态」 ────────────────────────────────────────────────
#: ⚠️ 指标 4 原先只数 ``direction='inbound'``，**完全不看 state** ⇒ 一条
#: ``canceled`` 的任务也会算成一次"平台访问"。2026-10-05 实测撞上：
#: 全项目入站 144 条里 canceled **只有 1 条**，而它**恰恰就是唯一那条外部访问**
#: （debate-pro ← 外部 AIC，2026-09-29T10:01，最早的联调期）。
#: ⇒ 当时"外部真实访问 1 次"这个最敏感的数字，实际是**一次被取消的调用**。
#:
#: 这里刻意**只**把 failed / canceled 单列为「非成功」，
#: **不**把 ``awaiting-completion`` 排除 —— 实测它在入站任务里占 123/144，
#: 是这套状态机下产出结果后的常态终态（产物确实落盘了，见 output/）。
#: 一律只认 ``completed`` 会把 144 报成 7，那是**另一种形式的编数**（少报到失真）。
#: 两个方向都错，取舍见下：宁可"少报到可信"，不可"少报到失真"，也不可"多报"。
_NON_SUCCESS_INBOUND_STATES = frozenset({"failed", "canceled"})

_DB_REL = os.path.join("{role}", "acps", "aip-tasks.sqlite3")
_HEARTBEAT_REL = os.path.join("{role}", "logs", "amp_heartbeat_*.jsonl")
_ACCESS_REL = os.path.join("{role}", "logs", "amp_access_*.jsonl")

_ACCESS_CMD = "start"

#: 判定「固定大小轮询请求」的门槛：某个 ``body.request.bodySizeBytes`` 取值要
#: **同时**满足 ① 出现次数 ≥ 本值 ② 占该角色已解析条目的一半以上，才认定为轮询。
#:
#: ⚠️ 刻意**不**把实测到的 499 写死成常量 —— 那是"抄一次观测结果当判据"：
#:    SDK 一换版本、或轮询报文多带一个字段就失效，而且失效方式是**静默**的
#:    （会把轮询继续当成任务提交，数字照旧打出来，看不出错了）。
#:    改成从数据里现场派生；派生不出来就返回 None，由展示层报「无法区分」，
#:    绝不退回"当作 0 个任务提交" —— 那等于把「没查出来」写成「查过了没有」。
_POLL_MIN_SAMPLES = 10


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _parse_ts(value: Any) -> datetime | None:
    """把任务库里的 ISO8601 时间串解析成 datetime；失败返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _short_aic(aic: str) -> str:
    """AIC 太长，打印时只留后两段。"""
    parts = str(aic).split(".")
    return "…" + ".".join(parts[-2:]) if len(parts) >= 2 else str(aic)


def _ms(delta_seconds: float) -> float:
    return round(delta_seconds * 1000.0, 3)


def _stats(values: Sequence[float]) -> dict[str, float] | None:
    """样本统计：n / min / p50 / p95 / max / mean。空样本返回 None。"""
    if not values:
        return None
    vs = sorted(values)
    n = len(vs)

    def pct(p: float) -> float:
        if n == 1:
            return vs[0]
        k = (n - 1) * p
        lo = int(k)
        hi = min(lo + 1, n - 1)
        return vs[lo] + (vs[hi] - vs[lo]) * (k - lo)

    return {
        "n": n,
        "min": round(vs[0], 3),
        "p50": round(pct(0.5), 3),
        "p95": round(pct(0.95), 3),
        "max": round(vs[-1], 3),
        "mean": round(sum(vs) / n, 3),
    }


def _fmt_stats(s: dict[str, float] | None, unit: str = "ms") -> str:
    if not s:
        return "（无样本）"
    return (
        f"n={int(s['n']):<3} p50={s['p50']:>8.1f}{unit}  "
        f"p95={s['p95']:>8.1f}{unit}  均值={s['mean']:>8.1f}{unit}  "
        f"min={s['min']:.1f} max={s['max']:.1f}"
    )


def _max_overlap(intervals: Sequence[tuple[datetime, datetime]]) -> int:
    """扫描线求最大重叠数。同一时刻**先结束后开始**（保守估计并发）。"""
    events: list[tuple[datetime, int]] = []
    for start, end in intervals:
        events.append((start, +1))
        events.append((end, -1))
    # 同刻 -1 排前面；用 (时刻, delta) 排序即可，因为 -1 < +1
    events.sort(key=lambda e: (e[0], e[1]))
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


# ── 数据读取 ────────────────────────────────────────────────────────────────


@dataclass
class TaskRow:
    """``aip_tasks`` 里的一行。

    ⚠️ 这是**全项目唯一的任务库读取口径**（`tools/trace.py` 的结论溯源表也走这里，
    不另写第二份 reader —— 同一事实读两遍必然漂移）。
    因此除了指标要用的列，还带上溯源要用的 ``task_id`` / ``products``：
    ``task_id`` 是**一跳两端的硬配对键**（实测：leader/outbound 与 fact-judge/inbound
    的 task_id 完全一致），``products`` 是对端交回的完整产物。
    """

    task_ref: str
    direction: str
    session_id: str
    local_aic: str
    remote_aic: str
    delivery: str
    state: str | None
    created_at: datetime | None
    updated_at: datetime | None
    commands: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    #: AIP 协议级任务号 —— 一跳两端配对用的硬键（见模块顶部与 docs/aicomp/结论溯源表设计.md）
    task_id: str = ""
    #: 对端回传的产物（``aip_tasks.products``，JSON 数组）。指标侧不用，溯源侧要用。
    products: list[dict[str, Any]] = field(default_factory=list)
    #: 写入库时的原始时间串（``created_at`` / ``updated_at`` 的字符串形态）——
    #: 溯源表要求「每一个事实都能回答它写在平台哪个文件的哪一行」，
    #: 所以原样保留，避免展示层与解析层互相污染。
    created_raw: str = ""
    updated_raw: str = ""

    @property
    def is_inbound(self) -> bool:
        return self.direction == "inbound"

    def start_sent_at(self) -> datetime | None:
        """本端发出的第一条 start 命令时刻。"""
        stamps = [
            t
            for c in self.commands
            if c.get("command") == _ACCESS_CMD
            for t in [_parse_ts(c.get("sentAt"))]
            if t
        ]
        return min(stamps) if stamps else None

    def first_event_at(self) -> tuple[datetime | None, str | None]:
        """本端最早入库的一条事件（时刻, event_type）。"""
        pairs = [
            (t, str(e.get("event_type") or ""))
            for e in self.events
            for t in [_parse_ts(e.get("created_at"))]
            if t
        ]
        return min(pairs) if pairs else (None, None)

    def comm_pairs(self) -> list[float]:
        """非 start 命令 → 其后第一条事件 的本端时钟差（秒）。

        刻意保持朴素：按时间排序后取"紧随其后的第一条"。
        本项目的委派命令是顺序 RPC，不存在同刻多命令，所以这个配对是可靠的；
        若将来改成并发命令，这里需要引入 commandId ↔ resultId 的显式关联。
        """
        cmds = sorted(
            (
                t,
                str(c.get("command") or ""),
            )
            for c in self.commands
            for t in [_parse_ts(c.get("sentAt"))]
            if t and c.get("command") != _ACCESS_CMD
        )
        evs = sorted(
            t for e in self.events for t in [_parse_ts(e.get("created_at"))] if t
        )
        out: list[float] = []
        for sent, _name in cmds:
            nxt = next((t for t in evs if t >= sent), None)
            if nxt is not None:
                out.append((nxt - sent).total_seconds())
        return out


def _read_tasks(db_path: str) -> list[TaskRow]:
    """只读打开一个角色的任务库，读出全部任务（含事件）。

    ⚠️ **全项目唯一的任务库 reader**（`tools/trace.py` 复用本函数）。
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT task_ref, direction, session_id, task_id, local_aic, remote_aic,
                   delivery, state, created_at, updated_at, command_history, products
            FROM aip_tasks
            ORDER BY created_at
            """
        ).fetchall()

        ev_by_task: dict[str, list[dict[str, Any]]] = {}
        for e in cur.execute(
            "SELECT task_ref, event_seq, message_id, event_type, event_data, created_at "
            "FROM aip_task_events ORDER BY task_ref, event_seq"
        ):
            ev_by_task.setdefault(e["task_ref"], []).append(
                {
                    "event_seq": e["event_seq"],
                    "message_id": e["message_id"],
                    "event_type": e["event_type"],
                    "event_data": e["event_data"],
                    "created_at": e["created_at"],
                }
            )

        tasks: list[TaskRow] = []
        for r in rows:
            try:
                commands = json.loads(r["command_history"] or "[]")
            except (ValueError, TypeError):
                commands = []
            try:
                products = json.loads(r["products"] or "[]")
            except (ValueError, TypeError):
                products = []
            tasks.append(
                TaskRow(
                    task_ref=r["task_ref"],
                    direction=r["direction"],
                    session_id=r["session_id"],
                    local_aic=r["local_aic"],
                    remote_aic=r["remote_aic"],
                    delivery=r["delivery"],
                    state=r["state"],
                    created_at=_parse_ts(r["created_at"]),
                    updated_at=_parse_ts(r["updated_at"]),
                    commands=commands if isinstance(commands, list) else [],
                    events=ev_by_task.get(r["task_ref"], []),
                    task_id=str(r["task_id"] or ""),
                    products=products if isinstance(products, list) else [],
                    created_raw=str(r["created_at"] or ""),
                    updated_raw=str(r["updated_at"] or ""),
                )
            )
        return tasks
    finally:
        con.close()


def _read_heartbeat(wit_dir: str, role: str) -> dict[str, Any] | None:
    """读 AMP 心跳的首末时间戳，用于佐证在线时长。"""
    files = sorted(glob.glob(os.path.join(wit_dir, _HEARTBEAT_REL.format(role=role))))
    if not files:
        return None

    stamps: list[str] = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        stamps.append(str(json.loads(line)["timestamp"]))
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            continue
    if not stamps:
        return None
    # ⚠️ 不要直接对字符串排序：ISO8601 只有在**时区写法完全一致**时字符串序才等于
    #    时间序。实测本项目心跳统一为 "...Z"（UTC、六位小数），当下是安全的；
    #    但格式一变（例如混入 +08:00 与 Z）就会静默算错在线时长。改为按解析后的
    #    datetime 排序，同时保留原始字符串用于展示。
    parsed = sorted((t, s) for s in stamps for t in [_parse_ts(s)] if t is not None)
    if not parsed:
        return None
    t0, s0 = parsed[0]
    t1, s1 = parsed[-1]
    hours = round((t1 - t0).total_seconds() / 3600.0, 2)
    return {
        "samples": len(stamps),
        "first": s0,
        "last": s1,
        "span_hours": hours,
        "files": [os.path.basename(p) for p in files],
    }


def _derive_poll_size(sizes: "collections.Counter[int]") -> int | None:
    """从本角色的 ``bodySizeBytes`` 直方图里**现场派生**"轮询报文的固定大小"。

    判据（见 ``_POLL_MIN_SAMPLES``）：出现次数 ≥ 门槛 **且** 超过已解析条目的一半。
    平票时取**较小的**那个取值，保证结果可复现（不依赖 Counter 的内部顺序）。

    派生不出来返回 ``None`` —— 调用方**必须**据此报「无法区分」，不许当 0 用。
    """
    if not sizes:
        return None
    total = sum(sizes.values())
    size, n = min(sizes.items(), key=lambda kv: (-kv[1], kv[0]))
    if n >= _POLL_MIN_SAMPLES and n * 2 > total:
        return int(size)
    return None


def _access_self_aic(path: str) -> str:
    """从文件名 ``amp_access_<AIC>.jsonl`` 派生本角色自己的 AIC。

    ⚠️ 刻意**不**用配置项推断：文件名是 AMP 自己写出来的，是唯一不会与
    「配置里写的是哪个角色」发生漂移的来源。取不到就返回空串（→ 方向不明）。
    """
    base = os.path.basename(path)
    if base.startswith("amp_access_") and base.endswith(".jsonl"):
        return base[len("amp_access_") : -len(".jsonl")]
    return ""


def _read_access(wit_dir: str, role: str) -> dict[str, Any] | None:
    """读一个角色的 AMP 访问日志（``amp_access_*.jsonl``），返回台账。

    ⚠️ **两条实测出来的硬性质**，不看代码只看文档会搞错：

    1. **一个角色的文件里两个方向都记**（2026-10-05 实测 3956 条：
       被调用 1978 / 我调人 1978，方向不明 0）。
       ⇒ 「被访问次数」**只能数** ``callee == 自身AIC`` 的那半；直接数行数
         会把自家外呼也算成被访问，**恰好翻倍**。
    2. **它是「每次 RPC」不是「每次任务」**。一次任务会在双方文件里留下
       start + 一串 499 字节轮询 + complete，所以被调用侧的行数远大于任务数。
       ⇒ 要折算成任务级，得先把**固定大小的轮询**剔掉（``_derive_poll_size``）。

    返回的 ``task_submissions`` 是**被调用侧**剔掉轮询后的条目数，可与任务库的
    inbound 数对账；两者应当一致，**不一致就是 access 日志有截断**（实测
    incitement 12 vs 20、harm 9 vs 16 —— 差值正是这次审计最值钱的产出）。
    """
    files = sorted(glob.glob(os.path.join(wit_dir, _ACCESS_REL.format(role=role))))
    if not files:
        return None

    lines = parsed = unparsed = 0
    as_callee = as_caller = unclear = 0
    by_caller: dict[str, int] = {}
    by_callee: dict[str, int] = {}
    status: dict[str, int] = {}
    sizes: collections.Counter[int] = collections.Counter()
    #: 被调用侧**单独**一份大小直方图 —— 折算任务级时只能扣这一份，
    #: 扣全局直方图会把"我轮询别人"也扣掉，让被调用数变成负数。
    callee_sizes: collections.Counter[int] = collections.Counter()
    durations: list[float] = []
    stamps: list[str] = []

    for path in files:
        me = _access_self_aic(path)
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    lines += 1
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        # ⚠️ 解析失败必须**单独计数**，不许静默丢弃：
                        #    丢掉的话"条目数"会悄悄变小，而对账表看不出来。
                        unparsed += 1
                        continue
                    if not isinstance(rec, dict):
                        unparsed += 1
                        continue
                    parsed += 1

                    body = rec.get("body") or {}
                    req = body.get("request") or {}
                    caller = str((body.get("caller") or {}).get("aic") or "").strip()
                    callee = str((body.get("callee") or {}).get("aic") or "").strip()
                    size = req.get("bodySizeBytes")
                    if isinstance(size, int):
                        sizes[size] += 1

                    code = str((body.get("response") or {}).get("statusCode") or "")
                    if code:
                        status[code] = status.get(code, 0) + 1

                    dur = body.get("durationMs")
                    if isinstance(dur, (int, float)):
                        durations.append(float(dur))

                    ts = rec.get("timestamp")
                    if isinstance(ts, str) and ts:
                        stamps.append(ts)

                    if me and callee == me and caller != me:
                        as_callee += 1
                        by_caller[caller] = by_caller.get(caller, 0) + 1
                        if isinstance(size, int):
                            callee_sizes[size] += 1
                    elif me and caller == me and callee != me:
                        as_caller += 1
                        by_callee[callee] = by_callee.get(callee, 0) + 1
                    else:
                        # 自身 AIC 取不到、或两端同 AIC —— 宁可单列也不猜
                        unclear += 1
        except OSError:
            continue

    if not parsed and not unparsed:
        return None

    poll_size = _derive_poll_size(sizes)
    if poll_size is None:
        # 分不出轮询 ⇒ 无法折算任务级。**None 而不是 0**：0 会被读成"没被访问过"。
        submissions: int | None = None
        poll_entries = 0
    else:
        poll_entries = sizes[poll_size]
        # 折算任务级：被调用侧剔掉固定大小的轮询，剩下的就是任务提交（start）。
        # ⚠️ 用**被调用侧自己的直方图**回算，不能用全局 sizes —— 否则会把
        #    "我轮询别人"的条目也扣掉，被调用数直接算成负数。
        submissions = as_callee - callee_sizes[poll_size]

    return {
        "files": [os.path.basename(p) for p in files],
        "lines": lines,
        "parsed": parsed,
        "unparsed": unparsed,
        "as_callee": as_callee,
        "as_caller": as_caller,
        "unclear": unclear,
        "by_caller": by_caller,
        "by_callee": by_callee,
        # ⚠️ 三分类**复用**任务库侧那三个判定（``_is_own`` / ``_is_external`` /
        #    ``_is_unknown``），绝不在这里写第二份 —— 同一判定写两处必然漂移，
        #    而且漂移方式是"两边都自洽、对外数字对不上"。
        #    「外部」只认**明确带非自家 AIC** 的；缺失的一律单列 unknown。
        "own": sum(n for a, n in by_caller.items() if _is_own(a)),
        "external": sum(n for a, n in by_caller.items() if _is_external(a)),
        "unknown": sum(n for a, n in by_caller.items() if _is_unknown(a)),
        "status": status,
        "poll_size": poll_size,
        "poll_entries": poll_entries,
        "task_submissions": submissions,
        "duration_ms": _stats(durations),
        "window": _window(stamps),
    }


def _window(stamps: Sequence[str]) -> list[str | None]:
    """时间窗：按**解析后的时间**取首末，字符串序不可靠（见心跳那段的教训）。"""
    parsed = sorted((t, s) for s in stamps for t in [_parse_ts(s)] if t is not None)
    if not parsed:
        return [None, None]
    return [parsed[0][1], parsed[-1][1]]


def discover_roles(wit_dir: str) -> list[str]:
    """扫描 wit_dir 下**带任何可分析数据**的角色目录（有什么分析什么，不会漏角色）。

    ⚠️ 刻意取**两个来源的并集**：任务库 与 AMP 访问日志。
    只认任务库会漏掉"从未发起过任务、只被调过"的角色；只认访问日志会漏掉
    "只发起过、还没被调过"的角色（`evidence-audio` 实测就是后者：有任务库、
    但没有 access 文件 —— 它从没受理过入站 RPC）。
    """
    found = {
        os.path.basename(os.path.dirname(os.path.dirname(p)))
        for p in glob.glob(os.path.join(wit_dir, "*", "acps", "aip-tasks.sqlite3"))
    }
    found |= {
        os.path.basename(os.path.dirname(os.path.dirname(p)))
        for p in glob.glob(os.path.join(wit_dir, "*", "logs", "amp_access_*.jsonl"))
    }
    known = [r for r in ROLES if r in found]
    extra = sorted(found - set(ROLES))
    return known + extra


# ── 指标计算 ────────────────────────────────────────────────────────────────


@dataclass
class RoleMetrics:
    role: str
    db_path: str | None
    readings: int = 0  # 库里任务总数
    out_tasks: int = 0  # 本端作为发起方
    in_tasks: int = 0  # 本端作为被调用方

    access_ms: list[float] = field(default_factory=list)  # 指标 1
    comm_ms: list[float] = field(default_factory=list)  # 指标 2

    sessions: set[str] = field(default_factory=set)
    partners: set[str] = field(default_factory=set)  # outbound 的活跃对端
    retired_partners: set[str] = field(default_factory=set)  # outbound 的已退役对端
    #: outbound 里**对端 AIC 缺失**（空/空白/NULL）的条数 —— 待排查，不计入伙伴数。
    unknown_partners: int = 0
    out_peak_concurrency: int = 0  # 本端同时发起几个
    in_peak_concurrency: int = 0  # 本端同时被调用几个
    peak_partners_per_session: int = 0

    inbound_total: int = 0  # 指标 4
    inbound_by_caller: dict[str, int] = field(default_factory=dict)
    #: 入站任务按**终态**分桶 —— 「被访问」不等于「成功受理」，两者必须分得开。
    inbound_by_state: dict[str, int] = field(default_factory=dict)
    #: 入站任务里**非成功终态**（failed / canceled）按调用方分桶。
    #: 与 ``inbound_by_caller`` 分开存，是为了让「外部访问里有多少是失败的」
    #: 能被单独问出来 —— 那是最敏感数字里最敏感的一格。
    inbound_bad_by_caller: dict[str, int] = field(default_factory=dict)
    inbound_window: tuple[str | None, str | None] = (None, None)

    state_counts: dict[str, int] = field(default_factory=dict)
    heartbeat: dict[str, Any] | None = None
    #: AMP 访问日志台账（`_read_access`）。**与任务库相互独立的第二个来源**，
    #: 只用于对账，不用于替代指标 4（理由见 `_read_access` 与展示层的说明）。
    access: dict[str, Any] | None = None

    # —— AMP 访问日志侧（RPC 级，只作对账用）——
    @property
    def access_lines(self) -> int:
        """access 文件里的非空行数（含解析失败的）。"""
        return int((self.access or {}).get("lines") or 0)

    @property
    def access_as_callee(self) -> int:
        """本端作为**被调用方**的条目数 —— 这才是"被访问"（RPC 级）。"""
        return int((self.access or {}).get("as_callee") or 0)

    @property
    def access_as_caller(self) -> int:
        """本端作为**调用方**的条目数（自家外呼，**不得**算进被访问）。"""
        return int((self.access or {}).get("as_caller") or 0)

    @property
    def access_own(self) -> int:
        return int((self.access or {}).get("own") or 0)

    @property
    def access_external(self) -> int:
        return int((self.access or {}).get("external") or 0)

    @property
    def access_unknown(self) -> int:
        return int((self.access or {}).get("unknown") or 0)

    @property
    def access_submissions(self) -> int | None:
        """access 侧折算出的**任务级**被访问数；分不出轮询时为 None。"""
        v = (self.access or {}).get("task_submissions")
        return None if v is None else int(v)

    @property
    def self_test_calls(self) -> int:
        """来自自家 AIC 的被调用次数。"""
        return sum(n for a, n in self.inbound_by_caller.items() if _is_own(a))

    @property
    def external_calls(self) -> int:
        """来自外部 AIC 的被调用次数 —— 这才是「平台访问」的硬证据。

        只数**明确带外部 AIC** 的；调用方缺失的走 `unknown_calls`，不计入这里。
        """
        return sum(n for a, n in self.inbound_by_caller.items() if _is_external(a))

    @property
    def unknown_calls(self) -> int:
        """调用方 AIC 缺失的入站任务数（待排查，不得计入平台访问量）。"""
        return sum(n for a, n in self.inbound_by_caller.items() if _is_unknown(a))

    @property
    def inbound_bad(self) -> int:
        """入站任务里**非成功终态**（failed / canceled）的条数。

        ⚠️ 这些**仍然算**「被访问」（对端确实打进来了），但不算「成功受理」。
        单列出来的唯一目的：不让一个 canceled 悄悄混进"平台访问量"。
        """
        return sum(self.inbound_bad_by_caller.values())

    @property
    def external_bad(self) -> int:
        """**外部**入站任务里非成功终态的条数 —— 「外部访问」这格的置信度。"""
        return sum(n for a, n in self.inbound_bad_by_caller.items() if _is_external(a))

    def external_detail(self) -> list[tuple[str, int]]:
        return sorted(
            ((a, n) for a, n in self.inbound_by_caller.items() if _is_external(a)),
            key=lambda kv: (-kv[1], kv[0]),
        )


def _caller_aic(aic: Any) -> str:
    """规范化调用方 AIC：None / 纯空白一律视作「未知」。"""
    return str(aic or "").strip()


def _is_own(aic: Any) -> bool:
    """是否本项目的 AIC（自家自测流量）。"""
    s = _caller_aic(aic)
    return bool(s) and s.startswith(OWN_AIC_PREFIX)


def _is_external(aic: Any) -> bool:
    """是否**明确的**外部 AIC。

    ⚠️ 空 / 空白必须返回 False。早期版本写成 ``not startswith(自家前缀)``，
    空串判 False 后被归进"外部真实访问" —— 一条来源不明的脏数据就足以
    静默虚增「平台累计访问次数」。那是最敏感的数字，宁可少报不可多报。
    """
    s = _caller_aic(aic)
    return bool(s) and not s.startswith(OWN_AIC_PREFIX)


def _is_unknown(aic: Any) -> bool:
    """调用方 AIC 缺失 —— 既不算自家也不算外部，单列待查。"""
    return not _caller_aic(aic)


def compute_role(wit_dir: str, role: str) -> RoleMetrics:
    db_rel = os.path.join(wit_dir, _DB_REL.format(role=role))
    db_path = db_rel if os.path.exists(db_rel) else None
    m = RoleMetrics(role=role, db_path=db_path)
    m.heartbeat = _read_heartbeat(wit_dir, role)
    # ⚠️ access 必须在「库不存在就提前返回」**之前**读：
    #    只有 access 日志没有任务库的角色（从未发起过任务、只被调过）也要统计，
    #    否则 discover_roles 认得出来的角色，compute_role 却给出空台账。
    m.access = _read_access(wit_dir, role)
    if db_path is None:
        return m

    tasks = _read_tasks(db_path)
    m.readings = len(tasks)

    out_iv: list[tuple[datetime, datetime]] = []
    in_iv: list[tuple[datetime, datetime]] = []
    per_session: dict[str, list[tuple[datetime, datetime]]] = {}

    inbound_ts: list[str] = []
    for t in tasks:
        m.state_counts[str(t.state)] = m.state_counts.get(str(t.state), 0) + 1

        if t.is_inbound:
            m.in_tasks += 1
            m.inbound_total += 1
            m.inbound_by_caller[t.remote_aic] = (
                m.inbound_by_caller.get(t.remote_aic, 0) + 1
            )
            st = str(t.state) if t.state is not None else "(NULL)"
            m.inbound_by_state[st] = m.inbound_by_state.get(st, 0) + 1
            if st in _NON_SUCCESS_INBOUND_STATES:
                m.inbound_bad_by_caller[t.remote_aic] = (
                    m.inbound_bad_by_caller.get(t.remote_aic, 0) + 1
                )
            if t.created_at:
                inbound_ts.append(t.created_at.isoformat())
        else:
            m.out_tasks += 1
            # 会话数只算"我发起的会话"；已退役的 demo 对端不计入协同伙伴
            if t.session_id:
                m.sessions.add(t.session_id)
            # ⚠️ 与入站侧同一条纪律：**空/空白 AIC 不得计入任何对外数字**。
            #    inbound 侧早就做了三分类（自家 / 外部 / 未知），outbound 侧却
            #    直接把 `t.remote_aic` 塞进 partners —— 一条对端 AIC 为 NULL/空白
            #    的脏任务就会让「协同伙伴数」静默 +1；而当它是 None 时，
            #    后面 `sorted(m.partners)` 还会在 None 与 str 之间比较、直接抛
            #    TypeError，让整份报表崩掉。
            #    最敏感的数字宁可少报不可多报 —— 单列「来源未知」待排查。
            peer = (t.remote_aic or "").strip()
            if not peer:
                m.unknown_partners += 1
            elif peer in RETIRED_AICS:
                m.retired_partners.add(peer)
            else:
                m.partners.add(peer)

        if t.created_at and t.updated_at and t.updated_at >= t.created_at:
            iv = (t.created_at, t.updated_at)
            (in_iv if t.is_inbound else out_iv).append(iv)
            if not t.is_inbound:
                per_session.setdefault(t.session_id, []).append(iv)

        # 指标 1 / 2 **只统计本端作为发起方（outbound）的任务**。
        # 原因见文件顶部「方向语义」——inbound 库里 command_history 是对端发来的命令、
        # 事件是本端发出的状态，同一算式量到的是处理/轮询时间而非通信时延。
        if not t.is_inbound:
            start_at = t.start_sent_at()
            first_ev, _ = t.first_event_at()
            if start_at and first_ev and first_ev >= start_at:
                m.access_ms.append(_ms((first_ev - start_at).total_seconds()))
            m.comm_ms.extend(_ms(d) for d in t.comm_pairs())

    m.out_peak_concurrency = _max_overlap(out_iv)
    m.in_peak_concurrency = _max_overlap(in_iv)
    m.peak_partners_per_session = max(
        (_max_overlap(v) for v in per_session.values()), default=0
    )
    if inbound_ts:
        inbound_ts.sort()
        m.inbound_window = (inbound_ts[0], inbound_ts[-1])
    return m


@dataclass
class Report:
    wit_dir: str
    generated_at: str
    roles: list[RoleMetrics]

    # —— 汇总（全角色合并）——
    @property
    def all_access_ms(self) -> list[float]:
        return [x for r in self.roles for x in r.access_ms]

    @property
    def all_comm_ms(self) -> list[float]:
        return [x for r in self.roles for x in r.comm_ms]

    @property
    def total_inbound(self) -> int:
        return sum(r.inbound_total for r in self.roles)

    @property
    def total_external(self) -> int:
        return sum(r.external_calls for r in self.roles)

    @property
    def total_selftest(self) -> int:
        return sum(r.self_test_calls for r in self.roles)

    @property
    def total_unknown(self) -> int:
        """调用方 AIC 缺失的入站任务合计（待排查）。"""
        return sum(r.unknown_calls for r in self.roles)

    @property
    def total_inbound_bad(self) -> int:
        """入站任务里非成功终态（failed / canceled）合计。"""
        return sum(r.inbound_bad for r in self.roles)

    @property
    def total_external_bad(self) -> int:
        """**外部**入站任务里非成功终态合计 —— 报「外部访问」前必看这一格。"""
        return sum(r.external_bad for r in self.roles)

    @property
    def partners(self) -> set[str]:
        return {p for r in self.roles for p in r.partners}

    # —— AMP 访问日志侧合计（RPC 级，对账用）——
    @property
    def total_access_lines(self) -> int:
        return sum(r.access_lines for r in self.roles)

    @property
    def total_access_callee(self) -> int:
        """全项目「被调用」条目合计 —— 一次 RPC 在双方各记一条，所以这个数
        **等于**「我调人」合计，也**等于**真实发生的 RPC 次数（不是 2 倍）。"""
        return sum(r.access_as_callee for r in self.roles)

    @property
    def total_access_caller(self) -> int:
        return sum(r.access_as_caller for r in self.roles)

    @property
    def total_access_external(self) -> int:
        return sum(r.access_external for r in self.roles)

    @property
    def total_access_own(self) -> int:
        return sum(r.access_own for r in self.roles)

    @property
    def total_access_unknown(self) -> int:
        return sum(r.access_unknown for r in self.roles)


def build_report(wit_dir: str = ".wit", roles: Iterable[str] | None = None) -> Report:
    if roles is None:
        roles = discover_roles(wit_dir)
    return Report(
        wit_dir=wit_dir,
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        roles=[compute_role(wit_dir, r) for r in roles],
    )


# ── 渲染 ────────────────────────────────────────────────────────────────────


def render_text(rep: Report) -> str:
    L: list[str] = []
    bar = "═" * 78
    L.append(bar)
    L.append(" 附件2「五、应用成效 · 2 量化成效分析」四项指标")
    L.append(f" 数据目录 {os.path.abspath(rep.wit_dir)}")
    L.append(f" 生成时间 {rep.generated_at}")
    L.append(bar)

    # —— 指标 1 ——
    L.append("")
    L.append("【指标 1】智能体接入响应速度")
    L.append("  口径：本端作为**发起方**发出 start 命令 → 收到对端首个回执（本端时钟差）")
    L.append("  范围：仅 direction=outbound；被调用方视角的时延须由调用方测量，本表不臆造")
    any_row = False
    empty: list[str] = []
    for m in rep.roles:
        if not m.access_ms:
            empty.append(m.role)
            continue
        any_row = True
        L.append(f"    {m.role:<12} {_fmt_stats(_stats(m.access_ms))}")
    if any_row:
        L.append(f"    {'合计':<12} {_fmt_stats(_stats(rep.all_access_ms))}")
    else:
        L.append("    （无样本：需要至少一次由本端发起的成功互联任务）")
    if empty:
        L.append(f"    （未作为发起方、故无样本：{', '.join(empty)}）")

    # —— 指标 2 ——
    L.append("")
    L.append("【指标 2】通信时延")
    L.append("  口径：本端作为**发起方**，非 start 命令（get/complete）→ 其后首条响应")
    L.append("  范围：仅 direction=outbound（原因同指标 1）")
    any_row = False
    empty = []
    for m in rep.roles:
        if not m.comm_ms:
            empty.append(m.role)
            continue
        any_row = True
        L.append(f"    {m.role:<12} {_fmt_stats(_stats(m.comm_ms))}")
    if any_row:
        L.append(f"    {'合计':<12} {_fmt_stats(_stats(rep.all_comm_ms))}")
    else:
        L.append("    （无样本）")
    if empty and any_row:
        L.append(f"    （未作为发起方、故无样本：{', '.join(empty)}）")

    # —— 指标 3 ——
    L.append("")
    L.append("【指标 3】并发协同数量")
    L.append("  3.1 本端作为发起方（同时向外发起几路协同）")
    L.append(
        f"    {'角色':<12}{'委派数':>8}{'伙伴数':>8}{'峰值并发':>10}"
        f"{'会话数':>8}{'单会话最大并行':>16}"
    )
    for m in rep.roles:
        L.append(
            f"    {m.role:<12}{m.out_tasks:>8}{len(m.partners):>8}"
            f"{m.out_peak_concurrency:>10}{len(m.sessions):>8}"
            f"{m.peak_partners_per_session:>16}"
        )
    L.append(f"    {'全项目去重伙伴':<12}{'':>8}{len(rep.partners):>8}")
    retired = sorted({p for m in rep.roles for p in m.retired_partners})
    if retired:
        L.append(
            f"    ⚠ 另有 {len(retired)} 个对端属于**已退役的历史 demo**"
            f"（曾参与自测、后已从平台删除），未计入伙伴数："
        )
        for a in retired:
            L.append(f"        {_short_aic(a)}")
    unknown_peers = sum(m.unknown_partners for m in rep.roles)
    if unknown_peers:
        L.append(
            f"    ⚠ 另有 {unknown_peers} 条 outbound 任务**对端 AIC 缺失**（空/NULL），"
            "未计入伙伴数（待排查）"
        )
        L.append("        —— 与「访问次数」同一条纪律：最敏感的数字宁可少报不可多报。")

    L.append("  3.2 本端作为被调用方（同时被几路调用）")
    any_in = False
    L.append(f"    {'角色':<12}{'受理数':>8}{'被调用峰值并发':>16}")
    for m in rep.roles:
        if m.in_tasks:
            any_in = True
            L.append(f"    {m.role:<12}{m.in_tasks:>8}{m.in_peak_concurrency:>16}")
    if not any_in:
        L.append("    （暂无被调用记录）")

    # —— 指标 4 ——
    L.append("")
    L.append("【指标 4】梧桐平台累计访问次数")
    L.append("  口径：本端作为被调用方受理的任务数（direction=inbound）")
    L.append("  拆分：自家自测 / 外部真实访问 / 来源未知（调用方 AIC 缺失，不得计入平台访问）")
    L.append(
        f"    {'角色':<12}{'被访问':>7}{'来自自家':>10}{'来自外部':>10}{'来源未知':>10}"
        f"{'非成功':>8}"
    )
    for m in rep.roles:
        L.append(
            f"    {m.role:<12}{m.inbound_total:>7}{m.self_test_calls:>10}"
            f"{m.external_calls:>10}{m.unknown_calls:>10}{m.inbound_bad:>8}"
        )
    L.append(
        f"    {'合计':<12}{rep.total_inbound:>7}{rep.total_selftest:>10}"
        f"{rep.total_external:>10}{rep.total_unknown:>10}{rep.total_inbound_bad:>8}"
    )
    L.append(
        "    「非成功」= 终态 failed / canceled：对端确实打进来了（仍算被访问），"
        "但不是成功受理。"
    )
    if rep.total_unknown:
        L.append(
            f"    ⚠ 有 {rep.total_unknown} 条入站任务的调用方 AIC 缺失，已单列「来源未知」；"
            "不得计入平台访问量，请查任务库的 remote_aic 为何为空。"
        )

    ext = [
        (m.role, aic, n)
        for m in rep.roles
        for aic, n in m.external_detail()
    ]
    L.append("")
    if ext:
        L.append("  ★ 外部真实访问明细（这才是「平台访问」的硬证据）")
        for role, aic, n in ext:
            # ⚠️ 按**调用方**取，不能按角色取 —— 一个角色可能有多个外部调用方，
            #    按角色取会把总数挂到每一个调用方名下，凭空放大。
            bad = next(
                (m.inbound_bad_by_caller.get(aic, 0) for m in rep.roles
                 if m.role == role),
                0,
            )
            flag = ""
            if bad:
                flag = (
                    f"   ⚠ 其中 **{bad} 次非成功终态**（failed/canceled）"
                    f" ⇒ 成功的外部访问 = {n - bad} 次"
                )
            L.append(f"      {role:<12} ← {aic}   {n} 次{flag}")
        if rep.total_external_bad:
            L.append(
                f"  🔴 全项目外部入站 {rep.total_external} 次，其中非成功终态 "
                f"{rep.total_external_bad} 次 ⇒ **对外可报的「成功外部访问」= "
                f"{rep.total_external - rep.total_external_bad} 次**。"
            )
            L.append(
                "     把 canceled 也算成「平台访问」是最危险的一类错 —— "
                "数字是真的，结论是假的。宁可少报不可多报。"
            )
    else:
        L.append("  ⚠ 暂无外部调用记录：入站任务全部来自自家 AIC。")
        L.append("    （自测流量不能当「平台访问量」报，需等外部/平台调用后再取数）")

    for m in rep.roles:
        if m.inbound_window[0]:
            L.append(f"      {m.role:<12} 时间窗 {m.inbound_window[0]} .. {m.inbound_window[1]}")

    # —— 指标 4 的对账方：AMP 访问日志 ——
    L.append("")
    L.append("【对账】AMP 访问日志（RPC 级）—— 用于对账，**不能**直接当访问次数报")
    L.append("  来源：.wit/<角色>/logs/amp_access_*.jsonl（AMP 自己写的 OTel 形态台账）")
    L.append("  性质：一次 RPC 一条，且**双方文件各记一条** ⇒ 全项目「被调用」合计")
    L.append("        与「我调人」合计应当相等，二者都等于真实发生的 RPC 次数。")
    L.append("  折算：剔掉**现场派生的**固定大小轮询报文后 = 任务提交数，可与指标 4 对账。")
    L.append("")
    L.append(
        f"    {'角色':<16}{'条目':>6}{'被调用':>8}{'我调人':>8}"
        f"{'轮询大小':>10}{'任务提交':>10}{'任务库受理':>12}{'差值':>7}"
    )
    for m in rep.roles:
        acc = m.access
        if not acc:
            L.append(f"    {m.role:<16}  （无 access 日志文件）")
            continue
        sub = m.access_submissions
        sub_s = "无法区分" if sub is None else str(sub)
        diff = "" if sub is None else f"{sub - m.inbound_total:+d}"
        ps = acc.get("poll_size")
        L.append(
            f"    {m.role:<16}{m.access_lines:>6}{m.access_as_callee:>8}"
            f"{m.access_as_caller:>8}"
            f"{('（未派生）' if ps is None else str(ps)):>10}"
            f"{sub_s:>10}{m.inbound_total:>12}{diff:>7}"
        )
    L.append(
        f"    {'合计':<16}{rep.total_access_lines:>6}{rep.total_access_callee:>8}"
        f"{rep.total_access_caller:>8}{'':>10}{'':>10}{rep.total_inbound:>12}{'':>7}"
    )

    acc_ext = [
        (m.role, a, n)
        for m in rep.roles
        for a, n in (m.access or {}).get("by_caller", {}).items()
        if _is_external(a)
    ]
    L.append("")
    L.append(
        f"    被访问来源（access 侧，仅统计被调用的那些条目）："
        f"自家 {rep.total_access_own} / 外部 {rep.total_access_external} "
        f"/ 未知 {rep.total_access_unknown}"
    )
    if acc_ext:
        for role, aic, n in acc_ext:
            L.append(f"      ★ {role:<14} ← {aic}   {n} 次（RPC 级）")
    else:
        L.append("      （access 侧无外部调用方：入站 RPC 全部来自自家 AIC）")

    L.append("")
    L.append("  ⚠ 三条必须一起读的结论：")
    L.append("    1. 本表证明的是「**本端**记录到了这些访问」，**不是**「平台上收到了」。")
    L.append("       实测：AMP SDK 运行时没有 upload/send 之类的方法，其字节码里也")
    L.append("       检索不到 http / ingest / telemetry / otlp 等字样；Registry 的")
    L.append("       OpenAPI 里同样没有访问量/排行榜接口 ⇒ **未发现上报通道**。")
    L.append("       平台侧计数只能以平台自己的界面为准，**不要**由本表推断。")
    L.append("    2. 「差值」为负 ⇒ access 日志**不完整**（旧条目被轮转/截断），")
    L.append("       此时**任务库才是权威**。宁可少报不可多报，方向不能反。")
    all_status = sorted(
        {c for m in rep.roles for c in (m.access or {}).get("status", {})}
    )
    if all_status == ["200"]:
        L.append("    3. 本轮 access 日志里**只有 200** —— 所以它**不能**用来证明")
        L.append("       「失败调用数」；失败要回任务库看 state / local_error。")

    # —— 佐证 ——
    L.append("")
    L.append("【佐证】AMP 心跳（在线时长）与任务终态分布")
    for m in rep.roles:
        hb = m.heartbeat
        hb_s = (
            f"心跳 {hb['samples']} 条 / 在线 {hb['span_hours']}h"
            if hb
            else "无心跳日志"
        )
        states = ", ".join(f"{k}×{v}" for k, v in sorted(m.state_counts.items()))
        L.append(f"    {m.role:<12} {hb_s}")
        if states:
            L.append(f"    {'':<12} 终态：{states}")
    L.append("")
    L.append(bar)
    L.append(" 提示：本表由 tools/metrics.py 只读生成；口径说明见该文件顶部 docstring。")
    L.append("       对外提交时请如实区分「自测流量」与「外部访问」，勿把前者报成平台访问量。")
    L.append(bar)
    return "\n".join(L)


def render_markdown(rep: Report) -> str:
    L: list[str] = []
    L.append("### 量化运行指标（附件2 §五.2）")
    L.append("")
    L.append(
        "> ⚠️ 本文件由 `python -m tools.metrics --md` **自动生成**；"
        "数据会随真实调用增长，**提交前请重跑刷新**："
    )
    L.append("> ```bash")
    L.append("> ./.venv/bin/python -m tools.metrics --md --out docs/aicomp/量化指标快照.md")
    L.append("> ```")
    L.append("")
    L.append(f"> 数据来源：`aip-tasks.sqlite3`（任务库）+ `amp_heartbeat_*.jsonl`（AMP 心跳）  ")
    L.append(f"> 生成时间：{rep.generated_at}")
    L.append("")

    L.append("| 指标 | 口径 | 样本数 | P50 | P95 | 均值 |")
    L.append("|---|---|---:|---:|---:|---:|")
    a, c = _stats(rep.all_access_ms), _stats(rep.all_comm_ms)
    if a:
        L.append(
            f"| 智能体接入响应速度 | 本端作为发起方：start 命令 → 首个回执（本端时钟） | {int(a['n'])} "
            f"| {a['p50']:.1f} ms | {a['p95']:.1f} ms | {a['mean']:.1f} ms |"
        )
    else:
        L.append("| 智能体接入响应速度 | start 命令 → 首个回执（本端时钟） | 0 | — | — | — |")
    if c:
        L.append(
            f"| 通信时延 | 本端作为发起方：get/complete 命令 → 其后首条响应（本端时钟） | {int(c['n'])} "
            f"| {c['p50']:.1f} ms | {c['p95']:.1f} ms | {c['mean']:.1f} ms |"
        )
    else:
        L.append("| 通信时延 | get/complete 命令 → 其后首条响应（本端时钟） | 0 | — | — | — |")
    L.append(
        f"| 并发协同数量 | 峰值并发任务 / 不同伙伴 AIC | {len(rep.partners)} 个伙伴 "
        f"| — | — | 峰值 {max((m.out_peak_concurrency for m in rep.roles), default=0)} 并发 |"
    )
    L.append(
        f"| 平台累计访问次数 | inbound 任务数（含自家/外部拆分） | {rep.total_inbound} "
        f"| — | — | 外部 {rep.total_external} / 自测 {rep.total_selftest}"
        + (f" / 未知 {rep.total_unknown}" if rep.total_unknown else "")
        + " |"
    )
    L.append("")
    L.append(
        "> 说明：时延两项**只统计本端作为发起方**的互联任务（口径见 `tools/metrics.py`）。"
        "被调用方视角的时延需由调用方测量，本表不用跨时钟数据臆造。"
    )
    L.append("")

    L.append("#### 各角色明细")
    L.append("")
    L.append(
        "| 角色 | 委派数 | 伙伴数 | 峰值并发 | 会话数 | 单会话最大并行 "
        "| 受理数 | 被调用峰值 | 被访问 | 自家 | 外部 | 未知 | 非成功 |"
    )
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for m in rep.roles:
        L.append(
            f"| {m.role} | {m.out_tasks} | {len(m.partners)} | {m.out_peak_concurrency} "
            f"| {len(m.sessions)} | {m.peak_partners_per_session} "
            f"| {m.in_tasks} | {m.in_peak_concurrency} "
            f"| {m.inbound_total} | {m.self_test_calls} | {m.external_calls} "
            f"| {m.unknown_calls} | {m.inbound_bad} |"
        )
    L.append("")
    L.append(
        "> 「非成功」= 终态 failed / canceled（对端确实打进来了，仍算被访问，"
        "但不是成功受理）。"
    )
    L.append("")

    ext = [(m.role, aic, n) for m in rep.roles for aic, n in m.external_detail()]
    if ext:
        L.append("#### 外部真实访问明细")
        L.append("")
        L.append("| 被调用方 | 调用方 AIC | 次数 | 其中非成功 |")
        L.append("|---|---|---:|---:|")
        for role, aic, n in ext:
            bad = next(
                (m.inbound_bad_by_caller.get(aic, 0) for m in rep.roles
                 if m.role == role),
                0,
            )
            L.append(f"| {role} | `{aic}` | {n} | {bad} |")
        L.append("")
        if rep.total_external_bad:
            L.append(
                f"> 🔴 外部入站合计 {rep.total_external} 次，其中非成功终态 "
                f"{rep.total_external_bad} 次 ⇒ **对外可报的「成功外部访问」= "
                f"{rep.total_external - rep.total_external_bad} 次**。"
                "把 canceled 也算成平台访问是最危险的一类错：数字是真的，结论是假的。"
            )
            L.append("")
        else:
            L.append(
                f"> 外部入站合计 {rep.total_external} 次，无非成功终态。"
            )
            L.append("")

    # —— AMP 访问日志对账（RPC 级）——
    L.append("#### 对账：AMP 访问日志（RPC 级）")
    L.append("")
    L.append(
        f"全项目合计：**{rep.total_access_lines} 条**记录，其中被调用 "
        f"**{rep.total_access_callee}** 条、我调人 **{rep.total_access_caller}** 条"
        "（一次 RPC 双方各记一条，故二者应当相等）。"
    )
    L.append("")
    L.append("| 角色 | 条目 | 被调用 | 我调人 | 轮询大小 | 任务提交 | 任务库受理 | 差值 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for m in rep.roles:
        acc = m.access
        if not acc:
            L.append(f"| {m.role} | — | — | — | — | — | {m.inbound_total} | 无日志 |")
            continue
        sub = m.access_submissions
        sub_s = "无法区分" if sub is None else str(sub)
        diff = "—" if sub is None else f"{sub - m.inbound_total:+d}"
        ps = acc.get("poll_size")
        ps_s = "未派生" if ps is None else str(ps)
        L.append(
            f"| {m.role} | {m.access_lines} | {m.access_as_callee} "
            f"| {m.access_as_caller} | {ps_s} | {sub_s} | {m.inbound_total} | {diff} |"
        )
    L.append("")
    L.append("> ⚠️ 三点口径，缺一不可：")
    L.append(">")
    L.append(
        "> 1. **本表是 RPC 级，不是任务级**。对外报「平台累计访问次数」以"
        "「各角色明细」里的**受理数**（任务库 inbound）为准；本表只用于对账。"
    )
    L.append(
        "> 2. **它证明的是本端记录到了访问，不证明平台上报**。实测 AMP SDK 无上报方法、"
        "字节码无 http/ingest/telemetry 字样、Registry OpenAPI 无访问量接口 ⇒ "
        "**未发现上报通道**，平台侧计数请以平台界面为准。"
    )
    L.append(
        f"> 3. **差值为负 = access 日志不完整**（旧条目被轮转/截断），"
        f"此时任务库是权威。被访问来源（access 侧）：自家 {rep.total_access_own} / "
        f"外部 {rep.total_access_external} / 未知 {rep.total_access_unknown}。"
    )
    L.append("")
    return "\n".join(L)


def render_json(rep: Report) -> str:
    def role_obj(m: RoleMetrics) -> dict[str, Any]:
        return {
            "role": m.role,
            "db_path": m.db_path,
            "tasks": m.readings,
            "tasks_as_initiator": m.out_tasks,
            "tasks_as_callee": m.in_tasks,
            "state_counts": m.state_counts,
            "access_response_ms": _stats(m.access_ms),
            "comm_latency_ms": _stats(m.comm_ms),
            "concurrency": {
                "as_initiator": {
                    "tasks": m.out_tasks,
                    "partners": sorted(m.partners),
                    "partner_count": len(m.partners),
                    "retired_partners": sorted(m.retired_partners),
                    "unknown_partner_tasks": m.unknown_partners,
                    "sessions": len(m.sessions),
                    "peak_concurrent_tasks": m.out_peak_concurrency,
                    "peak_partners_per_session": m.peak_partners_per_session,
                },
                "as_callee": {
                    "tasks": m.in_tasks,
                    "peak_concurrent_tasks": m.in_peak_concurrency,
                },
            },
            "access_count": {
                "inbound_total": m.inbound_total,
                "by_caller": m.inbound_by_caller,
                "by_state": m.inbound_by_state,
                "from_self": m.self_test_calls,
                "from_external": m.external_calls,
                "from_unknown": m.unknown_calls,
                "non_success_total": m.inbound_bad,
                "non_success_from_external": m.external_bad,
                "window": list(m.inbound_window),
            },
            "heartbeat": m.heartbeat,
            # RPC 级台账，**只用于对账**；折算任务级用 task_submissions（可为 None）
            "access_log": m.access,
        }

    payload = {
        "generated_at": rep.generated_at,
        "wit_dir": os.path.abspath(rep.wit_dir),
        "own_aic_prefix": OWN_AIC_PREFIX,
        "totals": {
            "access_response_ms": _stats(rep.all_access_ms),
            "comm_latency_ms": _stats(rep.all_comm_ms),
            "tasks": sum(m.readings for m in rep.roles),
            "partners": len(rep.partners),
            "peak_concurrency": max(
                (m.out_peak_concurrency for m in rep.roles), default=0
            ),
            "inbound_total": rep.total_inbound,
            "inbound_from_self": rep.total_selftest,
            "inbound_from_external": rep.total_external,
            "inbound_from_unknown": rep.total_unknown,
            "inbound_non_success": rep.total_inbound_bad,
            "inbound_external_success": rep.total_external - rep.total_external_bad,
            # —— AMP 访问日志侧（RPC 级，对账用）——
            "access_log": {
                "lines": rep.total_access_lines,
                "as_callee": rep.total_access_callee,
                "as_caller": rep.total_access_caller,
                "from_self": rep.total_access_own,
                "from_external": rep.total_access_external,
                "from_unknown": rep.total_access_unknown,
                # ⚠️ 本表不声明"已上报平台"：实测未发现上报通道，见 render_text 的说明
                "reported_to_platform": None,
            },
        },
        "roles": [role_obj(m) for m in rep.roles],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tools.metrics",
        description="导出附件2 §五.2 四项量化指标（只读，不改任何数据）",
    )
    ap.add_argument(
        "--wit",
        default=".wit",
        help="wit 工作目录（默认 .wit）。里面应有 <角色>/acps/aip-tasks.sqlite3",
    )
    ap.add_argument(
        "--role",
        action="append",
        default=None,
        help="只看指定角色，可重复。默认分析目录下所有带任务库的角色",
    )
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    ap.add_argument("--md", action="store_true", help="输出 Markdown（可直接贴技术报告）")
    ap.add_argument("--out", default=None, help="把结果写入文件（utf-8）")
    args = ap.parse_args(argv)

    wit_dir = args.wit
    if not os.path.isdir(wit_dir):
        print(
            f"找不到 wit 目录：{os.path.abspath(wit_dir)}\n"
            "  本地通常没有任务库（数据在服务器上）。请在项目根目录用：\n"
            "      ./.venv/bin/python -m tools.metrics\n"
            "  或显式指定：--wit <路径>",
            file=sys.stderr,
        )
        return 2

    roles = args.role if args.role else None
    rep = build_report(wit_dir, roles)

    if args.json:
        text = render_json(rep)
    elif args.md:
        text = render_markdown(rep)
    else:
        text = render_text(rep)

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        print(f"已写入 {os.path.abspath(args.out)}")
    else:
        print(text)

    # 退出码：无任何数据时给 1，方便脚本里判断
    has_data = any(m.readings for m in rep.roles)
    return 0 if has_data else 1


if __name__ == "__main__":
    raise SystemExit(main())
