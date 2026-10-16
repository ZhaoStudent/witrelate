"""列出 ACS 模型的权威字段清单，并查找「公开/私有」相关字段。

用法（在 witrelate 目录下，服务器上）：
    ./.venv/bin/python -m tools.inspect_acs_model
"""

from __future__ import annotations

from acps_sdk.acs import AgentCapabilitySpec

KEYWORDS = ("visib", "public", "private", "scope", "publish", "access", "disclos", "open")


def describe(model) -> None:
    fields = getattr(model, "model_fields", None) or {}
    print(f"字段数：{len(fields)}")
    for name, f in fields.items():
        alias = getattr(f, "alias", None)
        default = getattr(f, "default", None)
        required = getattr(f, "is_required", lambda: None)()
        desc = (getattr(f, "description", None) or "")[:90]
        print(f"  {name:24s} alias={str(alias):24s} required={required}  default={default!r}")
        if desc:
            print(f"      {desc}")


def main() -> None:
    print("=" * 72)
    print("AgentCapabilitySpec（ACS 根对象）的全部字段")
    print("=" * 72)
    describe(AgentCapabilitySpec)

    print()
    print("=" * 72)
    print("命中「公开/私有」类关键词的字段")
    print("=" * 72)
    fields = AgentCapabilitySpec.model_fields
    hits = [
        (n, f)
        for n, f in fields.items()
        if any(w in n.lower() for w in KEYWORDS)
        or any(w in (getattr(f, "alias", "") or "").lower() for w in KEYWORDS)
        or any(w in ((getattr(f, "description", "") or "")).lower() for w in KEYWORDS)
    ]
    if not hits:
        print("  （无）")
    for n, f in hits:
        print(f"  {n}  alias={getattr(f, 'alias', None)}")
        print(f"      desc: {(getattr(f, 'description', '') or '')[:200]}")

    print()
    print("=" * 72)
    print("各嵌套模型字段（provider / capabilities / endpoint / skill）")
    print("=" * 72)
    for attr in ("AgentProvider", "AgentCapabilities", "AgentEndPoint", "AgentSkill"):
        model = getattr(__import__("acps_sdk.acs", fromlist=[attr]), attr, None)
        if model is None:
            continue
        print(f"\n--- {attr}")
        describe(model)


if __name__ == "__main__":
    main()
