"""临时探针：核验并发扇出后的裁定书质量。"""
import json

d = json.load(open("output/ruling-20261011T100501Z.json", encoding="utf-8"))
print("event:", d["event"][:60])
print("verdict:", d["verdict"], "conf:", d["confidence"])
print("disputed:", len(d.get("disputed_points") or []), "条")
print("fallacies:", [f["kind"] for f in (d.get("fallacies") or [])])
basis = d.get("basis")
if isinstance(basis, list):
    print("basis:", " / ".join(str(b)[:150] for b in basis[:2]))
else:
    print("basis:", str(basis)[:200])
