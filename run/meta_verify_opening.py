"""元验证：立题 / 分层复用 / 难度 / 标记 这四批新断言，退回旧形态后必须报失败。

覆盖六个最容易"看起来绿、其实空转"的点：

  ① **裁判立题从"承诺"退回"没实现"** —— 删掉 `start()` 里的 `await self._opening()`。
     「开局时裁判真的被调用」必须失败。这一条是本环节的要害：契约里有字段、
     提示词里写满立题，都不代表有任何代码路径会走到它（那正是它的旧状态）。
  ② 裁判只挂揭示、不挂立题 —— 「同时挂了两个能力」必须失败。
  ③ **确定性守门退回"认不出方向倒置"** —— 从词表里删掉「被冤枉」。
     这是整个体验唯一的把关点，必须由断言守住。
  ④ **`group_fallacies` 退回"只认 .kind"** —— 它对 dict 一律归"未归类"。
     「dict 形态能被正确分层」必须失败。
  ⑤ **编排层退回"自己再写一份分层表"** —— 这是"同一事实散在多处"的老毛病。
     判据不是"输出像不像"，而是"改了 contracts 的定义，编排层跟不跟着变"；
     自写一份的实现不会跟着变（那正是它过去当死代码时的样子）。
  ⑥ **前端不再提交当事人的标记** —— 标记功能会退化成纯装饰。
     这一条同时由 Python 断言与 node 行为验证两处守住。

（写成文件而不是 heredoc：Bash 工具会把字节串里的 `\\n` 转义吃掉，
  详见 run/meta_verify_fixes.py 顶部说明。另：本文件里所有锚点都用
  `str.encode()` 而不是 `b"中文"` —— Python 的 bytes 字面量不允许非 ASCII。）
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SESSION = ROOT / "legal_acps/experience/session.py"
ROLES = ROOT / "legal_acps/experience/roles.py"
OPENING = ROOT / "legal_acps/experience/opening.py"
CONTRACTS = ROOT / "legal_acps/contracts.py"
HTML = ROOT / "legal_acps/experience/web/index.html"

#: 前端行为验证要用 node。找不到就**如实报出来**，不静默跳过 ——
#: 静默跳过会让"⑥ 由两处守住"变成"只由一处守住"，而报告看起来一样。
NODE = shutil.which("node") or (
    "C:/Users/Zhao5/.workbuddy/binaries/node/versions/22.22.2-5/node.exe"
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


def failed(needle: str) -> list[str]:
    return [
        line.strip()
        for line in run("tools.verify_experience").stdout.splitlines()
        if "[失败]" in line and needle in line
    ]


def node_failed() -> list[str]:
    """真跑前端脚本，看行为验证报出了哪些失败。"""
    proc = subprocess.run(
        [NODE, "run/verify_frontend_flow.mjs"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [line.strip() for line in proc.stdout.splitlines() if "[失败]" in line]


def revert(
    path: Path,
    old: str,
    new: str,
    *,
    label: str,
    needle: str,
    report: list,
    check_syntax: bool = True,
) -> None:
    """把文件临时退回旧形态跑一次，无论结果如何都逐字节复原。"""
    original = path.read_bytes()
    before = digest(path)
    old_b, new_b = old.encode("utf-8"), new.encode("utf-8")
    assert original.count(old_b) == 1, f"{path.name} 锚点不唯一：{original.count(old_b)} 次"
    try:
        path.write_bytes(original.replace(old_b, new_b))
        if check_syntax:
            assert syntax_ok(path), f"退回后的 {path.name} 语法不正确 —— 元验证无效！"
        bad = failed(needle)
        report.append((label, bool(bad), bad))
    finally:
        path.write_bytes(original)
        assert digest(path) == before, f"{path.name} 未逐字节复原！"


report: list[tuple[str, bool, list[str]]] = []

# ---------- ① 立题退回"没实现"：删掉 start() 里那次调用 ----------
revert(
    SESSION,
    "        # ②③ 立题（确定性守门 + 裁判语义立题）\n        await self._opening()\n",
    "",
    label="session.py · 删掉 start() 里的立题调用（回到「只写了没实现」）",
    needle="开局时裁判真的被调用",
    report=report,
)

# ---------- ② 裁判只挂揭示能力 ----------
revert(
    ROLES,
    "        capabilities=[open_case, reveal_deception],",
    "        capabilities=[reveal_deception],",
    label="roles.py · 裁判不挂立题能力",
    needle="裁判同时挂了「立题」与「揭示」两个能力",
    report=report,
)

# ---------- ③ 守门认不出方向倒置 ----------
revert(
    OPENING,
    '    r"被.{0,10}(?:冤枉|污蔑|诬陷|造谣|诽谤|抹黑)",\n',
    "",
    label="opening.py · 删掉方向倒置的主模式（含「被…污蔑」这种中间夹字的真实说法）",
    needle="守门：命题方向写反",
    report=report,
)

# ---------- ④ group_fallacies 退回"只认 .kind" ----------
revert(
    CONTRACTS,
    '    kind = getattr(item, "kind", None)\n'
    '    if kind is None and isinstance(item, Mapping):\n'
    '        kind = item.get("kind")\n'
    "    return kind\n",
    '    return getattr(item, "kind", item)\n',
    label="contracts.py · group_fallacies 退回只认 .kind（dict 全成未归类）",
    needle="dict 形态能被正确分层",
    report=report,
)

# ---------- ⑤ 编排层退回"自己再写一份分层表" ----------
LOCAL_TAXONOMY = '''    _LOCAL_LAYER = {
        "时间错配": "证据层", "断章取义": "证据层",
        "张冠李戴": "证据层", "数字夸张": "证据层",
        "诉诸情绪": "修辞层", "滑坡推论": "修辞层",
        "虚假二分": "修辞层", "稻草人": "修辞层",
        "诉诸权威": "修辞层", "举证倒置": "修辞层",
    }
    grouped_local: dict[str, list] = {}
    for trap in traps or []:
        layer = _LOCAL_LAYER.get((trap or {}).get("kind", ""), "未归类")
        grouped_local.setdefault(layer, []).append(trap)
    return [{"layer": name, "traps": items} for name, items in grouped_local.items()]
'''
revert(
    SESSION,
    '    return [\n'
    '        {"layer": name, "traps": items}\n'
    "        for name, items in group_fallacies(traps).items()\n"
    "        if items\n"
    "    ]\n",
    LOCAL_TAXONOMY,
    label="session.py · 编排层自己再写一份分层表（不再复用 contracts）",
    needle="改了 contracts 的层定义，编排层跟着变",
    report=report,
)

# ---------- ⑥ 前端不再提交当事人的标记 ----------
original = HTML.read_bytes()
before = digest(HTML)
old = "suspected: [...suspects]".encode()
new = "suspected: []".encode()
assert original.count(old) == 1, "index.html 锚点不唯一"
try:
    HTML.write_bytes(original.replace(old, new))
    py_bad = failed("前端：揭示请求带上当事人标的轮次")
    node_bad = node_failed()
    report.append(
        (
            "index.html · 揭示不再提交当事人的标记（Python 断言 + node 行为验证）",
            bool(py_bad) and bool(node_bad),
            py_bad + node_bad,
        )
    )
finally:
    HTML.write_bytes(original)
assert digest(HTML) == before, "index.html 未逐字节复原！"

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
