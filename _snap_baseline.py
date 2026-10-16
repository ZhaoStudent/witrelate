"""临时工具：把「会被元验证改写的文件」在**仓外**存一份字节级快照（用完即删）。

为什么必须存到仓外：元验证的退回形态会把源文件改坏，而工具超时的 SIGTERM
**不执行 `finally`** ⇒ 文件会留在退回形态，而**下一次运行会把退回形态当成基线**。
复原必须拿**外部副本**核 md5（本项目纪律，2026-10-05 / 2026-10-06 各踩一次）。

⚠️ 本文件**不能** `import run.meta_verify_fixes`：那支脚本的 import 有副作用
（会真的改写 tools/*.py），它现在会直接抛 `RuntimeError` 拒绝被 import。
所以它改的那两个文件在下面**显式列出** —— 这是本**临时**工具的唯一硬编码，
删掉本文件即消失；仓库里的残留核对由 `check_meta_residue` 负责（它按 CASES 派生）。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
ROOT = Path(".").resolve()

from run import meta_verify_run_ledger as M  # noqa: E402

SNAP = Path(sys.argv[1])
SNAP.mkdir(parents=True, exist_ok=True)

files = sorted(
    {c["file"] for c in M.CASES}
    | {
        ROOT / "tools" / "metrics.py",   # meta_verify_fixes 用例 1
        ROOT / "tools" / "status.py",    # meta_verify_fixes 用例 2/3/4
    }
)
out: dict[str, dict] = {}
for p in files:
    b = p.read_bytes()
    rel = str(p.relative_to(ROOT)).replace("\\", "/")
    out[rel] = {"md5": hashlib.md5(b).hexdigest(), "bytes": len(b)}
    (SNAP / rel.replace("/", "__")).write_bytes(b)
(SNAP / "baseline.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("外部基线已存 ->", SNAP)
print(json.dumps(out, ensure_ascii=False, indent=2))
