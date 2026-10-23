set -u
cd /home/ubuntu/witrelate || exit 1
export PYTHONPATH=/home/ubuntu/witrelate
export ACPS_BACKEND=wt

echo '=== DISCOVERY PROBE (index-only, no E2E) ==='
cat > /tmp/_probe_discovery.py <<'PYEOF'
"""ADP discovery 复查：目标「网络裁判」是否已被索引。只读。"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, "/home/ubuntu/witrelate")

from dotenv_lite import load_env

load_env()

from wit.acps.discovery import AcpsDiscoveryClient  # noqa: E402

from legal_acps.acps_config import build_config  # noqa: E402
from legal_acps import wt_compat  # noqa: E402

TARGET_AIC = "1.2.156.3088.1.BUPT.81M3FX.83WI6B.TF6QRX.0E91"
NS_PREFIX = "1.2.156.3088.1.BUPT.81M3FX"
QUERIES = ["网络裁判", "事实核查", "事实核查 网络裁判 裁定"]


async def main() -> None:
    wt_compat.apply()
    cfg = build_config(role="leader")
    client = AcpsDiscoveryClient(acps=cfg)

    seen: dict[str, str] = {}
    failed: list[str] = []
    for q in QUERIES:
        try:
            resp = await client.discover(query=q, limit=50, include_unavailable=False)
        except Exception as exc:  # noqa: BLE001
            print(f"### 查询 {q!r} -> ERR {type(exc).__name__}: {str(exc)[:160]}")
            failed.append(q)
            continue
        acs_map = (resp.get("result") or {}).get("acsMap") or {}
        print(f"### 查询 {q!r} -> {len(acs_map)} 条")
        for aic, acs in acs_map.items():
            if not isinstance(aic, str):
                continue
            name = acs.get("name") if isinstance(acs, dict) else None
            seen[aic] = name
            short = ".".join(aic.split(".")[-2:])
            mark = ""
            if aic == TARGET_AIC:
                mark = "   <== 目标"
            elif aic.startswith(NS_PREFIX):
                mark = "   <== 本账号"
            print(f"    - {name}  (…{short}){mark}")
        print()

    print("=== 汇总：去重后本账号 AIC ===")
    print(f"  去重共 {len(seen)} 条，失败查询 {len(failed)} 个")
    for aic, name in seen.items():
        if aic.startswith(NS_PREFIX):
            tag = "[目标]" if aic == TARGET_AIC else "[本账号]"
            print(f"  {tag}  {name}  {aic}")
    print()
    hit = TARGET_AIC in seen
    ns_hits = [a for a in seen if a.startswith(NS_PREFIX)]
    print(f"TARGET_HIT={hit}")
    print(f"NS_HIT_COUNT={len(ns_hits)}")
    for a in ns_hits:
        print(f"NS_HIT: {seen[a]}  {a}")
    print(f"FAILED_QUERIES={failed}")
    sys.exit(0 if hit else 3)


if __name__ == "__main__":
    asyncio.run(main())
PYEOF
./.venv/bin/python /tmp/_probe_discovery.py
PE=$?
rm -f /tmp/_probe_discovery.py
echo "PROBE_EXIT=$PE"
echo '=== DONE ==='
