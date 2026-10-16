"""线索复研判（B2 出路 · 2026-10-13）——「查不了」不是终点。

四个拍板决定（2026-10-13，见 `docs/aicomp/线索侦察角色-可行性分析与设计.md` §8
与当日讨论）：**B2 异步队列** + **用户侧递入/公开权威源**（平台侧拉取明确放弃）+
**建负控集** + **内部与可引用报告**。本模块是三步走第 1 步的收尾增量——
把「库里证据不足」的案件接进**既有**的每日研判装置：

  · **写侧（自动）**：`fact_issue_ruling` 落盘「无法判定」裁定 ⇒ 自动记一条
    **复研判请求**（`retries.jsonl`）。这不是新判断，是事实记录：
    「该事件截至事实库状态 E 仍证据不足」。
  · **读侧（每日）**：`--examine-one` 由 `scripts/case_round.sh` 在案例轮
    **之后顺序**调用（同一定时器、同一单元；顺序执行 ⇒ 天然不与案例轮抢
    工作区）。有合格工作项才真跑一轮：
      ① 群众来信（`lead_store.pending()`——受理官的队列从此**有消费者**，
         「队列由每日研判链路取用」这句写在受理官提示词里的承诺兑现）；
      ② 复研判请求，**且事实库指纹自入队以来变过**——库没长肥就重查
         = 拿同样的空库再弃权一次，纯烧预算（epoch 不变 ⇒ 不合格）。
  · **消费即终态**：一条工作项只查一次；若复研判**仍**无法判定，新一轮
    裁定落盘时写侧会用**新指纹**再记一条（append-only，不改旧记录）——
    「等下一次库长肥」由此闭环。

与既有装置的边界（不碰的都不碰）：
  · 线索受理官（lead-intake）仍是**群众来信的唯一入口**，本模块不受理、
    不改它的 queue/consumed 语义；
  · 快车道 `decide_lane` 是「库够不够」的**唯一判道入口**，本模块不复判；
  · 案例库轮换**一字不动**：研判轮的台账 label 用 `lead:` 前缀，
    `case_id_from_label` 只认 `case:` ⇒ 对轮换完全隐形；
  · 侦察角色（主动出网找线索）= 三步走第 3 步，须负控回测达标才建——
    本模块**刻意不给出网面**（零出网零授权，与受理官同一条红线）。

台账纪律（与 queue/consumed/runs.jsonl 同款）：retries 与 retry_consumed 都
append-only **只存事实**；「待处理」= 差集**读时现场算**；同一 ``(lead_id,
epoch)`` 不重复入队；同一 lead_id 有多个 epoch 的待处理记录时**读时只认最新**
（每次库长肥只值得查一次，不是每个历史 epoch 各查一次）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from . import lead_store

#: 复研判请求队列（与受理官的 queue.jsonl 同目录、不同文件——语义不同不混写：
#: 受理官的 queue 是「群众来信、从未查过」；retries 是「系统查过、证据不足」）。
RETRY_QUEUE_NAME = "retries.jsonl"
RETRY_CONSUMED_NAME = "retry_consumed.jsonl"
#: 研判轮的编排台账（记录「这一轮查了哪条工作项」；运行本身另记 runs.jsonl）。
EXAM_LEDGER_NAME = "exam_rounds.jsonl"
#: 研判轮台账 label 前缀。⚠️ **刻意不是 `case:`**——案例轮换只认 `case:`，
#: `lead:` 前缀对 `plan_next` 完全隐形（不进 counts、不进 unknown）。
LABEL_PREFIX = "lead:"
#: 一轮研判的子进程超时（对齐 case_round.ROUND_TIMEOUT_S——同一类活）。
EXAM_TIMEOUT_S = 900

EXIT_OK = 0
EXIT_CHECKS_FAILED = 1
EXIT_SKIPPED = 2

DEFAULT_ROLE = "leader"


# ---------------------------------------------------------------------------
# 路径与底层读写（append-only、单次 write 一整行——与 run_ledger 同纪律）
# ---------------------------------------------------------------------------


def retries_path(wit_dir=None) -> Path:
    return lead_store.leads_dir(wit_dir) / RETRY_QUEUE_NAME


def retry_consumed_path(wit_dir=None) -> Path:
    return lead_store.leads_dir(wit_dir) / RETRY_CONSUMED_NAME


def exam_ledger_path(wit_dir=None) -> Path:
    return lead_store.leads_dir(wit_dir) / EXAM_LEDGER_NAME


def _append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            out.append(json.loads(ln))
    return out


def _iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


# ---------------------------------------------------------------------------
# 事实库指纹（epoch）——「库长肥了吗」的唯一判据
# ---------------------------------------------------------------------------


def store_epoch(facts_root: Path | None = None) -> str:
    """事实库指纹：`legal_acps/facts/*.jsonl` 的（名, 大小, mtime）逐个哈希。

    指纹变 = 库变了（新增文件 / 重导 / 既有文件被改写）。重研判的资格就看
    「入队时的指纹 ≠ 现在的指纹」。⚠️ 不读文件内容——指纹要的是**变的时刻
    能感知**，不是内容寻址；读内容会把每次加载都变慢一截。
    """
    root = facts_root if facts_root is not None else Path(__file__).resolve().parent / "facts"
    if not root.exists():
        return "no-facts-dir"
    h = hashlib.md5()
    for p in sorted(root.glob("*.jsonl")):
        st = p.stat()
        h.update(f"{p.name}:{st.st_size}:{int(st.st_mtime_ns)}".encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 写侧：无法判定 ⇒ 复研判请求
# ---------------------------------------------------------------------------


def queue_retry(event: str, ruling_source: str = "", *, wit_dir=None,
                epoch: str | None = None) -> dict:
    """记一条复研判请求。**永远结构化返回**（失败也带原因，不抛出）。

    闸门与受理官**同一把尺子**（单一来源：`lead_store.validate_lead`）——
    事件文本不过闸（如自带定性词、太短）就如实拒收，不绕闸门硬塞；
    拒收不是错误：这类事件走不了自动重查，但人工重问永远可以。
    判重口径：**待处理集合里**已有同 ``(lead_id, epoch)`` ⇒ duplicate——
    已消费过的旧 epoch 记录不算数（库又长肥 ⇒ 新 epoch ⇒ 新记录，
    这正是「等下一次库长肥」的闭环）。
    """
    claim = (event or "").strip()
    from .contracts import LeadSubmission

    lead = LeadSubmission(claim=claim, category="其他")
    reasons = lead_store.validate_lead(lead)
    if reasons:
        return {
            "status": "rejected",
            "refusal_reason": "；".join(reasons),
            "how_to_fix": "事件文本未过受理闸门（与群众来信同一把尺子）；"
                          "此类事件不自动重查，人工重问不受影响",
        }
    ep = epoch if epoch is not None else store_epoch()
    lid = lead_store.lead_id_of(claim)
    pending = pending_retries(wit_dir)
    if any(r.get("lead_id") == lid and r.get("store_epoch") == ep for r in pending):
        return {"status": "duplicate", "lead_id": lid, "store_epoch": ep}
    record = {
        "retry_id": f"{lid}:{ep[:8]}",
        "lead_id": lid,
        "event": claim,
        "ruling_source": ruling_source,
        "store_epoch": ep,
        "queued_at": _iso(),
    }
    _append(retries_path(wit_dir), record)
    return {"status": "queued", **record}


def pending_retries(wit_dir=None) -> list[dict]:
    """待处理复研判请求 = retries − consumed（读时现场算）。

    同一 ``lead_id`` **只认最新一条**（文件序即时间序）——每次库长肥
    只值得查一次；老 epoch 的记录留着（事实不许改），但不再派活。
    🔴 「最新」必须在**全量行**里取，**不能**先滤掉已消费再取最新：
    先滤后取会让被消费的最新条（如 e2）消失、**过期的旧条（e1）复活**
    成新的「最新」——同一事件带着旧 ``ruling_source`` 再次变成合格活，
    重查死循环（⑦-11(4) 负对照实证）。正确口径：**最新条已消费 ⇒
    整个 lead 不再待处理**；库再长肥会入队一条**新 epoch** 记录，
    那才是下一轮重查的凭据。
    """
    consumed = {r.get("retry_id") for r in _read(retry_consumed_path(wit_dir))}
    newest: dict[str, dict] = {}
    for r in _read(retries_path(wit_dir)):
        lid = str(r.get("lead_id") or "")
        if lid:
            newest[lid] = r  # 文件序即时间序：后见覆盖先见（含已消费行）
    return [r for r in newest.values() if r.get("retry_id") not in consumed]


def eligible_retries(wit_dir=None, *, epoch: str | None = None) -> list[dict]:
    """**合格**的复研判请求：库自入队以来变过（指纹不同）。

    指纹相同 = 同一个库 ⇒ 重查只会得到同一个「无法判定」（判据相同、
    材料相同）。把这种重查当活干，是拿真模型的预算打水漂。
    """
    ep = epoch if epoch is not None else store_epoch()
    return [r for r in pending_retries(wit_dir) if r.get("store_epoch") != ep]


def next_work(wit_dir=None, *, epoch: str | None = None) -> dict | None:
    """下一个工作项：**群众来信优先**（用户在等），其次合格的重查（FIFO）。

    返回统一形态 ``{kind, id, text, label}``；没有可干的活 ⇒ None
    （「没有活」是常态，不是错误）。
    """
    leads = lead_store.pending(wit_dir)
    if leads:
        first = leads[0]
        lid = str(first.get("lead_id") or "")
        claim = str(first.get("claim") or "")
        if lid and claim:
            return {"kind": "lead", "id": lid, "text": claim,
                    "label": f"{LABEL_PREFIX}{lid}"}
    for r in eligible_retries(wit_dir, epoch=epoch):
        rid = str(r.get("retry_id") or "")
        event = str(r.get("event") or "")
        if rid and event:
            return {"kind": "retry", "id": rid, "text": event,
                    "label": f"{LABEL_PREFIX}{rid}"}
    return None


def consume_work(work: dict, *, run_id: str = "", consumer: str = "lead-examiner",
                 wit_dir=None) -> bool:
    """消费一条工作项（append-only 事实：谁、哪一轮）。返回是否成功。"""
    if work.get("kind") == "lead":
        return lead_store.consume(
            str(work.get("id") or ""), consumer=consumer, run_id=run_id,
            wit_dir=wit_dir,
        )
    known = {r.get("retry_id") for r in _read(retries_path(wit_dir))}
    rid = str(work.get("id") or "")
    if rid not in known:
        return False
    _append(retry_consumed_path(wit_dir), {
        "retry_id": rid, "consumer": consumer, "run_id": run_id,
    })
    return True


# ---------------------------------------------------------------------------
# 读侧：每日一轮研判（复用案例轮的闸门与体检，不重写）
# ---------------------------------------------------------------------------


def _spawn_examination(
    text: str, *, label: str, timeout_s: int, cwd: Path
) -> tuple[int | None, str, bool, float]:
    """起子进程跑一轮研判。形态与 `case_round._spawn_round` 同款：

    ⚠️ `sys.executable`（本模块被 venv python 起来，它就是对的解释器）；
    ⚠️ `cwd` 必须是仓库根（否则长出第二棵 `.wit`——案例轮模块头记过同一条）。
    """
    cmd = [
        sys.executable, "-u", "-m", "legal_acps.run_agent",
        DEFAULT_ROLE, "-i", text, "--label", label,
    ]
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
        )
        code: int | None = proc.returncode
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        code = None
        out = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + (
            (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        )
    except OSError as exc:
        code = None
        out = f"[lead-examiner] 子进程起不来：{type(exc).__name__}: {exc}"
    return code, out, timed_out, time.monotonic() - started


def _check_label(before_runs, after_runs, expect_label: str):
    """体检：这一轮在台账里的 label 对得上吗（`case:…` 同款纪律：
    label 写错 ⇒ 轮换/统计永远认不出这一轮）。"""
    from .case_round import Check

    fresh = [r for r in after_runs
             if r.run_id not in {b.run_id for b in before_runs}]
    if not fresh:
        return Check("label", False,
                     "台账里找不到本轮的新记录——子进程没走 run_ledgered？")
    bad = [r for r in fresh if r.label != expect_label]
    if bad:
        return Check(
            "label", False,
            f"本轮台账 label 应为 {expect_label!r}，实际 "
            f"{[r.label for r in bad]}（写错标签 = 这轮永远统计不到）",
        )
    return Check("label", True, f"label={expect_label}（台账 {len(fresh)} 条新记录）")


def examine_one(
    *,
    wit_dir=None,
    role: str = DEFAULT_ROLE,
    force: bool = False,
    dry_run: bool = False,
    timeout_s: int = EXAM_TIMEOUT_S,
    spawn: Callable[..., tuple[int | None, str, bool, float]] | None = None,
    port_check: Callable[[], Any] | None = None,
    artifacts_dir: Path | None = None,
    epoch: str | None = None,
) -> dict:
    """查**一个**工作项（或如实报告「没有可查的 / 被闸门拦下」）。

    返回结构化结果并落编排台账（exam_rounds.jsonl，append-only）。
    退出语义对齐案例轮：0 = 跑完且体检全过 / **idle 也算 0**（没有活是
    常态）；1 = 跑完但体检有未过项；2 = 没跑（闸门拦下——跳过 ≠ 通过，
    真相在本函数返回值与编排台账里）。
    `spawn` / `port_check` / `artifacts_dir` 是离线判据的注入口。
    """
    from .case_round import (
        busy_gate, check_artifacts, check_ports, offset_gate, read_runs,
        repo_root, snapshot_artifacts,
    )

    at = _iso()
    work = next_work(wit_dir, epoch=epoch)
    base: dict[str, Any] = {"at": at}

    if work is None:
        result = {
            **base, "outcome": "idle", "kind": None, "work_id": None,
            "label": None, "gates": [], "checks": [], "exit_code": EXIT_OK,
            "skip_reason": "没有可查的工作项（群众来信空 + 库未长肥），常态",
            "ledger_error": None,
        }
        _persist(result, wit_dir)
        return result

    base.update(kind=work["kind"], work_id=work["id"], label=work["label"])

    before_runs = read_runs(wit_dir=wit_dir, role=role)
    gates = [busy_gate(before_runs), offset_gate()]
    if force:
        gates = [
            type(g)(g.name, True,
                   g.detail if g.ok else f"[--force 已绕过] {g.detail}")
            for g in gates
        ]
    base["gates"] = [g.to_json() for g in gates]
    blocked = [g for g in gates if not g.ok]

    if dry_run:
        result = {**base, "outcome": "dry-run", "checks": [],
                  "exit_code": EXIT_OK, "skip_reason": "--dry-run：没有真跑",
                  "ledger_error": None}
        _persist(result, wit_dir)
        return result
    if blocked:
        reason = "；".join(f"[{g.name}] {g.detail}" for g in blocked)
        result = {**base, "outcome": "skipped", "checks": [],
                  "exit_code": EXIT_SKIPPED, "skip_reason": reason,
                  "ledger_error": None}
        _persist(result, wit_dir)
        return result

    art_before = snapshot_artifacts(artifacts_dir)
    code, output, timed_out, duration = (spawn or _spawn_examination)(
        work["text"], label=work["label"], timeout_s=timeout_s,
        cwd=repo_root(),
    )

    after_runs = read_runs(wit_dir=wit_dir, role=role)
    checks = [
        _check_label(before_runs, after_runs, work["label"]),
        check_artifacts(art_before, snapshot_artifacts(artifacts_dir)),
        (port_check or check_ports)(),
    ]
    # 消费 = 「这一条被查过了」这一事实。子进程起没起来（code=None）不消费
    # ——没查过就是没查过；查完体检红不红都消费——体检盯的是**产物**，
    # 而产物落盘与否已由 checks 如实记录，重查同一条不会再变好。
    consumed = False
    if code is not None:
        consumed = consume_work(work, wit_dir=wit_dir)

    fresh = [r for r in after_runs
             if r.run_id not in {b.run_id for b in before_runs}]
    run = fresh[0] if len(fresh) == 1 else None
    result = {
        **base,
        "outcome": "ran",
        "checks": [c.to_json() for c in checks],
        "exit_code": EXIT_OK if all(c.ok for c in checks) else EXIT_CHECKS_FAILED,
        "run_id": run.run_id if run else None,
        "status": run.status if run else None,
        "iterations": run.iterations if run else None,
        "duration_s": round(duration, 1),
        "timed_out": timed_out,
        "consumed": consumed,
        "tail": "\n".join(output.splitlines()[-60:]),
        "ledger_error": None,
    }
    _persist(result, wit_dir)
    return result


def _persist(result: dict, wit_dir=None) -> None:
    """落编排台账。写不下去也不抛（旁路记录不许改主流程结论）。"""
    try:
        _append(exam_ledger_path(wit_dir), result)
    except Exception as exc:  # noqa: BLE001 - 宽是有意的
        print(
            f"[lead-examiner] ⚠️ 编排台账写失败：{exc}\n"
            "             它是旁路记录 ⇒ 本轮结论照常；溯源上会缺一份凭证。",
            file=sys.stderr,
        )
        result["ledger_error"] = f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 只读视图与 CLI
# ---------------------------------------------------------------------------


def status(wit_dir=None, *, epoch: str | None = None) -> dict:
    """人看的只读快照：待处理的来信 / 重查（含不合格原因）。"""
    ep = epoch if epoch is not None else store_epoch()
    pending_leads = lead_store.pending(wit_dir)
    retries = pending_retries(wit_dir)
    eligible = eligible_retries(wit_dir, epoch=ep)
    return {
        "store_epoch": ep,
        "pending_user_leads": len(pending_leads),
        "pending_retries": len(retries),
        "eligible_retries": len(eligible),
        "eligible": [
            {"retry_id": r.get("retry_id"), "event": str(r.get("event"))[:80]}
            for r in eligible
        ],
    }


def _main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="线索复研判（B2 出路）：每日一轮，查一条群众来信或库长肥后的重查")
    ap.add_argument("--examine-one", action="store_true", default=True,
                    help="查一个工作项（默认动作；供 scripts/case_round.sh 调用）")
    ap.add_argument("--status", action="store_true",
                    help="只读快照：待处理来信/重查、合格清单")
    ap.add_argument("--force", action="store_true", help="绕过两个闸门；体检照跑")
    ap.add_argument("--dry-run", action="store_true",
                    help="只选工作项、只跑闸门；不起子进程、不消费")
    ap.add_argument("--timeout", type=int, default=EXAM_TIMEOUT_S,
                    help=f"子进程超时秒数（默认 {EXAM_TIMEOUT_S}）")
    args = ap.parse_args(argv)

    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return EXIT_OK

    result = examine_one(force=args.force, dry_run=args.dry_run,
                         timeout_s=args.timeout)
    outcome = result.get("outcome")
    if outcome == "ran":
        marks = "、".join(
            f"{c['name']}={'过' if c['ok'] else '红'}"
            for c in result.get("checks") or []
        )
        print(f"[lead-examiner] 本轮查了 {result.get('kind')} "
              f"{result.get('work_id')}：{marks}；消费={result.get('consumed')}")
    else:
        print(f"[lead-examiner] {outcome}：{result.get('skip_reason')}")
    return int(result.get("exit_code", EXIT_OK))


if __name__ == "__main__":
    raise SystemExit(_main())
