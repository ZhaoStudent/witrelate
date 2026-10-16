"""只读探查：每个角色的 access 文件里，条目究竟是「被调用」还是「我调别人」。

为什么要单独测这一条
══════════════════
`上线操作手册.md` §12.13 说：「一次 RPC 在双方各自的文件里各记一条，
在自己文件里 `callee == self` 的就是被调用，`caller == self` 的是调用」。
这句话同时允许两种实现：

  A) 文件只记**本端作为被调用方**的条目（callee 恒等于 self）
  B) 文件两个方向都记（同一文件里既有 callee==self 也有 caller==self）

两种实现下「数行数 = 被访问次数」的正确性完全不同：
  · A 下直接数行数是对的
  · B 下直接数行数会把**自己发出的调用**也算成"被访问"，数字翻倍

所以不能靠读文档下结论，只能靠真实数据分方向计数。
同时把 499 字节条目的方向分布一并打出 —— 因为"499=轮询"若成立，
轮询条目应当**只出现在被调用侧**（调用方轮询被调方取状态）。

只读，纯标准库。
"""

from __future__ import annotations

import collections
import glob
import json
import os
import sys

WIT = sys.argv[1] if len(sys.argv) > 1 else ".wit"


def load(path: str) -> list[dict]:
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def main() -> int:
    files = sorted(glob.glob(os.path.join(WIT, "*", "logs", "amp_access_*.jsonl")))
    # 角色 → 自身 AIC：文件名里就带着（amp_access_<AIC>.jsonl）
    self_aic: dict[str, str] = {}
    for fp in files:
        role = os.path.basename(os.path.dirname(os.path.dirname(fp)))
        base = os.path.basename(fp)
        self_aic[role] = base[len("amp_access_"): -len(".jsonl")]

    print(f"{'角色':<18}{'总数':>6}{'被调用':>8}{'我调人':>8}{'方向不明':>10}"
          f"{'499·被调用':>12}{'499·我调人':>12}")
    print("-" * 78)
    tot = collections.Counter()
    for fp in files:
        role = os.path.basename(os.path.dirname(os.path.dirname(fp)))
        me = self_aic[role]
        rows = load(fp)
        n_callee = n_caller = n_unclear = 0
        s_callee = s_caller = 0
        for r in rows:
            body = r.get("body") or {}
            caller = str((body.get("caller") or {}).get("aic") or "")
            callee = str((body.get("callee") or {}).get("aic") or "")
            size = (body.get("request") or {}).get("bodySizeBytes")
            is499 = size == 499
            if callee == me and caller != me:
                n_callee += 1
                s_callee += 1 if is499 else 0
            elif caller == me and callee != me:
                n_caller += 1
                s_caller += 1 if is499 else 0
            else:
                n_unclear += 1
        tot["callee"] += n_callee
        tot["caller"] += n_caller
        tot["unclear"] += n_unclear
        print(f"{role:<18}{len(rows):>6}{n_callee:>8}{n_caller:>8}{n_unclear:>10}"
              f"{s_callee:>12}{s_caller:>12}")
    print("-" * 78)
    print(f"{'合计':<18}{sum(tot.values()):>6}{tot['callee']:>8}"
          f"{tot['caller']:>8}{tot['unclear']:>10}")
    print()

    print("=== 各角色自身 AIC ===")
    for role, aic in sorted(self_aic.items()):
        print(f"    {role:<18}{aic}")
    print()
    print("=== 结论提示 ===")
    if tot["caller"] == 0 and tot["unclear"] == 0:
        print("    该文件**只记录本端作为被调用方**的条目 → 数行数即被访问次数（RPC 级）")
    elif tot["caller"] > 0:
        print(f"    ⚠ 文件里混有 {tot['caller']} 条「本端作为调用方」的条目 "
              "→ 直接数行数会把自家外呼也算成被访问")
    if tot["unclear"]:
        print(f"    ⚠ 有 {tot['unclear']} 条方向不明（caller/callee 缺失或都等于 self）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
