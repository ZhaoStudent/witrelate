"""解析 Registry 的 OpenAPI 契约，把「登录 / 注册 / 审核」相关接口的真实字段挖出来。

信息来源：https://www.ioa.pub/registry/openapi.json（官方 FastAPI 自动生成的规范）
比反编译字节码可靠得多。
"""

from __future__ import annotations

import json
import re

import httpx

SPEC_URL = "https://www.ioa.pub/registry/openapi.json"

spec = httpx.get(SPEC_URL, timeout=40.0).json()
paths = spec.get("paths", {})
components = spec.get("components", {}).get("schemas", {})

print(f"标题   : {spec['info']['title']}")
print(f"版本   : {spec['info']['version']}")
print(f"接口数 : {len(paths)}")
print()

KEYWORDS = ("auth", "login", "token", "register", "agent", "enroll", "verif", "identity", "review", "approv", "credential", "eab")

print("=" * 78)
print("全部接口（首行路径，次行摘要）")
print("=" * 78)
for path, ops in sorted(paths.items()):
    for method, op in ops.items():
        if method not in ("get", "post", "put", "patch", "delete"):
            continue
        summary = (op.get("summary") or "").strip()
        mark = "★" if any(k in path.lower() for k in KEYWORDS) else " "
        print(f"{mark} {method.upper():<6} {path:<52} {summary}")

print()
print("=" * 78)
print("重点接口的请求体字段")
print("=" * 78)


def resolve(ref: str) -> dict:
    name = ref.rsplit("/", 1)[-1]
    return components.get(name, {})


def dump_schema(schema: dict, indent: str = "    ") -> None:
    if "$ref" in schema:
        schema = resolve(schema["$ref"])
    required = set(schema.get("required", []))
    props = schema.get("properties", {})
    if not props:
        print(f"{indent}(无字段定义)")
        return
    for name, meta in props.items():
        if "$ref" in meta:
            meta = resolve(meta["$ref"])
        if meta.get("anyOf") or meta.get("allOf") or meta.get("oneOf"):
            variants = meta.get("anyOf") or meta.get("allOf") or meta.get("oneOf")
            types = []
            for v in variants:
                if "$ref" in v:
                    types.append(resolve(v["$ref"]).get("title", "object"))
                else:
                    types.append(v.get("type", "?"))
            type_str = " | ".join(types)
        else:
            type_str = meta.get("type") or meta.get("title") or "?"
        if type_str == "array":
            items = meta.get("items", {})
            inner = items.get("type") or (resolve(items["$ref"]).get("title", "object") if "$ref" in items else "?")
            type_str = f"array[{inner}]"
        flag = "必填" if name in required else "可选"
        desc = (meta.get("description") or "").replace("\n", " ")[:70]
        print(f"{indent}{name:<26} {type_str:<24} {flag}  {desc}")


for path, ops in sorted(paths.items()):
    if not any(k in path.lower() for k in KEYWORDS):
        continue
    for method, op in ops.items():
        if method not in ("post", "put", "patch", "delete"):
            continue
        print(f"\n▶ {method.upper()} {path}")
        if op.get("summary"):
            print(f"  摘要: {op['summary']}")
        if op.get("description"):
            print(f"  说明: {op['description'][:200]}")
        body = op.get("requestBody", {}).get("content", {})
        if body:
            for ctype, media in body.items():
                print(f"  请求体 ({ctype}):")
                dump_schema(media.get("schema", {}))
        params = op.get("parameters", [])
        if params:
            print("  路径/查询参数:")
            for p in params:
                req = "必填" if p.get("required") else "可选"
                print(f"    {p['name']:<24} {p.get('in'):<8} {req}")

print()
print("=" * 78)
print("注册相关 schema 全字段")
print("=" * 78)
for name, schema in sorted(components.items()):
    if not any(k in name.lower() for k in ("register", "enroll", "agent", "verif", "identity", "auth", "login", "credential")):
        continue
    print(f"\n◆ {name}")
    dump_schema(schema)
