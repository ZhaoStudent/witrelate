"""音频鉴伪的**合成样本库** —— 给 `run/probe_audio.py` 与 `tools/verify_audio.py` 共用。

为什么要合成而不是拿真实录音：断言需要**知道正确答案**。
"这段音频里第 1.2 秒到 1.6 秒是精确的零"是我造出来的事实，
所以"检查项应该在那个位置报异常"才有意义；拿一段真实录音，
连"到底剪没剪过"都不知道，断言就退化成了"输出没变"。

⚠️ 所有样本都**确定性**（固定随机种子 / 纯解析式），
换台机器、换个 numpy 版本，逐位结果应当一致（浮点舍入除外）。

⚠️ 这些样本**不是**真实录音，也不能用来校准阈值 ——
它们只用来验证"该抓的抓得到、不该抓的不抓"（行为断言）。
"""

from __future__ import annotations

import io
import wave

import numpy as np

DEFAULT_SR = 44100


# ---------------------------------------------------------------------------
# 容器
# ---------------------------------------------------------------------------

def wav_bytes(
    samples: np.ndarray, *, sr: int = DEFAULT_SR, channels: int = 1
) -> bytes:
    """把波形写成 16 位 PCM WAV 字节。`samples` 可以是 1-D 或 `(frames, ch)`。"""
    arr = np.asarray(samples, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr[:, None]
    if arr.shape[1] != channels:
        channels = int(arr.shape[1])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(
            (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        )
    return buf.getvalue()


def append_bytes(data: bytes, extra: bytes) -> bytes:
    """在容器**声明范围之外**追加字节（模拟"拼接时没修头"）。"""
    return data + extra


def truncate_bytes(data: bytes, keep: int) -> bytes:
    """截掉尾部若干字节（模拟传输中断 / 头被改过）。"""
    return data[: max(0, len(data) - keep)]


def flac_bytes(
    *, sr: int = DEFAULT_SR, channels: int = 2, bits: int = 16,
    total_samples: int = DEFAULT_SR, pad: int = 64,
) -> bytes:
    """一份**只读得到 STREAMINFO** 的 FLAC（`fLaC` + 元数据块头 + 34 字节 STREAMINFO + 填充）。

    用途：它是一个"参数读到了一部分、另一部分**根本没有**"的容器 ——
    `sample_rate` / `channels` / `bits_per_sample` 有值，而
    `byte_rate` / `block_align` / `data_bytes` 全为 `None`。
    也就是「编码参数自洽性」这一项**一次对照都做不了**的那种输入。
    （它曾经会被报成「编码参数**内部自洽**（字节率 / 块对齐 / 数据长度相互吻合）」
      —— 点名了三个一个都没读到的字段。）

    ⚠️ `pad` 不能省：`_parse_flac()` 要求总长 ≥ 46 字节才去读 STREAMINFO，
    否则会返回"文件太短"那条分支，**本样本就测不到目标路径**（实测踩过一次：
    42 字节的样本走了"太短"分支，探针白跑）。这里多写一点填充帧数据。
    """
    packed = (
        (int(sr) << 44)
        | ((int(channels) - 1) << 41)
        | ((int(bits) - 1) << 36)
        | int(total_samples)
    )
    info = b"\x00" * 10 + packed.to_bytes(8, "big") + b"\x00" * 16   # = 34 字节
    return b"fLaC" + bytes([0x80, 0x00, 0x00, 34]) + info + b"\x00" * int(pad)


def wav_odd_tail(*, sr: int = 8000, samples: int = 101) -> bytes:
    """16 位单声道 WAV，但 `data` 块长度是**奇数**（不是帧长 2 的整数倍）。

    用途：验证"末尾那一个落单字节被丢弃"这件事**说出来了**。
    它曾经被写进一个已经拷进 `facts` 的列表 ⇒ 提示永远到不了报告
    （实测 `facts.notes == ()`），而"静默少分析了一点东西"正是本项目
    最不能容忍的那类事实。

    ⚠️ 这里刻意**不**用 `wave` 模块写：标准库会把 data 长度写成偶数，
    构造不出这个样本。
    """
    pcm = b"\x01\x00" * int(samples) + b"\x7f"        # 尾部多一个落单字节
    fmt_chunk = (
        (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
        + int(sr).to_bytes(4, "little") + (int(sr) * 2).to_bytes(4, "little")
        + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    )
    body = (
        b"WAVE"
        + b"fmt " + len(fmt_chunk).to_bytes(4, "little") + fmt_chunk
        + b"data" + len(pcm).to_bytes(4, "little") + pcm
    )
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def wav_channels_zero(*, sr: int = 8000, samples: int = 200) -> bytes:
    """`fmt` 头里 `channels=0` / `byte_rate=0` / `block_align=0` 的 WAV。

    一份**头部本身不合法**的 WAV：正经编码器不可能产出它。
    用途：验证"读不到可对照的字段时不许说「内部自洽」"——
    它曾经落到 `_clean(...)`，报告里写着「字节率 / 块对齐 / 数据长度相互吻合」，
    而那三项**一个都没读到**。
    """
    n = int(samples)
    fmt_chunk = (
        (1).to_bytes(2, "little") + (0).to_bytes(2, "little")
        + int(sr).to_bytes(4, "little") + (0).to_bytes(4, "little")
        + (0).to_bytes(2, "little") + (16).to_bytes(2, "little")
    )
    pcm = b"\x01\x00" * n
    body = (
        b"WAVE"
        + b"fmt " + len(fmt_chunk).to_bytes(4, "little") + fmt_chunk
        + b"data" + len(pcm).to_bytes(4, "little") + pcm
    )
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def wav_without_fmt(*, samples: int = 200) -> bytes:
    """只有 `data` 块、**没有 `fmt` 块**的 WAV（头部不完整 / 被破坏）。

    它按魔术字节仍然是 `audio/wav`，但解不出波形 —— 于是它落在
    "没解出波形"的第二种原因上（**不是**"压缩格式没有解码器"）。
    用途：验证失败归因没有被含糊成一句话（那会让调用方去转格式，
    而真正要改的是他送进来的东西）。
    """
    pcm = b"\x01\x00" * int(samples)
    body = b"WAVE" + b"data" + len(pcm).to_bytes(4, "little") + pcm
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def mismatched_fmt(data: bytes, *, bits: int = 24) -> bytes:
    """只改 `fmt` 块**声明**的位深，不动块对齐与字节率（模拟头部被手工改过）。

    这一份字节在语法上完全合法（照样能解出波形），唯一的痕迹是
    `block_align` / `byte_rate` 与「声道 × 位深」对不上 —— 而这是编码参数层
    最硬的一条线索，因为它不可能由任何正常的编码器产生。

    它同时是**「闸门与正向触发同时出现」**这一组合的样本：
    `pcm_available=True` ⇒ 波形域六项照常执行，所以闸门**不该**触发，
    等级也不该被压成「无法判定」。

    ⚠️ 偏移量按 RIFF 规范算死：`RIFF(12) + "fmt "(4) + size(4)` ⇒
    块对齐在文件偏移 32、位深在 34。改动前先在此处核对，不要靠猜。
    """
    out = bytearray(data)
    if len(out) < 36 or out[:4] != b"RIFF" or out[8:12] != b"WAVE":
        raise ValueError("mismatched_fmt 只对 RIFF/WAVE 字节有效。")
    out[34:36] = int(bits).to_bytes(2, "little")
    return bytes(out)


# ---------------------------------------------------------------------------
# 内容
# ---------------------------------------------------------------------------

def white_noise(seconds: float, *, sr: int = DEFAULT_SR, level: float = 0.05,
                seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return level * rng.standard_normal(int(sr * seconds))


def tone(seconds: float, freq: float = 440.0, *, sr: int = DEFAULT_SR,
         level: float = 0.4) -> np.ndarray:
    t = np.arange(int(sr * seconds), dtype=np.float64) / sr
    return level * np.sin(2.0 * np.pi * freq * t)


def speech_like(seconds: float, *, sr: int = DEFAULT_SR, noise: float = 0.02,
                seed: int = 7) -> np.ndarray:
    """粗糙的"像语音"的信号：谐波堆 + 缓慢包络 + 底噪。

    ⚠️ 它不是语音，只是**宽带、有谐波结构、电平起伏**的信号，
    用来验证"正常内容不该被报成剪辑"（负对照）。
    """
    t = np.arange(int(sr * seconds), dtype=np.float64) / sr
    env = 0.5 + 0.5 * np.sin(2.0 * np.pi * 0.7 * t)
    voice = sum(
        np.sin(2.0 * np.pi * f * t) * (0.10 / (1.0 + i))
        for i, f in enumerate((150.0, 300.0, 450.0, 600.0, 900.0, 1350.0))
    ) * env
    rng = np.random.default_rng(seed)
    return voice + noise * rng.standard_normal(t.size)


def with_digital_silence(x: np.ndarray, *, sr: int = DEFAULT_SR,
                         t0: float = 1.2, t1: float = 1.6) -> np.ndarray:
    """在音轨**中间**插入一段采样值精确为 0 的区间（软件插入的痕迹）。"""
    out = np.array(x, dtype=np.float64, copy=True)
    out[int(sr * t0): int(sr * t1)] = 0.0
    return out


def spliced_noise_floors(
    seconds_each: float = 2.4, *, sr: int = DEFAULT_SR,
    quiet: float = 0.002, loud: float = 0.02, seed: int = 3,
) -> np.ndarray:
    """两段**底噪水平明显不同**的录音首尾相接（每段里各有"说话 + 停顿"）。

    ⚠️ 建模要如实：真实场景不是"一段纯底噪接另一段纯底噪"，而是两段各自
    "有内容 + 有停顿"的录音拼在一起。痕迹在**停顿期的底噪水平**上 ——
    一段的停顿是 -54 dBFS，另一段是 -34 dBFS。
    所以本函数每段的**后半段是纯底噪**（停顿），前半段是有内容的。

    这是"把两段不同来源的录音拼起来"的最小模型：没有咔哒、没有数字静音、
    语法上完全连续，唯一的痕迹就是两处停顿的底噪差了 20 dB。
    """

    def segment(floor: float, offset: int) -> np.ndarray:
        n = int(sr * seconds_each)
        t = np.arange(n, dtype=np.float64) / sr
        # 后半段是停顿（**要够长**：安静段判据要求连续 ≥3 个 0.4 秒窗）
        gate = (t < seconds_each * 0.4).astype(np.float64)
        rng = np.random.default_rng(seed + offset)
        content = sum(
            np.sin(2.0 * np.pi * f * t) * (0.12 / (1.0 + i))
            for i, f in enumerate((170.0, 340.0, 510.0, 850.0))
        ) * gate
        return content + floor * rng.standard_normal(n)

    return np.concatenate([segment(quiet, 0), segment(loud, 1)])


def hard_cutoff(x: np.ndarray, *, sr: int = DEFAULT_SR,
                cutoff: float = 10000.0) -> np.ndarray:
    """用 FFT 硬截断模拟"有损编码留下的低通"（截止处是一道陡壁）。"""
    spectrum = np.fft.rfft(np.asarray(x, dtype=np.float64))
    freqs = np.fft.rfftfreq(len(x), d=1.0 / sr)
    spectrum[freqs > cutoff] = 0.0
    return np.fft.irfft(spectrum, n=len(x))


def mains_hum(
    seconds: float = 6.0, *, sr: int = DEFAULT_SR, base: float = 50.0,
    level: float = 0.02, jump_at: float | None = None,
    seed: int = 11,
) -> np.ndarray:
    """带市电哼声（基频 + 谐波梳）的音轨；`jump_at` 处插入半周期相位跳变。

    相位跳变是"录音中间被剪掉一段再对上"在 ENF 上的表现：
    频率不变，但相位接不上。这正是 ENF 这条路线的价值所在。
    """
    n = int(sr * seconds)
    t = np.arange(n, dtype=np.float64) / sr
    hum = sum(
        np.sin(2.0 * np.pi * base * k * t + 0.3 * k) * (level / k)
        for k in (1, 2, 3, 4)
    )
    rng = np.random.default_rng(seed)
    hum = hum + 0.004 * rng.standard_normal(n)
    if jump_at is not None:
        idx = int(sr * jump_at)
        hum[idx:] = -hum[idx:]        # 半周期翻转 = 最干净的相位跳变
    return hum


# ---------------------------------------------------------------------------
# MPEG（伪帧头链：只用来验证**解析器**，不是可播放的 MP3）
# ---------------------------------------------------------------------------

_MPEG_BR_INDEX = {32: 1, 40: 2, 48: 3, 56: 4, 64: 5, 80: 6, 96: 7,
                  112: 8, 128: 9, 160: 10, 192: 11, 224: 12, 256: 13, 320: 14}
_MPEG_SR_INDEX = {44100: 0, 48000: 1, 32000: 2}


def mpeg_frame(bitrate: int = 128, sr: int = 44100, mode: int = 0,
               version: int = 3, layer: int = 1) -> bytes:
    """一个合法的 MPEG-1 Layer III 帧（帧头 + 静音填充，**不可播放**）。

    `version=3` → MPEG1；`layer=1` → Layer III；`mode=0` → stereo。
    """
    b1 = 0xE0 | (version << 3) | (layer << 1) | 0x01
    b2 = (_MPEG_BR_INDEX[bitrate] << 4) | (_MPEG_SR_INDEX[sr] << 2)
    b3 = mode << 6
    header = bytes([0xFF, b1, b2, b3])
    frame_len = 144 * bitrate * 1000 // sr
    return header + b"\x00" * max(0, frame_len - 4)


def mp3_bytes(frame_count: int = 40, *, bitrate: int = 128, sr: int = 44100,
              tail_sr: int | None = None, tail_from: int | None = None) -> bytes:
    """由若干帧拼成的 MPEG 字节流。

    `tail_from` / `tail_sr` 不为空时，从第 `tail_from` 帧起改用 `tail_sr` 采样率
    —— 用来构造"同一文件里采样率跳变"（在任何编码器里都不可能出现）。
    """
    parts = []
    for i in range(frame_count):
        if tail_from is not None and tail_sr is not None and i >= tail_from:
            parts.append(mpeg_frame(bitrate=bitrate, sr=tail_sr))
        else:
            parts.append(mpeg_frame(bitrate=bitrate, sr=sr))
    return b"".join(parts)


def with_id3v2(payload: bytes, *, title: str = "x") -> bytes:
    """在 MPEG 流前面加一个 ID3v2 头（解析器应当跳过它）。"""
    size = 0
    body = b"\x00" * size
    header = b"ID3\x03\x00\x00" + bytes(
        [(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F]
    )
    del title
    return header + body + payload
