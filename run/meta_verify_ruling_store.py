"""元验证：裁定经验库的判据**真的有牙**（2026-10-07）。

被测的判据都在 `legal_acps/check_local.py` 的 ⑦-5j：

  A. **循环论证闸门** —— 只给裁定、不给外部标注 ⇒ `graded` 必须为 0。
  B. **「没有可比的东西」不许变成一个数字** —— `graded == 0` 时 `accuracy`
     必须是 `None`，不是 `0.0`。
  C. **先例不许被当证据** —— 注入块必须 `trusted=False`，渲染必须带免责。
  D. **先例渲染必须带免责** —— 没有「不是本案的证据」这句，提示就变成了权威结论。
  E. **占位阈值必须有哨兵** —— 库涨到门槛而没实测重定 ⇒ 必须报 pending。

  ⚠️ **原定的「阈值形同虚设」用例被撤下**，因为实测证明它是**空转的**：
  在这份 12 条夹具上，`min_score=0.0` 与默认阈值返回**完全相同**的结果 ——
  真正把不相关挡在外面的是 `MIN_HITS`（≥2 个命中片段），**阈值一条都没多拦**。
  留着它只会让人误以为阈值在被验证（**一个不响的判据比没有判据更危险**：
  它给你"验过了"的错觉）。阈值这条改由 `MIN_SCORE_DERIVED` 哨兵（用例 E）盯着。

为什么必须元验证（而不是只跑一次 check_local 看它绿）：

  🔴 这个模块**最容易造出三种假象**，而每一种都**跑得通、看起来都正常**：

    1. 拿裁定自己当基准 ⇒ 一致率**恒等于 100%**，"校准"直接失去意义；
    2. `graded == 0` 时返回 `0.0` ⇒ 报表上写着"准确率 0%"，
       而真相是**一条可比的数据都没有**（"没有"与"全错"在数字上差得很远）；
    3. 先例块标成 `trusted=True` ⇒ 一条旧裁定变成了本案的权威依据，
       而它只是**提示**（这就是假证据的另一种写法）。

  ⇒ 绿不绿说明不了任何事，必须证明"退回旧形态时它会红"。

被修的缺陷：**全部是设计层面的自欺风险**，不是写错的 bug ——
所以每一条都对应一个"如果没人盯着，它会安静地一直错下去"的方向。

⚠️ 四条纪律（沿用本目录既有约定）：
  1. 退回后的文件**必须仍然语法正确**。
  2. 匹配要锚定 `[失败]` 前缀 —— 通过行的中文文案里本身就有这些字。
  3. 整篇读写一律 `read_bytes()` / `write_bytes()`（Windows 的 `write_text()` 会翻 CRLF）。
  4. `try/finally` 兜底复原 —— 失败路径最可能被中断，绝不能把旧形态留在工作区。
     🔴 **尤其别在前台跑**：工具超时 SIGTERM 会把源文件留在退回形态
     （2026-10-07 实证），而**下一次运行会把退回形态当成基线**。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERIFIER = [sys.executable, "-m", "legal_acps.check_local"]

RULING = ROOT / "legal_acps" / "ruling_store.py"
PRECEDENT = ROOT / "legal_acps" / "precedent.py"

CASES = [
    {
        "name": "基准结论不许从裁定自己推（循环论证）",
        "why": "拿裁定自己当基准 ⇒ 一致率恒等于 100%，「校准」这件事直接失去意义，"
               "而报表上什么都看不出来。",
        "file": RULING,
        "new": '                ground_truth=str(note.get("ground_truth") or "").strip(),',
        "old": '                ground_truth=str(obj.get("verdict") or "").strip(),',
        "needle": "[失败] 没有外部标注却有",
    },
    {
        "name": "一条基准都没有时 accuracy 必须是 None 而不是 0.0",
        "why": "`0.0` 会被读成「全错」，而真相是「没有可比的东西」—— "
               "「没有」与「全错」在数字上差得很远，在缺一个判断的代码里长得一样。",
        "file": RULING,
        "new": '            "accuracy": (right / len(graded)) if graded else None,',
        "old": '            "accuracy": (right / len(graded)) if graded else 0.0,',
        "needle": "[失败] 一条基准都没有，却给出了 accuracy=",
    },
    {
        "name": "先例块必须标成不可信（先例是提示，不是证据）",
        "why": "`trusted=True` 会把一条旧裁定变成本案的权威依据 —— "
               "那是假证据的另一种写法，而它看起来只是「参考了历史经验」。",
        "file": PRECEDENT,
        "new": "                trusted=False,",
        "old": "                trusted=True,",
        "needle": "[失败] 先例块被标成",
    },
    {
        "name": "先例渲染必须带免责（否则它会被当成本案的权威依据）",
        "why": "少了「不是本案的证据」这一句，先例就从**提示**变成了**权威结论**，"
               "而报告上只写着「注入了 N 条先例」—— 那是假证据的另一种写法。",
        "file": RULING,
        "new": '"【历史先例 · 仅供参照，**不是本案的证据**】",',
        "old": '"【历史先例 · 仅供参照】",',
        "needle": "[失败] 先例渲染缺",
    },
    {
        "name": "事件归一化不许塌缩（塌缩 ⇒ 整库被并成一组，判据必须察觉）",
        "why": "🔴 实测（2026-10-07，服务器）：**30 条裁定里只有 13 个不同事件**，"
               "其中一个事件被跑了 **25 轮**。而反向的病也同样要拦：归一化一旦塌缩"
               "（把所有事件并成一个 key），折叠会把**整个库并成一组** ⇒ "
               "total=1、repeats=14，检索彻底失效。"
               "⚠️ 退回这个形态时，先响的是「同事件折叠没做对」（折叠数对不上），"
               "distinct_events 那行反而不红 —— needle 如实指向会响的那行。",
        "file": RULING,
        "new": '    return NOISE.sub("", text or "")',
        "old": '    return "同一事件"  # 不去重，total 灌水',
        "needle": "[失败] 同事件折叠没做对",
    },
    {
        "name": "同事件折叠不许被绕过（重跑的旧裁定不许冒充独立经验）",
        "why": "实测（2026-10-07，服务器）：30 条裁定只有 13 个不同事件，一个事件被跑"
               "25 轮。不折叠 ⇒ total 虚报库容，且 df 被灌水 ⇒ 近重复项的每个 gram"
               "都超 df_cut ⇒ 自召回 16/30 失败（检索整个失效）。",
        "file": RULING,
        "new": "        cases, rep.duplicates = _fold_duplicates(cases)",
        "old": "        rep.duplicates = 0",
        "needle": "[失败] 同事件折叠没做对",
    },
    {
        "name": "占位阈值必须有哨兵（库涨到门槛而未实测 ⇒ 报 pending）",
        "why": "`MIN_SCORE` 当前是**占位值**，一个实测数据都没有。没有哨兵，"
               "它会被一直当真 —— 而「拍脑袋定的阈值」等于没有阈值。",
        # ⚠️ 锚点必须**整句一行**：源码那句若跨行写成 `… >= MIN_SCORE_DERIVE_AT`
        #    + 续行 `and not MIN_SCORE_DERIVED,`，替换后剩下悬空的 `and not …`
        #    ⇒ 退回后**语法错误** ⇒ 本次元验证证明不了任何事（2026-10-07 实证）。
        "file": RULING,
        "new": '"min_score_pending": len(self.cases) >= MIN_SCORE_DERIVE_AT and not MIN_SCORE_DERIVED,',
        "old": '"min_score_pending": False,',
        "needle": "[失败] 库已满门槛而",
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
