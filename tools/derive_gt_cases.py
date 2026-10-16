"""从事实库批量生成**校准基准草稿**（gt=官方定性，人核后才能用）。

为什么需要：ground_truth 手写 13 条太薄（n=13 的一致率说服力有限）。而事实库
131 条辟谣材料**本身就是官方定性**——每条辟谣文的发布方就是定性权威，这不是
「从裁定自己推基准」（循环论证），基准来源仍是外部官方信源。

🔴 三条纪律（违反任何一条，这个工具就从「扩样本」变成「编数据」）：
  1. **定性只认正文里的官方结论**（「不属实 / 不实 / 谣言 / 虚假 / 编造」），
     提取不到定性的一律**跳过并计数**，不许猜。事实库里约 23 条是科普/提示/
     治理成果类，本来就不是单事件辟谣——跳过是对的。
  2. **input 材料不含处罚结果**（案例库同款纪律）——辟谣文里的「某某被行政
     拘留」绝不进 input，否则等于把答案的后半段喂给裁判。
  3. **产出是草稿**（gt_candidates.jsonl），必须人逐条核对后才能进
     ground_truth.jsonl。工具不写 ground_truth——那是人的活。

谣言原文提取（优先级，取到即止）：
  ① title 问号前半（「四川一养猪场一天死亡500头猪？」→ 引号内为谣言原文）
  ② title 引号内（无问号形态）
  ③ body「网传"……"」引号内
三种都提取不到 ⇒ 跳过（计数），不许拿辟谣文的叙述句当谣言原文。

与经验库判重：与 `pick_todo` 同一把尺子（`_norm_event` 精确 + ratio ≥
FOLD_RATIO + BM25 ≥ COVER_SCORE，任一命中即已入库）——同一事件在库里已有
经验/基准的，跳过（重复进库只会虚增 n，不会增加信息量）。
"""

from __future__ import annotations

import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_acps.fact_store import FactStore  # noqa: E402
from legal_acps.ruling_store import (  # noqa: E402
    FOLD_RATIO, RulingStore, _norm_event,
)
from tools.fetch_facts import CURATED_URLS  # noqa: E402

#: 与 `run/_enrich_rulings.COVER_SCORE` 同一条线（那里有实测分布依据）。
#: 直接 import 那个模块会连累它 import 一堆运行时依赖（agents/llm），
#: 所以这里复制**数值**并在 check_local 里用「两处相等」断言钉住（单一来源的
#: 妥协形态：数值 + 断言，优于再抄一份判据）。
COVER_SCORE = 20.0

#: 官方定性（「不实」）判定。**定义已迁至 `legal_acps/lead_store.py`**（单一来源：
#: lead-submit 闸门与本工具筛候选共用同一把尺子——同一判定写两处必然漂移）。
#: **只认正文结论**，标题里的「辟谣/谣言」字样不算——标题写「辟谣」可能只是
#: 栏目词（「今日辟谣」的每条并不都是不实结论）。
from legal_acps.lead_store import FALSE_PAT, PUNISH_PAT  # noqa: E402

#: 谣言原文提取。
TITLE_Q = re.compile(r"^\s*[“\"]?([^？?”\"]{8,60})[”\"]?\s*[？?]")
TITLE_QUOTED = re.compile(r"[“\"]([^”\"]{8,60})[”\"]")
BODY_WANGCHUAN = re.compile(r"网传[“\"]([^”\"]{6,80})[”\"]")

#: 处罚类字样（input 检查用——提取出的谣言原文里不该有，有则跳过该条）。
#: 定义也在 lead_store（见上）。

#: 对齐窗口与最短公共子串：**定义已迁至 `legal_acps/fast_lane.py`**（2026-10-12
#: 快车道分级裁决——gt 派生的对齐筛子与快车道触发共用同一把尺子，
#: 单一来源不写两份）。从这里 import（tools 可 import runtime，反向不行）。
from legal_acps.fast_lane import (  # noqa: E402
    ALIGN_MIN_LEN,
    ALIGN_WINDOW,
    shares_substring as _shares_sub,
)

OUT = ROOT / "legal_acps" / "rulings" / "gt_candidates.jsonl"


def extract_rumor(f) -> str:
    """谣言原文：title 问号前半 > title 引号内 > body 网传引号内。取到即止。

    「栏目 | 内容」前缀一律剥掉（2026-10-11 实测：辟谣平台系文章标题普遍是
    「成都网络辟谣 | 四川绵阳……」形态，不剥会把栏目名当谣言的一部分）。
    """
    t = (f.title or "").strip()
    m = TITLE_Q.match(t)
    if m:
        raw = m.group(1)
    else:
        m = TITLE_QUOTED.search(t)
        if m:
            raw = m.group(1)
        else:
            m = BODY_WANGCHUAN.search(f.body or "")
            raw = m.group(1) if m else ""
    if not raw:
        return ""
    return raw.split("|")[-1].strip()


def false_verdict(f, rumor: str) -> str:
    """与**本谣言**对齐的官方定性引文；没有则空串（调用方跳过）。

    对齐判据：FALSE_PAT 的某个命中点 ±ALIGN_WINDOW 窗口里出现 rumor 的
    ≥ALIGN_MIN_LEN 连续子串——定性说的是这条谣言，不是同文里的其他谣言。
    """
    body = f.body or ""
    for m in FALSE_PAT.finditer(body):
        win = body[max(0, m.start() - ALIGN_WINDOW):m.end() + ALIGN_WINDOW]
        if _shares_sub(rumor, win):
            start = max(0, m.start() - 30)
            return body[start:m.end() + 30].replace("\n", " ").strip()
    return ""


# （旧 `_shares_sub` 本体已迁至 `legal_acps/fast_lane.shares_substring`，
#  上面的 import 以 `_shares_sub` 别名接住全部调用点。本地再定义一份
#  = 单一来源分家——check_local ⑦-10 用「同对象断言」盯着。）


def build_input(rumor: str) -> str:
    """当事人视角的求证输入（案例库同款口吻；不含定性、不含处罚结果）。"""
    return f"网上都在传「{rumor}」，说得有鼻子有眼的，好多人都在转。这是真的吗？"


def main() -> int:
    fs, frep = FactStore.load()
    if not len(fs):
        raise SystemExit("事实库为空 —— 先跑 tools/fetch_facts")
    store, _ = RulingStore.load()

    # —— 判重尺子（与 pick_todo 同款）——
    known_norms = [_norm_event(rc.event) for rc in store]

    def covered(rumor: str) -> bool:
        n = _norm_event(rumor)
        if any(n == k for k in known_norms):
            return True
        if any(SequenceMatcher(None, k, n).ratio() >= FOLD_RATIO
               for k in known_norms):
            return True
        return any(h.match.score >= COVER_SCORE
                   for h in store.search(rumor, limit=3, min_score=0.0))

    stats = {"总条数": len(fs), "提取不到谣言原文": 0, "定性未对齐本谣言": 0,
             "含处罚字样": 0, "已有基准(CURATED源)": 0,
             "已入库(判重)": 0, "候选": 0}
    curated_urls = {d["url"] for d in CURATED_URLS}
    candidates: list[dict] = []
    seen_rumors: set[str] = set()          # 事实库内部去重（同事件多篇辟谣文）
    for f in fs:
        status, payload = screen_fact(f, curated_urls, covered, seen_rumors)
        stats[status] = stats.get(status, 0) + 1
        if status == "候选":
            seen_rumors.add(_norm_event(payload["rumor_text"]))
            candidates.append(payload)

    OUT.write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in candidates),
        encoding="utf-8")
    print("统计：", json.dumps(stats, ensure_ascii=False))
    print(f"草稿已写 {OUT}（{len(candidates)} 条）——🔴 人逐条核对"
          "（quote 定性 / rumor 完整 / input 无处罚）后才能进 ground_truth")

    # —— 负对照断言（判据跟着工具走，跑一次自证一次）——
    bad = run_negatives()
    if bad:
        print("负对照失败：", bad)
        return 1
    print("负对照全部通过")
    return 0


def screen_fact(f, curated_urls, covered, seen_rumors):
    """单条筛（纯函数，负对照可注入）。返回 (统计键, 候选dict 或 None)。"""
    if f.url in curated_urls:
        # 已有基准的事件的源材料（13 案例的 CURATED 证据源）——它的 gt
        # 早已人写进 ground_truth，再收就是重复基准。
        return "已有基准(CURATED源)", None
    rumor = extract_rumor(f)
    if not rumor:
        return "提取不到谣言原文", None
    if _norm_event(rumor) in seen_rumors:
        return "已入库(判重)", None      # 事实库内部同事件，算重复
    quote = false_verdict(f, rumor)
    if not quote:
        # 对齐失败包含两种：全文无定性 / 定性说的是别的谣言（合集文）。
        # 分开数会两次全文扫描，不值得——两种的处置相同：跳过不猜。
        return "定性未对齐本谣言", None
    if PUNISH_PAT.search(rumor):
        return "含处罚字样", None
    if covered(rumor):
        return "已入库(判重)", None
    return "候选", {
        "fact_url": f.url,
        "publisher": f.publisher,
        "published": str(f.published),
        "title": f.title,
        "rumor_text": rumor,
        "input": build_input(rumor),
        "ground_truth": "不实",
        "quote": quote,
    }


def run_negatives() -> list[str]:
    """负对照：每条筛子用「只差一个变量」的输入证明自己真的在响。"""
    bad = []

    class _F:  # 最小 Fact 替身
        def __init__(self, title, body, url="http://x/1"):
            self.title, self.body, self.url = title, body, url
            self.publisher, self.published = "x", ""

    never = lambda r: False        # noqa: E731  判重 stub：永不判重
    empty_seen: set[str] = set()

    # ① 提取到谣言但**无定性**的条目必须被跳过（不许猜）
    st, _ = screen_fact(_F("国庆高速免费还能领补贴？",
                           "自驾出行、宾馆住宿等活动增多，请提前规划。"),
                        set(), never, empty_seen)
    if st != "定性未对齐本谣言":
        bad.append(f"① 无定性未被跳过（{st}）")
    # ② 「不属实」必须被认成不实定性（不得因「属实」子串而漏判）
    st, _ = screen_fact(
        _F("某地自来水被污染了？", "经核实，网传某地自来水被污染情况不属实。"),
        set(), never, empty_seen)
    if st != "候选":
        bad.append(f"②「不属实」没被认成不实定性（{st}）")
    # ③ 叙述句 title 提取不到谣言原文（不拿治理成果标题凑数）
    if extract_rumor(_F("清理违规视频13300余条 7月治理成果公布", "…")) != "":
        bad.append("③ 叙述句被当成谣言原文")
    # ④ 合集文错配必跳过：定性命中点说的是**别的**谣言
    st, _ = screen_fact(
        _F("河南许昌要开通第二条地铁？",
           "另悉：某地高架桥断裂系谣言，均为旧图拼接。"),
        set(), never, empty_seen)
    if st != "定性未对齐本谣言":
        bad.append(f"④ 合集文定性错配未被跳过（{st}）")
    # ⑤ CURATED 源必跳过（已有基准的事件不重复收）
    st, _ = screen_fact(_F("某地自来水被污染了？", "经核实，网传某地自来水被污染情况不属实。",
                           url="http://curated/1"),
                        {"http://curated/1"}, never, empty_seen)
    if st != "已有基准(CURATED源)":
        bad.append(f"⑤ CURATED 源未被跳过（{st}）")
    # ⑥ 处罚字样必跳过（input 不许含处罚结果）
    st, _ = screen_fact(
        _F("某某造谣者被行政拘留了？", "经核实，网传某某造谣者被行政拘留系谣言。"),
        set(), never, empty_seen)
    if st != "含处罚字样":
        bad.append(f"⑥ 处罚字样未被跳过（{st}）")
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
