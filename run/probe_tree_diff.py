"""`tools/tree_diff.py` 的**自身探针** —— 证明"基线不可用"这件事**判得出来**，且不误伤干净运行。

跑法（在 witrelate 目录下）：
    ./.venv/Scripts/python.exe -m run.probe_tree_diff      # exit 0 = 全绿

════════════════════════════════════════════════════════════════════════
它要防的是什么
════════════════════════════════════════════════════════════════════════

2026-10-03 深夜：`tree_diff` 报出"改动 240 个文件（基线只有 199 个）"，那份清单里
**混着别的轮次的改动**，而它看起来只是"改动多了一点" —— 判据本身变成了误导，
失守的原因正是**基线过期却没人发现**。

修完之后必须回答一个问题：**"基线不可用"这个判断，我凭什么信它？**
下面每一条都要求一个**会响的正面证据**，其中 D 是**非空洞性**证据（见下）。

════════════════════════════════════════════════════════════════════════
两条纪律
════════════════════════════════════════════════════════════════════════

1. ⚠️ 全程用 `TREE_SNAPSHOT` 指到**临时文件**：**不许动真基线**。
   把真基线弄脏，"基线过期"就又要退回靠人眼发现 —— 等于把刚修好的洞重新捅开。

2. ⚠️ **断言必须证明"它真的会响"，而"响了"要能和"永远都响"区分开**。
   所以 D 专门构造一种**看起来该响、实际不该响**的输入（改动落在预期清单**之内**）：
   它必须是 exit **1**，不是 3。少了 D，`3` 完全可能只是"只要有改动就报"，
   那 §`--expect-cases` 那套声明就是个摆设 —— 而它在外表上与"装好了"一模一样。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 用**本仓 venv 的解释器**跑子进程（与手工敲命令时的口径一致）。
#: ⚠️ 两种布局都要认：Windows 是 `.venv/Scripts/python.exe`，Linux 是 `.venv/bin/python`。
#:    写死 Windows 那条的后果是：同步到服务器后一跑就 `FileNotFoundError`。
#:    那是个**响亮**的失败（不是静默），但仍然等于"这个探针在服务器上根本不存在"——
#:    而本仓的判据一律要求在**两端**都能跑（`verify_trace` / `file_fingerprint` 都是如此）。
_VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PY = str(_VENV_PY) if _VENV_PY.is_file() else sys.executable

#: 探针自己往仓库里放的那一个文件。**故意放在 `run/` 下**：`walk()` 要看得见它，
#: 否则"新增/改动/删除"三条路径都测不到。`finally` 里删干净。
PROBE_REL = "run/_tree_diff_probe.txt"

#: 真·元验证模块（预期清单 = 它 CASES 里那 21 个文件）。
REAL_MOD = "run.meta_verify_trace"

fails: list[tuple] = []


def expect(label: str, got: int, want: int, extra: str = "") -> None:
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}：exit={got}（期望 {want}）{extra}")
    if not ok:
        fails.append((label, got, want))


def report(label: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}{extra}")
    if not ok:
        fails.append((label, extra, ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="treediff_probe_"))
    snap_a = tmp / "snap_a.json"          # 主基线
    snap_gone = tmp / "snap_absent.json"  # 故意不存在（测"没有基线"）
    probe = ROOT / PROBE_REL

    env = dict(os.environ, TREE_SNAPSHOT=str(snap_a))

    def run(*args: str, env_override: dict | None = None) -> tuple[int, str]:
        r = subprocess.run(
            [PY, "-m", "tools.tree_diff", *args], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env_override if env_override is not None else env,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def line_of(out: str, needle: str) -> str:
        hits = [l for l in out.splitlines() if needle in l]
        return hits[0].strip() if hits else f"（没找到含 {needle!r} 的行）"

    try:
        probe.unlink(missing_ok=True)

        # ── A. 基线自身：save → diff 必须干净 ────────────────────────────────
        print("A. save → diff 必须干净")
        code, out = run("save")
        expect("A1 save", code, 0, "　" + out.strip().splitlines()[-1] if out.strip() else "")
        code, out = run("diff")
        expect("A2 diff", code, 0)
        print("     " + line_of(out, "基线时间"))

        # ── B/C. 不在预期内的**新增 / 改动** ⇒ 3 且点名 ──────────────────────
        print(f"B. 新增一个**不在预期内**的文件（{PROBE_REL}）⇒ exit 3 并点名")
        probe.write_bytes(b"probe\n")
        code, out = run("diff", "--expect-cases", REAL_MOD)
        expect("B1 diff --expect-cases", code, 3)
        report("B2 点名了那个文件", PROBE_REL in out)
        print("     " + line_of(out, "不能用来判"))

        print("C. 改它的内容（走 changed 分支，不是 added）⇒ 仍 exit 3")
        probe.write_bytes(b"probe changed\n")
        code, _ = run("diff", "--expect-cases", REAL_MOD)
        expect("C diff（changed）", code, 3)

        # ── D. ★非空洞性正面控制★：同一个改动，换个"预期清单"就不该报 ───────
        #    构造一个**假的**元验证模块，它声明的 CASES 里**正好**是 PROBE_REL。
        #    于是"改了 PROBE_REL"这件事**落在预期内** ⇒ 必须 exit 1，不是 3。
        #    D 与 C 是**同一个改动、只换声明**：C=3 / D=1 才证明 `--expect-cases`
        #    真的在**分辨**，而不是"有改动就报 3"。
        print("D. 改动**落在预期清单内** ⇒ 必须是 exit 1（不是 3）—— 证明 3 不是"
              "「只要有改动就响」")
        fake = tmp / "fakemv.py"
        fake.write_text(
            "from pathlib import Path\n"
            "# 故意用**正斜杠字符串**：Windows 上 Path 的 str() 会给反斜杠，\n"
            "# 而 walk() 吐出来的是 posix 路径，对不齐就永远匹配不上。\n"
            f'CASES = ({{"file": {PROBE_REL!r}, "name": "probe", "new": "x", "old": "y"}},)\n',
            encoding="utf-8",
        )
        env_d = dict(env, PYTHONPATH=str(tmp))
        code, out = run("diff", "--expect-cases", "fakemv", env_override=env_d)
        expect("D diff（在预期内）", code, 1)
        print("     " + line_of(out, "逐条核对"))

        # ── E. **删除**分支：B/C 走的是 added/changed，这条路径原先没人走 ────
        print("E. 先在文件**存在时**记基线、再删掉它 ⇒ exit 3 并点名（走 removed 分支）")
        code, _ = run("save")
        expect("E1 save（含 probe）", code, 0)
        probe.unlink()
        code, out = run("diff", "--expect-cases", REAL_MOD)
        expect("E2 diff（removed）", code, 3)
        report("E3 点名了那个文件", f"删除 = ['{PROBE_REL}']" in out)

        # ── F. 老格式基线（裸 {路径:md5}）必须照样能读，且标明时间来源 ────────
        print("F. 老格式基线（裸 {路径:md5}，无 _saved_at）必须照样能读")
        flat = tmp / "flat.json"
        flat.write_text(json.dumps({"x.py": "0" * 32}, sort_keys=True), encoding="utf-8")
        code, out = run("diff", env_override=dict(os.environ, TREE_SNAPSHOT=str(flat)))
        report("F1 标明时间来源是 mtime", "mtime" in out and "老格式" in out)
        print("     " + line_of(out, "基线时间"))

        # ── G/H. 前提不成立时必须 exit 2（**不许**与"有差异"的 1 撞车）────────
        print("G. 模块名写错 ⇒ exit 2（拒绝给结论），而不是默默当「没有预期清单」")
        code, out = run("diff", "--expect-cases", "run.meta_verify_not_exist")
        expect("G 模块名错", code, 2)
        report("G2 给出了拒绝的理由", "拒绝给出结论" in out)

        print("H. 没有基线 ⇒ exit 2（本文件顶部承诺 2；原先代码给的是 1，与「有差异」撞车）")
        code, out = run("diff", env_override=dict(os.environ, TREE_SNAPSHOT=str(snap_gone)))
        expect("H 没有基线", code, 2)
        report("H2 提示去 save", "先跑" in out)

        # ── I. 快照必须带 _saved_at，否则"基线过期"又要靠人眼 ────────────────
        print("I. save 的快照里必须带 _saved_at")
        payload = json.loads(snap_a.read_text(encoding="utf-8"))
        report("I1 _saved_at 存在且 files 非空",
               bool(isinstance(payload, dict) and payload.get("_saved_at") and payload.get("files")),
               f"　= {payload.get('_saved_at')!r}（{len(payload.get('files') or {})} 个文件）")

    finally:
        probe.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILS（{len(fails)} 处）：")
        for item in fails:
            print(f"  · {item}")
        return 1
    print("全部通过。⚠️ 仍然要记得：探针全绿只说明**判据会响**，"
          "不说明本轮真的没残留 —— 那件事只能拿真基线跑一次 `diff` 才有结论。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
