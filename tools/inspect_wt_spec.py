"""解析梧桐 registry-server 的 OpenAPI 契约，找出创建 Agent 的必填字段。

用法（服务器 ~/witrelate 下）：
    ./.venv/bin/python -m tools.inspect_wt_spec
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

SPEC_URL = "https://wt.ioa.pub/registry-server/openapi.json"
CACHE = pathlib.Path("/tmp/wt_spec.json")


def load_spec() -> dict:
    if CACHE.exists() and CACHE.stat().st_size > 1000:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    with urllib.request.urlopen(SPEC_URL, timeout=40) as resp:
        raw = resp.read().decode("utf-8")
    CACHE.write_text(raw, encoding="utf-8")
    return json.loads(raw)


def resolve(spec: dict, node, depth: int = 0):
    """把 $ref 展开（限制深度，避免循环）。"""
    if depth > 8:
        return {"_note": "max depth"}
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = spec
        for part in node["$ref"][2:].split("/"):
            target = target.get(part, {})
        return resolve(spec, target, depth + 1)
    out = {}
    for key, val in node.items():
        if key == "properties" and isinstance(val, dict):
            out[key] = {k: resolve(spec, v, depth + 1) for k, v in val.items()}
        elif key in ("items", "additionalProperties") and isinstance(val, dict):
            out[key] = resolve(spec, val, depth + 1)
        elif key in ("allOf", "anyOf", "oneOf") and isinstance(val, list):
            out[key] = [resolve(spec, v, depth + 1) for v in val]
        else:
            out[key] = val
    return out


def show_fields(schema: dict, indent: str = "    ", depth: int = 0) -> None:
    if depth > 4:
        return
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    for name, info in props.items():
        if not isinstance(info, dict):
            continue
        typ = info.get("type")
        if typ is None:
            for comb in ("anyOf", "oneOf", "allOf"):
                if comb in info:
                    typ = " | ".join(
                        (x.get("type") or x.get("$ref", "ref").split("/")[-1])
                        for x in info[comb]
                        if isinstance(x, dict)
                    )
                    break
        if typ == "array":
            item = info.get("items") or {}
            inner = item.get("type") or item.get("$ref", "?").split("/")[-1]
            typ = f"list[{inner}]"
        mark = "*" if name in required else " "
        desc = (info.get("description") or "").replace("\n", " ")[:70]
        print(f"{indent}{mark} {name}: {typ}   {desc}")
        if isinstance(info.get("properties"), dict):
            show_fields(info, indent + "    ", depth + 1)
        if isinstance(info.get("items"), dict) and info["items"].get("properties"):
            show_fields(info["items"], indent + "    ", depth + 1)


def main() -> None:
    spec = load_spec()
    print(f"契约: {spec['info']['title']} v{spec['info']['version']}")
    print(f"接口总数: {len(spec['paths'])}")
    print("（字段前的 * 表示必填）")

    targets = sys.argv[1:] or [
        "/api/v1/agent/client",
        "/api/v1/agent/client/{agent_id}",
    ]
    for path in targets:
        ops = spec["paths"].get(path)
        print("\n" + "=" * 72)
        print(f"PATH  {path}")
        if not ops:
            print("  (契约里没有这个路径)")
            continue
        for method in ("get", "post", "put", "patch", "delete"):
            op = ops.get(method)
            if not op:
                continue
            print(f"\n  {method.upper()}  {op.get('summary', '')}")
            if op.get("description"):
                print(f"    {op['description'][:200]}")
            rb = op.get("requestBody") or {}
            for ctype, media in (rb.get("content") or {}).items():
                print(f"    [body {ctype}] required={rb.get('required')}")
                show_fields(resolve(spec, media.get("schema") or {}))
            for param in op.get("parameters") or []:
                p = resolve(spec, param)
                flag = "*" if p.get("in") == "path" or p.get("required") else " "
                print(f"    {flag} param {p.get('name')} ({p.get('in')})")


if __name__ == "__main__":
    main()
