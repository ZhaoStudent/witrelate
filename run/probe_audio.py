"""临时探针：打印合成样本的逐项判定，用来**人工核对判据**（不参与验收）。

用法（必须模块方式跑）：`python -m run.probe_audio`

它回答的是"每个检查项在我造的已知样本上都说了什么"，
真正的行为断言在 `tools/verify_audio.py`。
"""

from __future__ import annotations

import sys

from legal_acps.audio.forensics import analyze
from run import audio_samples as S

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def show(name: str, data: bytes, *, expect: str = "") -> None:
    try:
        report = analyze(data)
    except ValueError as exc:
        print(f"\nOK === {name} ===（如期抛出 ValueError：{exc}）")
        return
    flag = "OK " if (not expect or report.suspicion == expect) else "!! "
    print(f"\n{flag}=== {name} ===（期望 {expect or '任意'}，实得 {report.suspicion}）")
    print(f"   容器 {report.container} | pcm={report.pcm_available} "
          f"| {report.duration_seconds if report.duration_seconds is None else round(report.duration_seconds, 2)}s")
    for item in report.findings:
        print(f"   {item.check:22s} {item.verdict:6s} {item.detail[:78]}")


def main() -> int:
    sr = S.DEFAULT_SR
    # ⚠️ 「负对照」的期望值现在都钉死了：**不能因为某一项不适用就报
    #    「无法判定」**（那会让每一段正常录音都变成测不了）。
    #    图像侧有一条同源的成对断言：纯色图 → 无法判定；干净自然图 → 低。
    show("白噪 · 负对照", S.wav_bytes(S.white_noise(3.0, level=0.05, seed=1)),
         expect="低")
    show("谐波语音 · 负对照", S.wav_bytes(S.speech_like(3.0)), expect="低")
    show("纯音 440Hz · 负对照", S.wav_bytes(S.tone(3.0, 440.0)), expect="低")
    show("数字静音段（1.2–1.6s）",
         S.wav_bytes(S.with_digital_silence(S.speech_like(3.0))), expect="中")
    show("两段不同底噪拼接",
         S.wav_bytes(S.spliced_noise_floors()), expect="中")
    show("白噪 + 10kHz 硬截止（有损编码痕迹）",
         S.wav_bytes(S.hard_cutoff(S.white_noise(3.0, level=0.05, seed=2), cutoff=10000.0)),
         expect="低")
    show("市电 50Hz · 相位连续",
         S.wav_bytes(S.mains_hum(6.0, jump_at=None)), expect="低")
    show("市电 50Hz · 相位跳变",
         S.wav_bytes(S.mains_hum(6.0, jump_at=3.0)), expect="中")

    print("\n--- 容器层 ---")
    wav = S.wav_bytes(S.white_noise(1.0, seed=5))
    show("WAV + 尾部附加数据", S.append_bytes(wav, b"junk-1234"), expect="低")
    show("WAV + 截断", S.truncate_bytes(wav, 2000), expect="低")
    # 解不出波形的压缩格式 ⇒ **波形域整域没跑** ⇒ 材料级闸门 ⇒ 无法判定。
    # 这是"弃权分两层"里闸门那一层的直接后果，必须钉死。
    show("MP3 帧头链（128kbps 恒定）", S.mp3_bytes(40), expect="无法判定")
    show("MP3 帧头链 + 采样率跳变", S.mp3_bytes(40, tail_from=20, tail_sr=48000),
         expect="中")
    show("非音频字节", b"this is definitely not audio, pad pad pad pad")

    print("\n--- 中间结论（给人工看，不参与断言）---")
    report = analyze(S.wav_bytes(S.speech_like(3.0)))
    print("谐波语音的 rationale：", report.suspicion_rationale[:160])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
