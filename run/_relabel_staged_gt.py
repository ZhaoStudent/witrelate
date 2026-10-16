"""临时工具：gt 重标 c-staged-01 的 source 到最新轮 + error_reason 改写为历史教训措辞。"""
import json

GT = "legal_acps/rulings/ground_truth.jsonl"
OLD_SRC = "ruling-20261007T095103Z.json"
NEW_SRC = "ruling-20261011T101557Z.json"

raw = open(GT, "rb").read()
assert raw.count(b"\r\n") == 0, "基线必须纯 LF"

lines = raw.decode("utf-8").splitlines()
hit = 0
for i, ln in enumerate(lines):
    d = json.loads(ln)
    if d["source"] == OLD_SRC:
        assert "摆拍" in d.get("basis", "") or "寒假作业" in d.get("error_reason", ""), "锚定失败"
        d["source"] = NEW_SRC
        d["error_reason"] = (
            "历史教训（旧轮 20261007T042339Z/043608Z 曾犯，最新两轮已稳）："
            "命题偷换——把「视频确实发布过、通报属实」当成裁定对象；"
            "裁定对象应是「捡到作业」本身。20261007T095103Z 与 20261011T101557Z "
            "两轮均立对命题判「不实」。"
        )
        lines[i] = json.dumps(d, ensure_ascii=False)
        hit += 1

assert hit == 1, f"应命中 1 条，实际 {hit}"

new_raw = ("\n".join(lines) + "\n").encode("utf-8")
assert new_raw.count(b"\r\n") == 0, "写后必须纯 LF"
open(GT, "wb").write(new_raw)

# 写后自检
back = [json.loads(x) for x in open(GT, encoding="utf-8").read().splitlines()]
assert len(back) == 22, f"行数 {len(back)}"
assert sum(1 for d in back if d["source"] == NEW_SRC) == 1, "新 source 应恰 1 条"
assert sum(1 for d in back if d["source"] == OLD_SRC) == 0, "旧 source 应清零"
srcs = [d["source"] for d in back]
assert len(srcs) == len(set(srcs)), "source 重复"
print("OK：c-staged-01 gt 重标到最新轮，error_reason 已改写为历史教训措辞，22 行纯 LF")
