"""元验证：事实库的检索判据**真的有牙**（#135 的配套，2026-10-07）。

被测的三条判据（都在 `legal_acps/check_local.py` 的 ⑦-5h）：

  A. **闸门要分得开** —— 与库内容相关的查询必须返回非空，完全不相关的查询
     必须返回空（阈值真的在拦，不是摆设）。
  B. **溯源字段不许含糊** —— 每条事实必须带 `url` / 原始发布方 / 日期。
  C. **同 URL 只留一条**（2026-10-07 加）—— 重抓快照不许整文翻倍：库里实测
     12 个 URL 各存两份，检索 top-N 被翻倍条目挤占、库容虚高，而「重抓过」
     与「库里真有两条」在调用方看来一模一样。

为什么必须元验证（而不是只跑一次 check_local 看它绿）：

  事实库的打分函数**连着废了三版**（IDF top-K / 全量覆盖度 / max 归一化），
  每一版**都能跑通、都返回非空、check_local 都会绿**，但相关与不相关的分数区间
  **完全重叠** ⇒ 一个只断言"能检索出东西"的判据是**空转**的。
  ⇒ 绿不绿说明不了任何事，必须证明"退回旧形态时它会红"。

被修的缺陷（真实存在过，两个都在 2026-10-07 当天）：

  1. **阈值形同虚设**：`MIN_SCORE` 若被改成 0，不相关查询会返回 5 条噪声；
     而噪声一旦被当成"权威依据"喂给模型，就是**假证据**。
  2. 🔴 **权重折扣被静默丢掉**：`_score()` 写成 `for g, _ in qg` 又用
     `self._idf[g]` 取值 ⇒ `_query_grams()` 辛苦算好的虚字折扣与 df=1 折扣
     **全部没生效**，而函数看起来完全正常、分数也照常返回 ——
     这类"参数接对了但没被用上"正是本项目反复踩的病。

⚠️ 四条纪律（沿用本目录既有约定）：
  1. 退回后的文件**必须仍然语法正确**。
  2. 匹配要锚定 `[失败]` 前缀 —— 通过行的中文文案里本身就有"阈值""噪声"这些字。
  3. 整篇读写一律 `read_bytes()` / `write_bytes()`（Windows 的 `write_text()` 会翻 CRLF）。
  4. `try/finally` 兜底复原 —— 失败路径最可能被中断，绝不能把旧形态留在工作区。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERIFIER = [sys.executable, "-m", "legal_acps.check_local"]

FACT_STORE = ROOT / "legal_acps" / "fact_store.py"
#: 🔴 **锚点必须跟着实现搬家**（2026-10-07）：打分被抽到 `text_match.GramIndex`
#: （事实库与裁定经验库共用同一份，**不许写两处**）。所以「权重折扣」那条用例的
#: 目标文件从 `fact_store.py` 改成 `text_match.py` —— 不改的话，它会永远
#: 「前置不成立」而**看起来像被测代码没问题**。
TEXT_MATCH = ROOT / "legal_acps" / "text_match.py"
#: 受理接线（2026-10-07）：事实库与裁判上下文之间**唯一的桥**在
#: `agents.arbitrate_claim` 的返回值上 —— 桥断了，官方通报在库里、裁判看不见。
AGENTS = ROOT / "legal_acps" / "agents.py"

CASES = [
    {
        "name": "检索阈值不得形同虚设（不相关查询必须返回空）",
        "why": "`MIN_SCORE` 一旦失效，不相关查询会返回 5 条噪声；"
               "噪声被当成权威依据写进法律结论就是假证据。",
        "file": FACT_STORE,
        # ⚠️ 2026-10-07 锚点跟着实现搬家：库容 78→128（扩容 + 同 URL 去重）后
        #    重跑分布 ⇒ 阈值 10.0 → 18.0。**不改这里它就永远「前置不成立」，
        #    看起来却像被测代码没问题**（check_meta_residue 会红，见 #135 注）。
        "new": "MIN_SCORE = 18.0    #",
        "old": "MIN_SCORE = 0.0    #",
        "needle": "[失败] 事实库闸门：不相关查询返回了",
    },
    {
        "name": "打分必须用已打过折扣的权重（不许绕回原始 idf）",
        "why": "`for g, _ in qg` 再用 `self._idf[g]` 取值，会把虚字折扣与 df=1 折扣"
               "全部丢掉 —— 参数接对了却没被用上，而函数看起来完全正常。",
        "file": TEXT_MATCH,
        "new": "            score += w * (f * (K1 + 1.0)) / (f + norm)",
        "old": "            score += self._idf[g] * (f * (K1 + 1.0)) / (f + norm)",
        # 🔴 2026-10-07 needle 换过一次：库扩容 78→128 后，旧 needle
        #    「不相关查询返回了」的载体（火星查询靠 df=1 的「建筑/筑 」）
        #    被**稀释**——新库里这两个 gram 的 df≥2，退回前后分数一模一样
        #    （实测 13.55 = 13.55）⇒ 用例结构性空转。折扣伤害的可观测落点
        #    挪到了**纯转述零实体查询**（退回后 top1 9.58→21.60 过线）⇒
        #    ⑦-5h 为此加了第三条探针，needle 锚到它。
        #    ⚠️ 「判据的输入集合必须覆盖病灶的载体」——库一变，载体就变。
        "needle": "[失败] 事实库闸门：纯转述零实体查询返回了",
    },
    {
        # 🔴 退回形态 = 把去重分支短路成恒假：`if False and url in by_url:`
        #    分支体仍语法正确、永不执行 ⇒ 重复条目全进库。
        #    真库里现有 12 个重复 URL（两轮 curated 快照）⇒ ⑦-5h (6) 的
        #    真库断言必然响。⚠️ old 不是 new 的子串（`if False and` 挡在前面）。
        "name": "同 URL 只留一条（重抓快照不许整文翻倍）",
        "why": "重跑 `--curated` 会整批重写同一批 URL；load 不去重 ⇒ 同文两份"
               "挤占检索 top-N、虚高库容，而「重抓过」与「库里真有两条」"
               "长得一模一样（2026-10-07 实测 12 个 URL 翻倍）。",
        "file": FACT_STORE,
        "new": "                if url in by_url:",
        "old": "                if False and url in by_url:",
        "needle": "[失败] 事实库同 URL 重复",
    },
    {
        # 🔴 退回形态 = 把受理工具里的检索 import 换成 pass（函数体其余不动，
        #    语法保持正确）⇒ official_materials 恒为空 ⇒ ⑦-5h (7) 必响。
        #    ⚠️ 第一版锚点（签名+docstring 首行 → 签名+return 行）退回后
        #    docstring 剩余正文变裸文本、**语法错误**——退回形态必须仍
        #    语法正确，否则什么都证明不了（2026-10-07 实测）。
        "name": "受理工具必须查案底（事实库接进裁判上下文）",
        "why": "事实库此前没有任何生产链路消费它——判据全绿、检索命中 84 分，"
               "真机裁判照样判「无法判定」并明写「不存在官方通报」"
               "（2026-10-07 实测）。接线断了 ⇒ 官方通报在库里、裁判看不见。",
        "file": AGENTS,
        "new": "        from .fact_store import FactStore",
        "old": "        pass  # from .fact_store import FactStore",
        "needle": "[失败] 受理没带回案底",
    },
]


def _b(value: object) -> bytes:
    return str(value).encode("utf-8")


def syntax_ok(path: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)], capture_output=True
    ).returncode == 0


def run_verifier() -> str:
    r = subprocess.run(
        VERIFIER, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return (r.stdout or "") + (r.stderr or "")


def digests() -> dict[str, str]:
    return {str(c["file"]): hashlib.md5(c["file"].read_bytes()).hexdigest() for c in CASES}


def main() -> int:
    originals = {str(c["file"]): c["file"].read_bytes() for c in CASES}

    for c in CASES:
        raw = originals[str(c["file"])]
        if raw.count(_b(c["new"])) != 1 or _b(c["old"]) in raw:
            print(f"[步骤0] ❌ 前置不成立：{c['name']} 不是预期的'新形态'")
            return 1
    before = digests()
    print(f"[步骤0] ✅ {len(CASES)} 处均为新形态")

    try:
        for i, c in enumerate(CASES, 1):
            path: Path = c["file"]
            name, needle = str(c["name"]), str(c["needle"])
            original = originals[str(path)]

            broken = original.replace(_b(c["new"]), _b(c["old"]))
            if broken.count(_b(c["old"])) != 1 or _b(c["new"]) in broken:
                print(f"[{i}.1] ❌ {name}：替换未按预期发生")
                return 1
            path.write_bytes(broken)
            if not syntax_ok(path):
                print(f"[{i}.1] ❌ {name}：退回后文件语法错误 —— 本次元验证证明不了任何事")
                return 1
            print(f"[{i}.1] ✅ {name}：已退回旧形态（{path.name}），且文件仍语法正确")

            out = run_verifier()
            hit = needle in out
            print(f"[{i}.2] {'✅' if hit else '❌'} {name}：check_local 报出该失败 = {hit}")
            for line in out.splitlines():
                if needle in line:
                    print(f"          {line.strip()}")
            if not hit:
                print(f"[{i}.2] ⚠️ {name} 的断言没响 —— 它是空转的（或匹配词写错了）")
                return 1

            path.write_bytes(original)
            if not syntax_ok(path):
                print(f"[{i}.3] ❌ {name}：复原后语法错误")
                return 1
            print(f"[{i}.3] ✅ {name}：已复原")

        if digests() != before:
            print("[步骤9] ❌ 复原后摘要与基线不一致")
            return 1
        print(f"[步骤9] ✅ {len(CASES)} 条用例全部咬中，且源码已完整复原")
        return 0
    finally:
        for p, data in originals.items():
            Path(p).write_bytes(data)


if __name__ == "__main__":
    raise SystemExit(main())
