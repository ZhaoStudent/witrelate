"""元验证：线索复研判（⑦-11）的三处要害锚点，退回旧形态必须让 check_local
对应断言变红（或证明「摘掉登记 = 整节静默消失」）。

被测的判据（`legal_acps/check_local.py` ⑦-11「线索复研判」节）与被修的缺陷
（2026-10-13 该节首次进 check_local 主链路时逐个暴露）：

  A. pending_retries 不许「先滤已消费再取最新」——那会让被消费的最新条
     （e2）消失、**过期的旧条（e1）复活**成新的「最新」：同一事件带着旧
     ``ruling_source`` 再次变成合格活，重查死循环（烧真模型预算）。
     修法 = 最新在**全量行**里取，「lead 是否待处理」看最新条是否已消费。
  B. agents.py 的 B 出路（UNDECIDABLE 自动入队）不许缺 RulingVerdict 导入
     —— ⑦-10(4) 是第一个走到这行的调用方；缺导入之前全线绿灯，全靠
     这条路径**从来没被跑过**（「装了」≠「会响」的又一实证）。
  C. ⑦-11 在 main() 的登记不许摘 —— 摘掉的失败方式不是变红，是**整节
     静默消失**（函数在、没登记、check_local 照样全绿）。这一条用
     「absent」形态证：needle 不出现 + 退出码 0 + 整体绿，恰恰证明
     「定义了 ≠ 会跑」。

⚠️ 与 fast_lane 版框架的两点差异（有意的）：
  1. fail 形态的判据是 **needle 在输出里 且 退出码非 0** —— 只看 needle
     会放过「_fail 只 nonlocal bad」那类失败传递断掉的坑：[失败] 照打、
     退出码照 0（2026-10-13 实证）。锚 A 同时钉住这条。
  2. absent 形态（锚 C）：判据是 needle 不在输出 + 退出码 0 + 整体绿 ——
     证明摘掉登记后**没有任何东西会红**，这正是要防的静默。

⚠️ 退回形态的 `old` 与 `new` 互不为子串；退回后文件仍语法正确
（A 用先滤后取的等位块、B 用删一行导入名、C 用保留注释删登记行）。
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
        "name": "复研判不许旧 epoch 复活（先滤后取 = 死循环）",
        "why": "消费最新条后旧条复活成新的「最新」，同一事件带着旧 "
               "ruling_source 再次变成合格活 —— 每次库长肥查一次的语义"
               "被打破，重查变成烧真模型预算的死循环。本条同时钉住"
               "「失败必须传出去」：退出码非 0 是判据的一半（_fail 只 "
               "nonlocal bad 的话，[失败] 照打、退出码照 0）。",
        "file": ROOT / "legal_acps" / "lead_recon.py",
        "new": (
            "    consumed = {r.get(\"retry_id\") for r in _read(retry_consumed_path(wit_dir))}\n"
            "    newest: dict[str, dict] = {}\n"
            "    for r in _read(retries_path(wit_dir)):\n"
            "        lid = str(r.get(\"lead_id\") or \"\")\n"
            "        if lid:\n"
            "            newest[lid] = r  # 文件序即时间序：后见覆盖先见（含已消费行）\n"
            "    return [r for r in newest.values() if r.get(\"retry_id\") not in consumed]"
        ),
        "old": (
            "    consumed = {r.get(\"retry_id\") for r in _read(retry_consumed_path(wit_dir))}\n"
            "    newest: dict[str, dict] = {}\n"
            "    pending = [\n"
            "        r for r in _read(retries_path(wit_dir))\n"
            "        if r.get(\"retry_id\") not in consumed\n"
            "    ]\n"
            "    for r in pending:\n"
            "        lid = str(r.get(\"lead_id\") or \"\")\n"
            "        if lid:\n"
            "            newest[lid] = r\n"
            "    return list(newest.values())"
        ),
        "needle": "消费后仍有待处理",
        "mode": "fail",
    },
    {
        "name": "B 出路不许缺 RulingVerdict 导入（缺 = 全线绿灯靠没跑过）",
        "why": "issue_ruling 的 UNDECIDABLE 自动入队引用了未导入的名字；"
               "在 ⑦-10(4) 第一个走到这行的探针出现之前，这条路径"
               "从未被执行过 —— NameError 藏在「装了但没响」的位置，"
               "真机上每一次弃权裁定都会在入队时炸掉且挡住落盘。",
        "file": ROOT / "legal_acps" / "agents.py",
        "new": (
            "    LegalAdvice,\n"
            "    RulingVerdict,\n"
            ")"
        ),
        "old": (
            "    LegalAdvice,\n"
            ")"
        ),
        "needle": "NameError: name 'RulingVerdict' is not defined",
        "mode": "fail",
    },
    {
        "name": "⑦-11 不许只在定义不在登记（摘登记 = 整节静默消失）",
        "why": "2026-10-13 实证：_check_lead_recon 函数在、没登记进 main()、"
               "check_local 全绿 —— 「定义了」与「会跑」是两件事。摘掉"
               "登记行后没有任何东西会红，这条负对照证明的恰恰是这个"
               "静默本身（absent 形态）。",
        "file": ROOT / "legal_acps" / "check_local.py",
        "new": (
            "        # ⑦-11 线索复研判（2026-10-13）：UNDECIDABLE 的 B 出路有了消费者；\n"
            "        #    排在 ⑦-10 之后——issue_ruling 的入队接线先在 ⑦-10(4) 被证明，\n"
            "        #    这节证明队列另一侧（闸门/指纹/消费闭环）。\n"
            "        (\"线索复研判\", _check_lead_recon(True)),"
        ),
        "old": (
            "        # ⑦-11 线索复研判（2026-10-13）：UNDECIDABLE 的 B 出路有了消费者；\n"
            "        #    排在 ⑦-10 之后——issue_ruling 的入队接线先在 ⑦-10(4) 被证明，\n"
            "        #    这节证明队列另一侧（闸门/指纹/消费闭环）。\n"
            "        # (\"线索复研判\", _check_lead_recon(True)),  # 退回形态：只在定义不在登记"
        ),
        "needle": "线索复研判",
        "mode": "absent",
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
            mode = str(c.get("mode", "fail"))
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
            if mode == "fail":
                # 🔴 needle 与退出码各是一半判据：只看 needle 会放过
                #    「失败传递断掉」的坑（[失败] 照打、退出码照 0）。
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
            else:  # absent：摘掉的失败方式是静默，判据 = 不出现 + 全绿
                hit = (needle not in out) and rc == 0 and ("本地层面全部就绪" in out)
                print(f"[{i}.2] {'✅' if hit else '❌'} {name}："
                      f"摘掉后整节静默消失（needle 不在输出={needle not in out}、"
                      f"rc={rc}、整体绿={'本地层面全部就绪' in out}）= {hit}")
                if not hit:
                    print(f"[{i}.2] ⚠️ 摘掉登记仍有 {needle!r} 出现或整体不绿"
                          " —— absent 形态的负对照没立住")
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
