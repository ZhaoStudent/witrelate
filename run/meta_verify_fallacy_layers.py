"""元验证：话术两层的四条新断言，退回旧形态后必须报失败。

覆盖三个最容易"看起来绿、其实空转"的点：

  ① 诱导方提示词里的话术清单，必须**由枚举派生**（`{FALLACY_LAYERS_BRIEF}`）。
     退回成手写清单后，"提示词里出现每一类话术"必须失败 ——
     否则就会出现"枚举加了、提示词没加、模型永远不用新那一类"，
     而这种事在离线**完全看不出来**（提示词是自由文本，没有 schema 可校）。
  ② 每类话术都必须**归层**。退回成"新加一类但不归层"后，
     "每类话术都归了层"必须失败 —— 不归层的话术会在前端按层分组时静默消失。
  ③ 前端必须**消费服务端给的** `trap_groups`，而不是自己按 kind 判断。
     退回成读 `r.traps` 后，"前端：分层用服务端给的 trap_groups"必须失败。

（写成文件而不是 heredoc：Bash 工具会把字节串里的 `\\n` 转义吃掉，
  详见 run/meta_verify_fixes.py 顶部说明。另：本文件里所有锚点都用
  `str.encode()` 而不是 `b"中文"` —— Python 的 bytes 字面量不允许非 ASCII。）
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ROLES = ROOT / "legal_acps/experience/roles.py"
CONTRACTS = ROOT / "legal_acps/contracts.py"
HTML = ROOT / "legal_acps/experience/web/index.html"


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


def failed(needle: str) -> list[str]:
    return [
        line.strip()
        for line in run("tools.verify_experience").stdout.splitlines()
        if "[失败]" in line and needle in line
    ]


report: list[tuple[str, bool, list[str]]] = []

# ---------- ① 提示词清单退回手写 ----------
old = "{FALLACY_LAYERS_BRIEF}"
new = "① `时间错配` ② `断章取义` ③ `张冠李戴` ④ `数字夸张`"
original = ROLES.read_bytes()
assert original.count(old.encode()) == 1, "锚点不唯一"
before = digest(ROLES)
try:
    ROLES.write_bytes(original.replace(old.encode(), new.encode()))
    assert syntax_ok(ROLES), "退回后的 roles.py 语法不正确 —— 元验证无效！"
    bad = failed("诱导方提示词里出现每一类话术")
    report.append(("roles.py · 提示词清单退回手写四类", bool(bad), bad))
finally:
    ROLES.write_bytes(original)
assert digest(ROLES) == before, "roles.py 未复原！"

# ---------- ② 加一类话术但不归层 ----------
original = CONTRACTS.read_bytes()
before = digest(CONTRACTS)
old = '    BURDEN_SHIFT = "举证倒置"'
new = old + '\n    ORPHAN = "未归层的话术"'
assert original.count(old.encode()) == 1, "锚点不唯一"
try:
    CONTRACTS.write_bytes(original.replace(old.encode(), new.encode()))
    assert syntax_ok(CONTRACTS), "退回后的 contracts.py 语法不正确 —— 元验证无效！"
    bad = failed("每类话术都归了层")
    report.append(("contracts.py · 新增一类但不归层", bool(bad), bad))
finally:
    CONTRACTS.write_bytes(original)
assert digest(CONTRACTS) == before, "contracts.py 未复原！"

# ---------- ③ 前端改为自己判 kind ----------
original = HTML.read_bytes()
before = digest(HTML)
old, new = "Array.isArray(r.trap_groups)", "Array.isArray(r.traps)"
assert original.count(old.encode()) == 1, "锚点不唯一"
try:
    HTML.write_bytes(original.replace(old.encode(), new.encode()))
    bad = failed("前端：分层用服务端给的 trap_groups")
    report.append(("index.html · 前端改为自己判 kind", bool(bad), bad))
finally:
    HTML.write_bytes(original)
assert digest(HTML) == before, "index.html 未复原！"

# ---------- 复原后必须整体转绿 ----------
after = run("tools.verify_experience")
print("=== 元验证结果 ===")
all_ok = True
for label, caught, lines in report:
    mark = "有效" if caught else "❌ 空转"
    if not caught:
        all_ok = False
    print(f"  [{mark}] {label}：报出 {len(lines)} 条失败")
    for line in lines:
        print(f"          {line}")
print(
    f"  [{'通过' if after.returncode == 0 else '❌ 失败'}] "
    f"复原后 verify_experience 整体通过：{after.returncode == 0}"
)
print()
if not all_ok or after.returncode != 0:
    print("总判定：❌ 存在空转探针")
    sys.exit(1)
print("总判定：全部有效")
