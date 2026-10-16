"""临时工具：把 9 条人核 gt 追加进 ground_truth.jsonl（字节级、写后自检、防重复）。"""
import glob
import json

GT_PATH = "legal_acps/rulings/ground_truth.jsonl"

# 人核批次：rumor_text -> 定性引文人核依据
batch = {}
for line in open("legal_acps/rulings/gt_batch_20261011.jsonl", encoding="utf-8"):
    d = json.loads(line)
    batch[d["rumor_text"]] = d

existing_sources = set()
for line in open(GT_PATH, encoding="utf-8"):
    d = json.loads(line)
    existing_sources.add(d["source"])


def norm(s: str) -> str:
    return "".join(ch for ch in s if ch not in "“”「」\"'。？?，, ")


rows = []
for path in sorted(glob.glob("output/ruling-20261011T*.json")):
    d = json.load(open(path, encoding="utf-8"))
    event = d.get("event", "")
    hit = None
    for rumor_text, cand in batch.items():
        if norm(rumor_text) in norm(event) or norm(event) in norm(rumor_text):
            hit = cand
            break
    if not hit:
        raise SystemExit(f"候选未命中: {path} {event[:40]}")
    fname = path.split("/")[-1]
    if fname in existing_sources:
        raise SystemExit(f"source 已存在（防重复）: {fname}")
    basis = f"{hit['publisher']}《{hit['title']}》（{hit['published']}）：{hit['quote'][:60]}"
    rows.append({"source": fname, "ground_truth": "不实", "error_reason": "", "basis": basis})

assert len(rows) == 9, f"应为 9 条，实际 {len(rows)}"

with open(GT_PATH, "a", encoding="utf-8", newline="\n") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

# 写后自检：行数 13+9=22、无重复 source、每行可解析、纯 LF 追加无 CR
lines = open(GT_PATH, encoding="utf-8").read().splitlines()
assert len(lines) == 22, f"行数 {len(lines)}"
srcs = [json.loads(x)["source"] for x in lines]
assert len(srcs) == len(set(srcs)), "source 重复"
raw = open(GT_PATH, "rb").read()
assert raw.count(b"\r\n") == 0, "混入 CRLF"
print(f"OK 追加 9 条，ground_truth 现 {len(lines)} 条，纯 LF，无重复")
