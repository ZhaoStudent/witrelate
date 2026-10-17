"""一次性：重聚合 2026-10-17 夜批的 12 份 experiment.json（修正汇总窗口 bug）。

窗口：[2026-10-17T15:43Z, 2026-10-17T16:50Z]（批量 15:48:05 启动，16:42 汇总）。
输出：output_probes/stance-swap-batch-20261017T154805Z.json（覆盖错误版本，
原错误文件名不同（T164239Z），保留作对照、不删除——append-only 精神）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "output_probes"
W0 = datetime(2026, 10, 17, 15, 43, tzinfo=timezone.utc)
W1 = datetime(2026, 10, 17, 16, 50, tzinfo=timezone.utc)

valid: list[dict] = []
for run_dir in sorted(RUNS.glob("stance-swap-*")):
    ej = run_dir / "experiment.json"
    if not ej.exists():
        continue
    try:
        rec = json.loads(ej.read_text(encoding="utf-8"))
        t = datetime.strptime(rec.get("stamp", ""), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        continue
    if W0 <= t <= W1:
        rec["_dir"] = run_dir.name
        valid.append(rec)

mech_ok = [r for r in valid if r.get("mechanism_ok")]
stable = [r for r in mech_ok if r.get("verdict_stable")]
flipped = [r for r in mech_ok if not r.get("verdict_stable")]

report = {
    "batch_stamp": "2026-10-17T15:48:05+00:00",
    "reaggregated": "2026-10-18（修正汇总窗口 bug：首版基准取在跑完后，只收 1 条）",
    "window": [W0.isoformat(), W1.isoformat()],
    "n_valid_records": len(valid),
    "n_mechanism_ok": len(mech_ok),
    "n_stable": len(stable),
    "n_flipped": len(flipped),
    "pairs": [
        {
            "event": r["event"][:60],
            "dir": r["_dir"],
            "mechanism_ok": r.get("mechanism_ok"),
            "verdict_stable": r.get("verdict_stable"),
            "round1": r.get("round1", {}).get("verdict"),
            "round2": r.get("round2", {}).get("verdict"),
            "conf1": r.get("round1", {}).get("confidence"),
            "conf2": r.get("round2", {}).get("confidence"),
        }
        for r in valid
    ],
}
out = RUNS / "stance-swap-batch-20261017T154805Z.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"有效记录 {len(valid)}｜机制全绿 {len(mech_ok)}｜一致 {len(stable)}｜翻转 {len(flipped)}")
for r in report["pairs"]:
    mark = "一致" if r["verdict_stable"] else "翻转"
    mech = "机制绿" if r["mechanism_ok"] else "机制红"
    print(f"  [{mark}·{mech}] {r['event'][:38]}… r1={r['round1']!r} "
          f"r2={r['round2']!r} conf={r['conf1']}/{r['conf2']}")
print(f"报告：{out}")
