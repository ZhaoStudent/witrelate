"""元验证：系统性负控集（⑦-12）的三处要害锚点，退回旧形态必须让 check_local
对应断言变红。

被测的判据（`legal_acps/check_local.py` ⑦-12「系统性负控集」节）与被防的缺陷：

  A. 对齐筛子不许退回「无窗口」——正文出现定性词即算对齐（ALIGN_WINDOW
     存在之前的老病形态）。退回后近邻域负控（nc-policy-01 医保目录）会被
     错配到同域辟谣文上 ⇒ ⑦-12(3) 红。**这条证明负控集真的抓得住错配**：
     一个从未红过的负控集与没有负控集长得一模一样。
  B. 负控数据不许混入非负控文本——退回形态把一条负控文本换成含定性词的
     谣言文本 ⇒ ⑦-12(2) 红。闸门断言活着；「无害文本零阻力通过」与
     「什么文本都放行」必须分得开。
  C. 对齐筛子不许恒否——退回后 25 条负控照常全绿（这正是空转陷阱：
     对齐坏掉时负控**不会**报错），唯一的哨兵是 ⑦-12(4) 的正对照
     （从事实库现场派生的必对齐事件）。⇒ 正对照红。**这条证明
     「全 debate」与「对齐筛子坏了」分得开**。

⚠️ fail 形态判据 = needle 在输出 **且** 退出码非 0（与 meta_verify_lead_recon
   同款：只看 needle 会放过「失败传递断掉」的坑）。
⚠️ 退回形态的 `old` 与 `new` 互不为子串；退回后文件仍语法正确
   （A/C 用等位 return 块、B 用等位字符串行）。
⚠️ A 与 C 锚在**同一个函数体**上、`old` 不同——按残留核对的去重口径
   （文件, 新形态, 旧形态）各核一次，这正是想要的：防的旧形态不同。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERIFIER = [sys.executable, "-m", "legal_acps.check_local"]

#: official_confirms 的实现体（A/C 两锚共用的新形态文本）。
_ALIGN_NEW = (
    "    body = fact_body or \"\"\n"
    "    # 事件与窗口都先归一化（受理事件 vs 材料正文是同一件事的两种排版）；\n"
    "    # 筛子本体不做归一化（保 derive 原语义），norm 在这一层做。\n"
    "    event_norm = _norm_event(event or \"\")\n"
    "    for m in FALSE_PAT.finditer(body):\n"
    "        win = body[max(0, m.start() - ALIGN_WINDOW):m.end() + ALIGN_WINDOW]\n"
    "        if shares_substring(event_norm, _norm_event(win)):\n"
    "            return True\n"
    "    return False"
)

CASES = [
    {
        "name": "对齐筛子不许退回无窗口（见定性词即对齐）",
        "why": "无窗口对齐会把同域真事错配到别的谣言的证伪材料上——负控集"
               "存在的意义就是抓住这种错配；退回后 ⑦-12(3) 必须红，否则"
               "负控集从未咬合过（从未红过的负控 = 没有负控）。",
        "file": ROOT / "legal_acps" / "fast_lane.py",
        "new": _ALIGN_NEW,
        "old": (
            "    # 退回形态：无窗口对齐——正文出现定性词即算对齐"
            "（ALIGN_WINDOW 之前的老病）\n"
            "    return bool(FALSE_PAT.search(fact_body or \"\"))"
        ),
        "needle": "误触快车道",
        "mode": "fail",
    },
    {
        "name": "负控数据不许混入非负控文本（闸门断言不许空转）",
        "why": "闸门断言要求无害文本零阻力通过——它必须能被「数据里混进了"
               "含定性词的文本」触发，否则它挡不住负控集被悄悄塞进真谣言"
               "（那时负控集的口径就废了）。",
        "file": ROOT / "legal_acps" / "negative_controls.py",
        "new": (
            "        text=\"国家医保局公布新版药品目录，多款罕见病用药纳入报销范围\","
        ),
        "old": (
            "        text=\"国家医保局出新规了系谣言大家快别去办理了\","
        ),
        "needle": "未过受理闸门",
        "mode": "fail",
    },
    {
        "name": "对齐筛子不许恒否（正对照不许空转）",
        "why": "对齐恒否时 25 条负控照常全绿——「全 debate」与「对齐筛子坏"
               "了」在 (3) 里一模一样；唯一分得开它们的是 ⑦-12(4) 的正对照"
               "（事实库现场派生的必对齐事件必须触发 fast）。",
        "file": ROOT / "legal_acps" / "fast_lane.py",
        "new": _ALIGN_NEW,
        "old": (
            "    # 退回形态：对齐恒否——负控全绿恰是空转"
            "（正对照就是为这一刻存在的）\n"
            "    return False"
        ),
        "needle": "正对照派生失败",
        "mode": "fail",
    },
]


def _b(value: object) -> bytes:
    return str(value).encode("utf-8")


def syntax_ok(path: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)], capture_output=True
    ).returncode == 0


def run_verifier() -> tuple[str, int]:
    r = subprocess.run(
        VERIFIER, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return (r.stdout or "") + (r.stderr or ""), r.returncode


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

            out, rc = run_verifier()
            # 🔴 needle 与退出码各是一半判据（同 meta_verify_lead_recon）。
            hit = (needle in out) and rc != 0
            print(f"[{i}.2] {'✅' if hit else '❌'} {name}："
                  f"check_local 报出该失败且退出非 0 = {hit}"
                  f"（rc={rc}、needle={'在' if needle in out else '不在'}输出）")
            for line in out.splitlines():
                if needle in line:
                    print(f"          {line.strip()}")
                    break
            if not hit:
                print(f"[{i}.2] ⚠️ {name} 的断言没响（或失败没传出去）—— 它是空转的")
                return 1

            path.write_bytes(original)
            if not syntax_ok(path):
                print(f"[{i}.3] ❌ {name}：复原后语法错误")
                return 1
            print(f"[{i}.3] ✅ {name}：已复原")

        if digests() != before:
            print("[步骤4] ❌ 复原后 md5 与原文不一致")
            return 1
        out2, rc2 = run_verifier()
        green = ("[失败]" not in out2) and ("本地层面全部就绪" in out2) and rc2 == 0
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
