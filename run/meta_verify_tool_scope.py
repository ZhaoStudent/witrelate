"""元验证：工具收敛 / token 预算 / 降级 这三批断言，退回旧形态后必须报失败。

覆盖十个最容易"看起来绿、其实空转"的点 —— 它们全都来自**同一件真机事故**
（2026-10-01 夜：用真模型跑一局，揭示那一环一步都不调工具、输出为空，
整局在最后一步报废；连着三局把它逼出真相）：

  ① **裁判不挂收敛 Hook** —— 回到"两条能力同时摆在模型面前由它挑"的旧形态。
     「每一次请求都只暴露一个工具」必须失败。这是本环节的要害：Hook 的代码
     全都在，唯独没挂上去，链路照样全绿（本项目为"装了≠会响"付过学费）。
  ② **Hook 在"调用方没指定"时自己猜**（自动挑第一个工具）。
     「没指定 → 不动请求」必须失败：一个会替调用方猜的 Hook，比没有 Hook 更危险。
     ⚠️ 退回形态必须**精准**（只改"没指定"那条分支）。第一版顺手把主流程也改坏了，
     于是脚本在更早的用例上直接崩掉、这条断言压根没执行到 —— 探针被误判成"空转"。
     这正是下面 `probe()` 要区分"断言报了失败"与"脚本崩了"的原因。
  ③ **Hook 退回"目标工具不在就清空"** —— 把"这一轮必须交结论"悄悄换成"只能输出文本"。
     「目标工具不在本轮列表里 → 不清空」必须失败。
  ④ **`_run` 退回"不重试"** —— 一次偶发不调工具就让用户丢掉整局。
     「第一次没拿到结论 → 会重试一次」必须失败。
  ⑤ **`_run` 不再把目标工具写进 metadata** —— Hook 就成了摆设（没指令可执行）。
     「每一轮都带上了『本轮只暴露目标工具』的指令」必须失败。
  ⑥ **揭示退回共用默认步数预算**。
     「揭示拿到自己的、更宽的步数与 token 预算」必须失败。
  ⑦ **Hook 收下 token 预算却**不改请求**（安静地无效）** —— 参数接对了 ≠ 被用上了。
     「Hook 真的把 token 预算写进了本次请求」必须失败。
  ⑧ **揭示退回共用默认 token 预算** —— 这才是那次故障的**真正原因**：
     揭示是全链路上下文最长的一环，沿用全局 8192 会被思考过程吃光 ⇒ 空输出。
     「揭示的 token 预算确实比默认宽」必须失败。
  ⑨ **降级不再显式标注**（只删 `judge_ok: False`，保留降级本身）——
     降级会变成一个页面照常能看、而没人知道裁判一直没干活的常态。
     「裁判交不回结论时不抛错（且标注为 False）」必须失败。
  ⑩ **降级时仍只认裁判的汇总** —— 拿不到裁判结论时，连服务端手上那份
     真实话术清单也一起空了。「降级页仍然给出真实的话术清单」必须失败。

（写成文件而不是 heredoc：Bash 工具会把字节串里的 `\\n` 转义吃掉，
  详见 run/meta_verify_fixes.py 顶部说明。另：本文件里所有锚点都用
  `str.encode()` 而不是 `b"中文"` —— Python 的 bytes 字面量不允许非 ASCII。
  另：本脚本会**就地改写源文件再复原**，绝不能与别的验证并发跑，且**不要被超时打断**。）
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SESSION = ROOT / "legal_acps/experience/session.py"
ROLES = ROOT / "legal_acps/experience/roles.py"
SCOPE = ROOT / "legal_acps/experience/tool_scope.py"


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


def probe(needle: str) -> tuple[list[str], bool]:
    """跑一次 verify_experience：返回（匹配 needle 的 [失败] 行，脚本是否崩了）。

    ⚠️ **必须区分"断言报了失败"与"脚本半路崩溃"**：崩溃产生的是 traceback、
    不是 `[失败]` 行 —— 只看 `[失败]` 就会把"崩了"误判成"探针空转"。
    （2026-10-01 夜真的误判过一次：探针 ② 报"空转"，实际是脚本在用例 A 就崩了，
      后面的用例一条都没跑，于是这条断言压根没被执行到。同一个根因也让
      `verify_experience` 自己修好了：用例抛异常现在会记成一条 `[失败]` 并继续。）
    """
    p = run("tools.verify_experience")
    lines = [
        ln.strip()
        for ln in p.stdout.splitlines()
        if "[失败]" in ln and needle in ln
    ]
    crashed = "Traceback (most recent call last)" in (p.stderr or "")
    return lines, crashed


def revert(
    path: Path,
    old: str,
    new: str,
    *,
    label: str,
    needle: str,
    report: list,
) -> None:
    """把文件临时退回旧形态跑一次，无论结果如何都逐字节复原。"""
    original = path.read_bytes()
    before = digest(path)
    old_b, new_b = old.encode("utf-8"), new.encode("utf-8")
    assert original.count(old_b) == 1, f"{path.name} 锚点不唯一：{original.count(old_b)} 次"
    try:
        path.write_bytes(original.replace(old_b, new_b))
        assert syntax_ok(path), f"退回后的 {path.name} 语法不正确 —— 元验证无效！"
        bad, crashed = probe(needle)
        report.append((label, bool(bad), bad, crashed))
    finally:
        path.write_bytes(original)
        assert digest(path) == before, f"{path.name} 未逐字节复原！"


report: list[tuple[str, bool, list[str], bool]] = []

# ---------- ① 裁判不挂收敛 Hook ----------
revert(
    ROLES,
    "        # ⚠️ 这个 Hook 对本 Agent 是**必需**的，不是锦上添花：两条能力同时挂在\n"
    "        #    一轮的 `request.tools` 里，真模型会挑错 —— 真机上揭示那一环因此\n"
    "        #    偶发地一步都不调，整局在最后一步报废。详见 tool_scope.py。\n"
    "        hooks=[ToolScopeHooks()],\n",
    "",
    label="roles.py · 裁判不挂收敛 Hook（两条能力又摆回模型面前由它挑）",
    needle="每一次请求都只暴露一个工具",
    report=report,
)

# ---------- ② Hook 在"调用方没指定"时自己猜（挑第一个工具） ----------
# ⚠️ 退回形态要**精准**，不能顺手把主流程也弄坏：
#    否则脚本会在更早的用例上就崩掉，这条断言压根执行不到 ——
#    那就变成了"验了个寂寞"。这里只改"没指定"的分支，主流程
#    （每一次 `_run` 都带 metadata）完全不受影响。
revert(
    SCOPE,
    "def _apply_tool_scope(ctx: Any, request: ModelRequest) -> None:\n"
    "    only = only_tool_of(ctx)\n"
    "    if not only:\n"
    "        return\n",
    "def _apply_tool_scope(ctx: Any, request: ModelRequest) -> None:\n"
    "    only = only_tool_of(ctx)\n"
    "    if not only:\n"
    "        # 旧形态：调用方没说，就自己挑第一个\n"
    "        _head = list(getattr(request, \"tools\", None) or [])\n"
    "        only = _tool_name_of(_head[0]) if _head else \"\"\n",
    label="tool_scope.py · 调用方没指定时 Hook 自己猜（自动只留第一个工具）",
    needle="没指定 → **不动**请求",
    report=report,
)

# ---------- ③ Hook 退回"目标工具不在就清空" ----------
revert(
    SCOPE,
    "    if keep and len(keep) != len(originals):\n"
    "        request.tools = keep\n",
    "    if len(keep) != len(originals):\n"
    "        request.tools = keep\n",
    label="tool_scope.py · 目标工具不在本轮列表里也强行收敛（把工具清空）",
    needle="目标工具不在本轮列表里 → **不清空**",
    report=report,
)

# ---------- ④ _run 退回"不重试" ----------
revert(
    SESSION,
    "        for _ in range(RUN_RETRIES + 1):\n",
    "        for _ in range(1):\n",
    label="session.py · _run 退回「一次不调工具就放弃」",
    needle="第一次没拿到结论 → 会重试一次",
    report=report,
)

# ---------- ⑤ _run 不再把目标工具写进 metadata ----------
revert(
    SESSION,
    "            metadata: dict[str, Any] = {ONLY_TOOL_KEY: model_tool}\n",
    "            metadata: dict[str, Any] = {}\n",
    label="session.py · _run 不再告诉 Hook「本轮只留哪个工具」（Hook 成摆设）",
    needle="每一轮都带上了「本轮只暴露目标工具」的指令",
    report=report,
)

# ---------- ⑥ 揭示退回共用默认预算 ----------
revert(
    SESSION,
    "            # ⚠️ 揭示的两项预算都单独给：它是**上下文最长**的一环。\n"
    "            #    token 那条才是真机故障的解；步数那条只是\"给宽不花钱\"，\n"
    "            #    别把两者混为一谈（见两个常量的注释）。\n"
    "            max_steps=REVEAL_MAX_STEPS,\n",
    "            # 退回旧形态：什么都不单独给（步数也没了）\n",
    label="session.py · 揭示退回与普通环节共用默认步数预算",
    needle="揭示拿到自己的、更宽的步数与 token 预算",
    report=report,
)

# ---------- ⑦ Hook 收下 token 预算，却**不改请求** ----------
# ⚠️ 这一条补的是"参数接对了 ≠ 被用上了"：metadata 里有值、Hook 也收到了，
#    但没写进 request.settings ⇒ 模型照样拿空输出，而**哪里都不报错**。
revert(
    SCOPE,
    "    if isinstance(settings, dict):\n"
    "        settings[\"max_tokens\"] = wanted\n",
    "    if isinstance(settings, dict):\n"
    "        pass\n",
    label="tool_scope.py · Hook 收下 token 预算却不改请求（安静地无效）",
    needle="Hook 真的把 token 预算写进了本次请求",
    report=report,
)

# ---------- ⑧ 揭示退回"共用默认 token 预算" ----------
# 这是那次真机故障的**真正原因**所在：揭示是全链路上下文最长的一环，
# 沿用全局 8192 会被思考过程吃光 ⇒ content 为空、模型"没输出"。
revert(
    SESSION,
    "            max_tokens=REVEAL_MAX_TOKENS,\n",
    "",
    label="session.py · 揭示退回共用默认 token 预算（思考吃光 ⇒ 空输出）",
    needle="揭示的 token 预算确实比默认宽",
    report=report,
)

# ---------- ⑨ 降级不再显式标注（变成"安静的常态"） ----------
# ⚠️ 只删 `judge_ok: False` 这一个标记，**保留降级本身** ——
#    这样脚本不会崩，断言才有机会开口；顺带证明"标记本身"是被断言守着的：
#    没有它，降级会变成一个页面照常能看、而没人知道裁判一直没干活的状态。
revert(
    SESSION,
    "                \"judge_ok\": False,\n",
    "",
    label="session.py · 降级不再显式标注 judge_ok（降级变成安静的常态）",
    needle="裁判交不回结论时**不抛错**",
    report=report,
)

# ---------- ⑩ 降级时不用服务端自己的话术（改回只认裁判的汇总） ----------
revert(
    SESSION,
    "            \"trap_groups\": _group_traps(\n"
    "                (judge_out.get(\"traps\") or []) if judge_out.get(\"judge_ok\") else traps_all\n"
    "            ),\n",
    "            \"trap_groups\": _group_traps(judge_out.get(\"traps\")),\n",
    label="session.py · 降级时仍只认裁判的汇总（拿不到 ⇒ 话术清单也空了）",
    needle="降级页仍然给出**真实的话术清单**",
    report=report,
)

# ---------- 复原后必须整体转绿 ----------
after = run("tools.verify_experience")
print("=== 元验证结果 ===")
all_ok = True
for label, caught, lines, crashed in report:
    if caught:
        mark = "有效"
    elif crashed:
        # 崩溃 ≠ 空转，但也**不算**被这条断言逮到 —— 两种都不算通过。
        mark = "❌ 崩溃（没跑到这条断言）"
    else:
        mark = "❌ 空转"
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
print("总判定：全部有效（旧形态必被逮到）")
