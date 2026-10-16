"""**唯一的文本检索打分**（BM25 · 零依赖）。

════════════════════════════════════════════════════════════════════
⚠️ 为什么它必须是**唯一**的一份
════════════════════════════════════════════════════════════════════

事实库（``fact_store``）与裁定经验库（``ruling_store``）都要做「给一段查询，
从一堆短文本里挑出相关的那几条」。这是**同一件事**。

而本项目的硬规矩是：**同一判定写两处必然漂移**，且漂移的那一半恰好是没人
盯着的那一半。所以打分只有这一份；两个库都只是**喂文档进来、拿命中出去**。

════════════════════════════════════════════════════════════════════
检索为什么是 **BM25**（以及前三版 ad hoc 打分为什么全废了）
════════════════════════════════════════════════════════════════════

约束（实测）：服务器 **没有** jieba / sklearn / bs4，只有 numpy。
⇒ 不能分词、不能向量化、不能上 embedding。**零新增依赖**是硬要求。

**试错三版，全部失败**（别改回去）：

1. **IDF top-K 覆盖度** ⇒ 字符 bigram 里**越罕见越可能是噪声**
   （"天截"只出现 1 次，IDF 最高），把实词"地震"挤出 top-K。
2. **全量加权覆盖度 + 虚字过滤** ⇒ 相关与不相关两组分数**完全重叠**，
   且**召回也失败**：不相关条目靠命中 6 个通用词胜过命中 1 个实词的相关条目。
3. **max 单命中归一化** ⇒ **全部 1.000**，只要命中一个噪声词就满分。

⇒ 换成 **BM25**（检索领域标准公式，零依赖）。它自带前三者都没有的两件事：
**词频饱和**（k1）与**长度归一化**（b）。

════════════════════════════════════════════════════════════════════
🔴 必须如实说清的局限（不许粉饰）
════════════════════════════════════════════════════════════════════

1. **BM25 是词项匹配，不是语义匹配**。实测地震查询返回的前 3 条是
   「资助学生 / 证券App / 喝饮料」—— 它们与查询共享"聊天截图""转发"这类
   **表面特征**，而真正讲地震的条目排在第 4。
   ⇒ **`search()` 返回的是「候选」，不是「已判定相关的东西」**。
   把它当成"已经判定相关"来用，就是**假证据**。

2. ⚠️ **当前的 tf 其实是二值的**（`_grams()` 返回集合，正文词频恒为 1；
   只有 head 按 `HEAD_BOOST` 加权）。所以 k1 的"词频饱和"目前**几乎没在起作用**，
   真正把两组分开的主要是 **长度归一化（b）** 与 **df/idf 折扣**。
   这不是 bug，但**别在文档里声称"词频饱和在起作用"** —— 引错的注释比没有
   注释更糟。要让它真起作用，得把正文改成真实词频计数，**而那会改分数分布，
   必须重跑 `run/_probe_fact_store.py` 重定阈值**。见 `docs/上线操作手册.md`。

3. **阈值必须实测**，不能拍脑袋。相关组与不相关组的分数分布要用
   `run/_probe_fact_store.py` 打出来再取中间值 —— 拍脑袋定的阈值等于没有阈值。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

GRAM_N = 2          # 字符 n-gram；2 在中文上够用，3 会让短查询 gram 太少
DF_CUT_RATIO = 0.4  # 某 gram 出现在超过 40% 的文档里 ⇒ 当虚词丢掉

# 中文高频虚字。⚠️ 为什么要手写这一张表（而不是靠 IDF 自动压）：
# 小库里 **IDF 根本不可靠** —— "广东""工作""群里"这类通用词同样稀有
# ⇒ IDF 与实词「地震」不相上下 ⇒ **通用词靠数量压过了实词**
# （实测地震查询的命中片段是 ['广东','的 ','工作','群里',' 8']，实词一个没有）。
# ⇒ 只能靠一张**语言层面**的表把它们剔掉。这是零依赖下唯一能做的，
# 且它剔除的是"几乎不可能是实体"的字，误伤可控。
STOP_CHARS = frozenset(
    "的了是在和也就都而及与这那你我他她它们个上有不没很太更最吗呢吧啊把被给"
    "对从向到过还又再只就会能要将已可如若其所之乎者也矣焉哉"
)
STOP_PENALTY = 0.25  # gram 里含虚字时的权重折扣（不是丢弃：可能夹着实字）
MIN_HITS = 2         # 至少命中**两个**查询片段才返回：只中一个是巧合，不是相关
K1 = 1.5             # BM25 词频饱和参数（领域标准值，**不是我调出来的**）
B = 0.75             # BM25 长度归一化参数（同上）
HEAD_BOOST = 3       # 头部（标题/事件摘要）的词频按 3 倍计：信息密度远高于正文
HAPAX_MIN_DF = 2     # df 低于此值 ⇒ 视为"偶然组合"（见 `query_grams`）
HAPAX_PENALTY = 0.4  # df=1 的词的权重折扣：小库里它最可能是噪声，不是实体

# 汉字/字母数字之外的都当分隔符：标点、空白、HTML 残留
NOISE = re.compile(r"[^一-鿿0-9a-zA-Z]+")


def grams(text: str) -> set[str]:
    """切 n-gram。短文本（<n）退化成自身，保证不会返回空集。"""
    cleaned = NOISE.sub(" ", text)
    if len(cleaned) < GRAM_N:
        return {cleaned} if cleaned.strip() else set()
    return {cleaned[i:i + GRAM_N] for i in range(len(cleaned) - GRAM_N + 1)}


@dataclass(frozen=True)
class Match:
    """一条命中。`matched` 是**解释**：命中了查询里哪几个片段。

    ⚠️ 带解释不是锦上添花 —— 调用方（以及人）要能看出"为什么这条算相关"，
    否则"分数 0.31"是个无法质疑的数字，而这正是判据最怕的东西。
    """

    index: int
    score: float
    matched: tuple[str, ...] = ()


@dataclass
class IndexReport:
    """建索引的报告。**空库必须报出来**（「没有」与「查不到」不是一回事）。"""

    docs: int = 0
    empty_docs: int = 0

    @property
    def ok(self) -> bool:
        return self.docs > 0


class GramIndex:
    """BM25 索引。构造后**不可变**，避免"边查边变"这种说不清的状态。

    `docs` 是 `(head, body)` 序列：`head` 是主旨（标题 / 事件一句话），
    `body` 是正文。两者**都可以为空**，但全空的文档不产生任何 gram
    （会被如实记进报告的 `empty_docs`）。
    """

    def __init__(self, docs: Iterable[tuple[str, str]] = ()) -> None:
        pairs: Sequence[tuple[str, str]] = tuple(docs)
        self._n = len(pairs)
        self._head: tuple[set[str], ...] = tuple(grams(h or "") for h, _ in pairs)
        self._body: tuple[set[str], ...] = tuple(grams(b or "") for _, b in pairs)
        self._all: tuple[set[str], ...] = tuple(h | b for h, b in zip(self._head, self._body))

        df: dict[str, int] = {}
        for gset in self._all:
            for g in gset:
                df[g] = df.get(g, 0) + 1
        self._df = df

        tf: list[dict[str, int]] = []
        empty = 0
        for h, b in zip(self._head, self._body):
            if not h and not b:
                empty += 1
            tf.append({**{g: HEAD_BOOST for g in h},
                       **{g: c for g, c in Counter(b).items()}})
        self._tf: tuple[dict[str, int], ...] = tuple(tf)
        self._doc_len: tuple[float, ...] = tuple(float(sum(t.values())) for t in self._tf)
        self._avgdl: float = (sum(self._doc_len) / self._n) if self._n else 1.0
        self._df_cut = max(2, int(DF_CUT_RATIO * self._n)) if self._n else 0
        # BM25 的 idf（带 +1 平滑，保证非负 —— 与早期那个 `log((N+1)/(df+0.5))`
        # 不同，那个在 df > N 时会变负，是没有意义的）
        self._idf: dict[str, float] = {
            g: math.log(1.0 + (self._n - d + 0.5) / (d + 0.5)) for g, d in df.items()
        }
        self.report = IndexReport(docs=self._n, empty_docs=empty)

    def __len__(self) -> int:
        return self._n

    # ── 查询侧 ──────────────────────────────────────────────────────
    def query_grams(self, query: str) -> tuple[tuple[str, float], ...]:
        """取查询里**在库词汇表内、且不是到处都出现**的 gram。

        ⚠️ 第一版用「IDF 最高的 TOP_GRAMS 个」，实测**失效**：
        对字符 bigram 而言，**越罕见越可能是噪声** ——
        「天截」（来自"聊天截图"）这种偶然组合在库里只出现 1 次 ⇒ IDF 最高
        ⇒ 把实词「地震」挤出 top-8 ⇒ 地震查询命中了一条完全不相关的条目，
        而它的命中片段是 `['他的','发了','天截']`。**噪声拿到了最高权重。**

        ⇒ 改为**不做 top-K 截断**，改成**两端过滤**：
          · df == 0（库里从没见过）⇒ 无信息，丢掉；
          · df 过高（超过 `DF_CUT_RATIO` 比例的文档都有）⇒ 虚词/噪声，丢掉。
        中间的**全部参与**打分：实词与噪声都在，但噪声只在它出现的那一条上得分，
        **抢不走实词的位置**（这正是 top-K 截断丢掉的东西）。
        """
        gs = grams(query or "")
        if not gs:
            return ()
        out: list[tuple[str, float]] = []
        for g in gs:
            if g not in self._idf:
                continue                      # 库里从没见过 ⇒ 无信息
            if self._df[g] > self._df_cut:
                continue                      # 到处都是 ⇒ 虚词/噪声
            if all(ch in STOP_CHARS for ch in g):
                continue                      # 全是虚字 ⇒ 直接丢
            w = self._idf[g]
            if self._df[g] < HAPAX_MIN_DF:
                # 🔴 **全库只出现一次的词，最可能是偶然组合，不是实体** ——
                # 实测：不相关的火星查询命中"建筑"/"筑 "（两者 df 都是 1）
                # ⇒ 两个偶然共享的稀有词就把分数抬到 15.11，压过了真正相关的条目。
                # 小库里 idf 本身就不可靠，df=1 更是完全没证据
                # ⇒ 打折，不是剔除（万一它真是实体，还有 40% 权重留着）。
                w *= HAPAX_PENALTY
            if any(ch in STOP_CHARS for ch in g):
                w *= STOP_PENALTY             # 含虚字 ⇒ 大幅降权，不丢（可能夹着实字）
            out.append((g, w))
        return tuple(out)

    def score(self, qg: tuple[tuple[str, float], ...], idx: int) -> tuple[float, tuple[str, ...]]:
        """**BM25**。零依赖，纯公式 —— `k1`/`b` 是检索领域的标准取值，不是我调出来的。

        ⚠️ 为什么换成 BM25（前三版 ad hoc 打分**全部失败**，别再改回去）：见模块头。
        ⚠️ 阈值必须是**实测出来**的，见 `run/_probe_fact_store.py` 打印的两组分布。
        """
        tf = self._tf[idx]
        if not tf:
            return 0.0, ()
        norm = K1 * (1.0 - B + B * self._doc_len[idx] / self._avgdl)
        score = 0.0
        matched: list[str] = []
        for g, w in qg:                       # ⚠️ 必须用 **qg 里的权重 w**，
            f = tf.get(g)                     #    不是 `self._idf[g]` —— 前者已经
            if not f:                         #    打过虚字折扣与 df=1 折扣。
                continue                      #    写成 `for g, _ in qg` 又用
            matched.append(g)                 #    `self._idf[g]`，等于**把两道折扣
            score += w * (f * (K1 + 1.0)) / (f + norm)  # 全丢了**，而它看起来完全正常。
        if len(matched) < MIN_HITS:
            return 0.0, tuple(matched)
        return score, tuple(matched)

    def search(self, query: str, *, limit: int = 5,
               min_score: float = 0.0) -> list[Match]:
        """检索。**低于 `min_score` 的一律不返回** —— 返回一条不相关的，
        比不返回更糟（它会被当成"查到了依据"）。
        """
        if not self._n or not (query or "").strip():
            return []
        qg = self.query_grams(query)
        if not qg:
            return []
        out: list[Match] = []
        for i in range(self._n):
            sc, matched = self.score(qg, i)
            if sc >= min_score:
                out.append(Match(index=i, score=sc, matched=matched))
        out.sort(key=lambda m: (-m.score, m.index))
        return out[:limit]
