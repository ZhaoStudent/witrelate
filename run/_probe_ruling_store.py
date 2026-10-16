"""裁定经验库的**阈值探针**（2026-10-07）。

`ruling_store.MIN_SCORE` 不能拍脑袋定（**拍脑袋定的阈值等于没有阈值**）。
本脚本用**留一法自召回**造出两组真实分布：

  · **相关组**：把第 i 条裁定的 `event` 当查询，看它能不能把**自己**召回来
    （真值就是这个 —— 同一件事当然该是最像的）；
  · **不相关组**：同一查询下，**其他**裁定里的最高分（这些是"不该被当成先例"的）。

阈值应当取在两组之间。**只有 1 条裁定的库派生不出分布** ⇒ 本脚本会如实报
「样本不足，无法派生」，而不是吐一个好看的数字。

用法：

    ./.venv/bin/python -m run._probe_ruling_store
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from legal_acps.ruling_store import RulingStore  # noqa: E402


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def main() -> int:
    store, rep = RulingStore.load()
    print(f"经验库：total={rep.total} kept={rep.kept} dropped={rep.dropped} "
          f"graded={rep.graded}")
    stat = store.stats()
    print(f"不同事件数={stat['distinct_events']}（总条数 {stat['total']}）；"
          f"最大一类占比={stat['top_event_share']}")

    if len(store) < 3:
        print("\n[无法派生] 样本不足（< 3 条）⇒ 两组分布派生不出来。"
              "\n          阈值必须留作占位值，等库涨上来再跑本脚本。")
        return 2
    # 🔴 **偏食检测**：同一事件反复跑会把分布压成"能不能召回那一个事件"，
    #    派生出来的阈值**没有泛化意义** ⇒ 如实报「无法派生」，不吐一个好看的数字。
    if stat["distinct_events"] < 5 or (stat["top_event_share"] or 0) > 0.5:
        print("\n[无法派生] 样本**严重偏食** ⇒ 派生出的阈值只反映"
              "「能不能召回那一个事件」，没有泛化意义。"
              "\n          先把更多**不同**事件跑进库，再回来跑本脚本。"
              "\n          （阈值继续留作占位值，由 `MIN_SCORE_DERIVED` 哨兵盯着。）")
        return 2

    self_scores: list[float] = []
    other_max: list[float] = []
    miss: list[str] = []
    for c in store:
        hits = store.search(c.event, limit=10, min_score=0.0)
        mine = [h.match.score for h in hits if h.case.source == c.source]
        others = [h.match.score for h in hits if h.case.source != c.source]
        if not mine:
            miss.append(c.source)
            self_scores.append(0.0)
        else:
            self_scores.append(mine[0])
        other_max.append(max(others, default=0.0))

    print(f"\n＝＝ 两组分布（**无视阈值**，min_score=0，只为看清差多少）＝＝")
    print(f"相关（自召回）  n={len(self_scores)}  "
          f"min={min(self_scores):.2f}  p25={_pct(self_scores, .25):.2f}  "
          f"中位={_pct(self_scores, .5):.2f}  max={max(self_scores):.2f}")
    print(f"不相关（他条最高）n={len(other_max)}  "
          f"min={min(other_max):.2f}  p75={_pct(other_max, .75):.2f}  "
          f"中位={_pct(other_max, .5):.2f}  max={max(other_max):.2f}")
    if miss:
        print(f"\n⚠️ 自召回失败的条目 {len(miss)}/{len(store)}：{miss[:5]}"
              f" ⇒ 这些事件连自己都召不回，先例检索对它们无效")

    lo = max(other_max)
    hi = min(s for s in self_scores if s > 0) if any(self_scores) else 0.0
    print(f"\n⇒ 把阈值取在不相关最高 {lo:.2f} 与 相关最低 {hi:.2f} 之间。")
    if hi <= lo:
        print("⚠️ 两组**重叠** ⇒ 单靠分数分不开，需要换检索口径"
              "（扩库 / 领域词表 / 提高 MIN_HITS），不是调阈值能救的。")
        return 1
    print(f"   建议值（几何中点，取整留余量）：{int((lo * hi) ** 0.5)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
