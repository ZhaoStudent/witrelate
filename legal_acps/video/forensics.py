"""视频鉴伪的**纯函数检测核心** —— numpy / Pillow 只在帧层按需用到。

与图像链 `evidence/forensics.py`、音频链 `audio/forensics.py` 同源：
**不联网、不调模型、可离线断言**。判据本身则在 `legal_acps/verdict.py`
（三条链共用同一段逻辑，只各自提供数据）。

════════════════════════════════════════════════════════════════════════
视频链特有的一个结构性事实：**容器层本身就很有信息量**
════════════════════════════════════════════════════════════════════════

音频链的处境是「解不出波形 ⇒ 基本什么都没得说」。视频不太一样：
容器层（盒结构 / 时间线 / 样本表）**本身就是一条成熟且可验证的取证面**，
而且它是**零依赖**的。所以本模块的七项检查里，有五项完全不需要解码器：

    container   声明与真实容器是否一致、有没有截断 / 尾部多余数据
    structure   盒树布局：ftyp 位置、moov/mdat 前后、mdat 段数、free 填充
    timeline    三处时长是否自洽、采样间隔有没有跳变
    samples     样本表三方是否自洽、chunk 偏移有没有落在 mdat 之外
    edits       编辑列表（elst）—— **「有意跳过/裁剪」的标准做法**
    metadata    创建/修改时间、容器自报的编码工具
    frames      帧层（抽样帧接进图像链）—— **只有这一项需要 Pillow**

## 什么算「线索」，什么只是「常见做法」

这七项里有两项**天然高误报**，必须靠措辞把边界说清楚，否则整份报告
会把每一个正常导出的视频都说成可疑：

  · `structure`：`moov` 在 `mdat` 之后、存在 `free` 填充块、多段 `mdat`
    —— 这几件事在**手机边写边录**与**很多剪辑软件的正常导出**里都会出现。
    它说明的只是「这个文件被完整地写过 / 重写过一遍」。
  · `edits`：`elst` 是**剪映、QuickTime、iPhone 相机**导出时的标准动作
    （去掉前几帧的黑、对齐音视频起点）。它本身不是线索。

真正硬的只有三条：**样本表自相矛盾 / chunk 偏移越界 / 三处时长对不上**。
它们**不可能**由任何正常的写入器产生 —— 那种矛盾只可能来自「手工改过表」
或「把两段不同的媒体拼在一起却没重建表」。
"""

from __future__ import annotations

import hashlib
from typing import Callable, Sequence

from ..evidence.contracts import Finding, UpgradePath
from ..verdict import ABNORMAL, ABSTAIN, summarize_findings
from .contracts import TrackSummary, VideoForensicsReport
# ⚠️ 刻意 import 私名 `_OTHER_CONTAINERS`：它是**解码层的事实**（"我认得这些容器"），
#    而"收不收它"是**本层的策略**。接受判据必须**现场读它** —— 各写一份就是
#    "同一件事写两处"，而 Ogg 被挡在门外那件事（见 `is_supported_video`）的根因
#    正是这个。加容器的方式随之变成**只改解码层那张表**，判据自动跟上。
from .decode import EMPTY_EDIT, Decoded, VideoFacts, _OTHER_CONTAINERS, decode_video
from .frames import (
    GATE_ABSTAIN_PREFIX,
    NOT_APPLICABLE_PREFIX,
    FrameLayerResult,
    analyze_frames,
    extract_gif_frames,
    frame_finding,
)

__all__ = [
    "CHAIN_OF_CUSTODY",
    "CHECK_WEIGHTS",
    "DISCLAIMER",
    "EDITOR_SOFTWARE_HINTS",
    "GATE_ABSTAIN_PREFIX",
    "MIME_ALIASES",
    "MIN_DURATION_MISMATCH_SECONDS",
    "NON_VIDEO_CLAUSE_PREFIX",
    "NOT_APPLICABLE_PREFIX",
    "analyze",
    "check_container",
    "check_edits",
    "check_metadata",
    "check_samples",
    "check_structure",
    "check_timeline",
    "is_supported_video",
    "non_video_clause",
    "normalize_mime",
    "plan_upgrades",
    "sha256_of",
    "summarize",
]

#: 各检查项的权重 —— 综合等级由它算出，**不是模型给的**。
#: 口径与另两条链一致：能**直接**指向「两段不同来源被拼在一起 / 表被改过」
#: 的项给足权重；只在配合其它线索时才成立的项给一半。
#: **这是专家先验，不是拟合出来的** —— 改任何一个都要问一句「凭什么」。
CHECK_WEIGHTS = {
    "samples": 1.5,       # 最强：样本表自相矛盾 / chunk 偏移越界 —— 正常写入器不可能产出
    "timeline": 1.0,      # 三处时长对不上（同一件事实在不同地方写了不同的数）
    "frames": 1.0,        # 帧层直接看到像素（但只覆盖抽样的几帧）
    "edits": 0.5,         # elst 是标准做法，只在配合其它线索时才有意义
    "structure": 0.5,     # moov/mdat 布局：说明「被重写过」，不说明「被改过内容」
    "metadata": 0.5,      # 时间戳与自报工具都**可以零成本改写**
    "container": 0.5,
}

#: 转码 / 编辑软件在元数据里常留下的署名（命中即提示「经过软件处理」）。
#: ⚠️ 与音频侧那张表**故意各写一份**：两者的软件生态不同（这里是剪辑与转码），
#: 而共用一张表的代价是「加一个视频软件名会顺带影响音频链的判定」。
EDITOR_SOFTWARE_HINTS = (
    "ffmpeg", "libav", "lavf", "handbrake", "avidemux", "virtualdub",
    "adobe premiere", "premiere", "after effects", "davinci", "resolve",
    "final cut", "imovie", "capcut", "剪映", "万兴", "wondershare", "filmora",
    "格式工厂", "formatfactory", "mkvmerge", "mkvtoolnix", "mp4box", "gpac",
    "quicktime", "isomediafile", "adobe", "vegas", "edius", "会声会影",
    "corel", "screenflow", "camtasia", "obs studio", "obsproject",
)

#: 常见 MIME 写法 → 本项目按魔术字节判出的规范名。
#: ⚠️ 与另两条链同一条纪律：**这是显式别名表，不是模糊匹配**。
#: 只吸收**同一个容器**的不同写法；「前缀相同就算一致」会让这个检查废掉
#: （`video/mp4` 与 `video/quicktime` 是**两个不同的容器**，必须能分辨）。
MIME_ALIASES = {
    "video/mp4": "video/mp4",
    "video/x-m4v": "video/mp4",
    "video/m4v": "video/mp4",
    "application/mp4": "video/mp4",
    "video/3gpp": "video/mp4",
    "video/quicktime": "video/quicktime",
    "video/x-quicktime": "video/quicktime",
    "video/mov": "video/quicktime",
    "video/x-msvideo": "video/x-msvideo",
    "video/avi": "video/x-msvideo",
    "video/msvideo": "video/x-msvideo",
    "video/x-matroska": "video/x-matroska",
    "video/mkv": "video/x-matroska",
    "video/webm": "video/webm",
    "video/x-ms-wmv": "video/x-ms-wmv",
    "video/x-ms-asf": "video/x-ms-wmv",
    "video/mp2t": "video/mp2t",
    "video/mpeg": "video/mpeg",
    "video/x-flv": "video/x-flv",
    "image/gif": "image/gif",
    "video/gif": "image/gif",
    # ⚠️ Ogg 这一族是 2026-10-04 补的，而**缺的从来不是"容器"、是这里的别名**：
    #    解码层早就认得 Ogg（`_OTHER_CONTAINERS` 里有中文名），但接受闸门用的是
    #    前缀测试 ⇒ Ogg 走不到 `container` 检查这一步，于是"别名表里没有 Ogg"
    #    这件事一直没暴露。一旦让 Ogg 进来（见 `is_supported_video`），
    #    一份**声明完全正确**的 .ogv（标准 MIME 就是 `video/ogg`）会立刻吃到
    #    一条假线索：「调用方声明的是 video/ogg，而字节本身是 application/ogg」。
    #    Ogg 是容器，video/audio 是编解码层的主张，而本进程没有 Ogg 解析器
    #    ⇒ 三种写法一并归到容器名，不当作不一致。
    "application/ogg": "application/ogg",
    "video/ogg": "application/ogg",
    "audio/ogg": "application/ogg",
    "application/x-ogg": "application/ogg",
}

DISCLAIMER = (
    "本报告是基于公开视频 / 容器取证方法得出的**技术线索**，不是司法鉴定意见，"
    "不能单独作为认定事实的依据。所列「未提示异常」**不等于**该视频未经剪辑；"
    "所列「提示异常」**不等于**该视频被篡改 —— 转码、剪辑软件导出、平台二次压缩、"
    "手机边写边录都会产生同类痕迹。如需具备法律效力的结论，请走司法鉴定路径。"
)

CHAIN_OF_CUSTODY = [
    "立即保全**原始文件**（原始存储卡 / 手机 / 行车记录仪 / 只读镜像），"
    "**不要**用微信、QQ、短视频平台转发件代替 —— 转发会重新编码并重写容器，"
    "时间线与样本表全变，之后再鉴定就失去了比对基准。",
    "对原文件计算 SHA-256 并记录时间与操作人；本次报告的 sha256 字段即为该值。",
    "原始文件与任何转发件**分别保全、分别记录来源**：它们不是同一份材料。",
    "记下**这段视频是怎么来的**（哪台设备、什么时间、怎么导出）。"
    "容器里的创建时间与设备信息很容易被改写，而「当事人陈述的来路」不会 —— "
    "两者对不上本身就是最有用的一条线索。",
    "任何后续取证都在**只读副本**上进行，原件封装保存 —— 司法鉴定通常要求提交原始载体。",
]

#: 时长不一致的判定阈值：既要大于「不同时间刻度整除造成的舍入」，
#: 又要小到能抓住真正的改动。1 秒以内且相对差 1% 以内一律不报。
MIN_DURATION_MISMATCH_SECONDS = 0.5
MIN_DURATION_MISMATCH_RATIO = 0.01

#: 采样间隔跳变的判据：最大 / 最小间隔之比达到它才算「跳变」。
#: ⚠️ 设得这么宽是刻意的 —— **可变帧率（VFR）是常态**：
#: 手机录屏、屏幕录制、部分相机的自动曝光调整都会产出多种帧间隔。
#: 只有「差了 5 倍以上」才值得看一眼。
FRAME_RATE_JUMP_RATIO = 5.0

#: `sample count` 与 `stts` 样本总数允许的差异（正常写入器应当**完全相等**）。
SAMPLE_COUNT_TOLERANCE = 0


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


def _finding(check: str, verdict: str, detail: str, caveat: str,
             strength: float | None = None) -> Finding:
    return Finding(check=check, verdict=verdict, detail=detail, caveat=caveat,
                   strength=strength)


def _abstain(check: str, reason: str, caveat: str) -> Finding:
    """**帧层整层没跑**。前缀是判定用的，原因写在冒号后面。

    ⚠️ **只允许帧层那一项调用本函数**。逐项的「这项测不了」要走 `_na()` ——
    两者的区别是「整份报告还能不能下结论」，混用会让每一段正常视频
    都变成「无法判定」（与音频链同一条纪律）。
    """
    return _finding(check, ABSTAIN, f"{GATE_ABSTAIN_PREFIX}：{reason}", caveat)


def _na(check: str, reason: str, caveat: str) -> Finding:
    """**这一项**在这段视频上无从检验（其余各项照常跑）。"""
    return _finding(check, ABSTAIN, f"{NOT_APPLICABLE_PREFIX}：{reason}", caveat)


def _clean(check: str, detail: str, caveat: str) -> Finding:
    return _finding(check, "未提示异常", detail, caveat)


def _alert(check: str, detail: str, caveat: str,
           strength: float | None = None) -> Finding:
    return _finding(check, "提示异常", detail, caveat, strength)


# ---------------------------------------------------------------------------
# 1. container —— 容器自洽性 + 声明一致性
# ---------------------------------------------------------------------------

def check_container(facts: VideoFacts, declared_mime: str | None) -> Finding:
    """容器头部与它自己的内容是否对得上；调用方声明的类型是否属实。

    这里报的不是「这个文件可疑」，而是「这个文件的头部与内容对不上」 ——
    例如 RIFF 声明 1,000,000 字节、实际只有 998,000（截断），
    或最后一个盒之后还挂着 4 KB 的额外数据（拼接时没修头）。
    """
    check = "container"
    issues: list[str] = []
    if facts.truncated:
        issues.append(
            "容器声明的长度**大于**实际可用字节 ⇒ 这份文件是不完整的"
            "（传输中断 / 保存失败 / 头部被改过）"
        )
    if facts.appended_bytes > 0:
        issues.append(
            f"容器声明范围之外还有 **{facts.appended_bytes} 字节**额外数据"
            "（合法的写入器不会这么干；常见于手工拼接，或附加了另一种格式的尾巴）"
        )

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

    caveat = (
        "容器层只说明**文件是怎么被写出来的**，不说明画面内容被动过。"
        "截断与尾部多余数据最常见的成因是**传输 / 保存事故**，而不是有人篡改。"
    )
    if facts.parse_depth == "identify_only":
        # ⚠️ 这一支必须**单独说**：只识别、不解析的容器（TS / FLV / WMV / Ogg）
        #    没有盒树，上面那几条结构性判据一条都跑不了。把「没解析」写成
        #    「未提示异常」，就是本项目最忌讳的那种静默安全结论。
        mime_note += (
            " ⚠️ 本进程**没有为这种容器写解析器**，因此容器内部的结构、"
            "时间线与样本表**本次没有检查**。"
        )
    detail = (
        "；".join(issues) + "。" if issues else
        f"容器头部与内容自洽（{facts.container}）。"
    )
    if mime_note:
        detail += mime_note

    if issues:
        return _alert(check, detail, caveat)
    return _clean(check, detail, caveat)


# ---------------------------------------------------------------------------
# 2. structure —— 盒树 / 分块布局
# ---------------------------------------------------------------------------

def check_structure(facts: VideoFacts) -> Finding:
    """容器**是怎么被写出来的**：盒的顺序、媒体数据被分成几段、有没有填充块。

    ⚠️ 这一项的线索**天然弱**：`moov` 在 `mdat` 之后 = 边写边录（手机相机的
    默认行为），多段 `mdat` = 分段写入（某些导出器与「暂停再录」都会这样）。
    它们说明的是「这个文件被**完整地写过一遍**」，而不是「内容被改过」。
    所以权重只给 0.5，且 caveat 必须把正常成因列全。
    """
    check = "structure"
    caveat = (
        "这几条线索说明「这个文件被完整地重写过一遍」，**不说明画面被改过**："
        "手机边写边录（`moov` 在 `mdat` 之后）、导出器分段写盘（多段 `mdat`）、"
        "为对齐写入的 `free` 填充块，都是**正常**行为。"
        "只有与 `samples` / `timeline` 的硬矛盾同时出现时才值得深究。"
    )

    if facts.parse_depth == "identify_only":
        return _na(
            check,
            "这种容器（不是盒树结构）本进程没有写解析器，"
            "盒顺序 / 媒体数据分段 / 填充块这几件事一概看不到。",
            caveat,
        )
    if facts.parse_depth != "full" or not facts.top_level_boxes:
        return _na(
            check,
            "本次没有读到可分析的顶层结构（容器只被识别到类型这一层）。",
            caveat,
        )

    issues: list[str] = []
    boxes = facts.top_level_boxes
    if boxes[0] != "ftyp":
        issues.append(
            f"第一个顶层盒是 `{boxes[0]}` 而不是 `ftyp` "
            "（ISO BMFF 要求 `ftyp` 在最前；不满足的写入器很少见）"
        )
    if facts.fragmented and not facts.has_mvex:
        issues.append(
            "存在 `moof`（分片）但**没有** `mvex` —— 分片与视频轨的声明不一致"
        )
    if facts.moov_before_mdat is False:
        issues.append(
            "`moov` 位于 `mdat` **之后**（非流式布局：边写边录，或事后重新封装过）"
        )
    if facts.mdat_count > 1:
        issues.append(
            f"媒体数据被分成 **{facts.mdat_count} 段** `mdat` 写入"
            "（一次连续的录制通常只有一段；分段常见于暂停续录、分段导出或拼接）"
        )
    if facts.free_bytes > 0:
        issues.append(
            f"存在 **{facts.free_bytes} 字节**的 `free` / `skip` 填充块"
            "（为对齐而写；改写过的文件常留下这类残留）"
        )

    detail = (
        "；".join(issues) + "。" if issues else
        f"盒树布局没有异常（顶层盒：{'、'.join(boxes)}）。"
    )
    if facts.mdat_count == 0:
        detail += " ⚠️ 本次**没有找到 `mdat`** —— 这份字节里没有媒体数据实体。"
        return _alert(check, detail, caveat)
    if issues:
        return _alert(check, detail, caveat)
    return _clean(check, detail, caveat)


# ---------------------------------------------------------------------------
# 3. timeline —— 三处时长是否自洽 + 采样间隔有没有跳变
# ---------------------------------------------------------------------------

def check_timeline(facts: VideoFacts) -> Finding:
    """`mvhd` / `tkhd` / `mdhd` **三处声明的时长**是否互相对得上。

    这是本模块**最硬的三条线索之一**：同一个时长在三个地方各写一遍，
    正常写入器产出的三个值必然一致（差异只来自时间刻度的整数舍入）。
    对不上意味着「这张表是人手改过、或者两段不同的媒体被拼在一起却没重建表」。
    """
    check = "timeline"
    caveat = (
        "时长对不上**也可能**来自合法的处理：跨刻度换算的舍入、"
        "音频轨比视频轨长（尾部留白）、以及某些写入器对 `tkhd` 用"
        "「播放时长」而对 `mdhd` 用「媒体时长」的约定。"
        "所以本项只报「对不上」这个事实与差值大小，不下任何结论。"
    )

    if facts.duration_seconds is None:
        return _na(
            check,
            "容器头部没有给出可用的总时长（`mvhd` 缺失或时长字段为 0），"
            "本次无法做时长比对。",
            caveat,
        )

    video_tracks = [t for t in facts.tracks if t.handler == "video"]
    if not video_tracks:
        return _na(
            check,
            "本次没有识别出视频轨（只有音频轨，或 `hdlr` 缺失），"
            "时长比对的对象不存在。",
            caveat,
        )

    total = facts.duration_seconds
    issues: list[str] = []
    for index, track in enumerate(video_tracks):
        label = f"第 {index + 1} 条视频轨"
        for name, value in (
            ("`mdhd` 媒体时长", track.duration_seconds),
            ("`tkhd` 播放时长", track.track_header_duration_seconds),
        ):
            if value is None:
                continue
            delta = abs(value - total)
            if delta <= MIN_DURATION_MISMATCH_SECONDS:
                continue
            if total > 0 and delta / total <= MIN_DURATION_MISMATCH_RATIO:
                continue
            issues.append(
                f"{label}的 {name} 是 {value:.3f} 秒，"
                f"而 `mvhd` 的总时长是 {total:.3f} 秒（差 **{delta:.3f} 秒**）"
            )

        # 采样间隔跳变：**只报「差了 FRAME_RATE_JUMP_RATIO 倍以上」的那种**。
        lo, hi = track.delta_min, track.delta_max
        if lo and hi and lo > 0 and hi / lo >= FRAME_RATE_JUMP_RATIO:
            issues.append(
                f"{label}的帧间隔在 {lo} ~ {hi} 个时间单位之间（相差 "
                f"{hi / lo:.1f} 倍）⇒ 这一段**不是恒定帧率**"
            )

    if issues:
        return _alert(check, "；".join(issues) + "。", caveat)
    return _clean(
        check,
        f"`mvhd` 总时长 {total:.3f} 秒，各视频轨的 `mdhd` / `tkhd` 时长与之相符。",
        caveat,
    )


# ---------------------------------------------------------------------------
# 4. samples —— 样本表自洽 + chunk 偏移是否落在媒体数据里
# ---------------------------------------------------------------------------

def check_samples(facts: VideoFacts) -> Finding:
    """样本表的三方比对，以及 chunk 偏移有没有指向媒体数据之外。

    **这是本模块最硬的一项**：

      · `stsz` 说「一共 N 个样本」，而 `stts` 说「一共 M 个样本」 —— 两者不等
        ⇒ 表内部自相矛盾，正常写入器产不出这种文件；
      · `stco` 里的 chunk 偏移**落在所有 `mdat` 之外** ⇒ 指针指向了不存在的地方。

    两者都只可能来自「手工改过表」或「拼接后没重建表」。
    """
    check = "samples"
    caveat = (
        "样本表是容器里机器读的索引，**人眼看不到**，所以它被改过时文件"
        "在播放器里可能完全正常 —— 反过来，这一项报出矛盾也不等于画面被动过："
        "它只说明「这张索引表内部不自洽」，而重写表最常见的原因是**转码工具的实现差异**。"
    )

    if facts.parse_depth != "full" or not facts.top_level_boxes:
        return _na(
            check,
            "本次没有读到样本表（容器没有被解析到 `stbl` 这一层）。",
            caveat,
        )

    issues: list[str] = []
    notes: list[str] = []

    # -- ISO BMFF -----------------------------------------------------------
    for index, track in enumerate(facts.tracks):
        label = f"第 {index + 1} 条轨（{track.handler}）"
        sizes_n = track.sample_count
        times_n = track.delta_sample_total
        if sizes_n is not None and times_n is not None:
            if abs(sizes_n - times_n) > SAMPLE_COUNT_TOLERANCE:
                issues.append(
                    f"{label}的样本数**自相矛盾**：`stsz` 说 {sizes_n} 个，"
                    f"`stts` 说 {times_n} 个"
                )

    if facts.tracks and not facts.mdat_ranges:
        issues.append("容器里没有任何 `mdat`，但样本表却在描述媒体数据")
    if facts.mdat_ranges:
        low = min(start for start, _ in facts.mdat_ranges)
        high = max(end for _, end in facts.mdat_ranges)
        for index, track in enumerate(facts.tracks):
            lo_off, hi_off = track.chunk_offset_min, track.chunk_offset_max
            if lo_off is None or hi_off is None:
                continue
            if lo_off < low or hi_off >= high:
                issues.append(
                    f"第 {index + 1} 条轨的 chunk 偏移落在"
                    f" **{lo_off}~{hi_off}**，而媒体数据的范围是 **{low}~{high}** "
                    "—— 索引指向了不存在的地方"
                )
            if track.chunk_count and track.chunk_count > 200_000:
                notes.append(
                    f"（第 {index + 1} 条轨的 chunk 表有 {track.chunk_count} 项，"
                    "为避免耗尽内存与时间，本次只扫描了前 20 万项 —— "
                    "**后面的项没有核对**。）"
                )

    # -- AVI ----------------------------------------------------------------
    if facts.actual_frame_chunks is not None and facts.declared_frame_count is not None:
        if facts.actual_frame_chunks != facts.declared_frame_count:
            issues.append(
                f"AVI 头部声明 {facts.declared_frame_count} 帧，"
                f"而 `movi` 里实际数出 {facts.actual_frame_chunks} 个视频块 "
                "—— 帧数与索引对不上"
            )
    if facts.has_index is False:
        notes.append(
            "（这份 AVI 没有 `idx1` 索引块。部分写入器本来就不写它，"
            "本身不是缺陷，但缺少索引会让「帧数对不上」更难判断。）"
        )

    # -- GIF ----------------------------------------------------------------
    if facts.media_type == "image/gif" and facts.actual_frame_chunks is not None:
        notes.append(
            f"（GIF 按块计数共 {facts.actual_frame_chunks} 帧；"
            "GIF 没有独立的样本表，因此本项只做了帧计数。）"
        )

    detail = ("；".join(issues) + "。" if issues else
              "样本表内部自洽（样本数、帧数、chunk 偏移三者没有矛盾）。")
    if notes:
        detail += "".join(notes)
    if issues:
        return _alert(check, detail, caveat)
    return _clean(check, detail, caveat)


# ---------------------------------------------------------------------------
# 5. edits —— 编辑列表（elst）
# ---------------------------------------------------------------------------

def check_edits(facts: VideoFacts) -> Finding:
    """`edts/elst`：**「有意跳过 / 裁剪片段」的标准做法**。

    `media_time != 0` 意味着「从媒体时间轴的某一刻开始播」 —— 前面那段被跳过了。
    ⚠️ 剪映 / QuickTime / iPhone 相机导出**普遍会写** `elst`（去黑帧、对齐音视频），
    所以它是**弱线索**：权重 0.5，且 caveat 必须把正常成因说清楚。
    """
    check = "edits"
    caveat = (
        "`elst` 是**标准且常见**的做法，不是篡改手法：剪映、QuickTime、"
        "iPhone 相机在导出时都会写它，用来去掉开头几帧、对齐音视频起点。"
        "因此这一项**只有在与时长矛盾同时出现时**才值得深究 —— 单独看它，"
        "几乎每一个正常导出的视频都会命中。"
    )

    if facts.parse_depth != "full" or not facts.top_level_boxes:
        return _na(
            check,
            "本次没有读到编辑列表（容器没有被解析到 `edts` 这一层）。",
            caveat,
        )

    hit: list[str] = []
    checked = 0
    for index, track in enumerate(facts.tracks):
        checked += 1
        for seg, media, rate in track.edit_list:
            # ⚠️ `media == EMPTY_EDIT`（-1）是**空编辑**：这一段整段没有媒体。
            #    它**必须单独判**，不能并进 `media > 0` ——
            #    否则"把开头剪掉一块"最标准的写法会一条线索都不留，
            #    而下面的干净分支还会说「要么没有 elst，要么它的 media_time 为 0」。
            if media == EMPTY_EDIT:
                hit.append(
                    f"第 {index + 1} 条轨（{track.handler}）的编辑列表里有一个**空编辑**"
                    "（`media_time = -1`）—— 这一段整段没有媒体，"
                    "也就是**时间轴上被挖掉了一块**（最常见的是掐掉开头）"
                )
            elif media > 0:
                hit.append(
                    f"第 {index + 1} 条轨（{track.handler}）的编辑列表从媒体时间轴的 "
                    f"**{media:.3f} 秒**处开始播放（段声明时长 {seg:.3f} 秒）"
                    "—— 也就是**前面那段被跳过了**"
                )
            if abs(rate - 1.0) > 1e-6:
                hit.append(
                    f"第 {index + 1} 条轨的编辑列表把播放速率设成了 {rate:.4f}"
                    "（非 1.0 ⇒ 这一段被变速过）"
                )

    if checked == 0:
        return _na(check, "本次没有识别出任何轨道，编辑列表无从检查。", caveat)
    if hit:
        return _alert(check, "；".join(hit) + "。", caveat)
    return _clean(
        check,
        f"本次检查了 {checked} 条轨，**没有发现**带跳段或变速的编辑列表"
        "（要么没有 `elst`，要么它的 `media_time` 为 0）。",
        caveat,
    )


# ---------------------------------------------------------------------------
# 6. metadata —— 时间戳与容器自报的编码工具
# ---------------------------------------------------------------------------

def check_metadata(facts: VideoFacts) -> Finding:
    """创建 / 修改时间，以及容器**自报**的编码工具。

    ⚠️ 这一项的两条线索**都可以零成本改写**（时间戳与 `©too` 字段就是几个字节），
    所以它只当辅助。但「自报的编码器恰好是一个剪辑软件」这件事仍然有用：
    它把「这段视频**经过剪辑软件的手**」从猜测变成了事实。
    """
    check = "metadata"
    caveat = (
        "创建 / 修改时间与编码工具字符串都是容器里**自报**的字段，"
        "改写它们的成本几乎为零 ⇒ **不能**作为认定依据。"
        "另外，命中剪辑软件名只说明「这段视频被某个软件导出过」，"
        "而**导出**是上传、压缩、转格式的常规步骤，本身不代表剪辑。"
    )

    issues: list[str] = []
    hints: list[str] = []
    encoder = facts.encoder_hint or ""
    haystack = " ".join([encoder, *facts.tags, facts.muxing_app or "",
                         facts.writing_app or ""]).lower()
    for needle in EDITOR_SOFTWARE_HINTS:
        if needle in haystack:
            hints.append(needle)
            break

    if hints:
        issues.append(
            f"容器自报的编码工具命中 **{hints[0]}**"
            + (f"（原始值：{encoder!r}）" if encoder else "")
            + " ⇒ 这段视频**经过该软件处理过**（导出、转码或剪辑）"
        )

    created, modified = facts.creation_time, facts.modification_time
    if created and modified and modified < created:
        issues.append(
            f"修改时间（{modified}）**早于**创建时间（{created}）—— 时间戳倒挂"
        )

    stamp_note = (
        f"容器声明的时间：创建 {created or '未提供'}，修改 {modified or '未提供'}。"
    )
    if not created and not modified and not issues:
        return _na(
            check,
            "容器头部**没有给出**可核对的创建 / 修改时间，也没有自报的编码工具，"
            "本次没有可检查的元数据。",
            caveat,
        )

    detail = ("；".join(issues) + "。" if issues else
              "容器自报的编码工具里没有命中常见剪辑 / 转码软件，时间戳顺序正常。")
    detail += stamp_note
    if issues:
        return _alert(check, detail, caveat)
    return _clean(check, detail, caveat)


# ---------------------------------------------------------------------------
# 7. frames —— 帧层（抽样帧接进图像鉴权链）
# ---------------------------------------------------------------------------

def check_frames(decoded: Decoded, layer: FrameLayerResult) -> Finding:
    """帧层的结论由 `frames.frame_finding()` 生成 —— **判据不在这里**。

    本函数存在的理由只是「七项检查要由同一个 `analyze()` 平铺列出来」，
    免得以后有人以为少了一项。
    """
    total = None
    for track in decoded.facts.tracks:
        if track.handler == "video" and track.sample_count:
            total = track.sample_count
            break
    return frame_finding(layer, total_frames=total)


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def _gate_rationale(gated: list[str]) -> str:
    """帧层整层没跑时给读者的解释。**判据触发条件由 `verdict.py` 统一决定。**"""
    labels = "、".join(gated)
    return (
        "本次**取不到任何可分析的帧**，因此帧层检查（"
        f"{labels}）**整层没有执行**。"
        "本报告只覆盖容器结构、时间线自洽与元数据 —— 那几层能发现"
        "「文件被重写过 / 索引被改过 / 两段媒体拼在一起」，"
        "但**看不到画面本身**，也不能据此认为这段视频没有问题。"
    )


def summarize(findings: Sequence[Finding]) -> tuple[str, str]:
    """把逐项发现折成综合线索等级。

    ⚠️ **判据本身不在本函数里**，在 `legal_acps.verdict.summarize_findings()`
    —— 图像链、音频链、视频链共用同一段逻辑，只各自提供数据。
    三条链各写一份的话，就会出现**同一种失败在图像链是「无法判定」、
    在视频链是「低」**，而报告表面看不出任何异样。

    ⚠️ `gate_prefixes` **只给材料级前缀**（`GATE_ABSTAIN_PREFIX`）。
    把逐项前缀也塞进来，等于让「某一项不适用」拥有把整份报告压成
    「无法判定」的权力。反过来的隐患要在这里补上：等级既然照常给出，
    **就必须让读者知道有几项其实没参与判级**。
    """
    items = list(findings)
    level, why = summarize_findings(
        items,
        weights=CHECK_WEIGHTS,
        gate_prefixes=(GATE_ABSTAIN_PREFIX,),
        gate_rationale=_gate_rationale,
        subject="这段视频",
        finding_noun="篡改认定",
        clean_phrase="未经剪辑",
    )
    # ⚠️ 口径必须与共享判据一致：`triggered` = **提示异常**的项数。
    #    早先这里写成 `!= ABSTAIN`，于是「未提示异常」也被算成「有触发」
    #    ⇒ 连「全是干净项 + 一个材料级闸门」这种最普通的情形都会被判成
    #    「分支①走过」，下面那句补充说明就在 `_gate_rationale` 之外**又加一遍**，
    #    读者看到同一件事被说两次。判据漂移在措辞里最难发现 ——
    #    等级是对的，错的只是「这句该不该出现」。
    triggered = [f.check for f in items if f.verdict == ABNORMAL]
    gates = [
        f.check for f in items
        if f.verdict == ABSTAIN and str(f.detail).startswith(GATE_ABSTAIN_PREFIX)
    ]
    na = [
        f.check for f in items
        if f.verdict == ABSTAIN and str(f.detail).startswith(NOT_APPLICABLE_PREFIX)
    ]

    # ⚠️ 「材料级闸门 + 同时有正向触发」这一组合最容易漏交代：
    #    此时共享判据走的是分支①（有触发就先算账），`_gate_rationale`
    #    **根本没被调用** ⇒ 报告会给一个等级，却只字不提「另有几项整项没跑」。
    #    等级不是错的，**读者对它的信任程度会错**。所以这里为其余分支补一句。
    if gates and not (level == ABSTAIN and not triggered):
        why += (
            f"⚠️ 另有 {len(gates)} 项因**取不到可分析的帧**整项没有执行"
            f"（{'、'.join(gates)}）—— **上面这个等级是在这些项目缺席的情况下得出的**，"
            "原因见各自 `detail`。"
        )
    if na:
        why += (
            f"⚠️ 另有 {len(na)} 项对这段视频无从执行、未参与判级"
            f"（{'、'.join(na)}）—— 它们既不算「通过」也不算「异常」，"
            "原因见各自 `detail`。"
        )
    return level, why


def plan_upgrades(level: str) -> list[UpgradePath]:
    """更准但更贵的复检路径。**由程序按固定阶梯生成**，不由模型即兴发挥。

    ⚠️ 与另两条链同一条纪律：**每一档都返回，不随等级裁剪**。
    末档（声像资料司法鉴定）是**唯一有法律证明力的路径**，
    把它从「低」里删掉，等于对用户隐藏唯一可用的救济路径。
    成本口径取自 2026-10 公开资料；机构报价请以其最新公示为准。
    """
    del level
    return [
        UpgradePath(
            tier="online_free",
            name="MediaInfo / GPAC（MP4Box）/ ffprobe（均为免费工具）",
            cost="免费",
            turnaround="取决于人工，通常几十分钟",
            when_to_use=(
                "**先做这一步**：用 MediaInfo 看一遍容器与轨道的**完整字段**"
                "（本报告只摘了其中一部分），再用 `MP4Box -info` 或 "
                "`ffprobe -show_packets` 把**逐样本时间戳**导出来，"
                "人工核对本报告点出的可疑时间段。⚠️ 这些工具**只读表、不解码**，"
                "所以它们能验证「表是否自洽」，但看不到画面。"
            ),
        ),
        UpgradePath(
            tier="online_paid",
            name="Amped Authenticate / VideoForensics 等商用取证软件",
            cost="商业授权，万元级起",
            turnaround="小时级",
            when_to_use=(
                "需要把「可疑处」做**像素层面的解释**时使用（关键帧比对、"
                "压缩历史、双重压缩检测、画面内目标追踪）。"
                "⚠️ 这类软件的输出是**技术分析报告**，不是鉴定意见；"
                "厂商宣称的检测率多为实验室条件下的指标。"
            ),
        ),
        UpgradePath(
            tier="offline_forensic",
            name="声像资料司法鉴定（视频真实性 / 完整性鉴定）",
            cost="各省价格指引不同，通常 3000–8000 元/件（以受理机构公示为准）",
            turnaround="一般 30 个工作日左右",
            when_to_use=(
                "**涉及法律责任时唯一有证明力的路径**。须委托省级以上司法行政机关"
                "登记在册、业务范围含「声像资料鉴定」的机构，并提交**原始载体**"
                "（原始存储卡 / 手机 / 行车记录仪），**不要交转发件** —— "
                "转发件已被重新编码并重写容器，时间线与样本表都不是原件的样子。"
                "同时提交这段视频的**来路说明**（哪台设备、何时、如何导出）。"
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# 「这份字节我们要不要处理」—— **单一来源**
# ---------------------------------------------------------------------------
#
# ⚠️ 2026-10-04 实测出来的一个真缺陷，原样记在这里，免得改回去：
#
#     `application/ogg` **在解码层的容器表里**（`decode._OTHER_CONTAINERS`，
#     还配了中文名「Ogg（Theora 视频 / Opus 音频）」），`decode.sniff_media_type`
#     为它写了专门的 `OggS` 分支，`identify_only_note` 的 docstring 更直接
#     把它列为适用对象（"TS / FLV / WMV / **Ogg**"）—— 但原来的闸门写的是
#     `media_type.startswith("video/")`，一个**代理判据**。于是 Ogg 在自己的
#     家门口被拦下，而拒绝理由是一句假话：「不是可识别的视频容器」。
#     同族的 `video/mp2t` 因为带着 `video/` 前缀而通行无阻 —— 差别只在字符串前缀。
#
# 根因不是"漏了一个容器"，而是**「谁算视频」写了两处**：一处是解码层的容器表
# （认得 Ogg），一处是这个前缀测试（不认）。所以修法是：
#
#   ① 判据收成**下面这一个函数**，`analyze()` 与 MCP 工具层都调它；
#   ② "接受集合 ⊇ 解码层两张容器表"由 `tools/verify_video.py` **现场派生**地
#      逐容器断言（加容器不许漏），而不是靠这份注释承诺。

def is_supported_video(media_type: str) -> bool:
    """这份字节是不是本能力要处理的视频容器。**唯一判定入口。**

    ⚠️ 任何地方都**不许**再写 `media_type.startswith("video/")` 这类代理判据 ——
    MCP 工具层那三个工具的前提判定走的也是本函数（经 `_video_module()` 导入），
    所以"能分析"与"工具肯不肯收"必然是同一个答案。

    ⚠️ 判据里**读解码层那张表**（`_OTHER_CONTAINERS`），于是：
    「认得但没解析器」的容器只要进了表，就自动被接纳（走 `identify_only`
    —— 出报告、逐项如实说明哪几层没跑），不需要在这里再加一行。
    """
    return (
        media_type.startswith("video/")
        or media_type == "image/gif"
        or media_type in _OTHER_CONTAINERS
    )


#: 「不是视频」那句话的**前缀**（不含括号里的魔术字节判定结果）。
#:
#: ⚠️ 它是**一个值、两个使用者**：`non_video_clause()` 用它拼整句，
#:    `pipeline._FIXES` 那一族用它做**路由关键词**（把"字节族失败"路由到
#:    「你给错了东西、这样改」那条建议）。两处各写一份字面量的话，改句子就会
#:    **静默改丢路由**，而任何"字符串相等"的判据都发现不了（抄得一字不差照样通过）。
NON_VIDEO_CLAUSE_PREFIX = "不是可识别的视频容器"


def non_video_clause(media_type: str) -> str:
    """「不是视频」那句话的**唯一来源**。

    ⚠️ 2026-10-04 之前的形态是：检测层 `analyze()` 抛异常时拼一次，
    MCP 工具层的 `_non_video_result()` **再手抄一次**。两句话一模一样纯属运气，
    而且当时的判据只比对了「出路文案」（`how_to_fix`）、**没比对原因** ⇒
    改一处、另一处不动，整条判据链**照样全绿**。所以这里提成函数：
    两条路引用同一处，`verify_video_mcp` 第 3 节再断言这个子句同时出现在两条路上。
    """
    return f"{NON_VIDEO_CLAUSE_PREFIX}（按魔术字节判定为 {media_type}）"


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def analyze(
    data: bytes,
    *,
    declared_mime: str | None = None,
    name: str | None = None,
) -> VideoForensicsReport:
    """对一份视频的原始字节做本地初检，返回结构化报告。

    只接受字节 —— 取字节是调用方的事，本函数**不联网**（因此可离线断言）。
    """
    decoded = decode_video(data)
    # ⚠️ 判据走 `is_supported_video()`（单一来源）—— 这里**不许**再写前缀测试：
    #    那样会让"认得但没解析器"的容器里，凡媒体类型不带 `video/` 前缀的
    #    全被拦下（Ogg 就是这么被拦的，见上面那段）。
    if not is_supported_video(decoded.media_type):
        raise ValueError(
            non_video_clause(decoded.media_type)
            + (f"，文件名 {name}" if name else "")
        )

    frames = list(decoded.frames)
    frame_source = decoded.frame_source
    frame_reason = decoded.frames_reason
    # GIF 的帧要光栅化才有 —— 那是唯一需要 Pillow 的一步，放在 frames 里按需做。
    if not frames and decoded.media_type == "image/gif":
        gif_frames, why = extract_gif_frames(data)
        if gif_frames:
            frames = list(gif_frames)
            frame_source = "animated_gif"
            frame_reason = None
        elif why:
            # ⚠️ GIF 光栅化失败时**必须换成它自己的原因**，不能沿用上面那句
            #    「容器里没有逐帧存着的 JPEG/PNG」—— 对 GIF 而言那是**错的原因**：
            #    GIF 每一帧本来就是一张完整的位图（调色板 + LZW），
            #    问题出在「本进程没有 Pillow」或者「这份 GIF 的光栅化在中途失败」。
            #    沿用错原因，调用方会照着「这个容器不支持取帧」去换文件，
            #    而真正该做的是装 Pillow、或换一份能打开的 GIF。
            frame_reason = why

    # ⚠️ 把「为什么一帧都没取到」**如实转交给帧层**：`analyze_frames` 里那句
    #    写死的 MP4 文案在别的容器上是错的（见该函数的注释）。
    layer = analyze_frames(
        frames,
        unavailable_reason=(
            f"{GATE_ABSTAIN_PREFIX}：{frame_reason}"
            if not frames and frame_reason else None
        ),
    )

    findings: list[Finding] = [
        check_container(decoded.facts, declared_mime),
        check_structure(decoded.facts),
        check_timeline(decoded.facts),
        check_samples(decoded.facts),
        check_edits(decoded.facts),
        check_metadata(decoded.facts),
        check_frames(decoded, layer),
    ]
    level, rationale = summarize(findings)

    #: 帧层可用性的人话说明。**必须写清后果**，不能只说「没解码器」。
    if layer.analyzed:
        from .frames import frame_layer_note  # noqa: PLC0415 - 只为措辞，不参与判定

        frames_note = frame_layer_note(layer, total_frames=_video_sample_count(decoded))
    else:
        frames_note = frame_reason or layer.reason

    video_track = next(
        (t for t in decoded.facts.tracks if t.handler == "video"), None
    )
    return VideoForensicsReport(
        media_type=decoded.media_type,
        byte_size=len(data),
        sha256=sha256_of(data),
        container=decoded.facts.container,
        brand=decoded.facts.brand,
        duration_seconds=decoded.facts.duration_seconds,
        video_codec=(video_track.codec if video_track and video_track.codec else None),
        width=(video_track.coded_width if video_track else None),
        height=(video_track.coded_height if video_track else None),
        frame_count=(video_track.sample_count if video_track else None),
        tracks=[
            TrackSummary(
                handler=t.handler,
                codec=t.codec,
                timescale=t.timescale,
                duration_seconds=t.duration_seconds,
                sample_count=t.sample_count,
                width=t.coded_width,
                height=t.coded_height,
            )
            for t in decoded.facts.tracks
        ],
        frames_available=bool(layer.analyzed),
        frames_note=frames_note,
        frames_analyzed=layer.analyzed,
        frame_source=frame_source if layer.analyzed else "none",
        findings=findings,
        suspicion=level,  # type: ignore[arg-type]
        suspicion_rationale=rationale,
        upgrades=plan_upgrades(level),
        chain_of_custody=list(CHAIN_OF_CUSTODY),
        disclaimer=DISCLAIMER,
    )


def _video_sample_count(decoded: Decoded) -> int | None:
    for track in decoded.facts.tracks:
        if track.handler == "video" and track.sample_count:
            return int(track.sample_count)
    return None


#: 供 `summarize()` 的措辞回调使用（`verdict.summarize_findings` 要求它是纯函数）
_ = Callable
