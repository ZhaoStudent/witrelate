"""一次性探针：验证几处**怀疑**是不是真的缺陷。用完即删。

原则：不靠肉眼读代码下结论 —— 每一条都用真实输入跑一遍，打印**实际产出**。
"""
from __future__ import annotations

import json

from legal_acps.audio import decode as D
from legal_acps.audio import forensics as F
from legal_acps.mcp import audio_forensics as AF
from legal_acps.mcp import server as S


def hdr(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# --------------------------------------------------------------------------
# 探针 1：FLAC 的 codec_params 报了什么？
#   FLAC 的 facts 里 byte_rate / block_align / data_bytes **全是 None**
#   —— 也就是「字节率 / 块对齐 / 数据长度相互吻合」这句话一次比较都没做过。
# --------------------------------------------------------------------------
hdr("探针 1 · FLAC 的 codec_params")

sr, ch, bps, total = 44100, 2, 16, 44100
bits = (sr << 44) | ((ch - 1) << 41) | ((bps - 1) << 36) | total
info = b"\x00" * 10 + bits.to_bytes(8, "big") + b"\x00" * 16
flac = b"fLaC" + bytes([0x80, 0x00, 0x00, 34]) + info
print("构造的 FLAC 字节长度 =", len(flac))

dec = D.decode_audio(flac)
print("media_type =", dec.media_type)
print("facts.sample_rate =", dec.facts.sample_rate,
      "| byte_rate =", dec.facts.byte_rate,
      "| block_align =", dec.facts.block_align,
      "| data_bytes =", dec.facts.data_bytes,
      "| frame_count =", dec.facts.frame_count)
cp = F.check_codec_params(dec.facts)
print("→ check_codec_params verdict =", cp.verdict)
print("→ detail =", cp.detail)

rep = F.analyze(flac)
print("→ 整份报告综合等级 =", rep.suspicion)
print("→ summary_line 里的这一句会不会被原样转述？")
print("   ", cp.detail)


# --------------------------------------------------------------------------
# 探针 2：_parse_wav 里那条「末尾 N 字节被丢弃」的 note 有没有进 facts？
# --------------------------------------------------------------------------
hdr("探针 2 · WAV 尾部落单字节的 note")

# 构造：16 位单声道；data 块长度取奇数（不是块对齐 2 的整数倍）
sr2 = 8000
n_samples = 101                      # 奇数
pcm = (b"\x01\x00" * n_samples) + b"\x7f"   # 尾部多 1 个落单字节
fmt_chunk = (1).to_bytes(2, "little") + (1).to_bytes(2, "little") \
    + sr2.to_bytes(4, "little") + (sr2 * 2).to_bytes(4, "little") \
    + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
body = b"WAVE" + b"fmt " + len(fmt_chunk).to_bytes(4, "little") + fmt_chunk \
    + b"data" + len(pcm).to_bytes(4, "little") + pcm
wav = b"RIFF" + len(body).to_bytes(4, "little") + body

dec2 = D.decode_audio(wav)
print("data 块字节数 =", len(pcm), "（块对齐 2 ⇒ 落单 1 字节）")
print("facts.notes =", dec2.facts.notes)
print("→ 结论：note", "进了 facts" if dec2.facts.notes else "**没有进 facts（被静默丢掉）**")
print("→ decode 的 window_note =", dec2.window_note)


# --------------------------------------------------------------------------
# 探针 3：MCP 的 base_hz 有没有按 schema 的 enum [50,60] 校验？
# --------------------------------------------------------------------------
hdr("探针 3 · audio_enf_track 的 base_hz 校验")

for value in (50, 123, 7.5, True, "50"):
    res = AF.call_tool("audio_enf_track", {"audio_uri": "data:audio/wav;base64,QQ==",
                                           "base_hz": value})
    body = res["content"][0]["text"]
    print(f"  base_hz={value!r:>8} isError={res['isError']} → {body.splitlines()[0][:78]}")


# --------------------------------------------------------------------------
# 探针 4：`notifications/*` 带 id 时回不回应？（带 id 就是请求）
# --------------------------------------------------------------------------
hdr("探针 4 · notifications/ 方法带 id")

for msg in (
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 7, "method": "notifications/initialized"},
):
    print(f"  {json.dumps(msg, ensure_ascii=False)}")
    print("   → reply =", S.handle_message(msg))


# --------------------------------------------------------------------------
# 探针 5：check_container 那两个分支是不是等价的？
# --------------------------------------------------------------------------
hdr("探针 5 · check_container 的 if/elif 分支（声明 MIME 不一致时）")

facts_ok = dec2.facts
for declared in (None, "audio/wav", "audio/mpeg"):
    f = F.check_container(facts_ok, declared)
    print(f"  declared={declared!r:>14} → {f.detail[:110]}")
print()
print("（代码里：if not mime_note.startswith('调用方') → 追加；elif mime_note → 追加。")
print(" 两个分支体相同 ⇒ 那个 elif 分支永远不会产生不同行为。）")
