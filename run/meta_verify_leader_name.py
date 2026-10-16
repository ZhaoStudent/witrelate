"""元验证：主控提示词里"去发现谁"的名字必须与平台公布的名字**逐字一致**（#94）。

被测的两条判据（都在 `legal_acps/check_local.py` 的 2 节「Agent 编译与工具接线」）：

  A. 提示词里出现的名字 == 刚编译出的 ACS 草稿里的 `name`
  B. 提到该名字的**同一行里不许再写"几个字"**

被修的缺陷（2026-10-04，真实存在过）：
  主控提示词的三个模态分支把「证据鉴证 Leader」**手写了三遍**，其中一处还写着
  "`name` 必须是**这五个字**" —— 而这个名字是
  **4 个汉字 + 1 个空格 + 6 个字母**，任何读法下都不是五个字。
  模型照这个数去拼名字（或认定名字里不该有空格）⇒ **发现不到**该 Agent，
  而失败表现是"没找到"，看上去像平台索引问题，**不像提示词写错**
  —— 错误被归因到别处，正是最该根除的一类。

做法：把提示词那一行**退回旧形态**（手写名字 + 手写字数），要求 `check_local` 立刻报失败。

⚠️ 本模块的 harness 与 `meta_verify_run_ledger.py`（以及目录里既有 7 个模块）**有意重复**：
   沿用本目录既有约定，改动面最小。把 8 个模块的 harness 合并成一个共用实现
   是另一件事 —— 记在这里，不假装不存在。

⚠️ 四条纪律（前三条是本项目踩过的坑，第四条是本模块新增的）：
  1. 退回后的文件**必须仍然语法正确**。
  2. 匹配要锚定 `[失败]` 前缀 —— 通过行的中文文案里本身就含"名字""字数"这些字。
  3. 整篇读写一律 `read_bytes()` / `write_bytes()`（Windows 的 `write_text()` 会翻成 CRLF）。
  4. `try/finally` 兜底复原 —— 失败路径最可能被中断，绝不能把旧形态留在工作区里。
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
        "name": "提示词里的目标名必须逐字一致、且不许手写字数",
        "why": "手写的名字会与平台公布名漂移；手写的字数会被模型当成硬约束，"
               "而「证据鉴证 Leader」含空格与字母，根本没有「几个字」这个说法。",
        "file": ROOT / "legal_acps" / "agents.py",
        # 新形态：名字由常量插进来，且**不提字数**。
        "new": "  **「{EVIDENCE_LEADER_NAME}」**的 Agent（`name` 必须**逐字一致**，连中间那个空格\n",
        # 退回形态：手写名字 + 手写"这五个字"（原缺陷原文）。
        "old": "  **「证据鉴证 Leader」**的 Agent（`name` 必须是这五个字\n",
        "needle": "[失败] 提示词在提到该名字的同一行里还写着字数",
    },
    {
        # 🔴 2026-10-07 新增：煽动/危害停用指令（用户口径：最终输出不出现
        #    相关文字）。退回形态 = 把停用指令行改回"三项判定"的旧行 ——
        #    语法仍正确（都是 instructions 文本），check_local 1b 节必红。
        #    ⚠️ 锚点行内不含反引号/反斜杠（教训：锚点含特殊字符易转义坏）。
        "name": "煽动/危害停用指令必须在场（不许悄悄复活两项判定）",
        "why": "两项判定的专家已软废弃，用户口径 = 最终输出不出现煽动性/危害性"
               "文字。提示词停用段被删 ⇒ 模型继续输出这两类判定文字。",
        "file": ROOT / "legal_acps" / "agents.py",
        "new": "（conclusion / consistency_note / action_advice）\n**不得出现**「煽动 / 煽动性 / 危害 / 危害性 / 危害指数」等任何相关表述 ——",
        "old": "（conclusion / consistency_note / action_advice）\n继续按三项判定输出煽动性与危害性结论 ——",
        "needle": "[失败] 主控停用断言",
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
