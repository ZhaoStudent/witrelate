"""临时探针：列出本轮候选里的新增条目（不在上一批 11 条名单里的）。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OLD_KEYS = ("不停给12306", "许昌", "上海化工", "辽宁大道", "彭州", "养猪场",
            "浦东", "绿皮车", "直联点", "浮山", "医保可报销")

rows = [json.loads(l) for l in
        (ROOT / "legal_acps/rulings/gt_candidates.jsonl").read_text(
            encoding="utf-8").splitlines() if l.strip()]
new = [r for r in rows
       if not any(k in r["rumor_text"] for k in OLD_KEYS)]
print(f"候选共 {len(rows)} 条，其中新增 {len(new)} 条：")
for r in new:
    print(f"  [{r['rumor_text'][:42]}]")
    print(f"    定性: {r['quote'][:100]}")
    print(f"    title: {r['title'][:60]}")
