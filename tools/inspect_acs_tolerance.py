"""检查平台 AgentCreate.acs 字段的校验宽容度，以及 agent 状态机定义。

用法（在 witrelate 目录下，服务器上）：
    ./.venv/bin/python -m tools.inspect_acs_tolerance
"""

from __future__ import annotations

import json
import pathlib
import re

SPEC = pathlib.Path("/tmp/wt_spec.json")


def show(node, indent=2):
    print(json.dumps(node, ensure_ascii=False, indent=indent)[:1800])


def main() -> None:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    schemas = spec.get("components", {}).get("schemas", {})

    print("=" * 72)
    print("一、AgentCreate / AgentUpdate 的完整 schema")
    print("=" * 72)
    for name in ("AgentCreate", "AgentUpdate"):
        if name in schemas:
            print(f"\n--- {name}")
            show(schemas[name])
        else:
            print(f"\n--- {name}: （不存在）")

    print()
    print("=" * 72)
    print("二、所有名字里带 acs / Agent 的 schema，逐个看 acs 字段怎么定义")
    print("=" * 72)
    for name, s in schemas.items():
        target = (s.get("properties") or {}).get("acs")
        if target is not None:
            print(f"\n--- {name}.acs =")
            show(target)

    print()
    print("=" * 72)
    print("三、契约全文里 'acs' 的定义片段（看是否 anyOf / additionalProperties）")
    print("=" * 72)
    raw = SPEC.read_text(encoding="utf-8")
    for m in sorted(set(re.findall(r'"acs":\s*\{.{0,260}', raw)))[:12]:
        print("  ", m.replace("\n", " ")[:280])
        print()

    print("=" * 72)
    print("四、状态枚举（全文搜 DRAFT/PENDING/APPROVED/REJECTED）")
    print("=" * 72)
    for kw in ("DRAFT", "PENDING", "APPROVED", "REJECTED", "status"):
        cnt = len(re.findall(kw, raw))
        print(f"  {kw:10s} 出现 {cnt} 次")
    for m in sorted(set(re.findall(r'.{60}status.{100}', raw)))[:6]:
        print("   ...", m.replace("\n", " "))


if __name__ == "__main__":
    main()
