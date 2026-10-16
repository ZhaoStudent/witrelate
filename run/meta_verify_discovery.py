# -*- coding: utf-8 -*-
"""元验证：check_local 的「污染核对判据」断言真的能抓住缺陷。

做法（四步）：
  1. 备份 tools/check_discovery_pollution.py，把 verdict_of 的判据**退回旧形态**
     （只要 leaked 为空就给 ✅，不看查询是否失败、是否真的取到结果）；
  2. 确认退回后的文件**仍是语法正确的 Python** —— 否则证明的只是"文件坏掉会报错"，
     而不是"断言能抓住语义错误"；
  3. 跑 check_local，必须看到「污染核对判据」报 [失败]（判据退化被逮到）；
  4. 复原，确认与原文**逐字节一致**、语法正确、且 check_local 重新全绿。

结论：只有当 3 看到失败、4 看到复原成功，才说明这条断言不是空转的。
"""

from __future__ import annotations

import hashlib
import os
import py_compile
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tools" / "check_discovery_pollution.py"
PY = ROOT / ".venv" / "Scripts" / "python.exe"

#: 旧形态（有缺陷）：leaked 为空就打印 ✅，完全不看失败/空结果。
BUGGY = '''def verdict_of(
    seen: Mapping[str, str],
    failed_queries: Sequence[str],
    total_queries: int,
) -> tuple[int, list[str]]:
    """旧形态（有缺陷）：leaked 为空就给 ✅。"""
    leaked = sorted(a for a in seen if a in RETIRED_AICS)
    lines: list[str] = []
    if leaked:
        lines.append("仍有污染")
        for a in leaked:
            lines.append(a)
        return 1, lines
    lines.append("结论：发现结果无 demo 泄漏 ✅（清理生效）")
    return 0, lines


'''


def digest(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def run_check_local() -> str:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [str(PY), "-m", "legal_acps.check_local"],
        cwd=str(ROOT),
        capture_output=True,
        env=env,
    )
    out = (proc.stdout or b"") + (proc.stderr or b"")
    return out.decode("utf-8", errors="replace")


def syntax_ok(path: Path) -> bool:
    try:
        py_compile.compile(str(path), doraise=True)
        return True
    except py_compile.PyCompileError:
        return False


def main() -> int:
    if not TARGET.exists():
        print(f"[错误] 找不到 {TARGET}")
        return 2

    original = TARGET.read_bytes()
    before = digest(original)

    text = original.decode("utf-8")
    start = text.index("def verdict_of(")
    end = text.index("async def main(")
    buggy = text[:start] + BUGGY + text[end:]
    TARGET.write_bytes(buggy.encode("utf-8"))

    try:
        # ① 退回后的文件必须仍语法正确
        if not syntax_ok(TARGET):
            print("[失败] 退回形态语法就是坏的 —— 这证明不了断言有效（只是文件坏了）")
            return 1
        print("[步骤1] 已退回旧形态，且文件仍语法正确 ✅")

        # ② check_local 必须逮到判据退化
        out = run_check_local()
        broke = "[失败] 污染核对判据" in out
        print(f"[步骤2] 退回旧形态后 check_local 报判据失败：{'✅ 是' if broke else '❌ 否'}")
        if not broke:
            for line in out.splitlines():
                if "污染核对" in line:
                    print("        " + line.strip())
            print("[失败] 断言空转：判据退回旧形态却没有报失败")
            return 1
    finally:
        # ③ 无论成败都要复原
        TARGET.write_bytes(original)

    after = digest(TARGET.read_bytes())
    restored = after == before and syntax_ok(TARGET)
    print(f"[步骤3] 已复原：md5 {'一致 ✅' if after == before else '不一致 ❌'}、"
          f"语法{'正确 ✅' if syntax_ok(TARGET) else '坏 ❌'}")

    out2 = run_check_local()
    green = "[失败] 污染核对判据" not in out2
    print(f"[步骤4] 复原后 check_local 判据断言恢复通过：{'✅ 是' if green else '❌ 否'}")

    if restored and green:
        print("\n元验证通过：该断言在缺陷存在时报失败、缺陷消除后通过 —— 不是空转的。")
        return 0
    print("\n元验证未通过：复原或复跑环节有问题。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
