"""元验证：同步清单的资产覆盖断言，退回旧形态后必须报失败。

背景（2026-10-01 实测，**同一个坑掉了两次**）：

  ① `tools/file_fingerprint.PATTERNS` 原先只收 `legal_acps/**/*.py`，于是
     81 个文件的同步清单里 **一个 html 都没有** ——
     `legal_acps/experience/web/index.html` 从来没被同步过。本地能跑
     （文件就在磁盘上），部署到服务器后 `GET /` 会因找不到它直接失败。
  ② 当天稍后又加了 `run/verify_frontend_flow.mjs`（用 node 真跑前端脚本的行为验证），
     而 `("run", "*.py")` 收不到 `.mjs` —— **新加的文件再次不在清单里**。

  病根不是"忘了改"，是**清单按扩展名白名单收文件**：换一个扩展名就再漏一次。
  所以这里对两条 PATTERN 各验一次，证明对应断言真的能逮到。

（写成文件而不是 heredoc：Bash 工具会把字节串里的 `\\n` 转义吃掉，
  详见 run/meta_verify_fixes.py 顶部说明。）
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "tools" / "file_fingerprint.py"

#: (用例名, 要删掉的 PATTERN 行, 应当因此报失败的那条断言里的关键字)
CASES: tuple[tuple[str, bytes, str], ...] = (
    ("html 资产（第一次漏项）", b'    ("legal_acps", "**/*.html"),\n', "前端 index.html 在同步清单内"),
    ("mjs 脚本资产（第二次漏项）", b'    ("run", "*.mjs"),\n', "前端行为验证脚本在同步清单内"),
    # 第三次（2026-10-05，#27）：仓库里第一个 `.timer`（`acps-case-round.timer`）
    # 落在 `("deploy", "**/*.service")` 的白名单外，同一次还查出 `acps.logrotate`
    # 也一直不在清单里。退回形态 = 把那条目录规则整个删掉。
    ("deploy 目录资产（第三次漏项：.timer / .logrotate）", b'    ("deploy", "**/*"),\n',
     "deploy/ 与 docs/*.md 下的每个文件都在同步清单内"),
    # 第四次（同一次复查）：`docs/` 顶层的材料不在清单里，而
    # `acps-case-round.service` 的注释**点名**了 `docs/上线操作手册.md`。
    ("docs 顶层材料（第四次漏项）", b'    ("docs", "*.md"),\n',
     "deploy/ 与 docs/*.md 下的每个文件都在同步清单内"),
)


def run(mod: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", mod],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def syntax_ok(path: Path) -> bool:
    return (
        subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            cwd=str(ROOT),
            capture_output=True,
        ).returncode
        == 0
    )


def digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def main() -> int:
    original = TARGET.read_bytes()
    before = digest(TARGET)
    ok = True

    print("=== 元验证结果 ===")
    for name, anchor, needle in CASES:
        count = original.count(anchor)
        if count != 1:
            print(f"  [错误] 「{name}」锚点出现 {count} 次（应为 1）—— PATTERNS 改过，请同步本元验证")
            return 2

        caught: list[str] = []
        try:
            TARGET.write_bytes(original.replace(anchor, b""))
            if not syntax_ok(TARGET):
                print(f"  [错误] 「{name}」退回后 file_fingerprint.py 语法不正确 —— 元验证无效")
                ok = False
                continue
            res = run("tools.verify_experience")
            caught = [
                line.strip() for line in res.stdout.splitlines() if "[失败]" in line and needle in line
            ]
        finally:
            TARGET.write_bytes(original)

        if digest(TARGET) != before or not syntax_ok(TARGET):
            print(f"  [错误] 「{name}」未能复原 file_fingerprint.py")
            return 2

        print(f"  [{'有效' if caught else '❌ 空转'}] {name}：退回旧清单后报出 {len(caught)} 条失败")
        for line in caught:
            print(f"          {line}")
        ok = ok and bool(caught)

    # 复原后必须整体转绿 —— 只看那两条会漏掉"改坏了别的断言"。
    after = run("tools.verify_experience")
    print(
        f"  [{'通过' if after.returncode == 0 else '❌ 失败'}] 复原后 verify_experience 整体通过："
        f"{after.returncode == 0}"
    )
    ok = ok and after.returncode == 0

    print()
    print("总判定：", "全部有效" if ok else "❌ 存在空转探针")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
