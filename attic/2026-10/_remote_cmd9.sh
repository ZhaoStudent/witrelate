set -u
cd /home/ubuntu/witrelate || exit 1
export PYTHONPATH=/home/ubuntu/witrelate
export ACPS_BACKEND=wt

echo '=== DISCOVERY PROBE ==='
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
if [ "$PE" -ne 0 ]; then
  echo '=== SKIP E2E: target not indexed ==='
  exit 0
fi

echo '=== E2E START ==='
ls output/ruling-*.json 2>/dev/null | sort > /tmp/_pre_ruling.txt
echo "PRE_RULING_COUNT=$(wc -l < /tmp/_pre_ruling.txt)"
INPUT='请对这条网传信息做事实核查并发起辩论：网传某学校食堂使用过期食材，学生堵门讨要说法，据称后厨画面是上周拍摄的。'
echo "INPUT_LEN=${#INPUT}"
./.venv/bin/python -m legal_acps.run_agent leader --label automation --input "$INPUT"
echo "RUN_EXIT=$?"

echo '=== POST ruling ==='
ls output/ruling-*.json 2>/dev/null | sort > /tmp/_post_ruling.txt
echo "POST_RULING_COUNT=$(wc -l < /tmp/_post_ruling.txt)"
echo '--- NEW ruling files ---'
comm -13 /tmp/_pre_ruling.txt /tmp/_post_ruling.txt
echo '--- output tail ---'
ls -la --time-style=long-iso output/ | tail -20

echo '=== leader runs.jsonl tail ==='
tail -2 .wit/leader/runs.jsonl 2>/dev/null

echo '=== verdict check ==='
V=$(ls -t output/verdict-*.json 2>/dev/null | grep -v '\.meta\.json' | head -1)
echo "LATEST_VERDICT=$V"
./.venv/bin/python - "$V" <<'PYEOF2'
import json
import sys

p = sys.argv[1]
try:
    d = json.load(open(p, encoding="utf-8"))
except Exception as e:  # noqa: BLE001
    print("PARSE_ERR", e)
    raise SystemExit(0)
text = json.dumps(d, ensure_ascii=False)
for kw in ["未找到网络裁判", "网络裁判", "无法判定", "仍在进行中", "尚未返回", "未能获取"]:
    print(f"  {kw}: {text.count(kw)}")
print("  truth_score:", d.get("truth_score"))
print("  harm_index:", d.get("harm_index"))
print("  consistency_note:", str(d.get("consistency_note"))[:700])
print("  conclusion:", str(d.get("conclusion"))[:900])
PYEOF2
echo '=== DONE ==='
