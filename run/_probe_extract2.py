"""临时探针：扩提取器（body 网传无引号形态）能从「提取不到」池子捞几条。"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_acps.fact_store import FactStore  # noqa: E402
from tools.fetch_facts import CURATED_URLS  # noqa: E402

BODY_WC2 = re.compile(r"网传([^，。！？\s“”\"]{6,60}?)的?(说法|信息|消息|视频|图片|文章|言论|传言)")
FALSE_PAT = re.compile(
    r"不属实|情况不实|信息不实|.{0,6}系谣言|属谣言|为不实信息|不实信息|"
    r"虚假信息|系编造|编造.{0,8}谣言|为谣言|是谣言|严重不实")

fs, _ = FactStore.load()
curated = {d["url"] for d in CURATED_URLS}

TITLE_Q = re.compile(r"^\s*[“\"]?([^？?”\"]{8,60})[”\"]?\s*[？?]")
TITLE_QUOTED = re.compile(r"[“\"]([^”\"]{8,60})[”\"]")
BODY_WANGCHUAN = re.compile(r"网传[“\"]([^”\"]{6,80})[”\"]")


def old_extract(f):
    t = (f.title or "").strip()
    m = TITLE_Q.match(t)
    if m:
        raw = m.group(1)
    else:
        m = TITLE_QUOTED.search(t)
        if m:
            raw = m.group(1)
        else:
            m = BODY_WANGCHUAN.search(f.body or "")
            raw = m.group(1) if m else ""
    return raw.split("|")[-1].strip() if raw else ""


n = 0
for f in fs:
    if f.url in curated or old_extract(f):
        continue
    m = BODY_WC2.search(f.body or "")
    if not m:
        continue
    rumor = m.group(1)
    body = f.body or ""
    aligned = any(
        any(rumor[i:i+4] in body[max(0, mm.start()-120):mm.end()+120]
            for i in range(len(rumor) - 3))
        for mm in FALSE_PAT.finditer(body))
    n += 1
    print(f"  {'OK ' if aligned else 'no '} [{rumor[:36]}] {f.title[:40]}")
print(f"\n扩提取器捞到 {n} 条")
