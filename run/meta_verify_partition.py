"""元验证：功能④（信息分区+教学话术）的 ⑦-13 判据退回旧形态后必须报失败。

五锚（old/new 互不为子串；锚点全文唯一；跑法 = 字节级替换后单节跑
check_local._check_experience_partition，判据 = 输出里出现 [失败] 行）：

  A 隔离墙退回：build_deceiver_fact_section 把指控方视图换成当事人视图
    （mine 混进诱导方输入）⇒ 必须红「出现了 mine」。
  B 分区旁路退回：build_deceiver_fact_section 的 `if accuser:` 改 False
    ⇒ 有分区也走旧 JSON 路径 ⇒ 必须红「缺 shared/theirs」。
  C 相位机退回：session.start 的 `if followup:` 改 False
    ⇒ 受理官追问被无视、直接开辩 ⇒ 必须红「没暂停开辩」。
  D 教学指令段退回：build_reveal_prompt 不再附 `_teaching_section`
    ⇒ 裁判拿不到教学任务 ⇒ 必须红「缺「信息差教学」段」（2026-10-19）。
  E 板块注入退回：session.reveal 的 info_gap_review 恒 None
    ⇒ 前端拿不到板块数据 ⇒ 必须红「没带 info_gap_review」（2026-10-19）。

写成文件而不是 heredoc：Bash 工具会吃掉字节串里的转义（见
run/meta_verify_fixes.py 顶部说明）。🔴 元验证永远后台跑，窗口内禁 scp
（SIGTERM 会把源文件留在退回形态）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 单节跑 ⑦-13（独立子进程，避免本进程 import 的模块把退回形态缓存住）。
_SECTION13 = (
    "from legal_acps.check_local import _check_experience_partition; "
    "import sys; sys.exit(0 if _check_experience_partition(True) else 1)"
)


def section13() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SECTION13],
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

p_part = ROOT / "legal_acps/experience/partition.py"
orig_part = p_part.read_bytes()

# ---------- A 隔离墙退回：指控方视图 → 当事人视图（mine 无路可进的墙被拆） ----------
oldA = b"    accuser = render_accuser_view(case)"
newA = b"    accuser = render_user_view(case)  # deliberately broken"
assert orig_part.count(oldA) == 1, f"锚 A 不唯一：{orig_part.count(oldA)}"
assert oldA not in newA and newA not in oldA, "锚 A old/new 互为子串"
try:
    p_part.write_bytes(orig_part.replace(oldA, newA))
    assert syntax_ok(p_part), "退回后的 partition.py 语法不正确 —— 元验证无效！"
    badA = failed(
        section13(), ["出现了 mine", "输入含 mine", "mine 泄漏或缺 theirs"])
    report.append(("partition.py · 诱导方视图混进 mine（墙被拆）", bool(badA), badA))
finally:
    p_part.write_bytes(orig_part)

# ---------- B 分区旁路退回：有分区也走旧 JSON 路径 ----------
oldB = b"    if accuser:"
newB = b"    if False:  # deliberately broken"
assert orig_part.count(oldB) == 1, f"锚 B 不唯一：{orig_part.count(oldB)}"
assert oldB not in newB and newB not in oldB, "锚 B old/new 互为子串"
try:
    p_part.write_bytes(orig_part.replace(oldB, newB))
    assert syntax_ok(p_part), "退回后的 partition.py 语法不正确 —— 元验证无效！"
    badB = failed(section13(), ["缺 shared/theirs"])
    report.append(("partition.py · 分区被旁路（诱导方拿不到声称材料）", bool(badB), badB))
finally:
    p_part.write_bytes(orig_part)

# ---------- C 相位机退回：追问不再暂停开辩 ----------
p_sess = ROOT / "legal_acps/experience/session.py"
orig_sess = p_sess.read_bytes()
oldC = b"        if followup:"
newC = b"        if False:  # deliberately broken"
assert orig_sess.count(oldC) == 1, f"锚 C 不唯一：{orig_sess.count(oldC)}"
assert oldC not in newC and newC not in oldC, "锚 C old/new 互为子串"
try:
    p_sess.write_bytes(orig_sess.replace(oldC, newC))
    assert syntax_ok(p_sess), "退回后的 session.py 语法不正确 —— 元验证无效！"
    badC = failed(section13(), ["没暂停开辩"])
    report.append(("session.py · followup 相位机失灵（追问被无视）", bool(badC), badC))
finally:
    p_sess.write_bytes(orig_sess)

# ---------- D 教学指令段退回：揭示输入不再教裁判用三桶（2026-10-19 教学话术） ----------
oldD = b'        partition_section = full + "\\n\\n" + _teaching_section(case)'
newD = b'        partition_section = full + "\\n\\n"  # deliberately broken'
assert orig_part.count(oldD) == 1, f"锚 D 不唯一：{orig_part.count(oldD)}"
assert oldD not in newD and newD not in oldD, "锚 D old/new 互为子串"
try:
    p_part.write_bytes(orig_part.replace(oldD, newD))
    assert syntax_ok(p_part), "退回后的 partition.py 语法不正确 —— 元验证无效！"
    badD = failed(section13(), ["缺「信息差教学」段"])
    report.append(("partition.py · 教学指令段被裁（裁判拿不到教学任务）", bool(badD), badD))
finally:
    p_part.write_bytes(orig_part)

# ---------- E 板块注入退回：reveal 结果不再带信息差复盘数据 ----------
oldE = b'            "info_gap_review": build_info_gap_review(case),'
newE = b'            "info_gap_review": None,  # deliberately broken'
assert orig_sess.count(oldE) == 1, f"锚 E 不唯一：{orig_sess.count(oldE)}"
assert oldE not in newE and newE not in oldE, "锚 E old/new 互为子串"
try:
    p_sess.write_bytes(orig_sess.replace(oldE, newE))
    assert syntax_ok(p_sess), "退回后的 session.py 语法不正确 —— 元验证无效！"
    badE = failed(section13(), ["没带 info_gap_review"])
    report.append(("session.py · info_gap_review 注入被拆（前端拿不到板块）", bool(badE), badE))
finally:
    p_sess.write_bytes(orig_sess)

# ---------- 复原校验：字节相等 + 行为复原（复原后单节必须绿） ----------
assert p_part.read_bytes() == orig_part, "partition.py 未复原！"
assert p_sess.read_bytes() == orig_sess, "session.py 未复原！"
restored = section13()
restored_bad = [l for l in restored.stdout.splitlines() if "[失败]" in l]
report.append(
    ("复原后 ⑦-13 单节重跑（退出码 0 且无失败行）",
     restored.returncode == 0 and not restored_bad, restored_bad))

print("=== 复原校验 ===")
print("  partition.py / session.py 均已还原")
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
