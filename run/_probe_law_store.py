"""探针：法条库 BM25 分布 —— 相关/不相关两组要分得开，阈值取实测（2026-10-07）。

用法（本地或服务器均可，库在同步清单里）：

    ./.venv/Scripts/python.exe -m run._probe_law_store

相关组 = 八部法律覆盖的典型初期纠纷咨询（**自由口语**——同义扩展的存在
就是为了把口语锚到法言法语，见 `law_store.QUERY_SYNONYMS`）；
不相关组 = **真库外领域**（劳动仲裁/治安处罚/道交/出入境——这些单行法
不在八部之内，正是 law-advisor 该说「能力范围外」的形态）。

⚠️ 第一版探针的教训（2026-10-07，保留）：最初把「离婚分房」「合同违约金」
「留学中介」当"不相关"——**错**：民法典 1260 条本就覆盖婚姻家庭编/合同编/
中介合同，它们是**真命中不是噪声**。「不相关组」必须是单行法专属领域，
否则探针自己就在制造假分布。

阈值结论（2026-10-07，2,327 条库 + 同义扩展）：
  相关组（8 类口语咨询）top1 最低 22.50（借款）；不相关组（4 个库外
  领域）top1 最高 10.81 ⇒ 取 **22.0**（相关组贴线是设计使然：扩展词
  只在口语命中时追加，分数抬升全靠真交集）。
  🔴 改词表 / 改库容 / 换打分 ⇒ 必须重跑本探针重定。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_acps.law_store import LawStore  # noqa: E402

RELATED = [
    "邻居装修噪音太大，每天早上六点就开始施工，我想起诉他侵犯我的相邻权",
    "朋友借了我五万块钱一直不还，只有微信转账记录，怎么起诉",
    "网上买东西商家发的是假货，我可以主张退一赔三吗",
    "有人在朋友圈造谣诽谤我，我要告他诽谤罪需要什么证据",
    "公司老板拖欠工资，我听说拒不支付劳动报酬是犯罪",
    "我把发明专利许可给一家公司使用，他们超范围使用算侵权吗",
    "公司章程没经过股东会决议就被大股东改了，小股东有什么救济途径",
    "个体户没按时申报纳税，税务机关会怎么处罚",
]
UNRELATED = [
    "我被公司违法辞退了，劳动仲裁的时效是一年吗",
    "无证驾驶被行政拘留十五天，会留案底影响孩子考公务员吗",
    "闯红灯被电子眼拍到，扣六分罚款两百，可以申诉吗",
    "去美国留学签证被拒签了三次，中介费能退吗",
]


def main() -> int:
    store, rep = LawStore.load()
    print(f"库况：{rep.kept} 条 / {len(rep.laws)} 部（丢弃 {rep.dropped}）")
    if not rep.ok:
        print("库是空的 —— 先跑 tools.import_laws")
        return 1

    print("\n── 相关组（应命中）──")
    rel_top = []
    for q in RELATED:
        hits = store.search(q, min_score=0.0, limit=3)
        top = hits[0] if hits else None
        rel_top.append(top.score if top else 0.0)
        if top:
            print(f"  {top.score:6.2f} {top.article.citation}  ← {q[:22]}")
        else:
            print(f"  （零命中）← {q[:22]}")

    print("\n── 不相关组（单行法专属领域，库外）──")
    unrel_top = []
    for q in UNRELATED:
        hits = store.search(q, min_score=0.0, limit=3)
        top = hits[0] if hits else None
        unrel_top.append(top.score if top else 0.0)
        if top:
            print(f"  {top.score:6.2f} {top.article.citation}  ← {q[:22]}")
        else:
            print(f"  （零命中）← {q[:22]}")

    best_rel = min(rel_top)
    best_unrel = max(unrel_top)
    print(f"\n相关组 top1 最低 = {best_rel:.2f}；不相关组 top1 最高 = {best_unrel:.2f}")
    if best_rel > best_unrel:
        mid = (best_rel + best_unrel) / 2
        print(f"✅ 两组分开 ⇒ 阈值建议 {mid:.1f}"
              f"（距两边 {min(best_rel - mid, mid - best_unrel):.1f}+）")
        return 0
    print("❌ 两组重叠 —— 需要人工看分布，不许拍脑袋定阈值")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
