"""真机双跑：立场对调实验（#124 · 2026-10-13）。

问题：同一事件、两次辩论，只把正反双方的立场**对调**，裁定还稳定吗？
裁定应当取决于证据，而不是「谁抽到了哪个立场」——立场本来就是裁判
随机指派的（与真相无关），若对调后裁定翻转，说明裁定在跟着立场走。

为什么必须在受理工具边界注入（而不是跑两轮常驻链路碰运气）：
  ① 立场是 fact-judge 模型调 `fact_arbitrate_claim` 时自己写进
     `ArbitrationCase.pro_position / con_position` 的，代码不指派 ⇒
     常驻链路上探针**无法保证**第二轮正好对调（P=1/2 碰运气，不是判据）；
  ② 第一轮裁定若进生产经验库，会立刻成为**合格先例**（辩论道产出有
     快裁触发资格，`fast_lane.is_qualified`），同一事件第二轮 `decide_lane`
     判 fast、**根本没有辩手**——「同事件双跑辩论」在常驻链路上结构性
     不可能（这正是 e2e_fast_lane「负对照一次性」告诫的根源）。

解法（两根针都拔掉）：
  · **受控注入**：`capability` 装饰器把 `Capability`（frozen dataclass）
    挂在函数属性 `func.capability` 上，模型侧走它的 `handler`。探针用
    `dataclasses.replace` **只换 handler**——id / description / schema
    原样保留（零抄写：抄一遍 description 就是给单一来源开副本），在工具
    **返回值**上把立场对调——受理返回值正是模型派发辩手时抄的底稿
    （2026-10-13 实测：委派 payload 的【正方立场】【反方立场】逐字来自它）。
    第二轮同时把 event / proposition / fact_sheet / official_materials
    **钉成第一轮的**：两轮唯一差异 = 立场分配，其余全同 ⇒ 归因干净。
  · **写隔离**：`issue_ruling` 落盘走模块级 `agents.OUTPUT_DIR`（调用时
    全局查找 ⇒ 运行时可换），探针把它指到本轮隔离目录；经验库**读**路径
    （`ruling_store.OUTPUT_DIR`，decide_lane / PrecedentHooks 用）刻意不动
    ⇒ 两轮都读生产库、判道都是 debate，而实验裁定**永不进生产库**。
    附带好处：本探针的事件**可复跑**（不像 e2e_fast_lane 的 B 负对照是一次
    性的——它跑完入库，本探针跑完不入库）。
    `queue_retry`（B 出路，verdict=无法判定时自动记复研判）同样用 wit_dir
    隔离——实验裁定不在生产库，它引用的文件也不该进生产复研判队列。

链路形态：进程内起 fact-judge（真实身份、真实 ACPs 委派），辩手走
**常驻服务**（8446 / 8447）——与 e2e_fast_lane 同款、往下一层。
leader 不在本实验链路里（立场分配与裁定都发生在裁判环节）。

    ⚠️ 跑前必做（探针不替你做，防抢工作区 / 防旧代码）：
        systemctl stop acps-fact-judge     # 探针进程内起裁判（同一个
                                           # .wit 工作区，双开必抢）
        （acps-debate-pro / acps-debate-con 必须在跑）
    ⚠️ 跑完必做：
        systemctl start acps-fact-judge

用法（服务器，仓库根目录；本地无 .env 跑不了真模型）：

    ./.venv/bin/python -m run.e2e_stance_swap
    ./.venv/bin/python -m run.e2e_stance_swap --brief
    ./.venv/bin/python -m run.e2e_stance_swap --event "…"   # 库外新案

判据（机制判据必须全绿；实验测量如实报告、不进退出码）：
  P0 预检：事件库外（跑前预判 decide_lane == debate，现场证死）、双辩手
     AIC 现场取到、8448 没人监听（裁判服务已停）。
  R1（第一轮·基线，立场按模型自然指派）：
     R1a 受理被记下：pro/con 立场非空、互不相同、受理 lane == debate；
     R1b 并发扇出仍在：两条辩手委派（正反各一）、创建时间差毫秒级；
     R1c 交付落盘：隔离目录新增裁定书、lane == debate。
  R2（第二轮·对调）：
     R2a 注入「送出去」：模型实际拿到的受理返回值 = 第一轮的
        event / 命题 / 事实清单 / 案底 + 立场互换（「送出去」半边）；
     R2b 注入「被采纳」：给正方的委派 payload 里逐字是第一轮**反方**
        立场、给反方的是第一轮**正方**立场——断言打在**任务库 payload**
        上，不打在注入代码自己的标记上（「探针响了 ≠ 响的是你要证的那
        件事」）。⚠️ 两个立场互为子串时（「属实」⊂「不属实」），反向
        「不含」判据会误伤 ⇒ 只断强方向并如实注明（处数/措辞不写死）；
     R2c 交付落盘：同 R1c。
  测量（exit code 之外，如实报告）：两轮 verdict / confidence 对比——
     一致 = 本对样本上对调鲁棒；不一致 = **发现**（裁定跟着立场走，或
     纯粹模型非确定性——单对样本分不开这两个原因，需重复实验归因）。
     ⚠️ 一致与否都**不是**「系统没问题/有问题」的结论：退出码只证明
     「实验有效」（机制判据全绿），测量结果看打印与 experiment.json。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import socket
import sqlite3  # noqa: F401 —— 与 e2e_fast_lane 同款：只在 docstring 里说明任务库
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 现场派生/任务库工具与 e2e_fast_lane **共用同一份**（单一来源：rowid 差集、
# remote_aic 列名教训、扇出时间差解析都在那边钉死过，这里不抄第二份）。
from run.e2e_fast_lane import (  # noqa: E402
    FANOUT_WINDOW_S,
    fanout_gap_seconds,
    new_delegations,
    precheck_debate_event,
    snapshot_tasks,
    task_text,
)

TASKS_DB = ROOT / ".wit" / "fact-judge" / "acps" / "aip-tasks.sqlite3"

#: 实验事件（库外新案）。写死是**正当**的：它是实验输入本身，「真的是
#: 库外」由跑前预判 decide_lane == debate 现场证死（P0）。
#: ⚠️ 与 e2e_fast_lane 的 B_EVENT 刻意不同事件——那条已入库成合格先例。
#: 本探针写隔离 ⇒ 本事件不会因跑过而入库，**可复跑**（见模块头）。
DEFAULT_EVENT = (
    "网传某市地铁集团下发内部通知，因信号系统全面升级，"
    "全市地铁自下月起每周五下午提前两小时收班"
)

#: fact-judge 服务端口。跑前必须停——探针进程内起裁判（防抢工作区）。
JUDGE_PORT = 8448


def port_listening(port: int) -> bool:
    """端口有没有人在听。TCP 层即可——mTLS 探不了应用层，这里也不需要。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def debater_aic_map() -> dict[str, str]:
    """辩手 AIC 现场取，**按角色分开**（{aic: role}）。

    本实验必须知道「谁是谁」：对调断言按 AIC 落，正反映射写反了不会报错，
    只会替假结论背书（e2e_fast_lane 只用集合，因为它不区分正反——这里不行）。
    """
    from legal_acps.run_agent import _active_aic

    out: dict[str, str] = {}
    for role in ("debate-pro", "debate-con"):
        aic = _active_aic(role)
        if not aic:
            raise SystemExit(f"[预检失败] 取不到 {role} 的 AIC —— 身份文件不在？")
        out[aic] = role
    return out


# ══════════════════════════════════════════════════════════════════════
# 注入：受理工具边界（capability handler 包装）+ 副写隔离
# ══════════════════════════════════════════════════════════════════════


def install_intake_hook(post) -> object:
    """把 `fact.arbitrate_claim` 的 handler 包一层（**唯一注入点**）。

    `capability` 装饰器把 `Capability`（frozen dataclass）挂在函数属性
    ``func.capability`` 上、模型侧调用走它的 ``handler``。探针用
    ``dataclasses.replace`` **只换 handler**——id / description /
    input_schema 全部原样（零抄写）。``post`` 收到工具返回值（=
    ``case.model_dump()`` + 案底 + 车道判定），改完交回——受理返回值
    正是模型派发辩手时抄的底稿。

    返回还原函数（finally 里必须调——同一进程里的下一轮要干净的原始形态）。
    """
    import dataclasses

    import legal_acps.agents as agents_mod

    fn = agents_mod.arbitrate_claim
    cap = fn.capability
    orig_handler = cap.handler

    def handler(*a, **k):
        res = orig_handler(*a, **k)
        if inspect.isawaitable(res):  # 防御：handler 形态由框架定，两种都接
            async def _chained():
                return post(await res)

            return _chained()
        return post(res)

    fn.capability = dataclasses.replace(cap, handler=handler)

    def restore():
        fn.capability = cap

    return restore


def isolate_side_writes(round_dir: Path):
    """把本轮的两个**副写**指到隔离目录，返回还原函数。

    ① ``agents.OUTPUT_DIR``：`issue_ruling` 落盘裁定书（模块级全局、调用
       时查找 ⇒ 运行时可换）。经验库**读**路径（``ruling_store.OUTPUT_DIR``）
       刻意不动——两轮都读生产库、先例注入条件一致，而实验裁定永不进
       生产库（否则第一轮立刻成合格先例，第二轮判 fast，双跑塌掉）。
    ② ``lead_recon.queue_retry``：B 出路（verdict=无法判定 ⇒ 自动记复研判）。
       `issue_ruling` 在函数体内局部 import 它 ⇒ 换模块属性即生效；
       实验裁定不在生产库，它的 retry 记录引用的文件也不该进生产队列。
    """
    import legal_acps.agents as agents_mod
    import legal_acps.lead_recon as lead_recon_mod

    orig_output = agents_mod.OUTPUT_DIR
    orig_queue = lead_recon_mod.queue_retry
    round_dir.mkdir(parents=True, exist_ok=True)
    agents_mod.OUTPUT_DIR = round_dir

    def _queue_retry(event, ruling_source="", **k):
        k.setdefault("wit_dir", str(round_dir))
        return orig_queue(event, ruling_source, **k)

    lead_recon_mod.queue_retry = _queue_retry

    def restore():
        agents_mod.OUTPUT_DIR = orig_output
        lead_recon_mod.queue_retry = orig_queue

    return restore


# ══════════════════════════════════════════════════════════════════════
# 真机一轮（fact-judge 进程内直跑 → 常驻辩手）
# ══════════════════════════════════════════════════════════════════════


async def run_probe(runtime: object, parts: list, task: str, *, role: str,
                    wit_dir: str | None = None):
    """跑这一轮 —— **必须经发起轮台账**（与 e2e_fast_lane 同款）。

    ⚠️ 预算**刻意**用 ``RunConfig()``（默认 8 步）：裁判在服务模式就是这个
       预算（RunConfig 默认值），本实验要复刻的正是生产行为——所以这里
       **不**用 ``research_run_config()``（那是主控一次性研判的 14 步），
       3c 里也按 needs_config=False 登记。
    做成可 import 的函数，`legal_acps.check_local` 3c 才能塞假 runtime
    进来行为断言「探针这一轮真的记了台账」。
    """
    from wit import RunConfig

    from legal_acps import run_ledger

    return await run_ledger.run_ledgered(
        lambda: runtime.run(parts, config=RunConfig()),
        text=task, label=run_ledger.PROBE_LABEL, role=role, wit_dir=wit_dir,
    )


async def run_judge_round(event: str, round_dir: Path, *, pin: dict | None,
                          holder: dict) -> tuple[object, list[str]]:
    """进程内跑一轮 fact-judge（受理 → 发现 → 并发派发 → 等待 → 裁定）。

    参数
    ----
    pin
        ``None`` = 基线轮（受理透传，只记录）；传第一轮的受理快照 = 对调轮
        （event / proposition / fact_sheet / 案底钉成第一轮的，立场互换）。
    holder
        探针的记录口袋：``original`` = 工具原始返回值；``intake`` = 模型
        **实际拿到**的返回值（对调轮 = 改写后的）；``rewritten`` = 改写记录。
        模型多次调受理工具时按**最后一次**计（委派跟的是最后一次受理）。

    返回 (run 结果, 隔离目录里的裁定书文件名列表)。
    注入与副写隔离都在 finally 里还原——还原被跳过的风险只存在于本探针
    进程内，不波及常驻服务（它们是别的进程）。
    """

    def post(out):
        holder["original"] = out
        eff = out
        if pin is not None and isinstance(out, dict):
            eff = {
                **out,
                "event": pin.get("event"),
                "proposition": pin.get("proposition"),
                "fact_sheet": pin.get("fact_sheet"),
                "pro_position": pin.get("con_position"),
                "con_position": pin.get("pro_position"),
                "official_materials": pin.get("official_materials"),
                "official_materials_error": pin.get("official_materials_error"),
            }
            holder["rewritten"] = eff
        holder["intake"] = eff
        return eff

    restore_hook = install_intake_hook(post)
    restore_writes = isolate_side_writes(round_dir)
    try:
        from wit import TextPart, WitRuntime

        from legal_acps.acps_config import build_config
        from legal_acps.agents import build_agent
        from legal_acps.llm import build_model
        from legal_acps.run_agent import _active_aic

        role = "fact-judge"
        agent = build_agent(role, build_model(), self_aic=_active_aic(role))
        runtime = WitRuntime(agent, workspace=f".wit/{role}",
                             acps=build_config(role=role))
        text = task_text(event)
        run, _rec = await run_probe(
            runtime, [TextPart(text=text)], text, role=role,
        )
    finally:
        restore_hook()
        restore_writes()
    rulings = sorted(p.name for p in round_dir.glob("ruling-*.json"))
    return run, rulings


def payload_text(row: dict | None) -> str:
    """从委派行取 payload 全文（实测结构：``command_history`` JSON 字符串
    → list → 首项 ``dataItems[*].text``。列名教训见 e2e_fast_lane：真实
    列就是 ``command_history`` / ``remote_aic``，不猜候选键）。"""
    if not row:
        return ""
    ch = row.get("command_history")
    try:
        obj = json.loads(ch) if isinstance(ch, str) else ch
        item = obj[0] if isinstance(obj, list) and obj else obj
        items = (item or {}).get("dataItems") or []
        return "".join(str(i.get("text") or "") for i in items
                       if isinstance(i, dict))
    except Exception:  # noqa: BLE001 —— 解析不了 ⇒ 空串，由断言如实报红
        return ""


def check_swap_in_payloads(text_pro: str, text_con: str,
                           pro_stance_r1: str, con_stance_r1: str) -> list[str]:
    """R2b：对调真的被模型**采纳**了吗？断言打在任务库 payload 上。

    强方向（必须成立）：对方的第一轮立场逐字出现在本方 payload；
    反向「不含」（立场没换过来的证据）只在两个立场**互不为子串**时断——
    「属实」⊂「不属实」时它会被强方向的命中顺带满足，断它就是误伤。
    """
    problems: list[str] = []
    pro_in_con = bool(pro_stance_r1) and pro_stance_r1 in con_stance_r1
    con_in_pro = bool(con_stance_r1) and con_stance_r1 in pro_stance_r1
    if not text_pro or con_stance_r1 not in text_pro:
        problems.append("R2b 给正方的委派 payload 里没有第一轮反方立场"
                        "（对调未被采纳 / 被改写 / payload 解析失败）")
    if not text_con or pro_stance_r1 not in text_con:
        problems.append("R2b 给反方的委派 payload 里没有第一轮正方立场"
                        "（对调未被采纳 / 被改写 / payload 解析失败）")
    if not pro_in_con and pro_stance_r1 in text_pro:
        problems.append("R2b 给正方的 payload 里竟有第一轮**正方**立场"
                        "——立场没换过来（模型没跟受理返回值走）")
    if not con_in_pro and con_stance_r1 in text_con:
        problems.append("R2b 给反方的 payload 里竟有第一轮**反方**立场"
                        "——立场没换过来（模型没跟受理返回值走）")
    if pro_in_con or con_in_pro:
        print(f"[R2b·注] 两轮立场互为子串（pro⊂con={pro_in_con}, con⊂pro={con_in_pro}），"
              "反向「不含」判据跳过——强方向已断")
    return problems


def fanout_problems(rows: list[dict], aic_map: dict[str, str],
                    tag: str) -> list[str]:
    """R1b / R2·扇出的判据（2026-10-18 追问轮起放宽为 2–4 条）。

    裁判定向追问（`termination._maybe_followup`，fact-judge 装配
    followup_rounds=1）上线后，辩手委派总数不再是恒 2：
      · **首波**（时间序前 2 条）必须仍是正反各一、时间差毫秒级 ——
        并发扇出不许被追问功能顺手退化；
      · 追加委派（第 3–4 条，若有）是**追问轮**：目标仍只能是辩手；
        时间上本来就在辩论回齐之后，**不计时间窗**。
    rows 顺序 = 任务库 rowid 递增 = 委派创建顺序（new_delegations 单一来源）。
    """
    problems: list[str] = []
    print(f"[{tag}] 辩手委派 {len(rows)} 条")
    if not 2 <= len(rows) <= 4:
        problems.append(f"{tag} 辩手委派 {len(rows)} 条（应为 2–4："
                        "2=无追问，3–4=追问轮）—— 扇出/链路出问题")
        return problems
    first_roles = [aic_map.get(str(r.get("remote_aic"))) for r in rows[:2]]
    extra_roles = [aic_map.get(str(r.get("remote_aic"))) for r in rows[2:]]
    if set(first_roles) != {"debate-pro", "debate-con"}:
        problems.append(f"{tag} 首波两条委派不是正反各一：{first_roles}")
        return problems
    if any(r not in ("debate-pro", "debate-con") for r in extra_roles):
        problems.append(f"{tag} 追问轮委派竟派给了非辩手：{extra_roles}")
    gap = fanout_gap_seconds(rows)
    if extra_roles:
        print(f"[{tag}] 追问轮委派 {len(extra_roles)} 条 → {extra_roles}"
              "（晚于首波，不计时窗）")
    if gap is None:
        problems.append(f"{tag} 首波时间差未判定（created_at 解析不了）—— 跳过 ≠ 通过")
    elif gap > FANOUT_WINDOW_S:
        problems.append(f"{tag} 首波扇出时间差 {gap:.1f}s 超窗 —— 并发扇出退化成顺序派发")
    return problems


# ══════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════


async def main() -> int:
    ap = argparse.ArgumentParser(description="真机双跑：立场对调实验")
    ap.add_argument("--brief", action="store_true", help="不打长 payload 文本")
    ap.add_argument("--event", default=DEFAULT_EVENT,
                    help="库外新案的事件文本（跑前预判会现场证死它真的是库外）")
    args = ap.parse_args()

    from dotenv_lite import load_env

    load_env(ROOT / ".env")

    print("=" * 70)
    print("  真机双跑 · 立场对调实验（同一事件，正反立场互换，看裁定稳不稳）")
    print("=" * 70)

    # ── P0 预检 ─────────────────────────────────────────────────────
    pre = precheck_debate_event(args.event)
    print(f"[P0] 事件库外已证死：lane={pre['lane']}")
    print(f"[P0] 事件：{args.event[:80]}")
    aic_map = debater_aic_map()
    if not TASKS_DB.exists():
        raise SystemExit(f"[预检失败] 任务库不在：{TASKS_DB}")
    if port_listening(JUDGE_PORT):
        raise SystemExit(
            f"[预检失败] {JUDGE_PORT} 仍有人监听 —— acps-fact-judge 没停。"
            "探针要进程内起裁判（同一个 .wit 工作区，双开必抢）。"
            "先 systemctl stop acps-fact-judge 再跑；"
            "跑完 systemctl start acps-fact-judge。")
    print(f"[P0] {JUDGE_PORT} 无监听（裁判服务已停）、辩手 AIC 已取到、任务库在")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = ROOT / "output_probes" / f"stance-swap-{stamp}"
    bad: list[str] = []

    # ── R1 基线轮（立场按模型自然指派）──────────────────────────────
    print("\n[R1] 跑基线轮（fact-judge 进程内 → 常驻辩手）…")
    r1_dir = base / "round1"
    h1: dict = {}
    snap1 = snapshot_tasks()
    run1, rulings1 = await run_judge_round(args.event, r1_dir, pin=None, holder=h1)
    print(f"[R1] run 完成：status={getattr(run1, 'status', '?')} "
          f"iterations={getattr(run1, 'iterations', '?')} 新增裁定 {rulings1 or '无'}")
    out1 = h1.get("intake") if isinstance(h1.get("intake"), dict) else {}
    pro1 = str(out1.get("pro_position") or "")
    con1 = str(out1.get("con_position") or "")
    if not pro1 or not con1:
        bad.append("R1a 受理没记下立场（模型没调受理工具，或返回值没到探针手里）")
    elif pro1 == con1:
        bad.append("R1a 正反立场相同（互斥是 ArbitrationCase 契约要求）")
    if str(out1.get("lane") or "") != "debate":
        bad.append(f"R1a 受理 lane={out1.get('lane')!r} ≠ debate —— "
                   "库外预判与真判漂移，先归因再跑")
    else:
        print(f"[R1a] 立场已记下：正方「{pro1[:40]}…」 反方「{con1[:40]}…」"
              f"（受理 lane=debate）")
    rows1 = new_delegations(snap1, set(aic_map))
    bad += fanout_problems(rows1, aic_map, "R1b")
    if not args.brief:
        for r in rows1:
            print(f"[R1b·payload/{aic_map.get(str(r.get('remote_aic')))}] "
                  f"{payload_text(r)[:200]}")
    j1: dict = {}
    if not rulings1:
        bad.append("R1c 隔离目录没有新裁定书 —— 整轮没走完"
                   "（status=completed 不算数，交付唯一判据落空）")
    else:
        j1 = json.loads((r1_dir / rulings1[-1]).read_text(encoding="utf-8"))
        print(f"[R1c] 落盘 lane={j1.get('lane')!r} verdict={j1.get('verdict')!r} "
              f"confidence={j1.get('confidence')}")
        if str(j1.get("lane") or "") != "debate":
            bad.append(f"R1c 落盘 lane={j1.get('lane')!r} ≠ debate")
    # 基线轮立场拿不到 ⇒ 对调轮无从谈起，如实收场（继续跑只会烧预算）
    if not pro1 or not con1:
        print("\n" + "=" * 70)
        print(f"  ❌ 基线轮受理不成立，对调轮未跑。{len(bad)} 条判据不成立：")
        for b in bad:
            print(f"    · {b}")
        return 1

    # ── R2 对调轮（受理返回值 = 第一轮命题/事实清单/案底 + 立场互换）──
    print("\n[R2] 跑对调轮（唯一差异 = 立场分配）…")
    r2_dir = base / "round2"
    h2: dict = {}
    snap2 = snapshot_tasks()
    run2, rulings2 = await run_judge_round(args.event, r2_dir, pin=out1, holder=h2)
    print(f"[R2] run 完成：status={getattr(run2, 'status', '?')} "
          f"iterations={getattr(run2, 'iterations', '?')} 新增裁定 {rulings2 or '无'}")
    orig2 = h2.get("original") if isinstance(h2.get("original"), dict) else {}
    eff2 = h2.get("intake") if isinstance(h2.get("intake"), dict) else {}
    # R2a 注入「送出去」的半边：模型拿到的必须恰是第一轮 + 立场互换
    if str(orig2.get("lane") or "") != "debate":
        bad.append(f"R2a 第二轮受理真判 lane={orig2.get('lane')!r} ≠ debate —— "
                   "预判与真判漂移，先归因")
    if (str(eff2.get("pro_position") or "") != con1
            or str(eff2.get("con_position") or "") != pro1):
        bad.append("R2a 注入没生效：模型实际拿到的立场与「第一轮互换后」不一致")
    else:
        print(f"[R2a] 注入已送出：正方拿到第一轮反方立场「{con1[:40]}…」，"
              f"反方拿到第一轮正方立场「{pro1[:40]}…」")
    rows2 = new_delegations(snap2, set(aic_map))
    bad += fanout_problems(rows2, aic_map, "R2·扇出")
    # ⚠️ 立场断言只打在**首波**委派上：追问轮上线后同一辩手可能有第 2 条
    #    委派（追问问题文本），dict 全量折叠会拿追问 payload 顶掉首波 payload，
    #    「立场在对调后送对了」这条判据就被自己的新功能误伤。
    by_role = {aic_map.get(str(r.get("remote_aic"))): r for r in rows2[:2]}
    t_pro = payload_text(by_role.get("debate-pro"))
    t_con = payload_text(by_role.get("debate-con"))
    if not args.brief:
        print(f"[R2b·payload/正方] {t_pro[:240]}")
        print(f"[R2b·payload/反方] {t_con[:240]}")
        for r in rows2[2:]:
            print(f"[R2b·追问payload/{aic_map.get(str(r.get('remote_aic')))}] "
                  f"{payload_text(r)[:240]}")
    bad += check_swap_in_payloads(t_pro, t_con, pro1, con1)
    j2: dict = {}
    if not rulings2:
        bad.append("R2c 隔离目录没有新裁定书 —— 对调轮没走完")
    else:
        j2 = json.loads((r2_dir / rulings2[-1]).read_text(encoding="utf-8"))
        print(f"[R2c] 落盘 lane={j2.get('lane')!r} verdict={j2.get('verdict')!r} "
              f"confidence={j2.get('confidence')}")
        if str(j2.get("lane") or "") != "debate":
            bad.append(f"R2c 落盘 lane={j2.get('lane')!r} ≠ debate")

    # ── 测量：两轮裁定对比（如实报告，不进退出码）──────────────────
    v1, v2 = j1.get("verdict"), j2.get("verdict")
    stable = bool(v1) and (v1 == v2)
    record = {
        "event": args.event,
        "stamp": stamp,
        "round1": {
            "ruling": rulings1[-1] if rulings1 else "",
            "verdict": v1,
            "confidence": j1.get("confidence"),
            "lane": j1.get("lane"),
            "pro_position": pro1,
            "con_position": con1,
        },
        "round2": {
            "ruling": rulings2[-1] if rulings2 else "",
            "verdict": v2,
            "confidence": j2.get("confidence"),
            "lane": j2.get("lane"),
            "pro_position_assigned": eff2.get("pro_position"),
            "con_position_assigned": eff2.get("con_position"),
        },
        "verdict_stable": stable,
        "mechanism_ok": not bad,
    }
    base.mkdir(parents=True, exist_ok=True)
    (base / "experiment.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ── 汇总 ────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"  第一轮：verdict={v1!r} confidence={j1.get('confidence')}")
    print(f"  第二轮：verdict={v2!r} confidence={j2.get('confidence')}")
    print(f"  实验记录：{base / 'experiment.json'}")
    if bad:
        print(f"  ❌ 实验无效——{len(bad)} 条机制判据不成立（上面的测量别当结论用）：")
        for b in bad:
            print(f"    · {b}")
        return 1
    if stable:
        print("  ✅ 实验有效（机制判据全绿）；测量：对调后裁定**一致** —— "
              "本对样本上裁定不跟立场走")
    else:
        print("  ⚠️ 实验有效（机制判据全绿）；测量：对调后裁定**翻转** —— "
              "这是发现不是故障：单对样本分不清「立场敏感」与"
              "「模型非确定性」，需重复实验归因")
    print("  （实验裁定已隔离在 output_probes/ 下，生产经验库未动；"
          "跑完记得 systemctl start acps-fact-judge）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
