"""诊断：把每个样本的 **8 个偏移比值全打出来**，看清假阳性到底从哪来。

为什么需要它：冒烟测试只告诉了我们"报了什么"，没有告诉"为什么报"。
`block_grid` 的两道旧闸门看起来都合理，但 N2/P3 照样误报 ——
不看完整比值数组就只能靠猜，而"靠猜"正是本项目吃过亏的地方
（历史上 `check_flat_noise`、`check_copy_move` 的假阳性都是猜了两轮才修对）。

用法：`python -m run.diag_grid_samples`（`PYTHONPATH=.`）
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

from legal_acps.evidence import analyze
from legal_acps.evidence.forensics import block_grid_ratios
from run.samples import W, adobe_exif, jpeg, paste_patch_jpeg, photo, png, recrop_jpeg

SEP = "─" * 78
STATS = ("rank", "frac")


def profile(title: str, data: bytes, **kw: object) -> None:
    """打印样本自身统计量 + 两轴 8 偏移比值（两种统计量）+ 检测项的最终判定。

    ⚠️ **先打样本统计量，再看判据**（2026-09-30 的教训）：
    曾经因为 `_smooth_noise` 量纲写错，样本其实是张**白纸**（std=4.3、逐像素梯度 0.016），
    而所有检测项照常跑完、负对照照常"全绿" —— 校准了几轮全都在测噪声底。
    所以这里强制把 `std / 梯度 / 非零占比` 打在最上面，一眼就能看出样本是不是空的。
    """
    raw = Image.open(io.BytesIO(data))
    raw.load()
    arr = np.asarray(raw.convert("L")).astype(np.float32)
    grad = np.abs(np.diff(arr, axis=1))
    print(f"\n{SEP}\n{title}   {arr.shape[1]}x{arr.shape[0]}")
    print(f"  样本自检: std={arr.std():.2f}  均值梯度={grad.mean():.3f}  "
          f"零梯度列占比={float((grad.mean(axis=0) == 0).mean()):.1%}  "
          f"{'⚠️ 疑似空样本' if arr.std() < 10 or grad.mean() < 0.5 else 'ok'}")
    for stat in STATS:
        for label, ratios in block_grid_ratios(arr, stat=stat):
            cells = " ".join(f"{o}:{ratios[o]:.3f}" for o in range(8))
            best = max(ratios, key=lambda k: ratios[k])
            print(f"  [{stat:<6}] {label} {cells}")
            print(f"           峰值 offset={best}({ratios[best]:.3f})  "
                  f"对齐 offset=7({ratios[7]:.3f})  "
                  f"→ {'对齐' if best == 7 else '非对齐'}")
    rep = analyze(data, **kw)  # type: ignore[arg-type]
    for f in rep.findings:
        if f.check == "block_grid":
            print(f"  ⇒ 判定 [{f.verdict}] {f.detail}")


print("############ 对照组：细纹理层的两种实现，同一种底图 ############")
struct = photo("structured")
for tex in ("nearest", "white"):
    profile(f"[texture={tex}] 干净 JPEG q85", jpeg(photo("structured", texture=tex), 85))
for tex in ("nearest", "white"):
    profile(f"[texture={tex}] 元数据矛盾 JPEG q88",
            jpeg(photo("structured", texture=tex), 88, exif=adobe_exif()))
del struct

print("\n############ 本尊：冒烟脚本实际用的样本（texture=white）############")
profile("N2. 干净 JPEG q85", jpeg(photo("structured"), 85))
profile("N3. 纯周期纹理 q92（负对照）", jpeg(photo("periodic"), 92))
split = photo("structured").copy().astype(np.float32)
split[:, : W // 2] = split[:, : W // 2] * 0.55 + 40
split[:, W // 2:] += np.random.default_rng(11).normal(0, 11.0, size=split[:, W // 2:].shape)
profile("P1. 拼接 q92", jpeg(np.clip(split, 0, 255).astype(np.uint8), 92))
profile("P3. 元数据矛盾 q88", jpeg(photo("structured"), 88, exif=adobe_exif()))

print("\n############ 块网格相位偏移的正样本（本项唯一真正的检出力）############")
# 预期：裁剪错位 4px → 旧网格峰值应落在 offset (7-4)%8 = 3
profile("P4. 裁剪错位 4px + q95 重编码（预期 offset=3）",
        recrop_jpeg(jpeg(photo("structured"), 85), dx=4, dy=4))
profile("P4b. 裁剪错位 1px + q95 重编码（预期 offset=6）",
        recrop_jpeg(jpeg(photo("structured"), 85), dx=1, dy=1))
profile("P5. q60 图块错位 5px 贴进 q92 底图（预期 offset=2）",
        paste_patch_jpeg(jpeg(photo("structured"), 92), dx=5, dy=5))
profile("P5b. q60 图块**对齐**贴进 q92 底图（预期的负对照：仍应 offset=7）",
        paste_patch_jpeg(jpeg(photo("structured"), 92), dx=0, dy=0))

print("\n############ 我们自己引入了裁剪时要发生什么（相位 bug 回归）############")
big = photo("structured", w=2000, h=1500)
profile("E2. 2000x1500 干净 PNG（会被中心裁剪）", png(big))
