"""**法条库本体** —— 本地已核验法条的**唯一读取口径**（law-advisor 用，2026-10-07）。

与 `fact_store` 同一模式、同一条纪律：
  · **运行时不出网**：库由 `tools/import_laws.py` 离线导入（人触发），
    运行时只读本地 `legal_acps/laws/law-articles.jsonl`；
  · **唯一口径**：不许另写第二份 reader（第二份必然漂移）；
  · **打分不在本模块**：与事实库 / 裁定经验库共用 `text_match.GramIndex`
    （同一判定写两处必然漂移）；本模块只留**本库的阈值**（按本库实测分布定）。

索引字段设计：`(法名+条号, 条文正文)` —— 法名与条号占"标题"位（命中分量
更重），条文占正文位。用户咨询是自由文本（"邻居装修太吵想起诉"），BM25 是
词项匹配不是语义匹配（fact_store 的如实局限这里同样成立）：`search()` 返回
的是**候选条文**，判定哪条真相关是 law-advisor 模型的活。

🔴 低于阈值就返回空，不许凑数 —— 返回一条不相关法条比不返回更糟：
它会被当成"查到了法律依据"引用出去，而这正是防幻觉要拦的东西。

阈值由实测分布定（`run/_probe_law_store.py`）：
  · 2,327 条库 + 同义扩展（2026-10-07 首次实测）：相关组（8 类口语咨询）
    top1 **全部命中正确条文**、最低 23.30（公司决议）；不相关组（4 个
    单行法专属领域：劳动仲裁/治安处罚/道交/出入境）top1 最高 13.64 ⇒
    取 **22.0**（保守侧：相关组全过线，库外全挡住；中点 18.5 亦可，
    宁紧勿滥——一条不相关法条被引用出去比漏一条更糟）。
  · 🔴 改检索算法 / 改同义词表 / 改库容（补新法 / M9 发布后重导）
    **必须重跑分布**。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .text_match import GramIndex, Match  # noqa: F401  (Match 供类型标注)

LAWS_DIR = Path(__file__).resolve().parent / "laws"
OUT_FILE = "law-articles.jsonl"

#: 覆盖的八部法律（能力边界的事实来源——提示词里的能力声明以这里为准，
#: **不许在提示词里另写一份清单**：同一事实散在多处必然漂移）。
SUPPORTED_LAWS_FILE = "law-articles.jsonl"   # meta 行的 laws 字段是唯一来源

MIN_SCORE = 22.0   # 实测分布见模块 docstring；改库必须重跑

#: 🔴 查询侧同义扩展（口语 → 法言法语）。为什么必须有（2026-10-07 实测）：
#:    GramIndex 是字符 bigram，**首尾不相交**——查询「借了钱不还」的 gram
#:    （借了/钱了/不还）与民法典 667 条「借款合同…返还借款」的 gram
#:    （借款/款合/返还）**零交集**，正确条文进不了前 60。这不是阈值问题，
#:    是召回问题；把法言法语补进查询后 667 升到 top1（11→22.5 分）。
#:    ⚠️ 纪律边界：这张表只做**查询增强**（把口语锚到法言法语），不做
#:    判定、不做能力声明——能力边界以库的 meta（八部法律清单）为准。
#:    ⚠️ 它是行为可断言的（check_local ⑦-8 有专门判据：含口语词的查询
#:    与不含的必须召回不同），词表退化会被判据抓住。
QUERY_SYNONYMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("借钱", "借款", "欠钱", "欠款", "欠了", "还钱", "不还"),
     "借款 借款合同 返还借款"),
    (("噪音", "噪声", "太吵", "扰民"),
     "噪声 排放 相邻关系"),
    (("假货", "假的就是", "质量问题", "质量不合格"),
     "质量不符合约定 违约责任 赔偿损失"),
    (("造谣", "谣言", "诽谤", "污蔑", "辱骂"),
     "捏造事实 诽谤 侮辱 名誉权"),
    (("被骗", "诈骗", "骗了", "骗局"),
     "诈骗罪 虚构事实 骗取财物"),
    (("打人", "动手", "伤人", "打伤"),
     "故意伤害 轻伤"),
    (("工资", "劳动报酬", "薪水", "拖欠"),
     "劳动报酬 拒不支付"),
    (("股东", "股权", "分红", "退股"),
     "股东会 决议 股权"),
    (("专利", "发明", "实用新型"),
     "专利权 许可实施 侵犯专利权"),
    (("纳税", "报税", "税款", "税务"),
     "纳税申报 税款 征收"),
    (("违约", "毁约", "不履行"),
     "违约责任 继续履行 赔偿损失"),
    (("彩礼", "离婚", "抚养权", "赡养"),
     "婚姻家庭 离婚 抚养 赡养"),
    (("房产", "房子", "不动产"),
     "不动产 所有权"),
)


def expand_query(query: str) -> str:
    """把查询里的口语词锚到法言法语（只追加，不删原文）。

    多个口语词命中 ⇒ 全部追加（它们本来就可能在讲同一件事）。
    纯函数（判据锚）：同输入同输出，不碰库。
    """
    extras = [text for keys, text in QUERY_SYNONYMS
              if any(k in query for k in keys)]
    return query + " " + " ".join(extras) if extras else query


@dataclass(frozen=True)
class LawArticle:
    """一条已核验的法条。字段全部来自导入原料，不做任何加工。"""

    id: str                    # 上游核验 id（如 CIVIL_CODE_584_v1）
    law_code: str
    law_name: str              # 《中华人民共和国民法典》
    article_number: str        # 第五百八十四条
    content: str               # 条文正文（逐字核验）
    effective_date: str
    source_url: str            # 国家法律法规数据库等官方来源
    verified_at: str = ""

    @property
    def citation(self) -> str:
        """规范引用形态：《中华人民共和国民法典》第五百八十四条。"""
        return f"《{self.law_name.replace('中华人民共和国', '中华人民共和国')}》{self.article_number}"


@dataclass(frozen=True)
class LawHit:
    """一条检索命中（带解释，与 fact_store.Hit 同理）。"""

    article: LawArticle
    score: float
    matched: tuple[str, ...] = ()


@dataclass
class LawLoadReport:
    total: int = 0
    kept: int = 0
    dropped: int = 0
    laws: list[str] = None  # type: ignore[assignment]
    reasons: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.laws is None:
            self.laws = []
        if self.reasons is None:
            self.reasons = {}

    @property
    def ok(self) -> bool:
        return self.kept > 0


class LawStore:
    """法条库。构造后不可变（索引一次建好）。"""

    def __init__(self, articles: Iterable[LawArticle]) -> None:
        self.articles: tuple[LawArticle, ...] = tuple(articles)
        #: (法名+条号, 条文正文) —— 法名条号占标题位（分量更重）。
        self.index = GramIndex(
            (f"{a.law_name}{a.article_number}", a.content) for a in self.articles
        )

    # ── 加载 ────────────────────────────────────────────────────────
    @classmethod
    def load(cls, root: Path | None = None, *,
             strict: bool = False) -> tuple["LawStore", "LawLoadReport"]:
        """读 `laws/law-articles.jsonl`（第一行是 meta，其余每行一条）。"""
        d = root or LAWS_DIR
        rep = LawLoadReport()
        arts: list[LawArticle] = []
        p = d / OUT_FILE
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            if strict:
                raise
            rep.reasons[f"读不了 {p.name}: {type(exc).__name__}"] = 0
            return cls(()), rep
        for i, ln in enumerate(lines):
            if not ln.strip():
                continue
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                rep.dropped += 1
                rep.reasons[f"第 {i + 1} 行坏 JSON"] = \
                    rep.reasons.get(f"第 {i + 1} 行坏 JSON", 0) + 1
                continue
            if obj.get("type") == "meta":
                rep.laws = [str(x) for x in (obj.get("laws") or [])]
                continue
            rep.total += 1
            # 溯源纪律：缺法名 / 条号 / 正文 / 来源 ⇒ 丢弃并计数，不静默
            missing = [k for k in ("law_name", "article_number",
                                   "content", "source_url") if not obj.get(k)]
            if missing:
                rep.dropped += 1
                rep.reasons[f"缺 {missing}（无法溯源/不完整）"] = \
                    rep.reasons.get(f"缺 {missing}（无法溯源/不完整）", 0) + 1
                continue
            arts.append(LawArticle(
                id=str(obj.get("id") or ""),
                law_code=str(obj.get("law_code") or ""),
                law_name=str(obj["law_name"]),
                article_number=str(obj["article_number"]),
                content=str(obj["content"]),
                effective_date=str(obj.get("effective_date") or ""),
                source_url=str(obj["source_url"]),
                verified_at=str(obj.get("verified_at") or ""),
            ))
        rep.kept = len(arts)
        if not rep.laws:
            rep.laws = sorted({a.law_name for a in arts})
        return cls(arts), rep

    # ── 检索 ────────────────────────────────────────────────────────
    def search(self, query: str, *, limit: int = 5,
               min_score: float = MIN_SCORE) -> list[LawHit]:
        """检索（内部先做同义扩展）。低于 `min_score` 的一律不返回（宁缺勿假）。"""
        if not query or not query.strip() or not self.articles:
            return []
        expanded = expand_query(query)
        return [
            LawHit(article=self.articles[m.index], score=m.score,
                   matched=m.matched)
            for m in self.index.search(expanded, limit=limit,
                                       min_score=min_score)
        ]

    def __iter__(self):
        return iter(self.articles)

    def __len__(self) -> int:
        return len(self.articles)
