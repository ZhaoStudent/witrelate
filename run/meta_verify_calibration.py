"""元验证：判定校准台账的判据**真的有牙**（2026-10-07，第 ④ 步；同日值域补「不实」后扩 3 条）。

被测的判据都在 `legal_acps/check_local.py` 的 ⑦-5k；被测实现是
`tools/calibration.py` 与 `legal_acps/contracts.py`。七条退回用例各对应
一种「仪表盘安静给出误导性数字」或「契约悄悄回退」的旧形态 —— 每一种都
跑得通、不报错、报表看起来还挺像样：

  A. **缺口检测被绕过**（`allowed` 把基准取值并进去 ⇒ 缺口永远为空）——
     一致率低就全背在模型头上，而真相是那些样本**结构性不可能判对**。
     （2026-10-07 实测：值域补「不实」之前，13 条可校准基准里 11 条基准是
     「不实」而值域里没有它。当日已补 —— 但缺口检测本身继续在线：值域与
     基准是两份独立维护的事实，谁也不保证下次标注不出新缺口。）
  B. **弃权并进判错**（`abstain += 1` 退回 `wrong += 1`）—— 弃权（证据
     通路的病）与判错（判断逻辑的病）合成一栏，一致率变成说不清的数字。
  C. **弃权混进校准曲线**（分桶把「无法判定」也算进预测）—— 弃权没有
     「预测」，进曲线只会把校准曲线搅浑。
  D. **汇总数自算**（render 不再引用 `stats()` 的 total/fallacies）——
     同一个数存在两处，口径漂移只是时间问题（本项目实证过的病；
     判据用 monkeypatch `stats()` 证明台账**跟着变**，退回后它就不变了）。
  E. **值域回退：契约删掉 `FALSE = "不实"`**（2026-10-07 补值域后最危险的
     静默回退）—— 官方证伪的案子重新变回结构性判不对，而没有任何东西变红。
  F. **值域回退：capability description 退回三选一**—— Enum 有「不实」、
     进指纹的 description 枚举没有 ⇒ 模型不知道能选它（一处实现两处枚举
     的漂移，且这处在指纹里、改它须重审，最容易「懒得动」而漂掉）。
  G. **值域回退：instructions 退回三选一**—— 提示词里的枚举漂移
     （不进指纹、重启即生效，恰恰因为没有流程卡着而最容易被忘掉）。

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

CAL = ROOT / "tools" / "calibration.py"
CONTRACTS = ROOT / "legal_acps" / "contracts.py"
AGENTS = ROOT / "legal_acps" / "agents.py"

CASES = [
    {
        "name": "值域缺口检测不许被绕过（缺口为空 ⇒ 结构性偏差背在模型头上）",
        "why": "把基准取值并进 allowed ⇒ domain_gap 永远返回空 ⇒ 报表上一致率 0% "
               "会被读成「模型全错」，而真相是那些样本在当前契约下**不可能判对**。"
               "（2026-10-07 实测：真实基准 13 条里 11 条是「不实」，值域里没有它。）",
        "file": CAL,
        # ⚠️ old 不许包含 new 作为子串（old = new + " | …" 的写法会让替换后
        #    `new in broken` 恒真 ⇒ 「替换未按预期发生」，2026-10-07 实证）。
        #    这里用**写法不同**的等价旧形态表达同一个病（把基准取值并进白名单）。
        "new": "    allowed = {v.value for v in RulingVerdict}",
        "old": "    allowed = set(c.ground_truth for c in store if c.graded) | set(v.value for v in RulingVerdict)",
        "needle": "[失败] 值域缺口没报出来",
    },
    {
        "name": "弃权与判错不许合并（两种病因不同的病，药方完全不同）",
        "why": "弃权的病因在证据通路（运行时不出网、拿不到权威信源），判错的病因"
               "在判断逻辑（如命题偷换）。合成一栏 ⇒ 一致率就是个说不清的数字。",
        "file": CAL,
        "new": "            abstain += 1",
        "old": "            wrong += 1  # 弃权并入判错",
        "needle": "[失败] 弃权与判错没分开",
    },
    {
        "name": "弃权不许混进置信度分桶（弃权没有「预测」）",
        "why": "校准曲线的原料是「预测 vs 实际」的配对；「无法判定」没有预测，"
               "进桶只会把曲线搅浑 —— 桶里的判对率从此数的是两种东西的混合。",
        "file": CAL,
        "new": "                   if c.graded and c.verdict != RulingVerdict.UNDECIDABLE.value]",
        "old": "                   if c.graded]",
        "needle": "[失败] 置信度分桶把弃权",
    },
    {
        "name": "台账汇总数不许自算（必须引用 stats()，monkeypatch 后要跟着变）",
        "why": "同一个数存在两处（calibration 自算 + stats()），口径迟早漂移 —— "
               "而漂移的那一半恰好是没人盯着的那一半。判据把 stats() 换成假值，"
               "台账必须跟着变；退回自算形态后它就不变了 ⇒ 判据红。",
        "file": CAL,
        "new": "    st = store.stats()",
        "old": "    st = {**store.stats(), \"total\": len(store), \"fallacies\": {}}",
        "needle": "[失败] 台账的汇总数没有引用 stats()",
    },
    {
        "name": "值域不许回退：契约删掉 FALSE=不实（官方证伪案重新结构性判不对）",
        "why": "2026-10-07 补的值域（未决 #16），最危险的后续是**悄悄回退** —— "
               "没有任何东西会变红，只有校准数字默默变差。判据 ⑦-5k(0) 钉死："
               "FactRuling 必须接受 verdict=不实。",
        # ⚠️ old 是**替身注释**（不能是空串：空串 in 任何文本恒真 ⇒ 步骤0 必挂；
        #    也不能含 new 的整行子串 ⇒ 会触发互不为子串判据）。
        # ⚠️ 锚点用**两行连锚**：contracts.py 里 `ClaimVerdict`（harm 链旧枚举）
        #    也有 `FALSE = "不实"` 单行 ⇒ 只锚一行不唯一（2026-10-07 实测撞车）。
        "file": CONTRACTS,
        "new": '    TRUE = "确有其事"\n    FALSE = "不实"',
        "old": '    TRUE = "确有其事"\n    # 值域回退形态：不实已从枚举中移除',
        "needle": "[失败] 值域缺「不实」",
    },
    {
        "name": "值域不许回退：capability description 退回三选一（模型不知道能选不实）",
        "why": "Enum 有「不实」而进指纹的 description 枚举没有 ⇒ 模型从工具描述里"
               "根本不知道有这个选项。这处在 ACS 指纹里、改它须重审 —— 正因流程重，"
               "最容易「懒得动」而漂掉，所以必须有人（判据）盯着。",
        "file": AGENTS,
        "new": '"提交最终裁定书并落盘到 output/。verdict 取「确有其事 / 不实 / 断章取义 / 无法判定」；"',
        "old": '"提交最终裁定书并落盘到 output/。verdict 取「确有其事 / 断章取义 / 无法判定」；"',
        "needle": "[失败] 值域缺「不实」",
    },
    {
        "name": "值域不许回退：instructions 退回三选一（提示词与契约漂移）",
        "why": "提示词里的枚举不含「不实」⇒ 模型被提示词拦住选不了它，契约改了"
               "等于没改。instructions 不进指纹、重启即生效 —— 恰恰因为没有流程"
               "卡着而最容易被忘掉。",
        "file": AGENTS,
        "new": "- verdict 四选一：`确有其事` / `不实` / `断章取义` / `无法判定`。",
        "old": "- verdict 三选一：`确有其事` / `断章取义` / `无法判定`。",
        "needle": "[失败] 值域缺「不实」",
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
