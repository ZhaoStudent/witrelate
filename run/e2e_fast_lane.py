"""真机 A/B：快车道分级裁决的**两向**判据（2026-10-12 · #156）。

A｜正对照——经验库有合格先例 ⇒ 快车道必须真的触发：
    事件**现场派生**（不信假设）：从 output/ 经验库挑一条 qualified 案例，
    预判 decide_lane == fast 才允许开跑。判据三条：
      A1 落盘交付：本轮新增裁定书 lane == "fast"（落盘重算在真机上判了 fast）；
      A2 跳过辩手：fact-judge 任务库本轮**没有**向辩手的出站委派
         （「命中即裁」的执行半边在真机上成立 —— 装了 ≠ 会响，这里就是响没响）；
      A3 主控收文：leader 回复里出现裁定结论（软判据，只打印不断言 ——
         字符串判据会命中自身反面，交付权威在 A1）。

B｜负对照——库外新案 ⇒ 辩论道照走（辩手真被派出）：
    独造事件，**跑前现场证死**预判 decide_lane == debate（「负对照真的是负」
    是判据不是假设）。判据：
      B1 预判 = debate（跑前派生，写进结论）；
      B2 fact-judge 任务库本轮有**两条**辩手委派、创建时间差毫秒级
         （并发扇出在分级改写后仍然在 —— ⑦-0 的真机版）；
      B3 落盘交付：本轮新增裁定书 lane == "debate"。
    B 跑完产出的裁定（lane=debate）自动入库成为合格先例 —— 这正是
    「辩完入库供下次复用」的另一半，属设计内，不清理。

链路形态：与 `run/e2e_leader_image.py` 同款 —— 进程内起 leader（真实身份、
真实 ACPs 委派），fact-judge 走**常驻服务**（8448）。因此：

    ⚠️ 跑前必做（探针不替你做，防抢工作区 / 防旧代码）：
        systemctl restart acps-fact-judge    # 常驻裁判加载快车道新代码
        systemctl stop acps-leader           # 探针进程内起主控
    ⚠️ 跑完必做：
        systemctl start acps-leader

用法（服务器，仓库根目录；本地无 .env 跑不了真模型）：

    ./.venv/bin/python -m run.e2e_fast_lane
    ./.venv/bin/python -m run.e2e_fast_lane --brief     # 只看判据不看长文本

判据的可判定性（本章是本脚本唯一值得细读的部分）：

  · 「本轮」的指认不靠时间戳猜：任务库用 **rowid 差集**（跑前快照最大
    rowid，跑后只看新增行），再按 callee_aic ∈ {辩手 AIC} 过滤 —— AIC
    由 `_active_aic()` 现场取，不写死显示名（显示名会改、AIC 不会）。
  · 事件选取不写死：A 组事件从经验库**现场派生**（优先有人核 gt 标注的），
    派不出就 SystemExit —— 不许拿「库里肯定有」当假设。
  · 期望不许取自被测对象自己的标记：A1/B3 断言的 lane 是 issue_ruling
    **落盘重算**的结果（不是模型自报 —— 契约里根本没有 lane 字段）。
  · 时间差解析失败 ≠ 通过：created_at 解析不了就**如实报「未判定」**，
    退出码给红（跳过 ≠ 通过）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from legal_acps.agents import OUTPUT_DIR  # noqa: E402

TASKS_DB = ROOT / ".wit" / "fact-judge" / "acps" / "aip-tasks.sqlite3"

#: B 组独造事件（库外新案）。写死是**正当**的：它是测试输入本身，且
#: 「真的是负对照」由跑前预判 decide_lane == debate 现场证死（B1）。
#: ⚠️ **负对照是一次性的**：B 轮裁定（lane=debate）跑完自动入库成为合格
#:    先例 ⇒ 同一事件第二次跑就不再是库外新案（预判会当场拦下）。重跑时
#:    用 `--b-event` 传一个新事件。
B_EVENT = (
    "网传某市第一实验小学要求全体学生寒假期间到校补课，"
    "并收取每人2600元补课费，不缴费者下学期开学不得报到"
)

#: B2 时间差上限（秒）。并发扇出实测 34.6ms；给到 2s 容调度抖动，
#: 但必须远小于「等正方回包再派反方」的旧形态（40+s）。
FANOUT_WINDOW_S = 2.0


# ══════════════════════════════════════════════════════════════════════
# 现场派生：A 事件 / B 预判 / 辩手 AIC / 任务库快照
# ══════════════════════════════════════════════════════════════════════


def pick_fast_event() -> tuple[str, dict]:
    """从经验库现场派生一条必然触发快车道的事件。

    优先挑有人核 gt 标注的案例（graded 比历史默认更硬），用案例自己的
    event 文本预判 —— 同一事件 ratio=1.0 必命中，派不出说明**库现状不支持**，
    如实炸（不许拿假设开真机）。
    """
    from legal_acps.fast_lane import decide_lane
    from legal_acps.ruling_store import RulingStore

    store, _rep = RulingStore.load(OUTPUT_DIR)
    graded = [c for c in store if c.graded]
    pool = graded or list(store)
    for c in pool:
        info = decide_lane(c.event)
        if info["lane"] == "fast" and (
            info["qualified_precedents"] or info["aligned_official"]
        ):
            return c.event, info
    raise SystemExit(
        f"[预判失败] 经验库 {len(store)} 条（其中 {len(graded)} 条有 gt 标注）"
        "竟派不出一条必然触发快车道的事件 —— 库现状不支持本探针，先归因再跑"
    )


def precheck_debate_event(event: str) -> dict:
    """B 组负对照的跑前证死：预判必须是 debate，否则「负对照」不成立。"""
    from legal_acps.fast_lane import decide_lane

    info = decide_lane(event)
    if info["lane"] != "debate" or (info["qualified_precedents"]
                                   or info["aligned_official"]):
        raise SystemExit(
            "[预判失败] B 事件在库里竟有命中（lane="
            f"{info['lane']}，先例 {len(info['qualified_precedents'])} 条、"
            f"官方 {len(info['aligned_official'])} 条）—— 负对照不真负，"
            "换一个库外事件再跑"
        )
    return info


def debater_aics() -> set[str]:
    """辩手 AIC 现场取（不写死显示名 —— 显示名会改、AIC 不会）。"""
    from legal_acps.run_agent import _active_aic

    aics = {_active_aic(r) for r in ("debate-pro", "debate-con")}
    aics.discard(None)
    if not aics:
        raise SystemExit("[预检失败] 取不到辩手 AIC —— 服务器身份文件不在？")
    return aics  # type: ignore[return-value]


def snapshot_tasks() -> int:
    """跑前快照：任务库当前最大 rowid（本轮 = 此后的新增行）。"""
    con = sqlite3.connect(f"file:{TASKS_DB}?mode=ro", uri=True)
    try:
        return int(con.execute("SELECT COALESCE(MAX(rowid), 0) FROM aip_tasks")
                   .fetchone()[0])
    finally:
        con.close()


def new_delegations(since_rowid: int, aics: set[str]) -> list[dict]:
    """rowid 差集 + 辩手 AIC 过滤 ⇒ 本轮向辩手的出站委派行。

    ⚠️ 列名是 ``remote_aic``（2026-10-13 真机实测：第一版照着
       ``_probe_fanout_tasks.py`` 的**候选键清单**写成 ``callee_aic``——
       那个脚本用 ``if k in d`` 过滤，候选键 ≠ 真实列。真实列里没有
       ``callee_aic`` ⇒ ``d.get()`` 恒空 ⇒ B2 恒红、A2 恒绿（空转）。
       一个词没命中 ≠ 没做 —— 负对照把空转判据逼了出来，这里钉死。
    """
    con = sqlite3.connect(f"file:{TASKS_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = list(con.execute(
            "SELECT * FROM aip_tasks WHERE rowid > ? ORDER BY rowid", (since_rowid,)
        ))
    finally:
        con.close()
    out = []
    for r in rows:
        d = dict(r)
        if (d.get("direction") == "outbound"
                and str(d.get("remote_aic") or "") in aics):
            out.append(d)
    return out


def fanout_gap_seconds(rows: list[dict]) -> float | None:
    """两条辩手委派的创建时间差（秒）；解析不了返回 None（由调用方如实报）。"""
    if len(rows) < 2:
        return None
    ts = []
    for d in rows[:2]:
        raw = str(d.get("created_at") or "")
        try:
            ts.append(float(raw))  # epoch 形态
        except ValueError:
            try:
                ts.append(datetime.fromisoformat(raw).timestamp())
            except ValueError:
                return None
    return abs(ts[1] - ts[0])


# ══════════════════════════════════════════════════════════════════════
# 真机一轮（leader 进程内直跑，与 e2e_leader_image 同形态）
# ══════════════════════════════════════════════════════════════════════


def task_text(event: str) -> str:
    return (
        f"网传：{event}。请核实这个说法是否属实，给出结论与依据。"
        "（上面引号外的事件描述请作为待核实的命题原文使用，不要改写主体与事实点。）"
    )


async def run_probe(
    runtime: object,
    parts: list[object],
    task: str,
    config: object,
    *,
    role: str = "leader",
    wit_dir: str | None = None,
):
    """跑这一轮 —— **必须经发起轮台账**（与 `e2e_leader_image.run_probe` 同款）。

    做成可 import 的函数，`legal_acps.check_local` 3c 才能塞假 runtime 进来，
    行为断言「探针这一轮真的记了台账」—— 探针的台账行为不能只靠人肉检查。
    """
    from legal_acps import run_ledger

    return await run_ledger.run_ledgered(
        lambda: runtime.run(parts, config=config),
        text=task, label=run_ledger.PROBE_LABEL, role=role, wit_dir=wit_dir,
    )


async def run_leader_round(event: str) -> tuple[object, list[str], str]:
    """跑一轮 leader→fact-judge，返回 (run 结果, 新增裁定书名, 回复文本)。"""
    from wit import RunConfig, TextPart, WitRuntime

    from legal_acps.acps_config import build_config
    from legal_acps.agents import build_agent
    from legal_acps.llm import build_model
    from legal_acps.run_agent import _active_aic, research_run_config

    role = "leader"
    agent = build_agent(role, build_model(), self_aic=_active_aic(role))
    runtime = WitRuntime(agent, workspace=f".wit/{role}",
                         acps=build_config(role=role))
    text = task_text(event)
    before = {p.name for p in OUTPUT_DIR.glob("ruling-*.json")}
    run, _rec = await run_probe(
        runtime, [TextPart(text=text)], text,
        RunConfig(max_steps=research_run_config().max_steps), role=role,
    )
    after = {p.name for p in OUTPUT_DIR.glob("ruling-*.json")}
    new = sorted(after - before)
    # 回复文本取 output_parts 里的 TextPart（⚠️ 不认 ReasoningPart —— 它也有
    # .text，不过滤会把模型内部思考当最终答复打出来；同 e2e_leader_image）。
    reply = "\n".join(
        t for part in (getattr(run, "output_parts", None) or [])
        if type(part).__name__ == "TextPart"
        for t in [getattr(part, "text", None)]
        if isinstance(t, str) and t
    )
    return run, new, reply


def lane_of(ruling_name: str) -> str:
    return str(json.loads((OUTPUT_DIR / ruling_name).read_text(
        encoding="utf-8")).get("lane") or "")


# ══════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════


async def main() -> int:
    ap = argparse.ArgumentParser(description="真机 A/B：快车道两向判据")
    ap.add_argument("--brief", action="store_true", help="不打长回复文本")
    ap.add_argument("--b-event", default=B_EVENT,
                    help="B 组库外新案的事件文本（负对照一次性 ⇒ 重跑必换新事件，"
                         "预判会现场证死它真的是库外）")
    args = ap.parse_args()

    from dotenv_lite import load_env

    load_env(ROOT / ".env")

    print("=" * 70)
    print("  真机 A/B · 快车道分级（正对照触发 / 负对照走辩论道）")
    print("=" * 70)

    a_event, a_pre = pick_fast_event()
    print(f"[A·预判] lane={a_pre['lane']}  先例 {len(a_pre['qualified_precedents'])}"
          f" 条 / 官方 {len(a_pre['aligned_official'])} 条")
    print(f"[A·事件] {a_event[:80]}")
    b_pre = precheck_debate_event(args.b_event)
    print(f"[B·预判] lane={b_pre['lane']}（负对照已现场证死为库外新案）")
    print(f"[B·事件] {args.b_event[:80]}")
    aics = debater_aics()
    print(f"[预检] 辩手 AIC {len(aics)} 个已现场取到")
    if not TASKS_DB.exists():
        raise SystemExit(f"[预检失败] 任务库不在：{TASKS_DB}")

    bad: list[str] = []

    # ── A｜正对照：命中即裁，跳过辩手 ──────────────────────────────
    print("\n[A] 跑正对照轮（leader → fact-judge，应走快车道）…")
    snap_a = snapshot_tasks()
    run_a, new_a, reply_a = await run_leader_round(a_event)
    print(f"[A] run 完成：status={getattr(run_a, 'status', '?')} "
          f"iterations={getattr(run_a, 'iterations', '?')} 新增裁定 {new_a or '无'}")
    # A1 落盘交付：lane == fast
    if not new_a:
        bad.append("A1 无新增裁定书（status=completed 不算数，交付唯一判据落空）")
    else:
        lane = lane_of(new_a[-1])
        print(f"[A1] 落盘 lane={lane!r}")
        if lane != "fast":
            bad.append(f"A1 落盘 lane={lane!r} ≠ fast —— 受理端真判没触发快车道"
                       "（模型改写事件致对齐失败？预判与真判漂移？归因后重跑）")
    # A2 跳过辩手：本轮任务库无辩手委派
    dele_a = new_delegations(snap_a, aics)
    print(f"[A2] 本轮辩手委派 {len(dele_a)} 条")
    if dele_a:
        bad.append(f"A2 快车道轮竟派了辩手 {len(dele_a)} 条 —— instructions 的"
                   " fast 分支在真机上没管住模型（装了 ≠ 会响）")
    # A3 主控收文（软判据：只打印）
    if not args.brief:
        print(f"[A3·回复] {reply_a[:600]}")
    else:
        print(f"[A3·回复摘要] {reply_a[:120]}")

    # ── B｜负对照：库外新案，辩论道照走 ────────────────────────────
    print("\n[B] 跑负对照轮（库外新案，应并发扇出辩手）…")
    snap_b = snapshot_tasks()
    run_b, new_b, reply_b = await run_leader_round(args.b_event)
    print(f"[B] run 完成：status={getattr(run_b, 'status', '?')} "
          f"iterations={getattr(run_b, 'iterations', '?')} 新增裁定 {new_b or '无'}")
    # B2 辩手真被派出 + 时间差毫秒级
    dele_b = new_delegations(snap_b, aics)
    gap = fanout_gap_seconds(dele_b)
    print(f"[B2] 本轮辩手委派 {len(dele_b)} 条  时间差={gap}")
    if len(dele_b) < 2:
        bad.append(f"B2 辩手委派只有 {len(dele_b)} 条（应为 2）—— 分级改写把"
                   "扇出改丢了，或裁判没走受理工具")
    elif gap is None:
        bad.append("B2 时间差未判定（created_at 解析不了）—— 跳过 ≠ 通过，"
                   "人工核任务库后修解析")
    elif gap > FANOUT_WINDOW_S:
        bad.append(f"B2 扇出时间差 {gap:.1f}s 超窗 —— 并发扇出退化成顺序派发")
    # B3 落盘交付：lane == debate
    if not new_b:
        bad.append("B3 无新增裁定书 —— 辩论道整轮没走完（委派都发了却没交付？）")
    else:
        lane_b = lane_of(new_b[-1])
        print(f"[B3] 落盘 lane={lane_b!r}")
        if lane_b != "debate":
            bad.append(f"B3 落盘 lane={lane_b!r} ≠ debate —— 库外新案被判进"
                       "快车道（负对照反向失守，比漏开更危险）")
    if not args.brief:
        print(f"[B·回复] {reply_b[:600]}")
    else:
        print(f"[B·回复摘要] {reply_b[:120]}")

    # ── 汇总 ────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    if bad:
        print(f"  ❌ {len(bad)} 条判据不成立：")
        for b in bad:
            print(f"    · {b}")
        return 1
    print("  ✅ A（正对照：命中即裁 + 跳过辩手 + 落盘 fast）"
          " / B（负对照：库外新案仍并发扇出 + 落盘 debate）两向判据全部成立")
    print("  （B 轮新裁定 lane=debate 已自动入库成为合格先例 —— "
          "「辩完入库供下次复用」，设计内，不清理）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
