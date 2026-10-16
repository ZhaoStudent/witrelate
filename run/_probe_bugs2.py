"""一次性探针 v2：把探针 1 取样补齐，并测第二种形态（WAV 头里 channels=0）。"""
from __future__ import annotations

from legal_acps.audio import decode as D
from legal_acps.audio import forensics as F


def show(tag: str, facts, dec) -> None:
    print(f"\n--- {tag} ---")
    print("  media_type      =", facts.media_type)
    print("  sample_rate     =", facts.sample_rate)
    print("  channels        =", facts.channels)
    print("  bits_per_sample =", facts.bits_per_sample)
    print("  byte_rate       =", facts.byte_rate)
    print("  block_align     =", facts.block_align)
    print("  data_bytes      =", facts.data_bytes)
    print("  frame_count     =", facts.frame_count)
    cp = F.check_codec_params(facts)
    print("  → verdict =", cp.verdict)
    print("  → detail  =", cp.detail)
    print("  → 波形可用 =", dec.waveform is not None)


# --------------------------------------------------------------------------
# A. 正常长度的 FLAC（≥46 字节才读得到 STREAMINFO）
# --------------------------------------------------------------------------
sr, ch, bps, total = 44100, 2, 16, 44100
bits = (sr << 44) | ((ch - 1) << 41) | ((bps - 1) << 36) | total
info = b"\x00" * 10 + bits.to_bytes(8, "big") + b"\x00" * 16
flac = b"fLaC" + bytes([0x80, 0x00, 0x00, 34]) + info + b"\x00" * 64   # 尾部补帧数据
print("FLAC 字节长度 =", len(flac), "（需 ≥ 46 才解析 STREAMINFO）")
show("A · FLAC", (dec_flac := D.decode_audio(flac)).facts, dec_flac)
print("  整份报告等级 =", F.analyze(flac).suspicion)

# --------------------------------------------------------------------------
# B. WAV 头里 channels=0（头部不可信），但 sample_rate 是有效的
# --------------------------------------------------------------------------
sr2, n = 8000, 200
pcm = b"\x01\x00" * n
fmt_chunk = (1).to_bytes(2, "little") + (0).to_bytes(2, "little") \
    + sr2.to_bytes(4, "little") + (0).to_bytes(4, "little") \
    + (0).to_bytes(2, "little") + (16).to_bytes(2, "little")
body = b"WAVE" + b"fmt " + len(fmt_chunk).to_bytes(4, "little") + fmt_chunk \
    + b"data" + len(pcm).to_bytes(4, "little") + pcm
wav = b"RIFF" + len(body).to_bytes(4, "little") + body
show("B · WAV，fmt 里 channels=0 / byte_rate=0 / block_align=0",
     (dec_wav := D.decode_audio(wav)).facts, dec_wav)
