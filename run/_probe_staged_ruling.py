"""临时探针：核 c-staged-01 新一轮裁定的命题立题与结论。"""
import json

d = json.load(open("output/ruling-20261011T101557Z.json", encoding="utf-8"))
print("event:", d["event"][:100])
print("verdict:", d["verdict"], "conf:", d["confidence"])
print()
print("reasoning 前600:")
print(d["reasoning"][:600])
print()
print("basis[0]:", str(d.get("basis") or [""])[0][:200] if isinstance(d.get("basis"), list) else str(d.get("basis"))[:200])
