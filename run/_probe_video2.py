"""临时探针 2（用完即删）—— 拆开 MJPEG / 封面帧两条取帧路。"""

from __future__ import annotations

import numpy as np

from legal_acps.video import decode as D
from run import samples as S
from run import video_samples as vs

rng = np.random.default_rng(7)
arr = (rng.random((48, 48, 3)) * 60 + 100).astype("uint8")
REAL_JPEG = S.jpeg(arr, quality=90)
print("JPEG 头:", REAL_JPEG[:8].hex(), "长度", len(REAL_JPEG))

cover_data = vs.with_cover(REAL_JPEG)
dec = D.decode_video(cover_data)
print("\n[封面帧]")
print("  media_type:", dec.media_type, "frames:", len(dec.frames), "source:", dec.frame_source)
print("  facts.tags:", dec.facts.tags, "encoder:", dec.facts.encoder_hint)
print("  notes:", dec.facts.notes)
print("  top:", dec.facts.top_level_boxes)

mj = vs.motion_jpeg_mp4([REAL_JPEG])
dec2 = D.decode_video(mj)
print("\n[MJPEG]")
print("  media_type:", dec2.media_type, "frames:", len(dec2.frames), "source:", dec2.frame_source)
print("  tracks:", [(t.handler, t.codec, t.sample_count, t.framing) for t in dec2.facts.tracks])
print("  notes:", dec2.facts.notes)

# 手工走一遍 MJPEG 取帧的内部步骤
top = list(D._iter_boxes(mj, 0, len(mj)))
moov = next(b for b in top if b.type == "moov")
for box in D._children(mj, moov):
    if box.type != "trak":
        continue
    mdia = D._find(mj, [box], "mdia")
    hdlr = D._find(mj, [mdia], "hdlr")
    stbl = D._find(mj, [mdia], "minf", "stbl")
    stsd = D._find(mj, [stbl], "stsd")
    print("\n  手工: stbl=", stbl is not None, "stsd=", stsd is not None)
    if stsd is not None:
        print("  手工: stsd 解析=", D._parse_stsd(mj, stsd))
    stco = D._find(mj, [stbl], "stco")
    print("  手工: stco=", stco is not None, "offset=", D._parse_chunk_offsets(mj, stco) if stco else None)
    print("  手工: located=", D._bmff_sample_offsets(mj, stbl, want=[0]) if stbl else None)
    print("  手工: mdat_ranges=", dec2.facts.mdat_ranges)
    print("  手工: 文件长度=", len(mj))
