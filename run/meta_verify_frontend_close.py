# -*- coding: utf-8 -*-
"""元验证：前端「揭示即收尾」这条断言真的能抓住缺陷。

做法（四步）：
  1. 备份 legal_acps/experience/web/index.html，把 `await closeSession();`
     删掉 —— 这就是**修复前的旧形态**（前端从不调用 /close）；
  2. 确认改完的文件仍是结构完整的 HTML（还有 <script> 和 </html>），
     否则证明的只是"文件坏了"，不是"断言能抓住语义缺陷"；
  3. 跑 tools.verify_experience，必须看到「前端：揭示处理里 await closeSession()」报失败；
  4. 复原，确认与原文逐字节一致，且 verify_experience 重新全绿。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "legal_acps" / "experience" / "web" / "index.html"
PY = ROOT / ".venv" / "Scripts" / "python.exe"

#: 旧形态：揭示之后什么都不做（没有收尾调用）
NEEDLE = "    await closeSession();\n"


def digest(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def run_verify() -> str:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [str(PY), "-m", "tools.verify_experience"],
        cwd=str(ROOT),
        capture_output=True,
        env=env,
    )
    out = (proc.stdout or b"") + (proc.stderr or b"")
    return out.decode("utf-8", errors="replace")


def main() -> int:
    if not TARGET.exists():
        print(f"[错误] 找不到 {TARGET}")
        return 2

    original = TARGET.read_bytes()
    before = digest(original)
    text = original.decode("utf-8")

    if NEEDLE not in text:
        print(f"[错误] 找不到锚点 {NEEDLE!r} —— 前端结构变了，元验证需要同步更新")
        return 2

    buggy = text.replace(NEEDLE, "", 1)
    TARGET.write_bytes(buggy.encode("utf-8"))

    try:
        # ① 退回后的文件必须仍是结构完整的 HTML
        b = TARGET.read_text(encoding="utf-8")
        if not ("<script>" in b and "</html>" in b):
            print("[失败] 退回形态的 HTML 结构就是坏的 —— 证明不了断言有效")
            return 1
        print("[步骤1] 已删掉 `await closeSession();`，HTML 仍结构完整 ✅")

        # ② verify_experience 必须逮到
        out = run_verify()
        broke = "[失败] 前端：揭示处理里 await closeSession()" in out
        print(f"[步骤2] 退回旧形态后 verify_experience 报该断言失败：{'✅ 是' if broke else '❌ 否'}")
        if not broke:
            for line in out.splitlines():
                if "closeSession" in line:
                    print("        " + line.strip())
            print("[失败] 断言空转：前端不再收尾却没有报失败")
            return 1
    finally:
        TARGET.write_bytes(original)

    after = digest(TARGET.read_bytes())
    print(f"[步骤3] 已复原：md5 {'一致 ✅' if after == before else '不一致 ❌'}")

    out2 = run_verify()
    # 复原后要求**整体全绿** —— 只看那一条会漏掉"改坏了别的断言"。
    green = "[失败]" not in out2
    print(f"[步骤4] 复原后 verify_experience 整体通过：{'✅ 是' if green else '❌ 否'}")
    if not green:
        for line in out2.splitlines():
            if "[失败]" in line:
                print("        " + line.strip())

    if after == before and green:
        print("\n元验证通过：该断言在缺陷存在时报失败、缺陷消除后通过 —— 不是空转的。")
        return 0
    print("\n元验证未通过。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
