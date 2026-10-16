"""裁定经验库 —— 让裁判的**判断力**能被累积（2026-10-07）。

════════════════════════════════════════════════════════════════════
这条要解决什么（以及为什么它**不是**"训练权重"）
════════════════════════════════════════════════════════════════════

需求原文：「裁判能从正反方辩论内容里自主学习，增强判断能力」。
`llm.py` 是固定的 `OpenAIChatProvider`（`temperature=0`），**没有微调管线** ⇒
"改权重"这条路走不通，而且说出来会被一句"你的奖励函数是什么"问倒。

但**不改权重也能让判断力变强**，三条通路都已落地：

  1. **先例注入**（本模块）—— 同类事件的既往裁定 + 当年错在哪，检索出来
     注入裁判这一轮的上下文。对应设计文档 §3.6 的「裁定经验库（RAG）」。
  2. **校准统计**（`tools/calibration.py`）—— 累计一致率、弃权率、分桶偏差。
  3. **提示词迭代** —— 高频错因写进 `instructions` 的负面样例。

════════════════════════════════════════════════════════════════════
🔴 三条不许碰的红线（碰了就是**假证据 / 循环论证**）
════════════════════════════════════════════════════════════════════

1. **基准结论不许从裁定自己推**。
   「我判了 X」与「X 是对的」是两件事。用裁定自己当基准去"校准" ⇒
   一致率恒等于 100%，**校准这件事直接失去意义**。
   ⇒ `ground_truth` 只能来自**外部标注**（`legal_acps/rulings/ground_truth.jsonl`），
   没有标注就是没有（字段留空，**不许用裁定填**）。

2. **「N 条经验」≠「N 条可用于校准的经验」**。
   绝大多数裁定没有基准结论（没人去核），它们能进先例（提示"当年怎么判的"），
   但**不能**进一致率的分子分母。
   ⇒ `stats()` 分开报 `total` / `graded` / `undetermined_rate`；
   `graded == 0` 时 `accuracy` 是 **`None`（无法判定）**，**不是 0.0**
   （0.0 会被读成"全错"，而真相是"没有可比的东西"）。

3. **先例是提示，不是证据**。
   注入的每一条都必须带上「命中了哪几个片段」的解释，并明写
   "先例不能直接当本案结论" —— 否则它会变成一条没有来源的权威依据。

════════════════════════════════════════════════════════════════════
数据从哪来（**不新增写入通路**）
════════════════════════════════════════════════════════════════════

经验 = **已有的裁定产物** `output/ruling-*.json`（本来就在落盘，是唯一事实源）
     + **外部基准标注** `legal_acps/rulings/ground_truth.jsonl`（维护者手写）。

⇒ 本模块**只读不写**，不存在"第二份裁定记录"。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path

from .text_match import NOISE, GramIndex, Match

ROOT = Path(__file__).resolve().parent.parent
#: 裁定产物的目录。**唯一事实源**：裁定本来就在落盘，不去另建一份。
OUTPUT_DIR = ROOT / "output"
RULING_GLOB = "ruling-*.json"

#: 外部基准标注。**按裁定文件名对应**，不做模糊匹配 ——
#: "这条裁定对应哪个案子"这种判断不该由相似度偷偷做主。
GROUND_TRUTH_FILE = ROOT / "legal_acps" / "rulings" / "ground_truth.jsonl"

#: 每轮最多注入几条先例。宁少勿滥：先例是**提示**，塞太多会变成噪声权威。
DEFAULT_LIMIT = 3

#: 同事件折叠的相似度门槛。⚠️ **这是去重口径，不是检索阈值** —— 两个用途的
#: 容错方向相反：折叠宁可并得准（并错 = 丢一条真不同的经验），检索宁可拦得狠
#: （放错 = 喂一条假相关）。实测（2026-10-07，服务器 30 条真实裁定）：
#: 食堂事件的 10 种措辞变体（±几个字的传述标记差异）两两 ratio ≥ 0.85；
#: 而「某小区自来水污染」与「某县城自来水污染」这类**同题材不同事件**
#: ratio ≈ 0.5–0.6 ⇒ 取 0.75 居中，两组都不误伤。**改它必须拿真实数据重验**。
FOLD_RATIO = 0.75

#: ⚠️ 检索阈值。**当前是占位值，不是实测值** —— 库里裁定太少（< 20 条），
#: 两组分布根本派生不出来。见 `MIN_SCORE_DERIVED`：一旦库涨到
#: `MIN_SCORE_DERIVE_AT` 条而阈值仍未实测重定，`check_local` 会**报失败**。
#: （本项目纪律：拍脑袋定的阈值等于没有阈值；所以让判据盯着这件事。）
MIN_SCORE = 6.0
MIN_SCORE_DERIVED = False
MIN_SCORE_DERIVE_AT = 20


@dataclass(frozen=True)
class RulingCase:
    """一条裁定经验。

    字段全部来自裁定产物 + 外部标注，**不做任何加工** —— 加工就是编造。
    """

    source: str                 # 从哪份产物读来的（`ruling-*.json` 的文件名）
    event: str                  # 被研判的事件描述（裁定书里的 `event`）
    verdict: str                # 裁定：属实 / 不实 / 无法判定 / …
    confidence: float
    basis: tuple[str, ...] = ()
    fallacies: tuple[str, ...] = ()
    ground_truth: str = ""      # 基准结论；**空 = 没有基准**（不许拿裁定填）
    error_reason: str = ""      # 错因，由维护者在标注里写；代码**不推断**
    recorded_at: str = ""
    #: 该轮走的车道（2026-10-12 分级裁决）：``fast`` = 快车道产出（跳过辩手）、
    #: ``debate`` = 辩论道产出。**历史裁定（旧 JSON 无此字段）按 debate 处理**——
    #: 它们确实都经过辩论，是事实陈述不是宽容默认。
    #: 🔴 这个字段是「快裁不能自我复制」回路切断的依据：fast + 无 gt 标注
    #:    ⇒ 无快裁触发资格（``fast_lane.is_qualified``）。
    lane: str = "debate"
    #: 该事件在库里被裁定过几轮（折叠时累加）。>1 说明这是「反复出现的事件」，
    #: 对裁判是有用的信号（同类事件反复被研判 ⇒ 值得额外谨慎）。
    repeats: int = 1

    @property
    def graded(self) -> bool:
        """有没有基准结论。没有 ⇒ 不能进一致率的分子分母。"""
        return bool(self.ground_truth)

    @property
    def fast_lane_qualified(self) -> bool:
        """快裁触发资格：**辩论道产出 或 有人核 gt 标注**（单一来源）。

        · lane=fast 且无标注 = 无资格——「快裁不能自我复制」的回路切断点
          （快裁→入库→命中先例→仍不能快裁→落回辩论道重新过对抗）；
        · fast + 有标注 = 有资格（人核比「无对抗」的疑点更硬）；
        · 历史裁定 lane=debate ⇒ 有资格（它们确实都经过辩论）。
        消费方：``fast_lane.decide_lane``（触发判定）与 ``render``
        （先例注入的降权打标）——同一判定不写两处。
        """
        return self.lane != "fast" or self.graded

    @property
    def correct(self) -> bool | None:
        """与基准结论是否一致。**无基准 ⇒ `None`（无法判定），不是 False**。"""
        if not self.graded:
            return None
        return self.verdict.strip() == self.ground_truth.strip()


@dataclass(frozen=True)
class RulingHit:
    case: RulingCase
    match: Match


@dataclass
class LoadReport:
    total: int = 0
    kept: int = 0
    dropped: int = 0
    #: 同事件折叠掉的条数（`total - kept - dropped` 应等于它）。
    #: 🔴 必须报出来：「30 条经验」与「13 个事件、其中 25 轮是同一件事」
    #: 是两句话，少报一个就是在虚报库容。
    duplicates: int = 0
    graded: int = 0
    #: 折叠前的有效条数（`kept + duplicates`）。与 `total`（读过几个文件）
    #: 的差 = 坏文件数。三个数一起看，才说得清「30 条裁定」到底是 30 个事件
    #: 还是 13 个事件跑出 30 份裁定书。
    kept_before_fold: int = 0
    files: list[str] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kept > 0


def _load_ground_truth(path: Path) -> dict[str, dict[str, str]]:
    """读外部基准标注。文件不存在 ⇒ 返回空（**不是错误**：大部分裁定没基准）。"""
    out: dict[str, dict[str, str]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        src = str(obj.get("source") or "").strip()
        if src:
            out[src] = obj
    return out


def _fold_duplicates(cases: list[RulingCase]) -> tuple[list[RulingCase], int]:
    """同事件多轮裁定折叠成一条经验（🔴 治偏食的库侧半边，2026-10-07）。

    为什么必须有它（实测）：**30 条裁定里只有 13 个不同事件**，其中一个事件
    被跑了 **25 轮**（每次真机验证都用同一句测试输入）。后果有三层 ——
      1. `total=30` 虚报库容；一致率变成"那一个事件的一致率"；
      2. **df 被灌水**：近重复项的每个 gram df≈25 > `df_cut=12` ⇒ 全被当
         虚词丢掉 ⇒ 自召回探针 **16/30 连自己都召不回**（检索直接失效）；
      3. 先例注入会一次吐出 3 条几乎相同的食堂先例（limit=3 全被同一事件占满）。

    折叠规则：
      · 分组：先按归一化文本精确匹配，再按 `SequenceMatcher.ratio ≥ FOLD_RATIO`
        贪心并入已有组（处理顺序 = 文件名排序 = 时间正序，⇒ 结果可复现）。
      · 代表：**有基准标注的那轮优先** —— `verdict` 与 `ground_truth` 必须
        来自**同一轮**，把旧轮的基准拼到新轮的裁定上 = **伪造配对**；
        都没标注 ⇒ 取最新一轮（代表当前判断水平）。
      · `repeats` 记组内轮数 —— 「该事件反复出现」这个信号**不许在折叠中丢掉**。

    ⚠️ 折叠的边界（如实说清，不许粉饰）：ratio 折叠**认不出**换了主体的
    同模板事件（「A 学校食堂」vs「B 学校食堂」ratio 很高但可能是真不同事件）。
    错并的代价是少一条经验；错分的代价是留重复。在「库小、重复严重」的现状下
    前者便宜得多 —— 但**库大了以后要重验 FOLD_RATIO**（写死它就是为了让人看见）。
    """
    groups: list[dict[str, object]] = []
    for c in cases:
        norm = _norm_event(c.event)
        target: dict[str, object] | None = None
        for g in groups:
            if norm == g["norm"] or SequenceMatcher(
                    None, str(g["norm"]), norm).ratio() >= FOLD_RATIO:
                target = g
                break
        if target is None:
            groups.append({"norm": norm, "newest": c, "graded": c if c.graded else None,
                           "repeats": 1})
            continue
        target["repeats"] = int(target["repeats"]) + 1
        # newest：文件名序 = 时间正序 ⇒ 后来的就是更新的
        target["newest"] = c
        if c.graded:
            # 有标注的轮优先当代表；后来又有标注 ⇒ 更新的标注轮接管
            target["graded"] = c
    out: list[RulingCase] = []
    for g in groups:
        rep = g["graded"] or g["newest"]
        assert isinstance(rep, RulingCase)
        out.append(replace(rep, repeats=int(g["repeats"])))
    folded = sum(int(g["repeats"]) - 1 for g in groups)
    return out, folded


class RulingStore:
    """裁定经验库。构造后**不可变**。"""

    def __init__(self, cases: Iterable[RulingCase]) -> None:
        self.cases: tuple[RulingCase, ...] = tuple(cases)
        # `head` = 事件描述（主旨），`body` = 裁定依据（`basis` 合起来）
        self.index = GramIndex((c.event, " ".join(c.basis)) for c in self.cases)

    # ── 加载 ────────────────────────────────────────────────────────
    @classmethod
    def load(cls, root: Path | None = None, *,
             ground_truth: Path | None = None) -> tuple["RulingStore", LoadReport]:
        """读 `output/ruling-*.json` + 外部基准标注。

        返回 **(库, 报告)** —— 报告不许丢：丢了多少、有几条有基准，
        是判断"这个数字能不能用"的依据。
        """
        d = root or OUTPUT_DIR
        rep = LoadReport()
        cases: list[RulingCase] = []
        try:
            files = sorted(d.glob(RULING_GLOB))
        except OSError as exc:
            rep.reasons[f"目录不可读: {exc}"] = 0
            return cls(()), rep

        gt = _load_ground_truth(ground_truth or GROUND_TRUTH_FILE)
        if not files:
            rep.reasons["没有 ruling-*.json（还没跑过裁定）"] = 0
            return cls(()), rep

        for p in files:
            rep.files.append(p.name)
            rep.total += 1
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                rep.dropped += 1
                rep.reasons[f"{p.name}: 读不了 {type(exc).__name__}"] = \
                    rep.reasons.get(f"{p.name}: 读不了 {type(exc).__name__}", 0) + 1
                continue
            event = str(obj.get("event") or "").strip()
            if not event:
                # 没有事件描述的裁定书对不上任何新事件 ⇒ 丢了就丢了，但**要记账**
                rep.dropped += 1
                rep.reasons["缺 event（无法与新事件比对）"] = \
                    rep.reasons.get("缺 event（无法与新事件比对）", 0) + 1
                continue
            note = gt.get(p.name) or {}
            try:
                conf = float(obj.get("confidence") or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            cases.append(RulingCase(
                source=p.name,
                event=event,
                verdict=str(obj.get("verdict") or "").strip(),
                confidence=conf,
                basis=tuple(str(x) for x in (obj.get("basis") or [])),
                fallacies=tuple(
                    str(f.get("kind") or "") for f in (obj.get("fallacies") or [])
                    if isinstance(f, dict)),
                ground_truth=str(note.get("ground_truth") or "").strip(),
                error_reason=str(note.get("error_reason") or "").strip(),
                recorded_at=p.name.replace("ruling-", "").replace(".json", ""),
                # lane 解析：缺字段 = 历史辩论道裁定（见 RulingCase.lane 注释）。
                # ⚠️ 值白名单收紧：只认 "fast"，**其余一律归 debate**——
                #    不认识的值、写错的值都保守按辩论道处理（快车道宁可漏开不可误开）。
                lane="fast" if str(obj.get("lane") or "").strip() == "fast" else "debate",
            ))
        rep.kept_before_fold = len(cases)
        # 🔴 同事件折叠（治偏食）：30 条 → 13 组的事就发生在这一行。
        #    折叠数必须报出来 —— 「库很干净」与「偷偷并掉一半」要分得开。
        cases, rep.duplicates = _fold_duplicates(cases)
        rep.kept = len(cases)
        rep.graded = sum(1 for c in cases if c.graded)
        return cls(cases), rep

    # ── 检索 ────────────────────────────────────────────────────────
    def search(self, event: str, *, limit: int = DEFAULT_LIMIT,
               min_score: float = MIN_SCORE) -> list[RulingHit]:
        """找**同类事件**的既往裁定。低于阈值一律不返回（宁缺勿假）。"""
        if not (event or "").strip() or not self.cases:
            return []
        return [RulingHit(case=self.cases[m.index], match=m)
                for m in self.index.search(event, limit=limit, min_score=min_score)]

    # ── 注入提示词的渲染 ────────────────────────────────────────────
    @staticmethod
    def render(hits: Iterable[RulingHit]) -> str:
        """把先例渲染成给裁判看的一段话。

        ⚠️ **必须带解释**（命中了哪几个片段）与**必须带免责**（先例不是证据）——
           少了前者，"参考先例"就是个无法质疑的权威；少了后者，它会被当成结论用。
        ⚠️ **合格先例排前、无资格先例打标降后**（2026-10-12 分级裁决）：
           lane=fast 且无 gt 标注的先例是「无对抗快裁」，它没有快裁触发资格，
           注入时也必须让裁判看见这层身份——不标出来，模型就会把它当成
           与辩论道裁定同等的依据，防自我确认的「降权」就成了空话。
        """
        hits = list(hits)
        if not hits:
            return ""
        # 稳定排序：资格优先，同资格保持命中序（sort 稳定 ⇒ 可复现）。
        hits = sorted(hits, key=lambda h: 0 if h.case.fast_lane_qualified else 1)
        lines = [
            "【历史先例 · 仅供参照，**不是本案的证据**】",
            "以下是你（或本系统）此前对**相似事件**出具的裁定，用于提示"
            "同类事件当年容易判错在哪。",
            "",
        ]
        for i, h in enumerate(hits, 1):
            c = h.case
            head = f"{i}. 相似度 {h.match.score:.2f}｜命中片段 {list(h.match.matched)}"
            lines.append(head)
            lines.append(f"   事件：{c.event[:160]}")
            if c.repeats > 1:
                lines.append(f"   （⚠️ 同类事件已累计裁定 {c.repeats} 轮，"
                             f"此处展示的是{'有基准标注的那一轮' if c.graded else '最新一轮'}）")
            lines.append(f"   当时裁定：{c.verdict}（置信度 {c.confidence:.2f}）")
            if not c.fast_lane_qualified:
                # 🔴 无资格先例（fast + 无标注）必须打标——不标，防自我确认
                #    的降权就是一句空话；模型看不见身份差异就无从降权。
                lines.append("   ⚠️ 此条为**无对抗快裁**（跳过辩论的快速裁定、"
                             "未经人核）：参考价值低于经对抗检验的裁定，"
                             "只作方向提示，不得作为采信依据。")
            if c.graded:
                flag = "一致" if c.correct else "**不一致**"
                extra = f"；复盘：{c.error_reason}" if c.error_reason else ""
                lines.append(f"   基准结论：{c.ground_truth} → {flag}{extra}")
            else:
                lines.append("   基准结论：（未标注 —— 这条只作参考，不进一致率统计）")
            if c.fallacies:
                lines.append(f"   当时命中的话术：{'、'.join(c.fallacies)}")
            lines.append("")
        lines.append(
            "⚠️ 先例只说明「同类事件当时是怎么判的」，**不能直接推出本案结论**；"
            "本案仍须按你手上的材料独立判断。"
        )
        return "\n".join(lines)

    # ── 统计（校准台账的口径）────────────────────────────────────────
    def stats(self) -> dict[str, object]:
        """🔴 `accuracy` 在 `graded == 0` 时是 **`None`**，不是 0.0。

        「无法判定」与「全错」在数字上差得很远，而在"缺一个判断"的代码里
        长得一模一样 —— 这正是本项目反复踩的病，所以显式区分。
        """
        graded = [c for c in self.cases if c.graded]
        right = sum(1 for c in graded if c.correct)
        undetermined = len(self.cases) - len(graded)
        # 🔴 **去重后的事件数**：`total` 会被"同一事件反复跑"灌水，
        #    而一致率、阈值都是**按事件**才有意义的量。
        by_event: dict[str, int] = {}
        for c in self.cases:
            key = _norm_event(c.event)
            by_event[key] = by_event.get(key, 0) + 1
        raw_total = sum(c.repeats for c in self.cases)
        top = max((c.repeats for c in self.cases), default=0)
        return {
            "total": len(self.cases),
            # 🔴 **原始裁定数**（折叠前）：与 total 分开报 —— 「13 个事件、30 份
            #    裁定书」与「13 个事件、13 份裁定书」是两种完全不同的库况。
            "raw_total": raw_total,
            "distinct_events": len(by_event),
            "top_event_share": (top / raw_total) if raw_total else None,
            "graded": len(graded),
            "correct": right,
            "accuracy": (right / len(graded)) if graded else None,
            "undetermined": undetermined,
            "undetermined_rate": (undetermined / len(self.cases)) if self.cases else None,
            "verdicts": _counts(c.verdict for c in self.cases),
            "fallacies": _counts(f for c in self.cases for f in c.fallacies if f),
            "min_score_derived": MIN_SCORE_DERIVED,
            # ⚠️ 这一句刻意写成**一行**：元验证要拿它当退回锚点，跨行的续行
            #    被替换后会变成悬空的 `and not …` ⇒ 退回后**语法错误**，
            #    而语法一错，那次元验证就**证明不了任何事**（2026-10-07 实证）。
            "min_score_pending": len(self.cases) >= MIN_SCORE_DERIVE_AT and not MIN_SCORE_DERIVED,
        }

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self) -> Iterator[RulingCase]:
        return iter(self.cases)


def _norm_event(text: str) -> str:
    """事件的归一化形态（去掉标点空白后比对）。

    ⚠️ 为什么要有它：实测（2026-10-07，服务器）**30 条裁定里只有 13 个不同事件**，
    其中「某学校食堂使用过期食材」一个事件被跑了 **25 轮**。
    ⇒ `total=30` 是个**虚高的数字**：一致率会变成"那一个事件的一致率"，
    阈值也会被近重复项污染。所以统计**必须**同时报 `distinct_events`。
    """
    return NOISE.sub("", text or "")


def _counts(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        key = v.strip() or "(空)"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda t: -t[1]))
