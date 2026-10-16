"""元验证：人机辩论驱动器的四处要害锚点，退回旧形态必须让 check_local ⑦-6 变红。

被测的判据（`legal_acps/check_local.py` 的 ⑦-6「人机辩论」节）与被修的缺陷形态：

  A. 回放完整 —— `build_round_input` 必须把**全部**历史轮拼进输入。
     退回形态 = 只回放最近一轮（`history[-1:]`）：无框架会话里漏掉的轮
     对正方**等于没发生过**，对抗性检验静默失效。
  B. 抽取口径 —— 只认 `debate.submit_argument` **且 status == success** 的调用。
     退回形态 = 只认工具名不认状态：失败调用的 result 残渣会被当成正方论点
     写进台账（「装了 ≠ 会响」的抽取版）。
  C. 失败轮必落盘 —— 每次失败（异常 / 只写散文）都要写 `round_failed` 行。
     退回形态 = 写一条含糊的 `round_skipped`：失败被静默降级，
     「重试过几次、为什么重试」从台账里消失（旁路记录不许顶掉主流程）。
  D. concede 传播 —— `round_decision` 必须把认输当终止信号。
     退回形态 = 判空串：正方认输后辩论继续空转，用户被蒙在鼓里。

⚠️ 沿用本目录既有 harness（与 `meta_verify_leader_name.py` 等有意重复）：
   退回后必须仍语法正确；整篇读写一律 read_bytes / write_bytes；
   try/finally 兜底复原；复原后整体转绿才算数。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERIFIER = [sys.executable, "-m", "legal_acps.check_local"]

CASES = [
    {
        "name": "回放必须含全部历史轮（不许只回放最近一轮）",
        "why": "人机辩论无框架会话，正方的记忆完全来自显式回放；"
               "只回放最近一轮 ⇒ 更早的轮对正方等于没发生过。",
        "file": ROOT / "legal_acps" / "human_debate.py",
        "new": "        for h in history:",
        "old": "        for h in history[-1:]:",
        "needle": "[失败] 人机辩论回放",
    },
    {
        "name": "抽取只认成功的 submit 调用（失败残渣不许当论点）",
        "why": "真机 observation 的失败调用带什么 result 字段没有被承诺；"
               "不校验 status ⇒ 错误残渣被记进台账当正方论点。",
        "file": ROOT / "legal_acps" / "human_debate.py",
        "new": '        if o.get("tool") == TOOL_ID and o.get("status") == "success":',
        "old": '        if o.get("tool") == TOOL_ID:',
        "needle": "[失败] 人机辩论抽取",
    },
    {
        "name": "失败轮必须以 round_failed 落盘（不许降级成含糊记录）",
        "why": "台账是单一事实源：重试过几次、为什么重试，事后只能从这里查；"
               "失败被写成 round_skipped ⇒ 「异常留痕」变「异常蒸发」。",
        "file": ROOT / "legal_acps" / "human_debate.py",
        "new": ("                ledger.round_failed(round_no, user_arg, reason, run_status,\n"
                "                                    attempt)"),
        "old": ('                ledger.append({"type": "round_skipped", "round": round_no,\n'
                '                               "user_argument": user_arg})'),
        "needle": "[失败] 人机辩论循环",
    },
    {
        "name": "concede 必须终止辩论（不许空转到上限）",
        "why": "认输是正方给用户的诚实信号；不传播 ⇒ 用户被蒙在鼓里继续空转，"
               "轮数与 token 白白烧掉。",
        "file": ROOT / "legal_acps" / "human_debate.py",
        "new": '    if str(pro.get("stance") or "") == "concede":',
        "old": '    if str(pro.get("stance") or "") == "":',
        "needle": "[失败] 人机辩论终止",
    },
]


def _b(value: object) -> bytes:
    return str(value).encode("utf-8")


def syntax_ok(path: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)], capture_output=True
    ).returncode == 0


def run_verifier() -> str:
    r = subprocess.run(
        VERIFIER, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return (r.stdout or "") + (r.stderr or "")


def digests() -> dict[str, str]:
    return {str(c["file"]): hashlib.md5(c["file"].read_bytes()).hexdigest() for c in CASES}


def main() -> int:
    originals = {str(c["file"]): c["file"].read_bytes() for c in CASES}

    for c in CASES:
        raw = originals[str(c["file"])]
        if raw.count(_b(c["new"])) != 1 or _b(c["old"]) in raw:
            print(f"[步骤0] ❌ 前置不成立：{c['name']} 不是预期的'新形态'")
            return 1
    before = digests()
    print(f"[步骤0] ✅ {len(CASES)} 处均为新形态")

    try:
        for i, c in enumerate(CASES, 1):
            path: Path = c["file"]
            name, needle = str(c["name"]), str(c["needle"])
            original = originals[str(path)]

            broken = original.replace(_b(c["new"]), _b(c["old"]))
            if broken.count(_b(c["old"])) != 1 or _b(c["new"]) in broken:
                print(f"[{i}.1] ❌ {name}：替换未按预期发生")
                return 1
            path.write_bytes(broken)
            if not syntax_ok(path):
                print(f"[{i}.1] ❌ {name}：退回后文件语法错误 —— 本次元验证证明不了任何事")
                return 1
            print(f"[{i}.1] ✅ {name}：已退回旧形态（{path.name}），且文件仍语法正确")

            out = run_verifier()
            hit = needle in out
            print(f"[{i}.2] {'✅' if hit else '❌'} {name}：check_local 报出该失败 = {hit}")
            for line in out.splitlines():
                if needle in line:
                    print(f"          {line.strip()}")
            if not hit:
                print(f"[{i}.2] ⚠️ {name} 的断言没响 —— 它是空转的（或匹配词写错了）")
                return 1

            path.write_bytes(original)
            if not syntax_ok(path):
                print(f"[{i}.3] ❌ {name}：复原后语法错误")
                return 1
            print(f"[{i}.3] ✅ {name}：已复原")

        if digests() != before:
            print("[步骤4] ❌ 复原后 md5 与原文不一致")
            return 1
        out2 = run_verifier()
        green = ("[失败]" not in out2) and ("本地层面全部就绪" in out2)
        print(f"[步骤4] {'✅' if green else '❌'} 复原后整体转绿 = {green}")
        return 0 if green else 1
    finally:
        for c in CASES:
            path: Path = c["file"]
            if path.read_bytes() != originals[str(path)]:
                path.write_bytes(originals[str(path)])
                print(f"[finally] 已强制复原 {path.name}")


if __name__ == "__main__":
    raise SystemExit(main())
