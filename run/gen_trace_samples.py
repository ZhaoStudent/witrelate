"""重跑三份真机溯源样本（**在服务器上执行** —— 本地没有真机任务库）。

## 为什么要有这个脚本

`docs/aicomp/真机溯源样本-2026-10-03/` 是**答辩交付物**：截图存证、技术方案里的表格，
都从它来。而它全部是**工具产物**。把生成命令散在 README 里必然后患无穷 ——
生成器一改，README 就与产物对不上，而这看起来只是"文档旧了"（本项目那条
"同一事实散在多处 ⇒ 迟早漂移"）。

所以：**生成清单只此一处**（`SAMPLES` × `VARIANTS`），README 只指路、不抄命令。

## 三个样本各管一件事（对应样本目录 README）

| # | verdict | 覆盖 |
|---|---|---|
| 1 | `verdict-20261002T022916Z.json` | 旗舰：唯一一次**三级链路全通**，3 条硬溯源；**无 sidecar** ⇒ 走降级认领 |
| 2 | `verdict-20261003T101123Z.json` | 最新真机：**有 sidecar** ⇒ 这条路上不存在猜测 |
| 3 | `verdict-20261003T080645Z.json` | 对照：同一形状的链路**把 sidecar 挪走**再算 ⇒ 证明降级路径不瞎猜 |

## 产物（每份 5 个）

`--json` > `--html` > `--md` > 文本 是 `tools.trace.main()` 里的优先级；
`--evidence` 只对 html / 文本生效。见 `VARIANTS`。

## 安全

· **只读**任务库；唯一的写操作是样本 3 的 sidecar 挪动，由 `try/finally` 保证挪回来。
· 产物换行一律纯 LF（`tools/trace.py` 用 `newline="\n"` 写）。
· 任何一份生成失败就整轮退出非零 —— **不产半成品**。

用法（服务器上、仓库根目录）：
    ./.venv/bin/python -m run.gen_trace_samples --out docs/aicomp/真机溯源样本-2026-10-03
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: (样本名, verdict 相对路径, 是否临时把 sidecar 挪走)
SAMPLES: tuple[tuple[str, str, bool], ...] = (
    ("1-旗舰样本·5跳3层", "output/verdict-20261002T022916Z.json", False),
    ("2-最新真机·有sidecar", "output/verdict-20261003T101123Z.json", False),
    ("3-对照·无sidecar降级", "output/verdict-20261003T080645Z.json", True),
)

#: (产物后缀, 给 `tools.trace` 的附加参数)
VARIANTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("单页.html", ("--html",)),
    ("单页（含每跳原文）.html", ("--html", "--evidence")),
    ("技术方案.md", ("--md",)),
    ("机读.json", ("--json",)),
    ("终端树（含证据抽屉）.txt", ("--evidence",)),
)

#: 挪走 sidecar 时用的后缀。**故意不成 `.json` 结尾**：`_verdict_files` 的 glob
#: 是 `verdict-*.json`，`.json` 结尾的临时名会被它当成一份 verdict 捞进来。
HIDDEN_SUFFIX = ".hidden-by-gen-samples"


def sidecar_of(verdict: Path) -> Path:
    return verdict.with_name(verdict.name[: -len(".json")] + ".meta.json")


def run_one(verdict: Path, extra: tuple[str, ...], out: Path) -> None:
    cmd = [sys.executable, "-m", "tools.trace",
           "--verdict", str(verdict), *extra, "--out", str(out)]
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out_text = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise SystemExit(
            f"生成失败（exit={r.returncode}）：{' '.join(cmd)}\n{out_text}"
        )
    raw = out.read_bytes()
    if raw.count(b"\r\n"):
        raise SystemExit(f"产物不是纯 LF：{out}（CRLF×{raw.count(b'\r\n')}）")


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m run.gen_trace_samples")
    ap.add_argument("--out", required=True,
                    help="产物目录（仓库相对路径或绝对路径）")
    args = ap.parse_args()

    if not (ROOT / ".wit").is_dir():
        raise SystemExit(
            f"没有真机任务库（{ROOT / '.wit'} 不存在）—— 本脚本要在**服务器**上跑"
        )
    outdir = Path(args.out)
    if not outdir.is_absolute():
        outdir = ROOT / outdir
    if not outdir.is_dir():
        raise SystemExit(f"产物目录不存在：{outdir}")

    made: list[Path] = []
    for name, verdict_rel, hide_sidecar in SAMPLES:
        verdict = ROOT / verdict_rel
        if not verdict.is_file():
            raise SystemExit(f"样本 `{name}` 的 verdict 不在：{verdict_rel}")
        sc = sidecar_of(verdict)
        hidden: Path | None = None
        try:
            if hide_sidecar and sc.is_file():
                hidden = sc.with_name(sc.name + HIDDEN_SUFFIX)
                sc.rename(hidden)
                print(f"[{name}] 已把 sidecar 挪走（{sc.name}）")
            elif hide_sidecar:
                print(f"[{name}] 本就没有 sidecar —— 已经是降级路径")
            for suffix, extra in VARIANTS:
                out = outdir / f"{name}·{suffix}"
                run_one(verdict, extra, out)
                made.append(out)
                print(f"[{name}] ✅ {out.name}（{out.stat().st_size} 字节）")
        finally:
            if hidden is not None and hidden.is_file():
                hidden.rename(sc)
                print(f"[{name}] 已把 sidecar 挪回来")

    print()
    print(f"共生成 {len(made)} 个产物 → {outdir}")
    print("⚠️ 产物是工具输出，**不要手工改** —— 改了「复算指引」给出的命令就对不上了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
