"""元验证：体验应用的两处修复，退回旧形态后断言必须报失败。

（写成文件而不是 heredoc：Bash 工具会把字节串里的 `\\n` 转义吃掉，
  详见 run/meta_verify_fixes.py 顶部说明。）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

NL = chr(10).encode()
ROOT = Path(__file__).resolve().parent.parent


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


def failed(res: subprocess.CompletedProcess, needles: list[str]) -> list[str]:
    return [
        l.strip()
        for l in res.stdout.splitlines()
        if "[失败]" in l and any(n in l for n in needles)
    ]


report: list[tuple[str, bool, list[str]]] = []

# ---------- ① server.py：把空白输入的 400 改回 500（= 修复前的 HTTP 语义） ----------
#
#   为什么不用"让 ValueError 逃逸"来退回旧形态：TestClient 默认会把服务端异常
#   重新抛出，verify_experience 会直接**崩掉**、后面的断言根本没跑到 ——
#   那样证明的是"崩了"，不是"断言能抓住"。改成返回 500，语义与修复前一致
#   （修复前就是未处理异常 → FastAPI 500），但断言能被真正检验。
p1 = ROOT / "legal_acps/experience/server.py"
orig1 = p1.read_bytes()
old1 = b"                raise HTTPException(status_code=400, detail=str(exc)) from exc"
new1 = b"                raise HTTPException(status_code=500, detail=str(exc)) from exc"
n1 = orig1.count(old1)
assert n1 >= 2, f"应当有 create_session 与 reply 两处，实际 {n1}"
try:
    p1.write_bytes(orig1.replace(old1, new1))
    assert syntax_ok(p1), "退回后的 server.py 语法不正确 —— 元验证无效！"
    bad1 = failed(run("tools.verify_experience"), ["空白"])
    report.append((f"server.py · 空白输入返回 500（{n1} 处）", bool(bad1), bad1))
finally:
    p1.write_bytes(orig1)

# ---------- ② session.py：把 to_dict 的白名单改成无条件带上 traps ----------
p2 = ROOT / "legal_acps/experience/session.py"
orig2 = p2.read_bytes()
old2 = b"            if include_hidden:"
assert orig2.count(old2) == 1, f"锚点不唯一：{orig2.count(old2)}"
try:
    p2.write_bytes(orig2.replace(old2, b"            if True:  # deliberately broken"))
    assert syntax_ok(p2), "退回后的 session.py 语法不正确 —— 元验证无效！"
    bad2 = failed(run("tools.verify_experience"), ["traps"])
    report.append(("session.py · 公开视图泄漏 traps", bool(bad2), bad2))
finally:
    p2.write_bytes(orig2)

# ---------- 复原与总结 ----------
assert p1.read_bytes() == orig1, "server.py 未复原！"
assert p2.read_bytes() == orig2, "session.py 未复原！"
print("=== 复原校验 ===")
print("  server.py / session.py 均已还原")
print()
print("=== 元验证结果 ===")
all_ok = True
for label, caught, lines in report:
    mark = "有效" if caught else "❌ 空转"
    if not caught:
        all_ok = False
    print(f"  [{mark}] {label}：报出 {len(lines)} 条失败")
    for l in lines:
        print(f"          {l}")
print()
print("总判定：", "全部有效" if all_ok else "❌ 存在空转探针")
sys.exit(0 if all_ok else 1)
