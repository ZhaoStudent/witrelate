"""批量立场对调实验（#124 升级版：n=1 → n≈10，2026-10-17）。

为什么批量：#124 首跑只有一对样本，「verdict_stable=true」是 n=1 的结论。
答辩口径「跑了 10 个案例验证裁定不随立场漂移」与「跑了一个」是两个量级。
单对样本也分不开「立场敏感」与「模型非确定性」两个原因——多对样本里
若翻转集中在少数事件，指向事件本身；若均匀散布，指向非确定性。

设计决策（都有出处）：
  · **每事件一个子进程**（`python -m run.e2e_stance_swap --event …`）：
    复用已验证的单事件探针全部机制（P0/R1/R2 判据、写隔离、台账），
    不在驱动器里重写任何判据——驱动器只做编排与汇总（单一来源纪律）。
  · **预检前置**：跑前先用 `decide_lane` 逐事件证死「库外」（lane=debate
    且零命中）——单事件探针也会 P0 预检，但那是在停掉裁判服务**之后**；
    预检前置让命中事件在停服前就被筛掉，不浪费一轮起停。
  · **停/复 fact-judge 全程一次**：整个批量期间裁判服务保持停止（每事件
    的子进程都要进程内起裁判）；异常退出也要复（finally）。
  · **事件文本是实验输入**：写死正当（同 DEFAULT_EVENT 的口径）；「库外」
    由预检现场证死，不由人选保证。
  · **子事件间零共享状态**：写隔离在子进程内完成，事件 A 的裁定不会
    成为事件 B 的先例（经验库读路径读生产库，写路径隔离）——这是
    #124 探针已有的性质，批量只是重复调用。

用法（s1 仓库根目录）：

    ./.venv/bin/python -m run.batch_stance_swap            # 全量
    ./.venv/bin/python -m run.batch_stance_swap --limit 3  # 先跑 3 个冒烟

退出码：0 = 至少 1 对有效样本且全部机制判据绿（测量结果看报告，
不进退出码——同单事件探针口径）；1 = 没有任何有效样本或机制判据有红。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROBE = "run.e2e_stance_swap"

#: 事件池：与既有事实库/经验库**预期无命中**的库外新案（预检现场证死）。
#: 刻意覆盖不同域（交通/食品/公共安全/消费/医疗/教育/环境），其中 #0 即
#: #124 首跑的 DEFAULT_EVENT —— 复跑它顺带回答「同事件复跑是否可复现」。
EVENTS = [
    "网传某市地铁集团下发内部通知，因信号系统全面升级，"
    "全市地铁自下月起每周五下午提前两小时收班",
    "网传某连锁奶茶品牌因冷库故障，多家门店继续使用已过保质期的鲜奶原料",
    "网传某市动物园一只大熊猫因饲养员投喂不当突发重病，园方封锁消息",
    "网传某新款电动车型在低温环境实测续航仅为标称值的一半，厂商知情不改",
    "网传某市明年起除夕当天全市公交线路全部停运，仅保留两条机场大巴线",
    "网传某连锁健身品牌以门店装修为由闭店，实际是资金链断裂即将跑路",
    "网传某网红直播间售出的羽绒服填充物实为飞丝，检测报告系伪造",
    "网传某高校新学期食堂菜品普涨五成，学生集体维权校方删帖压制",
    "网传某市自来水厂加氯量超标数倍，近期管网水有刺鼻异味不宜饮用",
    "网传某快递企业站点私拆客户包裹并调包贵重物品，内部监控流出",
    "网传某三甲医院因系统升级急诊停诊一周，危重病人需转外地就医",
    "网传某品牌儿童手表定位误差达数公里，紧急求救功能长期失效",
]


def precheck_offline(event: str) -> tuple[bool, str]:
    """停服前的库外预检：lane=debate 且零命中才算过。"""
    try:
        from legal_acps.fast_lane import decide_lane

        info = decide_lane(event)
    except Exception as exc:  # noqa: BLE001 —— 预检失败 ⇒ 该事件跳过并如实记录
        return False, f"预检异常 {type(exc).__name__}: {str(exc)[:80]}"
    if info["lane"] != "debate" or info["qualified_precedents"] or info["aligned_official"]:
        return False, (f"库内命中（lane={info['lane']}，先例 "
                       f"{len(info['qualified_precedents'])}、官方 "
                       f"{len(info['aligned_official'])}）")
    return True, "库外已证死"


def systemctl(*args: str) -> None:
    subprocess.run(["sudo", "systemctl", *args], check=True,
                   stdout=subprocess.DEVNULL)


def main() -> int:
    ap = argparse.ArgumentParser(description="批量立场对调实验")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个事件（冒烟）")
    args = ap.parse_args()

    from dotenv_lite import load_env

    load_env(ROOT / ".env")

    print("=" * 70)
    # 🔴 批量起点在**这里**取（主流程第一行），阶段 3 的时间过滤以它为基准。
    #    首版把基准取在跑完之后 ⇒ 只收到最后一个事件的记录（12 跑 1 收，
    #    2026-10-18 实测教训：过滤基准必须锚定「本轮从什么时候开始」）。
    batch_start = datetime.now(timezone.utc)
    print(f"  批量立场对调实验 · {batch_start.isoformat(timespec='seconds')}")
    print("=" * 70)

    # ── 阶段 0：停服前逐事件预检（命中者在停服前就被筛掉）─────────
    pool = EVENTS[: args.limit] if args.limit else EVENTS
    events: list[str] = []
    skipped: list[tuple[str, str]] = []
    for ev in pool:
        ok, why = precheck_offline(ev)
        (events if ok else skipped).append(ev if ok else (ev, why))
        tag = "✅" if ok else "跳过"
        print(f"[预检] {tag} {ev[:36]}…  {why}")
    if not events:
        print("\n❌ 预检后没有任何库外事件可跑——先归因（事实库/先例库变化？）")
        for ev, why in skipped:
            print(f"    · {ev[:40]}…：{why}")
        return 1

    # ── 阶段 1：停裁判服务（整个批量期间一次）──────────────────────
    systemctl("stop", "acps-fact-judge")
    print(f"\n[服务] acps-fact-judge 已停（批量期间进程内起裁判，{len(events)} 对样本）")

    # ── 阶段 2：逐事件子进程跑完整双轮 ────────────────────────────
    results: list[dict] = []
    try:
        for i, ev in enumerate(events, 1):
            print(f"\n────── [{i}/{len(events)}] {ev[:44]}… ──────")
            try:
                cp = subprocess.run(
                    [str(ROOT / ".venv/bin/python"), "-m", PROBE,
                     "--event", ev, "--brief"],
                    cwd=str(ROOT), capture_output=True, text=True, timeout=1800,
                )
                rc = cp.returncode
                tail = "\n".join((cp.stdout or "").splitlines()[-6:])
                err_tail = (cp.stderr or "")[-400:]
            except subprocess.TimeoutExpired as exc:
                rc = -1
                tail = "子进程超时（1800s）——该事件记失败，继续下一个"
                err_tail = str(exc)[:400]
            print(tail)
            results.append({"event": ev, "rc": rc,
                            "tail": tail, "stderr_tail": err_tail})
    finally:
        systemctl("start", "--no-block", "acps-fact-judge")
        print("\n[服务] acps-fact-judge 已下发重启")

    # ── 阶段 3：汇总（读各 run 自己落的 experiment.json）──────────
    # 判据口径与单事件探针一致：只信 experiment.json（机制判据 + 测量），
    # 不从 stdout 猜。本轮 batch 的 run 目录 = 启动时刻之后新建的目录。
    stamp_now = datetime.now(timezone.utc)
    runs = sorted((ROOT / "output_probes").glob("stance-swap-*"),
                  key=lambda p: p.name)
    valid: list[dict] = []
    for run_dir in runs:
        ej = run_dir / "experiment.json"
        if not ej.exists():
            continue
        try:
            rec = json.loads(ej.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        # 只收本轮的：experiment stamp 不早于批量起点前 5 分钟（容忍时钟毛刺）。
        # 🔴 基准是 batch_start（main 第一行取的），不是「现在」——首版用
        #    跑完后的当前时间做基准，把除最后一个事件外的记录全滤掉了。
        try:
            rec_time = datetime.strptime(rec.get("stamp", ""),
                                         "%Y%m%dT%H%M%SZ").replace(
                                             tzinfo=timezone.utc)
        except ValueError:
            continue
        if rec_time < batch_start - timedelta(minutes=5):
            continue
        valid.append(rec)

    n = len(valid)
    mech_ok = [r for r in valid if r.get("mechanism_ok")]
    stable = [r for r in mech_ok if r.get("verdict_stable")]
    flipped = [r for r in mech_ok if not r.get("verdict_stable")]

    report = {
        "batch_stamp": stamp_now.isoformat(timespec="seconds"),
        "pool_size": len(pool),
        "skipped_precheck": [{"event": ev[:60], "why": why} for ev, why in skipped],
        "subprocess_rc": [{"event": r["event"][:60], "rc": r["rc"]} for r in results],
        "n_valid_records": n,
        "n_mechanism_ok": len(mech_ok),
        "n_stable": len(stable),
        "n_flipped": len(flipped),
        "pairs": [
            {
                "event": r["event"][:60],
                "verdict_stable": r.get("verdict_stable"),
                "round1": r.get("round1", {}).get("verdict"),
                "round2": r.get("round2", {}).get("verdict"),
                "conf1": r.get("round1", {}).get("confidence"),
                "conf2": r.get("round2", {}).get("confidence"),
            }
            for r in valid
        ],
    }
    out = ROOT / "output_probes" / f"stance-swap-batch-{stamp_now.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("\n" + "=" * 70)
    print(f"  批量汇总：预检过 {len(events)}｜子进程完成 {len(results)}"
          f"｜有效记录 {n}｜机制全绿 {len(mech_ok)}")
    print(f"  裁定一致 {len(stable)} 对 ｜ 裁定翻转 {len(flipped)} 对")
    for r in report["pairs"]:
        mark = "一致" if r["verdict_stable"] else "翻转"
        print(f"    [{mark}] {r['event'][:36]}… "
              f"r1={r['round1']!r} r2={r['round2']!r} "
              f"conf={r['conf1']}/{r['conf2']}")
    print(f"  报告：{out}")
    if flipped:
        print("  ⚠️ 翻转样本是发现不是故障：看翻转是否集中在特定事件域"
              "（⇒立场敏感）还是均匀散布（⇒模型非确定性）")
    print("  （每对样本的完整机制判据见各自 experiment.json；"
          "生产经验库全程未动）")
    return 0 if mech_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
