"""冒烟测试：看合成样本上各检查项的真实输出，用于校准阈值（不是断言脚本）。

样本设计刻意分两类：
  * **正样本**：应当被某几项抓到（拼接 / 克隆 / 元数据矛盾）
  * **负对照**：**不应**被抓到（干净照片、纯周期纹理）——
    负对照比正样本更重要，它守的是「不误报」。

样本构造器已抽到 `run/samples.py` —— **诊断脚本 `run/diag_grid_samples.py` 与本脚本
必须用同一批样本**，否则两边看到的数对不上，诊断就失去意义。

用法：`python -m run.probe_evidence`（`PYTHONPATH=.`）
"""

from __future__ import annotations

import numpy as np

from legal_acps.evidence import analyze
from run.samples import W, adobe_exif, jpeg, photo, png, recrop_jpeg

rng = np.random.default_rng(20260930)


def show(title: str, data: bytes, **kw: object) -> None:
    rep = analyze(data, **kw)  # type: ignore[arg-type]
    fired = [f.check for f in rep.findings if f.verdict == "提示异常"]
    print(f"\n===== {title} =====")
    print(f"  容器={rep.media_type} 尺寸={rep.width}x{rep.height} "
          f"综合={rep.suspicion} 触发={fired or '（无）'} 升级建议={len(rep.upgrades)} 条")
    for f in rep.findings:
        mark = {"未提示异常": "  ok", "提示异常": "  ! ", "无法判定": "  ? "}[f.verdict]
        print(f"{mark} {f.check:<16} {f.verdict:<6} {f.detail[:76]}")


print("############ 负对照（应当不触发，或只在预期项上触发）############")
show("N1. 干净 PNG（低频明暗 + 局部结构 + 噪声）", png(photo("structured")))
show("N2. 干净 JPEG q85", jpeg(photo("structured"), 85))
show("N3. 纯周期纹理（负对照：不应报克隆）", jpeg(photo("periodic"), 92))

print("\n############ 正样本（应当被抓到）############")
split = photo("structured").copy().astype(np.float32)
split[:, : W // 2] = split[:, : W // 2] * 0.55 + 40
split[:, W // 2:] += rng.normal(0, 11.0, size=split[:, W // 2:].shape)
show("P1. 拼接（左右噪声水平差异大）",
     jpeg(np.clip(split, 0, 255).astype(np.uint8), 92))

clone = photo("structured").copy()
# 位移取 312px = 39 × 8 —— **必须是块步长 8 的整数倍**，否则源块与目标块落在
# 不同的格点上，特征对不上（本实现以 8px 为块步长匹配，见 check_copy_move 的 caveat）。
clone[150:214, 160:224] = clone[150:214, 472:536]
show("P2. 复制-移动（64×64 块贴到 312px 之外）", jpeg(clone, 92))

exif = adobe_exif()
show("P3. 元数据矛盾（Software=Photoshop / 无相机 / 时间戳不一致）",
     jpeg(photo("structured"), 88, exif=exif))

# P4 是 block_grid 的**唯一正样本**：裁剪错位 4px 后用 q95 重编码。
# 预期：检出「两个不同相位的块网格」，相位偏移 4 像素。
show("P4. 裁剪错位 4px + q95 重编码（块网格相位偏移）",
     recrop_jpeg(jpeg(photo("structured"), 85), dx=4, dy=4))

print("\n############ 边界 ############")
try:
    analyze(b"%PDF-1.7 not an image")
except ValueError as exc:
    print("\n  E1. 非图像输入 → ValueError（预期）：", exc)

import io as _io  # noqa: E402

big = photo("structured", w=2400, h=1800)
rep = analyze(png(big))
print(f"\n  E2. 大图裁剪：原图 {big.shape[1]}x{big.shape[0]} → 报告 "
      f"{rep.width}x{rep.height}（应为原图尺寸）")
print("      crop_note =", rep.crop_note[:60])

print("\n############ image part 的模型侧 schema 关系断言 ############")
from wit.acps import tool_support as ts  # noqa: E402

variants = {v["properties"]["type"]["const"]: v for v in ts.TOOL_CONTENT_PART_SCHEMA["oneOf"]}
img = variants["image"]
print("  image required =", img["required"])
print("  uri 描述 =", img["properties"]["uri"]["description"])
print("  含 bytes 字段 =", "bytes" in img["properties"])
