"""快车道判定（2026-10-12 · 检索优先分级裁决）—— **代码判，模型只执行**。

背景（用户拍板的设计，讨论记录与真机 A/B 见《上线操作手册》第 16 步）：

  旧流程里辩论是每轮必经；新流程按「库里有没有可复用的判定」分级：

    L0 快车道：事实库官方材料对齐命中，**或** 经验库合格先例事件级对齐命中
               → 跳过辩手，直接出裁定（引用官方材料 / 先例出处）。
    L1 辩论道：无命中 / 检索失败 / 只命中无资格先例 → 现行并发扇出流程，一字不动。

  检索对象主体是**经验库**（裁判自己维护的库）；事实库命中是更硬的一层。
  方向无关——「确有其事」与「不实」都能走快车道（对齐判据不看方向）。

════════════════════════════════════════════════════════════════════
防自我确认回路（中间档「进池但降权 + 打标」的核心规则）
════════════════════════════════════════════════════════════════════

  经验库的入库是自动滴灌的（`fact_issue_ruling` 无条件落盘、
  `RulingStore.load()` glob 全量收）。快车道的裁定也会这么进库——
  若不加限制，「快裁 → 入库 → 下次命中先例 → 又快裁」的自我确认回路
  结构性存在，且每一跳都无人核验。故定死资格规则：

      **只有「辩论道产出」或「有人核 gt 标注」的裁定才有快裁触发资格；
        快车道自己的裁定进池、打标降权，但永远无触发资格。**

  这样回路在第一跳就被切断（快裁的祖先链上必有至少一场辩论或一次人核），
  而「辩完入库供下次复用」完整保住。

════════════════════════════════════════════════════════════════════
单一来源（同一判定不写两处）
════════════════════════════════════════════════════════════════════

  · ``ALIGN_WINDOW`` / ``ALIGN_MIN_LEN`` / ``shares_substring``：**定义在本模块**，
    ``tools/derive_gt_cases.py`` 从这里 import（gt 派生的对齐筛子与快车道
    共用同一把尺子——它不能被 runtime import，runtime 模块可以被 tools import）。
  · 窗口筛子用 ``lead_store.FALSE_PAT``（官方定性模式）——与 lead-submit 闸门、
    derive 派生是同一个正则对象。
  · 事件级对齐用 ``ruling_store.FOLD_RATIO`` + ``_norm_event``——与经验库
    「同一事件折叠」是同一条判定（库说「这是同一件事」与快车道说「这是
    同一件事」必须是同一句话）。
  · ``is_qualified``：唯一的快裁资格判定，受理工具、先例渲染都从这里取。
"""

from __future__ import annotations

from difflib import SequenceMatcher

from .lead_store import FALSE_PAT
from .ruling_store import FOLD_RATIO, RulingCase, _norm_event

#: 对齐窗口：定性命中点前后各 120 字（从 derive_gt_cases 迁来，原值不变）。
#: 月度综述/合集辟谣文里「不实/谣言」命中说的是**别的**谣言
#: （2026-10-11 人核 21 条实测：11 条错配全是这种形态）。
ALIGN_WINDOW = 120

#: 对齐所需的最短公共子串长度（事件文本的连续片段出现在定性窗口里）。
ALIGN_MIN_LEN = 4

#: 快车道 lane 值（落盘进裁定书 JSON 的 `lane` 字段）。
LANE_FAST = "fast"
LANE_DEBATE = "debate"


def shares_substring(a: str, b: str, *, min_len: int = ALIGN_MIN_LEN) -> bool:
    """a 的某个 ≥min_len 连续片段是否出现在 b 里（对齐筛子本体）。

    ⚠️ **不做任何归一化**——原封继承 derive_gt_cases 的 `_shares_sub`
       语义（迁移必须零行为变化；derive 的负对照盯着老行为）。
       调用方需要容标点差异时**自己先 norm**（见 `official_confirms`）。
    """
    a, b = a or "", b or ""
    if len(a) < min_len or len(b) < min_len:
        return False
    for i in range(len(a) - min_len + 1):
        if a[i:i + min_len] in b:
            return True
    return False


def official_confirms(event: str, fact_body: str) -> bool:
    """官方材料的定性是否与**本事件**对齐（derive 同款窗口筛子）。

    对齐判据：``FALSE_PAT`` 的某个命中点 ±ALIGN_WINDOW 窗口里出现事件文本的
    ≥ALIGN_MIN_LEN 连续片段——定性说的是这件事，不是同文里的其他谣言。
    ⚠️ 只覆盖证伪向（FALSE_PAT 是「不实」系模式）；证实向材料天然不走这条，
    「确有其事」的快车道由经验库合格先例承担（方向无关的对齐）。
    """
    body = fact_body or ""
    # 事件与窗口都先归一化（受理事件 vs 材料正文是同一件事的两种排版）；
    # 筛子本体不做归一化（保 derive 原语义），norm 在这一层做。
    event_norm = _norm_event(event or "")
    for m in FALSE_PAT.finditer(body):
        win = body[max(0, m.start() - ALIGN_WINDOW):m.end() + ALIGN_WINDOW]
        if shares_substring(event_norm, _norm_event(win)):
            return True
    return False


def same_event(a: str, b: str) -> bool:
    """事件级对齐：归一化后 ``SequenceMatcher.ratio ≥ FOLD_RATIO``。

    与经验库 ``_fold_duplicates`` 判「同一事件」用同一条尺子——
    「折叠说这是同一件事」与「快车道说这是同一件事」必须是同一句话，
    否则两处各改各的，漂移从此静默。
    """
    na, nb = _norm_event(a or ""), _norm_event(b or "")
    if not na or not nb:
        return False
    return SequenceMatcher(None, na, nb).ratio() >= FOLD_RATIO


def is_qualified(case: RulingCase) -> bool:
    """快裁资格：**辩论道产出 或 有人核 gt 标注**。

    判定本体在 ``RulingCase.fast_lane_qualified``（单一来源，避免本模块
    与 ``ruling_store.render`` 循环 import）；本函数只是它在本模块的别名，
    供 ``decide_lane`` 的可读性。

    · 历史裁定（无 lane 字段）按辩论道处理——它们确实都经过辩论，这是
      事实陈述，不是宽容默认；
    · fast + 无标注 = 无资格（防自我复制，见模块头）；
    · fast + 有标注 = 有资格（人核推翻「无对抗」的疑点，gt 是更硬的一层）。
    """
    return bool(getattr(case, "fast_lane_qualified", False))


def decide_lane(
    event: str,
    *,
    fact_hits: list | None = None,
    ruling_root=None,
) -> dict:
    """对一段事件文本判定快/辩车道。**唯一判定入口**（受理与落盘共用）。

    参数
    ----
    event
        被研判的事件一句话（受理时是 ``case.event``，落盘时是 ``ruling.event``）。
    fact_hits
        事实库检索结果（``FactStore.Hit`` 列表）。受理工具已搜过就传进来复用；
        ``None``（落盘路径）时本函数自己搜——判定逻辑只此一份。
    ruling_root
        经验库根目录（判据注入临时目录用；生产留空 = 默认 ``output/``）。

    返回（永远结构化，检索失败**不挡**调用方主流程）::

        {
          "lane": "fast" | "debate",
          "aligned_official": [官方材料摘要…],   # 事实库对齐命中
          "qualified_precedents": [合格先例摘要…], # 经验库合格先例命中
          "lane_error": "",                       # 检索失败如实记
        }

    ⚠️ 「模型自报的 lane」**不存在也不许存在**：落盘时裁判重建这份判定
       （``fact_issue_ruling`` 只信自己重算的）——「判据的期望不许取自
       被测对象自己的标记」。
    """
    out: dict = {
        "lane": LANE_DEBATE,
        "aligned_official": [],
        "qualified_precedents": [],
        "lane_error": "",
    }
    aligned_official: list[dict] = []

    # ── 第一层：事实库官方材料（定性窗口对齐）──────────────────────
    try:
        if fact_hits is None:
            from .fact_store import FactStore

            store_f, _rep = FactStore.load()
            fact_hits = store_f.search(event, limit=5)
        for h in fact_hits:
            fact = getattr(h, "fact", None)
            body = getattr(fact, "body", "") or ""
            if official_confirms(event, body):
                aligned_official.append({
                    "title": getattr(fact, "title", ""),
                    "url": getattr(fact, "url", ""),
                    "origin_publisher": getattr(fact, "origin_publisher", ""),
                    "published": getattr(fact, "published", ""),
                    "score": round(float(getattr(h, "score", 0.0)), 2),
                })
    except Exception as exc:  # noqa: BLE001 —— 旁路：如实记，不挡主流程
        out["lane_error"] = f"fact_store: {type(exc).__name__}: {exc}"
    out["aligned_official"] = aligned_official

    # ── 第二层：经验库合格先例（事件级对齐 + 资格规则）──────────────
    qualified: list[dict] = []
    try:
        from .ruling_store import RulingStore

        store, _rep = RulingStore.load(ruling_root)
        # ⚠️ min_score=0：BM25 在这里只是**候选生成器**，不是门槛——
        #    单文档/小库的 IDF 退化会让完全相同的事件文本只得 ~5 分
        #    （2026-10-12 实测 5.08 < MIN_SCORE 6.0），若用它预筛，
        #    先例层会静默空转（「装了 ≠ 会响」同型）。真正的门槛是
        #    下面的 same_event（归一化 ratio ≥ FOLD_RATIO）+ 资格规则。
        for h in store.search(event, limit=8, min_score=0.0):
            c = h.case
            if not (is_qualified(c) and same_event(event, c.event)):
                continue
            qualified.append({
                "source": c.source,
                "event": c.event,
                "verdict": c.verdict,
                "confidence": round(float(c.confidence), 2),
                "graded": c.graded,
                "ground_truth": c.ground_truth,
                "lane": getattr(c, "lane", LANE_DEBATE),
            })
    except Exception as exc:  # noqa: BLE001 —— 旁路：如实记，不挡主流程
        msg = f"ruling_store: {type(exc).__name__}: {exc}"
        out["lane_error"] = (out["lane_error"] + "；" + msg).strip("；")
    out["qualified_precedents"] = qualified

    # 两层任一命中 ⇒ 快车道。检索失败（lane_error 非空且无命中）
    # 保守回辩论道——快车道宁可漏开不可误开。
    if aligned_official or qualified:
        out["lane"] = LANE_FAST
    return out
