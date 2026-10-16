import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from legal_acps import fact_store as fs  # noqa: E402

store, rep = FactStore = fs.FactStore.load()
Q = "火星上发现了一座古代城市遗址，考古学家确认是外星文明留下的建筑。"

for h in store.search(Q, limit=5, min_score=0.0):
    print(f"  {h.score:6.2f}  {h.fact.title[:40]}")
    print(f"         命中={list(h.matched)}")
    print(f"         库中该 gram 的 df={[(g, store.index._df.get(g)) for g in h.matched]}")
print()
print("查询里 idf 最高的 8 个 gram：")
qg = store.index.query_grams(Q)
for g, w in sorted(qg, key=lambda t: -t[1])[:8]:
    print(f"   {g!r:8s} idf={w:.3f}  df={store.index._df.get(g)}")
