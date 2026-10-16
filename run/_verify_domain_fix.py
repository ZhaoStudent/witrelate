"""真机验证：值域补「不实」后，官方证伪的案子**真的判得出来**（2026-10-07，未决 #16 收尾）。

一次性脚本（跑完即弃）。背景：`RulingVerdict` 补 `FALSE = "不实"` 并重审
fact-judge v2.0.0 后，「装了 ≠ 会响」——新值域是否真的让裁判**判出「不实」**，
只能真机跑一轮基准=「不实」的案例来看，没有捷径。

选 c-food-01（矿泉水泡尸水）：基准「不实」（公安部网安局通报），库里已有
同事件旧轮（verdict=无法判定）—— 同事件新轮会被 `_fold_duplicates` 折进
同一组（有标注轮优先代表），**台账的混淆矩阵/弃权数会随之变化**，这正是
本次验证要观察的。

判据：
  1. 硬判据：output/ 新增 ruling-*.json（复用 _enrich_rulings.run_one，含兜底重试）。
  2. 值域判据：新裁定的 verdict 字段实际取值（打印出来，人来看）。
     ⚠️ 不在脚本里断言「必须判出不实」—— provider 非严格确定性，且证据
     通路（事实库未覆盖该事件？）可能仍导致弃权；判「无法判定」也是合法
     输出。本脚本只如实报告，结论由人结合台账下。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path("/home/ubuntu/witrelate")
sys.path.insert(0, str(ROOT))

from run._enrich_rulings import run_one  # noqa: E402
from legal_acps.case_library import CASES  # noqa: E402


async def main() -> int:
    case = next(c for c in CASES if c.id == "c-food-01")
    out_dir = ROOT / "output"
    before = set(p.name for p in out_dir.glob("ruling-*.json"))
    ok = await run_one(case.id, case.text)
    if not ok:
        print("[结论] 未产出裁定 —— 链路问题，与值域无关；如实记失败")
        return 1
    import json
    new = sorted(set(p.name for p in out_dir.glob("ruling-*.json")) - before)
    for name in new:
        obj = json.loads((out_dir / name).read_text(encoding="utf-8"))
        print(f"[{name}] event={obj.get('event', '')[:60]}")
        print(f"[{name}] verdict={obj.get('verdict')!r} confidence={obj.get('confidence')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
