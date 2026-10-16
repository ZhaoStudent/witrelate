"""临时探针 3（用完即删）—— 新样本 + 编码工具 + 重复帧。"""

from __future__ import annotations

import numpy as np

from legal_acps.video import forensics
from run import samples as S
from run import video_samples as vs

rng = np.random.default_rng(11)


def photo(seed: int) -> bytes:
    r = np.random.default_rng(seed)
    return S.jpeg((r.random((64, 64, 3)) * 60 + 100).astype("uint8"), quality=90)


A, B, C = photo(1), photo(2), photo(3)


def show(name: str, data: bytes) -> None:
    try:
        rep = forensics.analyze(data)
    except Exception as exc:  # noqa: BLE001
        print(f"{name:22s} 异常 {type(exc).__name__}: {exc}")
        return
    fired = [f.check for f in rep.findings if f.verdict == "提示异常"]
    skipped = [f.check for f in rep.findings if f.verdict == "无法判定"]
    ff = next(f for f in rep.findings if f.check == "frames")
    print(
        f"{name:22s} 等级={rep.suspicion} 触发={fired} 弃权={skipped}"
        f" 帧={rep.frames_analyzed}/{rep.frame_source}"
    )
    if "frames" in fired:
        print("      frames detail:", str(ff.detail)[:220])
    md = next(f for f in rep.findings if f.check == "metadata")
    if md.verdict != "无法判定":
        print("      metadata:", str(md.detail)[:180])


show("GIF 真动画3帧", vs.rasterizable_gif(3))
show("MJPEG 3 帧(互不相同)", vs.motion_jpeg_mp4([A, B, C]))
show("MJPEG 3 帧(全相同)", vs.motion_jpeg_mp4([A, A, A]))
show("封面+编码工具", vs.with_cover(A, encoder="Lavf58.76.100"))
show("封面+编码工具(剪映)", vs.with_cover(A, encoder="CapCut 3.2"))
