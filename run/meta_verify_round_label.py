"""元验证：体验应用的**轮次/轮数**必须由服务端决定，不得采纳模型自报值。

这是同一病根的两处（"记录里的字段交给了被记录的那一方来写"）：

  A. 每轮 `Turn.round`  —— 旧写法 `int(out.get("round") or nxt)`
  B. 揭示 `rounds`      —— 旧写法 `self.state.reveal = judge_out`（直接采纳裁判自报）
                           退回形态取**那一行**的等价写法 `judge_out.get("rounds")`：
                           既不依赖字典其余部分（不再随增删漂移），又精准对应被测性质。

做法：把每处**退回旧形态**，要求 `verify_experience` 立刻报失败。

⚠️ 三条纪律（都是本项目踩过的坑）：
  1. 退回后的文件**必须仍然语法正确** —— 否则证明的只是"文件坏了会报错"，
     而不是"断言能抓住这个缺陷"（曾用 bash heredoc 写元验证脚本，`\\n` 被吃成字面量，
     整个元验证一度是空转的）。
  2. 匹配失败行要锚定 `[失败]` 前缀 —— 通过行的中文文案里本身就含"轮次"这些字，
     用裸词匹配会把"已通过"误判成"已报失败"。
  3. 整篇读写一律 `read_bytes()` / `write_bytes()` —— Windows 上 `write_text()`
     会把 `\\n` 翻成 `\\r\\n`，曾把源文件整篇变成 CRLF 并同步上服务器。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "legal_acps" / "experience" / "session.py"

CASES = [
    {
        "name": "每轮 round 标签",
        "new": b"                round=nxt,\n",
        "old": b'                round=int(out.get("round") or nxt),\n',
        "needle": "[失败] 诱导方轮次标签",
    },
    {
        "name": "揭示 rounds 轮数",
        # ⚠️ 这个锚点曾经是**整段字典字面量的快照**，于是 `session.py` 里那段
        #    一改就断 —— 它已经因此断过**三次**：加 `trap_groups`（话术分层）、
        #    加 `suspect_report`（当事人标记对照）、加降级分支与
        #    `trap_groups` 的输入二选一。三次它都**主动报了「前置不成立」并退出 1**，
        #    没有悄悄跳过 —— 这正是前置检查存在的意义（比"改完还绿"更危险的是
        #    "改完不跑了却看不出来"）。
        #    但"断三次"本身就是设计错误：**退回形态必须精准**。
        #    被测的性质只有一件 —— `rounds` 是不是由**服务端**算的；
        #    而旧形态就是"直接采纳裁判自报"（历史写法 `self.state.reveal = judge_out`）。
        #    所以锚点缩到**那一行**：既精准对应被测性质，又不再随字典增删漂移。
        "new": b'            "rounds": self.state.pro_rounds,\n',
        "old": b'            "rounds": judge_out.get("rounds"),\n',
        "needle": "[失败] 揭示的轮数",
    },
]


def syntax_ok() -> bool:
    r = subprocess.run(
        [sys.executable, "-m", "py_compile", str(TARGET)], capture_output=True
    )
    return r.returncode == 0


def run_verify() -> str:
    r = subprocess.run(
        [sys.executable, "-m", "tools.verify_experience"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (r.stdout or "") + (r.stderr or "")


def digest() -> str:
    return hashlib.md5(TARGET.read_bytes()).hexdigest()


def main() -> int:
    original = TARGET.read_bytes()

    # [0] 前提：每一处都必须是"新形态"
    for c in CASES:
        assert isinstance(c["new"], bytes) and isinstance(c["old"], bytes)
        if original.count(c["new"]) != 1 or c["old"] in original:
            print(f"[步骤0] ❌ 前置不成立：{c['name']} 不是预期的'新形态'")
            return 1
    before = digest()
    print(f"[步骤0] ✅ 文件为新形态，md5={before}；共 {len(CASES)} 处待验")

    try:
        for i, c in enumerate(CASES, 1):
            name = str(c["name"])
            new, old, needle = c["new"], c["old"], str(c["needle"])

            # [1] 退回旧形态
            broken = original.replace(new, old)  # type: ignore[arg-type]
            if broken.count(old) != 1 or new in broken:  # type: ignore[arg-type]
                print(f"[{i}.1] ❌ {name}：替换未按预期发生")
                return 1
            TARGET.write_bytes(broken)
            if not syntax_ok():
                print(f"[{i}.1] ❌ {name}：退回后文件语法错误 —— 本次元验证证明不了任何事")
                return 1
            print(f"[{i}.1] ✅ {name}：已退回旧形态，且文件仍语法正确")

            # [2] 断言必须报失败
            out = run_verify()
            hit = needle in out
            print(f"[{i}.2] {'✅' if hit else '❌'} {name}：verify_experience 报出该失败 = {hit}")
            for line in out.splitlines():
                if needle in line:
                    print(f"          {line.strip()}")
            if not hit:
                print(f"[{i}.2] ⚠️ {name} 的断言没响 —— 它是空转的（或匹配词写错了）")
                return 1

            # [3] 复原
            TARGET.write_bytes(original)
            if not syntax_ok():
                print(f"[{i}.3] ❌ {name}：复原后语法错误")
                return 1
            print(f"[{i}.3] ✅ {name}：已复原")

        # [4] 两处都复原后，md5 必须与原文一致，且整体转绿
        if digest() != before:
            print("[步骤4] ❌ 复原后 md5 与原文不一致")
            return 1
        out2 = run_verify()
        green = ("[失败]" not in out2) and ("全部符合预期" in out2)
        print(f"[步骤4] {'✅' if green else '❌'} 复原后整体转绿 = {green}")
        for line in out2.splitlines():
            if "全部符合预期" in line:
                print(f"          {line.strip()}")
        return 0 if green else 1
    finally:
        # 任何异常路径都保证文件回到原样
        if TARGET.read_bytes() != original:
            TARGET.write_bytes(original)
            print("[清理] 已把 session.py 恢复为原始内容")


if __name__ == "__main__":
    sys.exit(main())
