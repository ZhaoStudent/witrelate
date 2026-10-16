"""判定校准台账 —— 让「判断力」的变化**可测量**（2026-10-07，第 ④ 步）。

════════════════════════════════════════════════════════════════════
这是什么、不是什么（术语纪律，见 docs/aicomp/第二阶段方案评价与设计.md §2.5）
════════════════════════════════════════════════════════════════════

**是**：离线统计 + 校准。从裁定产物（`output/ruling-*.json`）与外部基准标注
（`legal_acps/rulings/ground_truth.jsonl`）汇总：一致率、弃权率、混淆矩阵、
置信度分桶 —— 即「置信度标定」的原料。

**不是**：强化学习 / 自主学习。没有奖励函数、没有策略更新、没有微调管线。
对外措辞用「判定校准台账」「置信度标定」，不用那两个词。

════════════════════════════════════════════════════════════════════
🔴 单一来源纪律（本模块最重要的一条）
════════════════════════════════════════════════════════════════════

数据只有一个口径：`legal_acps.ruling_store.RulingStore`。
`total / accuracy / undetermined / verdicts / fallacies` 一律**引用** `stats()`
的返回值，本模块**不许再算一遍** —— 同一个数算两遍，迟早有一天两个地方
不一样，而漂移的那一半恰好是没人盯着的那一半（本项目实证过的病）。

本模块只做 stats() **没有**的三件事（全部从同一批 `RulingCase` 派生）：

  1. **混淆矩阵**（verdict × ground_truth）—— 错在哪里一眼可见；
  2. **弃权与判错分开**——「该判而没判」和「判了但判错」是两种病，
     药方完全不同（前者补证据通路，后者改判断逻辑）；合在一栏里，
     一致率就成了个说不清的数字；
  3. **值域观察**——基准标注里出现、而裁定契约值域（`RulingVerdict`）里
     **没有**的取值 ⇒ 那些样本在当前契约下**不可能判对**。这是结构性偏差，
     不是模型偏差；不把它单拎出来，「一致率低」就会背错锅。

════════════════════════════════════════════════════════════════════
用法
════════════════════════════════════════════════════════════════════

    ./.venv/bin/python -m tools.calibration                 # 打印台账
    ./.venv/bin/python -m tools.calibration --write <path>  # 另存 markdown

（🧪 判据见 `check_local` ⑦-5k；元验证见 `run/meta_verify_calibration.py`。）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from legal_acps.contracts import RulingVerdict  # noqa: E402
from legal_acps.ruling_store import RulingStore  # noqa: E402

#: 置信度分桶的边界。⚠️ 校准曲线的标准做法是等宽分桶；桶宽 0.2 在当前样本量
#: （十几条）下每桶 0–4 条 —— 所以 `confidence_buckets` 会如实报每桶 n，
#: n 太小的桶**不许**拿来下结论（报告里原样标出）。
CONF_EDGES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0001)

#: 值域观察的提示语锚点（判据要用它断言「提示真的在报告里」）。
DOMAIN_GAP_MARK = "值域缺口"


def load(root: Path | None = None) -> RulingStore:
    """唯一的取数口径 —— 与先例注入、check_local 用的是同一个 `RulingStore`。"""
    store, rep = RulingStore.load(root)
    if not rep.ok:
        print(f"[警告] 经验库为空或不可读：{rep.reasons or rep.files}", file=sys.stderr)
    return store


def confusion(store: RulingStore) -> dict[tuple[str, str], int]:
    """混淆矩阵：`(verdict, ground_truth) → 条数`。只数 **graded**（有基准）的。"""
    out: dict[tuple[str, str], int] = {}
    for c in store:
        if c.graded:
            key = (c.verdict, c.ground_truth)
            out[key] = out.get(key, 0) + 1
    return out


def abstain_split(store: RulingStore) -> dict[str, int]:
    """graded 样本三分类：**弃权 / 判对 / 判错**（不许合并成「不对」一栏）。

    ⚠️ 为什么弃权要单列：弃权的病因是「拿不到权威信源」（运行时不出网、
    事实库没覆盖）——药方在**证据通路**；判错的病因在**判断逻辑**（如
    命题偷换）——药方在提示词/校准。两种病混在一栏，一致率就说不清了。
    """
    abstain = right = wrong = 0
    for c in store:
        if not c.graded:
            continue
        if c.verdict == RulingVerdict.UNDECIDABLE.value:
            abstain += 1
        elif c.correct:
            right += 1
        else:
            wrong += 1
    return {"abstain": abstain, "right": right, "wrong": wrong}


def domain_gap(store: RulingStore) -> list[str]:
    """基准标注里出现、而裁定契约值域里**没有**的取值。

    🔴 2026-10-07 实测发现（第一份真实台账就撞上了）：当时 `RulingVerdict`
    值域是 {确有其事, 断章取义, 无法判定} —— **没有「不实」**，而官方证伪的
    谣言（基准=「不实」）恰恰是本项目的主要案件类型 ⇒ 13 条可校准基准里
    11 条**结构性不可能判对**。当日已补（`RulingVerdict.FALSE`，未决 #16 关闭）。
    本函数继续留着：值域与基准**是两份独立维护的事实**，谁也不能保证下一次
    标注不会又出现值域外取值（如「部分属实」）—— 缺口检测必须一直在线。
    """
    allowed = {v.value for v in RulingVerdict}
    seen: dict[str, int] = {}
    for c in store:
        if c.graded and c.ground_truth not in allowed:
            seen[c.ground_truth] = seen.get(c.ground_truth, 0) + 1
    return [f"{g}（{n} 条）" for g, n in sorted(seen.items(), key=lambda t: -t[1])]


def confidence_buckets(store: RulingStore) -> list[dict[str, object]]:
    """置信度校准曲线的原料：`(confidence 桶, 判对率)`，只数**给出明确判断**
    且有基准的样本（弃权没有「预测」，进曲线只会把曲线搅浑）。

    ⚠️ 每桶的 n 原样输出：校准曲线在 n<5 的桶上**没有统计意义**，
    报告里如标注、不许拿来下结论。
    """
    out: list[dict[str, object]] = []
    graded_firm = [c for c in store
                   if c.graded and c.verdict != RulingVerdict.UNDECIDABLE.value]
    for lo, hi in zip(CONF_EDGES, CONF_EDGES[1:]):
        bucket = [c for c in graded_firm if lo <= c.confidence < hi]
        if not bucket:
            continue
        right = sum(1 for c in bucket if c.correct)
        out.append({
            "range": f"{lo:.1f}–{hi:.1f}",
            "n": len(bucket),
            "right": right,
            "rate": right / len(bucket),
        })
    return out


def render(store: RulingStore) -> str:
    """渲染 markdown 台账。🔴 汇总数全部**引用** `stats()`，本模块不自算。"""
    st = store.stats()
    L: list[str] = []
    L.append("# 判定校准台账")
    L.append("")
    L.append("> 生成：`python -m tools.calibration` · 数据：`output/ruling-*.json` + "
             "`legal_acps/rulings/ground_truth.jsonl` · 统计口径：`ruling_store.stats()`")
    L.append("")

    # ── 一、库况（全部引用 stats）────────────────────────────────────
    L.append("## 一、库况")
    L.append("")
    L.append(f"- 经验：**{st['total']} 个事件**（原始裁定 {st['raw_total']} 份；"
             f"最大单一事件占比 {st['top_event_share']:.2%}）")
    L.append(f"- 有基准结论（可校准）：**{st['graded']} 条**；无基准：{st['undetermined']} 条"
             f"（弃权率 {st['undetermined_rate']:.0%} —— 无基准的不进一致率，只作先例）")
    acc = st["accuracy"]
    L.append(f"- 一致率（verdict == 基准）：**{acc if acc is not None else 'None（无可校准样本）'}**"
             + (f"（{st['correct']}/{st['graded']}）" if acc is not None else ""))
    L.append("")
    if st["graded"] == 0:
        L.append("⚠️ **无可校准样本** —— 一条基准标注都没有，本台账给不出任何"
                 "「判断质量」的数字。先标注 `legal_acps/rulings/ground_truth.jsonl`。")
        L.append("")
        return "\n".join(L)

    # ── 二、弃权与判错分开 ──────────────────────────────────────────
    sp = abstain_split(store)
    L.append("## 二、弃权与判错（两种病，分开设栏）")
    L.append("")
    L.append(f"| 弃权（无法判定） | 判对 | 判错 |")
    L.append(f"|---:|---:|---:|")
    L.append(f"| {sp['abstain']} | {sp['right']} | {sp['wrong']} |")
    L.append("")
    firm = sp["right"] + sp["wrong"]
    L.append(f"- 弃权占可校准样本的 **{sp['abstain'] / st['graded']:.0%}** ——"
             f"病因在**证据通路**（运行时不出网、事实库未覆盖 ⇒ 诚实地拿不到信源）；")
    if firm:
        L.append(f"- 给出明确判断的 {firm} 条里判对 {sp['right']} 条 ——"
                 f"病因在**判断逻辑**（错因见标注文件的 `error_reason`）。")
    L.append("")

    # ── 三、混淆矩阵 ───────────────────────────────────────────────
    conf = confusion(store)
    verdicts = sorted({k[0] for k in conf} | {v.value for v in RulingVerdict})
    truths = sorted({k[1] for k in conf})
    L.append("## 三、混淆矩阵（行 = 裁定，列 = 基准）")
    L.append("")
    L.append("| 裁定＼基准 | " + " | ".join(truths) + " |")
    L.append("|---|" + "---:|" * len(truths))
    for v in verdicts:
        row = [str(conf.get((v, t), 0)) for t in truths]
        L.append(f"| {v} | " + " | ".join(row) + " |")
    L.append("")

    # ── 四、值域缺口（结构性偏差，先于一切模型归因）──────────────────
    gaps = domain_gap(store)
    L.append(f"## 四、{DOMAIN_GAP_MARK}（结构性偏差）")
    L.append("")
    if gaps:
        L.append(f"🔴 基准标注里出现了裁定契约值域（{[v.value for v in RulingVerdict]}）"
                 f"**没有**的取值：{'、'.join(gaps)}。")
        L.append("   这些样本在当前契约下**不可能判对** —— 一致率低的那部分"
                 "要先算在值域头上，而不是模型头上。")
        L.append("   ⇒ 修法是扩展 `RulingVerdict`（影响工具 schema 与调用方，"
                 "须按 ACS 流程评估），不是调提示词。")
    else:
        L.append("（无 —— 基准取值全部落在裁定契约值域内。）")
    L.append("")

    # ── 五、置信度分桶 ─────────────────────────────────────────────
    buckets = confidence_buckets(store)
    L.append("## 五、置信度分桶（校准曲线原料）")
    L.append("")
    if not buckets:
        L.append("（原料不足：还没有「给出明确判断且有基准」的样本 ——"
                 "先积累，再谈曲线。）")
    else:
        L.append("| confidence | n | 判对 | 判对率 |")
        L.append("|---|---:|---:|---:|")
        for b in buckets:
            warn = " ⚠️n<5" if b["n"] < 5 else ""
            L.append(f"| {b['range']}{warn} | {b['n']} | {b['right']} | "
                     f"{b['rate']:.0%} |")
        L.append("")
        L.append("⚠️ n<5 的桶没有统计意义，**不许**拿来下结论；它只是原料的如实陈列。")
    L.append("")

    # ── 六、话术命中（引用 stats）───────────────────────────────────
    L.append("## 六、话术命中计数（引用 `stats()`）")
    L.append("")
    if st["fallacies"]:
        for k, n in st["fallacies"].items():
            L.append(f"- {k}：{n}")
    else:
        L.append("（无）")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="判定校准台账（离线统计，不是训练）")
    ap.add_argument("--write", default="", help="另存 markdown 的路径（默认只打印）")
    args = ap.parse_args(argv)

    store = load()
    text = render(store)
    print(text)
    if args.write:
        Path(args.write).write_text(text, encoding="utf-8")
        print(f"→ 已写入 {args.write}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
