"""临时探针（用完即删）—— 确认「帧取到了、但一帧都没送进图像链」时的报告形态。"""

from __future__ import annotations

import numpy as np

from legal_acps.video import forensics
from run import samples as S
from run import video_samples as vs

rng = np.random.default_rng(7)
arr = (rng.random((48, 48, 3)) * 60 + 100).astype("uint8")
REAL_JPEG = S.jpeg(arr, quality=90)

FAKE_JPEG = b"\xff\xd8\xff" + b"\x00" * 64      # 有 JPEG 魔术字节、但不是可解码的 JPEG

CASES = {
    "A 无帧(裸MP4)": lambda: vs.clean_mp4(),
    "B 封面帧": lambda: vs.with_cover(REAL_JPEG),
    "C MJPEG真JPEG": lambda: vs.motion_jpeg_mp4([REAL_JPEG]),
    "D MJPEG假JPEG": lambda: vs.motion_jpeg_mp4([FAKE_JPEG]),
    "E 封面假JPEG": lambda: vs.with_cover(FAKE_JPEG),
    "F 动图GIF": lambda: vs.animated_gif(3),
}

for name, make in CASES.items():
    data = make()
    try:
        report = forensics.analyze(data)
    except Exception as exc:  # noqa: BLE001
        print(f"{name:16s} 异常 {type(exc).__name__}: {exc}")
        continue
    frames_finding = next(f for f in report.findings if f.check == "frames")
    print(
        f"{name:16s} 等级={report.suspicion} 触发={[f.check for f in report.findings if f.verdict == '提示异常']}"
        f"\n                 frames_available={report.frames_available} analyzed={report.frames_analyzed}"
        f" source={report.frame_source}"
        f"\n                 frames.verdict={frames_finding.verdict!r} detail={str(frames_finding.detail)[:110]!r}"
        f"\n                 frames_note={report.frames_note[:110]!r}"
    )
