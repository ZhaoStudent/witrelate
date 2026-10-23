"""临时工具：取几个**当场派生**的数字（不留中间态的快照值）。跑完即删。

⚠️ 为什么不用"对每个文件起一个 `py_compile` 子进程"的老写法：本机实测，
那种写法现在会被沙箱 **SIGTERM** 掉（两次都是空输出 + `Signal: SIGTERM`）。
改用**进程内 `compile()`**，并且**按字节**喂进去 —— 这样编码声明（encoding cookie）
的处理与 `py_compile` 完全一致，抓到的语法错误**同一套解析器**，只是不起子进程。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, ".")

files = sorted(
    p
    for p in Path(".").rglob("*.py")
    if "__pycache__" not in str(p) and ".venv" not in str(p)
)
bad: list[str] = []
for p in files:
    try:
        compile(p.read_bytes(), str(p), "exec")
    except SyntaxError as exc:
        bad.append(f"{p}: {exc}")
except_msg = "（无）"

print("全仓语法检查：文件数 =", len(files), " 失败 =", len(bad))
for b in bad:
    print("   ", b)

print("e2e_audio_live 行数 =", len(Path("run/e2e_audio_live.py").read_text(encoding="utf-8").splitlines()))

from tools.file_fingerprint import collect  # noqa: E402

print("同步清单文件数 =", len(collect(Path("."))))
print(except_msg)
