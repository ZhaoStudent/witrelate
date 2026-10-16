"""元验证：把历次修复各**退回旧形态**，断言必须报失败。

现有 6 条：2026-09-30 的四处（metrics 空 AIC / status 状态表 ×3）
＋ 2026-10-05 的两条 3b 换行符判据（#129：`run/*.py` 原先不在判据里；
清单里出现**没归类的新后缀**也必须报失败）。

⚠️ 为什么写成文件、而不是用 heredoc 直接跑：
   Bash 工具的 heredoc 会把 Python 字节串里的 `\\n` 吃掉转义、变成字面 `/n`，
   于是"退回旧形态"实际变成"写入语法错误 / 把文件清空" —— 元验证就退化成
   "文件坏掉会报失败"，测的根本不是我们要测的东西。
   （2026-09-30 踩到：status 的元验证因此一度是空转的，是后来核对才发现。）
   所以这里额外加一道保险：**退回后的文件必须仍然是语法正确的**。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

NL = chr(10).encode()
ROOT = Path(__file__).resolve().parent.parent

# ===========================================================================
# 🔴 **本文件是脚本，不是库** —— 它的 `import` 有副作用：下面每一段都会**真的改写**
#    `tools/metrics.py` 与 `tools/status.py`，再在 `finally` 里复原。
#
#    2026-10-06 实测（**同一个坑第二次**）：一个只想读一份文件清单的辅助脚本
#    `import` 了本模块，跑到用例 ④ 时被工具超时 **SIGTERM** 掐断 ——
#    而 Python 收到 SIGTERM **不执行 `finally`** ⇒ `tools/status.py` 被留在退回形态
#    （`BENIGN_STATES` 里少了 `"serving"`）。更糟的是：**下一次运行会把这份坏文件
#    当成 `orig2`（基线）**，"复原"复的是坏版本，而前置检查当时只查了一个锚点、
#    没查到坏掉的那一个 ⇒ 一声没响。最后是 `check_local` 3b ⑤ 报出
#    `[失败] 状态分类：有状态**未归类**：['serving']` 才被发现。
#
#    ⇒ 把"被 import"变成一声**响亮的拒绝**（本项目一贯取向：宁可响亮拒绝，
#      不要静默地"安全"）。真想用本模块里的锚点数据，正确做法是：
#        ① 给本模块加一个 `CASES` 列表（`{name, why, file, new, old, needle}`，
#           `new`/`old` 一律 **str**）；
#        ② 在 `run/check_meta_residue.py` 的 `SOURCES` 里登记一行；
#        ③ 再把下面这个闸门放宽。
#      在此之前**不要**为了"读点常量"而 import 它。
# ===========================================================================
if __name__ != "__main__":
    raise RuntimeError(
        "run/meta_verify_fixes.py 是一支**会改写源文件的脚本**，不是库："
        "import 它会当场把 tools/metrics.py 与 tools/status.py 退回旧形态再复原，"
        "而进程被 SIGTERM 掐断时 finally 不执行 ⇒ 文件会留在**坏形态**上，"
        "下一次运行还会把坏文件当成基线（2026-10-06 实测，同一个坑第二次）。"
        "请用 `./.venv/Scripts/python.exe -m run.meta_verify_fixes` 跑它；"
        "需要它里面的锚点数据，请先做 `CASES` 再登记进 check_meta_residue 的 SOURCES。"
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


def failed(res: subprocess.CompletedProcess, needles: list[str]) -> list[str]:
    return [
        l.strip()
        for l in res.stdout.splitlines()
        if "[失败]" in l and any(n in l for n in needles)
    ]


report: list[tuple[str, bool, list[str]]] = []

# ---------- 前置检查：**全部四个锚点**都必须在新形态 ----------
# 🔴 2026-10-05 实测（改 3b 判据时踩到的）：本模块第一次跑被工具超时 **SIGTERM** 掐断，
#    而 Python 收到 SIGTERM **不会**执行 `finally` ⇒ `tools/status.py` 被**留在退回形态**。
#    再跑时，它把这份**坏文件**当成 `orig2` 读了进来 —— 于是"复原"复的是坏版本，
#    断言全绿、退出码 0，**而磁盘上一直躺着一个坏文件**，直到 `verify.sh` 报出来
#    （`[失败] tools/status.py 有 1 个不存在的死键`）。
#    这是判据的通用病：**"没得比"与"不一样"长得一模一样**（本项目纪律：
#    比对类判据必须先单独断言「基线存在」）。
#
# 🔴 **2026-10-06 同一个坑第二次**，而且暴露了当时那条修法的缺口：那时只查了
#    `DEAD_KEY` **一个**锚点，而这一次被掐断时坏掉的是**另一个**锚点
#    （`BENIGN_STATES` 里少了 `"serving"`）⇒ 前置检查一声没响，坏文件又被当成基线。
#    ⇒ **覆盖不全的前置检查，与"没有前置检查"在关键路径上长得一模一样**
#      （判据的"输入集合"必须完整，这是本项目的老账）。
#
#    所以下面把**全部四个锚点**一起查；而且诊断必须**指向真因**：
#    `serving 锚点不唯一：0` 只会让人以为锚点写错了，真相却是"上一个进程被掐断、
#    文件留在退回形态"。**修法必须写在屏幕上**，否则下一个人还得自己推。
#
# ⚠️ 本块**先跑、且一有问题就 sys.exit** ⇒ 下面各用例直接消费这里算好的量，
#    不再各自推导一遍（"同一判定写两处必然漂移"）。
p1 = ROOT / "tools" / "metrics.py"
orig1 = p1.read_bytes()
lines1 = orig1.split(NL)
i1 = next((k for k, l in enumerate(lines1) if b"peer = (t.remote_aic" in l), -1)
j1 = next(
    (k for k, l in enumerate(lines1) if k > i1 and b"m.partners.add(peer)" in l), -1
)
#: **修复后**（新形态）：空 AIC 单独计入 retired，不算伙伴。
old1 = NL.join(lines1[i1 : j1 + 1]) if (i1 >= 0 and j1 > i1) else b""
#: **退回后**（旧形态）：空 AIC 也算进伙伴数。
new1 = NL.join(
    [
        b'            if t.remote_aic in RETIRED_AICS:',
        b'                m.retired_partners.add(t.remote_aic)',
        b'            else:',
        b'                m.partners.add(t.remote_aic)',
    ]
)

p2 = ROOT / "tools" / "status.py"
orig2 = p2.read_bytes()
DEAD_KEY = b'    "needs_agent_registration"'
#: `BENIGN_STATES` 里的一个**真实**状态项（用例 (4) 就是把它摘掉）。
SERVING_ITEM = b'        "serving",' + NL
#: 用例 (3) 的插入点。
RENEWAL_ANCHOR = b'    "needs_renewal"'

_baseline_problems: list[str] = []
if not old1 or orig1.count(old1) != 1 or new1 in orig1:
    _baseline_problems.append(
        "tools/metrics.py 的锚点不在新形态"
        f"（新形态 x{orig1.count(old1) if old1 else 0}、"
        f"旧形态在里面={new1 in orig1}）—— 上一次运行多半被 SIGTERM 掐断、"
        "把文件留在了退回形态（SIGTERM 不执行 finally）"
    )
if DEAD_KEY in orig2:
    _baseline_problems.append(
        "tools/status.py 里已经有那个死键 `needs_agent_registration` 了"
        "（同上：上一次被掐断留下的退回形态）"
    )
if orig2.count(SERVING_ITEM) != 1:
    _baseline_problems.append(
        "tools/status.py 的 BENIGN_STATES 里 `serving` 一项不在位"
        f"（实得 {orig2.count(SERVING_ITEM)} 处）—— 同上，是上一次被掐断留下的退回形态；"
        "它会让 check_local 3b 的「状态分类」报「有状态未归类」，"
        "而那条报错**不会**告诉你这里是元验证留下的残留（2026-10-06 实测）"
    )
if RENEWAL_ANCHOR not in orig2:
    _baseline_problems.append(
        "tools/status.py 里找不到 `needs_renewal` 锚点（用例 (3) 的插入点）"
    )

if _baseline_problems:
    print(
        "前置检查未通过：**基线不在新形态** —— 本次一个字都不改"
        "（改了只会把坏文件当成基线）。"
    )
    for _p in _baseline_problems:
        print(f"   - {_p}")
    print("   修法：仓库**没有版本历史**，拿服务器上的权威副本核对／还原：")
    print("         ssh <服务器> 'cd ~/witrelate && md5sum tools/status.py tools/metrics.py'")
    sys.exit(1)

# ---------- (1) tools/metrics.py：outbound 空 AIC 不得计入伙伴数 ----------
try:
    p1.write_bytes(orig1.replace(old1, new1))
    assert syntax_ok(p1), "退回后的 metrics.py 语法不正确 —— 元验证无效！"
    bad = failed(run("tools.verify_metrics"), ["缺失"])
    report.append(("metrics · 空 AIC 计入伙伴数", bool(bad), bad))
finally:
    p1.write_bytes(orig1)

# ---------- ② tools/status.py：状态表缺一个真实状态 ----------
lines2 = orig2.split(NL)
keep = [l for l in lines2 if b'"serving":' not in l]
assert len(keep) == len(lines2) - 1, "status 锚点未命中"
try:
    p2.write_bytes(NL.join(keep))
    assert syntax_ok(p2), "退回后的 status.py 语法不正确 —— 元验证无效！"
    bad = failed(run("legal_acps.check_local"), ["真实状态", "死键"])
    report.append(("status · 缺一个真实状态", bool(bad), bad))
finally:
    p2.write_bytes(orig2)

# ---------- ③ tools/status.py：塞回那个死键 ----------
#: 锚点在不在位由**上面的前置检查**保证（它先跑，缺了就 sys.exit）。
#: 留一条重复的 assert 在这里就是「装了但不会响」——本项目明令不许。
anchor = RENEWAL_ANCHOR
idx = orig2.find(anchor)
eol = orig2.find(NL, idx)
assert eol > idx, "找不到行尾"
dead_line = '    "needs_agent_registration": ("尚未注册", "x"),'.encode()
# ⚠️ 局部再确认一次：若死键**已经**在文件里，这里再插一条只会变成"插了两条"，
#    而断言照样会响 —— "响了"和"响的是你要证的那件事"是两回事。
assert DEAD_KEY not in orig2, "死键已经在 status.py 里（基线不对），先修文件再跑"
with_dead = orig2[: eol + 1] + dead_line + NL + orig2[eol + 1 :]
try:
    p2.write_bytes(with_dead)
    assert syntax_ok(p2), "退回后的 status.py 语法不正确 —— 元验证无效！"
    bad = failed(run("legal_acps.check_local"), ["死键"])
    report.append(("status · 塞回死键", bool(bad), bad))
finally:
    p2.write_bytes(orig2)

# ---------- ④ tools/status.py：状态**分类**漏掉一个 ----------
# 模拟"wit 新增了一个状态，本地看板没归类" —— 它会静默落进"无需处理"。
# 这里从 BENIGN_STATES 里摘掉 "serving"（保持三张表都在，文件仍语法正确），
# 断言 3b ⑤ 的完整性检查会把它揪出来。
#: 同上：`serving` 那一项在不在位由上面的前置检查保证。
anchor4 = SERVING_ITEM
try:
    p2.write_bytes(orig2.replace(anchor4, b""))
    assert syntax_ok(p2), "退回后的 status.py 语法不正确 —— 元验证无效！"
    bad = failed(run("legal_acps.check_local"), ["未归类"])
    report.append(("status · 状态分类漏归类", bool(bad), bad))
finally:
    p2.write_bytes(orig2)

# ---------- ⑤ 3b 换行符判据：CRLF 的 `run/*.py` 必须被逮到 --------------
# 2026-10-05（#129）：3b ① 原先**按扩展名白名单**收文件，`run/*.py`（54 个、
# **明明在同步清单里**）根本不在判据范围内 —— 于是"把一份 CRLF 推上服务器"
# 与"从没推过 CRLF"在自检里**长得一模一样**。判据改成按同步清单现场派生之后，
# 必须证明**新纳入的那一类真的会被逮到**：只改判据不实测 = 不知道它响不响。
# ⚠️ 探针是一个临时文件，`finally` + 末尾断言双重保证不留下残留 ——
#    留在 `run/` 里会被同步清单收走，变成一次**永久的假警报**。
probe = ROOT / "run" / "_crlf_probe_tmp.py"
assert not probe.exists(), f"探针文件已存在（上一次没清干净）：{probe}"
try:
    # 内容无害：判据只看字节，不 import 它
    probe.write_bytes(b"# CRLF probe" + b"\r\n" + b"x = 1" + NL)
    bad = failed(run("legal_acps.check_local"), ["CRLF", "_crlf_probe_tmp"])
    report.append(("3b · run/*.py 里的 CRLF", bool(bad), bad))
finally:
    probe.unlink(missing_ok=True)
assert not probe.exists(), f"探针文件未清掉，请手工删除：{probe}"

# ---------- ⑥ 3b 换行符判据：**没见过的新后缀**必须报失败 --------------
# 同一处实现里的**第二种缺陷**，所以要**另一条**退回用例（本项目纪律：
# "一处实现两种缺陷必须拆成两条退回用例"）。⑤ 证的是"新纳入的目录真的会被查"，
# ⑥ 证的是"以后加一类新后缀不会又静默漏掉" —— 而后者正是这次改写的**起因**
# （"再补一条白名单"不是修法）。做法：往同步清单覆盖的目录里放一个后缀
# 谁都不认识的文件，判据必须点名要人归类，而不是默认放过。
probe2 = ROOT / "deploy" / "_probe_tmp.toml"
assert not probe2.exists(), f"探针文件已存在（上一次没清干净）：{probe2}"
try:
    probe2.write_bytes(b"# unknown suffix probe" + NL)
    bad = failed(run("legal_acps.check_local"), ["没被归类", ".toml"])
    report.append(("3b · 清单里出现未分类的新后缀", bool(bad), bad))
finally:
    probe2.unlink(missing_ok=True)
assert not probe2.exists(), f"探针文件未清掉，请手工删除：{probe2}"

# ---------- 复原与总结 ----------
assert p1.read_bytes() == orig1, "metrics.py 未复原！"
assert p2.read_bytes() == orig2, "status.py 未复原！"
print("=== 复原校验 ===")
print("  metrics.py / status.py 均已还原为修复后版本")
print(f"  3b 探针（{probe.relative_to(ROOT).as_posix()}）已删除：{not probe.exists()}")
print(f"  3b 探针（{probe2.relative_to(ROOT).as_posix()}）已删除：{not probe2.exists()}")
print()
print("=== 元验证结果 ===")
all_ok = True
for label, caught, lines in report:
    mark = "有效" if caught else "❌ 空转"
    if not caught:
        all_ok = False
    print(f"  [{mark}] {label}：退回旧形态后报出 {len(lines)} 条失败")
    for l in lines:
        print(f"          {l}")
print()
print("总判定：", "全部有效（旧形态必被逮到）" if all_ok else "❌ 存在空转探针")
sys.exit(0 if all_ok else 1)
