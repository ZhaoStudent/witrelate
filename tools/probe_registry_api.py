"""查 wt 注册局的 agent 相关接口细节。

重点：`DELETE /api/v1/agent/client/{agent_id}` 是**软删除**（is_deleted=True），
发现服务不跟随。看看有没有硬删除/清除的接口或参数。
"""

from __future__ import annotations

import json

import httpx

SPEC_URL = "https://wt.ioa.pub/registry-server/openapi.json"
METHODS = ("get", "post", "put", "patch", "delete")


def dump_op(path: str, method: str, op: dict) -> None:
    print(f"  --- {method.upper()}")
    for key in ("summary", "description"):
        val = op.get(key)
        if val:
            print(f"      {key:<11}: {str(val)[:400]}")
    for prm in op.get("parameters") or []:
        sch = prm.get("schema") or {}
        name = prm.get("name")
        loc = prm.get("in")
        req = prm.get("required")
        default = sch.get("default")
        desc = str(prm.get("description"))[:140]
        print(f"      param      : {name} in={loc} required={req} default={default} desc={desc}")
    body = (op.get("requestBody") or {}).get("content") or {}
    for ctype, spec in body.items():
        print(f"      body({ctype}) : {json.dumps(spec.get('schema'), ensure_ascii=False)[:260]}")


def main() -> None:
    spec = httpx.get(SPEC_URL, timeout=30).json()
    paths = spec.get("paths") or {}
    print(f"共 {len(paths)} 条路径；spec title = {(spec.get('info') or {}).get('title')}")
    print()

    interest = [p for p in sorted(paths) if "agent" in p]
    print("=== agent 相关路径 ===")
    for p in interest:
        ms = ",".join(sorted(m.upper() for m in paths[p] if m in METHODS))
        print(f"  {ms:22} {p}")
    print()

    for target in ("/api/v1/agent/client", "/api/v1/agent/client/{agent_id}"):
        if target not in paths:
            continue
        print("=" * 66)
        print(f"### {target}")
        for method in METHODS:
            op = paths[target].get(method)
            if op:
                dump_op(target, method, op)
        print()


if __name__ == "__main__":
    main()
