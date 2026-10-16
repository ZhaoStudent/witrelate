"""在平台的 OpenAPI 契约里找「公开 / 私有」相关的字段名与取值。

用法（在 witrelate 目录下，服务器上）：
    ./.venv/bin/python -m tools.inspect_visibility
"""

from __future__ import annotations

import json
import pathlib
import re

SPEC = pathlib.Path("/tmp/wt_spec.json")
KEYWORDS = ("visib", "public", "private", "publish", "scope", "release", "disclos", "open")


def walk(node, path=""):
    """递归遍历 JSON，输出命中关键字的键。"""
    hits = []
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else k
            if any(w in k.lower() for w in KEYWORDS):
                if isinstance(v, dict):
                    info = f"type={v.get('type')} enum={v.get('enum')} default={v.get('default')}"
                    desc = (v.get("description") or "")[:110]
                else:
                    info = repr(v)[:110]
                    desc = ""
                hits.append((here, info, desc))
            hits += walk(v, here)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits += walk(v, f"{path}[{i}]")
    return hits


def main() -> None:
    if not SPEC.exists():
        raise SystemExit(f"缺少 {SPEC}，请先下载：curl -sS https://wt.ioa.pub/registry-server/openapi.json -o /tmp/wt_spec.json")
    spec = json.loads(SPEC.read_text(encoding="utf-8"))

    print("=" * 72)
    print("一、契约中与「公开/私有」相关的键")
    print("=" * 72)
    found = walk(spec)
    if not found:
        print("  （无）")
    for here, info, desc in found:
        print(f"  {here}\n      {info}\n      {desc}")

    print()
    print("=" * 72)
    print("二、POST /api/v1/agent/client 的请求体字段（完整）")
    print("=" * 72)
    op = spec["paths"].get("/api/v1/agent/client", {}).get("post", {})
    rb = (op.get("requestBody") or {}).get("content") or {}
    for ct, media in rb.items():
        sch = media.get("schema", {})
        if "$ref" in sch:
            name = sch["$ref"].split("/")[-1]
            sch = spec.get("components", {}).get("schemas", {}).get(name, {})
            print(f"  (schema: {name})")
        print(f"  content-type = {ct}")
        print(f"  required     = {sch.get('required')}")
        for k, v in (sch.get("properties") or {}).items():
            t = v.get("type") or v.get("$ref", "?")
            print(f"    - {k}: {t}  default={v.get('default')}  enum={v.get('enum')}")
            d = (v.get("description") or "").strip()
            if d:
                print(f"        {d[:150]}")

    print()
    print("=" * 72)
    print("三、全文中出现 'visib' 的原文片段")
    print("=" * 72)
    raw = SPEC.read_text(encoding="utf-8")
    for m in sorted(set(re.findall(r".{80}visib.{110}", raw, re.I))):
        print("  ...", m)


if __name__ == "__main__":
    main()
