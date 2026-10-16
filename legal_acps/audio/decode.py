"""音频字节 → 波形 + 容器事实。**只做解码与事实提取，不做任何判定。**

════════════════════════════════════════════════════════════════════════
为什么自己解析，而不是 `import librosa` / `soundfile` / `pydub`
════════════════════════════════════════════════════════════════════════

1. **本机与服务器都没有这些库**，也没有 ffmpeg / sox。装不上（服务器只有
   3.6 GB 内存、无 GPU，装 librosa 会顺带拖进 numba/llvmlite 一大串）。
2. **解码层的输出要能当证据用**：容器声明长度、chunk 布局、帧头参数这些都是
   **鉴伪线索本身**（拼接与重编码会在这些地方留痕）。用第三方库读，就只能拿到
   "它解码成功了、这是波形"，拿不到"它声明了 1,000,000 字节、实际只有 998,000"。
3. `audioop` 在 Python 3.13 已被移除 ⇒ 8/16/24 位样本的字节→浮点转换
   必须自己写（本模块用 numpy 做，一举两得）。

════════════════════════════════════════════════════════════════════════
能解到什么程度（**这是本能力的边界，必须如实对外说明**）
════════════════════════════════════════════════════════════════════════

  WAV / RIFF   ✅ 完整解析 RIFF chunk 链 + 解出 PCM 波形（8/16/24/32 位整数、
                  32/64 位浮点、WAVE_FORMAT_EXTENSIBLE 的 PCM 子格式）
  MP3 / MPEG   ⚠️ **不解码波形**，但完整解析**帧头链**（比特率 / 采样率 /
                  声道模式序列、帧链是否断裂、尾部标签）
  FLAC         ⚠️ 不解码波形，但读 STREAMINFO（采样率 / 声道 / 位深 / 总样本数）
  OGG / M4A / AIFF / AMR
               ⚠️ 只识别容器并如实标注"未解码"

⚠️ "解不出波形"**不等于**"这次没检查" —— 波形域的每一项都必须在 findings 里
以**弃权**（`verdict="无法判定"` + `GATE_ABSTAIN_PREFIX` 前缀的 detail）出现。
若只报几条容器层结论就收尾，报告读起来会像"检查过了、没问题"，
而那正是本项目最忌讳的**静默安全结论**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

#: 分析上限：**帧数**（不是字节）。约 3 分钟 @44.1 kHz。
#: 超长音轨**只分析开头这一段并如实标注**（`analysis_window`），
#: 绝不静默截断 —— 静默截断会让所有"全程一致性"类的统计失真，
#: 而失真出的结论看上去和真结论一模一样。
MAX_ANALYSIS_FRAMES = 8_000_000

#: WAV 编码类型（`fmt` chunk 的 `wFormatTag`）
WAV_FORMAT_PCM = 0x0001
WAV_FORMAT_FLOAT = 0x0003
WAV_FORMAT_ALAW = 0x0006
WAV_FORMAT_MULAW = 0x0007
WAV_FORMAT_EXTENSIBLE = 0xFFFE

_WAV_CODEC_NAMES = {
    0x0001: "PCM",
    0x0003: "IEEE float",
    0x0006: "A-law",
    0x0007: "µ-law",
    0x0011: "IMA ADPCM",
    0x0055: "MP3 (in WAV)",
    0xFFFE: "Extensible",
}

#: RIFF chunk id → 人话（只列会影响判断的那些）。
#: **公开**：`forensics.check_container()` 用它把 chunk 列表翻译成人话。
WAV_CHUNK_NOTES = {
    "fact": "非 PCM 音频的样本数声明块（出现它通常意味着不是未压缩 PCM）",
    "bext": "广播扩展块（专业录音设备常见，是**来源正规**的正面线索之一）",
    "cue ": "提示点表（录音/编辑软件写入的标记）",
    "smpl": "采样器循环点（编辑软件写入）",
    "LIST": "信息列表块（软件名 / 注释等文字标签）",
    "id3 ": "ID3 标签块（被媒体软件处理过的痕迹）",
    "iXML": "现场录音元数据（专业设备）",
    "JUNK": "占位填充块",
}

#: `LIST/INFO` 子块 id → 人话。**公开**：`forensics.check_metadata()` 用它。
INFO_TAG_LABELS = {
    "ISFT": "软件",
    "ICMT": "注释",
    "INAM": "标题",
    "IART": "作者",
    "ICRD": "日期",
    "IENG": "工程师",
    "ITCH": "技术员",
    "ISBJ": "主题",
    "IGNR": "流派",
    "ICOP": "版权",
}

#: 「按魔术字节认得、但本进程没有解析器」的容器名 —— 键是 `sniff_media_type()`
#: 会返回的**规范名**。
#:
#: 🔴 **不变量：表里每一个键都必须真的有嗅探分支。** 否则它就是一个**死键** ——
#:    本层宣称"我认得它"，而 `analyze()` 会拿 `application/octet-stream` 把真文件
#:    拒掉，拒绝理由还是一句假话「不是可识别的音频容器」。
#:    2026-10-05 实测踩到：`audio/ac3` 在表里挂了很久，而 `sniff_media_type()`
#:    **根本没有 AC-3 分支** ⇒ 真的 `.ac3` 一路走到拒绝。同一天量到的视频侧是
#:    反过来的同一个病（表里有 `application/ogg`，闸门却只认 `video/` 前缀）。
#:    这条不变量现在由 `tools/verify_audio.py` 第 3c 节**现场派生**地钉住：
#:    逐个键造魔术字节，必须能从 `sniff_media_type()` 走到 `analyze()` 出报告。
_OTHER_CONTAINERS = {
    "audio/flac": "FLAC（无损压缩）",
    "audio/ogg": "Ogg（Vorbis/Opus）",
    "audio/mp4": "MP4 / M4A（AAC）",
    "audio/aiff": "AIFF",
    "audio/amr": "AMR（窄带语音）",
    "audio/wma": "WMA",
    "audio/ac3": "AC-3",
}


@dataclass(frozen=True)
class Waveform:
    """解出来的波形。`samples` 形状 `(frames, channels)`，取值归一化到 [-1, 1]。"""

    samples: np.ndarray
    sample_rate: int
    channels: int
    bits_per_sample: int
    encoding: str

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])

    @property
    def duration(self) -> float:
        return self.frames / float(self.sample_rate or 1)

    def mono(self) -> np.ndarray:
        """降成单声道（**多声道取平均**）。所有波形域统计都在它上面做。

        ⚠️ 取平均而不是取第一声道：只取一路会丢掉另一路的不连续，
        而剪辑往往**只动了一路**（尤其双话筒录音里的单侧替换）。
        """
        if self.samples.ndim == 1:
            return self.samples
        if self.samples.shape[1] == 1:
            return self.samples[:, 0]
        return self.samples.mean(axis=1)


@dataclass(frozen=True)
class ContainerFacts:
    """容器层事实。**全部来自字节本身**，没有一条是推断出来的。"""

    media_type: str
    container: str
    sample_rate: int | None = None
    channels: int | None = None
    bits_per_sample: int | None = None
    duration_seconds: float | None = None
    #: `fmt` 块**声明**的字节率 / 块对齐。与 采样率×声道×位深 对不上，
    #: 就说明头部被人改过（或手工拼过）—— 这是编码参数层最硬的一条线索。
    byte_rate: int | None = None
    block_align: int | None = None
    declared_size: int | None = None      # 容器头声明的总长（如 RIFF size + 8）
    actual_size: int = 0
    data_bytes: int | None = None         # 音频数据块声明的长度
    appended_bytes: int = 0               # 容器声明范围**之外**的多余字节
    truncated: bool = False               # 声明长度 > 实际可用字节
    chunks: tuple[str, ...] = ()
    tags: tuple[tuple[str, str], ...] = ()      # 容器内的文字标签（软件名等）
    # -- MPEG 专有 --
    frame_count: int | None = None
    sample_rates: tuple[int, ...] = ()
    bitrates: tuple[int, ...] = ()
    channel_modes: tuple[str, ...] = ()
    frame_chain_broken: bool = False
    trailing_tags: tuple[str, ...] = ()
    # -- 解析过程中值得如实记下的事实（不参与判定，但解释"为什么少了几个字节"）--
    notes: tuple[str, ...] = ()
    #: **这份字节的内部结构到底有没有被解析过。**
    #:
    #: ⚠️ 2026-10-05 补的字段。在此之前 `check_container()` 对"按魔术字节认得、
    #:    但本进程没有解析器"的容器（Ogg / M4A / AIFF / AMR / WMA / AC-3）也说
    #:    「声明的长度与实际字节**相符**，没有附加数据或尾部标签」——
    #:    而那三项检查**一次都没跑**（这些容器连长度字段都没有）。把"没做过的检查"
    #:    写成正面结论，是本仓最危险的一类缺陷（`verify_audio` 第 3b 节整节都在修它）。
    #:    有解析器的容器（WAV / MP3 / FLAC）默认 `True` ⇒ 一个字都不变。
    #: 判据由 `verify_audio` 第 3c 节按「同一字段、两种输入、两种结论」成对钉住。
    structure_parsed: bool = True


@dataclass(frozen=True)
class Decoded:
    """解码结果。`waveform is None` 时 `pcm_reason` 必然非空。"""

    media_type: str
    byte_size: int
    waveform: Waveform | None
    pcm_reason: str | None
    facts: ContainerFacts
    #: 与实际分析范围有关的如实说明（超长只分析开头等）
    window_note: str = ""
    analysis_frames: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 类型识别（按魔术字节，不信任扩展名与调用方声明）
# ---------------------------------------------------------------------------

def sniff_media_type(data: bytes) -> str:
    """按**魔术字节**判类型。⚠️ 与图像侧同名函数同义：不信任扩展名。"""
    if len(data) < 12:
        return "application/octet-stream"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        return "audio/aiff"
    if data[:4] == b"fLaC":
        return "audio/flac"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[4:8] == b"ftyp":
        # MP4 家族：品牌决定是纯音频还是视频。M4A / isom / mp42 都可能装 AAC。
        return "audio/mp4"
    if data[:4] == b"#!AM" or data[:5] == b"#!AMR":
        return "audio/amr"
    if data[:4] == b"wvpK" or (data[0:2] == b"0\x26" and data[2:4] == b"\xb2\x75"):
        return "audio/wma"
    if data[:2] == b"\x0b\x77":
        # AC-3（含 E-AC-3）的同步字：每帧开头都是 0x0B77。⚠️ 这一支是 2026-10-05
        # 补的 —— 在此之前 `_OTHER_CONTAINERS` 早就把 `audio/ac3` 列为"认得"，
        # 而**没有任何路径能返回它**：一个死键。真的 `.ac3` 因此被 `analyze()`
        # 拒绝，理由还是假话「不是可识别的音频容器」。见那张表上面的不变量说明。
        return "audio/ac3"
    if data[:3] == b"ID3":
        return "audio/mpeg"
    if _looks_like_mpeg_frame(data, 0):
        return "audio/mpeg"
    return "application/octet-stream"


def pcm_unavailable_text(media_type: str) -> str:
    """"解不出波形"的人话原因。**必须写清后果**，不能只说"不支持"。"""
    head = (
        "本进程没有可用的音频解码器（服务器上既没有 ffmpeg / sox，"
        "也没有 librosa / soundfile 这类解码库）"
    )
    return (
        f"{head}，因此**本次没有解出波形**（{media_type} 是压缩 / 带封装的格式）。"
        "这不等于「没看出问题」，而是**波形域的那几项检查（电网频率 ENF、"
        "底噪一致性、数字静音段、谱不连续、重采样痕迹、样本级不连续）"
        "整项没有执行** —— 而它们才是能发现剪辑 / 拼接的项目。"
        "本报告因此只覆盖容器与编码参数层。"
    )


def identify_only_note(media_type: str) -> str:
    """识别了容器但不做任何解析时的说明（Ogg / M4A / AIFF / AMR）。"""
    name = _OTHER_CONTAINERS.get(media_type, media_type)
    return (
        f"已按魔术字节识别为 {name}，但本进程**没有解析它的内部结构**"
        "（既没有解码器，也没有为这种容器写解析器）。"
        "因此容器内部的编码参数、帧链连续性等线索**本次没有检查**。"
    )


# ---------------------------------------------------------------------------
# WAV / RIFF
# ---------------------------------------------------------------------------

def _iter_riff_chunks(data: bytes) -> list[tuple[str, int, int, int]]:
    """扫 RIFF chunk 链，返回 `[(id, 声明长度, 数据起点, 实际可用长度)]`。

    走到"声明长度超出文件"就停下并**把这一条也返回**（调用方据此判截断）。
    限制最多 512 个 chunk，避免畸形文件把循环拖死。
    """
    out: list[tuple[str, int, int, int]] = []
    pos = 12
    total = len(data)
    for _ in range(512):
        if pos + 8 > total:
            break
        cid = data[pos:pos + 4].decode("latin-1")
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = pos + 8
        avail = max(0, min(size, total - body))
        out.append((cid, size, body, avail))
        if size == 0 and cid in ("data", "fact"):
            break          # 长度为 0 的 data 块：再往下走没有意义
        pos = body + size + (size & 1)     # chunk 按偶数字节对齐
        if pos <= body:
            break
    return out


def _parse_wav(data: bytes) -> tuple[Waveform | None, ContainerFacts, str | None]:
    """解析 WAVE。返回 `(波形或 None, 事实, 解不出波形的原因)`。

    ⚠️ 解析过程中记下的提示都进 `facts.notes`，**不另开一条返回通道** ——
    两份"关于这次解析的说明"一定会漂。
    但 `facts.notes` **本身不是报告的字段** ⇒ 光写进去等于没写，所以
    `decode_audio()` 会把它挂到 `window_note` 上（那是会被打印出来的那一条）。
    ⚠️ 也因此，往 `notes` 里写东西必须发生在 `facts` **构造之前**。
    """
    notes: list[str] = []
    riff_size = int.from_bytes(data[4:8], "little")
    declared_total = riff_size + 8
    chunks = _iter_riff_chunks(data)

    fmt: bytes | None = None
    data_off = data_len = None
    tags: dict[str, str] = {}
    chunk_ids: list[str] = []
    for cid, size, body, avail in chunks:
        chunk_ids.append(cid.strip() or cid)
        if cid == "fmt " and fmt is None:
            fmt = data[body:body + avail]
        elif cid == "data" and data_off is None:
            data_off, data_len = body, size
        elif cid == "LIST" and avail >= 4 and data[body:body + 4] == b"INFO":
            tags.update(_parse_info_list(data, body + 4, avail - 4))

    appended = max(0, len(data) - declared_total)
    truncated = declared_total > len(data)

    if fmt is None or len(fmt) < 16:
        facts = ContainerFacts(
            media_type="audio/wav",
            container="RIFF / WAVE（**缺 fmt 块，无法读取采样参数**）",
            declared_size=declared_total,
            actual_size=len(data),
            appended_bytes=appended,
            truncated=truncated or fmt is None,
            chunks=tuple(chunk_ids),
            tags=tuple(sorted(tags.items())),
            notes=("RIFF 头里没有可用的 fmt 块 —— 文件头不完整或已被破坏。",),
        )
        return None, facts, "WAV 头里没有可用的 fmt 块，读不出采样参数，也无法解出波形。"

    (
        w_format, channels, sample_rate, byte_rate, block_align, bits,
    ) = (
        int.from_bytes(fmt[0:2], "little"),
        int.from_bytes(fmt[2:4], "little"),
        int.from_bytes(fmt[4:8], "little"),
        int.from_bytes(fmt[8:12], "little"),
        int.from_bytes(fmt[12:14], "little"),
        int.from_bytes(fmt[14:16], "little"),
    )
    sub_format = (
        int.from_bytes(fmt[24:26], "little") if w_format == WAV_FORMAT_EXTENSIBLE and len(fmt) >= 26
        else None
    )
    effective = sub_format if sub_format is not None else w_format
    codec_name = _WAV_CODEC_NAMES.get(effective, f"未知编码 0x{effective:04X}")
    container = f"RIFF / WAVE，{codec_name} {bits}-bit"

    declared_data = data_len
    available = 0 if data_off is None else max(0, min(int(data_len or 0), len(data) - data_off))
    frames = None
    if block_align:
        frames = available // block_align

    # ⚠️ 这条 note 必须**在 `facts` 构造之前**算出来并写进 `notes`。
    #    2026-10 实测踩到：它原先写在 `facts = ContainerFacts(...)` **之后**，
    #    于是 append 进的是一个**已经拷进 facts 的列表**（`tuple(notes)` 早拍过照）
    #    ⇒ 这条提示永远到不了报告，实测 `facts.notes == ()`。
    #    它的内容恰恰是本项目最不能静默的那一类："末尾有 N 字节没参与分析"。
    #    （同一件事的另一半在 `decode_audio()` 里：光写进 `facts.notes` 也还不够，
    #      因为解出波形时 `window_note` 总非空、会把它盖掉。）
    block = max(1, (bits // 8) * max(1, channels))
    usable = available - (available % block)
    if (effective in (WAV_FORMAT_PCM, WAV_FORMAT_FLOAT)
            and available and usable != available):
        notes.append(
            f"data 块长度 {available} 不是帧长 {block} 的整数倍，"
            f"末尾 {available - usable} 字节被丢弃。"
        )

    facts = ContainerFacts(
        media_type="audio/wav",
        container=container,
        sample_rate=sample_rate or None,
        channels=channels or None,
        bits_per_sample=bits or None,
        duration_seconds=(frames / sample_rate) if (frames and sample_rate) else None,
        byte_rate=byte_rate or None,
        block_align=block_align or None,
        declared_size=declared_total,
        actual_size=len(data),
        data_bytes=declared_data,
        appended_bytes=appended,
        truncated=truncated or (declared_data is not None and data_off is not None
                                and data_off + declared_data > len(data)),
        chunks=tuple(chunk_ids),
        tags=tuple(sorted(tags.items())),
        notes=tuple(notes),
    )

    if effective not in (WAV_FORMAT_PCM, WAV_FORMAT_FLOAT):
        reason = (
            f"WAV 容器里装的不是未压缩 PCM，而是 **{codec_name}** —— "
            "本进程没有对应的解码器，解不出波形。"
        )
        return None, facts, reason
    if data_off is None or available == 0:
        return None, facts, "WAV 里没有 data 块（或 data 块长度为 0），没有可分析的波形。"

    # `block` / `usable` 已在 `facts` 构造之前算好（那条 note 要在那里落笔），
    # 这里直接拿来切字节 —— 不重算，免得两处对"帧长"的理解漂移。
    raw = data[data_off:data_off + usable]
    try:
        wave = _pcm_from_bytes(raw, effective, bits, channels, sample_rate)
    except (ValueError, ZeroDivisionError) as exc:
        return None, facts, f"PCM 波形解不出来：{exc}"
    return wave, facts, None


def _parse_info_list(data: bytes, off: int, length: int) -> dict[str, str]:
    """解析 `LIST/INFO` 子块（软件名、注释这类文字标签）。"""
    tags: dict[str, str] = {}
    pos = off
    end = off + length
    for _ in range(64):
        if pos + 8 > end:
            break
        tid = data[pos:pos + 4].decode("latin-1")
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = pos + 8
        if size < 0 or body + size > len(data):
            break
        text = data[body:body + size].split(b"\x00", 1)[0]
        try:
            value = text.decode("utf-8")
        except UnicodeDecodeError:
            value = text.decode("latin-1", "replace")
        if value.strip():
            tags[tid] = value.strip()
        pos = body + size + (size & 1)
        if pos <= body:
            break
    return tags


def _pcm_from_bytes(
    raw: bytes, fmt: int, bits: int, channels: int, sample_rate: int
) -> Waveform:
    """把 PCM 字节转成 `(frames, channels)` 的 float64（归一化到 [-1, 1]）。

    ⚠️ `audioop` 在 Python 3.13 已移除，所以这里是**手写的换算**。
    8 位 PCM 是**无符号**（偏移 128），其余位深是有符号/浮点 —— 这一条最容易写错，
    写错的后果是波形整体偏移，而所有统计量照样能算出来、照样"通过"。
    """
    channels = max(1, int(channels))
    if fmt == WAV_FORMAT_FLOAT:
        if bits == 32:
            arr = np.frombuffer(raw, dtype="<f4").astype(np.float64)
        elif bits == 64:
            arr = np.frombuffer(raw, dtype="<f8").astype(np.float64)
        else:
            raise ValueError(f"浮点 WAV 的位深只能是 32 或 64，读到 {bits}")
    elif bits == 8:
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif bits == 16:
        arr = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8)
        b = b[: len(b) - (len(b) % 3)].reshape(-1, 3)
        v = (
            b[:, 0].astype(np.int32)
            | (b[:, 1].astype(np.int32) << 8)
            | (b[:, 2].astype(np.int32) << 16)
        )
        v = np.where(v >= (1 << 23), v - (1 << 24), v)
        arr = v.astype(np.float64) / 8388608.0
    elif bits == 32:
        arr = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"不支持的 PCM 位深：{bits}")

    # 统一成二维 `(frames, channels)`：**不区分单双声道**，免得下游还要判 ndim。
    arr = arr[: len(arr) - (len(arr) % channels)].reshape(-1, channels)
    return Waveform(
        samples=arr,
        sample_rate=int(sample_rate) or 1,
        channels=channels,
        bits_per_sample=int(bits),
        encoding=f"pcm_{'f' if fmt == WAV_FORMAT_FLOAT else 'i'}{bits}"
                 f"{'le' if bits > 8 else ''}",
    )


# ---------------------------------------------------------------------------
# MPEG（MP3）
# ---------------------------------------------------------------------------

#: MPEG 版本 / 层 → (采样率表)
_MPEG_SR = {
    3: (44100, 48000, 32000),      # MPEG 1
    2: (22050, 24000, 16000),      # MPEG 2
    0: (11025, 12000, 8000),       # MPEG 2.5
}

_MPEG_BR_V1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_MPEG_BR_V2_L3 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)

_MPEG_CHANNEL_MODES = ("stereo", "joint_stereo", "dual_channel", "mono")


def _looks_like_mpeg_frame(data: bytes, pos: int) -> bool:
    """`pos` 处是不是一个**字段全部合法**的 MPEG 帧头。

    ⚠️ 必须逐字段校验（版本 / 层 / 比特率 / 采样率都不能是保留值）：
    只判 `FF Ex` 两位会把大量随机字节认成帧 —— 那会让"帧链断裂"这类结论
    变成噪声驱动的假线索。
    """
    if pos + 4 > len(data):
        return False
    b0, b1, b2 = data[pos], data[pos + 1], data[pos + 2]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return False
    version = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    if version == 0b01 or layer == 0b00:
        return False
    br_index = (b2 >> 4) & 0x0F
    sr_index = (b2 >> 2) & 0x03
    return br_index not in (0x00, 0x0F) and sr_index != 0x03


def _parse_mp3(data: bytes) -> tuple[ContainerFacts, list[str]]:
    """解析 MPEG 帧头链。**不解码波形**，但帧头参数本身就是鉴伪线索。"""
    notes: list[str] = []
    pos = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        size = 0
        for byte in data[6:10]:
            size = (size << 7) | (byte & 0x7F)
        pos = 10 + size
        if pos > len(data):
            pos = 0
            notes.append("ID3v2 头声明的长度超出文件，已从头开始扫描。")

    sample_rate = None
    channels = None
    frame_count = 0
    sample_rates: list[int] = []
    bitrates: list[int] = []
    modes: list[str] = []
    broken = False
    spf_first: int | None = None      # 每帧样本数（由第一帧的版本/层决定）
    first = pos

    for _ in range(200_000):
        if not _looks_like_mpeg_frame(data, pos):
            break
        b1, b2, b3 = data[pos + 1], data[pos + 2], data[pos + 3]
        version = (b1 >> 3) & 0x03
        layer = (b1 >> 1) & 0x03
        br_index = (b2 >> 4) & 0x0F
        sr_index = (b2 >> 2) & 0x03
        padding = (b2 >> 1) & 0x01
        mode = _MPEG_CHANNEL_MODES[(b3 >> 6) & 0x03]
        sr = _MPEG_SR[version][sr_index]
        table = _MPEG_BR_V1_L3 if version == 3 else _MPEG_BR_V2_L3
        br = table[br_index] * 1000
        if layer == 0b11:      # Layer I
            frame_len = (12 * br // sr + padding) * 4
            samples_per_frame = 384
        elif layer == 0b10:    # Layer II
            frame_len = 144 * br // sr + padding
            samples_per_frame = 1152
        else:                  # Layer III
            coef = 144 if version == 3 else 72
            frame_len = coef * br // sr + padding
            samples_per_frame = 1152 if version == 3 else 576
        if frame_len <= 0:
            break
        if spf_first is None:
            spf_first = samples_per_frame
        frame_count += 1
        sample_rates.append(sr)
        bitrates.append(br)
        modes.append(mode)
        if sample_rate is None:
            sample_rate = sr
            channels = 1 if mode == "mono" else 2
        pos += frame_len

    if frame_count and pos < len(data) - 4:
        # 帧链中断之后，后面还能不能找到合法的帧？
        # 这里只是"如实记录事实"，**判不判异常由 forensics 决定**。
        probe = pos
        limit = min(len(data), pos + 8192)
        while probe < limit:
            if _looks_like_mpeg_frame(data, probe):
                broken = True
                break
            probe += 1

    trailing: list[str] = []
    tail = data[-128:]
    if len(data) >= 128 and tail[:3] == b"TAG":
        trailing.append("ID3v1（尾部 128 字节标签）")
    if b"APETAGEX" in data[-64:]:
        trailing.append("APE 尾部标签")
    tail_region = data[-5100:] if len(data) > 5100 else data
    if b"LYRICSBEGIN" in tail_region:
        trailing.append("Lyrics3 尾部标签")

    duration = None
    if frame_count and sample_rate and spf_first:
        # 每帧样本数由**第一帧**的版本/层决定；同一文件里版本不会混。
        duration = frame_count * spf_first / sample_rate

    container = "MPEG 音频（MP3）"
    if not frame_count:
        notes.append("从头到尾没能解析出任何一帧 —— 可能是 MPEG 帧同步之前的私有数据，或文件已被破坏。")
    if first:
        notes.append(f"跳过 ID3v2 标签 {first} 字节。")

    return ContainerFacts(
        media_type="audio/mpeg",
        container=container,
        sample_rate=sample_rate,
        channels=channels,
        bits_per_sample=None,
        duration_seconds=duration,
        declared_size=None,
        actual_size=len(data),
        appended_bytes=0,
        truncated=False,
        chunks=(),          # chunk 是 RIFF 概念，MPEG 里没有
        frame_count=frame_count,
        sample_rates=tuple(sorted(set(sample_rates))),
        bitrates=tuple(sorted(set(bitrates))),
        channel_modes=tuple(sorted(set(modes))),
        frame_chain_broken=broken,
        trailing_tags=tuple(trailing),
        notes=tuple(notes),
    ), notes


# ---------------------------------------------------------------------------
# FLAC（只读 STREAMINFO）
# ---------------------------------------------------------------------------

def _parse_flac(data: bytes) -> ContainerFacts:
    """读 FLAC 的 STREAMINFO 元数据块（34 字节，字段是位打包的）。"""
    if len(data) < 8 + 4 + 34:
        return ContainerFacts(
            media_type="audio/flac", container=_OTHER_CONTAINERS["audio/flac"],
            actual_size=len(data), truncated=True,
            notes=("文件太短，读不到 STREAMINFO。",),
        )
    header = data[4:8]
    last = bool(header[0] & 0x80)
    btype = header[0] & 0x7F
    length = int.from_bytes(header[1:4], "big")
    if btype != 0 or length < 34:
        return ContainerFacts(
            media_type="audio/flac", container=_OTHER_CONTAINERS["audio/flac"],
            actual_size=len(data),
            notes=(f"第一个元数据块不是 STREAMINFO（type={btype}），未读取采样参数。",),
        )
    info = data[8:8 + 34]
    bits = int.from_bytes(info[10:18], "big")
    sample_rate = (bits >> 44) & 0xFFFFF
    channels = ((bits >> 41) & 0x07) + 1
    bps = ((bits >> 36) & 0x1F) + 1
    total = bits & ((1 << 36) - 1)
    duration = (total / sample_rate) if sample_rate else None
    return ContainerFacts(
        media_type="audio/flac",
        container=_OTHER_CONTAINERS["audio/flac"],
        sample_rate=sample_rate or None,
        channels=channels,
        bits_per_sample=bps,
        duration_seconds=duration,
        actual_size=len(data),
        declared_size=None,
        notes=(
            "已读取 STREAMINFO（采样率 / 声道 / 位深 / 总样本数来自头部声明）。",
            f"STREAMINFO 是{'最后一个' if last else '非最后一个'}元数据块。",
        ),
    )


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def decode_audio(data: bytes) -> Decoded:
    """字节 → 波形 + 容器事实。**不联网、不抛判定性异常。**

    认不出容器时返回 `media_type="application/octet-stream"`，
    由 `forensics.analyze()` 决定要不要拒绝（判据放在检测层，不散在解码层）。
    """
    media_type = sniff_media_type(data)
    note_extra = ""

    if media_type == "audio/wav":
        wave, facts, reason = _parse_wav(data)
    elif media_type == "audio/mpeg":
        facts, _notes = _parse_mp3(data)
        wave, reason = None, pcm_unavailable_text(media_type)
    elif media_type == "audio/flac":
        facts = _parse_flac(data)
        wave, reason = None, pcm_unavailable_text(media_type)
    elif media_type in _OTHER_CONTAINERS:
        facts = ContainerFacts(
            media_type=media_type,
            container=_OTHER_CONTAINERS[media_type],
            actual_size=len(data),
            notes=(identify_only_note(media_type),),
            # ⚠️ 这一支**只认得、不解析** ⇒ 必须如实标出来。不标的后果是检测层
            #    会把"没跑过的自洽性检查"写成正面结论（见该字段的说明）。
            structure_parsed=False,
        )
        wave, reason = None, pcm_unavailable_text(media_type)
    else:
        return Decoded(
            media_type=media_type,
            byte_size=len(data),
            waveform=None,
            pcm_reason="按魔术字节判定，这份字节不是本项目能识别的音频容器。",
            facts=ContainerFacts(
                media_type=media_type, container="未能识别", actual_size=len(data),
                # 认都认不出来，更谈不上解析 —— 同一条纪律。
                structure_parsed=False,
            ),
        )

    window_note = ""
    frames = 0
    # ⚠️ `note_extra` 必须在**用它的地方之前**算出来。它原先在 return 之前才拼，
    #    于是解出波形的那些路径根本拿不到它 —— 而那些路径正是"有话说"的那些。
    note_extra = "；".join(facts.notes) if facts.notes else ""
    if wave is not None and wave.frames > MAX_ANALYSIS_FRAMES:
        cut = MAX_ANALYSIS_FRAMES
        wave = Waveform(
            samples=wave.samples[:cut],
            sample_rate=wave.sample_rate,
            channels=wave.channels,
            bits_per_sample=wave.bits_per_sample,
            encoding=wave.encoding,
        )
        window_note = (
            f"⚠️ 原始时长 {facts.duration_seconds:.1f} 秒**超过分析上限**，"
            f"本次**只分析了开头的 {wave.duration:.1f} 秒**（{MAX_ANALYSIS_FRAMES} 帧）。"
            "这不代表后半段没有痕迹 —— 需要覆盖全程时请分段提交。"
        )
    if wave is not None:
        frames = wave.frames
        if not window_note:
            window_note = f"本次分析了**全长 {wave.duration:.2f} 秒**。"
        # ⚠️ 解析期的提示（例如"末尾 12 字节没参与分析"）**必须挂在看得见的地方**。
        #    `facts.notes` 不是报告的字段，而解出波形时 `window_note` 又一定非空
        #    ⇒ 只写进 `facts.notes` 等于没写 —— 那是"收下了却看不见"，
        #    与本项目最忌讳的"静默截断"是同一件事。
        if note_extra:
            window_note += f"（解析提示：{note_extra}）"
        if wave.channels > 1:
            window_note += f"（{wave.channels} 声道，波形域统计在**声道平均**后的单声道上做。）"

    return Decoded(
        media_type=media_type,
        byte_size=len(data),
        waveform=wave,
        pcm_reason=reason,
        facts=facts,
        window_note=window_note or (note_extra or "本次没有可分析的波形。"),
        analysis_frames=frames,
    )


__all__ = [
    "ContainerFacts",
    "Decoded",
    "MAX_ANALYSIS_FRAMES",
    "WAV_FORMAT_EXTENSIBLE",
    "WAV_FORMAT_FLOAT",
    "WAV_FORMAT_PCM",
    "Waveform",
    "decode_audio",
    "identify_only_note",
    "pcm_unavailable_text",
    "sniff_media_type",
]
