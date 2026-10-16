"""元验证：#123 跨进程委托链的三处要害锚点，退回旧形态必须让 check_local
2b 节的跨进程链断言变红。

背景：#123 的实现（2026-10-06 落地）与判据（check_local 2b 节）当日就已
完成，手册 §12.18 也做过「定点复现三条」——但那是**一次性的人肉验证**
（逐条退回实测 + md5 复原，跑完就没了）。本模块把那三条退回形态**持久化**
成可复跑的元验证用例：将来任何人改坏这三处中的任何一处，check_local 会
当场红，而不是等下一次真机事故来发现。

三处锚点各自防的旧形态（与手册 §12.18 的「定点复现三条」一一对应）：

  A. **入站不写 metadata**（`on_run_start`）——#123 的那个洞本身：
     挑到链却不写 `state.metadata` ⇒ `_base_path` 退化成 `(self_aic,)` ⇒
     跨进程的 A→B→A 在 B 眼里"派回 A"完全合法。判据应报
     「跨进程环 A→B→A 没被拦住」。
  B. **出站不并链**（`_with_chain_payload`）——把注入短路成"原样发出"：
     链根本没送出本进程 ⇒ 出站台账留下 `injected=False`。判据应报
     「有委派没能并进载荷（台账里留了 injected=False）」。
     ⚠️ 这条专门钉「并失败必须留痕」：一个"链没传过去而没人知道"的
     世界，与"链传过去了"在产物里长得一模一样。
  C. **接管时吃掉结构化数据**（`EvidenceInboxHooks._delegate_with_bytes`）
     ——收件箱 Hook 在有附件时会**重写**整个 `request`；不带走外层刚
     并进去的链标记 ⇒ 跨进程链深**恰恰在有附件的那几条链上**静默失效，
     而判据跑在没有附件那条路上全绿（「被你重写掉的那个值，还有谁在
     用？」）。判据应报「接管委派时把结构化数据吃掉了」。

⚠️ 三条都是 fail 形态：needle 在 check_local 输出里**且**退出码非 0
   （只看 needle 会放过「失败传递断掉」的坑，见 meta_verify_lead_recon 锚 A）。
⚠️ 退回形态的 `old` 与 `new` 互不为子串；退回后文件仍语法正确。
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
        "name": "入站不许挑到链不写 metadata（#123 的洞本身）",
        "why": "on_run_start 挑到上游的 debate_chain 却不写进 "
               "state.metadata ⇒ _base_path 退化成 (self_aic,)，"
               "跨进程的环与超深在下游眼里全部合法。这正是 #123 "
               "落地前「创新点在跨进程段一直空着」的那个洞。",
        "file": ROOT / "legal_acps" / "debate.py",
        "new": (
            "        chain = inbound_chain(parts)\n"
            "        self._record_inbound(chain)\n"
            "        if chain:\n"
            "            metadata[CHAIN_PAYLOAD_KEY] = list(chain)"
        ),
        "old": (
            "        chain = inbound_chain(parts)\n"
            "        self._record_inbound(chain)\n"
            "        if chain:\n"
            "            pass  # 退回形态：挑到链也不写 metadata（#123 之前的旧形态）"
        ),
        "needle": "跨进程环 A→B→A 没被拦住",
        "mode": "fail",
    },
    {
        "name": "出站不许把注入短路（并失败必须留痕）",
        "why": "_with_chain_payload 把注入结果丢掉、原样发出 ⇒ 链根本"
               "没送出本进程。判据抓的是台账里的 injected=False ——"
               "「链没传过去而没人知道」与「链传过去了」在产物里长得"
               "一模一样，排查只能靠这份留痕。",
        "file": ROOT / "legal_acps" / "debate.py",
        "new": (
            "        new_request, injected = request_with_chain(arguments.get(\"request\"), chain)"
        ),
        "old": (
            "        new_request, injected = arguments.get(\"request\"), False  # 退回形态：不并链"
        ),
        "needle": "有委派没能并进载荷",
        "mode": "fail",
    },
    {
        "name": "接管委派不许吃掉结构化数据（有附件的链不许静默失效）",
        "why": "EvidenceInboxHooks 接管时会用收件箱字节重写整个 request；"
               "不带走外层并进去的链标记 ⇒ 跨进程链深恰恰在有附件的那"
               "几条链上失效，而判据跑在没有附件那条路上全绿。两个 Hook"
               "碰同一个字段，改一处必须问另一处：被你重写掉的那个值，"
               "还有谁在用？",
        "file": ROOT / "legal_acps" / "evidence_hooks.py",
        "new": (
            "        for entry in plan.get(\"data_items\") or []:\n"
            "            parts.append(\n"
            "                DataPart(data=dict(entry[\"data\"]), metadata=dict(entry.get(\"metadata\") or {}))\n"
            "            )"
        ),
        "old": (
            "        # for entry in plan.get(\"data_items\") or []:  # 退回形态：接管时吃掉结构化数据\n"
            "        #     parts.append(\n"
            "        #         DataPart(data=dict(entry[\"data\"]), metadata=dict(entry.get(\"metadata\") or {}))\n"
            "        #     )"
        ),
        "needle": "接管委派时把结构化数据吃掉了",
        "mode": "fail",
    },
]


def _b(value: object) -> bytes:
    return str(value).encode("utf-8")


def syntax_ok(path: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)], capture_output=True
    ).returncode == 0


def run_verifier() -> tuple[str, int]:
    r = subprocess.run(
        VERIFIER, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return (r.stdout or "") + (r.stderr or ""), r.returncode


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
            mode = str(c.get("mode", "fail"))
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

            out, rc = run_verifier()
            if mode == "fail":
                # 🔴 needle 与退出码各是一半判据：只看 needle 会放过
                #    「失败传递断掉」的坑（[失败] 照打、退出码照 0）。
                hit = (needle in out) and rc != 0
                print(f"[{i}.2] {'✅' if hit else '❌'} {name}："
                      f"check_local 报出该失败且退出非 0 = {hit}"
                      f"（rc={rc}、needle={'在' if needle in out else '不在'}输出）")
                for line in out.splitlines():
                    if needle in line:
                        print(f"          {line.strip()}")
                        break
                if not hit:
                    print(f"[{i}.2] ⚠️ {name} 的断言没响（或失败没传出去）—— 它是空转的")
                    return 1
            else:  # pragma: no cover —— 本模块三条都是 fail 形态
                hit = (needle not in out) and rc == 0 and ("本地层面全部就绪" in out)
                print(f"[{i}.2] {'✅' if hit else '❌'} {name}：absent 形态 = {hit}")
                if not hit:
                    return 1

            path.write_bytes(original)
            if not syntax_ok(path):
                print(f"[{i}.3] ❌ {name}：复原后语法错误")
                return 1
            print(f"[{i}.3] ✅ {name}：已复原")

        if digests() != before:
            print("[步骤4] ❌ 复原后 md5 与原文不一致")
            return 1
        out2, rc2 = run_verifier()
        green = ("[失败]" not in out2) and ("本地层面全部就绪" in out2) and rc2 == 0
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
