"""探：事实库检索**分不分得开**。

闸门要"分得开"才算闸门：一对只差一个变量的输入 ⇒ 必须给出**不同**结论。
这里的两组：
  · 与库里内容相关的查询 ⇒ 必须命中，且溯源字段齐全
  · 完全不相关的查询       ⇒ 必须**返回空**（阈值真的在拦，不是摆设）
只测"能命中"的判据是空转的。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from legal_acps.fact_store import FactStore, FACTS_DIR  # noqa: E402

store, rep = FactStore.load()
print(f"加载：total={rep.total} kept={rep.kept} dropped={rep.dropped}")
print(f"      丢弃原因：{rep.reasons or '无'}")
print(f"      文件：{rep.files}")
print()
print("统计：", store.stats())
print()

QUERIES = [
    ("相关 · 地震（案例库 c-quake-01 的原话）",
     "家族群里转发了一张聊天截图，说某院士预测广东将发生 8 级以上大地震。"
     "截图里有院士的名字和他的头像，看起来像是在某个工作群里发出来的。"),
    ("相关 · 饮用水（c-water-01）",
     "自来水出现异味之后，网上流传一份警情通报，说是因为粪水排污导致的。"),
    ("相关 · AI 造谣",
     "新疆喀纳斯棕熊索食险酿大祸？有人说这是AI生成的虚假内容。"),
    ("不相关 · 火星",
     "火星上发现了一座古代城市遗址，考古学家确认是外星文明留下的。"),
    ("不相关 · 无关长文本",
     "今天天气不错，我打算下午去公园散步，顺便买点水果和面包回来。"),
]

# 阈值**由这段实测定**，不拍脑袋：先看相关与不相关两组各自的分数分布，
# 再取能把两组分开的那个值写进 `fact_store.MIN_SCORE`。
print("＝＝ 分数分布（**无视阈值**，min_score=0，只为看清两组差多少）＝＝")
for name, q in QUERIES:
    raw = store.search(q, limit=3, min_score=0.0)
    real = store.search(q, limit=3)          # 带阈值的实际结果
    tops = " / ".join(f"{h.score:.3f}" for h in raw) or "（无）"
    print(f"  {name[:26]:28s} top3={tops:26s} 过阈值 {len(real)} 条")
print()

print("＝＝ 召回检查：top-10 里有没有「讲同一件事」的条目 ＝＝")
EXPECT = {"相关 · 地震（案例库 c-quake-01 的原话": "地震",
          "相关 · 饮用水（c-water-01）": "水",
          "相关 · AI 造谣": "棕熊"}
for name, q in QUERIES:
    if name not in EXPECT:
        continue
    kw = EXPECT[name]
    top = store.search(q, limit=10, min_score=0.0)
    rank = next((i + 1 for i, h in enumerate(top)
                 if kw in h.fact.title or kw in h.fact.body[:400]), None)
    print(f"  {name[:26]:28s} 关键词『{kw}』→ "
          f"{('第 %d 名' % rank) if rank else '❌ 不在 top-10（召回失败）'}")
    for i, h in enumerate(top[:10], 1):
        mark = "  <- 含关键词" if (kw in h.fact.title or kw in h.fact.body[:400]) else ""
        print(f"      {i:2d}. {h.score:6.2f}  {h.fact.title[:38]}{mark}")
print()

for name, q in QUERIES:
    hits = store.search(q, limit=3)
    print(f"── {name}")
    if not hits:
        print("   （空）⇒ 闸门拦住了")
    for h in hits:
        f = h.fact
        print(f"   score={h.score:.3f}  {f.title[:40]}")
        print(f"        原始发布方={f.origin_publisher or '(空)'}  "
              f"日期={f.published or '(空)'}  等级={f.level}")
        print(f"        url={f.url[:66]}")
        print(f"        命中片段={list(h.matched)[:6]}")
    print()
