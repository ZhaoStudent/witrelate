"""平台写的**会话表** (`aip_sessions`) 读取口径 —— **全项目唯一一份**。

为什么需要这个模块
════════════════
结论溯源表要回答「这份 verdict 对应平台上的哪一局」。实测（2026-10-03，服务器）：

  · **leader 库里只有 outbound（52 条，0 条 inbound）** —— 主控是被**进程内**调起的，
    平台没有为它写一条入站行；所以「按 leader 的 inbound 行取 session」这条路不存在；
  · 一跳两端的硬键是 `task_id`；**跨层没有硬键**（见 `docs/aicomp/结论溯源表设计.md` §1.2）；
  · **但 `aip_sessions` 是平台自己写的表**（`session_id` / `owner_aic` /
    `created_at` / `updated_at`），leader 最新一条窗口 `08:03:04 .. 08:06:45`
    正好**包住** 08:03 那次全链运行。

⇒ 取 session 一律以**这张平台表**为准，不靠我们自己的日志、更不靠"最近的 outbound 行"猜。

为什么放在 `tools/` 而不是 `legal_acps/`
════════════════════════════════════════
两侧都要用它：运行时（`legal_acps/agents.py` 落盘 verdict 时写 sidecar）与
离线（`tools/trace.py` 在没有 sidecar 时降级推）。`legal_acps/__init__.py` 会
`import wt_compat`（进而拉 wit）—— 从 `tools/` 反向依赖它，会让 `tools/` 丢掉
「纯标准库、任何装了 Python 3.11+ 的地方都能跑」这条纪律（`tools/metrics.py` 就是这么要求自己的）。
`tools/` 是**命名空间包**（无 `__init__.py`）⇒ 导入本模块不会执行任何包级代码。

本模块**只读**：sqlite 一律 `mode=ro` 打开，不写入、不迁移、不加索引。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

__all__ = [
    "FRESH_WINDOW_S",
    "SESSIONS_DDL",
    "SessionRef",
    "own_aic",
    "pick_live_session",
    "read_sessions",
    "sessions_covering",
]

#: `aip_sessions` 的表定义（与平台落盘的 schema 逐列一致）。
#: 放在本模块而不是各测试文件里各写一份 —— **同一 schema 只写一处**，
#: 否则改了列名，测试还在拿旧表"验证"新代码。
SESSIONS_DDL = """
CREATE TABLE aip_sessions (
  session_id text primary key,
  owner_aic text not null,
  created_at text not null,
  updated_at text not null
);
"""

#: 判定「这一局还在进行中」的宽限：会话的 `updated_at` 距今超过它就**拒绝**写 sidecar。
#: 为什么要有这道闸：常驻进程可能已经空闲很久，'取最新一条会话'就会把**上一局**的
#: session 写到这一局的 verdict 上 —— 那比不写更糟（会静默给出错误的溯源）。
#: 宁可让 sidecar 缺位（溯源表会打 WARNING 降级），也不给一条错的。
FRESH_WINDOW_S = 600.0

_DB_REL = os.path.join("{role}", "acps", "aip-tasks.sqlite3")


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class SessionRef:
    """`aip_sessions` 里的一行（平台写的，权威）。"""

    session_id: str
    owner_aic: str
    created_at: datetime | None
    updated_at: datetime | None
    created_raw: str = ""
    updated_raw: str = ""


def role_db_path(wit_dir: str, role: str) -> str:
    """`.wit/<role>/acps/aip-tasks.sqlite3`。"""
    return os.path.join(wit_dir, _DB_REL.format(role=role))


def read_sessions(db_path: str) -> list[SessionRef]:
    """读一个角色库里的会话表（按 `created_at` 升序）。库不存在/表缺失时返回 []。"""
    if not os.path.exists(db_path):
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT session_id, owner_aic, created_at, updated_at "
            "FROM aip_sessions ORDER BY created_at"
        ).fetchall()
    except sqlite3.Error:
        # 老版本任务库可能没有这张表 —— 不是错误，只是没有这个来源。
        return []
    finally:
        con.close()
    return [
        SessionRef(
            session_id=str(r["session_id"] or ""),
            owner_aic=str(r["owner_aic"] or ""),
            created_at=_parse_ts(r["created_at"]),
            updated_at=_parse_ts(r["updated_at"]),
            created_raw=str(r["created_at"] or ""),
            updated_raw=str(r["updated_at"] or ""),
        )
        for r in rows
    ]


def own_aic(db_path: str) -> str:
    """从任务库自身取本角色的 AIC（`aip_tasks.local_aic`，平台写的，不硬编码）。"""
    if not os.path.exists(db_path):
        return ""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        row = con.execute(
            "SELECT local_aic FROM aip_tasks WHERE local_aic <> '' LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        con.close()
    return str(row[0]).strip() if row else ""


def pick_live_session(
    sessions: Sequence[SessionRef],
    *,
    now: datetime,
    owner: str = "",
    fresh_within_s: float = FRESH_WINDOW_S,
) -> tuple[SessionRef | None, str]:
    """挑出**当前这一局**的会话；挑不出时返回 `(None, 原因)`。

    判据（**三条**都要满足，缺一不可）：
      ① `owner_aic` 等于本角色的 AIC（本端作为发起方创建的会话）；
      ② 会话的 `updated_at` 距 `now` 不超过 `fresh_within_s` —— 防"常驻进程空闲很久，
         把上一局的会话写到这一局的 verdict 上"；
      ③ 新鲜候选之间**窗口不许互相重叠** —— 重叠就说明有两局在时间上说不清，
         那就**不猜**。

    返回的原因串会原样写进 sidecar 缺失时的告警，**不许含糊**。

    ⚠️ 两处曾经"静默放宽"，都是本项目最怕的那一类（会把**错的**归属写进去，
    而 sidecar 会被溯源表当成**权威**、连 WARNING 都不打）：
      · `owner` 为空时旧写法是 `list(sessions)` —— 把 owner 过滤**整个跳过**，
        于是能认领到**别的角色**的会话，返回的原因串还写着"owner 匹配"
        （**证据串本身在说谎**）。现在 owner 未知一律拒绝。
      · 多个新鲜候选时旧写法是 `max(created_at)` —— 静默取最新。顺序跑的局里
        这样取通常没错，但**两局时间上重叠**时它就是在赌。
        现在只有"新鲜候选彼此不重叠"才取最新，否则拒绝认领。
      两条都遵循本模块的既定取向：**宁可让 sidecar 缺位，也不给一条错的归属**
      （缺位时溯源表会降级并打 WARNING，那是响的）。
    """
    if not sessions:
        return None, "平台上没有任何会话记录（aip_sessions 为空）"
    if not owner:
        return None, (
            "本角色的 AIC 未知（任务库里取不到 local_aic）⇒ 无法确认会话归属，"
            "拒绝认领，宁缺勿错"
        )
    cands = [s for s in sessions if s.owner_aic == owner]
    if not cands:
        return None, f"没有 owner_aic == {owner} 的会话"

    def _age(ref: datetime | None) -> float | None:
        """`ref` 距 `now` 的秒数。跨时区比较：两边都按"绝对时刻"比。

        ⚠️ naive / aware 混用会直接抛 TypeError —— 这里统一按 `now` 的形态对齐。
        """
        if ref is None:
            return None
        if ref.tzinfo is None and now.tzinfo is not None:
            ref = ref.replace(tzinfo=now.tzinfo)
        elif ref.tzinfo is not None and now.tzinfo is None:
            ref = ref.replace(tzinfo=None)
        return (now - ref).total_seconds()

    fresh: list[tuple[SessionRef, float]] = []
    for s in cands:
        age = _age(s.updated_at)
        if age is not None and 0 <= age <= fresh_within_s:
            fresh.append((s, age))
    if not fresh:
        newest = max(cands, key=lambda s: (s.created_at or datetime.min, s.session_id))
        age = _age(newest.updated_at)
        return None, (
            f"最新会话 {newest.session_id} 的最后更新距今 "
            f"{age:.0f}s（超过 {fresh_within_s:.0f}s）"
            if age is not None
            else f"最新会话 {newest.session_id} 的 updated_at 无法解析"
        ) + " ⇒ 拒绝认领，宁缺勿错"

    fresh.sort(key=lambda p: (p[0].created_at or datetime.min, p[0].session_id))
    # ③ 只有**互相不重叠**才敢取最新：顺序跑的局里"最新"就是"当前这一局"，
    #    但两局时间重叠时它只是个赌注。
    for (a, _), (b, _) in zip(fresh, fresh[1:]):
        if a.updated_at and b.created_at and b.created_at < a.updated_at:
            return None, (
                f"有 {len(fresh)} 个自己人的会话都还新鲜，且**时间上互相重叠**"
                f"（…{a.session_id[-12:]} 与 …{b.session_id[-12:]}）"
                "⇒ 无法判断本局属于哪一个，拒绝认领，宁缺勿错"
            )

    chosen, age = fresh[-1]
    extra = f"（另有 {len(fresh) - 1} 个已结束的旧会话，窗口与本局不重叠）" if len(fresh) > 1 else ""
    return chosen, f"owner 匹配、updated_at 距今 {age:.0f}s{extra}"


def sessions_covering(
    sessions: Iterable[SessionRef],
    at: datetime,
    *,
    tol_s: float = 0.0,
) -> list[SessionRef]:
    """窗口 `[created_at, updated_at]`（各放宽 `tol_s`）覆盖 `at` 的会话，按开始时刻排序。

    **这是降级路径**：没有 sidecar 时，用 verdict 文件的时间戳去平台上"认领"一局。
    调用方必须把结果标成 `inferred` —— 文件时间戳是我方事实，平台窗口是平台事实，
    两者对上是**很强的推断**，但它仍然不是协议级硬键。
    """
    out: list[SessionRef] = []
    pad = timedelta(seconds=tol_s)
    for s in sessions:
        if s.created_at is None or s.updated_at is None:
            continue
        lo, hi = s.created_at - pad, s.updated_at + pad
        if lo.tzinfo is None and at.tzinfo is not None:
            lo, hi = lo.replace(tzinfo=at.tzinfo), hi.replace(tzinfo=at.tzinfo)
        if at.tzinfo is None and lo.tzinfo is not None:
            at = at.replace(tzinfo=lo.tzinfo)
        if lo <= at <= hi:
            out.append(s)
    out.sort(key=lambda s: (s.created_at or datetime.min, s.session_id))
    return out
