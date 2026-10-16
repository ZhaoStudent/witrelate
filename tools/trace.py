"""结论溯源表（机制 5）—— 把一次研判从**输入原文**到**最终结论**之间的每一跳
跨智能体交互，落成一份**第三方拿着任务库就能独立复算**的表。

设计文档：`docs/aicomp/结论溯源表设计.md`（本文件是它的实现）。
它和 `output/verdict-*.json` 的根本区别：verdict 只说「结论是什么」，
本表要说「结论**凭什么**、**经谁的手**、**每一步在哪条记录里**」。

三条不可让步的纪律（写进 schema，不是写在注释里）
════════════════════════════════════════════════
1. **每条跳都带 `task_ref` + `session_id`** ⇒ 任何人可回任务库逐字复核。
2. **`chain_edges` 必带 `evidence`，且不许出现没有 `why` 的 `inferred`。**
   实测（2026-10-03）：一跳两端有硬键 `task_id`（leader/outbound 与
   fact-judge/inbound 的 task_id 完全一致）；但**跨层没有硬键** ——
   外层 session 与内层 session 是两个不同的值，只能靠时间包含关系推断。
   所以跨层归属一律标 `inferred`，并在 `why` 里写清判据。
3. **`gaps` 是必需字段**：宁可列出"这一项找不到来源"，也不许静默留空 ——
   空着会被读成"没问题"。

口径（**本文件最要紧的部分**）
════════════════════════════════
· **唯一的任务库 reader** 是 `tools/metrics.py::_read_tasks`（本文件不另写第二份，
  同一事实读两遍必然漂移）。缺口只是它没取 `task_id` / `products`，已在那侧做加法扩展。
· **数据来源**：`.wit/<角色>/acps/aip-tasks.sqlite3` 与 `.wit/<角色>/acps/` 里的
  会话表 —— 都是**平台落盘**的；我方只落 `output/verdict-<TS>.meta.json` 这一个
  sidecar（见 `legal_acps/agents.py`），且它只提供"这份 verdict 属于哪一局"的握手。
· **不采用**：`DebateChainGuardHooks.dispatched` 等**内存态**、任何运行期 `print` 日志。
  它们是给运行期护栏用的，重启即失、第三方无法验证，不构成证据。
· **耗时**只从 `aip_tasks.created_at/updated_at` 与命令/事件时刻算，不另算一份。
· **字段溯源是内容驱动的**：拿 verdict 的**实际字段名**去各跳**对端返回的产物原文**里找
  （字段名现场派生，不写死映射表 —— 写死的表加一个字段必漏）。
  找得到 ⇒ `partner_product` / `hard`（附命中片段，可自行 grep 复核）；
  找不到 ⇒ `leader_synthesis` / `derived`，并**同时进 `gaps`**。

用法
════
    ./.venv/bin/python -m tools.trace --latest              # 最近一局，终端
    ./.venv/bin/python -m tools.trace --latest --md         # Markdown（贴技术方案）
    ./.venv/bin/python -m tools.trace --latest --html       # 自包含 HTML（可截图）
    ./.venv/bin/python -m tools.trace --latest --json       # 机读
    ./.venv/bin/python -m tools.trace --session session-…   # 指定局
    ./.venv/bin/python -m tools.trace --verdict output/verdict-….json
    ./.venv/bin/python -m tools.trace --latest --evidence   # 附带命令/事件原文

退出码：`0` 成功；`2` **找不到该局（明确失败，不产半成品）**。
本文件**只读**，纯标准库（`sqlite3` + `json`），与 `tools/metrics.py` 一致。
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from tools.aip_sessions import (
    own_aic,
    read_sessions,
    role_db_path,
    sessions_covering,
)
from tools.metrics import (
    TaskRow,
    _parse_ts,
    _short_aic,
    discover_roles,
    _read_tasks,
)

__all__ = [
    "MAX_DEPTH",
    "SCHEMA",
    "TraceReport",
    "build_trace",
    "main",
    "render_html",
    "render_json",
    "render_markdown",
    "render_text",
]

SCHEMA = "legal-acps/trace@1"

#: 跨层归属最多下钻几层。本项目的真实链深是 3（leader → fact-judge → 辩手），
#: 设成 3 是"够用且不会把无关任务卷进来"的折中；超出的层会在 warnings 里点名。
MAX_DEPTH = 3

#: 一跳耗时超过它就标 `slow`（只是标注，不影响溯源事实）。
SLOW_MS = 30_000

#: 没有 sidecar 时，用 verdict 时间戳去平台上"认领"一局的时间容差（秒）。
#: 会话窗口由平台写，verdict 文件名时刻由我们写；两边对时可能有秒级差。
DEFAULT_TOL_S = 90.0

_OUTPUT_REL = "output"


# ── 数据结构 ────────────────────────────────────────────────────────────────


@dataclass
class Hop:
    """一跳跨智能体委派。**全部字段都是任务库里的事实。**"""

    seq: int
    depth: int
    from_role: str
    from_aic: str
    to_role: str
    to_aic: str
    task_ref: str
    task_id: str
    session_id: str
    state: str
    sent_at: str
    last_event_at: str
    latency_ms: int | None
    request_excerpt: str
    product_ids: list[str]
    events: int
    health: str
    #: `hard` = 对端入站行按 task_id 配上；`unmatched` = 配不上（对端未落库）。
    pairing: str
    #: 本跳的上级 task_ref（depth>0 时非空）—— 跨层归属是推断，见 chain_edges。
    parent_task_ref: str
    commands: list[dict[str, Any]] = field(default_factory=list)
    event_rows: list[dict[str, Any]] = field(default_factory=list)
    #: 对端交回的产物原文（`aip_tasks.products`）。字段溯源拿它去找字段名。
    products: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ChainEdge:
    """跨层归属。**永远是 inferred** —— 实测无协议级硬键。"""

    from_task_ref: str
    to_session_id: str
    relation: str
    evidence: str
    why: str
    ambiguous: bool = False


@dataclass
class FieldSource:
    """verdict 的一个字段从哪来。"""

    field: str
    value_preview: str
    source: dict[str, Any]
    evidence: str  # hard | derived


@dataclass
class RunInfo:
    session_id: str
    verdict_file: str
    subject: str
    started_at: str
    finished_at: str
    wall_ms: int | None
    resolution: str  # sidecar | inferred
    basis: str


@dataclass
class TraceReport:
    generated_at: str
    wit_dir: str
    source_files: list[str]
    run: RunInfo
    hops: list[Hop]
    chain_edges: list[ChainEdge]
    verdict_fields: list[FieldSource]
    gaps: list[dict[str, str]]
    warnings: list[str]

    @property
    def inferred_edges(self) -> list[ChainEdge]:
        return [e for e in self.chain_edges if e.evidence == "inferred"]

    @property
    def hard_fields(self) -> list[FieldSource]:
        return [f for f in self.verdict_fields if f.evidence == "hard"]


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


def _ms_between(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return int(round((b - a).total_seconds() * 1000.0))


def _utc(dt: datetime | None) -> datetime | None:
    """统一成 UTC（naive 一律**当作** UTC）—— **只用于排序/取最值**，不要拿它显示。

    ⚠️ 为什么非要这一步：`sent_at` / `last_event_at` 是**字符串**，直接
    `min()` / `max()` 是**字典序**，不是时间序。实测（2026-10-03）平台眼下
    统一写 `+00:00`，字典序**恰好**等于时间序 —— 但这只是巧合：
    只要有一行写成 `Z`、或带上本地偏移 `+08:00`，字典序就会把**更早**的时刻
    判成**更晚**，于是本表的总耗时与泳道横轴悄悄错掉，而页面照样长得正常。
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _preview(text: Any, limit: int = 60) -> str:
    """压成单行短串。

    ⚠️ 必须用 `is None` 判空，**不能写 `text or ""`** —— 后者会把**假值抹掉**：
    `False` / `0` 都是假值，于是 `is_incitement=False` 与 `harm_index=0` 会被显示成
    **空白**，读起来像"这一项没有值"。这是本项目明令禁止的那类"静默丢数字"。
    """
    s = " ".join(("" if text is None else str(text)).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _md_cell(x: Any, limit: int = 400) -> str:
    """Markdown **表格单元格**：把竖线转义掉。

    ⚠️ 不转义的话，值里一个 `|` 就把这一行**拆成两行** —— 页面看着只是"多了一行怪东西"，
    读者却会把后半截当成新的一行数据（本项目最怕的"看起来正常、其实是另一回事"）。
    换行由 `_preview()` 压掉；竖线得自己处理。
    """
    return _preview(x, limit).replace("|", "\\|")


def _text_payload(items: Any) -> str:
    """从 `dataItems` 里取**真正的正文**。

    ⚠️ 曾经写成「递归收集 JSON 里所有字符串」—— 那是错的：一个文本项的形态是
    `{"type": "text", "text": "正文"}`，递归会把 `"type"` 的**值** `"text"`
    也当成正文收进来，于是"请求原文"前面凭空多出一行 `text`。
    它看起来只是"多一行"，实质是**假证据** —— 本模块的纪律是
    「取不到就如实返回空串，**不拿别的字段凑**」。
    （回归：`tools/verify_trace.py` 用例 I 把这句钉死。）

    只认 `text` 键的值；退化形态（项本身就是字符串）也用，别的键一律不要。
    """
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for it in items:
        if isinstance(it, dict):
            t = it.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t)
        elif isinstance(it, str) and it.strip():
            parts.append(it)
    return "\n".join(parts)


def _request_excerpt(row: TaskRow) -> str:
    """从 `start` 命令的 `dataItems` 里取请求原文。

    实测结构（2026-10-03，服务器）：
        {"type":"task-command","command":"start","sentAt":…,"senderRole":"leader",
         "taskId":…,"sessionId":…,"dataItems":[{"type":"text","text":"…"}]}
    取不到就如实返回空串 —— **不拿别的字段凑**（凑出来的"请求原文"是假证据）。
    """
    for c in row.commands:
        if str(c.get("command") or "") != "start":
            continue
        joined = _text_payload(c.get("dataItems"))
        if joined.strip():
            return joined
    return ""


def _health(row: TaskRow, latency_ms: int | None) -> str:
    state = str(row.state or "")
    if state == "failed":
        return "failed"
    if state in ("working", "") or not row.events:
        return "pending"
    if latency_ms is not None and latency_ms > SLOW_MS:
        return "slow"
    if not row.products:
        return "pending"
    return "ok"


# ── 载入与局解析 ────────────────────────────────────────────────────────────


def _load_all(wit_dir: str) -> tuple[dict[str, list[TaskRow]], list[str], list[str]]:
    """读全部角色的任务库（只读）。

    返回 `(角色→任务, 实际读到的库路径, 读取失败的告警)`。

    ⚠️ **读不出来必须变成一条告警**，不许只是"这个角色没数据"。踩过：
    把某个角色的库文件写坏（或库被锁、schema 旧）后，`_read_tasks` 抛异常被
    `except` 吞掉 ⇒ 那个角色的任务**全部消失** ⇒ 链路凭空少一层，
    而输出照样像模像样；更糟的是唯一那条警告说"**配不到对端的入站行**" ——
    **把原因指到了错误的方向**，让人去查配对、而不是查那个读不出来的库。
    （回归：`tools/verify_trace.py` 用例 L 把这句钉死。）

    ⚠️ 但**库文件不存在不算失败**：那是"这个角色从来没在本机跑过"的正常形态
    （例如未部署的 evidence-* 角色），在它上面报警会让真告警被淹没。
    """
    by_role: dict[str, list[TaskRow]] = {}
    files: list[str] = []
    errors: list[str] = []
    for role in discover_roles(wit_dir):
        db = role_db_path(wit_dir, role)
        if not os.path.exists(db):
            by_role[role] = []
            continue
        try:
            by_role[role] = _read_tasks(db)
        except Exception as exc:  # noqa: BLE001 - 单个库坏掉不该让整份溯源表崩掉
            by_role[role] = []
            errors.append(
                f"角色 `{role}` 的任务库读不出来（{type(exc).__name__}: {exc}）"
                f" ⇒ 本表缺少该角色的全部记录，链路可能因此少层"
                f"（文件：{db.replace(os.sep, '/')}）。"
                "下面若出现「配不到对端的入站行」，很可能就是这个原因 —— "
                "先修这个库，再谈配对。"
            )
            continue
        files.append(db.replace(os.sep, "/"))
    return by_role, files, errors


def _aic_index(by_role: dict[str, list[TaskRow]]) -> dict[str, str]:
    """AIC → 角色。从各库自己的 `local_aic` 现场派生，**不硬编码任何 AIC**。"""
    idx: dict[str, str] = {}
    for role, tasks in by_role.items():
        for t in tasks:
            aic = (t.local_aic or "").strip()
            if aic:
                idx.setdefault(aic, role)
    return idx


def _inbound_by_task_id(by_role: dict[str, list[TaskRow]]) -> dict[str, tuple[str, TaskRow]]:
    """`task_id` → (角色, 入站行)。这是一跳两端配对的硬键。"""
    idx: dict[str, tuple[str, TaskRow]] = {}
    for role, tasks in by_role.items():
        for t in tasks:
            if t.is_inbound and t.task_id:
                idx.setdefault(t.task_id, (role, t))
    return idx


def _verdict_files(wit_dir: str) -> list[str]:
    """列 `output/` 下的 verdict 文件。

    ⚠️ **必须排掉 sidecar**：`verdict-<TS>.meta.json` 也以 `.json` 结尾，
    `glob("verdict-*.json")` 会把它一起捞进来 —— 于是"取最新一份"取到了 sidecar，
    再去找 `…meta.meta.json` 自然找不到，整局定不出来。
    （这正是本项目那条"最像样的产物掩盖真失败"的老毛病：不报错，只是空。）
    """
    root = os.path.normpath(os.path.join(wit_dir, "..", _OUTPUT_REL))
    return sorted(
        p for p in glob.glob(os.path.join(root, "verdict-*.json"))
        if not p.endswith(".meta.json")
    )


def _stamp_of(path: str) -> datetime | None:
    """从 `verdict-20261002T150142Z.json` 里取文件名上的 UTC 时刻。"""
    base = os.path.basename(path)
    if not base.startswith("verdict-") or not base.endswith(".json"):
        return None
    raw = base[len("verdict-"):-len(".json")]
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _sidecar_of(verdict_path: str) -> str:
    return verdict_path[: -len(".json")] + ".meta.json"


def _read_sidecar(verdict_path: str) -> dict[str, Any] | None:
    path = _sidecar_of(verdict_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass
class _Resolved:
    session_id: str
    verdict_path: str
    subject: str
    resolution: str
    basis: str
    warnings: list[str]


def _resolve_run(
    wit_dir: str,
    by_role: dict[str, list[TaskRow]],
    *,
    verdict: str | None,
    session: str | None,
    tol_s: float,
    owner_role: str = "leader",
) -> _Resolved | None:
    """定出「这一局」。**定不出来就返回 None（明确失败，不产半成品）。**"""
    warns: list[str] = []

    if session:
        sid = session if session.startswith("session-") else f"session-{session}"
        # ⚠️ 显式指定也要**验存在**：写错一个字符就得到一份"看起来像模像样"的空表，
        #    那正是本项目最怕的「静默给出一份看似成功的产物」。查不到就当找不到这一局。
        known = {s.session_id for role in by_role
                 for s in read_sessions(role_db_path(wit_dir, role))}
        known |= {t.session_id for tasks in by_role.values() for t in tasks}
        if sid not in known:
            return None
        # 找一个引用了该 session 的 verdict（有则补上终点信息）
        for path in reversed(_verdict_files(wit_dir)):
            sc = _read_sidecar(path)
            if sc and str(sc.get("session_id")) == sid:
                text = _read_verdict(path)
                return _Resolved(sid, path, str(text.get("subject") or ""),
                                 "sidecar", "由 sidecar 直接指定", warns)
        return _Resolved(sid, "", "", "explicit", "命令行 --session 指定", warns)

    path = verdict
    if not path:
        files = _verdict_files(wit_dir)
        if not files:
            return None
        path = files[-1]
    if not os.path.exists(path):
        return None

    text = _read_verdict(path)
    subject = str(text.get("subject") or "")

    sc = _read_sidecar(path)
    if sc and sc.get("session_id"):
        sid = str(sc["session_id"])
        return _Resolved(sid, path, subject, "sidecar",
                         f"sidecar {os.path.basename(_sidecar_of(path))}", warns)

    # —— 降级路径：用 verdict 的时间戳去平台上"认领"一局 ——
    #
    # ⚠️ **只认「本份 verdict 的产出角色」的会话**（默认 leader，verdict 就是它写的）。
    #    实测踩过：把所有角色的会话混在一起找，`fact-judge` 的**内层**会话窗口
    #    也会罩住同一时刻 —— 于是"认领"到了一个内层 session，整条链凭空少一层，
    #    而输出**照样像模像样**。这正是本项目最怕的「静默给出错误归属」。
    stamp = _stamp_of(path)
    if stamp is None:
        return None
    owner_db = role_db_path(wit_dir, owner_role)
    owner_aic_value = own_aic(owner_db)
    sessions = read_sessions(owner_db)
    if not sessions:
        warns.append(
            f"缺少 sidecar，且 {owner_role} 的任务库里没有会话表（aip_sessions）"
            " ⇒ 无法定出这一局"
        )
        return None

    # 两段式：先要求**严格包含**；只有找不到才放宽到容差。
    # 这样相邻两局的窗口不会因为容差而互相"抢"。
    cands = sessions_covering(sessions, stamp, tol_s=0.0)
    used_tol = 0.0
    if not cands:
        cands = sessions_covering(sessions, stamp, tol_s=tol_s)
        used_tol = tol_s
    if not cands:
        warns.append(
            f"缺少 sidecar，且 {owner_role} 的会话窗口都不覆盖 "
            f"{stamp.isoformat()}（容差 {tol_s:.0f}s） ⇒ 无法定出这一局"
        )
        return None
    if len(cands) > 1:
        warns.append(
            "缺少 sidecar，且 "
            f"{len(cands)} 个候选会话窗口覆盖该时刻（容差 {used_tol:.0f}s："
            + "、".join(f"…{c.session_id[-12:]}" for c in cands)
            + "）⇒ 取最外层（started 最早者）；本次归属为推断，请人工核对"
        )
    # 最外层 = 全局那一局。嵌套时内层窗口一定被外层包住，所以 started 最早的那个是外层。
    chosen = min(cands, key=lambda c: (c.created_at, c.session_id))
    warns.append(
        f"缺少 sidecar（{os.path.basename(_sidecar_of(path))} 不存在）⇒ "
        f"按 verdict 时刻在 {owner_role} 的平台会话表里认领到 "
        f"…{chosen.session_id[-12:]}"
        + (f"（owner=…{owner_aic_value[-14:]}）" if owner_aic_value else "")
        + f"；容差 {used_tol:.0f}s。本表把 verdict 归到哪一局靠推断，"
        "不是 sidecar 的协议级握手 —— 请核对后再引用。"
    )
    return _Resolved(chosen.session_id, path, subject, "inferred",
                     f"verdict 时刻 {stamp.isoformat()} 落在 {owner_role} 的"
                     f"平台会话窗口内（容差 {used_tol:.0f}s）", warns)


def _read_verdict(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ── 链路重建 ────────────────────────────────────────────────────────────────


def _window(row: TaskRow) -> tuple[datetime, datetime] | None:
    if row.created_at is None or row.updated_at is None:
        return None
    if row.updated_at < row.created_at:
        return None
    return row.created_at, row.updated_at


def _make_hop(
    seq: int,
    depth: int,
    row: TaskRow,
    *,
    to_role: str,
    roles_by_aic: dict[str, str],
    inbound_idx: dict[str, tuple[str, TaskRow]],
    parent: str,
) -> tuple[Hop, str]:
    """由一行 outbound 任务造一条跳。返回 (hop, pairing)。"""
    sent = row.start_sent_at()
    if sent is None:
        sent = row.created_at
    last_ev = max(
        (t for e in row.events for t in [_parse_ts(e.get("created_at"))] if t),
        default=None,
    )
    latency = _ms_between(sent, last_ev)
    pairing = "unmatched"
    if row.task_id and row.task_id in inbound_idx:
        pairing = "hard"
    if not to_role:
        to_role = roles_by_aic.get((row.remote_aic or "").strip(), "")
    hop = Hop(
        seq=seq,
        depth=depth,
        from_role=roles_by_aic.get((row.local_aic or "").strip(), ""),
        from_aic=row.local_aic,
        to_role=to_role,
        to_aic=row.remote_aic,
        task_ref=row.task_ref,
        task_id=row.task_id,
        session_id=row.session_id,
        state=str(row.state or ""),
        sent_at=_iso(sent),
        last_event_at=_iso(last_ev),
        latency_ms=latency,
        request_excerpt=_request_excerpt(row),
        product_ids=[str(p.get("id") or "") for p in row.products],
        events=len(row.events),
        health=_health(row, latency),
        pairing=pairing,
        parent_task_ref=parent,
        commands=row.commands,
        event_rows=row.events,
        products=list(row.products),
    )
    return hop, pairing


def _build_chain(
    by_role: dict[str, list[TaskRow]],
    session_id: str,
) -> tuple[list[Hop], list[ChainEdge], list[str]]:
    """从局的 session 出发，逐层重建链路。

    第 0 层：`session_id == 本局` 且 `direction == outbound` 的行（== 主控委派）。
    第 n+1 层：**接收方**的 outbound 行，且其时间窗**落**在父跳窗口内；
             这一步没有硬键 ⇒ 全部标推断，并检查是否**多候选**。
    """
    roles_by_aic = _aic_index(by_role)
    inbound_idx = _inbound_by_task_id(by_role)
    warns: list[str] = []
    hops: list[Hop] = []
    edges: list[ChainEdge] = []
    seq = 0

    layer0: list[TaskRow] = []
    for role, tasks in sorted(by_role.items()):
        for t in tasks:
            if not t.is_inbound and t.session_id == session_id:
                layer0.append(t)
    layer0.sort(key=lambda t: (t.created_at or datetime.min, t.task_ref))

    frontier: list[tuple[TaskRow, Hop]] = []
    for row in layer0:
        seq += 1
        recv_role = inbound_idx.get(row.task_id, ("", None))[0] if row.task_id else ""
        hop, _ = _make_hop(seq, 0, row, to_role=recv_role,
                           roles_by_aic=roles_by_aic, inbound_idx=inbound_idx,
                           parent="")
        hops.append(hop)
        frontier.append((row, hop))

    # ⚠️ `seen_sessions` 曾经是个**死守卫**：它只在"要不要 add"那一句上 `continue`，
    #    而真正造跳的循环在它下面照跑 —— 于是**主控在同一局里对同一个下游委派两趟**
    #    （重试 / 多轮）时，同一个内层会话会被每一个父跳各展开一遍：
    #    同一条任务被造成 4 条跳、层号一路涨到 `MAX_DEPTH`，
    #    连"跨智能体调用次数"也跟着虚增。而且它看起来完全正常。
    #    现在：会话一旦被某个父**认领**过就不再展开；被第二个父也罩住时，
    #    那是**跨父歧义**，必须标 `ambiguous` 并告警，不许静默挑一个。
    #    （回归：`tools/verify_trace.py` 用例 J 把这条钉死。）
    seen_sessions = {session_id}
    claimed_by: dict[str, str] = {}  # 子会话 → 认领它的父 task_ref
    for depth in range(1, MAX_DEPTH + 1):
        nxt: list[tuple[TaskRow, Hop]] = []
        for parent_row, parent_hop in frontier:
            win = _window(parent_row)
            target_role = parent_hop.to_role
            if win is None or not target_role or target_role not in by_role:
                continue
            lo, hi = win
            # ⚠️ 两个会话都要排掉：**本局会话**（跨层的前提就是换会话）与
            #    **父跳自己所在的会话**。只排前者会漏出一种自指边：
            #    父跳本身就在内层会话 INNER 里，而它的时间窗一定"落在自己之内"，
            #    于是同一会话里的那行会被记成"INNER 的子会话" ——
            #    `task_Jp → …INNER` 这种边没有任何含义，却会出现在表里。
            #    （回归：`tools/verify_trace.py` 用例 J 把这条钉死。）
            cands = [
                t for t in by_role[target_role]
                if not t.is_inbound
                and t.session_id != session_id
                and t.session_id != parent_row.session_id
                and _window(t) is not None
                and lo <= _window(t)[0] and _window(t)[1] <= hi
            ]
            if not cands:
                continue
            child_sessions = sorted({t.session_id for t in cands})
            # 两类歧义都算：① 同一个父的窗口里出现**多个**子会话；
            # ② **另一个父**也罩住了同一个子会话（两边都说得通，无法取舍）。
            others = {
                sid: claimed_by[sid] for sid in child_sessions
                if sid in claimed_by and claimed_by[sid] != parent_row.task_ref
            }
            ambiguous = len(child_sessions) > 1 or bool(others)
            for sid in child_sessions:
                edges.append(ChainEdge(
                    from_task_ref=parent_row.task_ref,
                    to_session_id=sid,
                    relation="parent-child",
                    evidence="inferred",
                    why=(
                        f"{target_role} 的该会话起止时间落在 {parent_row.task_ref} 的"
                        f"生命周期 [{_iso(lo)} .. {_iso(hi)}] 之内；"
                        "跨层无协议级硬键（外层 session ≠ 内层 session，实测）"
                        + (f"；⚠️ 会话 …{sid[-12:]} 同时落在 {others[sid]} 的窗口内"
                           if sid in others else "")
                    ),
                    ambiguous=ambiguous,
                ))
            if len(child_sessions) > 1:
                warns.append(
                    f"{parent_row.task_ref} 的窗口内，{target_role} 有 "
                    f"{len(child_sessions)} 个候选会话（"
                    + "、".join(f"…{s[-12:]}" for s in child_sessions)
                    + "）⇒ 归属存疑，已标 ambiguous，请人工核对"
                )
            if others:
                detail = "、".join(
                    f"…{sid[-12:]}（已被 {claimer} 认领）"
                    for sid, claimer in sorted(others.items())
                )
                warns.append(
                    f"有子会话同时落在多个父跳的窗口内：{detail} "
                    f"⇒ 本表把它记在最早认领的那个父名下，并已标 ambiguous；"
                    "跨层本来就没有硬键，这种情况请人工核对到底属于哪一趟。"
                )
            # ⚠️ 只展开**还没被认领过**的会话 —— 这是上面那条死守卫真正该做的事。
            fresh = [t for t in cands if t.session_id not in seen_sessions]
            fresh.sort(key=lambda t: (t.created_at or datetime.min, t.task_ref))
            for t in fresh:
                seq += 1
                recv_role = inbound_idx.get(t.task_id, ("", None))[0] if t.task_id else ""
                hop, _ = _make_hop(seq, depth, t, to_role=recv_role,
                                   roles_by_aic=roles_by_aic,
                                   inbound_idx=inbound_idx,
                                   parent=parent_row.task_ref)
                hops.append(hop)
                nxt.append((t, hop))
            for sid in child_sessions:
                claimed_by.setdefault(sid, parent_row.task_ref)
                seen_sessions.add(sid)
        if not nxt:
            break
        frontier = nxt

    # ⚠️ 跨父歧义是**事后**才看得全的：后处理的父才知道前面有人认领过。
    #    不回填的话，同一个会话的两条边会一个标 ambiguous、一个不标 ——
    #    读者会以为"没标的那条是确定的"。所以这里统一回填。
    per_session: dict[str, int] = {}
    for e in edges:
        per_session[e.to_session_id] = per_session.get(e.to_session_id, 0) + 1
    for e in edges:
        if per_session[e.to_session_id] > 1:
            e.ambiguous = True
            e.why += "；⚠️ 该子会话同时落在其它父跳的窗口内，归属存疑"

    unmatched = [h for h in hops if h.pairing != "hard"]
    if unmatched:
        warns.append(
            f"{len(unmatched)} 条跳在任务库里配不到对端的入站行"
            f"（task_id 未出现于任何角色的 inbound）："
            + "、".join(h.task_ref for h in unmatched[:6])
            + "。这些跳的接收方角色是按 AIC 反查的，属推断。"
        )
    return hops, edges, warns


# ── 结论字段溯源 ────────────────────────────────────────────────────────────


#: 产物**外壳**里由框架加的键。它们不是"下游说的内容"，是信封。
#: 实测（2026-07 起）：产物形态是
#: `{"id": "product-task-…", "name": "Agent response", "dataItems": […]}`
_ENVELOPE_KEYS = ("id", "name")

#: 词边界字符类（字母 / 数字 / 下划线 / 汉字）。
#: 汉字要算进去 —— 否则字段名 `结论` 会命中 `无结论性表述`。
_FIELD_BOUNDARY = r"[0-9A-Za-z_\u4e00-\u9fff]"


def _content_blob(prod: Any) -> str:
    """产物的**内容**（剔掉框架加的外壳键）。

    ⚠️ 为什么要剔：外壳里就有 `name` 键。verdict 里若恰好有个叫 `name`（或 `id`）
    的字段，整份产品 JSON 一搜就"命中"了信封 —— 这是**假的硬溯源**：
    看上去有出处，出处却是包装纸。
    （回归：`tools/verify_trace.py` 用例 K 把这句钉死。）
    """
    if not isinstance(prod, dict):
        return ""
    inner = {k: v for k, v in prod.items() if k not in _ENVELOPE_KEYS}
    return json.dumps(inner, ensure_ascii=False) if inner else ""


def _field_span(blob: str, name: str) -> tuple[int, int] | None:
    """在内容里按**词边界**定位字段名；找不到返回 None。

    ⚠️ 不许用 `name in blob` 这种纯子串匹配：`band` 会命中 `abandon`、
    `is_incitement` 会命中 `不是is_incitement性` —— 于是给出**假的硬溯源**，
    而 `[硬]` 这个标记正是本表全部说服力的来源。判据必须带词边界。
    （回归：`tools/verify_trace.py` 用例 K 把这句钉死。）

    ⚠️ 词边界是**启发式**，不是语法级的键匹配：产物正文是**模型自由文本**，
    字段名可能以 `truth_score = 50`、`"truth_score": 50`、`truth_score：50`
    等多种形态出现，所以不能用"必须是 JSON 键"那种严格判据（会把真命中打掉）。
    代价是仍可能偶发误命中 —— 因此 `hard` 一律**附命中片段**供人工复核，
    且渲染时写明"硬溯源"的含义是"字段名出现在下游产物里"，不是平台级认证。
    """
    if not name:
        return None
    m = re.search(
        rf"(?<!{_FIELD_BOUNDARY}){re.escape(name)}(?!{_FIELD_BOUNDARY})", blob
    )
    return (m.start(), m.end()) if m else None


def _attribute_fields(
    verdict: dict[str, Any], hops: list[Hop]
) -> tuple[list[FieldSource], list[dict[str, str]]]:
    """把 verdict 的**实际字段**逐个挂到证据上。

    ⚠️ **内容驱动，不写死映射表**：字段名现场派生（`verdict.keys()`），
    拿它去各跳**对端返回的产物原文**里找。写死的表加一个字段必漏，
    这正是本项目反复吃的亏（"同一事实散在多处 ⇒ 加角色必漏"）。

    ⚠️ 两条防腐判据（都是踩出来的，见 `_content_blob` / `_field_span`）：
    只搜**内容**（不搜产物外壳）、按**词边界**匹配（不做子串匹配）。
    """
    out: list[FieldSource] = []
    gaps: list[dict[str, str]] = []
    returned = [h for h in hops if h.product_ids]

    for name, value in verdict.items():
        hit: FieldSource | None = None
        for hop in hops:
            for prod in hop.products:
                pid = str(prod.get("id") or "")
                blob = _content_blob(prod)
                span = _field_span(blob, name) if blob else None
                if span is None:
                    continue
                idx = span[0]
                excerpt = _preview(blob[max(0, idx - 40): idx + 120], 160)
                hit = FieldSource(
                    field=name,
                    value_preview=_preview(value),
                    source={
                        "kind": "partner_product",
                        "role": hop.to_role or hop.to_aic,
                        "task_ref": hop.task_ref,
                        "product_id": pid,
                        "excerpt": excerpt,
                    },
                    evidence="hard",
                )
                break
            if hit:
                break
        if hit:
            out.append(hit)
            continue
        src = {
            "kind": "leader_synthesis",
            "based_on": [h.task_ref for h in returned],
            "note": "该字段名未出现在任何下游产物的内容里（按词边界匹配，不含产物外壳 id/name） ⇒ 由主控自己综合得出",
        }
        out.append(FieldSource(field=name, value_preview=_preview(value),
                               source=src, evidence="derived"))
        gaps.append({
            "field": name,
            "reason": ("链上没有任何下游产物的内容包含该字段名"
                       "（按词边界匹配，不含产物外壳 id/name）"
                       + (f"（本局共 {len(returned)} 跳返回了产物）" if returned
                          else "（本局没有任何下游产物返回：各跳均失败或超时）")),
        })
    return out, gaps


def build_trace(
    wit_dir: str = ".wit",
    *,
    verdict: str | None = None,
    session: str | None = None,
    tol_s: float = DEFAULT_TOL_S,
    owner_role: str = "leader",
) -> TraceReport | None:
    """组装一份溯源表；定不出这一局时返回 None。"""
    if not os.path.isdir(wit_dir):
        return None
    by_role, files, load_errors = _load_all(wit_dir)
    resolved = _resolve_run(wit_dir, by_role, verdict=verdict, session=session,
                            tol_s=tol_s, owner_role=owner_role)
    if resolved is None or not resolved.session_id:
        return None

    hops, edges, warns = _build_chain(by_role, resolved.session_id)
    # ⚠️ 顺序有意义：**读库失败排在最前** —— 它是根因，后面的"配不到对端"
    #    "缺少 sidecar" 都可能是它派生出来的。告警若按时间先后随便排，
    #    读的人会先去查那个派生现象。
    warns = list(load_errors) + list(resolved.warnings) + warns

    if not hops:
        warns.append(
            f"会话 …{resolved.session_id[-12:]} 在任务库里没有任何 outbound 行 ⇒ "
            "本局没有跨智能体调用可溯源"
        )

    verdict_path = resolved.verdict_path
    verdict = _read_verdict(verdict_path) if verdict_path else {}
    fields, gaps = _attribute_fields(verdict, hops) if verdict else ([], [])

    # ⚠️ 取最值必须**先解析再比**（`_utc`）。直接 min/max 字符串是字典序比较 ——
    #    平台现在统一 `+00:00` 所以恰好对得上，换个偏移形态就会静静取错端点。
    sent = [t for t in (_utc(_parse_ts(h.sent_at)) for h in hops) if t is not None]
    fin = [t for t in (_utc(_parse_ts(h.last_event_at)) for h in hops) if t is not None]
    started = _iso(min(sent)) if sent else ""
    finished = _iso(max(fin)) if fin else ""
    wall = _ms_between(_parse_ts(started), _parse_ts(finished))

    if not verdict_path:
        warns.append("本次没有绑定 verdict 文件 ⇒ 结论字段溯源为空（只重建了链路）")

    return TraceReport(
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        wit_dir=wit_dir,
        source_files=files,
        run=RunInfo(
            session_id=resolved.session_id,
            verdict_file=verdict_path.replace(os.sep, "/"),
            subject=resolved.subject,
            started_at=started,
            finished_at=finished,
            wall_ms=wall,
            resolution=resolved.resolution,
            basis=resolved.basis,
        ),
        hops=hops,
        chain_edges=edges,
        verdict_fields=fields,
        gaps=gaps,
        warnings=warns,
    )


def _to_dict(rep: TraceReport) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "generated_at": rep.generated_at,
        "source_files": rep.source_files,
        "run": {
            "session_id": rep.run.session_id,
            "verdict_file": rep.run.verdict_file,
            "subject": rep.run.subject,
            "started_at": rep.run.started_at,
            "finished_at": rep.run.finished_at,
            "wall_ms": rep.run.wall_ms,
            "resolution": rep.run.resolution,
            "basis": rep.run.basis,
        },
        "hops": [
            {
                "seq": h.seq, "depth": h.depth,
                "from": {"role": h.from_role, "aic": h.from_aic},
                "to": {"role": h.to_role, "aic": h.to_aic},
                "task_ref": h.task_ref, "task_id": h.task_id,
                "session_id": h.session_id, "state": h.state,
                "sent_at": h.sent_at, "last_event_at": h.last_event_at,
                "latency_ms": h.latency_ms,
                "request_excerpt": _preview(h.request_excerpt, 200),
                "product_ids": h.product_ids, "events": h.events,
                "health": h.health, "pairing": h.pairing,
                "parent_task_ref": h.parent_task_ref,
            }
            for h in rep.hops
        ],
        "chain_edges": [
            {
                "from_task_ref": e.from_task_ref,
                "to_session_id": e.to_session_id,
                "relation": e.relation,
                "evidence": e.evidence,
                "why": e.why,
                "ambiguous": e.ambiguous,
            }
            for e in rep.chain_edges
        ],
        "verdict_fields": [
            {"field": f.field, "value": f.value_preview,
             "source": f.source, "evidence": f.evidence}
            for f in rep.verdict_fields
        ],
        "gaps": rep.gaps,
        "warnings": rep.warnings,
    }


# ── 渲染：终端 ──────────────────────────────────────────────────────────────

_EVID = {"hard": "硬", "derived": "推导", "inferred": "推断"}


def render_text(rep: TraceReport, *, evidence: bool = False) -> str:
    L: list[str] = []
    bar = "═" * 66
    L.append(bar)
    L.append(f" 结论溯源 · {_preview(rep.run.subject, 44)}")
    t0, t1 = _parse_ts(rep.run.started_at), _parse_ts(rep.run.finished_at)
    span = (
        f"{t0.astimezone().strftime('%Y-%m-%d %H:%M:%S')} → "
        f"{t1.astimezone().strftime('%H:%M:%S')}"
        if t0 and t1 else "（时刻不可得）"
    )
    L.append(f" 局 {rep.run.session_id}   {span}"
             + (f"  ({rep.run.wall_ms / 1000:.1f}s)" if rep.run.wall_ms else ""))
    L.append(f" 定局依据：{rep.run.basis}")
    L.append(" 数据来源：.wit/*/acps/aip-tasks.sqlite3（平台落盘，第三方可复算）")
    L.append(bar)

    for w in rep.warnings:
        L.append(f" ⚠ WARNING  {w}")
    if rep.warnings:
        L.append("")

    L.append(" [委托链]  每一跳都可在任务库中按 task_ref 查到")
    if not rep.hops:
        L.append("   （本局无可溯源的跨智能体调用）")
    for h in rep.hops:
        mark = {"ok": "✅", "slow": "🐢", "failed": "❌", "pending": "⏳"}[h.health]
        lat = f"{h.latency_ms / 1000:.1f}s" if h.latency_ms is not None else "  …"
        indent = " │  " * h.depth
        pair = "" if h.pairing == "hard" else "  ⚠配不上对端入站行（角色为推断）"
        L.append(f"  {indent}├─ {h.seq:02d} {h.from_role or '?'} → "
                 f"{h.to_role or _short_aic(h.to_aic) or '?'}   "
                 f"{mark} {lat:>7}  {'、'.join(h.product_ids) or '（无产物）'}")
        L.append(f"  {indent}│     task_ref={h.task_ref}  task_id={h.task_id}{pair}")
        if h.parent_task_ref:
            L.append(f"  {indent}│     ↑ 跨层归属：推断自 {h.parent_task_ref}（无协议级硬键）")

    L.append("")
    L.append(" [跨层归属]  *全部为推断* —— 实测跨层无协议级硬键")
    if not rep.chain_edges:
        L.append("   （本局只有一层，没有跨层边）")
    for e in rep.chain_edges:
        flag = "  ⚠多候选" if e.ambiguous else ""
        L.append(f"   {e.from_task_ref} → 会话 …{e.to_session_id[-12:]}  "
                 f"[{_EVID[e.evidence]}]{flag}")
        L.append(f"      why: {e.why}")

    L.append("")
    L.append(" [结论溯源]")
    # 🔴 强度口径必须写出来 —— 见 `render_html` 里同一段图例的理由：
    #    `硬` 不解释，就会被读成"平台级认证"。
    L.append(f"   强度口径：{_EVID['hard']}＝该字段名出现在下游返回的产物正文里"
             "（行内附命中片段，可人工复核）——它不是平台级认证；"
             f"{_EVID['derived']}＝按上游材料推导，链上没有原文直接支撑；"
             f"{_EVID['inferred']}＝跨层归属，协议里没有硬键。")
    if not rep.verdict_fields:
        L.append("   （没有绑定 verdict 文件，本次只重建了链路）")
    for f in rep.verdict_fields:
        src = f.source
        if src.get("kind") == "partner_product":
            where = f"{src['role']} / {src['product_id'][:28]}… / {src['task_ref']}"
        else:
            where = "主控综合（链上无对应下游产物）"
        L.append(f"   {f.field:<18} {f.value_preview:<22} ← {where}"
                 f"   [{_EVID[f.evidence]}]")
        if src.get("excerpt") and f.evidence == "hard":
            L.append(f"        └ 命中片段：{src['excerpt']}")

    if rep.gaps:
        L.append("")
        L.append(f" [⚠ 找不到来源的字段]  共 {len(rep.gaps)} 项（如实列出，不留空）")
        for g in rep.gaps:
            L.append(f"   {g['field']:<18} {g['reason']}")

    L.append("")
    L.append(" [复算]")
    if rep.run.verdict_file:
        L.append("   python -m tools.trace \\")
        L.append(f"     --verdict {rep.run.verdict_file} --md")
    L.append("   sqlite3 .wit/leader/acps/aip-tasks.sqlite3 \\")
    L.append(f"     \"SELECT task_ref,direction,remote_aic,state,created_at FROM aip_tasks "
             f"WHERE session_id='{rep.run.session_id}'\"")
    L.append("")
    L.append(f" 生成器 tools/trace.py（只读，不写入任何任务库）  "
             f"生成时刻 {rep.generated_at}")
    L.append(bar)

    if evidence:
        L.append("")
        L.append(" [证据原文]  （脱敏：只保留任务库自身字段）")
        for h in rep.hops:
            L.append(f"  ── 跳 {h.seq} · {h.task_ref}")
            for c in h.commands:
                L.append("     cmd  " + json.dumps(c, ensure_ascii=False)[:400])
            for e in h.event_rows:
                L.append("     evt  " + json.dumps(e, ensure_ascii=False)[:400])
    return "\n".join(L)


# ── 渲染：Markdown ──────────────────────────────────────────────────────────


def render_markdown(rep: TraceReport) -> str:
    L: list[str] = []
    L.append("### 结论溯源表（机制 5）")
    L.append("")
    L.append("> ⚠️ 本文件由 `python -m tools.trace --md` **自动生成**，数据来自平台任务库。")
    L.append(f"> 生成时刻：{rep.generated_at}　·　定局依据：{rep.run.basis}")
    L.append("")
    for w in rep.warnings:
        L.append(f"> ⚠ **WARNING**：{w}")
    if rep.warnings:
        L.append("")

    L.append("#### 表 1 · 本次研判")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append(f"| 输入原文 | {_md_cell(rep.run.subject, 80)} |")
    L.append(f"| 局（session） | `{rep.run.session_id}` |")
    L.append(f"| 总耗时 | "
             + (f"{rep.run.wall_ms / 1000:.1f} 秒 |" if rep.run.wall_ms else "— |"))
    roles = {h.from_role for h in rep.hops} | {h.to_role for h in rep.hops if h.to_role}
    L.append(f"| 参与 Agent | {len(roles)} 个（{'、'.join(sorted(roles))}） |")
    L.append(f"| 跨智能体调用次数 | {len(rep.hops)} |")
    L.append("")

    L.append("#### 表 2 · 委托链（每一跳都可回任务库核对）")
    L.append("")
    L.append("| # | 层 | 发起 → 接收 | 状态 | 耗时 | 产物 | 任务号 | 配对 |")
    L.append("|---:|---:|---|---|---:|---|---|---|")
    for h in rep.hops:
        lat = f"{h.latency_ms / 1000:.1f}s" if h.latency_ms is not None else "—"
        pair = "硬" if h.pairing == "hard" else "⚠推断"
        L.append(
            f"| {h.seq} | {h.depth} | {h.from_role or '?'} → "
            f"{h.to_role or _short_aic(h.to_aic) or '?'} | {h.health} | {lat} "
            f"| {_md_cell('、'.join(h.product_ids) or '—', 120)} | `{h.task_ref}` | {pair} |"
        )
    L.append("")

    if rep.chain_edges:
        L.append("#### 表 2b · 跨层归属（**全部为推断**，无协议级硬键）")
        L.append("")
        L.append("| 上级任务号 | 下级会话 | 关系 | 证据 | 判据 |")
        L.append("|---|---|---|---|---|")
        for e in rep.chain_edges:
            amb = "⚠多候选 " if e.ambiguous else ""
            L.append(f"| `{e.from_task_ref}` | …{e.to_session_id[-12:]} | {e.relation} "
                     f"| **{_EVID[e.evidence]}** | {amb}{_md_cell(e.why)} |")
        L.append("")

    L.append("#### 表 3 · 结论字段溯源")
    L.append("")
    L.append("| 结论字段 | 值 | 来源 | 证据强度 |")
    L.append("|---|---|---|---|")
    for f in rep.verdict_fields:
        src = f.source
        if src.get("kind") == "partner_product":
            where = f"{src['role']} Partner · `{src['product_id']}` · `{src['task_ref']}`"
            if src.get("excerpt"):
                where += f" ｜ 命中片段：{_md_cell(src['excerpt'], 160)}"
        else:
            where = "主控综合（链上无对应下游产物）"
        L.append(f"| `{f.field}` | {_md_cell(f.value_preview, 200)} | {where} "
                 f"| **{_EVID[f.evidence]}** |")
    if not rep.verdict_fields:
        L.append("| — | — | 本次未绑定 verdict 文件 | — |")
    L.append("")
    # 🔴 强度口径必须写出来 —— 见 `render_html` 里同一段图例的理由。
    L.append(f"> 强度口径 —— **{_EVID['hard']}**：该字段名出现在下游返回的产物正文里，"
             "行内附有命中片段可供人工复核；它不是平台级认证。"
             f"　**{_EVID['derived']}**：按上游材料推导，链上没有原文直接支撑。"
             f"　**{_EVID['inferred']}**：跨层归属，协议里没有硬键。")
    L.append("")

    if rep.gaps:
        L.append("#### 表 3b · ⚠ 找不到来源的字段（如实列出，不留空）")
        L.append("")
        L.append("| 结论字段 | 原因 |")
        L.append("|---|---|")
        for g in rep.gaps:
            L.append(f"| `{g['field']}` | {_md_cell(g['reason'], 300)} |")
        L.append("")

    L.append("#### 复算指引")
    L.append("")
    L.append("任何人都可以用下面这些命令，从平台自己的任务库里复现上面每一行：")
    L.append("")
    L.append("```bash")
    if rep.run.verdict_file:
        L.append(f"python -m tools.trace --verdict {rep.run.verdict_file} --md")
    L.append("sqlite3 .wit/leader/acps/aip-tasks.sqlite3 \\")
    L.append(f"  \"SELECT task_ref,direction,remote_aic,state,created_at FROM aip_tasks "
             f"WHERE session_id='{rep.run.session_id}'\"")
    L.append("```")
    L.append("")
    L.append("本页数据文件：")
    for f in rep.source_files:
        L.append(f"  · `{f}`")
    L.append("")
    L.append(f"生成器：`tools/trace.py`（**不写入任何任务库**）")
    return "\n".join(L)


# ── 渲染：HTML ──────────────────────────────────────────────────────────────

_HTML_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#1c1c1e;
 font:14px/1.65 -apple-system,"Segoe UI","Noto Sans CJK SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:20px;font-weight:600;margin:0 0 6px}
h2{font-size:15px;font-weight:600;margin:0 0 12px;color:#3a3a3c}
.card{background:#fff;border:1px solid #e3e5e9;border-radius:10px;padding:18px 20px;margin:0 0 18px}
.muted{color:#6b6f76}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.subj{font-size:16px;font-weight:600;line-height:1.5;margin-bottom:10px}
.strip{display:flex;flex-wrap:wrap;gap:18px;font-size:13px;color:#3a3a3c;margin-bottom:14px}
.strip b{font-weight:600}
.warn{background:#fff6e6;border:1px solid #f0d9a8;border-radius:8px;padding:10px 12px;
 color:#6b4a06;font-size:13px;margin:0 0 10px}
.scale{position:relative;height:34px;margin:6px 0 4px}
.scale .bar{position:absolute;left:0;right:0;top:12px;height:10px;border-radius:5px;
 background:linear-gradient(90deg,#e6f1fb,#eeeeee,#fbe9e7)}
.scale .pin{position:absolute;top:4px;width:2px;height:26px;background:#c0392b}
.scale .lab{position:absolute;top:22px;font-size:11px;color:#6b6f76;transform:translateX(-50%)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #eceef1;vertical-align:top}
th{font-weight:600;color:#3a3a3c;background:#fafbfc}
td.hard,tr.hard td{color:#1d6f4f}.badge{display:inline-block;padding:1px 7px;border-radius:20px;
 font-size:11px;border:1px solid #cfd4da;color:#4a4f56;background:#f4f5f7}
tr.gap td{color:#8a8f96;background:#fbfbfc}
.lanes{position:relative;margin:6px 0 0}
.lane{display:flex;align-items:center;height:34px}
.lane .who{width:150px;flex:0 0 150px;font-size:12px;color:#3a3a3c;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lane .track{position:relative;flex:1;height:22px;border-bottom:1px dashed #e8eaee}
.node{position:absolute;top:5px;height:12px;border-radius:6px;background:#1d9e75;
 border:1px solid #16775a;min-width:6px}
.node.pending{background:#c9ccd1;border-color:#b3b7bd}
.node.failed{background:#e24b4a;border-color:#c0392b}
.node.slow{background:#ef9f27;border-color:#c47f10}
.node.inferred{background:#fff;border:1.5px dashed #378add}
.node span{position:absolute;left:0;top:-1px;transform:translateY(-100%);
 font-size:11px;color:#4a4f56;white-space:nowrap}
.axis{display:flex;margin-left:150px;font-size:11px;color:#8a8f96}
.axis div{flex:1;border-left:1px solid #e8eaee;padding-left:4px}
details{border:1px solid #eceef1;border-radius:8px;padding:8px 12px;margin:8px 0}
summary{cursor:pointer;font-size:13px;font-weight:500}
pre{background:#f7f8fa;border:1px solid #eceef1;border-radius:6px;padding:10px;
 overflow:auto;max-height:260px;font-size:11.5px;margin:8px 0 0}
.legend{font-size:12px;color:#6b6f76;margin-top:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
code{background:#f3f4f7;padding:1px 5px;border-radius:4px;
 font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
"""


def _esc(x: Any) -> str:
    return html.escape(str(x if x is not None else ""), quote=True)


def _pct(dt: datetime | None, lo: datetime, hi: datetime) -> float:
    if dt is None or hi <= lo:
        return 0.0
    return max(0.0, min(100.0, (dt - lo).total_seconds() / (hi - lo).total_seconds() * 100))


def render_html(rep: TraceReport, *, evidence: bool = False) -> str:
    """自包含单页（浅色，截图/打印友好）。四屏见设计文档 §5.2。

    ⚠️ 刻意**不跟随宿主深色主题** —— 这一页的用途是截图与打印，浅色底才可读。
    """
    L: list[str] = []
    A = L.append
    A("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>")
    A("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    A(f"<title>结论溯源 · {_esc(_preview(rep.run.subject, 40))}</title>")
    A(f"<style>{_HTML_CSS}</style></head><body><div class='wrap'>")

    # —— 第 1 屏：结论卡 ——
    A("<div class='card'>")
    A("<h1>结论溯源表</h1>")
    A(f"<div class='subj'>{_esc(rep.run.subject) or '（本次未绑定 verdict 文件）'}</div>")
    roles = sorted({h.from_role for h in rep.hops} | {h.to_role for h in rep.hops if h.to_role})
    wall = f"{rep.run.wall_ms / 1000:.1f} 秒" if rep.run.wall_ms else "—"
    A("<div class='strip'>")
    A(f"<span>⏱ <b>{_esc(wall)}</b></span>")
    A(f"<span>Agent <b>{len(roles)}</b> 个</span>")
    A(f"<span>跨智能体调用 <b>{len(rep.hops)}</b> 次</span>")
    A(f"<span>局 <code>{_esc(rep.run.session_id)}</code></span>")
    A(f"<span>定局依据 {_esc(rep.run.basis)}</span>")
    A("</div>")
    for w in rep.warnings:
        A(f"<div class='warn'>⚠ {_esc(w)}</div>")

    raw = _read_verdict(rep.run.verdict_file) if rep.run.verdict_file else {}
    hi = raw.get("harm_index")
    pos: float | None = None
    clamped = False
    try:
        raw_pos = (float(hi) + 100) / 200 * 100
    except (TypeError, ValueError):
        raw_pos = None
    if raw_pos is not None:
        # ⚠️ 越界必须**夹住并且说出来**：不夹的话 pin 会被甩到色条外面，
        #    页面看着只是"有点怪"，而它其实在暗示一个标尺上不存在的档位。
        pos = max(0.0, min(100.0, raw_pos))
        clamped = pos != raw_pos
    if pos is not None:
        A("<div class='scale'>")
        A(f"<div class='bar'></div><div class='pin' style='left:{pos:.2f}%'></div>")
        for v in (-100, -25, 0, 50, 100):
            A(f"<div class='lab' style='left:{(v + 100) / 200 * 100:.1f}%'>{v}</div>")
        A("</div>")
        A(f"<div class='muted' style='font-size:12px'>社会危害性指数 "
          f"<b>{_esc(hi)}</b>（越左越正当，越右危害越大）　档位 {_esc(raw.get('band'))}"
          + ("　<b>⚠ 该值超出 -100..100 标尺，pin 已夹到边界</b>" if clamped else "")
          + "</div>")
    A("</div>")

    # —— 第 2 屏：委托链泳道 ——
    A("<div class='card'><h2>委托链泳道图（横轴 = 时间）</h2>")
    if not rep.hops:
        A("<div class='muted'>本局没有任何跨智能体调用可画。</div>")
    else:
        t0 = _parse_ts(rep.run.started_at)
        t1 = _parse_ts(rep.run.finished_at)
        if t0 is None or t1 is None or t1 <= t0:
            t1 = (t0 or datetime.now(timezone.utc)) + timedelta(seconds=1)
            t0 = t0 or t1 - timedelta(seconds=1)
        lanes: list[str] = []
        for h in rep.hops:
            if h.from_role not in lanes:
                lanes.append(h.from_role)
            if h.to_role and h.to_role not in lanes:
                lanes.append(h.to_role)
        A("<div class='lanes'>")
        for who in lanes:
            A("<div class='lane'>")
            A(f"<div class='who'>{_esc(who or '（未识别）')}</div>")
            A("<div class='track'>")
            for h in rep.hops:
                if h.from_role != who:
                    continue
                a = _parse_ts(h.sent_at)
                b = _parse_ts(h.last_event_at) or a
                left = _pct(a, t0, t1)
                width = max(0.6, _pct(b, t0, t1) - left)
                cls = h.health + ("" if h.depth == 0 else " inferred")
                title = (f"#{h.seq} {h.from_role}→{h.to_role or h.to_aic}"
                         f" · {h.health} · task_ref={h.task_ref} · pairing={h.pairing}")
                A(f"<div class='node {cls}' style='left:{left:.2f}%;width:{width:.2f}%' "
                  f"title='{_esc(title)}'><span>#{h.seq}</span></div>")
            A("</div></div>")
        A("</div>")
        A("<div class='axis'><div>0s</div><div>25%</div><div>50%</div>"
          "<div>75%</div><div>100%</div></div>")
        A("<div class='legend'>"
          "<i style='background:#1d9e75'></i>已完成（硬事实，task_ref 可查）　"
          "<i style='background:#fff;border:1.5px dashed #378add'></i>跨层归属=推断"
          "（虚线；hover 看 task_ref）　"
          "<i style='background:#e24b4a'></i>失败　"
          "<i style='background:#c9ccd1'></i>进行中/无产物</div>")
        if rep.chain_edges:
            A("<div style='margin-top:10px'>")
            for e in rep.chain_edges:
                A(f"<div class='muted' style='font-size:12px'>"
                  f"推断边：<code>{_esc(e.from_task_ref)}</code> → "
                  f"<code>…{_esc(e.to_session_id[-12:])}</code>"
                  f"{'　⚠多候选' if e.ambiguous else ''}<br>判据：{_esc(e.why)}</div>")
            A("</div>")
    A("</div>")

    # —— 第 3 屏：字段溯源 ——
    A("<div class='card'><h2>结论字段溯源</h2>")
    if not rep.verdict_fields:
        A("<div class='muted'>本次未绑定 verdict 文件，只重建了链路。</div>")
    else:
        A("<table><thead><tr><th>结论字段</th><th>值</th><th>来源</th><th>强度</th></tr>"
          "</thead><tbody>")
        for f in rep.verdict_fields:
            src = f.source
            if src.get("kind") == "partner_product":
                where = (f"{_esc(src['role'])} Partner · "
                         f"<code>{_esc(src['product_id'])}</code><br>"
                         f"<span class='muted mono'>task_ref={_esc(src['task_ref'])}</span>")
                if src.get("excerpt"):
                    # 硬溯源**必须**把命中片段摆出来，否则「硬」只是一句无法复核的断言。
                    where += (f"<br><span class='muted' style='font-size:11.5px'>"
                              f"命中片段：{_esc(src['excerpt'])}</span>")
            else:
                where = "主控综合（链上无对应下游产物）"
            cls = "hard" if f.evidence == "hard" else "gap"
            A(f"<tr class='{cls}'>"
              f"<td><code>{_esc(f.field)}</code></td>"
              f"<td>{_esc(f.value_preview)}</td><td>{where}</td>"
              f"<td><span class='badge'>{_EVID[f.evidence]}</span></td></tr>")
        A("</tbody></table>")
        # 🔴 图例**必须有**：`硬` 若不解释，读者会读成"平台级认证" ——
        #    而它的真实含义只是"该字段名出现在下游返回的产物正文里"。
        #    （`_field_span` 的 docstring 早就这么要求了，渲染层却一直没做：
        #      **说了但没做**，比不说更危险，因为代码里留着"已经处理过"的印象。）
        A("<div class='legend'>强度口径 —— "
          f"<b>{_EVID['hard']}</b>：该字段名出现在下游返回的产物正文里，"
          "行内附有命中片段可供人工复核；它不是平台级认证。"
          f"　<b>{_EVID['derived']}</b>：按上游材料推导，链上没有原文直接支撑。"
          f"　<b>{_EVID['inferred']}</b>：跨层归属，协议里没有硬键。</div>")
        if rep.gaps:
            A(f"<div class='warn' style='margin-top:12px'>⚠ 下表 {len(rep.gaps)} 个字段"
              "在整条链上找不到下游产物来源 —— 如实列出，不留空"
              "（留空会被读成『这一项没问题』）。</div>")
            A("<table style='margin-top:8px'><thead><tr><th>结论字段</th><th>原因</th></tr>"
              "</thead><tbody>")
            for g in rep.gaps:
                A(f"<tr class='gap'><td><code>{_esc(g['field'])}</code></td>"
                  f"<td>{_esc(g['reason'])}</td></tr>")
            A("</tbody></table>")
    A("</div>")

    # —— 第 4 屏：复算指引 + 证据抽屉 ——
    A("<div class='card'><h2>复算指引</h2>")
    A("<div>任何人都可以用下面这些命令，从平台自己的任务库里复现上面每一行：</div>")
    A("<pre>")
    if rep.run.verdict_file:
        A(_esc(f"$ python -m tools.trace --verdict {rep.run.verdict_file} --md"))
    A(_esc("$ sqlite3 .wit/leader/acps/aip-tasks.sqlite3 \\\n"
           f"    \"SELECT task_ref,direction,remote_aic,state,created_at FROM aip_tasks "
           f"WHERE session_id='{rep.run.session_id}'\""))
    A("</pre>")
    A("<div class='muted' style='font-size:12px'>本页数据文件：</div><ul "
      "style='margin:6px 0 0;padding-left:20px'>")
    for f in rep.source_files:
        A(f"<li class='mono'>{_esc(f)}</li>")
    A("</ul>")
    A(f"<div class='muted' style='font-size:12px;margin-top:10px'>"
      f"生成时刻 {_esc(rep.generated_at)}　·　生成器 tools/trace.py"
      f"（只读：不写入任何任务库）</div>")
    A("</div>")

    A("<div class='card'><h2>证据抽屉（每一跳的命令与事件原文）</h2>")
    for h in rep.hops:
        A(f"<details><summary>跳 {h.seq} · {_esc(h.from_role)} → "
          f"{_esc(h.to_role or _short_aic(h.to_aic))} · "
          f"<code>{_esc(h.task_ref)}</code> · {h.health}"
          f"{'' if h.pairing == 'hard' else ' · ⚠配对为推断'}</summary>")
        A(f"<div class='muted' style='font-size:12px'>请求原文（start 命令 dataItems）："
          f"<br>{_esc(h.request_excerpt) or '（无）'}</div>")
        if evidence:
            for c in h.commands:
                A("<pre>" + _esc(json.dumps(c, ensure_ascii=False, indent=2)) + "</pre>")
            for e in h.event_rows:
                A("<pre>" + _esc(json.dumps(e, ensure_ascii=False, indent=2)) + "</pre>")
        else:
            A("<div class='muted' style='font-size:12px'>"
              "（用 <code>--evidence</code> 生成可展开命令与事件原文）</div>")
        A("</details>")
    A("</div>")

    A("</div></body></html>")
    return "".join(L)


def render_json(rep: TraceReport) -> str:
    return json.dumps(_to_dict(rep), ensure_ascii=False, indent=2)


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tools.trace",
        description="结论溯源表：从平台任务库重建一次研判的跨智能体链路（只读）",
    )
    ap.add_argument("--wit", default=".wit", help="wit 工作目录（默认 .wit）")
    ap.add_argument("--verdict", default=None, help="指定 verdict JSON（默认取最新一份）")
    ap.add_argument("--session", default=None, help="直接指定 session_id")
    ap.add_argument("--latest", action="store_true", help="最近一局（默认行为）")
    ap.add_argument("--tol", type=float, default=DEFAULT_TOL_S,
                    help=f"无 sidecar 时按时间认领一局的容差秒数（默认 {DEFAULT_TOL_S:.0f}）")
    ap.add_argument("--role", default="leader",
                    help="verdict 的产出角色（决定拿谁的会话表认领，默认 leader）")
    ap.add_argument("--md", action="store_true", help="输出 Markdown")
    ap.add_argument("--html", action="store_true", help="输出自包含 HTML（浅色，可截图）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机读）")
    ap.add_argument("--evidence", action="store_true", help="附带命令/事件原文")
    ap.add_argument("--out", default=None, help="写入文件（utf-8、纯 LF）")
    args = ap.parse_args(argv)

    rep = build_trace(args.wit, verdict=args.verdict, session=args.session,
                      tol_s=args.tol, owner_role=args.role)
    if rep is None:
        print(
            "找不到这一局：既没有可用的 verdict 文件，也没有任何平台会话窗口能对上。\n"
            "  可用：--verdict <path> / --session <id> / --tol <秒>\n"
            "  本命令不产半成品 —— 定不出局就是失败。",
            file=sys.stderr,
        )
        return 2

    if args.json:
        text = render_json(rep)
    elif args.html:
        text = render_html(rep, evidence=args.evidence)
    elif args.md:
        text = render_markdown(rep)
    else:
        text = render_text(rep, evidence=args.evidence)

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + ("" if text.endswith("\n") else "\n"))
        print(f"已写入 {os.path.abspath(args.out)}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
