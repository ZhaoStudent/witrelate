"""**真机端到端 · 组合研判**（方案①②缝合的真机验收 · #170–#173）。

问的问题只有一个：**媒体链与裁判链在真机上真的缝起来了吗？**

  ①（主控编排）：用户**同时**给出媒体材料与文字主张、问"这个说法是不是真的"
     ⇒ 主控必须在**同一条消息里**并发派「证据鉴证 Leader」与「网络裁判」。
     —— 串行是被终止钩子第 1 层**结构上锁死**的（先回齐的线会锁定交付工具，
        第二条线永远派不出去），所以"并发"不是优化、是唯一能跑通的形态。
  ②（共享库）：证据官出报告的同时把核验结论归档成事实库记录
     （`raw-evidence-live.jsonl`，url=`evidence://…`），之后**文本检索能命中它**。

════════════════════════════════════════════════════════════════════════
判据（全部打在"本轮"的事实上，跑前先快照，杜绝借用历史产物）
════════════════════════════════════════════════════════════════════════

  A′｜附件落盘收件箱 —— 沿用 `e2e_leader_image` 的口径（密码学哈希查磁盘，
      与模型措辞无关）。
  N｜**两链并发扇出**：本轮主控任务库（`.wit/leader/…`）新增出站委派里
      **同时**含 evidence-leader 与 fact-judge 两个目标，且两条（各自的
      首条）创建时间差 ≤ `FANOUT_WINDOW_S`。
      —— 时间差是关键：若主控"先等鉴证报告再派裁判"（串行反例），两条
        委派会相隔**分钟级**（鉴证链内层要跑 2~3 分钟）；并发则毫秒~秒级。
        这条判据专防"两条都派了、但派成了串行"的假绿。
  B′｜载体结论随终稿交付：样本独有位移 Δ=(312,0) 出现在**本轮新落盘**
      verdict 的 consistency_note 里（位移是本批样本参数，模型猜不出）。
  V｜事件裁定随终稿交付：同一份 verdict 带 band/truth_score（裁判线也回来了，
      两条线汇进**同一份**交付物）。
  R1｜核验记录落库：`raw-evidence-live.jsonl` 本轮新增行且 url 以
      `evidence://` 开头（②的写入侧）。
  R2｜**再检索命中**：跑完后用真库（含新记录）+ 带区分度词项的查询检索，
      top 命中里有 `evidence://` 条目（②的复利侧——落了库但检索不出来，
      与没落库在生产上没有区别）。
  T｜无 token 截断（三态，进退出码）。

⚠️ 跑前服务面（与 stance_swap 相反，**只停 leader**）：
  - **必须** `systemctl stop acps-leader`（探针进程内起主控，防抢工作区；
     Restart=always，kill 没用，必须 systemctl stop）。
  - fact-judge(8448) / debate-pro(8446) / debate-con(8447) /
    evidence-leader(8450) **必须**在跑；image 叶子(8449@s2) 由真实委派
    端到端验证（mTLS 探不了，B′/R1 就是那"真调一次"）。

用法（s1 仓根下）：
    ./.venv/bin/python -m run.e2e_combined
    ./.venv/bin/python -m run.e2e_combined --brief
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sqlite3
from pathlib import Path

from dotenv_lite import load_env

load_env()

from wit import ImagePart, RunConfig, TextPart, WitRuntime  # noqa: E402

from legal_acps.acps_config import build_config  # noqa: E402
from legal_acps.agents import build_agent  # noqa: E402
from legal_acps.evidence.inbox import (  # noqa: E402
    InboxRefused,
    handle_for,
    inbox_root,
    resolve,
)
from legal_acps.fact_store import (  # noqa: E402
    EVIDENCE_LIVE_FILE,
    EVIDENCE_URL_PREFIX,
    FACTS_DIR,
    FactStore,
)
from legal_acps.llm import build_model  # noqa: E402
from legal_acps.run_agent import _active_aic, research_run_config  # noqa: E402
from legal_acps.token_budget import truncated_of  # noqa: E402

# 现场派生工具与 e2e_fast_lane / e2e_leader_image **共用同一份**（单一来源：
# 扇出窗口常量、时间差解析、样本制造、终稿位移判据都钉在那边）。
from run.e2e_fast_lane import FANOUT_WINDOW_S, fanout_gap_seconds  # noqa: E402
from run.e2e_leader_image import (  # noqa: E402
    dump_parts,
    evidence_in_verdict,
    make_images,
    newest_verdict,
    output_text,
)


async def run_probe(
    runtime: object,
    parts: list[object],
    task: str,
    config: "RunConfig",
    *,
    role: str = "leader",
    wit_dir: str | None = None,
):
    """跑这一轮 —— **必须经发起轮台账**。

    实现在 `e2e_leader_image.run_probe`（单一来源，那里有整段"为什么要做成
    可 import 函数"的论证）；这里必须**再包一层**而不是直接 import 顶名使用：
    3c 的清单判据按"本文件有没有 `def run_probe(`"扫（它只读文本不 import，
    为的是不把 numpy 拉进自检进程），且它驱动的是**本探针自己的**入口。
    """
    from run.e2e_leader_image import run_probe as _ledgered

    return await _ledgered(runtime, parts, task, config, role=role, wit_dir=wit_dir)

ROOT = Path(__file__).resolve().parent.parent

#: 主控自己的任务库——判据 N 看的是**主控出站**的两条委派
#: （e2e_fast_lane 的 TASKS_DB 是 fact-judge 的库，**不是**同一口井，
#   列名教训 `remote_aic` 与 `direction` 过滤在那边钉死，这里照抄口径不抄实现）。
LEADER_DB = ROOT / ".wit" / "leader" / "acps" / "aip-tasks.sqlite3"

#: 事件主张（库外新案）。⚠️ 刻意带高区分度专名「星湖湾」：R2 的检索查询要靠
#: 它命中新落库的核验记录（正文里 subject 会引用案情说明）；通用词在 149 条
#: 真库里会撞出一堆同域候选，把判据变成抛硬币。
#: ⚠️ 主张原文**不许含 FALSE_PAT 定性词**（不实/辟谣/谣言系）——它会被写进
#: 核验记录的 body，撞上安全闸 ⇒ 记录被拒落库 ⇒ R1 红得不明不白。
CLAIM = (
    "网传星湖湾商场在周年庆促销活动中发生踩踏，多名顾客受伤，"
    "商场为了压消息已经把监控视频都删了。"
)

#: R2 的检索查询：区分度词项 + 案情上下文（真实受理检索是长查询形态，
#: ⑦-10 的教训：短查询在小库上分数不可靠）。
RETRIEVAL_QUERY = "星湖湾商场 周年庆 促销 踩踏 现场照片 复制粘贴"


def task_text() -> str:
    """组合分支的触发形态：媒体**作为主张的证据**出示 + 问"说法是不是真的"。

    ⚠️ 与 e2e_leader_image.task_text 的差别是刻意的：那边是"看看图改没改，
       **另外**按标准流程研判文字"（两个并列请求，主问载体）；这边把照片
       写成主张的"现场证据"、主问**说法真假**——组合分支的触发条件
       （agents.py「组合研判分支」节）就是这么写的，措辞差一点就会滑进
       别的分支，白跑一轮。
    """
    return (
        f"我在家庭群里收到一张图和一段话：「{CLAIM}」"
        "群友都说这张照片就是踩踏现场的实拍图。"
        "请帮我核实这个说法到底是不是真的：这张照片有没有被改过、"
        "是不是拼接的；以及这个踩踏说法本身是否属实。"
    )


def port_listening(port: int) -> bool:
    """端口有没有人在听。TCP 层即可——mTLS 探不了应用层，这里也不需要。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def snapshot_leader_tasks() -> int:
    """跑前快照：主控任务库当前最大 rowid（本轮 = 此后的新增行）。"""
    con = sqlite3.connect(f"file:{LEADER_DB}?mode=ro", uri=True)
    try:
        return int(
            con.execute("SELECT COALESCE(MAX(rowid), 0) FROM aip_tasks").fetchone()[0]
        )
    finally:
        con.close()


def new_outbound(since_rowid: int, aics: set[str]) -> list[dict]:
    """rowid 差集 + 目标 AIC 过滤 ⇒ 本轮主控的出站委派行。

    ⚠️ 列名是 ``remote_aic``（e2e_fast_lane.new_delegations 钉死的真机教训：
       写 ``callee_aic`` 恒空 ⇒ 假绿）。direction 出站 = ``outbound``。
    """
    con = sqlite3.connect(f"file:{LEADER_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = list(
            con.execute(
                "SELECT * FROM aip_tasks WHERE rowid > ? ORDER BY rowid", (since_rowid,)
            )
        )
    finally:
        con.close()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("direction") == "outbound" and str(d.get("remote_aic") or "") in aics:
            out.append(d)
    return out


def live_records() -> list[dict]:
    """`raw-evidence-live.jsonl` 当前的全部行（文件不存在 = 空列表）。"""
    p = FACTS_DIR / EVIDENCE_LIVE_FILE
    if not p.exists():
        return []
    out: list[dict] = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            out.append({"_bad_line": ln[:80]})
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", action="store_true", help="不打长文本，只看结构")
    parser.add_argument(
        "--max-steps", type=int, default=0,
        help="覆盖步数预算（0 = research_run_config() 正式值）。"
             "传比正式值小的数 = 负对照（预期 N/B′/R 变红）。",
    )
    args = parser.parse_args()

    # ── 预检：服务面 ───────────────────────────────────────────────────
    ev_role, judge_role = "evidence-leader", "fact-judge"
    ev_aic = _active_aic(ev_role)
    judge_aic = _active_aic(judge_role)
    if not ev_aic or not judge_aic:
        print(f"[预检失败] 取不到 AIC：{ev_role}={bool(ev_aic)} "
              f"{judge_role}={bool(judge_aic)} —— 身份文件不在？")
        return 1
    if port_listening(8445):
        print("[预检失败] 8445 还有人听 —— leader 服务没停。"
              "先 systemctl stop acps-leader（探针进程内起主控，防抢工作区）。")
        return 1
    for port, name in ((8448, "fact-judge"), (8446, "debate-pro"),
                       (8447, "debate-con"), (8450, "evidence-leader")):
        if not port_listening(port):
            print(f"[预检失败] {name}({port}) 没在听 —— 组合研判两条链都要真跑。")
            return 1

    # ── 预检：事件主张撞不撞快车道（只报告，不当判据——fast 也是合法裁定）──
    lane_note = ""
    try:
        from legal_acps.fast_lane import decide_lane

        info = decide_lane(CLAIM)
        lane_note = (f"lane={info['lane']}（先例 {len(info['qualified_precedents'])} 条、"
                     f"官方 {len(info['aligned_official'])} 条）")
    except Exception as exc:  # noqa: BLE001 —— 预检失败不挡跑，但必须如实报
        lane_note = f"预判失败：{type(exc).__name__}: {exc}"

    samples = make_images(1)          # 单图 720×540 复制-移动样本，Δ=(312,0)
    jpegs = [b for b, _ in samples]
    dxs = [dx for _, dx in samples]
    mime = "image/jpeg"
    expected_handles = [handle_for(b) for b in jpegs]
    task = task_text()

    print("=" * 70)
    print("  真机端到端 · 组合研判（媒体链 × 裁判链 · 方案①②缝合验收）")
    print("=" * 70)
    print(f"  图片        ：{len(jpegs)} 张 {len(jpegs[0])} 字节  位移 Δ=({dxs[0]},0)")
    print(f"  事件主张    ：{CLAIM[:48]}…")
    print(f"  快车道预判  ：{lane_note}")
    print(f"  证据官 AIC  ：{ev_aic}")
    print(f"  网络裁判 AIC：{judge_aic}")

    config_hint = (
        research_run_config()
        if args.max_steps <= 0
        else RunConfig(max_steps=args.max_steps)
    )
    print(f"  步数预算    ：{config_hint.max_steps} 步（服务模式钉死 {RunConfig().max_steps}）")

    role = "leader"
    agent = build_agent(role, build_model(), self_aic=_active_aic(role))
    config = build_config(role=role)
    runtime = WitRuntime(agent, workspace=f".wit/{role}", acps=config)

    parts: list[object] = [TextPart(text=task)]
    for i, b in enumerate(jpegs, 1):
        parts.append(ImagePart(mime_type=mime, data=b, name=f"scene{i}.jpg"))

    # ── 跑前快照（判据全部只认"本轮新增"，杜绝借用历史产物）────────────
    snap_tasks = snapshot_leader_tasks()
    before_live = live_records()
    before_verdict = newest_verdict()

    print(f"\n  快照：任务库 rowid≤{snap_tasks}  核验记录 {len(before_live)} 行  "
          f"verdict={'有' if before_verdict else '无'}")
    print("\n  正在跑……（两链并行：鉴证 ~2-3 分钟 + 辩论 ~3-6 分钟，取 max）")

    try:
        result, ledger_run = await run_probe(runtime, parts, task, config_hint, role=role)
    except Exception as exc:  # noqa: BLE001 - 全真跑要把失败原样打出来给人看
        print(f"  ❌ run 抛异常：{type(exc).__name__}: {exc}")
        return 1

    print(f"\n  [台账] run_id = {ledger_run.run_id}  label = {ledger_run.label}")
    print(f"  status = {result.status}   迭代 = {result.iterations} 轮")

    if not args.brief:
        dump_parts("消息部件构成", result.messages)
        for obs in result.observations or []:
            name = obs.get("tool") or obs.get("name") or "?"
            print(f"  · {name:<28} status={obs.get('status', '?'):<8} "
                  f"{obs.get('duration_ms', '?')} ms")

    body = output_text(result.output_parts)
    print(f"\n{body if (body and not args.brief) else '  （最终文本略，--brief）'}")

    # ════════════════════════════════════════════════════════════════════
    # 判据
    # ════════════════════════════════════════════════════════════════════
    results: list[tuple[str, bool, str]] = []

    # A′｜附件落盘
    role_workspace = f".wit/{role}"
    box = inbox_root(Path(role_workspace))
    landed = []
    for h in expected_handles:
        try:
            resolve(h, root=box)
        except InboxRefused:
            landed.append(False)
        else:
            landed.append(True)
    crit_a = all(landed) and bool(landed)
    results.append(("A′ 附件落盘收件箱", crit_a,
                    f"{sum(landed)}/{len(landed)} 份字节在磁盘上（{box}）"))

    # N｜两链并发扇出（各自首条委派的时间差 ≤ 窗口）
    rows = new_outbound(snap_tasks, {ev_aic, judge_aic})
    first_by_target: dict[str, dict] = {}
    for r in rows:  # rows 按 rowid 升序 ⇒ setdefault 留下"首条"
        first_by_target.setdefault(str(r["remote_aic"]), r)
    crit_n = ev_aic in first_by_target and judge_aic in first_by_target
    n_detail = (f"出站委派 {len(rows)} 条：证据官 {sum(1 for r in rows if r['remote_aic'] == ev_aic)} 条、"
                f"裁判 {sum(1 for r in rows if r['remote_aic'] == judge_aic)} 条")
    if crit_n:
        pair = [first_by_target[ev_aic], first_by_target[judge_aic]]
        gap = fanout_gap_seconds(pair)
        if gap is None:
            crit_n = False
            n_detail += "；时间差解析失败 ⇒ 无法证明并发"
        else:
            crit_n = gap <= FANOUT_WINDOW_S
            n_detail += (f"；首条时间差 {gap:.3f}s"
                         f"（窗口 {FANOUT_WINDOW_S}s —— 串行反例会是分钟级）")
    else:
        miss = [t for t in (ev_aic, judge_aic) if t not in first_by_target]
        n_detail += f"；缺目标 {[m[:12] + '…' for m in miss]} ⇒ 两链没有都派出"
    results.append(("N 两链并发扇出", crit_n, n_detail))

    # B′ + V｜终稿（本轮新落盘才算数——负对照逼出来的教训）
    after = newest_verdict()
    fresh = after is not None and after != before_verdict
    note = ""
    verdict_data: dict = {}
    if fresh and after is not None:
        try:
            verdict_data = json.loads(after.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  （读 verdict 失败：{type(exc).__name__}: {exc}）")
        note = str(verdict_data.get("consistency_note") or "")
    crit_b, cited = evidence_in_verdict(note, dxs, fresh=fresh)
    results.append(("B′ 载体结论随终稿交付", crit_b,
                    "位移 Δ=(312,0) 在本轮新落盘 verdict 的 consistency_note 里"
                    if crit_b else
                    ("本轮没有新 verdict 落盘" if not fresh else
                     "新 verdict 里找不到本批样本的位移值")))
    crit_v = fresh and bool(verdict_data.get("band"))
    results.append(("V 事件裁定随终稿交付", crit_v,
                    f"band={verdict_data.get('band')} truth_score={verdict_data.get('truth_score')}"
                    if crit_v else "verdict 缺 band（裁判线没有汇进同一份交付物）"))

    # R1｜核验记录落库（写入侧）
    after_live = live_records()
    new_recs = after_live[len(before_live):]
    ev_recs = [r for r in new_recs
               if str(r.get("url") or "").startswith(EVIDENCE_URL_PREFIX)]
    crit_r1 = bool(ev_recs)
    results.append(("R1 核验记录落库", crit_r1,
                    f"新增 {len(new_recs)} 行（evidence:// {len(ev_recs)} 条）"
                    + (f" → {ev_recs[0].get('url')}" if ev_recs else "")))

    # R2｜再检索命中（复利侧）——用真库（含新记录）+ 区分度长查询
    crit_r2 = False
    r2_detail = "检索没有返回 evidence:// 条目"
    if crit_r1:
        store, rep = FactStore.load()
        hits = store.search(RETRIEVAL_QUERY, limit=5)
        ev_hits = [h for h in hits
                   if h.fact.url.startswith(EVIDENCE_URL_PREFIX)]
        crit_r2 = bool(ev_hits)
        top = hits[0] if hits else None
        r2_detail = (f"库 {len(store)} 条（本轮含新记录）；top1 "
                     f"{'evidence://' if top and top.fact.url.startswith(EVIDENCE_URL_PREFIX) else (top.fact.url[:40] if top else '无')}"
                     f" score={top.score:.2f}" if top else "库检索返回空")
        r2_detail += f"；evidence:// 命中 {len(ev_hits)} 条"
    else:
        r2_detail = "前置 R1 未成立，无从检索"
    results.append(("R2 核验记录可检索", crit_r2, r2_detail))

    # T｜无 token 截断（三态：0 干净 / n>0 截断 / -1 无法判定）
    truncated = truncated_of(result)
    crit_t = truncated == 0
    results.append(("T 无 token 截断", crit_t,
                    {0: "0 次 finish_reason=length", -1: "无法判定（拿不到 metadata）"}.get(
                        truncated, f"被截断 {truncated} 次")))

    print("\n" + "─" * 70 + "\n  判据\n" + "─" * 70)
    for name, ok, detail in results:
        print(f"  [{'✅' if ok else '❌'}] {name}：{detail}")

    print(f"\n  落盘：verdict={after.name if after else '无'}"
          f"{'（本轮新增）' if fresh else '（⚠️ 未新增）'}"
          f"  核验记录={FACTS_DIR / EVIDENCE_LIVE_FILE}")
    if fresh and not args.brief and note:
        print(f"\n  consistency_note（{len(note)} 字）：\n{note}")

    # 判据全进退出码：本探针**只判本轮**（跑前快照钉死），没有
    # e2e_leader_image 那种"历史产物借位"的常年红风险 ⇒ 红就是真红。
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
