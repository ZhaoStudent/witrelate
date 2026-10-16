"""音频鉴伪的检测核心 —— **纯函数、无模型、不联网、可离线断言**。

════════════════════════════════════════════════════════════════════════
九项检查，每一项都对应一种公开发表的音频取证手段
════════════════════════════════════════════════════════════════════════

**容器层（不需要波形，压缩格式也跑得了）**

  1. `container`     容器自洽性：RIFF 声明长度 / data 块长度 / 帧链完整性 /
                     尾部标签；以及"调用方声明的类型"与"真实容器"是否一致
  2. `codec_params`  编码参数自洽性：`byte_rate == 采样率×声道×位深/8`？
                     `block_align == 声道×位深/8`？MP3 的采样率 / 声道模式是否跳变？
                     （⚠️ MP3 的**比特率**跳变是 VBR，正常，不算线索）
  3. `metadata`      容器内可见的文字标签（软件名、注释）。命中已知编辑软件即提示。

**波形层（需要 PCM 波形）**

  4. `spectral_cutoff`  频谱高端**硬截止**。有损编码（MP3/AAC）会砍掉高频，
                        再用 WAV 存下来时那个截止还在 —— 是"曾经被重编码"的直接线索。
                        也能抓重采样（上采样会在原奈奎斯特频率留下台阶）。
  5. `noise_floor`      背景噪声一致性。同一环境连续录音的底噪水平应大致恒定；
                        把两段不同环境的录音拼起来，安静段的底噪水平会明显不同。
  6. `silence`          **数字静音**段。真实录音的"安静"里总有底噪；
                        样本恰好为 0 的长段只可能来自软件插入 —— 是最强的剪接线索之一。
  7. `spectral_flux`    谱**形状**的不连续（帧间归一化谱的余弦距离尖峰）。
  8. `sample_discontinuity` 样本级不连续（波形上的"咔哒"），剪辑点常见的瞬态。
  9. `enf`              电网频率（ENF）痕迹。市电会给录音引入 50/60 Hz 及其谐波的
                        微弱哼声，其相位在时间上是**连续**的；剪辑会造成相位跳变。
                        ⚠️ 检出率本来就不高（手机录音里往往没有），**检不出即弃权**。

════════════════════════════════════════════════════════════════════════
三条与图像链同源的纪律
════════════════════════════════════════════════════════════════════════

1. **不输出「真 / 假」**。只有线索强度 + 逐项 caveat + 综合等级 + 升级建议。
2. **每条发现必须自带 `caveat`**（它**不能**证明什么）。
3. **「测不了」绝不能写成「没问题」**：任何一项因前提不满足而没跑，都必须
   以 `verdict="无法判定"` + 前缀的 detail 出现。音频链的弃权比例**天然比
   图像链高**（压缩格式解不出波形、纯音不适用、样本太短、ENF 检不出……），
   所以这条纪律在这里更吃紧。
4. ⚠️ **弃权有轻重之分，前缀必须区分开**（2026-10 踩到）：
   `GATE_ABSTAIN_PREFIX` 只留给**材料级**前提 —— 没有波形 / 太短 / 近乎无声，
   这三种一旦成立，**波形域六项会一起失效**，此时整份报告确实只能说
   "测不了"。而「这段没有第二处安静区可对照」「这段没有市电痕迹」
   `「这段是纯音」是**逐项**的数据限制，其余各项照样跑通了 —— 若也打同一个
   前缀，`summarize()` 会把整份报告一律压成「无法判定」：
   结果是**任何一段正常录音都得到「无法判定」**（因为 ENF 在真实录音里
   本来就常常检不出），报告于是彻底失去信息量，用户只能绕开它。
   两条都不许走极端：闸门要触发得**准**，逐项弃权要**写得明**。

依赖：numpy（**不引入 librosa / soundfile / scipy** —— 服务器上没有，
装不上，且它们会把 numba/llvmlite 一类重依赖拖进来）。
"""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

import numpy as np

from ..evidence.contracts import Finding, UpgradePath
from ..verdict import ABSTAIN, summarize_findings
from .contracts import AudioForensicsReport
# ⚠️ 刻意 import 私名 `_OTHER_CONTAINERS`：它是**解码层的事实**（"我认得这些容器"），
#    而"收不收它"是**本层的策略**。接受判据必须**现场读它** —— 各写一份就是
#    "同一件事写两处"。2026-10-05 实测：这张表的键**全部**带 `audio/` 前缀，
#    所以今天把前缀测试换成读表**行为完全等价**（纯结构保险）；而视频那侧的同一条
#    判据是真的会咬人的（那张表里有 `application/ogg`，前缀测试把 Ogg 挡在了门外）。
#    加容器的方式随之变成**只改解码层那张表**，判据自动跟上。
from .decode import (
    INFO_TAG_LABELS,
    WAV_CHUNK_NOTES,
    ContainerFacts,
    Waveform,
    _OTHER_CONTAINERS,
    decode_audio,
    identify_only_note,
)

#: **材料级**闸门前缀：由 `_wave_gates()` 独占使用。
#:
#: 含义严格限定为「**波形域整域都没跑**」—— 没有解出波形 / 太短 / 近乎无声。
#: 只有这三种情形会同时让六个波形域检查失效，此时整份报告才该说「无法判定」。
#: **逐项的弃权不许用这个前缀**（理由见模块 docstring 第 4 条）。
#: `summarize()` 靠它把"没检查"从"检查过没问题"里分出来。
GATE_ABSTAIN_PREFIX = "音轨不满足波形域检查的前提"

#: **逐项**弃权前缀：某一项因为"这段音轨上无从检验"而没跑（纯音谈频谱截止、
#: 只有一处安静区谈底噪一致性、检不出市电痕迹谈相位连续性……）。
#:
#: ⚠️ 它**不是**闸门：其余各项已经跑通并给出了结论，所以综合等级照常按
#: 加权规则算。但这一项在 `findings` 里仍然是 `无法判定`，`detail` 里写清
#: 为什么 —— 报告读者看得见"这一项没测"，而不会被含糊的「未提示异常」骗过。
NOT_APPLICABLE_PREFIX = "本项对这段音轨无从执行"

#: 各检查项的权重 —— 综合等级由它算出，**不是模型给的**。
#: 口径：能直接指向"两段不同来源被拼在一起"的项给足权重；
#: 间接项（要配合其它线索才成立）给一半。**这是专家先验，不是拟合出来的**，
#: 改任何一个都要问一句"凭什么"。
CHECK_WEIGHTS = {
    "enf": 1.5,                   # 最强：相位不连续几乎只可能来自剪辑
    "silence": 1.0,               # 数字静音段：软件插入的直接产物
    "noise_floor": 1.0,           # 不同环境的底噪水平不同
    "codec_params": 1.0,          # 采样率/声道模式跳变 = 两段不同来源
    "spectral_flux": 0.5,         # 瞬态干扰多，只作辅助
    "sample_discontinuity": 0.5,  # 同上
    "spectral_cutoff": 0.5,       # 只能说明"曾重编码"，不能说明"被剪过"
    "container": 0.5,
    "metadata": 0.5,
}

#: 编辑 / 转码软件在元数据里常留下的署名（命中即提示"经过软件处理"）
EDITOR_SOFTWARE_HINTS = (
    "audacity", "adobe audition", "audition", "cool edit", "goldwave",
    "soundforge", "sound forge", "wavosaur", "reaper", "izotope", "waves",
    "pro tools", "protools", "logic pro", "garageband", "cubase", "nuendo",
    "studio one", "fl studio", "wavepad", "ocenaudio", "twistedwave",
    "ffmpeg", "sox", "avconv", "格式工厂", "formatfactory", "剪映", "capcut",
    "迅捷", "风云", "goldwave",
)

#: 常见 MIME 写法 → 本项目按魔术字节判出的规范名。
#: ⚠️ 与图像侧同一条纪律：**这是显式别名表，不是模糊匹配**。
#: 只吸收**同一个容器**的不同写法；"前缀相同就算一致"会把
#: `audio/wav` 与 `audio/wave`（同一个）与 `audio/x-wav` 之外的东西一并放过，
#: 于是"声明与真实容器不符"这个检查就废了。
MIME_ALIASES = {
    "audio/wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/x-pn-wav": "audio/wav",
    "audio/mpeg": "audio/mpeg",
    "audio/mp3": "audio/mpeg",
    "audio/x-mp3": "audio/mpeg",
    "audio/mpeg3": "audio/mpeg",
    "audio/x-mpeg": "audio/mpeg",
    "audio/x-mpeg-3": "audio/mpeg",
    "audio/mp4": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
    "audio/aac": "audio/mp4",
    "audio/x-aac": "audio/mp4",
    "video/mp4": "audio/mp4",
    "audio/flac": "audio/flac",
    "audio/x-flac": "audio/flac",
    "audio/ogg": "audio/ogg",
    "application/ogg": "audio/ogg",
    "audio/vorbis": "audio/ogg",
    "audio/opus": "audio/ogg",
    "audio/aiff": "audio/aiff",
    "audio/x-aiff": "audio/aiff",
    "audio/amr": "audio/amr",
    # ⚠️ 下面两族是 2026-10-05 补的，而**缺的从来不是"容器"、是这里的别名**：
    #    ① WMA —— 解码层按 ASF 头认它并报 `audio/wma`，而 `.wma` 在业界通用的
    #       MIME 恰恰是 `audio/x-ms-wma`。别名表里没有它 ⇒ 一份**声明正确**的 WMA
    #       会吃到假线索「声明与真实容器不一致」，综合等级还会被从「无法判定」
    #       抬到「低」。这与视频侧 `video/ogg` vs `application/ogg` 是同一个病。
    #    ② AC-3 —— `audio/ac3` 是嗅探器会返回的规范名（必须能归一到自己），
    #       `audio/x-ac3` / `audio/vnd.dolby.dd-raw` 是常见的第三方写法。
    "audio/wma": "audio/wma",
    "audio/x-ms-wma": "audio/wma",
    "audio/x-wma": "audio/wma",
    "audio/ac3": "audio/ac3",
    "audio/x-ac3": "audio/ac3",
    "audio/vnd.dolby.dd-raw": "audio/ac3",
}

DISCLAIMER = (
    "本报告是基于公开音频取证方法得出的**技术线索**，不是司法鉴定意见，"
    "不能单独作为认定事实的依据。所列「未提示异常」**不等于**该录音未经过剪辑；"
    "所列「提示异常」**不等于**该录音被篡改 —— 转码、降噪、平台压缩、"
    "录音环境变化都会产生同类痕迹。如需具备法律效力的结论，请走司法鉴定路径。"
)

CHAIN_OF_CUSTODY = [
    "立即保全**原始文件**（原始录音设备 / 存储卡 / 手机内部存储），"
    "**不要**用微信、QQ、邮件转发件代替 —— 转发会重新编码，"
    "底噪与编码痕迹全变，之后再鉴定就失去了比对基准。",
    "对原文件计算 SHA-256 并记录时间与操作人；本次报告的 sha256 字段即为该值。",
    "原始文件与任何转发件**分别保全、分别记录来源**：它们不是同一份材料。",
    "任何后续取证都在**只读副本**上进行，原件封装保存 —— 司法鉴定通常要求提交原始载体。",
]

#: 波形域检查的最小样本量（秒）。低于它的统计量不可信 ⇒ 弃权，不给档位。
MIN_WAVEFORM_SECONDS = 0.6
#: 「近乎无声」的判据（dBFS）。低于它的音轨，所有基于电平的统计都没有意义。
SILENCE_FLOOR_DBFS = -70.0
#: 数字静音判据：归一化后 |x| 小于它即视为"恰好为零"。
#: 16 位 PCM 的最低有效位是 3.05e-5，所以这个阈值实际上只命中**精确的 0**。
DIGITAL_SILENCE_EPS = 1e-7


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_mime(value: str | None) -> str | None:
    """把调用方声明的 MIME 归一成规范名；认不出就原样返回（小写、去参数）。"""
    text = (value or "").strip().lower().split(";", 1)[0].strip()
    if not text:
        return None
    return MIME_ALIASES.get(text, text)


def _db(x: np.ndarray | float) -> np.ndarray | float:
    return 10.0 * np.log10(np.asarray(x, dtype=np.float64) + 1e-20)


def dbfs(x: np.ndarray) -> float:
    """整段电平（dBFS）。全零信号返回 -200（而不是 -inf，便于比较与打印）。"""
    arr = np.asarray(x, dtype=np.float64)
    if arr.size == 0:
        return -200.0
    rms = float(np.sqrt(np.mean(arr * arr)))
    return 20.0 * float(np.log10(rms)) if rms > 0 else -200.0


def robust_threshold(values: np.ndarray, k: float = 6.0) -> tuple[float, float] | None:
    """稳健的"正常范围上界"：`中位数 + k × 1.4826 × MAD`。

    ⚠️ **刻意不用「均值 + kσ」**：要检测的那几个突变点本身会把均值和标准差
    一起拉高（尤其当它离群很远时），于是阈值水涨船高、反而检不出来 ——
    这是"用被污染的统计量去判污染"的经典错误。中位数与 MAD 对离群点不敏感。

    返回 `None` 表示**这批数值离散度为零**（比如整段完全恒定）——
    此时"离群"没有定义，调用方必须**弃权**，不能当成"没有离群"。
    """
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        return None
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    scale = 1.4826 * mad
    if scale <= 1e-12:
        std = float(np.std(arr))
        if std <= 1e-12:
            return None
        scale = std
    return med, med + k * scale


def welch_psd(
    x: np.ndarray, sample_rate: int, *, nfft: int = 2048, max_frames: int = 192
) -> tuple[np.ndarray, np.ndarray]:
    """分段平均周期图（Welch 法）—— 自实现，只依赖 numpy。

    返回 `(频率轴, 功率谱密度)`。分帧数上限 `max_frames`：
    对超长音轨**等间隔取样**若干帧而不是全跑一遍，否则 8 MB 的单声道 WAV
    会产出一万多个帧、白白吃掉几百 MB 内存（服务器还要同时跑 8 个角色）。
    """
    arr = np.asarray(x, dtype=np.float64).ravel()
    nfft = int(min(nfft, max(64, 1 << int(np.floor(np.log2(max(64, arr.size)))))))
    if arr.size < nfft:
        arr = np.pad(arr, (0, nfft - arr.size))
    hop = max(1, nfft // 2)
    total = 1 + (arr.size - nfft) // hop
    starts = (
        np.linspace(0, arr.size - nfft, num=max_frames).astype(np.int64)
        if total > max_frames
        else np.arange(total, dtype=np.int64) * hop
    )
    win = np.hanning(nfft)
    idx = starts[:, None] + np.arange(nfft)[None, :]
    frames = arr[idx] * win[None, :]
    spec = np.fft.rfft(frames, axis=1)
    psd = (np.abs(spec) ** 2).mean(axis=0)
    # 归一化到"每 Hz 的功率"，与窗能量和采样率有关；这里只用于**相对比较**
    norm = float((win ** 2).sum()) * float(sample_rate)
    if norm > 0:
        psd = psd / norm
    freqs = np.fft.rfftfreq(nfft, d=1.0 / float(sample_rate))
    return freqs, psd


def goertzel(x: np.ndarray, freq: float, sample_rate: int) -> complex:
    """非整数频点的单点 DFT（Goertzel 的直算形式）—— 取该频率的复幅度。

    比 `rfft` 更适合"只关心某几个频点"的场景：代价是 O(N) 而不是 O(N log N)，
    但只算一个点，反而更快，而且**频率不必落在 FFT 栅格上**
    （ENF 要跟踪的是 50 Hz 附近 ±0.1 Hz 的变化，栅格化会把它抹平）。
    """
    arr = np.asarray(x, dtype=np.float64).ravel()
    if arr.size == 0:
        return 0j
    n = np.arange(arr.size, dtype=np.float64)
    return complex(np.sum(arr * np.exp(-2j * np.pi * freq * n / float(sample_rate))))


def steepest_drop_db_per_khz(
    freqs: np.ndarray, db: np.ndarray, around_hz: float, span_hz: float = 400.0
) -> float:
    """`around_hz` 附近最陡的**下降速率**（dB/kHz，正数表示往下掉得多快）。

    频带边缘的"硬截止"与"自然滚降"在**下降速率**上差一个量级，所以这是
    区分"有损编码留下的低通"与"设备本身的频率响应"的关键量。
    """
    if freqs.size < 4:
        return 0.0
    df = float(freqs[1] - freqs[0])
    if df <= 0:
        return 0.0
    band = (freqs >= max(0.0, around_hz - span_hz)) & (freqs <= around_hz + span_hz)
    if np.count_nonzero(band) < 3:
        return 0.0
    seg = np.asarray(db[band], dtype=np.float64)
    slope = np.diff(seg) / df          # dB/Hz
    return float(max(0.0, -np.min(slope) * 1000.0))


def parabolic_peak_hz(
    freqs: np.ndarray, psd: np.ndarray, lo: float, hi: float
) -> tuple[float, float] | None:
    """`[lo, hi]` 内的谱峰位置（**对数域抛物线插值**，比取 bin 中心细一个量级）。

    返回 `(峰频率 Hz, 峰值)`；带内没有可用点时为 `None`。

    ⚠️ 为什么必须插值：电网频率的漂移只有 ±0.05 Hz 量级，而 8192 点 FFT 在
    44.1 kHz 下的栅格是 5.4 Hz —— 直接取 bin 中心的话，所有窗都会落到**同一个
    bin** 上，于是"逐窗基频标准差"恒为 0、稳定性检查**永远通过**。
    那不是"检出了稳恒的市电痕迹"，而是**判据被分辨率抹平了**。
    """
    band = (freqs >= lo) & (freqs <= hi)
    idx = np.flatnonzero(band)
    if idx.size < 3:
        return None
    k = int(idx[int(np.argmax(psd[band]))])
    if k <= 0 or k >= psd.size - 1:
        return None
    y0, y1, y2 = (float(np.log(psd[k - 1] + 1e-30)), float(np.log(psd[k] + 1e-30)),
                  float(np.log(psd[k + 1] + 1e-30)))
    denom = y0 - 2.0 * y1 + y2
    delta = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-18 else 0.0
    delta = max(-0.5, min(0.5, delta))
    df = float(freqs[1] - freqs[0])
    return float(freqs[k] + delta * df), float(psd[k])


def runs_of(mask: np.ndarray) -> list[tuple[int, int]]:
    """把布尔数组切成连续的 `True` 区间 `[(起点, 终点))`。纯 numpy，边界安全。"""
    m = np.asarray(mask, dtype=bool).ravel()
    if m.size == 0:
        return []
    padded = np.concatenate(([False], m, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(edges[i]), int(edges[i + 1])) for i in range(0, len(edges), 2)]


# ---------------------------------------------------------------------------
# Finding 构造器（三个，覆盖全部取值）
# ---------------------------------------------------------------------------

def _finding(check: str, verdict: str, detail: str, caveat: str,
             strength: float | None = None) -> Finding:
    return Finding(check=check, verdict=verdict, detail=detail, caveat=caveat,
                   strength=strength)


def _abstain(check: str, reason: str, caveat: str) -> Finding:
    """**波形域整域没跑**。前缀是判定用的，原因写在冒号后面。

    ⚠️ **只允许 `_wave_gates()` 调用本函数**。逐项的"这项测不了"要走
    `_na()` —— 两者的区别是「整份报告还能不能下结论」，混用会让正常录音
    一律变成「无法判定」（见模块 docstring 第 4 条）。
    """
    return _finding(check, "无法判定", f"{GATE_ABSTAIN_PREFIX}：{reason}", caveat)


def _na(check: str, reason: str, caveat: str) -> Finding:
    """**这一项**在这段音轨上无从检验（其余各项照常跑）。

    与 `_abstain()` 一样是 `无法判定`、一样把原因写清楚，唯一的区别是
    **不打闸门前缀**，因此不会把整份报告的结论压成「无法判定」。

    什么时候该用它：弃权的原因是**这段音轨的性质**（是纯音、没有第二处
    安静区、没有市电痕迹），而不是"整个波形域没法看"（没解出波形 / 太短 /
    近乎无声）。
    """
    return _finding(check, "无法判定", f"{NOT_APPLICABLE_PREFIX}：{reason}", caveat)


def _clean(check: str, detail: str, caveat: str) -> Finding:
    return _finding(check, "未提示异常", detail, caveat)


def _alert(check: str, detail: str, caveat: str, strength: float | None = None) -> Finding:
    return _finding(check, "提示异常", detail, caveat, strength)


def _wave_gates(wave: Waveform | None, check: str, caveat: str) -> Finding | None:
    """波形层的公共前置闸门：没波形 / 太短 / 近乎无声 ⇒ 弃权。

    返回 `None` 表示**可以继续**。抽出来是为了让五个波形域检查
    **用同一套闸门**（各写一份的话，改一处必然漏掉另一处）。
    """
    if wave is None:
        return _abstain(
            check,
            "本次**没有解出波形**（压缩 / 带封装的格式，本进程没有解码器），"
            "该项整项没有执行。",
            caveat,
        )
    x = wave.mono()
    if x.size < int(wave.sample_rate * MIN_WAVEFORM_SECONDS):
        return _abstain(
            check,
            f"音轨只有 {x.size / wave.sample_rate:.2f} 秒，短于 "
            f"{MIN_WAVEFORM_SECONDS} 秒，统计量不可信，该项整项没有执行。",
            caveat,
        )
    level = dbfs(x)
    if level < SILENCE_FLOOR_DBFS:
        return _abstain(
            check,
            f"整段电平只有 {level:.1f} dBFS（近乎无声），"
            "没有可分析的底噪与频谱结构，该项整项没有执行。",
            caveat,
        )
    return None


# ---------------------------------------------------------------------------
# 1. container —— 容器自洽性 + 声明一致性
# ---------------------------------------------------------------------------

def check_container(facts: ContainerFacts, declared_mime: str | None) -> Finding:
    """容器层事实与**内部自洽性**。

    这里报的不是"这个文件可疑"，而是"这个文件的头部与它自己的内容对不上"
    —— 例如 RIFF 声明 1,000,000 字节、实际只有 998,000（截断），
    或 data 块结束后还挂着 4 KB 的额外数据（拼接时没修头）。
    """
    check = "container"
    issues: list[str] = []
    if facts.truncated:
        issues.append(
            "容器声明的长度**大于**实际可用字节（文件被截断，或头部被改过）"
        )
    if facts.appended_bytes > 0:
        issues.append(
            f"容器声明范围之外还有 **{facts.appended_bytes} 字节**额外数据"
            "（合法的写入器不会这么干；常见于手工拼接或附加了别的格式的尾巴）"
        )
    if facts.frame_chain_broken:
        issues.append(
            "MPEG 帧链**中断**：连不上帧头的位置之后又出现了合法帧"
            "（正常编码器产出的帧是连续的）"
        )
    if facts.trailing_tags:
        issues.append(f"文件尾部带标签：{'、'.join(facts.trailing_tags)}")

    declared = normalize_mime(declared_mime)
    if declared is None:
        mime_note = "调用方**没有声明**类型，本次不比对「声明与真实容器是否一致」。"
    elif declared != facts.media_type:
        issues.append(
            f"调用方声明的是 `{declared_mime}`，而**字节本身**是 `{facts.media_type}`"
            "（按魔术字节判定）"
        )
        mime_note = ""
    else:
        mime_note = f"声明的类型 `{declared_mime}` 与真实容器一致。"

    chunk_note = ""
    if facts.chunks:
        known = [f"{c}（{WAV_CHUNK_NOTES[c]}）" if c in WAV_CHUNK_NOTES else c
                 for c in facts.chunks]
        chunk_note = f"RIFF 块序列：{'、'.join(known)}。"

    caveat = (
        "容器层只说明**文件是怎么被写出来的**，不说明录音内容被动过。"
        "截断与附加数据也可能是下载中断、存储损坏、或播放器写入标签造成的。"
    )
    if issues:
        detail = (
            f"容器为 {facts.container}；发现 {len(issues)} 处不自洽："
            + "；".join(issues)
            + "。"
            + (f" {mime_note}" if mime_note else "")
            + (f" {chunk_note}" if chunk_note else "")
        )
        return _alert(check, detail, caveat, strength=min(1.0, 0.4 + 0.2 * len(issues)))

    if facts.declared_size is None:
        # ⚠️ 「认得」≠「看过里面」。2026-10-05 实测：这一支原先与**能比对长度**的容器
        #    共用下面那句「声明的长度与实际字节**相符**，没有附加数据或尾部标签」，
        #    而**没有长度声明可比的容器**根本不成立这句话：
        #      · 只识别、没解析的（Ogg / M4A / AIFF / AMR / WMA / AC-3）—— 没读过内部；
        #      · 有解析器的 MP3 / FLAC **也不写总长**（帧流 / 块链，头里没有文件长度），
        #        实测 `declared_size is None`。
        #    把"没做过的检查"写成正面结论是本仓最危险的一类缺陷（第 3b 节整节在修它）。
        #    ⇒ 判据就是**实测出来的那个事实**：有没有可比的长度声明。
        #    两种"没有"的**说法不同**（一种是压根没解析，一种是这种容器就不写总长），
        #    所以分两句写；句子仍从解码层那份说明取（唯一来源），不在这里另写一句。
        if facts.structure_parsed:
            why = ("这种容器**不在文件头里写总长**（帧流 / 块链），"
                   "没有可比的长度声明")
        else:
            why = identify_only_note(facts.media_type)
        detail = f"容器为 {facts.container}；{why}"
        if facts.structure_parsed:
            detail += ("，因此容器自洽性（声明长度 / 附加数据 / 尾部标签）"
                       "本次**没有检查**。")
        if mime_note:
            detail += f" {mime_note}"
        return _clean(
            check, detail,
            caveat + "⚠️ 这次**没有**检查容器自洽性（声明长度 / 附加数据 / 尾部标签）"
                     "—— 这份字节里没有可比的长度声明，比不了。",
        )

    detail = f"容器为 {facts.container}；声明的长度与实际字节**相符**，没有附加数据或尾部标签。"
    # ⚠️ 这里原先写成 `if not mime_note.startswith("调用方"): … elif mime_note: …` ——
    #    两个分支体**完全相同**，于是那个 `elif` 永远不会产生不同行为。
    #    读代码的人会以为这两种情形被区别对待（"声明一致"与"没声明"），
    #    而实际上它们走的是同一句追加。合并成一句，语义不变、歧义消失。
    if mime_note:
        detail += f" {mime_note}"
    if chunk_note:
        detail += f" {chunk_note}"
    return _clean(
        check, detail,
        caveat + "⚠️ 容器自洽**只说明写入过程没有被粗暴打断**，"
                 "用专业软件重新封装一份剪辑过的音频，同样可以做到完全自洽。",
    )


# ---------------------------------------------------------------------------
# 2. codec_params —— 编码参数自洽性 / 跳变
# ---------------------------------------------------------------------------

def check_codec_params(facts: ContainerFacts) -> Finding:
    """编码参数之间是否自洽；同一文件内部是否出现参数跳变。"""
    check = "codec_params"
    caveat = (
        "参数不自洽说明**头部与内容对不上**（被改过、或手工拼过），"
        "但不说明录音内容本身被动过；而参数跳变也可能来自"
        "拼接多个来源、或某些编码器的异常行为。"
    )
    issues: list[str] = []
    #: 本次**真的做过**的对照项。用来决定下面那句「内部自洽」有没有资格说出口 ——
    #: 见 `if not compared` 那一段的注释。（每个 `compared.append` 都紧挨着
    #: 它对应的那次比较，不许只在文案里提一句就算数。）
    compared: list[str] = []

    sr, ch, bits = facts.sample_rate, facts.channels, facts.bits_per_sample
    if facts.media_type == "audio/wav" and sr and ch and bits:
        expect_align = ch * bits // 8
        expect_rate = sr * expect_align
        if facts.block_align:
            compared.append("块对齐")
            if facts.block_align != expect_align:
                issues.append(
                    f"`fmt` 声明的块对齐 {facts.block_align} 字节 ≠ 声道数×位深/8 = "
                    f"{expect_align} 字节"
                )
        if facts.byte_rate:
            compared.append("字节率")
            if facts.byte_rate != expect_rate:
                issues.append(
                    f"`fmt` 声明的字节率 {facts.byte_rate} ≠ 采样率×块对齐 = {expect_rate}"
                )
        if facts.data_bytes and facts.block_align:
            compared.append("数据长度")
            if facts.data_bytes % facts.block_align:
                issues.append(
                    f"data 块长度 {facts.data_bytes} 不是块对齐 {facts.block_align} 的整数倍"
                    "（末尾有半个采样帧）"
                )

    if facts.media_type == "audio/mpeg":
        if facts.frame_count == 0:
            return _na(
                check,
                "一个 MPEG 帧都没解析出来，读不到任何编码参数，该项整项没有执行。",
                caveat,
            )
        compared += ["采样率跳变", "声道模式跳变"]
        if len(facts.sample_rates) > 1:
            issues.append(
                f"同一文件里出现了 {len(facts.sample_rates)} 种采样率"
                f"（{'、'.join(str(v) for v in facts.sample_rates)} Hz）—— "
                "单个文件不可能改变采样率，这几乎只能是多段拼接"
            )
        if len(facts.channel_modes) > 1:
            issues.append(
                f"声道模式发生变化（{'、'.join(facts.channel_modes)}）—— "
                "同一段录音不会中途从单声道变成立体声"
            )

    if not (facts.sample_rate or facts.frame_count):
        return _na(
            check,
            "这种容器本次没有解析出编码参数（不认识它的内部结构），该项整项没有执行。",
            caveat,
        )

    # ⚠️ **一次对照都没做过时，绝不能说「内部自洽」。**（2026-10 实测踩到）
    #
    # 原先这里直接落到 `_clean(...)`，文案写的是「编码参数**内部自洽**
    # （字节率 / 块对齐 / 数据长度相互吻合）」—— 而在下面这两种输入上，
    # 那三个字段**一个都没读到**，也就是那句话点名的三次比较**一次都没发生**：
    #
    #   · FLAC：`_parse_flac` 只读 STREAMINFO（采样率 / 声道 / 位深），
    #     `byte_rate` / `block_align` / `data_bytes` 全是 `None`；
    #   · WAV 而 `fmt` 头里缺字段（例如 `channels=0`）：上面的 wav 分支
    #     因为 `ch` 为空而**整段跳过**，`block_align` / `byte_rate` 也读不到。
    #
    # 后果正是本项目最忌讳的那一种：**把"没测"写成了"没问题"**，
    # 而且写得非常具体（点名了三个字段"相互吻合"），读者无从怀疑。
    # 判"内部自洽"的前提是**真的对照过**；没对照过就只能弃权。
    if not compared:
        return _na(
            check,
            "这种容器的头部**没有给出可互相对照的编码参数**"
            "（字节率 / 块对齐 / 数据长度这几种字段本次一个都没读到），"
            "「参数之间是否自洽」无从检验，该项整项没有执行。",
            caveat,
        )

    if issues:
        br_note = ""
        if facts.media_type == "audio/mpeg" and len(facts.bitrates) > 1:
            br_note = (
                f"（比特率有 {len(facts.bitrates)} 种取值，$"
                f"{min(facts.bitrates) // 1000}–{max(facts.bitrates) // 1000} kbps —— "
                "**这是 VBR，属正常**，不构成线索。）"
            )
        return _alert(
            check,
            f"编码参数发现 {len(issues)} 处问题：" + "；".join(issues) + "。" + br_note,
            caveat,
            strength=min(1.0, 0.55 + 0.2 * len(issues)),
        )

    # ⚠️ 文案里**点名的必须是本次真的对照过的那几项**（`compared` 现场派生）。
    #    写死一句"字节率 / 块对齐 / 数据长度相互吻合"，就会出现"报告说的那件事
    #    其实没做"—— 与上面那条注释是同一个道理的两种表现。
    detail = (
        "编码参数**内部自洽**（本次核对了："
        + "、".join(compared)
        + "），没有参数跳变。"
    )

    if facts.media_type == "audio/mpeg":
        detail = (
            f"解析出 {facts.frame_count} 个 MPEG 帧，采样率唯一"
            f"（{facts.sample_rates[0] if facts.sample_rates else '?'} Hz）、"
            f"声道模式唯一（{facts.channel_modes[0] if facts.channel_modes else '?'}）。"
            "编码参数自洽，没有跳变。"
        )
    return _clean(
        check, detail,
        caveat + "⚠️ **整段重新编码**（把剪辑结果整体导出为 MP3/WAV）"
                 "会让参数完全自洽 —— 本项对此无能为力。",
    )


# ---------------------------------------------------------------------------
# 3. metadata —— 容器内可见的文字标签
# ---------------------------------------------------------------------------

def check_metadata(facts: ContainerFacts) -> Finding:
    """容器内可见的文字标签（软件署名、注释等）。"""
    check = "metadata"
    caveat = (
        "**没有署名不等于没有编辑过**：绝大多数编辑软件默认不写入署名，"
        "而转码工具（ffmpeg）会在几乎每个文件里留下自己的名字 —— "
        "命中它**只能说明文件被转码过**，这是极常见的操作。"
    )
    hits: list[str] = []
    for key, value in facts.tags:
        label = INFO_TAG_LABELS.get(key, key)
        lowered = value.lower()
        for hint in EDITOR_SOFTWARE_HINTS:
            if hint in lowered:
                hits.append(f"{label}「{value}」命中已知软件 `{hint}`")
                break
    if facts.tags and not hits:
        shown = "；".join(
            f"{INFO_TAG_LABELS.get(k, k)}={v}" for k, v in facts.tags[:6]
        )
        return _clean(
            check,
            f"容器里有 {len(facts.tags)} 条文字标签（{shown}），"
            "其中**没有**命中已知的编辑 / 转码软件署名。",
            caveat,
        )
    if hits:
        return _alert(
            check,
            "容器元数据里带有软件署名：" + "；".join(hits) + "。"
            "—— 只说明该文件**经过软件处理**，不说明内容被剪过。",
            caveat,
            strength=0.35,
        )
    return _clean(
        check,
        "容器里没有可读的文字标签（"
        # ⚠️ 2026-10-05 之前这一句**不分情形**地写着「这种容器本次也没有解析内部标签」
        #    —— 对 WAV 那是假话（它的内部标签**查过了**，确实一个可读的都没有）。
        #    两种情形的**补救动作不同**，所以必须分开说；判据成对钉住
        #    （`verify_audio` 第 3c 节）：同一字段、两种输入、两种不同的话。
        #    ⚠️ 有解析器那一支的措辞**不许**说成"查过了"：FLAC 这类"解析器存在、
        #    但这份字节没走到标签层"的情形下那是另一句假话。
        + ("本能力为这种容器写的解析器**没有读出**可读标签"
           if facts.structure_parsed else "这种容器本次也没有解析内部标签")
        + "，所以**本项覆盖的是「有没有可见署名」，不是「有没有编辑过」**）。",
        caveat,
    )


# ---------------------------------------------------------------------------
# 4. spectral_cutoff —— 高频硬截止（重编码 / 重采样痕迹）
# ---------------------------------------------------------------------------

#: 有效带宽低于这个比例时，样本本身是窄带信号（纯音、单音符），
#: 「频谱截止」这件事对它没有意义 ⇒ 弃权。
MIN_BANDWIDTH_FRACTION = 0.05
#: 硬截止必须出现在 1 kHz 以上才可能是编码造成的。
#: 低于 1 kHz 的"截止"只说明样本是窄带信号（例如一段 440 Hz 单音）
#: —— 那不是线索，是样本本身的性质。
MIN_CUTOFF_HZ = 1000.0
#: 判"有效带宽上限"用的动态范围：从**频谱峰值**往下这么多 dB。
#:
#: ⚠️ 参考电平必须取峰值，不能取"语音带（300–3000 Hz）中位数"：
#: 对纯音 / 窄带样本，那个中位数低得离谱 ⇒ 阈值被拉到噪声底以下 ⇒
#: 连本底都算作"有能量"，带宽被算成 100%，一段 440 Hz 单音反而报"没有截止"。
#: 这是实测踩到的（见 `tools/verify_audio.py` 的纯音用例）。
CUTOFF_DYNAMIC_RANGE_DB = 45.0
#: 「硬截止」的陡度判据（dB/kHz）。
#:
#: ⚠️ 只看"能量降到哪"是不够的：**语音与音乐的高频本来就会自然滚降**
#: （8 kHz 以上掉 40 dB 很常见），只看绝对频率会把每一段正常语音都报成
#: "曾经有损编码"。有损编码的特征是**一道陡壁**（低通滤波器造成），
#: 所以判据必须是**下降速率**。
CUTOFF_EDGE_MIN_DB_PER_KHZ = 40.0


def check_spectral_cutoff(wave: Waveform | None) -> Finding:
    """频谱高端的**硬截止**：有损编码或重采样的直接痕迹。"""
    check = "spectral_cutoff"
    caveat = (
        "截止频率**只能说明该音频在某个环节被有损编码或重采样过**，"
        "不能说明内容被剪过。录音设备、平台上传、语音消息转码都会造成同样的截止。"
        "它是一条「来源经过处理」的线索，不是「被篡改」的线索。"
    )
    gate = _wave_gates(wave, check, caveat)
    if gate is not None:
        return gate
    assert wave is not None
    x = wave.mono()
    sr = wave.sample_rate

    freqs, psd = welch_psd(x, sr, nfft=2048)
    nyq = sr / 2.0
    db = np.asarray(_db(psd), dtype=np.float64)
    # 参考电平取**频谱峰值**（理由见 CUTOFF_DYNAMIC_RANGE_DB 的注释）。
    threshold = float(np.max(db)) - CUTOFF_DYNAMIC_RANGE_DB
    above = db >= threshold
    if not above.any():
        return _na(check, "整段频谱都低于可判读的电平，该项整项没有执行。", caveat)

    cutoff = float(freqs[above].max())
    bandwidth_fraction = float(np.count_nonzero(above)) / float(above.size)
    if bandwidth_fraction < MIN_BANDWIDTH_FRACTION or cutoff < MIN_CUTOFF_HZ:
        return _na(
            check,
            f"样本是**窄带信号**（有效带宽只有 {bandwidth_fraction * 100:.1f}% 、"
            f"上限 {cutoff:.0f} Hz）—— 对纯音 / 单音符谈「频谱截止」没有意义，"
            "该项整项没有执行。",
            caveat,
        )

    ratio = cutoff / nyq
    drop_rate = steepest_drop_db_per_khz(freqs, db, cutoff)
    measured = (
        f"有效带宽上限约 **{cutoff:.0f} Hz**（奈奎斯特频率 {nyq:.0f} Hz 的 "
        f"{ratio * 100:.0f}%），该处最陡的下降约 **{drop_rate:.0f} dB/kHz**。"
    )
    if ratio >= 0.90:
        return _clean(
            check,
            measured + "高频一直延伸到接近奈奎斯特频率，**没有发现硬截止**。",
            caveat,
        )
    if drop_rate < CUTOFF_EDGE_MIN_DB_PER_KHZ:
        return _clean(
            check,
            measured
            + f"下降是**平缓的**（低于 {CUTOFF_EDGE_MIN_DB_PER_KHZ:.0f} dB/kHz 的判据），"
              "符合录音设备自身的频率响应，**不像有损编码留下的硬截止**。",
            caveat,
        )
    return _alert(
        check,
        measured
        + f"这是一道**硬截止**（判据 ≥ {CUTOFF_EDGE_MIN_DB_PER_KHZ:.0f} dB/kHz），"
          "符合「曾经过有损编码 / 降采样之后又被转存为无损容器」的特征。",
        caveat,
        strength=min(1.0, drop_rate / 120.0),
    )


# ---------------------------------------------------------------------------
# 5. noise_floor —— 背景噪声一致性
# ---------------------------------------------------------------------------

#: 「相对安静区」判据：某窗的电平比它**周围 ±1 秒**的**高百分位电平**低多少 dB，
#: 才算"这一窗里只有底噪"。
#:
#: ⚠️ 刻意用**相对**判据，而不是"低于 -45 dBFS"这类绝对阈值。
#: 绝对阈值会把"两段底噪分别是 -54 与 -26 dBFS"这种真实情形里的后一段
#: 整个排除掉（-26 在绝对值上不算"安静"），于是**永远凑不出两段可对照的安静区**
#: —— 而那恰恰是本项要抓的情形。录音可以在很吵的环境里进行，
#: 「安静」是**相对于同一份录音里的内容**而言的。
QUIET_LOCAL_DROP_DB = 10.0
#: 算"周围参考电平"时的半窗（以分析窗为单位，5 ⇒ 左右各约 1 秒）
NOISE_LOCAL_HALF = 5
#: 参考电平取邻域的多高百分位。
#:
#: ⚠️ **必须取高百分位，不能取中位数**（2026-10 实测踩到）：
#: 安静期在邻域里往往占了**一半以上**的窗（一段 2.4 秒的录音里有 1.2 秒停顿，
#: 邻域 ±1 秒共 11 个窗里安静窗就有 6 个）⇒ 中位数**落在安静段自己身上**
#: ⇒ 安静窗与参考值的差恰好为 0 ⇒ 一段"两处停顿底噪差 20 dB"的样本
#: 报出"0 段安静区"。取高百分位则让参考电平落在**内容**上，
#: 这正是"安静"这个词的对照物。
NOISE_ENVELOPE_PCT = 90.0
#: 两组安静段的底噪水平差多少 dB 才算线索。
NOISE_LEVEL_SPREAD_DB = 6.0
#: 每组至少要几个窗（0.4 s/窗 ⇒ 3 窗 = 1.2 s）
NOISE_MIN_GROUP_WINDOWS = 3


def check_noise_floor(wave: Waveform | None) -> Finding:
    """背景噪声一致性：安静段的底噪水平是否一致。

    ⚠️ 判据刻意做成**「比较两段安静区」**而不是"找电平台阶"：
    语音的开始 / 结束本来就是电平台阶（那是内容，不是剪辑），
    用台阶判据会把每一段正常语音都报成异常。

    ⚠️ 数字静音窗（采样值精确为 0）**必须排除在对照之外**：它是
    `check_silence` 的领域，电平低到 -200 dBFS，混进来会让"某段安静区的
    底噪差了 180 dB"这种荒谬的差值成立，把一条本该干净的样本报成异常。
    两项各管各的，判据才不互相污染。
    """
    check = "noise_floor"
    caveat = (
        "底噪水平不同**只说明这几段可能录自不同环境 / 不同设备 / 不同增益**，"
        "而录像是可以在**同一段连续录音里**走动的（从室内到室外、"
        "或开了空调）。它是一条需要与其它线索合看的方向性提示，"
        "**不是剪辑的认定**。"
    )
    gate = _wave_gates(wave, check, caveat)
    if gate is not None:
        return gate
    assert wave is not None
    x = wave.mono()
    sr = wave.sample_rate

    win = max(1, int(sr * 0.4))
    hop = max(1, win // 2)
    count = 1 + (x.size - win) // hop
    if count < NOISE_MIN_GROUP_WINDOWS * 2:
        return _na(
            check,
            f"音轨只能切出 {count} 个分析窗，不足以比较安静段，该项整项没有执行。",
            caveat,
        )
    starts = np.arange(count, dtype=np.int64) * hop
    powers = np.empty(count, dtype=np.float64)
    digital = np.zeros(count, dtype=bool)
    for i, s in enumerate(starts):
        seg = x[s:s + win]
        powers[i] = float(np.mean(seg ** 2))
        # 整窗峰值都在数字静音阈值以下 ⇒ 归 `check_silence` 管，本项不参与对照。
        digital[i] = bool(np.max(np.abs(seg)) <= DIGITAL_SILENCE_EPS)
    levels = 10.0 * np.log10(powers + 1e-20)

    # 「相对安静」= 明显低于自己周围的**内容电平**。这样不论录音整体多吵，
    # 只要**同一份录音里**存在电平不同的安静期，就能被找出来对照。
    local = np.empty(count, dtype=np.float64)
    for i in range(count):
        lo = max(0, i - NOISE_LOCAL_HALF)
        hi = min(count, i + NOISE_LOCAL_HALF + 1)
        local[i] = np.percentile(levels[lo:hi], NOISE_ENVELOPE_PCT)
    quiet = (levels < (local - QUIET_LOCAL_DROP_DB)) & ~digital
    groups = [g for g in runs_of(quiet) if (g[1] - g[0]) >= NOISE_MIN_GROUP_WINDOWS]
    if digital.any() and not groups:
        # 整段只有数字静音「安静」，没有真正的底噪安静区 —— 如实说明，
        # 免得读报告的人以为"没找到安静区"等于"没有安静期"。
        return _na(
            check,
            "全段唯一的低电平区是**数字静音段**（采样值精确为 0），"
            "它归「数字静音段」那一项管；本项需要的是**有底噪的**安静期"
            "（例如说话之间的停顿），本次一段都没找到，该项整项没有执行。",
            caveat,
        )
    if len(groups) < 2:
        # ⚠️ **这里是弃权，不是「未提示异常」**：没有第二段安静区，
        #    底噪一致性**根本没有被检验过**。报"未提示异常"会把"没测"
        #    说成"没问题" —— 本项目最忌讳的失败形态。
        return _na(
            check,
            f"全段切出 {count} 个窗，只找到 **{len(groups)} 段**「相对安静区」——"
            "没有第二段可以对照，底噪一致性无从检验，该项整项没有执行。",
            caveat,
        )

    group_levels = [float(np.median(levels[a:b])) for a, b in groups]
    spread = max(group_levels) - min(group_levels)
    times = [f"{starts[a] / sr:.1f}s–{starts[b - 1] / sr:.1f}s" for a, b in groups]
    measured = (
        f"找到 {len(groups)} 段安静区（{'、'.join(times)}），"
        f"它们的底噪水平分别是 "
        f"{'、'.join(f'{v:.1f}' for v in group_levels)} dBFS，"
        f"**相差 {spread:.1f} dB**。"
    )
    if spread >= NOISE_LEVEL_SPREAD_DB:
        return _alert(
            check,
            measured + f"超过 {NOISE_LEVEL_SPREAD_DB:.0f} dB 的判据 —— "
                       "各段可能录自不同环境 / 设备 / 增益设置。",
            caveat,
            strength=min(1.0, spread / 20.0),
        )
    return _clean(
        check,
        measured + f"差异在 {NOISE_LEVEL_SPREAD_DB:.0f} dB 以内，**底噪水平基本一致**。",
        caveat,
    )


# ---------------------------------------------------------------------------
# 6. silence —— 数字静音段
# ---------------------------------------------------------------------------

#: 数字静音段的最短时长（秒）。太短的零样本可能是编码器边界的填充。
MIN_DIGITAL_SILENCE_SECONDS = 0.05
#: 首尾各排除多少秒：很多设备在开头 / 结尾本来就会写一段数字零。
SILENCE_EDGE_GUARD_SECONDS = 0.30


def check_silence(wave: Waveform | None) -> Finding:
    """**数字静音**段：样本恰好为 0 的连续区间。

    这是本模块里**最硬**的一条波形层线索：真实录音的"安静"里总有底噪
    （哪怕只有 -70 dBFS），采样值精确为零意味着**这段是软件写进去的**。
    """
    check = "silence"
    caveat = (
        "数字静音段说明**这一段是软件产生的**，不说明整段录音被剪过："
        "降噪插件、通话静音抑制、录音 App 的「静音填充」都会写出精确的零。"
        "但若它出现在**音轨中间**、且两侧是正常底噪，则与「把两段录音接起来」"
        "高度一致。"
    )
    gate = _wave_gates(wave, check, caveat)
    if gate is not None:
        return gate
    assert wave is not None
    x = wave.mono()
    sr = wave.sample_rate

    guard = int(sr * SILENCE_EDGE_GUARD_SECONDS)
    min_len = int(sr * MIN_DIGITAL_SILENCE_SECONDS)
    mask = np.abs(x) <= DIGITAL_SILENCE_EPS
    interior = np.zeros_like(mask)
    # ⚠️ **"内部区间不够长"必须弃权，不能说"没有发现"**（2026-10 补）。
    #    首尾各 0.3 秒的保护带是必要的（很多设备本来就会在首尾写一段零），
    #    但对 0.6–0.65 秒的音轨来说，保护带一扣就只剩不到 50 毫秒的内部区间
    #    —— 连一次判据都放不下。此时"没有发现数字静音段"是**一次没查**，
    #    而下面那句"这符合连续录音的形态"会被读成"查过了、没问题"。
    #    同一族的另外三项（底噪 / 谱突变 / 样本不连续）在窗口切不出来时
    #    都走弃权，这一项没有理由例外。
    if x.size - 2 * guard < min_len:
        return _na(
            check,
            f"音轨只有 {x.size / sr:.2f} 秒，扣掉首尾各 "
            f"{SILENCE_EDGE_GUARD_SECONDS:.1f} 秒的保护带之后，"
            "可检查的内部区间短于判据本身，该项整项没有执行。",
            caveat,
        )
    # 走到这里 ⇒ 内部区间至少放得下一次判据（上面那条已把更短的挡掉），
    # 所以这里**不再需要** `if x.size > 2 * guard` —— 留一个恒真的分支只会
    # 让下一个人以为还有"内部为空"的情形没处理。
    interior[guard:x.size - guard] = True
    hits = [
        (a, b) for a, b in runs_of(mask & interior) if (b - a) >= min_len
    ]
    if not hits:
        return _clean(
            check,
            f"音轨内部**没有发现数字静音段**（判据：连续 "
            f"{MIN_DIGITAL_SILENCE_SECONDS * 1000:.0f} 毫秒以上、"
            f"采样值精确为 0、且不在首尾 {SILENCE_EDGE_GUARD_SECONDS:.1f} 秒内）。"
            "—— 这符合连续录音的形态。",
            caveat,
        )
    spans = "、".join(f"{a / sr:.2f}s–{b / sr:.2f}s（{(b - a) / sr:.2f} 秒）"
                     for a, b in hits[:6])
    return _alert(
        check,
        f"音轨内部发现 **{len(hits)} 段数字静音**：{spans}"
        + ("…" if len(hits) > 6 else "")
        + "。样本精确为 0 意味着这些段落是软件写入的，"
          "自然录音的安静段会保留设备底噪。",
        caveat,
        strength=min(1.0, 0.5 + 0.15 * len(hits)),
    )


# ---------------------------------------------------------------------------
# 7. spectral_flux —— 谱形状不连续
# ---------------------------------------------------------------------------

SPECTRAL_FLUX_FFT = 1024
SPECTRAL_FLUX_HOP = 256
#: 离群判据的 k（越大越保守）。这里取 14 是因为瞬态干扰（爆破音、鼓点）
#: 在谱形状上本来就会造成大跳变 —— 宁可漏报，也不要在这类音频上误报成堆。
#: 实测把 k 取 10 时，一段"谐波 + 底噪"的合成语音会报出 66 处尖峰
#: （低电平段由底噪主导、高电平段由谐波主导，两者谱形状天然不同）。
SPECTRAL_FLUX_K = 14.0
#: 候选尖峰的**绝对下限**：余弦距离小于它的变化不值得看。
#: 只靠 MAD 的相对判据在"各帧都很像"的样本上会把极小差异也算成离群。
SPECTRAL_FLUX_MIN_DISTANCE = 0.25
#: 至少要有几处孤立尖峰才算"提示异常"。
SPECTRAL_FLUX_MIN_HITS = 3
#: 相邻多近的尖峰算**同一次**突变（帧数）。一个突变事件会让相邻几帧的距离
#: 一起偏高，不合并的话"1 处突变"会被数成几十处，报出来的数字完全不反映现场。
SPECTRAL_FLUX_MERGE_FRAMES = 5


def check_spectral_flux(wave: Waveform | None) -> Finding:
    """帧间**谱形状**的突变点（归一化后算余弦距离，只看形状不看音量）。"""
    check = "spectral_flux"
    caveat = (
        "爆破音（p/t/k）、鼓点、关门、碰麦等瞬态都会造成谱形状突变，"
        "**本项在语音与音乐上天然会有输出**。它只提示「这里有一个突变点、"
        "值得人工听一遍」，绝不等于「这里被剪过」。"
    )
    gate = _wave_gates(wave, check, caveat)
    if gate is not None:
        return gate
    assert wave is not None
    x = wave.mono()
    sr = wave.sample_rate

    nfft, hop = SPECTRAL_FLUX_FFT, SPECTRAL_FLUX_HOP
    if x.size < nfft * 4:
        return _na(check, f"音轨短于 {nfft * 4 / sr:.2f} 秒，分不出足够的分析帧，"
                          "该项整项没有执行。", caveat)
    count = 1 + (x.size - nfft) // hop
    starts = np.arange(count, dtype=np.int64) * hop
    win = np.hanning(nfft)
    idx = starts[:, None] + np.arange(nfft)[None, :]
    spec = np.abs(np.fft.rfft(x[idx] * win[None, :], axis=1))
    norm = np.linalg.norm(spec, axis=1, keepdims=True)
    norm[norm <= 0] = 1.0
    unit = spec / norm
    cos = np.sum(unit[1:] * unit[:-1], axis=1)
    distance = 1.0 - np.clip(cos, -1.0, 1.0)

    bounds = robust_threshold(distance, k=SPECTRAL_FLUX_K)
    if bounds is None:
        return _na(check, "各帧的谱形状完全一致（离散度为零），"
                         "无法定义「突变」，该项整项没有执行。", caveat)
    _med, upper = bounds
    # 下钳到绝对下限（免得"各帧都很像"时把微小差异也算离群），
    # 上钳到 1.0（余弦距离的天然上界）—— 不钳上界会出现
    # "判据上界 1.363" 这种**永远不可能触发**的阈值，判据当场自废。
    upper = min(max(upper, SPECTRAL_FLUX_MIN_DISTANCE), 1.0)
    peaks = [
        i for i in range(1, distance.size - 1)
        if distance[i] > upper and distance[i] >= distance[i - 1] and distance[i] >= distance[i + 1]
    ]
    # 合并相邻尖峰：**一次**突变会让附近连续几帧的距离一起抬高，
    # 不合并就会把"1 处"数成几十处（实测数字静音样本报出 66 处，
    # 而现场其实只有静音段的两个端点是真正的突变）。
    events: list[int] = []
    for idx in peaks:
        if events and (idx - events[-1]) <= SPECTRAL_FLUX_MERGE_FRAMES:
            continue
        events.append(int(idx))
    when = "、".join(f"{(starts[i] + nfft) / sr:.2f}s" for i in events[:6])
    measured = (
        f"共 {distance.size} 个帧间比较，突变判据上界 {upper:.3f}，"
        f"发现 **{len(events)} 处**突变事件"
        + (f"（{when}{'…' if len(events) > 6 else ''}）" if events else "")
        + f"，最大突变 {float(distance.max()):.3f}。"
    )
    if len(events) >= SPECTRAL_FLUX_MIN_HITS:
        return _alert(
            check,
            measured + f"达到「至少 {SPECTRAL_FLUX_MIN_HITS} 处」的判据。",
            caveat,
            strength=min(1.0, len(events) / 8.0),
        )
    return _clean(check, measured + "未达到异常判据。", caveat)


# ---------------------------------------------------------------------------
# 8. sample_discontinuity —— 样本级不连续（咔哒）
# ---------------------------------------------------------------------------

#: 包络窗长（毫秒）：只看 1 毫秒内的最大一阶差分，用来抓孤立瞬态。
SAMPLE_ENV_MS = 1.0
SAMPLE_DISCONTINUITY_K = 12.0
#: 相邻命中之间至少隔几个窗才算"孤立"（足够远 = 不是一个瞬态的余振）
SAMPLE_ISOLATION_WINDOWS = 3
SAMPLE_MIN_HITS = 3


def check_sample_discontinuity(wave: Waveform | None) -> Finding:
    """波形上的一阶差分孤立尖峰（"咔哒"）。"""
    check = "sample_discontinuity"
    caveat = (
        "麦克风碰触、键盘、关门、衣物摩擦都会产生同样的尖峰。"
        "本项在**任何真实录音**上都可能命中，因此只作为最弱的辅助线索。"
    )
    gate = _wave_gates(wave, check, caveat)
    if gate is not None:
        return gate
    assert wave is not None
    x = wave.mono()
    sr = wave.sample_rate

    diff = np.abs(np.diff(x))
    win = max(2, int(sr * SAMPLE_ENV_MS / 1000.0))
    count = diff.size // win
    if count < SAMPLE_MIN_HITS * 8:
        return _na(check, "音轨太短，切不出足够的分析窗，该项整项没有执行。", caveat)
    env = diff[: count * win].reshape(count, win).max(axis=1)

    bounds = robust_threshold(env, k=SAMPLE_DISCONTINUITY_K)
    if bounds is None:
        return _na(check, "波形差分完全恒定（离散度为零），无法定义"
                          "「突变」，该项整项没有执行。", caveat)
    _med, upper = bounds
    raw_hits = np.flatnonzero(env > upper)
    isolated: list[int] = []
    for hit in raw_hits:
        if not isolated or (hit - isolated[-1]) >= SAMPLE_ISOLATION_WINDOWS:
            isolated.append(int(hit))
        elif env[hit] > env[isolated[-1]]:
            isolated[-1] = int(hit)
    measured = (
        f"共 {count} 个 {SAMPLE_ENV_MS:.0f} ms 窗，尖峰判据上界 {upper:.4f}，"
        f"其中**相互孤立**的有 {len(isolated)} 处"
        + (f"（最早在 {isolated[0] * win / sr:.2f}s）" if isolated else "")
        + f"，全段最大一阶差分 {float(env.max()):.4f}。"
    )
    if len(isolated) >= SAMPLE_MIN_HITS:
        return _alert(
            check,
            measured + f"达到「至少 {SAMPLE_MIN_HITS} 处」的判据，"
                       "值得听一遍这些位置是否有不自然的爆音。",
            caveat,
            strength=min(1.0, len(isolated) / 20.0),
        )
    return _clean(check, measured + "未达到异常判据。", caveat)


# ---------------------------------------------------------------------------
# 9. enf —— 电网频率痕迹
# ---------------------------------------------------------------------------

#: 候选基频：中国大陆 / 欧洲 50 Hz，北美 / 日本东部 60 Hz。
ENF_BASE_FREQS = (50.0, 60.0)
#: 谐波梳取前几阶
ENF_HARMONICS = (1, 2, 3, 4)
#: 梳齿的搜索半宽（Hz）。⚠️ **不能用百分比**（原先写的是 ±1%）：
#: 8192 点 FFT 在 44.1 kHz 下的栅格就是 5.4 Hz，±1% 的 50 Hz 只有 ±0.5 Hz，
#: 带里一个频点都没有 ⇒ 梳齿扫描结果恒为空、本项永远"检不出"。
ENF_COMB_HALF_WIDTH_HZ = 2.5
#: ENF 分析专用的 FFT 长度（分辨率 1.35 Hz @ 44.1 kHz）
ENF_FFT = 32768
#: 判"梳齿是否凸起"时，邻域取多宽（Hz）。要显著宽于梳齿搜索半宽，
#: 否则邻域里全是峰自己的裙边，对比度会被算小。
ENF_COMB_NEIGHBOUR_HZ = 12.0
#: 频率分辨率差于它就直接弃权 —— 分辨率不够时做出来的"稳定性"是假象。
ENF_MAX_BIN_HZ = 4.0
#: 判定"有市电痕迹"的梳齿信噪比门槛（dB）
ENF_SNR_DETECTED = 12.0
ENF_SNR_WEAK = 6.0
#: 梳齿电平相对**全谱峰值**的最低要求（dB）。低于它的一律不算痕迹 ——
#: 真实市电哼声即使很弱也在峰值以下 30–60 dB，比这还低两个数量级的东西
#: 是数值噪声 / 合成伪影。理由见 `_enf_comb_snr()` 里的实测反例。
ENF_MIN_RELATIVE_DB = -75.0
#: 梳齿过弱时的哨兵分数（比任何真实对比度都低，保证不会被判为检出）
ENF_FAINT_SNR = -99.0
#: 稳定性判据：逐窗估计出的基频，标准差必须小于它（Hz）才算"稳恒"。
#:
#: 这道闸门是为了把**人类语音的基频**排除在外：语音基频落在同一频段
#: （男声 80–180 Hz、女声 160–300 Hz）却随语调快速起伏（秒级变化几 Hz），
#: 不设闸门的话，一段男声讲话就会被当成"检出了市电哼声"。
#:
#: ⚠️ 取值 0.5 而不是 0.15，是**被分辨率约束的诚实结果**：真实电网频率
#: 分钟尺度上只漂 ±0.05 Hz，但用抛物线插值从 1.35 Hz 的栅格上估峰，
#: 精度也就 0.2–0.3 Hz ⇒ 判据不能比测量精度还小，那只会变成随机通过 / 随机失败。
ENF_STABILITY_STD_HZ = 0.5
#: 相位连续性判据：相邻窗之间解缠后的相位残差跳变超过它（弧度）即记一处
ENF_PHASE_JUMP_RAD = 0.6
#: 至少几处相位跳变才算线索
ENF_MIN_PHASE_JUMPS = 2


def _enf_comb_snr(freqs: np.ndarray, psd: np.ndarray, base: float) -> float | None:
    """基频 `base` 的谐波梳**局部对比度**（dB）。返回 `None` 表示无法评估。

    ⚠️ 判据是"每个梳齿**从它自己的邻域里凸出来**"，不是"低频段整体有能量"。

    实测踩到的反例：一段 **440 Hz 纯音**，用"500 Hz 以下的中位数"当参考时，
    在 50 Hz 处算出了 **15.5 dB** 的"梳齿信噪比" —— 因为整段低频都是 440 Hz
    的主瓣 / 旁瓣泄漏，而泄漏电平沿频率单调下降，拿远处的电平做中位数，
    **任何单调下降的曲线都会被算成有峰**。改用局部对比度后，
    纯音的 50 Hz 处不再凸起，该项正确地报"检不出"。
    """
    contrasts: list[float] = []
    peaks: list[float] = []
    half = ENF_COMB_HALF_WIDTH_HZ
    for k in ENF_HARMONICS:
        f = base * k
        if f >= freqs[-1] * 0.98:
            break
        band = (freqs >= f - half) & (freqs <= f + half)
        ring = (
            (freqs >= f - ENF_COMB_NEIGHBOUR_HZ)
            & (freqs <= f + ENF_COMB_NEIGHBOUR_HZ)
            & ~((freqs >= f - half - 0.5) & (freqs <= f + half + 0.5))
        )
        if not band.any() or np.count_nonzero(ring) < 3:
            continue
        peak = float(psd[band].max())
        local = float(np.median(psd[ring]))
        if local <= 0:
            continue
        contrasts.append(peak / local)
        peaks.append(peak)
    if len(contrasts) < 3:
        return None

    # ⚠️ **绝对电平闸门**：局部对比度高 **不等于** 有市电痕迹。
    #    实测踩到的反例：一段 440 Hz 正弦量化成 16 位后，量化误差的周期是
    #    2205 个样本（20 Hz），于是它的谐波谱里 **60/120/180/240 Hz 全都在**，
    #    被评为"60 Hz 电网痕迹、对比度 42.7 dB" —— 而那一组谱线的绝对电平
    #    比全谱峰值低了 **101 dB**，是彻头彻尾的数值伪影。
    #    真实市电哼声即使很弱，也在峰值以下 30–60 dB 这个量级。
    peak_all = float(psd.max())
    if peak_all > 0:
        relative = 10.0 * np.log10((float(np.median(peaks)) + 1e-30) / peak_all)
        if relative < ENF_MIN_RELATIVE_DB:
            return ENF_FAINT_SNR

    # 取**中位数**而不是均值：某一阶可能正好被内容（如某个乐器泛音）压掉，
    # 用均值会让那一阶拖垮整体判断。
    return float(10.0 * np.log10(float(np.median(contrasts)) + 1e-20))


def _enf_freq_series(
    x: np.ndarray, sr: int, base: float, *, window_seconds: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """逐窗估计基频，返回 `(窗中心时刻, 基频估计)`。**这是基频估计的唯一实现。**

    `_enf_stability()`（只看标准差）与对外暴露的 `enf_track()`（要整条轨迹）
    共用它 —— 各写一份的话，"同一个算法"这句话就没人能保证了：
    轨迹画出来是稳的、而稳定性判据说它不稳，两边都"跑通了"。
    """
    win = max(1, int(sr * window_seconds))
    hop = max(1, win // 2)
    if x.size < win * 3:
        return np.empty(0), np.empty(0)
    count = min(1 + (x.size - win) // hop, 24)
    half = ENF_COMB_HALF_WIDTH_HZ
    times: list[float] = []
    est: list[float] = []
    for i in range(count):
        seg = x[i * hop: i * hop + win]
        # ⚠️ 这里必须用**解析峰位**（抛物线插值）而不是 `argmax` 的 bin 中心：
        #    直接取 bin 中心时，所有窗都会落在同一个 bin 上 ⇒ 标准差恒为 0 ⇒
        #    "稳定性检查"永远通过 —— 那不是检出了稳恒痕迹，是判据被分辨率抹平了。
        freqs, psd = welch_psd(seg, sr, nfft=ENF_FFT, max_frames=4)
        peak = parabolic_peak_hz(freqs, psd, base - half, base + half)
        if peak is None:
            continue
        times.append((i * hop + win / 2.0) / sr)
        est.append(peak[0])
    return np.asarray(times, dtype=np.float64), np.asarray(est, dtype=np.float64)


def _enf_stability(x: np.ndarray, sr: int, base: float) -> float | None:
    """逐窗估计基频，返回这些估计值的标准差（Hz）。`None` = 样本不足 / 测不出变化。"""
    _times, est = _enf_freq_series(x, sr, base)
    if est.size < 3:
        return None
    spread = float(np.std(est))
    if spread == 0.0:
        # ⚠️ 标准差**恰好为零**不是"完美稳恒"，而是"**测量不出任何变化**"：
        #    只有信号在分析窗上严格周期（纯音、合成音）时才会出现。
        #    这时本项没有分辨能力 ⇒ 宁可不评估，也不要把它当成"稳恒"。
        return None
    return spread


def _enf_phase_jumps(x: np.ndarray, sr: int, base: float) -> tuple[int, float]:
    """跟踪基频处的相位，返回 `(跳变处数, 残差 RMS)`。"""
    win = int(sr * 0.5)
    hop = max(1, win // 4)
    if x.size < win * 4:
        return 0, 0.0
    count = 1 + (x.size - win) // hop
    count = min(count, 48)
    phases = np.array(
        [np.angle(goertzel(x[i * hop: i * hop + win], base, sr)) for i in range(count)],
        dtype=np.float64,
    )
    if phases.size < 4:
        return 0, 0.0
    unwrapped = np.unwrap(phases)
    t = np.arange(phases.size, dtype=np.float64)
    slope, intercept = np.polyfit(t, unwrapped, 1)
    residual = unwrapped - (slope * t + intercept)
    steps = np.abs(np.diff(residual))
    return int(np.count_nonzero(steps > ENF_PHASE_JUMP_RAD)), float(np.sqrt(np.mean(residual ** 2)))


def check_enf(wave: Waveform | None) -> Finding:
    """电网频率（ENF）痕迹及其相位连续性。

    **检出率本来就不高**：手机录音里市电哼声往往弱到测不出。所以"检不出"
    是**弃权**（`无法判定`），不是"没有异常" —— 这一点在本项上尤其重要，
    因为"没有 ENF"最容易被误读成"录得很干净、所以没问题"。

    ⚠️ 这是**逐项**弃权（`_na`），不是材料级闸门：ENF 只是九条线索之一，
    它走不通不代表其余八条没跑。「检不出市电痕迹」这条限制本身
    已经在 `detail` 里写明，读者看得见。
    """
    check = "enf"
    caveat = (
        "ENF 只在**录音里有市电哼声**时才成立（靠近电源的固定设备、"
        "室内录音、有线麦克风较容易有；手机在户外录音通常测不到）。"
        "相位跳变也可能来自采样时钟抖动、变速播放、或电池供电设备的频率漂移。"
        "**检不出 ENF ⇒ 本项什么也没能证明**，不是「通过」。"
    )
    gate = _wave_gates(wave, check, caveat)
    if gate is not None:
        return gate
    assert wave is not None
    x = wave.mono()
    sr = wave.sample_rate
    # 电网频率是 50 Hz 量级，采样率太低（≤ 8 kHz）时奈奎斯特边缘的
    # 抗混叠滤波器会把它削掉，无从分析。
    if sr < 4000:
        return _na(
            check,
            f"采样率只有 {sr} Hz，低频段分辨率不足以分析电网频率，该项整项没有执行。",
            caveat,
        )

    freqs, psd = welch_psd(x, sr, nfft=ENF_FFT, max_frames=32)
    if freqs.size > 1:
        bin_hz = float(freqs[1] - freqs[0])
        if bin_hz > ENF_MAX_BIN_HZ:
            return _na(
                check,
                f"本次的频率分辨率只有 {bin_hz:.2f} Hz/bin"
                f"（要求 ≤ {ENF_MAX_BIN_HZ:.1f}），不足以分辨电网频率的漂移，"
                "该项整项没有执行。",
                caveat,
            )
    scored: list[tuple[float, float, float | None]] = []
    for base in ENF_BASE_FREQS:
        snr = _enf_comb_snr(freqs, psd, base)
        if snr is None:
            continue
        stability = _enf_stability(x, sr, base) if snr >= ENF_SNR_WEAK else None
        scored.append((base, snr, stability))

    if not scored:
        return _na(
            check,
            "本次连一组可评估的谐波梳都没能算出来（带宽或分辨率不足），"
            "该项整项没有执行。",
            caveat,
        )

    detected = [
        (b, s, st) for b, s, st in scored
        if s >= ENF_SNR_DETECTED and st is not None and st <= ENF_STABILITY_STD_HZ
    ]

    def _describe(base: float, snr: float, stability: float | None) -> str:
        if snr <= ENF_FAINT_SNR:
            return (
                f"{base:.0f} Hz 的谐波梳**过弱**"
                f"（比全谱峰值低 {-ENF_MIN_RELATIVE_DB:.0f} dB 以上，判为无数值意义）"
            )
        text = f"{base:.0f} Hz 的谐波梳信噪比 {snr:.1f} dB"
        text += (
            f"、逐窗基频标准差 {stability:.3f} Hz"
            if stability is not None else "、稳定性无法评估"
        )
        return text

    if not detected:
        detail = (
            "**本次没有在音轨里检出足以分析的市电（ENF）痕迹**："
            + "；".join(_describe(b, s, st) for b, s, st in scored)
            + f"（判据：≥ {ENF_SNR_DETECTED:.0f} dB 且标准差 ≤ {ENF_STABILITY_STD_HZ} Hz）。"
              "**因此相位连续性这一项无法检验** —— 这不代表录音干净，"
              "只代表本地这条路走不通。"
        )
        return _na(
            check,
            detail + "（注：人类语音的基频会落在同一频段但快速起伏，"
                     "已用「逐窗基频稳定性」把语音排除在外。）",
            caveat,
        )

    base = detected[0][0]
    snr, stability = detected[0][1], detected[0][2]
    jumps, residual = _enf_phase_jumps(x, sr, base)
    measured = (
        f"检出来自 **{base:.0f} Hz 电网**的稳恒痕迹（谐波梳信噪比 {snr:.1f} dB、"
        f"逐窗基频标准差 {stability:.3f} Hz），相位跟踪残差 RMS {residual:.3f} 弧度，"
        f"发现 **{jumps} 处**相位跳变（判据 > {ENF_PHASE_JUMP_RAD} 弧度）。"
    )
    if jumps >= ENF_MIN_PHASE_JUMPS:
        return _alert(
            check,
            measured + "—— 相位在时间上本应连续，出现多处跳变与「录音中间被剪掉一段"
                       "或改过速度」高度一致。",
            caveat,
            strength=min(1.0, jumps / 6.0),
        )
    return _clean(
        check,
        measured + "—— 相位保持连续，**未提示异常**"
                   "（ENF 这条路走通了，且没有看到剪辑痕迹）。",
        caveat,
    )


# ---------------------------------------------------------------------------
# ENF 轨迹（对外暴露的细化视图）
# ---------------------------------------------------------------------------

#: 轨迹视图**必须**附带的说明。抽成常量是因为它同时出现在
#: `forensics.enf_track()` 与 MCP 工具的描述里 —— 两处措辞不一致时，
#: 工具的使用者会按较宽松的那一处去理解结果。
ENF_TRACK_NOTE = (
    "轨迹点的意义**完全取决于闸门**：只有当 `detected=true` 时，"
    "这些点才是在跟踪真实的电网频率，此时「相邻点跳变 / 整体漂移」才有解释力；"
    "`detected=false` 时它们只是各窗里**噪声峰的位置**，"
    "起伏是随机的 —— 看这条轨迹**看不出任何东西**，"
    "不要用「抖动大」当作剪辑的依据。"
)


def enf_track(
    wave: Waveform | None,
    *,
    base: float | None = None,
    window_seconds: float = 1.0,
) -> dict[str, Any]:
    """把 ENF 基频的**逐窗轨迹**取出来（供人工核查，不参与自动判级）。

    ⚠️ 本函数**不改变任何判定** —— 综合等级仍由 `check_enf()` → `summarize()`
    给出。它存在的理由是：ENF 这条线索的结论很依赖"看"，
    而工具层只给一句话结论时，复核的人无从判断那句话靠不靠得住。

    参数
    ----
    base
        要跟踪的基频（50 / 60）。不传则按 `check_enf()` 的同一套判据
        自动挑一个；两个都检不出时返回 `detected=False` 与只读的原始轨迹。
    window_seconds
        分析窗长。默认 1 秒 —— 与 `_enf_freq_series` 的默认值一致
        （**同一件事不能有两个默认值**）。
    """
    if wave is None:
        return {
            "available": False,
            "detected": False,
            "reason": (
                "这段音频没有解出波形（压缩 / 带封装的格式，本进程没有解码器），"
                "ENF 轨迹无从计算。"
            ),
            "points": [],
            "note": ENF_TRACK_NOTE,
        }
    x = wave.mono()
    sr = wave.sample_rate
    if sr < 4000:
        return {
            "available": False,
            "detected": False,
            "reason": f"采样率只有 {sr} Hz，低频段分辨不出电网频率。",
            "points": [],
            "note": ENF_TRACK_NOTE,
        }
    if x.size < int(sr * MIN_WAVEFORM_SECONDS) * 3:
        return {
            "available": False,
            "detected": False,
            "reason": f"音轨只有 {x.size / sr:.2f} 秒，切不出足够多的分析窗。",
            "points": [],
            "note": ENF_TRACK_NOTE,
        }

    candidates = (base,) if base is not None else ENF_BASE_FREQS
    freqs, psd = welch_psd(x, sr, nfft=ENF_FFT, max_frames=32)
    best: tuple[float, float, float | None] | None = None
    for candidate in candidates:
        snr = _enf_comb_snr(freqs, psd, candidate)
        if snr is None or snr <= ENF_FAINT_SNR:
            continue
        stability = _enf_stability(x, sr, candidate)
        if best is None or snr > best[1]:
            best = (float(candidate), float(snr), stability)

    if best is None:
        return {
            "available": False,
            "detected": False,
            "reason": (
                "本次没有在音轨里检出足以分析的市电（ENF）痕迹 —— "
                "轨迹无从计算（不是「没有异常」，是这条路走不通）。"
            ),
            "points": [],
            "note": ENF_TRACK_NOTE,
        }

    picked, snr, stability = best
    times, series = _enf_freq_series(x, sr, picked, window_seconds=window_seconds)
    detected = (
        snr >= ENF_SNR_DETECTED
        and stability is not None
        and stability <= ENF_STABILITY_STD_HZ
    )
    return {
        "available": True,
        "detected": bool(detected),
        "base_hz": picked,
        "comb_snr_db": round(snr, 2),
        "std_hz": None if stability is None else round(stability, 4),
        "sample_rate": sr,
        "window_seconds": window_seconds,
        "points": [
            {"t_seconds": round(float(t), 4), "freq_hz": round(float(f), 4)}
            for t, f in zip(times, series)
        ],
        "reason": (
            f"检出了 **{picked:.0f} Hz** 电网痕迹（谐波梳信噪比 {snr:.1f} dB，"
            f"判据 ≥ {ENF_SNR_DETECTED:.0f} dB）。"
            if detected else
            f"**未达检出判据**：{picked:.0f} Hz 的谐波梳信噪比 {snr:.1f} dB"
            f"（判据 ≥ {ENF_SNR_DETECTED:.0f} dB）、"
            f"逐窗基频标准差 "
            + ("无法评估" if stability is None else f"{stability:.3f} Hz")
            + f"（判据 ≤ {ENF_STABILITY_STD_HZ} Hz）。轨迹点**不是**真实电网频率。"
        ),
        "note": ENF_TRACK_NOTE,
    }


# ---------------------------------------------------------------------------
# 汇总与升级建议
# ---------------------------------------------------------------------------

def _gate_rationale(gated: list[str]) -> str:
    """闸门触发时的理由（**只有材料级前提**才会走到这里）。

    ⚠️ 列举的原因必须与 `_wave_gates()` 里那三个分支**逐条对应**。
    早先这里还写着"或市电痕迹检不出" —— 而市电痕迹检不出已经改成
    逐项弃权（`_na`）了。措辞与判据不对应，读者就会照着这段去理解
    报告为什么是「无法判定」，从而得出错误的因果。
    """
    return (
        f"音轨不满足波形域检查的前提：{len(gated)} 项整项没有执行"
        f"（{'、'.join(gated)}）。而这些恰恰是能发现剪辑 / 拼接 / 重编码的项目，"
        "**所以本报告既不能说「正常」也不能说「异常」**。"
        "请按各条 `detail` 里写明的原因处理（三种：没有解出波形 —— 压缩 / "
        "带封装的格式需要先用其它工具转成 WAV；样本短于 "
        f"{MIN_WAVEFORM_SECONDS} 秒；或整段近乎无声）。"
        "**注意这与「某一项不适用」是两回事**：那种情况下其余各项已经跑通，"
        "综合等级照常给出，只在 `findings` 里留下一条「无法判定」。"
    )


def summarize(findings: Sequence[Finding]) -> tuple[str, str]:
    """把逐项发现折成综合线索等级。

    ⚠️ **判据本身不在本函数里**，在 `legal_acps.verdict.summarize_findings()`
    —— 图像链与音频链共用同一段逻辑，只各自提供数据。
    两条链各写一份的话，就会出现"同一种失败在图像链是「无法判定」、
    在音频链是「低」"，而报告表面看不出任何异样。

    ⚠️ `gate_prefixes` **只给材料级前缀**（`GATE_ABSTAIN_PREFIX`）。
    把逐项前缀（`NOT_APPLICABLE_PREFIX`）也塞进来，等于让"某一项不适用"
    拥有把整份报告压成「无法判定」的权力 —— 而 ENF 在真实录音里本来就
    常常检不出，于是**每一段正常录音都会得到「无法判定」**。

    ⚠️ 但反过来的隐患要在这里补上：等级既然照常给出，**就必须让读者知道
    有几项其实没参与判级**。否则一段"ENF 和底噪一致性都没跑"的音轨会拿到
    一句干净的"所有可执行的检查项均未提示异常"，而读者无从知道
    「可执行」只覆盖了七项。所以本函数在共享判据之上**补一句交代**。
    """
    items = list(findings)
    level, why = summarize_findings(
        items,
        weights=CHECK_WEIGHTS,
        gate_prefixes=(GATE_ABSTAIN_PREFIX,),
        gate_rationale=_gate_rationale,
        subject="该音轨",
        finding_noun="篡改认定",
        clean_phrase="未经剪辑",
    )
    triggered = [f.check for f in items if f.verdict == "提示异常"]
    gates = [
        f.check for f in items
        if f.verdict == ABSTAIN and str(f.detail).startswith(GATE_ABSTAIN_PREFIX)
    ]
    na = [
        f.check for f in items
        if f.verdict == ABSTAIN and str(f.detail).startswith(NOT_APPLICABLE_PREFIX)
    ]
    # ⚠️ 「材料级闸门 + 同时有正向触发」这一组合最容易漏交代：
    #    此时共享判据走的是分支①（有触发就先算账），`_gate_rationale` **根本没被调用**
    #    ⇒ 报告会给一个等级，却只字不提"另有六项整项没跑"。
    #    实测形态：一个位深写错的 WAV —— `codec_params` 触发（1.0 ⇒ 中）、
    #    波形域六项全灭，而结论里看不出后者。等级不是错的，**读者对它的信任程度会错**。
    #    分支②本来就在 `_gate_rationale` 里点全了，所以这里跳过它、只为其余分支补一句。
    if gates and not (level == ABSTAIN and not triggered):
        why += (
            f"⚠️ 另有 {len(gates)} 项因**不满足波形域检查的前提**整项没有执行"
            f"（{'、'.join(gates)}）—— **上面这个等级是在这些项目缺席的情况下得出的**，"
            "原因见各自 `detail`。"
        )
    if na:
        why += (
            f"⚠️ 另有 {len(na)} 项**对这段音轨无从执行**、未参与判级"
            f"（{'、'.join(na)}）—— 它们既不算「通过」也不算「异常」，"
            "原因见各自 `detail`。"
        )
    return level, why


def plan_upgrades(level: str) -> list[UpgradePath]:
    """更准但更贵的复检路径。**由程序按固定阶梯生成**，不由模型即兴发挥。

    ⚠️ 与图像链同一条纪律：**每一档都返回，不随等级裁剪**。
    末档（声像资料司法鉴定）是**唯一有法律证明力的路径**，
    把它从"低"里删掉，等于对用户隐藏唯一可用的救济路径。
    成本口径取自 2026-10 公开资料；机构报价请以其最新公示为准。
    """
    ladder = [
        UpgradePath(
            tier="online_free",
            name="Audacity / Sonic Visualiser / ENF-Extractor（均为开源）",
            cost="免费",
            turnaround="取决于人工，通常几十分钟",
            when_to_use=(
                "**先做这一步**：把音轨载入 Audacity **放大看波形**，"
                "重点看本报告点出的时间点（数字静音段、谱突变点、相位跳变处）。"
                "再用 Sonic Visualiser 看谱图，或用 ENF-Extractor 单独跑一遍 ENF 轨迹，"
                "人工确认那里的波形是否「接不上」。"
            ),
        ),
        UpgradePath(
            tier="online_paid",
            name="iZotope RX / Cedar 等专业音频修复取证工具",
            cost="商业授权，万元级",
            turnaround="小时级",
            when_to_use=(
                "需要把「可疑处」做**声学层面的解释**时使用（去咔哒、看残余频谱、"
                "分离底噪）。⚠️ 这类工具是**修复工具**，其输出不能作为鉴定意见；"
                "厂商宣称的检测能力多为实验室条件下的指标。"
            ),
        ),
        UpgradePath(
            tier="offline_forensic",
            name="声像资料司法鉴定（录音真实性 / 完整性鉴定）",
            cost="各省价格指引不同，通常 2000–5000 元/份（以受理机构公示为准）",
            turnaround="一般 15–30 个工作日",
            when_to_use=(
                "**涉及法律责任时唯一有证明力的路径**。须委托省级以上司法行政机关"
                "登记在册、业务范围含「声像资料鉴定」的机构，并提交**原始载体**"
                "（原始录音设备 / 存储卡），**不要交转发件** —— "
                "转发件已被重新编码，底噪与编码痕迹都不是原件的样子。"
            ),
        ),
    ]
    return list(ladder)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

#: 「不是音频」那句话的**前缀**（不含括号里的魔术字节判定结果）。
#:
#: ⚠️ 它是**一个值、两个使用者**：
#:   ① `non_audio_clause()` 用它拼整句；
#:   ② `pipeline._FIXES` 那一族用它做**路由关键词**（把"字节族失败"路由到
#:      「你给错了东西、这样改」那条建议，而不是兜底那句）。
#: 两处若各写一份字面量，改句子就会**静默改丢路由** —— 调用方拿到的会是兜底那句
#: （"请确认提交的是完整、未截断的原始音频字节"），而真正该听的是"你给的根本不是
#: 音频"；**任何"字符串相等"的判据都发现不了**（抄得一字不差照样通过）。
NON_AUDIO_CLAUSE_PREFIX = "不是可识别的音频容器"


def is_supported_audio(media_type: str) -> bool:
    """这份字节是不是本能力要处理的音频容器。**唯一判定入口。**

    ⚠️ 任何地方都**不许**再写 `media_type.startswith("audio/")` 这类**代理判据**。
    代理判据的危险不是"判错"，而是"判错的方式随容器名变化"：视频那侧实测过
    ——同一个写法把 `application/ogg`（解码层认得、表里有中文名）挡在门外，
    却让 `video/mp2t` 只因为字符串带 `video/` 前缀就通行无阻。差别只在字符串前缀。

    所以判据是「**解码层认不认得**」，而不是「名字长什么样」：前缀只是其中一条，
    真正的依据是 `_OTHER_CONTAINERS`。以后加容器只需改解码层那张表
    （`tools/verify_audio.py` 第 3c 节按那张表现场派生地验它）。
    """
    return media_type.startswith("audio/") or media_type in _OTHER_CONTAINERS


def non_audio_clause(media_type: str) -> str:
    """「不是音频」那句话的**唯一来源**。

    ⚠️ 2026-10-05 之前的形态是：检测层 `analyze()` 抛异常时拼一次，
    MCP 工具层 `_handle_enf_track` **再手抄一次**。两句话一模一样纯属运气，
    而且当时的判据只比对了「出路文案」（`how_to_fix`）、**没比对原因** ⇒
    改一处、另一处不动，整条判据链**照样全绿**。所以这里提成函数：
    两条路引用同一处，`verify_audio_mcp` 第 3 节再断言这个子句同时出现在两条路上，
    并用**哨兵**做行为化证明（手抄的实现在哨兵下不会跟着变）。
    """
    return f"{NON_AUDIO_CLAUSE_PREFIX}（按魔术字节判定为 {media_type}）"


def analyze(
    data: bytes,
    *,
    declared_mime: str | None = None,
    name: str | None = None,
) -> AudioForensicsReport:
    """对一份音频的原始字节做本地初检，返回结构化报告。

    只接受字节 —— 取字节是调用方的事，本函数**不联网**（因此可离线断言）。
    """
    decoded = decode_audio(data)
    if not is_supported_audio(decoded.media_type):
        raise ValueError(
            non_audio_clause(decoded.media_type)
            + (f"，文件名 {name}" if name else "")
        )
    facts = decoded.facts
    wave = decoded.waveform

    findings: list[Finding] = [
        check_container(facts, declared_mime),
        check_codec_params(facts),
        check_metadata(facts),
        check_spectral_cutoff(wave),
        check_noise_floor(wave),
        check_silence(wave),
        check_spectral_flux(wave),
        check_sample_discontinuity(wave),
        check_enf(wave),
    ]
    level, rationale = summarize(findings)

    pcm_note = (
        f"已解出波形，波形域检查**已执行**（{decoded.window_note}）"
        if wave is not None
        else f"**没有解出波形**：{decoded.pcm_reason}"
    )

    return AudioForensicsReport(
        media_type=decoded.media_type,
        byte_size=len(data),
        sha256=sha256_of(data),
        container=facts.container,
        sample_rate=facts.sample_rate,
        channels=facts.channels,
        bits_per_sample=facts.bits_per_sample,
        duration_seconds=facts.duration_seconds,
        pcm_available=wave is not None,
        pcm_note=pcm_note,
        analysis_window=decoded.window_note or "本次没有可分析的波形。",
        findings=findings,
        suspicion=level,  # type: ignore[arg-type]
        suspicion_rationale=rationale,
        upgrades=plan_upgrades(level),
        chain_of_custody=list(CHAIN_OF_CUSTODY),
        disclaimer=DISCLAIMER,
    )


__all__ = [
    "CHAIN_OF_CUSTODY",
    "CHECK_WEIGHTS",
    "DISCLAIMER",
    "EDITOR_SOFTWARE_HINTS",
    "ENF_BASE_FREQS",
    "GATE_ABSTAIN_PREFIX",
    "MIME_ALIASES",
    "MIN_WAVEFORM_SECONDS",
    "NON_AUDIO_CLAUSE_PREFIX",
    "NOT_APPLICABLE_PREFIX",
    "SILENCE_FLOOR_DBFS",
    "analyze",
    "check_codec_params",
    "check_container",
    "check_enf",
    "check_metadata",
    "check_noise_floor",
    "check_sample_discontinuity",
    "check_silence",
    "check_spectral_cutoff",
    "check_spectral_flux",
    "dbfs",
    "enf_track",
    "goertzel",
    "is_supported_audio",
    "non_audio_clause",
    "normalize_mime",
    "plan_upgrades",
    "robust_threshold",
    "runs_of",
    "sha256_of",
    "summarize",
    "welch_psd",
]
