"""只读探查 AMP access 日志的真实字段分布（一次性探针，不改任何数据）。

存在理由
════════
`docs/上线操作手册.md` §12.13 记了一条结论：「`bodySizeBytes == 499` = 固定大小的
轮询/取状态请求，其余才是任务提交」。这条结论若为真，就能把 RPC 级日志折算成
任务级访问次数；若为假（499 只是某一天某一角色的巧合），照它写进代码就是
**把魔数当判据** —— 而这正是本项目反复踩的坑。

所以本探针**不假设**它成立，只把真实分布打出来，让数字自己说话：

  · 每个角色：总条数、路由集合、状态码集合、caller 分布
  · 全局：`bodySizeBytes` 的取值直方图（这是"499 判据"的原料）
  · 每个角色内部：bodySizeBytes 直方图（看 499 是不是**跨角色恒定**）

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
    print(f"access 日志文件数 = {len(files)}   目录 = {os.path.abspath(WIT)}")
    print()

    global_sizes: collections.Counter[int] = collections.Counter()
    global_routes: collections.Counter[str] = collections.Counter()
    global_status: collections.Counter[str] = collections.Counter()
    global_caller: collections.Counter[str] = collections.Counter()
    global_keys: collections.Counter[str] = collections.Counter()

    print(f"{'角色':<18}{'条数':>6}  {'路由':<20}{'状态':<12}{'caller 数':>9}")
    print("-" * 78)
    for fp in files:
        role = os.path.basename(os.path.dirname(os.path.dirname(fp)))
        rows = load(fp)
        per_size: collections.Counter[int] = collections.Counter()
        routes: collections.Counter[str] = collections.Counter()
        status: collections.Counter[str] = collections.Counter()
        callers: collections.Counter[str] = collections.Counter()
        for r in rows:
            body = r.get("body") or {}
            req = body.get("request") or {}
            resp = body.get("response") or {}
            caller = body.get("caller") or {}
            n = req.get("bodySizeBytes")
            if isinstance(n, int):
                per_size[n] += 1
                global_sizes[n] += 1
            routes[str(req.get("route") or req.get("url") or "?")] += 1
            global_routes[str(req.get("route") or req.get("url") or "?")] += 1
            status[str(resp.get("statusCode"))] += 1
            global_status[str(resp.get("statusCode"))] += 1
            ca = str(caller.get("aic") or "") or "(缺失)"
            callers[ca[-18:] or ca] += 1
            global_caller[ca] += 1
            for k in ("schema_version", "timestamp", "log_type", "log_id",
                      "correlation_id", "severity_text", "severity_number",
                      "body", "resource"):
                if k in r:
                    global_keys[k] += 1
        top_routes = ",".join(f"{k}×{v}" for k, v in routes.most_common(3))
        top_status = ",".join(f"{k}×{v}" for k, v in status.most_common(3))
        print(f"{role:<18}{len(rows):>6}  {top_routes:<20}{top_status:<12}{len(callers):>9}")
        # 每个角色内部：bodySizeBytes 直方图（前 6 个）
        hist = ", ".join(f"{s}×{c}" for s, c in per_size.most_common(6)) or "（无）"
        print(f"{'':<18}   大小直方图：{hist}")
    print()

    print("=== 全局 bodySizeBytes 直方图（「499 判据」的原料）===")
    for s, c in global_sizes.most_common(15):
        print(f"    {s:>8} 字节 × {c}")
    print()
    print(f"    不同取值个数 = {len(global_sizes)}   总样本 = {sum(global_sizes.values())}")
    print()
    print("=== 全局路由 ===")
    for k, v in global_routes.most_common():
        print(f"    {k!r} × {v}")
    print("=== 全局状态码 ===")
    for k, v in global_status.most_common():
        print(f"    {k} × {v}")
    print("=== 全局 caller（后 18 位）===")
    for k, v in global_caller.most_common():
        print(f"    {k[-18:] or k} × {v}")
    print("=== 顶层字段出现次数 ===")
    for k, v in global_keys.most_common():
        print(f"    {k} × {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
