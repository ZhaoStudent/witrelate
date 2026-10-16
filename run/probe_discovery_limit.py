# -*- coding: utf-8 -*-
"""只读探查：区分「索引丢失」与「top-N 截断」。

背景：平台的 ADP discovery 一直召回不到本项目的自研 Agent
（16:46: 21 条里仅 2 条是我们的；18:52: 2/6；21:05: 0/6）。
两种可能的成因，处置方式完全不同：

  A. **索引丢失** —— 我们的 Agent 压根不在索引里。→ 平台侧问题，我们无能为力。
  B. **top-N 截断** —— 在索引里，但语义排序把我们挤出了前 limit 名。→ 可调。

判据：对**同一批查询词**用不同 limit 各查一次，看本账号前缀下的召回条数是否随 limit 上升。

    python run/probe_discovery_limit.py            # 默认 10 / 20 / 50
    python run/probe_discovery_limit.py 10 100

全程只读，不注册、不改服务。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv_lite import load_env  # noqa: E402

load_env()

from wit.acps.discovery import AcpsDiscoveryClient  # noqa: E402

from legal_acps import wt_compat  # noqa: E402
from legal_acps.acps_config import build_config  # noqa: E402
from tools.check_discovery_pollution import QUERIES  # noqa: E402

#: 本账号的 AIC 前缀（10 段 AIC 的前 7 段为账号级）。
PREFIX = "1.2.156.3088.1.BUPT.81M3FX"


async def probe(limit: int) -> tuple[int, int]:
    """返回 (去重后总条数, 其中属于本账号的条数)。"""
    cfg = build_config(role="leader")
    client = AcpsDiscoveryClient(acps=cfg)
    seen: dict[str, str | None] = {}
    for q in QUERIES:
        try:
            resp = await client.discover(query=q, limit=limit, include_unavailable=False)
        except Exception as exc:  # noqa: BLE001
            print(f"    [ERR] query={q!r}: {type(exc).__name__}: {str(exc)[:110]}")
            continue
        acs_map = (resp.get("result") or {}).get("acsMap") or {}
        for aic, acs in acs_map.items():
            if isinstance(aic, str):
                seen[aic] = acs.get("name") if isinstance(acs, dict) else None
    ours = [a for a in seen if a.startswith(PREFIX)]
    return len(seen), len(ours)


async def main() -> int:
    wt_compat.apply()
    limits = [int(a) for a in sys.argv[1:]] or [10, 20, 50]
    print(f"（已应用 wt 兼容补丁：{wt_compat.is_applied()}；查询词 {len(QUERIES)} 个）\n")
    results: list[tuple[int, int, int]] = []
    for lim in limits:
        total, ours = await probe(lim)
        results.append((lim, total, ours))
        print(f"  limit={lim:<4} 去重后共 {total:<3} 条；本账号 {ours} 条")
    print()
    if all(ours == 0 for _, _, ours in results):
        print("结论：**任何 limit 下本账号都是 0 条 ⇒ 更可能是「索引丢失」**（平台侧），")
        print("      而不是被排序截断 —— 调 limit 或换查询词都救不回来。")
    elif results[-1][2] > results[0][2]:
        print("结论：**本账号召回随 limit 上升 ⇒ 是「top-N 截断」**（排序问题），")
        print("      可以从查询词/limit 入手改善。")
    else:
        print("结论：本账号有召回但数量不随 limit 变化，需人工判断。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
