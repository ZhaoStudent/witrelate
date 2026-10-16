"""临时探针：查候选 URL 与 CURATED_URLS 的关系 + 合集文定性命中次数。"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_acps.fact_store import FactStore  # noqa: E402
from tools.fetch_facts import CURATED_URLS  # noqa: E402

FALSE_PAT = re.compile(
    r"不属实|情况不实|信息不实|.{0,6}系谣言|属谣言|为不实信息|不实信息|"
    r"虚假信息|系编造|编造.{0,8}谣言|为谣言|是谣言|严重不实")

fs, _ = FactStore.load()
cands = [json.loads(l) for l in
         (ROOT / "legal_acps/rulings/gt_candidates.jsonl").read_text(
             encoding="utf-8").splitlines() if l.strip()]
curated = {d["url"] for d in CURATED_URLS}
print("URL 在 CURATED / 命中次数 / 谣言：")
for c in cands:
    f = next(x for x in fs if x.url == c["fact_url"])
    n = len(FALSE_PAT.findall(f.body or ""))
    tag = "CURATED" if c["fact_url"] in curated else "new"
    print(f"  {tag}  {n}次  {c['rumor_text'][:34]}")
