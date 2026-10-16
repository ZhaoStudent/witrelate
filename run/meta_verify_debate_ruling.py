"""元验证：辩论裁定（阶段 2）的三处要害锚点 + 硬判据实现，退回旧形态必须让
check_local ⑦-7 变红。

被测的判据（`legal_acps/check_local.py` 的 ⑦-7「辩论裁定」节）与被修的缺陷形态：

  A. 辩词全文进输入 —— `build_judge_input` 必须回放**全部**轮次。
     退回形态 = 只回放最后一轮（`rounds[-1:]`）：裁判裁决的是这场辩论，
     漏掉的轮对它等于没发生过。
  B. 坏台账必炸 —— `load_debate` 对空场（无 round）必须 SystemExit。
     退回形态 = `if False:`（炸的分支永远不走）：空场交裁判 = 凭空裁定，
     失败形态伪装成「跑通但裁定书很空」。
  C. 「不要 discover/delegate」框架在场 —— 不登录直跑的命门。
     退回形态 = 框架句换成普通陈述：裁判按标准流程找辩手（不登录必失败，
     步数烧光、裁定书只剩终止 Hook 兜底）。步骤 0 实测：带框架
     iterations=3 干净走完「受理→裁定」。
  D. `new_rulings` 集合差 —— 「唯一硬判据」的实现本体。
     退回形态 = `return list(after)`（不看差集）：没有新增也报非空 ⇒
     「status=completed 不算数」这条被抽空。

⚠️ 退回形态的 `old` 与 `new` 互不为子串（D 的第一版想法
`return sorted(after)` 是 new 的前缀子串 ⇒ [步骤0] 必然不成立，已换）。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERIFIER = [sys.executable, "-m", "legal_acps.check_local"]

CASES = [
    {
        "name": "辩词必须全部回放（不许只回放最后一轮）",
        "why": "裁判裁决的是这场辩论；漏掉的轮对它等于没发生过。",
        "file": ROOT / "legal_acps" / "debate_ruling.py",
        "new": "    for r in rounds:",
        "old": "    for r in rounds[-1:]:",
        "needle": "[失败] 辩论裁定输入：缺",
    },
    {
        "name": "空台账必须当场炸（不许静默交裁判）",
        "why": "没有轮次的空场交裁判 = 凭空裁定；失败形态会伪装成"
               "「跑通但裁定书很空」，比当场炸危险得多。",
        "file": ROOT / "legal_acps" / "debate_ruling.py",
        "new": "    if not rounds:",
        "old": "    if False:",
        "needle": "[失败] 辩论裁定台账",
    },
    {
        "name": "「辩手离线、不要派发」框架必须在场",
        "why": "裁判 instructions 是「受理→discover→派发→裁定」的严格序列；"
               "没有这句它会找辩手 —— 不登录必失败，步数烧光、"
               "裁定书只剩终止 Hook 兜底。",
        "file": ROOT / "legal_acps" / "debate_ruling.py",
        "new": ("        \"这场辩论已经完成，两位辩手均已离线（**不要 acps_discover、\"\n"
                "        \"不要 acps_delegate** —— 派发不会成功，纯烧步数）。请：\\n\""),
        "old": ("        \"这场辩论已经完成，两位辩手均已离线。\"\n"
                "        \"请：\\n\""),
        "needle": "[失败] 辩论裁定输入：缺",
    },
    {
        "name": "new_rulings 必须是集合差（没新增就是没交付）",
        "why": "「唯一硬判据 = output/ 新增 ruling-*.json」的实现本体；"
               "不看差集 ⇒ 没有交付也报非空，判据被抽空。",
        "file": ROOT / "legal_acps" / "debate_ruling.py",
        "new": "    return sorted(after - before)",
        "old": "    return list(after)",
        "needle": "[失败] 辩论裁定判据",
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
            print("[步骤4] ❌ 复原后 md5 与原文不一致")
            return 1
        out2 = run_verifier()
        green = ("[失败]" not in out2) and ("本地层面全部就绪" in out2)
        print(f"[步骤4] {'✅' if green else '❌'} 复原后整体转绿 = {green}")
        return 0 if green else 1
    finally:
        for c in CASES:
            path: Path = c["file"]
            if path.read_bytes() != originals[str(path)]:
                path.write_bytes(originals[str(path)])
                print(f"[finally] 已强制复原 {path.name}")


if __name__ == "__main__":
    raise SystemExit(main())
