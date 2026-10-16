# -*- coding: utf-8 -*-
"""元验证：前端「调用方法与显隐闭合」这三条新断言真的能抓住缺陷。

背景（2026-10-01 实测修掉的两个缺陷，**都是由这轮前端断言覆盖不到的空白造成的**）：

  · 缺陷 A（致命）：`api()` 用 `body === undefined ? 'GET' : 'POST'` **推断**方法，
    于是 `api('.../reveal')`（不带 body）被静默发成 GET；服务端那个端点只注册 POST，
    实测 `GET → 405` / `POST → 200`。**用户点「揭晓话术」只看到"揭示失败 (405)"，
    整局的核心交付物走不到。**
  · 缺陷 B（严重）：`renderReveal()` 藏了 `#composer`，`resetToIntro()` 从不恢复它，
    于是「再体验一次」之后**输入框永远不出现**，用户没法替自己辩白。
    它当时被缺陷 A 掩盖着 —— 揭示从没成功过，也就没人发现第二局是坏的。

做法（照 `meta_verify_frontend_close.py` 的同一套纪律）：对每个缺陷，
把 `index.html` 临时退回**它修复前的真实形态**，确认：
  1. 退回后的文件仍是结构完整的 HTML（否则只证明了"文件坏了"）；
  2. `tools.verify_experience` 里**指名的那条断言**报失败；
  3. 复原后与原文逐字节一致，且整体重新全绿。

⚠️ 退回的是**真实旧形态**，不是凭空造的字符串 —— 这样证明的才是
"这条断言能抓住那个真实缺陷"，而不是"断言对某段自造文本有反应"。
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

#: 每个用例＝一次真实的「退回旧形态」。old 必须在文件里唯一。
CASES: tuple[dict[str, bytes | str], ...] = (
    {
        # ⚠️ 缺陷 A 的形态变了：现在揭示调用**自带 body**（要提交当事人标的轮次），
        #    所以它的"旧形态"不再是"完全不带 body"，而是**带了 body 却漏写 method**。
        #    在新签名 `api(path, {method = 'GET', body})` 下，漏写 method 会退化成
        #    **GET** —— 服务端那个端点只注册 POST，405 一模一样地回来。
        #    要守的性质没变：**每一处调用都必须显式声明方法**，不能靠"有没有 body"推断。
        "name": "缺陷 A：揭示调用漏写 method（有 body 也照样被发成 GET → 405）",
        "old": (
            b"api('/api/session/' + sid + '/reveal', {\n"
            b"      method: 'POST',\n"
            b"      body: {suspected: [...suspects]}\n"
            b"    })"
        ),
        "new": (
            b"api('/api/session/' + sid + '/reveal', {\n"
            b"      body: {suspected: [...suspects]}\n"
            b"    })"
        ),
        "needle": "[失败] 前端：每一处 api() 调用都显式声明 HTTP 方法",
    },
    {
        "name": "缺陷 B：composer 只隐藏、没有恢复路径（第二局输入框消失）",
        "old": b"  $('composer').classList.remove('hide');\n",
        "new": b"",
        "needle": "[失败] 前端：每个被隐藏的元素都有恢复路径",
    },
)


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
    ok = True

    for index, case in enumerate(CASES, start=1):
        old, new = case["old"], case["new"]  # type: ignore[assignment]
        name, needle = case["name"], case["needle"]  # type: ignore[assignment]
        print()
        print(f"── 用例 {index}：{name} " + "─" * 10)

        old_s = old.decode("utf-8")
        if text.count(old_s) != 1:
            print(f"[错误] 锚点不唯一（出现 {text.count(old_s)} 次），元验证需同步更新")
            return 2

        TARGET.write_bytes(text.replace(old_s, new.decode("utf-8"), 1).encode("utf-8"))
        try:
            body = TARGET.read_text(encoding="utf-8")
            if not ("<script>" in body and "</html>" in body):
                print("[失败] 退回形态的 HTML 结构就是坏的 —— 证明不了断言有效")
                ok = False
                continue
            print("[步骤1] 已退回旧形态，HTML 仍结构完整 ✅")

            out = run_verify()
            broke = needle in out
            print(f"[步骤2] verify_experience 报该断言失败：{'✅ 是' if broke else '❌ 否'}")
            if not broke:
                for line in out.splitlines():
                    if "[失败]" in line:
                        print("        " + line.strip())
                print("[失败] 断言空转：退回旧形态却没有报失败")
                ok = False
        finally:
            TARGET.write_bytes(original)

        if digest(TARGET.read_bytes()) != before:
            print("[失败] 未能逐字节复原！")
            return 2
        print("[步骤3] 已复原：md5 与原文一致 ✅")

    # 复原后要求**整体全绿** —— 只看那几条会漏掉"改坏了别的断言"。
    out2 = run_verify()
    green = "[失败]" not in out2
    print()
    print(f"── 复原后整体复跑：{'✅ 全绿' if green else '❌ 仍有失败'} " + "─" * 10)
    if not green:
        for line in out2.splitlines():
            if "[失败]" in line:
                print("        " + line.strip())
        ok = False

    print()
    if ok and green:
        print("元验证通过：两个真实缺陷各自都能被对应断言逮到 —— 不是空转的。")
        return 0
    print("元验证未通过。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
