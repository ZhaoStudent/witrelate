"""元验证：把 tools/metrics.py 的「访问」口径各退回旧形态，断言必须报失败。

⚠️ 为什么必须做这一步
════════════════════
「断言通过了」只能证明**当前实现**符合预期，证明不了**断言本身有牙齿**。
本项目吃过这个亏：heredoc 把 `\\n` 吃掉转义 ⇒ 退回形态实际是语法错误 ⇒
元验证退化成"文件坏掉会报失败"，测的根本不是要测的东西。所以这里：

  1. 退回**一律字节级**（`read_bytes` / `write_bytes`）—— `Path.write_text` 在
     Windows 上会把 `\\n` 翻成 `\\r\\n`，一次往返就把纯 LF 的文件弄脏，
     而内容看起来完全一样。
  2. 退回后的文件**必须仍然语法正确**，否则元验证无效。
  3. 每条性质都要**真的**造出它要测的那个前提（例：「不区分方向」必须让
     「我调人」的条目真的落到「被调用」里，而不是随便改个数字）。
  4. 每条变换前先断言锚点**唯一** —— 不唯一的替换会静默改到别处。

被测性质（均为 2026-10-05 给 `tools/metrics.py` 新增/修正的口径）
────────────────────────────────────────────────────────────────
  AMP 访问日志（`_read_access`）：
    ① 分方向：只数 callee==self，自家外呼不算被访问
    ② 轮询折算：任务提交 = 被调用 − 固定大小轮询（轮询大小现场派生）
    ③ 三分类：调用方 AIC 缺失单列「未知」，不得算成外部
    ④ 坏行计数：解析失败单独计数，不得静默丢弃
  入站访问次数（`compute_role`）：
    ⑤ 终态分桶：failed / canceled 单列「非成功」，不得混进"成功受理"
       （实测：全项目唯一那条外部访问**就是** canceled 的）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

NL = chr(10).encode()
ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "tools" / "metrics.py"


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


def swap(src: bytes, old: bytes, new: bytes) -> bytes:
    """字节级替换，替换前断言锚点唯一。"""
    assert src.count(old) == 1, f"锚点不唯一（{src.count(old)} 处）：{old[:60]!r}"
    return src.replace(old, new)


def revert_direction(src: bytes) -> bytes:
    """① 不区分方向：把「我调人」的条目也算进「被调用」。"""
    return swap(
        src,
        b"                    elif me and caller == me and callee != me:"
        + NL
        + b"                        as_caller += 1"
        + NL
        + b"                        by_callee[callee] = by_callee.get(callee, 0) + 1",
        b"                    elif me and caller == me and callee != me:"
        + NL
        + b"                        as_callee += 1"
        + NL
        + b"                        by_caller[callee] = by_caller.get(callee, 0) + 1",
    )


def revert_poll(src: bytes) -> bytes:
    """② 折算任务级时不扣掉轮询报文。"""
    return swap(
        src,
        b"        submissions = as_callee - callee_sizes[poll_size]",
        b"        submissions = as_callee",
    )


def revert_unknown(src: bytes) -> bytes:
    """③ 把「来源未知」当成外部访问。"""
    return swap(
        src,
        b'"external": sum(n for a, n in by_caller.items() if _is_external(a)),',
        b'"external": sum(n for a, n in by_caller.items() if not _is_own(a)),',
    )


def revert_unparsed(src: bytes) -> bytes:
    """④ 解析失败的行静默丢弃（不计数）。

    定位方式：**按行索引**，而不是去匹配中文注释的 UTF-8 字节 —— 后者既难读
    又会在注释措辞一改时静默失效（锚点的唯一性 > 不锚注释，但能锚 ASCII 时
    优先锚 ASCII）。
    """
    lines = src.split(NL)
    i = next(
        k for k, l in enumerate(lines)
        if l == b"                    except ValueError:"
    )
    j = next(
        k for k, l in enumerate(lines)
        if k > i and l == b"                        unparsed += 1"
    )
    lines[j] = b"                        parsed += 1"
    return NL.join(lines)


def revert_state(src: bytes) -> bytes:
    """⑤ 回到"不看终态"的旧形态：非成功集合为空 ⇒ canceled 混进成功访问。

    退回方式是**把集合清空**，而不是删掉那段代码 —— 后者会让「非成功」直接
    变成语法错误或 KeyError，元验证就退化成"文件坏掉会报失败"，测不到
    「canceled 被当成成功」这件事本身。
    """
    return swap(
        src,
        b'_NON_SUCCESS_INBOUND_STATES = frozenset({"failed", "canceled"})',
        b"_NON_SUCCESS_INBOUND_STATES = frozenset()",
    )


CASES: list[tuple[str, object, list[str]]] = [
    ("① 分方向：不区分「被调用 / 我调人」", revert_direction, ["被调用", "我调人"]),
    ("② 轮询折算：任务提交不去扣轮询", revert_poll, ["折算任务提交"]),
    ("③ 三分类：把「未知」当外部", revert_unknown, ["外部 = 1"]),
    ("④ 坏行计数：解析失败静默丢弃", revert_unparsed, ["解析失败"]),
    ("⑤ 终态：failed/canceled 不单列", revert_state, ["非成功合计", "里的非成功"]),
]


def main() -> int:
    orig = TARGET.read_bytes()
    print("=" * 74)
    print(f"元验证：tools/metrics.py 的「访问」口径 {len(CASES)} 条，"
          "各退回旧形态后断言必须报失败")
    print("=" * 74)

    report: list[tuple[str, bool, list[str]]] = []
    for name, revert, needles in CASES:
        try:
            TARGET.write_bytes(revert(orig))  # type: ignore[operator]
            assert syntax_ok(TARGET), f"{name}：退回后的文件语法不正确 —— 元验证无效！"
            bad = failed(run("tools.verify_metrics"), needles)
            report.append((name, bool(bad), bad))
        finally:
            TARGET.write_bytes(orig)

    print()
    ok = True
    for name, hit, bad in report:
        if hit:
            print(f"  [通过] {name}  ->  退回后报失败 {len(bad)} 项")
            for b in bad[:3]:
                print(f"         {b}")
        else:
            ok = False
            print(f"  [失败] {name}  ->  退回旧形态后断言**没有响**（空转的判据！）")

    assert TARGET.read_bytes() == orig, "metrics.py 未复原！"

    res = run("tools.verify_metrics")
    restored = res.returncode == 0
    tail = [
        l.strip() for l in res.stdout.splitlines() if "全部符合预期" in l
    ]
    print()
    print(f"  复原后 verify_metrics exit = {res.returncode}  "
          f"（{'全绿' if restored else '仍有失败 —— 复原失败！'}）{tail[0] if tail else ''}")
    print("=" * 74)
    return 0 if (ok and restored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
