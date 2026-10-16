"""离线导入法条库 —— verified-chinese-law-kb（M1–M8，2026-10-07 新增 law-advisor）。

数据来源（由维护者手动 clone，**运行时不出网** —— SSRF 纪律与 fact_store 同款）：

    git clone --depth 1 https://github.com/vickywu97/verified-chinese-law-kb.git

为什么选这个库（三个候选里）：
  · **律师逐字核验**：每条 verification_status=verified、带 verified_at 与
    官方 source_url（flk.npc.gov.cn 国家法律法规数据库）—— 溯源字段齐全，
    匹配本项目「溯源字段不许含糊」纪律；
  · **带版本轴**：effective_date + revision_of，防「旧法当新法」式幻觉；
  · **RAG-ready JSONL**：一行一条、字段稳定，零解析风险。
  （另两个候选：Chinese-Laws-folk 覆盖广但是未核验的连续文本 dump，噪声
  风险高；wisdomInterrogatory 是大模型项目、知识库不在 repo 内直接可用。）

收录范围：**M1–M8 共 8 部 2,327 条**（民法典 1260 / 刑法 505 / 公司法 266 /
税收征管 94 / 增值税 38 / 企业所得税 60 / 个税 22 / 专利法 82）。
🔴 M9 著作权法是上游 **internal（未发布）** 状态 ⇒ **刻意不收**——
「未发布的内部数据」与「正式发布的核验数据」在落库后长得一样，只能靠
导入时按上游发布状态过滤。M9 正式发布后重跑本脚本即可。

复查记录（2026-10-11，五件事之四）：
  · 上游 README 仍标「内部自用：未发布、未计入公开 release」；
  · laws_index.json 已登记 COPYRIGHT_LAW（order 9）但 release 未收录
    —— 上游自身不一致，按「未发布」从严处理；
  · 数据本体 10 条均 verified（2010/2020 两版总则第 1–5 条），质量没问题，
    但**仅总则 5 条**：收了 ⇒ 能力边界声明含「著作权法」而实务条文
    （侵权赔偿 / 合理使用）一条查不到 —— **薄收录 = 能力边界虚标，
    比不收更有害**（能力边界单一来源纪律：提示词 ⊆ 库 meta）。
  · 下次复查触发条件：上游扩至全文 + 正式计入 release。

许可证（数据 CC BY-SA 4.0）：来源与署名写进产物 meta 行 + 本 docstring。
用法（仓库根目录）：

    git clone --depth 1 https://github.com/vickywu97/verified-chinese-law-kb.git /tmp/kb
    ./.venv/bin/python -m tools.import_laws /tmp/kb
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "legal_acps" / "laws"
OUT_FILE = OUT_DIR / "law-articles.jsonl"

#: 只收**已发布**模块（M9 internal 不收，见 docstring）。
MODULES = ("M1_civil_code", "M2_criminal_law", "M3_company_law",
           "M4_tax_admin_law", "M5_vat_law", "M6_eit_law",
           "M7_iit_law", "M8_patent_law")

#: 必须存在的溯源字段（缺任何一条 ⇒ 整条丢弃并计数，不静默）。
REQUIRED = ("id", "law_code", "article_number", "content",
            "effective_date", "verification_status", "source_url")

SOURCE_NOTE = (
    "数据来源：verified-chinese-law-kb（github.com/vickywu97/verified-chinese-law-kb），"
    "条文经执业律师逐字比对官方来源核验；数据许可证 CC BY-SA 4.0。"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kb_dir", type=Path, help="clone 下来的仓库目录")
    args = ap.parse_args()
    kb = args.kb_dir

    # 法名映射：law_code -> 中文名（含别名，供检索文本拼进 gram 索引）
    index_file = kb / "knowledge_base" / "laws_index.json"
    try:
        laws_index = json.loads(index_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"找不到 {index_file} —— kb 目录不对？") from None
    names = {code: str(v.get("name") or "") for code, v in laws_index.items()}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    kept, dropped = [], []
    for mod in MODULES:
        p = kb / "modules" / mod / "statutes.jsonl"
        if not p.exists():
            raise SystemExit(f"缺少模块文件 {p} —— 上游结构变了？")
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                dropped.append(f"{mod}: 坏 JSON 行")
                continue
            missing = [k for k in REQUIRED if not rec.get(k)]
            if missing:
                dropped.append(f"{rec.get('id') or mod}: 缺 {missing}")
                continue
            # 🔴 只收核验通过的：rejected/pending 不许进库 ——
            #    「没核验的条文」与「核验过的条文」落库后长得一样。
            if str(rec.get("verification_status")) != "verified":
                dropped.append(f"{rec['id']}: status={rec.get('verification_status')}")
                continue
            kept.append({
                "id": str(rec["id"]),
                "law_code": str(rec["law_code"]),
                "law_name": names.get(str(rec["law_code"]), str(rec["law_code"])),
                "article_number": str(rec["article_number"]),
                "content": str(rec["content"]),
                "effective_date": str(rec["effective_date"]),
                "source_url": str(rec["source_url"]),
                "verified_at": str(rec.get("verified_at") or ""),
            })

    with open(OUT_FILE, "w", encoding="utf-8", newline="\n") as f:
        meta = {"type": "meta", "source": SOURCE_NOTE,
                "laws": sorted({r["law_name"] for r in kept}),
                "count": len(kept)}
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_law: dict[str, int] = {}
    for r in kept:
        by_law[r["law_name"]] = by_law.get(r["law_name"], 0) + 1
    print(f"导入完成：{len(kept)} 条 → {OUT_FILE}")
    for name, n in sorted(by_law.items(), key=lambda x: -x[1]):
        print(f"  · {name}: {n} 条")
    if dropped:
        print(f"⚠️ 丢弃 {len(dropped)} 条（如实报告）：")
        for d in dropped[:10]:
            print(f"    - {d}")
        if len(dropped) > 10:
            print(f"    …（其余 {len(dropped) - 10} 条略）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
