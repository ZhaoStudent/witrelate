"""临时探针：9 份新裁定 ↔ 人核批次映射，产出 gt 追加行（source/ground_truth/basis）。"""
import glob
import json

# 人核批次：rumor_text -> 定性引文人核依据
batch = {}
for line in open("legal_acps/rulings/gt_batch_20261011.jsonl", encoding="utf-8"):
    d = json.loads(line)
    batch[d["rumor_text"]] = d

used = set()
rows = []
for path in sorted(glob.glob("output/ruling-20261011T*.json")):
    d = json.load(open(path, encoding="utf-8"))
    event = d.get("event", "")

    def norm(s: str) -> str:
        return "".join(ch for ch in s if ch not in "“”「」\"'。？?，, ")

    hit = None
    for rumor_text, cand in batch.items():
        if norm(rumor_text) in norm(event) or norm(event) in norm(rumor_text):
            hit = cand
            rumor = rumor_text
            break
    if not hit:
        print(f"!! {path} 候选未命中: {event[:50]}")
        continue
    used.add(rumor)
    fname = path.split("/")[-1]
    basis = f"{hit['publisher']}《{hit['title']}》（{hit['published']}）：{hit['quote'][:60]}"
    rows.append({
        "source": fname,
        "ground_truth": "不实",
        "error_reason": "",
        "basis": basis,
    })
    print(f"{fname}\tverdict={d.get('verdict')}\trum={rumor[:30]}")

print(f"\n映射成功 {len(rows)}/9；批次未用 {len(batch)-len(used)} 条")
for r in batch:
    if r not in used:
        print("  未用:", r[:40])
