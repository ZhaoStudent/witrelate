"""诊断：JPEG 8×8 块网格的能量剖面，确认偏移判据是否可靠。"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

rng = np.random.default_rng(7)
W, H = 720, 540


def make(*, texture: float) -> np.ndarray:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    lum = 110 + 30 * np.sin(xx / 61.0) + 24 * np.cos(yy / 47.0)
    if texture:
        fine = rng.normal(0, texture, (H // 2 + 1, W // 2 + 1)).astype(np.float32)
        fine_img = Image.fromarray(
            np.clip((fine - fine.min()) / (np.ptp(fine) + 1e-9) * 255, 0, 255).astype(np.uint8)
        )
        lum += (np.asarray(fine_img.resize((W, H), Image.NEAREST)).astype(np.float32) - 128) * 0.28
    rgb = np.stack([lum, lum, lum], axis=2) + rng.normal(0, 2.5, (H, W, 3))
    return np.clip(rgb, 0, 255).astype(np.uint8)


for label, tex, q in [("无纹理 q85", 0.0, 85), ("有细纹理 q85", 40.0, 85),
                      ("有细纹理 q92", 40.0, 92), ("有细纹理 q60", 40.0, 60)]:
    buf = io.BytesIO()
    Image.fromarray(make(texture=tex), "RGB").save(buf, "JPEG", quality=q)
    arr = np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("L")).astype(np.float32)
    col = np.abs(np.diff(arr, axis=1)).mean(axis=0)
    row = np.abs(np.diff(arr, axis=0)).mean(axis=1)
    print(f"\n===== {label}  shape={arr.shape} =====")
    for name, energy in (("横向(列)", col), ("纵向(行)", row)):
        ratios = []
        for off in range(8):
            idx = np.arange(off, energy.size, 8)
            mask = np.ones(energy.size, dtype=bool)
            mask[idx] = False
            ratios.append(float(energy[idx].mean() / max(energy[mask].mean(), 1e-6)))
        best = int(np.argmax(ratios))
        shift = (best + 1) % 8
        print(f"  {name}: ratios={[round(r, 3) for r in ratios]}")
        print(f"    最强 offset={best} → JPEG 网格位移 shift={shift}  ({'对齐' if shift == 0 else '偏移'})")
