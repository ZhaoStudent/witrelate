"""**事实库本体** —— 本地权威事实的**唯一读取口径**。

════════════════════════════════════════════════════════════════════
⚠️ 为什么它是"唯一口径"（不许另写第二份 reader）
════════════════════════════════════════════════════════════════════

事实搜集能力要"从权威来源取信息"，但**运行时不出网**（`tools/fetch_facts.py`
的 docstring 写了为什么）。所以运行时读的是**本地库**，而本地库只能有
**一个**读取口径 —— 第二份 reader 必然漂移，而漂移的那一半正是没人盯的那一半。

════════════════════════════════════════════════════════════════════
检索为什么是 **BM25**（以及前三版 ad hoc 打分为什么全废了）
════════════════════════════════════════════════════════════════════

约束（实测）：服务器 **没有** jieba / sklearn / bs4，只有 numpy。
⇒ 不能分词、不能向量化、不能上 embedding。**零新增依赖**是硬要求。

**试错三版，全部失败**（详细数据记在 `_score` 的 docstring 里，别改回去）：

1. **IDF top-K 覆盖度** ⇒ 字符 bigram 里**越罕见越可能是噪声**
   （"天截"只出现 1 次，IDF 最高），把实词"地震"挤出 top-K。
2. **全量加权覆盖度 + 虚字过滤** ⇒ 相关与不相关两组分数**完全重叠**，
   且**召回也失败**：不相关条目靠命中 6 个通用词胜过命中 1 个实词的相关条目。
3. **max 单命中归一化** ⇒ **全部 1.000**，只要命中一个噪声词就满分。

⇒ 换成 **BM25**（检索领域标准公式，零依赖）。它自带前三者都没有的两件事：
**词频饱和**（k1）与**长度归一化**（b）。实测两组终于分开：
相关组 top1 = 78.8 / 18.4 / 14.4，不相关组 top1 = 7.7 / 7.4 ⇒ 阈值取 10.0。

🔴 **必须如实说清的局限（不许粉饰）**：

BM25 是**词项匹配，不是语义匹配**。实测地震查询返回的前 3 条是
「资助学生 / 证券App / 喝饮料」—— 它们与查询共享"聊天截图""转发"这类
**表面特征**，而真正讲地震的条目排在第 4。
⇒ **`search()` 返回的是「候选」，不是「已判定相关的事实」**。
最终判断哪条真相关，是**模型（Leader / 裁判）的活**，不是这个函数的活。
把它当成"已经判定相关"来用，就是**假证据**。

改进方向（按性价比）：**扩库**（78 条太少，IDF 不可靠是根因之一）>
引入领域词表 > 上 embedding（但违背零依赖与不出网两条约束）。

════════════════════════════════════════════════════════════════════
四条硬纪律
════════════════════════════════════════════════════════════════════

1. **低于阈值就返回空，不许凑数**。返回一条不相关的事实，比不返回更糟 ——
   它会被当成"查到了权威依据"，而这是**假证据**。
2. **溯源字段不许含糊**：每条结果必须带 `url` / `origin_publisher` / `published`。
   缺 `url` 或 `title` 的原料**直接丢弃**并**如实计数**（丢弃数不报出来，
   "库很干净"与"我偷偷丢了一半"就长得一样）。
3. **加载失败要响亮报出**，不许静默返回一个空库 ——
   「没加载」与「库是空的」在调用方看来完全一致。
4. **一条事实 = 一个 URL**（2026-10-07）。同一 URL 的多个原料快照只保留
   `fetched_at` 最新的一份，去重数报出来 —— 同文两份会挤占检索 top-N、
   虚高库容，而「重抓过」与「库里真有两条」在调用方看来一模一样。

用法：

    from legal_acps.fact_store import FactStore
    store = FactStore.load()
    for hit in store.search("某院士预测广东将发生8级地震", limit=5):
        print(hit.score, hit.fact.title, hit.fact.origin_publisher, hit.fact.url)
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .text_match import GramIndex, Match  # noqa: F401  (Match 供类型标注)

FACTS_DIR = Path(__file__).resolve().parent / "facts"
GLOB = "raw-*.jsonl"

#: 🔴 **打分实现不在本模块**。事实库与裁定经验库做的是同一件事（给一段查询、
#: 从一堆短文本里挑出相关的几条），而**同一判定写两处必然漂移**，漂移的那一半
#: 恰好是没人盯着的那一半 ⇒ 两库共用 `legal_acps.text_match.GramIndex`。
#: 本模块只留**阈值**（它是按本库的实测分布定的，换库就要重跑分布）。

MIN_SCORE = 18.0    # 低于此分**不返回**（纪律 1）。
                    # ⚠️ 由实测分布定（BM25 分无上界），**改检索算法或改库容都必须重跑**：
                    #   · 78 条库（2026-10-07 上午，`run/_probe_fact_store.py`）：
                    #     相关组 top1 = 78.77 / 18.39 / 14.39，不相关组 = 7.73 / 7.39 ⇒ 10.0
                    #   · 128 条库（2026-10-07 下午，扩容 + 同 URL 去重后重跑）：
                    #     相关组（13 案例全文）top1 最低 24.60；不相关组（6 个异话题
                    #     长文本）top1 最高 13.55（火星查询撞上「越王楼拆除」短标题条目，
                    #     通用词「发现/城市/建筑」在短文里密度被 BM25 长度归一化放大）
                    #     ⇒ 取 18.0：距两组都有 4.5+ 余量。
                    #   🔴 旧阈值 10.0 在 128 条库上会把噪声放进来（实测 13.55 过线）——
                    #      「阈值不动」与「阈值还有效」在库扩容后长得一样。


@dataclass(frozen=True)
class Fact:
    """一条权威事实。**字段全部来自抓取原料，不做任何加工** —— 加工就是编造。"""

    title: str
    url: str
    publisher: str            # 我从哪个站抓的（转载平台）
    origin_publisher: str     # 这条内容**谁发的**（真正的权威方）
    level: str                # 权威结论 / 一手材料 / 二手转述
    published: str            # 发布方标注的日期；推不出为空（**不许用抓取时间冒充**）
    body: str = ""
    source_key: str = ""
    fetched_at: str = ""


@dataclass(frozen=True)
class Hit:
    """一条检索命中。`matched` 是**解释**：命中了查询里哪几个片段。

    ⚠️ 带解释不是锦上添花 —— 调用方（以及人）要能看出"为什么这条算相关"，
    否则"分数 0.31"是个无法质疑的数字，而这正是判据最怕的东西。
    """

    fact: Fact
    score: float
    matched: tuple[str, ...] = ()


@dataclass
class LoadReport:
    """加载结果。**丢弃数必须报出来**（纪律 2）。"""

    total: int = 0
    kept: int = 0
    dropped: int = 0
    #: 同 URL 多快照被去重的条数（2026-10-07，纪律 4）。
    #: ⚠️ 去重**不许静默**：「库里 140 条」与「其实是 128 篇文章、12 篇存了两份」
    #:    在调用方看来完全一样，而后者意味着库容统计虚高、检索 top-N 被翻倍条目
    #:    挤占 —— 计数必须报出来，人才知道发生过重抓。
    deduped: int = 0
    files: list[str] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kept > 0


class FactStore:
    """事实库。构造后**不可变**（索引一次建好），避免"边查边变"这种说不清的状态。"""

    def __init__(self, facts: Iterable[Fact]) -> None:
        self.facts: tuple[Fact, ...] = tuple(facts)
        self._n = len(self.facts)
        #: 唯一打分实现。`(标题, 正文)` —— 标题命中的分量更重（`HEAD_BOOST`）。
        self.index = GramIndex((f.title, f.body) for f in self.facts)

    # ── 加载 ────────────────────────────────────────────────────────
    @classmethod
    def load(cls, root: Path | None = None, *, strict: bool = False) -> tuple["FactStore", LoadReport]:
        """读 `facts/raw-*.jsonl`。返回 **(库, 报告)** —— 报告不许丢。

        ⚠️ 为什么不只返回库：丢了多少条、为什么丢，是**判断库能不能用**的依据。
        只返回一个库的话，"加载失败"与"库本来就是空的"在调用方看来一模一样。

        `strict=True` 时原料有问题直接抛（给判据用：`load` 静默吞掉异常会让
        判据永远绿）。
        """
        d = root or FACTS_DIR
        rep = LoadReport()
        facts: list[Fact] = []
        by_url: dict[str, int] = {}   # url → 在 facts 里的下标（纪律 4 去重用）
        try:
            files = sorted(d.glob(GLOB))
        except OSError as exc:
            if strict:
                raise
            rep.reasons[f"目录不可读: {exc}"] = 0
            return cls(()), rep

        if not files:
            rep.reasons["没有 raw-*.jsonl（先跑 tools.fetch_facts）"] = 0
            return cls(()), rep

        for p in files:
            rep.files.append(p.name)
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                if strict:
                    raise
                rep.reasons[f"{p.name}: 读不了 {type(exc).__name__}"] = 0
                continue
            for ln in lines:
                if not ln.strip():
                    continue
                rep.total += 1
                try:
                    obj = json.loads(ln)
                except json.JSONDecodeError:
                    rep.dropped += 1
                    rep.reasons["JSON 坏了"] = rep.reasons.get("JSON 坏了", 0) + 1
                    continue
                title = str(obj.get("title") or "").strip()
                url = str(obj.get("url") or "").strip()
                # 纪律 2：没有 url 或标题 = 无法溯源 ⇒ 丢弃，不许进库
                if not title or not url:
                    rep.dropped += 1
                    rep.reasons["缺 url 或 title（无法溯源）"] = \
                        rep.reasons.get("缺 url 或 title（无法溯源）", 0) + 1
                    continue
                fact = Fact(
                    title=title,
                    url=url,
                    publisher=str(obj.get("publisher") or "").strip(),
                    origin_publisher=str(obj.get("origin_publisher") or "").strip(),
                    level=str(obj.get("level") or "").strip(),
                    published=str(obj.get("published") or "").strip(),
                    body=str(obj.get("body") or ""),
                    source_key=str(obj.get("source_key") or "").strip(),
                    fetched_at=str(obj.get("fetched_at") or "").strip(),
                )
                # 纪律 4（2026-10-07）：一条事实 = 一个 URL。同一 URL 出现在多个
                # 原料快照里是**常态**（重跑 `--curated` / 列表页重抓都会整批
                # 重写），不去重 ⇒ 同一篇文章在库里存两份：检索 top-N 被翻倍条目
                # 挤占（挤掉的是**别的**相关证据）、库容统计虚高，而「重抓过」
                # 与「库里真有两条」在调用方看来一模一样。
                # ⇒ 保留 `fetched_at` 最新的一份（新快照取代旧快照），计数报出来。
                if url in by_url:
                    rep.deduped += 1
                    i = by_url[url]
                    if fact.fetched_at >= facts[i].fetched_at:
                        facts[i] = fact
                else:
                    by_url[url] = len(facts)
                    facts.append(fact)
        rep.kept = len(facts)
        return cls(facts), rep

    # ── 检索 ────────────────────────────────────────────────────────
    #
    # 🔴 打分**不在这里**。实现在 `text_match.GramIndex`（`self.index`）：
    #    它与裁定经验库（`ruling_store`）共用同一份 —— **同一判定写两处必然漂移**，
    #    而漂移的那一半恰好是没人盯着的那一半。
    #    这里只做两件事：本库自己的阈值 `MIN_SCORE`（按本库实测分布定），
    #    以及「命中 → `Hit`」的映射。
    def search(self, query: str, *, limit: int = 5,
               min_score: float = MIN_SCORE) -> list[Hit]:
        """检索。**低于 `min_score` 的一律不返回**（纪律 1）。"""
        if not query or not query.strip() or not self.facts:
            return []
        return [
            Hit(fact=self.facts[m.index], score=m.score, matched=m.matched)
            for m in self.index.search(query, limit=limit, min_score=min_score)
        ]

    # ── 给判据 / 校准用的统计 ────────────────────────────────────────
    def stats(self) -> dict[str, object]:
        by_origin: dict[str, int] = {}
        by_level: dict[str, int] = {}
        no_date = 0
        for f in self.facts:
            by_origin[f.origin_publisher or "(未标注)"] = \
                by_origin.get(f.origin_publisher or "(未标注)", 0) + 1
            by_level[f.level or "(未标注)"] = by_level.get(f.level or "(未标注)", 0) + 1
            if not f.published:
                no_date += 1
        return {
            "facts": self._n,
            "no_date": no_date,
            "by_origin_publisher": dict(sorted(by_origin.items(), key=lambda t: -t[1])),
            "by_level": dict(sorted(by_level.items(), key=lambda t: -t[1])),
        }

    def __len__(self) -> int:
        return self._n

    def __iter__(self) -> Iterator[Fact]:
        return iter(self.facts)


# ═════════════════════════════════════════════════════════════════════════
# 媒体核验记录（2026-10-18 方案②）：证据鉴证链的核验结论 → 可检索的一手材料
# ═════════════════════════════════════════════════════════════════════════
#
# 证据鉴证链（evidence-leader → 模态叶子）此前的产出**只进 output/ 报告文件**，
# 与事实核查链（fact-judge）唯一的连接点是用户自己把两边结论抄到一起。
# 本节把每份鉴证报告同时归档成一条事实库记录：之后**任何**文本研判（裁判受理
# `arbitrate_claim` 的检索、快车道 `decide_lane` 的对齐层）都能检索到
# 「同一媒体材料的既往核验结论」—— 真实谣言生态里"同一张旧图反复配新说法"
# 极常见，这正是它的用武之地。
#
# ⚠️ 检索命中的形态：记录 title/body 携带 `subject`（材料说明，含案情上下文）
#    的自然语言 ⇒ 文本查询能按词项匹配到它；url 是 `evidence://` 合成溯源键
#    （本系统核验没有网页 url，溯源指向本地报告文件与媒体指纹，不冒充网页）。
#
# 🔴 安全闸（这是本节最重要的一段）：快车道对齐层 `official_confirms` 的判据是
#    FALSE_PAT（「不实」系定性词）± 窗口对齐——一条正文里带定性词的核验记录，
#    会让之后**同事件的文本研判被快车道直接判「不实」、跳过辩论**。证据链的
#    铁律"只给线索、不断言真伪"约束的是模型提示词；这里是**代码层**兜底：
#    渲染出的正文撞上 FALSE_PAT ⇒ 这条记录**绝不落库**（`render` 返回 None 并
#    报出撞上的模式）。静默写进去 = 静默给安全结论，本库最忌讳的失败形态。
# ═════════════════════════════════════════════════════════════════════════

#: 核验记录 url 前缀。url 同时是去重键（纪律 4）：同一媒体（同 sha256）复检
#: ⇒ 同 url ⇒ `load` 时新记录顶旧记录；拿不到指纹的报告各占一条（report/<stamp>）。
EVIDENCE_URL_PREFIX = "evidence://"

#: 运行时核验记录的落库文件（GLOB `raw-*.jsonl` 天然能读到它）。
EVIDENCE_LIVE_FILE = "raw-evidence-live.jsonl"


def render_evidence_record(ruling: dict) -> tuple[dict | None, list[str]]:
    """把一份 ``EvidenceRuling``（dict 形态）渲染成事实库记录。

    返回 ``(record, refused_patterns)``：
    · record 非空、refused 为空  ⇒ 可落库；
    · record 为 None、refused 非空 ⇒ **拒绝入库**（正文撞上 FALSE_PAT 安全闸），
      refused 里是撞上的模式原文，调用方必须如实报出、不许静默吞掉。
    """
    from datetime import datetime, timezone

    from .lead_store import FALSE_PAT  # 局部 import：避免模块级环

    subject = str(ruling.get("subject") or "").strip()
    modality = str(ruling.get("modality") or "").strip() or "未知模态"
    suspicion = str(ruling.get("suspicion") or "").strip() or "无法判定"
    sha = str(ruling.get("sha256") or "").strip()
    conclusion = str(ruling.get("conclusion") or "").strip()
    findings = ruling.get("findings") or []
    not_run = ruling.get("not_run") or []

    clue_lines: list[str] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        check = str(f.get("check") or "").strip()
        detail = str(f.get("detail") or "").strip()
        caveat = str(f.get("caveat") or "").strip()
        head = detail or check  # detail 是人话（check 只是 exif/ela 这类标识符）
        if head:
            clue_lines.append(
                f"· {head}" + (f"（caveat：{caveat}）" if caveat else "")
            )

    body = (
        f"本系统证据鉴证链核验记录。材料说明：{subject}。模态：{modality}。"
        f"综合线索等级：{suspicion}（线索等级不是伪造概率，不能单独作为真伪结论）。"
        f"技术线索：{'；'.join(clue_lines) if clue_lines else '（无）'}。"
        f"未执行的检查项：{'；'.join(str(x) for x in not_run) if not_run else '（无）'}。"
        f"综合说明：{conclusion}。"
        + (f"文件指纹 sha256：{sha}。" if sha else "")
        + "本记录是系统核验留痕（一手材料），不是权威定性结论。"
    )
    # 🔴 安全闸只查 **body**：`official_confirms` 的对齐判定只读 fact.body，
    #    title 不进对齐窗口。查 title 会把"用户主张原文里带『谣言』二字"的
    #    合法记录也拒掉（subject 常引用主张原文），白丢记录。
    refused = sorted({m.group() for m in FALSE_PAT.finditer(body)})
    if refused:
        return None, refused

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    url = (
        f"{EVIDENCE_URL_PREFIX}sha256/{sha}"
        if sha
        else f"{EVIDENCE_URL_PREFIX}report/{stamp}"
    )
    record = {
        "source_key": "evidence",
        "publisher": "本系统·证据鉴证链",
        "origin_publisher": "本系统·证据鉴证链（evidence-leader）",
        "level": "一手材料",
        "title": f"媒体核验记录：{subject[:60]}（{modality}，线索等级{suspicion}）",
        "url": url,
        "published": now.date().isoformat(),
        "body": body,
        "fetched_at": stamp,
    }
    return record, []


def append_evidence_record(record: dict, *, root: Path | None = None) -> dict:
    """把一条核验记录追加进事实库目录。旁路纪律：**失败不挡调用方主流程**，
    结果永远结构化（status ∈ written / error + 原因）。"""
    out: dict = {"status": "written", "url": str(record.get("url") or ""), "reason": ""}
    try:
        d = Path(root) if root is not None else FACTS_DIR
        d.mkdir(parents=True, exist_ok=True)
        path = d / EVIDENCE_LIVE_FILE
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 —— 旁路：如实记失败，不挡报告交付
        out["status"] = "error"
        out["reason"] = f"{type(exc).__name__}: {exc}"
    return out
