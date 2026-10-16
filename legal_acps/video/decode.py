"""字节 → 容器事实 + 可分析帧。**只做解析与事实提取，不做任何判定。**

分层理由与音频链 `audio/decode.py` 同源：判定全部落在 `forensics.py`，
这一层只回答「字节里写着什么」。

════════════════════════════════════════════════════════════════════════
⚠️ 本模块**只依赖 Python 标准库**（连 numpy 都不需要）
════════════════════════════════════════════════════════════════════════

这不是巧合，是设计：视频的容器层解析（ISO BMFF 盒树 / RIFF AVI / EBML / GIF 块）
本来就是纯粹的字节偏移计算，**一个数学库都用不上**。这一点直接决定了两件事：

  · 「服务器上没装 numpy」时，视频链**仍能给出容器层结论**（只是帧层跑不了）；
  · 样本构造（`run/video_samples.py`）可以完全脱离运行环境，
    在任何 Python 上逐位复现 —— 断言才有意义。

⚠️ 与音频侧**刻意不同**的一点：**帧层不在这里**。
   `audio/decode.py` 用 numpy 解出波形；视频这边「H.264 解帧」需要 ffmpeg，
   本进程没有 ⇒ 能取到的只有「容器里本来就逐帧存着 JPEG/PNG」的那几种。
   那部分同样是纯字节搬运，所以留在这里，且**零解码**。
   真正需要 Pillow 的（动画 GIF 的逐帧光栅化）放在 `frames.py`，按需导入。

## 能「零解码」取帧的容器，比想象中多

    MJPEG / AVI   `strh.fccHandler` ∈ {MJPG, mjpg, JPEG, dmb1, MJPA, MJPB}
                  ⇒ `movi` 里每个视频块**就是一张完整的 JPEG**
    Motion JPEG 的 MP4/MOV   `stsd` 样本格式 ∈ {jpeg, mjpa, mjpb}
                  ⇒ 每个样本就是一张 JPEG（按 stbl 的 stco/stsc/stsz 定位）
    MP4/MOV 内嵌封面  `moov/udta/meta/ilst/covr/data`
                  ⇒ 导出工具写进去的一张 JPEG / PNG（常为视频首帧）
    动画 GIF      每帧是 LZW 压缩位图 ⇒ 需要 Pillow，见 `frames.py`

**这些帧是原始像素数据，不是我们转码出来的** —— 这一点很要紧：
如果为了「能给模型看」而把帧重新编码成 JPEG，图像链的量化表 / 块网格那几项
就会量到**我们自己编码器的痕迹**，那是纯人造的假线索。
所以本模块**从不重编码**：取到什么字节就是什么字节。
"""

from __future__ import annotations

# ⚠️ `verdict` 是三条链共用的纯逻辑模块（不 import pydantic / 第三方库），
#    这里引进来只为 `stable_reason`：异常文本里若带着内存地址，
#    同一份字节两次跑出的报告就不一样（旁证复核会说不清 —— 详见那里的注释）。
from ..verdict import stable_reason
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Iterator

#: 最多取几帧去做像素域分析。**这是抽样，不是全覆盖** ——
#: 报告里必须如实写 `frames_analyzed`，让人看得出「4 帧 != 全片」。
#: 取 4 而不是 1：一帧看不出「帧与帧之间不一致」，而后者恰恰是拼接的痕迹。
MAX_FRAMES = 4

#: 单帧字节上限。取出一个超大的内嵌图会连带影响其它角色的可用性，
#: 而「这一帧没分析」本身是可如实报告的事实。
MAX_FRAME_BYTES = 4 * 1024 * 1024

#: 定位样本时最多走多少个 chunk。`stco` 动辄上百万项，而我们要的只是
#: 首 / 中 / 末三个样本 ⇒ 超出这个预算就**如实报告「没能定位到」**，
#: 而不是让服务卡在那里（除步数预算外还有真实的时间预算）。
MAX_CHUNK_SCAN = 200_000

#: 一次最多保留多少个「不同的 stts 采样间隔」用来说明帧率是否跳变。
MAX_DELTA_KINDS = 8

#: 解析时最多看多少个盒（防畸形文件把内存与时间吃光）。
MAX_BOXES = 20_000

# ---------------------------------------------------------------------------
# 容器标识
# ---------------------------------------------------------------------------

#: 我们**能解析内部结构**的容器（其余只做识别，见 `identify_only_note`）。
PARSED_CONTAINERS = (
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-matroska",
    "video/webm",
    "image/gif",
)

#: 只识别、不解析的容器 → 人话名。
_OTHER_CONTAINERS = {
    "video/mp2t": "MPEG-2 传输流（.ts，广播 / 监控机常见）",
    "video/x-flv": "Flash Video（.flv，老式录屏与直播留存）",
    "video/mpeg": "MPEG-1/2 节目流（.mpg）",
    "video/x-ms-wmv": "Windows Media Video（.wmv / .asf）",
    "application/ogg": "Ogg（Theora 视频 / Opus 音频）",
    "video/x-msvideo-unknown": "RIFF（不是 AVI 的 RIFF 容器）",
}

#: MJPEG 家族的 fourcc。AVI 的 `strh.fccHandler` 用这张表。
MJPEG_FOURCCS = frozenset({"MJPG", "mjpg", "MJPA", "mjpb", "jpeg", "JPEG", "dmb1", "AVRn"})

#: ISO BMFF 里「逐样本就是一张 JPEG」的样本格式。
JPEG_SAMPLE_FORMATS = frozenset({"jpeg", "mjpa", "mjpb"})

#: 编辑列表（`edts/elst`）里**空编辑**的标记：`media_time == -1` 表示这一段没有媒体。
#: ⚠️ 这个值**原样保留**，不折算成"秒" —— 折算之后它与"没有跳段"就再也分不开，
#: 而它恰恰是"把开头剪掉一段"最标准的写法。**唯一定义**在这里，
#: `forensics.check_edits` 从它 import，不自己再写一个字面量 -1。
EMPTY_EDIT = -1.0

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# 事实结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrackFacts:
    """一条轨道的**声明信息**（全部照抄容器头，没有一条是推断的）。

    ⚠️ 「声明」这两个字是本层最要紧的限定：一条被改过头的视频，它的 `stsd`
    里写着 30 fps 而实际是 25 fps —— 那正是 `timeline` 那一项要找的东西。
    所以这一层必须保持「只记录、不判断」，否则判据会被自己的记录污染。
    """

    handler: str = "other"          # video / sound / other
    codec: str = ""
    timescale: int | None = None
    duration_seconds: float | None = None
    sample_count: int | None = None
    #: `stsd` 里声明的**编码**尺寸（编码器输出多大）
    coded_width: int | None = None
    coded_height: int | None = None
    #: `tkhd` 声明的**展示**尺寸（播放时要显示多大）。两者不同是正常的
    #: （像素宽高比 / 旋转矩阵 / 裁切），**但差异过大就值得看一眼**。
    display_width: int | None = None
    display_height: int | None = None
    #: `tkhd` 声明的时长（秒）。它与 `mdhd` 时长、`mvhd` 时长三者**应当一致**。
    track_header_duration_seconds: float | None = None
    #: `stts` 的采样间隔统计（用于判断帧率是否恒定 / 有没有断层）
    delta_kinds: tuple[int, ...] = ()
    delta_sample_total: int | None = None
    delta_min: int | None = None
    delta_max: int | None = None
    #: `stsz` / `stco` 的聚合量（不存全表：`stco` 可以有上百万项）
    sample_size_constant: int | None = None
    sample_size_max: int | None = None
    chunk_count: int | None = None
    chunk_offset_min: int | None = None
    chunk_offset_max: int | None = None
    #: `edts/elst` 的条目：`(段时长秒, 媒体起点秒, 速率)`
    edit_list: tuple[tuple[float, float, float], ...] = ()
    #: Motion JPEG 的样本定位所需（只在真的能取帧时非空）
    framing: str = ""               # "bmff_mjpeg" / ""


@dataclass(frozen=True)
class VideoFacts:
    """容器层事实。**全部来自字节本身**，没有一条是推断出来的。"""

    media_type: str
    container: str
    actual_size: int
    #: 容器**声明**的总长（ISO BMFF 是最后一个盒的终点；RIFF 是 size + 8）。
    declared_size: int | None = None
    truncated: bool = False            # 声明长度 > 实际可用字节
    appended_bytes: int = 0            # 容器声明范围**之外**的多余字节

    brand: str | None = None
    compatible_brands: tuple[str, ...] = ()
    timescale: int | None = None
    duration_seconds: float | None = None
    creation_time: str | None = None
    modification_time: str | None = None

    tracks: tuple[TrackFacts, ...] = ()

    # -- 结构（ISO BMFF 专有；其它容器为 None / 0）--------------------------
    top_level_boxes: tuple[str, ...] = ()
    #: `moov` 是否在 `mdat` **之前**（true = 流式友好布局；false = 边写边录
    #: 或事后重新封装）。**这不是缺陷**，是「这个文件被写/被改过一遍」的线索。
    moov_before_mdat: bool | None = None
    mdat_count: int = 0
    mdat_ranges: tuple[tuple[int, int], ...] = ()
    free_bytes: int = 0
    fragmented: bool = False           # 有 moof / mfra
    has_mvex: bool = False

    # -- AVI 专有 -----------------------------------------------------------
    #: `avih.dwTotalFrames` 声明的总帧数（与 `movi` 里实际的块数是**两件事**）
    declared_frame_count: int | None = None
    #: `movi` 里实际的视频块数（数出来的）
    actual_frame_chunks: int | None = None
    has_index: bool | None = None      # 有没有 `idx1`

    # -- Matroska 专有 ------------------------------------------------------
    muxing_app: str | None = None
    writing_app: str | None = None
    cluster_count: int = 0

    #: `©too` / `ISFT` / `MuxingApp` 这类**自报**的编码工具字符串。
    #: ⚠️ 自报 = 改写它一个字节的成本是零 ⇒ 只当线索，不当证据。
    encoder_hint: str | None = None
    #: 容器内的其它文字标签（软件名等），人话形式
    tags: tuple[str, ...] = ()

    # -- 解析过程中值得如实记下的事实（不参与判定，但解释「为什么少看了东西」）--
    notes: tuple[str, ...] = ()
    #: 该容器我们**能解析到什么程度**
    parse_depth: str = "full"          # full / identify_only / unknown


@dataclass(frozen=True)
class ExtractedFrame:
    """从容器里**零解码**取出的一帧。`data` 是原始字节，未经任何重编码。"""

    index: int                     # 它在视频里的样本序号（封面帧为 -1）
    mime_type: str                 # image/jpeg 或 image/png
    data: bytes
    source: str                    # mjpeg_samples / cover_art / animated_gif


@dataclass(frozen=True)
class Decoded:
    """解析结果。`frames == ()` 时 `frames_reason` 必然非空。"""

    media_type: str
    byte_size: int
    facts: VideoFacts
    frames: tuple[ExtractedFrame, ...] = ()
    frames_reason: str | None = None
    frame_source: str = "none"
    window_note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 类型识别（按魔术字节，不信任扩展名与调用方声明）
# ---------------------------------------------------------------------------

def sniff_media_type(data: bytes) -> str:
    """按**魔术字节**判类型。⚠️ 与图像 / 音频侧同名函数同义：不信任扩展名。

    刻意把 `.mov` 与 `.mp4` 分开报（`video/quicktime` / `video/mp4`）：
    两者的 `ftyp.major_brand` 就不同，而「声明成 MP4、实际是 QuickTime」
    正是 `container` 那一项要抓的东西 —— 在这一层把它们合并掉，
    下游就再也分不出来了。
    """
    if len(data) < 16:
        return "application/octet-stream"

    if data[4:8] == b"ftyp":
        return "video/quicktime" if data[8:12] == b"qt  " else "video/mp4"

    if data[:4] == b"RIFF":
        return "video/x-msvideo" if data[8:12] == b"AVI " else "video/x-msvideo-unknown"

    if data[:4] == b"\x1aE\xdf\xa3":
        # EBML：DocType 决定是 WebM 还是 Matroska。取前 64 字节足够含 DocType。
        return "video/webm" if b"webm" in data[:64] else "video/x-matroska"

    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"

    if data[:4] == b"FLV\x01":
        return "video/x-flv"
    if data[:4] == b"\x00\x00\x01\xba":
        return "video/mpeg"
    if data[:8] == b"\x30\x26\xb2\x75\x8e\x66\xcf\x11":
        return "video/x-ms-wmv"
    if data[:4] == b"OggS":
        return "application/ogg"
    # MPEG-TS：同步字节 0x47 每 188 字节出现一次。判 3 处，避免误判。
    if len(data) > 564 and data[0] == 0x47 and data[188] == 0x47 and data[376] == 0x47:
        return "video/mp2t"

    return "application/octet-stream"


def frames_absent_clause(parse_depth: str) -> str:
    """「一帧都没取到」的那半句理由 —— **取决于这一次到底解析过没有**。

    ⚠️ 长原因（`frames_unavailable_text`）与报告里那句短说明原先**都**不分情形地写
    「容器里也没有逐帧存着的 JPEG/PNG」。对**只识别不解析**的容器（TS / FLV / WMV /
    Ogg …）那是把**没查过写成查过了**：本进程根本没往里看，说不出"有没有"。
    （与音频侧 `declared_size is None` 那次同族 —— 这是本仓最危险的一类缺陷：
    报告看起来很具体、读者无从怀疑，而那三项检查一次都没跑。）
    ⇒ 两处共用这一个定义，不许各写一句（"同一判定写两处必然漂移"）。
    """
    if parse_depth == "full":
        return "容器里也没有逐帧存着的 JPEG/PNG"
    return "本进程没有为它写解析器，因此**无法判断**里面有没有逐帧存着的 JPEG/PNG"


def frames_unavailable_text(media_type: str, parse_depth: str) -> str:
    """「取不到可分析帧」的人话原因。**必须写清后果**，不能只说「不支持」。

    `parse_depth` 是**必填**的（不给默认值）：默认值会变成"忘了传就拿到那句
    自信的结论"，而那正是本函数要防的事。
    """
    head = "本进程没有视频解码器（服务器上既没有 ffmpeg，也没有 OpenCV / PyAV），"
    if parse_depth == "full":
        why = (
            f"而这份字节（{media_type}）{frames_absent_clause(parse_depth)}"
            "（只有 MJPEG 逐帧编码与内嵌封面帧才会那样存）"
        )
        tail = "本报告因此只覆盖容器结构、时间线自洽与元数据。"
    else:
        why = f"而这份字节（{media_type}）{frames_absent_clause(parse_depth)}"
        tail = (
            "本报告因此**只有容器识别这一条结论**（见容器项），"
            "容器结构、时间线与帧层**都没有检查**。"
        )
    return (
        head + why + "，所以**本次一帧都没取到**。"
        "这不等于「没看出问题」，而是**帧层检查整项没有执行** —— "
        "而帧层才是能直接看到画面被改动过的那一层。" + tail
    )


def identify_only_note(media_type: str) -> str:
    """识别了容器但不做深解析时的说明（TS / FLV / WMV / Ogg）。"""
    name = _OTHER_CONTAINERS.get(media_type, media_type)
    return (
        f"已按魔术字节识别为 {name}，但本进程**没有为它写解析器** —— "
        "这类容器的结构不是盒树，逐个实现会把本模块撑成一个小型解码库，"
        "而收益只覆盖很小一部分输入。"
        "因此容器内部的盒结构、时间线与样本表**本次没有检查**。"
    )


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _u32(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 4], "big")


def _i32(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 4], "big", signed=True)


def _u64(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 8], "big")


def _tag(data: bytes, off: int, size: int = 4) -> str:
    return data[off:off + size].decode("latin-1", errors="replace").strip()


def _looks_like_box_type(btype: str) -> bool:
    """盒类型是 **4 个单字节字符、且不含控制字符**（ISO 4CC）。

    ⚠️ 这条判断不是洁癖，它决定了两条**结论**对不对：

      · 一段**零填充**的尾巴，原先会被当成一个 `size == 0` 的盒
        （"一直到文件尾"在规范里是合法的，但只对**最后一个**盒成立）而被**吞掉**
        —— 于是 `appended_bytes` 算出来是 0，`check_container` 给出
        「容器头部与内容自洽」这句**看起来是结论的假结论**。
        而尾部零填充恰恰是"手工拼接时忘了修头"的最常见形态。
      · 一段**随机字节**的尾巴，原先会被当成一个超大声明长度 ⇒ 报成
        「文件被截断、不完整」—— 那是**诊断反了**：文件没缺字节，而是多出来了。

    两种都指向同一个缺失的判断：**"这 4 个字节根本不是盒类型"**。

    ⚠️⚠️ **但判据不能收紧成「全是 ASCII 可打印字符」** —— 初版就是那么写的，
    而真实的 4CC 并不都在 ASCII 里：iTunes 风格元数据的盒名是
    `©too` / `©nam` / `©ART` / `©day` 一整个家族（首字节 **0xA9**），
    而 iPhone 相机、QuickTime、剪映导出的 MP4 **几乎必然含它们**。
    用 `isascii()` 的后果不是"读不到那几项"，而是**方向反了的假阳性**：
    解析在第一个 `©` 原子处提前收工 ⇒ 它后面的**全部**字节被算成
    「尾部多余数据」⇒ `check_container` 对一份**完全正常**的 iPhone 视频
    报「容器声明范围之外还有 N 字节额外数据」。假阳性比漏报更伤 ——
    它会教人学会忽略这一项，于是真线索也被一起忽略。

    ⚠️ 这里**刻意不**维护「已知盒名清单」：清单会漂（下一个新盒名就漏），
    而真正要挡的只有"这不是盒头"这一件事，判据只需覆盖**控制字符**。
    残余的模糊地带（一段每个字节都 ≥ 0x20 的随机尾巴仍会被当成盒）是
    已知的、且代价可控（`check_container` 至多说"截断"，不会说"没问题"）。
    """
    if len(btype) != 4:
        return False
    return all(
        ch == " " or 0x21 <= ord(ch) <= 0x7E or 0x80 <= ord(ch) <= 0xFF
        for ch in btype
    )


def _looks_like_frame(data: bytes) -> str | None:
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    return None


# ---------------------------------------------------------------------------
# ISO BMFF（MP4 / MOV / M4V / 3GP）—— 盒树
# ---------------------------------------------------------------------------
#
# 盒格式（ISO/IEC 14496-12）：size(u32) type(4B) [largesize(u64)] payload
# `size == 0` 表示「一直到文件结尾」，`size == 1` 表示后面跟着 64 位长度。
# 这两种写法不常见，但**恰恰是最容易被忽略的那两种** —— 忽略它们的后果
# 是把一个正常文件判成「结构损坏」，也就是一次假阳性。


@dataclass(frozen=True)
class _Box:
    type: str
    start: int      # 盒起点（含头）
    body: int       # 载荷起点
    end: int        # 盒终点（不含）


def _iter_boxes(
    data: bytes, start: int, end: int, *, notes: list[str] | None = None,
) -> Iterator[_Box]:
    """遍历同层的盒。

    遇到「声明长度超出可用字节」就**停止并把这一条也吐出去**（调用方据此判截断），
    而不是静默跳过 —— 静默跳过会让一个被截断的文件看起来「结构完整、只是少了几个盒」。
    """
    off = start
    seen = 0
    while off + 8 <= end:
        seen += 1
        if seen > MAX_BOXES:
            if notes is not None:
                notes.append(
                    f"同层的盒超过 {MAX_BOXES} 个，本层解析到此为止 —— "
                    "这本身是「这个容器不太常规」的一条线索。"
                )
            return
        size = _u32(data, off)
        btype = _tag(data, off + 4)
        if not _looks_like_box_type(btype):
            # 这里已经不是盒结构了 ⇒ 剩下的字节按「尾部多余数据」计。
            # 继续往下走会得出**方向相反**的结论（见 `_looks_like_box_type`）。
            if notes is not None:
                notes.append(
                    f"偏移 {off} 处的 4 字节不是合法的盒类型（{btype!r}）⇒ "
                    f"此处往后的 {end - off} 字节**不是盒结构**，按「尾部多余数据」计。"
                )
            return
        header = 8
        if size == 1:
            if off + 16 > end:
                if notes is not None:
                    notes.append(
                        f"盒 `{btype}`（偏移 {off}）声称是 64 位长度，"
                        "但剩余字节不足 16 —— 它的头部本身就是坏的。"
                    )
                return
            size = _u64(data, off + 8)
            header = 16
        elif size == 0:
            size = end - off

        if size < header:
            if notes is not None:
                notes.append(
                    f"偏移 {off} 处的盒 `{btype}` 声明长度为 {size} 字节，"
                    f"小于盒头自身（{header} 字节）—— 结构在这里就不成立了。"
                )
            return
        if off + size > end:
            if notes is not None:
                notes.append(
                    f"盒 `{btype}`（偏移 {off}）声明 {size} 字节，"
                    f"但到可用范围末尾只剩 {end - off} 字节 ⇒ **文件在这里被截断**。"
                )
            yield _Box(btype, off, off + header, end)
            return

        yield _Box(btype, off, off + header, off + size)
        off += size


def _tiles_exactly(kids: list[_Box], box: _Box) -> bool:
    """这串子盒是否**严丝合缝地铺满**了父容器（最后一个子盒的终点 = 父盒终点）。

    ⚠️ 这是「该不该跳过 FullBox 头那 4 字节」的**唯一判据**，而它必须是
    一个**结构性**判据、而不是一张盒名清单：
    跳过 4 字节会让整串子盒整体平移，于是最后一个子盒的终点正好落在父盒终点上；
    不跳则会**错位 4 字节**，读出来的第一个「盒」长度通常荒谬（见 `_children`），
    末尾几乎不可能恰好对齐。所以「谁铺满」就是「谁对」。
    """
    return bool(kids) and kids[-1].end == box.end


def _children(data: bytes, box: _Box, *, notes: list[str] | None = None) -> list[_Box]:
    """取一个容器盒的子盒。

    ⚠️ `_Box.body` 只跳过了 `size + type`（8 字节）—— 对 **FullBox 容器**
    还差 4 字节的 `version + flags`，真正的子盒从 `body + 4` 开始。

    `meta` 与 `ilst` 都是 FullBox 容器。原先只给 `meta` 写了特例，后果是
    **`ilst` 的子盒一个都读不到** —— 而 `ilst` 正是**内嵌封面帧（`covr`）**
    与**自报编码工具（`©too`）**的唯一入口。它不是「少一条线索」：
      · `with_cover` 样本实测 `facts.tags == ()`、封面帧 0 张；
      · 而 `check_metadata` 会因此说「容器自报的编码工具里没有命中常见剪辑软件」
        —— 一句**看起来正常的正面结论**，说的却是「我什么都没读到」。
    这与 `_find` 那次缺陷**同形**：能力整项死掉，而所有断言都绿。

    ⚠️ 为什么不做成「盒名清单 + `if name in {meta, ilst}`」：清单会漂
    （下一个 FullBox 容器加进来时没人会想起这里），而判据本来就存在 ——
    **哪一种偏移能把子盒铺满父容器**。`meta` 那一版「先试 0、空再试 4」
    也会在 `ilst` 上失效：`ilst` 在偏移 0 处读到的是**非空但错位**的一串，
    于是"看有没有读到"这个条件不成立。
    """
    first = list(_iter_boxes(data, box.body, box.end))
    if _tiles_exactly(first, box):
        return first
    if box.body + 4 <= box.end:
        shifted = list(_iter_boxes(data, box.body + 4, box.end))
        if _tiles_exactly(shifted, box) or (not first and shifted):
            return shifted
    if first:
        return first
    # 两次都没能铺满 ⇒ 这个容器的子盒结构确实有问题，**这时才**把诊断记进 notes
    # （前两次刻意传 `notes=None`：它们里的失败是"探路"，不是"文件有问题"）。
    return list(_iter_boxes(data, box.body, box.end, notes=notes))


def _find(data: bytes, boxes: Iterable[_Box], *path: str,
          notes: list[str] | None = None) -> _Box | None:
    """按路径取第一个匹配的盒。

    **调用约定**：`boxes` 是**候选父容器**，`path` 是它**内部**的路径 ——
    也就是 `_find(data, [moov], "mvhd")` 找的是 `moov` 的**子盒** `mvhd`。

    ⚠️ 这里原先写成"在 `boxes` 自己这一层里找 `path[0]`"，而**全部 20 个调用点**
    都是"传一个 `moov` / `mdia` / `stbl` 进去、再问它是不是 `mvhd`" ——
    答案永远是"不是"。后果**不是报错**，而是：

        `moov` 里的一切都读不到 —— 时长、轨道、编码格式、样本表、
        封面帧、编码工具**全部缺席**；而外层的"有没有 `moov`、
        `mdat` 在不在前面、尾部有没有多余数据"照常工作。

    报告照样出得来，只是**每一份都是「无法判定」**。这正是本项目最怕的那类缺陷：
    **能力整项死掉，而所有断言都绿。**（2026-10-02 第一次用合成样本真正喂到
    BMFF 这条路时才暴露 —— 此前所有冒烟测试都只走到"取不到帧"就返回了。）

    修正后的写法对**既有 20 个调用点零改动**：每个调用点本来就把父容器传进来了。
    """
    current: list[_Box] = list(boxes)
    found: _Box | None = None
    for name in path:
        found = None
        for parent in current:
            found = next(
                (b for b in _children(data, parent, notes=notes) if b.type == name),
                None,
            )
            if found is not None:
                break
        if found is None:
            return None
        current = [found]
    return found


def _version_flags(data: bytes, box: _Box) -> tuple[int, int]:
    if box.body + 4 > box.end:
        return 0, 0
    return data[box.body], _u32(data, box.body) & 0x00FFFFFF


#: ISO BMFF 纪元是 1904-01-01（不是 1970）。差 2082844800 秒。
_BMFF_EPOCH_OFFSET = 2_082_844_800


def _bmff_time(raw: int) -> str | None:
    """`mvhd` 时间戳 → ISO 8601 字符串（UTC）。

    ⚠️ 这个换算必须显式做：直接把它当 Unix 时间会得到 1904 年附近的怪值，
    而「创建时间比文件本身早 66 年」看起来像一条线索，其实只是我们算错了。
    """
    if raw <= _BMFF_EPOCH_OFFSET:
        return None
    import datetime as _dt

    try:
        stamp = _dt.datetime.fromtimestamp(
            raw - _BMFF_EPOCH_OFFSET, tz=_dt.timezone.utc
        )
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_mvhd(data: bytes, box: _Box) -> dict[str, Any]:
    version, _ = _version_flags(data, box)
    off = box.body + 4
    if version == 1:
        created, modified = _u64(data, off), _u64(data, off + 8)
        timescale = _u32(data, off + 16)
        duration = _u64(data, off + 20)
    else:
        created, modified = _u32(data, off), _u32(data, off + 4)
        timescale = _u32(data, off + 8)
        duration = _u32(data, off + 12)
    return {
        "timescale": timescale or None,
        "duration": duration,
        "duration_seconds": (duration / timescale) if timescale else None,
        "creation_time": _bmff_time(created),
        "modification_time": _bmff_time(modified),
    }


def _parse_tkhd(data: bytes, box: _Box, mvhd_timescale: int | None) -> dict[str, Any]:
    version, flags = _version_flags(data, box)
    off = box.body + 4
    # ⚠️ v0 的 `duration` 在载荷偏移 **16**，不是 12 ——
    #    v0 布局：creation(0) modification(4) track_ID(8) **reserved(12)** duration(16)。
    #    这里原先读的是 `off + 12`，也就是把 **`reserved`** 当成了时长。
    #    后果不是"读不到"，而是读到一个**看起来很正常的具体数字**：
    #    规范要求 `reserved` 写 0 ⇒ 时长恒为 0.000 秒 ⇒
    #    `check_timeline` 会对**每一个正常 MP4** 报「轨道时长与总时长不符」。
    #    假阳性比漏报更糟：它让人学会忽略这一项，于是真线索也被一起忽略。
    #    （v1 的 `off + 24` 是对的：creation(0) modification(8) track_ID(16)
    #      reserved(20) duration(24)。）
    duration = _u64(data, off + 24) if version == 1 else _u32(data, off + 16)
    # 展示宽高是 `tkhd` 的**最后 8 字节**（16.16 定点）。按「从尾部数」取，
    # 就不必去数中间那 36 字节的变换矩阵 —— 数错一位的代价是一个假线索。
    width = height = None
    if box.end - 8 >= box.body:
        raw_w = _u32(data, box.end - 8)
        raw_h = _u32(data, box.end - 4)
        width = round(raw_w / 65536.0, 3) or None
        height = round(raw_h / 65536.0, 3) or None
    return {
        "duration": duration,
        "duration_seconds": (duration / mvhd_timescale) if mvhd_timescale else None,
        "enabled": bool(flags & 0x1),
        "display_width": width,
        "display_height": height,
    }


def _parse_mdhd(data: bytes, box: _Box) -> dict[str, Any]:
    version, _ = _version_flags(data, box)
    off = box.body + 4
    if version == 1:
        timescale = _u32(data, off + 16)
        duration = _u64(data, off + 20)
    else:
        timescale = _u32(data, off + 8)
        duration = _u32(data, off + 12)
    return {
        "timescale": timescale or None,
        "duration": duration,
        "duration_seconds": (duration / timescale) if timescale else None,
    }


def _parse_hdlr(data: bytes, box: _Box) -> str:
    off = box.body + 4
    if off + 8 > box.end:
        return "other"
    raw = _tag(data, off + 4)
    return {"vide": "video", "soun": "sound"}.get(raw, "other")


def _parse_stsd(data: bytes, box: _Box) -> tuple[str, int | None, int | None]:
    """取第一个样本条目的 `(格式 fourcc, 编码宽, 编码高)`。

    视频样本条目的宽高在载荷偏移 32 / 34（8 字节 SampleEntry 头
    + 24 字节 VisualSampleEntry 前段），这是按 14496-12 的字段顺序数出来的。
    """
    off = box.body + 4
    if off + 4 > box.end:
        return "", None, None
    first = off + 4
    if first + 8 > box.end:
        return "", None, None
    codec = _tag(data, first + 4)
    width = height = None
    if first + 36 <= box.end:
        width = int.from_bytes(data[first + 32:first + 34], "big") or None
        height = int.from_bytes(data[first + 34:first + 36], "big") or None
    return codec, width, height


def _parse_stts(data: bytes, box: _Box) -> dict[str, Any]:
    off = box.body + 4
    if off + 4 > box.end:
        return {}
    count = _u32(data, off)
    off += 4
    total = 0
    kinds: list[int] = []
    lo: int | None = None
    hi: int | None = None
    for _ in range(min(count, 100_000)):
        if off + 8 > box.end:
            break
        n = _u32(data, off)
        delta = _u32(data, off + 4)
        off += 8
        total += n
        lo = delta if lo is None else min(lo, delta)
        hi = delta if hi is None else max(hi, delta)
        if delta not in kinds and len(kinds) < MAX_DELTA_KINDS:
            kinds.append(delta)
    return {
        "delta_kinds": tuple(kinds),
        "delta_sample_total": total,
        "delta_min": lo,
        "delta_max": hi,
    }


def _parse_stsz(data: bytes, box: _Box) -> dict[str, Any]:
    off = box.body + 4
    if off + 8 > box.end:
        return {}
    constant = _u32(data, off)
    count = _u32(data, off + 4)
    if constant:
        return {"sample_size_constant": constant, "sample_size_max": constant,
                "sample_count": count, "sample_sizes": ()}
    off += 8
    biggest = 0
    sizes: list[int] = []
    for i in range(min(count, 200_000)):
        if off + 4 > box.end:
            break
        value = _u32(data, off)
        biggest = max(biggest, value)
        if i < MAX_FRAMES * 64:
            sizes.append(value)
        off += 4
    return {
        "sample_size_constant": None,
        "sample_size_max": biggest or None,
        "sample_count": count,
        "sample_sizes": tuple(sizes),
    }


def _parse_chunk_offsets(data: bytes, box: _Box) -> dict[str, Any]:
    """`stco`（32 位）与 `co64`（64 位）共用。**只留 min/max/count**。

    ⚠️ 不存全表：一个两小时的 4K 视频 `stco` 会有上百万项，存下来就是几十 MB
    常驻内存 —— 而我们真正要判的只有一件事：「这些偏移落不落在 `mdat` 范围里」
    （越界 = 样本表被动过）。一个聚合量就够，而且是**有界**的。
    """
    off = box.body + 4
    if off + 4 > box.end:
        return {}
    count = _u32(data, off)
    off += 4
    wide = box.type == "co64"
    step = 8 if wide else 4
    lo: int | None = None
    hi: int | None = None
    scan = 0
    for _ in range(min(count, MAX_CHUNK_SCAN)):
        if off + step > box.end:
            break
        value = _u64(data, off) if wide else _u32(data, off)
        lo = value if lo is None else min(lo, value)
        hi = value if hi is None else max(hi, value)
        off += step
        scan += 1
    return {
        "chunk_count": count,
        "chunk_offsets_scanned": scan,
        "chunk_offset_min": lo,
        "chunk_offset_max": hi,
    }


def _parse_elst(
    data: bytes, box: _Box, timescale: int | None,
) -> tuple[tuple[float, float, float], ...]:
    """`edts/elst`：编辑列表 —— **「有意跳过 / 裁剪片段」的标准做法**。

    `(segment_duration, media_time, media_rate)`：

      · `media_time > 0` —— 「从媒体时间轴的某一刻开始播」，前面那段被跳过了；
      · `media_time == EMPTY_EDIT`（-1）—— **空编辑**：这一段整段没有媒体，
        是"把开头剪掉一块"最常见的写法；
      · `media_time == 0` —— 正常从头播。

    剪映 / QuickTime / iPhone 相机导出普遍会写它，所以它本身**不是**线索；
    它与「轨道时长对不上」同时出现才值得看一眼。
    """
    version, _ = _version_flags(data, box)
    off = box.body + 4
    if off + 4 > box.end:
        return ()
    count = _u32(data, off)
    off += 4
    entries: list[tuple[float, float, float]] = []
    for _ in range(min(count, 64)):
        if version == 1:
            if off + 20 > box.end:
                break
            seg = _u64(data, off)
            media = int.from_bytes(data[off + 8:off + 16], "big", signed=True)
            rate = _i32(data, off + 16) / 65536.0
            off += 20
        else:
            if off + 12 > box.end:
                break
            seg = _u32(data, off)
            media = _i32(data, off + 4)
            rate = _i32(data, off + 8) / 65536.0
            off += 12
        seg_seconds = (seg / timescale) if timescale else 0.0
        # ⚠️ `media_time == -1` 是**空编辑**的专用标记（"这段时间轴上没有媒体"），
        #    它恰恰是"把开头剪掉一段"**最标准**的写法。原先这里写成
        #    `if media > 0 else 0.0`，把它折算成了 0.0 ⇒ 与"没有跳段"再也分不开；
        #    而 `check_edits` 的干净分支还会据此说
        #    「要么没有 elst，要么它的 media_time 为 0」—— 一句**像结论的错话**。
        #    这里原样保留 `EMPTY_EDIT`：它**不是**一个时长，是一个标记。
        media_seconds = EMPTY_EDIT if media < 0 else (
            round(media / timescale, 4) if timescale else 0.0
        )
        entries.append((round(seg_seconds, 4), media_seconds, round(rate, 4)))
    return tuple(entries)


def _read_stbl(data: bytes, stbl: _Box, notes: list[str]) -> dict[str, Any]:
    """把一条轨道 `minf/stbl` 里的事实一次读出来。"""
    info: dict[str, Any] = {}
    names = (
        ("stsd", _parse_stsd, "codec"),
        ("stts", _parse_stts, None),
        ("stsz", _parse_stsz, None),
    )
    for name, parser, _ in names:
        box = _find(data, [stbl], name, notes=notes)
        if box is not None:
            if name == "stsd":
                codec, w, h = _parse_stsd(data, box)
                info["codec"] = codec
                info["coded_width"] = w
                info["coded_height"] = h
            else:
                info.update(parser(data, box))
    for name in ("stco", "co64"):
        box = _find(data, [stbl], name, notes=notes)
        if box is not None:
            info.update(_parse_chunk_offsets(data, box))
            break
    return info


def _cover_art(data: bytes, kids: list[_Box]) -> list[ExtractedFrame]:
    """从 `udta/meta/ilst` 取内嵌封面帧（`covr`）—— 常为导出工具写进去的首帧。"""
    frames: list[ExtractedFrame] = []
    for box in kids:
        if box.type != "covr":
            continue
        for data_box in _iter_boxes(data, box.body, box.end):
            if data_box.type != "data" or data_box.body + 8 > data_box.end:
                continue
            kind = _u32(data, data_box.body) & 0x00FFFFFF
            payload = data[data_box.body + 8:data_box.end]
            mime = {13: "image/jpeg", 14: "image/png"}.get(kind) or _looks_like_frame(payload)
            if mime and 0 < len(payload) <= MAX_FRAME_BYTES:
                frames.append(
                    ExtractedFrame(index=-1, mime_type=mime, data=payload,
                                   source="cover_art")
                )
    return frames


def _ilst_strings(kids: list[_Box], data: bytes) -> tuple[list[str], str | None]:
    """从 `ilst` 取文字项。`©too` = 编码工具（最有用的一条）。"""
    tags: list[str] = []
    encoder: str | None = None
    for box in kids:
        value = ""
        for data_box in _iter_boxes(data, box.body, box.end):
            if data_box.type != "data" or data_box.body + 8 > data_box.end:
                continue
            if (_u32(data, data_box.body) & 0x00FFFFFF) != 1:   # 1 = UTF-8 文本
                continue
            value = data[data_box.body + 8:data_box.end].decode(
                "utf-8", errors="replace"
            ).strip()
        if not value:
            continue
        if box.type == "\xa9too":
            encoder = value
        tags.append(f"{box.type!r}={value}")
    return tags, encoder


def _bmff_sample_offsets(
    data: bytes, stbl: _Box, *, want: list[int],
) -> list[tuple[int, int, int]]:
    """按 `stsc` / `stco` / `stsz` 定位样本，返回 `[(样本序号, 偏移, 长度)]`。

    只支持「样本大小恒定」与「有 stsz 逐样本表」两种情形；定位不到就返回空 ——
    **不猜**。返回空会被上层如实报成「没能取到帧」，而不是拿错位置去当帧。

    ⚠️ **样本序号必须随返回值一起回去**。初版只返回 `(偏移, 长度)`，上层用
    `zip(located, want)` 配序号 —— 只要中间有任何一个样本定位不到，
    它后面的所有序号就**整体错位一位**：报告会把「第 50 帧」写成「第 1 帧」。
    那是本项目最忌讳的那种错（结论看着正常、数字是错的），所以序号由
    本函数自己带出来，配对的正确性由构造保证。
    """
    stco = _find(data, [stbl], "stco") or _find(data, [stbl], "co64")
    stsc = _find(data, [stbl], "stsc")
    stsz = _find(data, [stbl], "stsz")
    if stco is None or stsc is None or stsz is None:
        return []

    off = stco.body + 4
    if off + 4 > stco.end:
        return []
    chunk_count = _u32(data, off)
    wide = stco.type == "co64"
    step = 8 if wide else 4
    offsets: list[int] = []
    p = off + 4
    for _ in range(min(chunk_count, MAX_CHUNK_SCAN)):
        if p + step > stco.end:
            break
        offsets.append(_u64(data, p) if wide else _u32(data, p))
        p += step

    p = stsc.body + 4
    if p + 4 > stsc.end:
        return []
    entry_count = _u32(data, p)
    p += 4
    entries: list[tuple[int, int]] = []
    for _ in range(min(entry_count, 4096)):
        if p + 12 > stsc.end:
            break
        entries.append((_u32(data, p), _u32(data, p + 4)))
        p += 12
    if not entries:
        return []

    size_const = _u32(data, stsz.body + 4) if stsz.body + 8 <= stsz.end else 0
    size_count = _u32(data, stsz.body + 8) if stsz.body + 8 <= stsz.end else 0
    size_table: list[int] = []
    if not size_const:
        q = stsz.body + 12
        for i in range(min(size_count, MAX_CHUNK_SCAN)):
            if q + 4 > stsz.end:
                break
            if i < max(want) + 16:
                size_table.append(_u32(data, q))
            q += 4

    result: list[tuple[int, int, int]] = []
    sample_index = 0
    for chunk_i in range(len(offsets)):
        per = entries[0][1]
        for first_chunk, samples_per_chunk in entries:
            if chunk_i + 1 >= first_chunk:
                per = samples_per_chunk
        offset = offsets[chunk_i]
        for _ in range(per):
            size = size_const or (
                size_table[sample_index] if sample_index < len(size_table) else 0
            )
            if sample_index in want and size:
                result.append((sample_index, offset, size))
            offset += size
            sample_index += 1
            if sample_index > max(want):
                break
        if sample_index > max(want):
            break
    return result


def _parse_bmff(data: bytes) -> tuple[VideoFacts, list[ExtractedFrame], str]:
    notes: list[str] = []
    top = list(_iter_boxes(data, 0, len(data), notes=notes))
    if not top:
        return (
            VideoFacts(
                media_type=sniff_media_type(data),
                container="未能解析",
                actual_size=len(data),
                notes=tuple(notes) or ("第一个盒的头部就不成立，无法开始解析。",),
                parse_depth="unknown",
            ),
            [],
            "none",
        )

    brand = None
    compatible: tuple[str, ...] = ()
    ftyp = next((b for b in top if b.type == "ftyp"), None)
    if ftyp is not None and ftyp.body + 8 <= ftyp.end:
        brand = _tag(data, ftyp.body)
        body = data[ftyp.body + 8:ftyp.end]
        compatible = tuple(
            _tag(body, i) for i in range(0, max(0, len(body) - 3), 4)
        )

    moov = next((b for b in top if b.type == "moov"), None)
    mdat_boxes = [b for b in top if b.type == "mdat"]
    free_bytes = sum(b.end - b.start for b in top if b.type in ("free", "skip", "wide"))
    fragmented = any(b.type in ("moof", "mfra") for b in top)
    declared_size = top[-1].end
    appended = max(0, len(data) - declared_size)

    mvhd_info: dict[str, Any] = {}
    timescale: int | None = None
    tracks: list[TrackFacts] = []
    tags: list[str] = []
    encoder: str | None = None
    has_mvex = False
    covers: list[ExtractedFrame] = []
    framing = ""

    if moov is not None:
        mvhd = _find(data, [moov], "mvhd", notes=notes)
        if mvhd is not None:
            mvhd_info = _parse_mvhd(data, mvhd)
            timescale = mvhd_info.get("timescale")
        has_mvex = _find(data, [moov], "mvex", notes=notes) is not None

        for box in _children(data, moov, notes=notes):
            if box.type != "trak":
                continue
            mdia = _find(data, [box], "mdia", notes=notes)
            if mdia is None:
                continue
            mdhd = _find(data, [mdia], "mdhd", notes=notes)
            hdlr = _find(data, [mdia], "hdlr", notes=notes)
            stbl = _find(data, [mdia], "minf", "stbl", notes=notes)
            tkhd = _find(data, [box], "tkhd", notes=notes)

            mdhd_info = _parse_mdhd(data, mdhd) if mdhd is not None else {}
            stbl_info = _read_stbl(data, stbl, notes) if stbl is not None else {}
            tkhd_info = _parse_tkhd(data, tkhd, timescale) if tkhd is not None else {}
            codec = stbl_info.get("codec", "")

            tracks.append(
                TrackFacts(
                    handler=_parse_hdlr(data, hdlr) if hdlr is not None else "other",
                    codec=codec,
                    timescale=mdhd_info.get("timescale"),
                    duration_seconds=mdhd_info.get("duration_seconds"),
                    sample_count=stbl_info.get("sample_count"),
                    coded_width=stbl_info.get("coded_width"),
                    coded_height=stbl_info.get("coded_height"),
                    display_width=tkhd_info.get("display_width"),
                    display_height=tkhd_info.get("display_height"),
                    track_header_duration_seconds=tkhd_info.get("duration_seconds"),
                    delta_kinds=stbl_info.get("delta_kinds", ()),
                    delta_sample_total=stbl_info.get("delta_sample_total"),
                    delta_min=stbl_info.get("delta_min"),
                    delta_max=stbl_info.get("delta_max"),
                    sample_size_constant=stbl_info.get("sample_size_constant"),
                    sample_size_max=stbl_info.get("sample_size_max"),
                    chunk_count=stbl_info.get("chunk_count"),
                    chunk_offset_min=stbl_info.get("chunk_offset_min"),
                    chunk_offset_max=stbl_info.get("chunk_offset_max"),
                    edit_list=(
                        _parse_elst(data, elst, mdhd_info.get("timescale"))
                        if (elst := _find(data, [box], "edts", "elst", notes=notes))
                        else ()
                    ),
                    framing="bmff_mjpeg" if codec in JPEG_SAMPLE_FORMATS else "",
                )
            )

        udta = _find(data, [moov], "udta", notes=notes)
        if udta is not None:
            ilst = _find(data, [udta], "meta", "ilst", notes=notes)
            if ilst is not None:
                kids = _children(data, ilst, notes=notes)
                covers = _cover_art(data, kids)
                tags, encoder = _ilst_strings(kids, data)

    frames: list[ExtractedFrame] = []
    mjpeg_tracks = [t for t in tracks if t.framing == "bmff_mjpeg"]
    if mjpeg_tracks and moov is not None:
        frames = _bmff_frames(data, moov, tracks)
        framing = "mjpeg_samples"
    if not frames and covers:
        frames = covers[:MAX_FRAMES]
        framing = "cover_art"

    facts = VideoFacts(
        media_type="video/quicktime" if brand == "qt" else "video/mp4",
        container=(
            f"ISO BMFF（{'QuickTime' if brand == 'qt' else 'MP4 家族'}）"
            + (f"，major_brand={brand}" if brand else "")
        ),
        actual_size=len(data),
        declared_size=declared_size,
        appended_bytes=appended,
        brand=brand,
        compatible_brands=compatible,
        timescale=timescale,
        duration_seconds=mvhd_info.get("duration_seconds"),
        creation_time=mvhd_info.get("creation_time"),
        modification_time=mvhd_info.get("modification_time"),
        tracks=tuple(tracks),
        top_level_boxes=tuple(b.type for b in top),
        moov_before_mdat=(
            None if (moov is None or not mdat_boxes)
            else moov.start < min(b.start for b in mdat_boxes)
        ),
        mdat_count=len(mdat_boxes),
        mdat_ranges=tuple((b.body, b.end) for b in mdat_boxes),
        free_bytes=free_bytes,
        fragmented=fragmented,
        has_mvex=has_mvex,
        encoder_hint=encoder,
        tags=tuple(tags),
        notes=tuple(notes),
        parse_depth="full",
    )
    return facts, frames, framing


def _bmff_frames(data: bytes, moov: _Box, tracks: list[TrackFacts]) -> list[ExtractedFrame]:
    """为 MJPEG 的 MP4/MOV 取首 / 中 / 末三帧（真的按样本表定位）。"""
    out: list[ExtractedFrame] = []
    for box in _children(data, moov):
        if box.type != "trak":
            continue
        mdia = _find(data, [box], "mdia")
        if mdia is None:
            continue
        hdlr = _find(data, [mdia], "hdlr")
        if hdlr is None or _parse_hdlr(data, hdlr) != "video":
            continue
        stbl = _find(data, [mdia], "minf", "stbl")
        if stbl is None:
            continue
        stsd = _find(data, [stbl], "stsd")
        if stsd is None or _parse_stsd(data, stsd)[0] not in JPEG_SAMPLE_FORMATS:
            continue
        count_box = _find(data, [stbl], "stsz")
        if count_box is None or count_box.body + 12 > count_box.end:
            continue
        # ⚠️ `stsz` 是 FullBox，载荷布局是
        #      version+flags(body..body+4) · sample_size(body+4) · sample_count(body+8)
        #    原先读的是 `body + 4` ⇒ 拿到的是 **`sample_size`**（"所有样本同长"时
        #    它才非零，逐样本表时恒为 0）⇒ `count <= 0` ⇒ 直接 `continue` ⇒
        #    **MJPEG 的 MP4/MOV 一帧都取不到**（而 GIF / 封面帧两条路是好的，
        #    所以现象看起来只像"这类文件没有帧"）。报告给的还是一句**错的原因**：
        #    「容器里没有逐帧存着的 JPEG/PNG」—— 其实里面每一帧都是完整 JPEG。
        #    `_parse_stsz` 与 `_bmff_sample_offsets` 两处用的都是 `+8`，只有这里错。
        count = _u32(data, count_box.body + 8)
        if count <= 0:
            continue
        want = sorted({0, max(0, count // 2), count - 1} | ({1} if count > 3 else set()))
        want = want[:MAX_FRAMES]
        located = _bmff_sample_offsets(data, stbl, want=want)
        for index, offset, size in located:
            if offset + size > len(data) or size <= 0:
                continue
            payload = data[offset:offset + size]
            mime = _looks_like_frame(payload)
            if mime is None or len(payload) > MAX_FRAME_BYTES:
                continue
            out.append(
                ExtractedFrame(index=index, mime_type=mime, data=payload,
                               source="mjpeg_samples")
            )
        if out:
            break
    del tracks
    return out


# ---------------------------------------------------------------------------
# RIFF / AVI
# ---------------------------------------------------------------------------

def _iter_riff(data: bytes, start: int, end: int) -> Iterator[tuple[str, int, int, int]]:
    """扫 RIFF chunk 链，产出 `(id, 声明长度, 数据起点, 实际可用长度)`。

    走到「声明长度超出文件」就把这一条也吐出去（调用方据此判截断）。
    """
    off = start
    while off + 8 <= end:
        cid = _tag(data, off)
        size = _u32(data, off + 4)
        body = off + 8
        avail = max(0, min(size, end - body))
        yield cid, size, body, avail
        if body + size > end:
            return
        off = body + size + (size & 1)          # RIFF 块按偶数字节对齐


def _parse_avi(data: bytes) -> tuple[VideoFacts, list[ExtractedFrame], str]:
    notes: list[str] = []
    facts_declared = _u32(data, 4) + 8 if len(data) >= 8 else None
    appended = max(0, len(data) - facts_declared) if facts_declared else 0
    truncated = bool(facts_declared and len(data) < facts_declared)

    declared_frames: int | None = None
    width = height = None
    video_codec = ""
    cfg_handler = ""
    declared_frame_count_avih = None
    has_index: bool | None = None
    encoder: str | None = None
    tags: list[str] = []
    audio_tracks = 0
    total_frames_declared = None

    for cid, _size, body, avail in _iter_riff(data, 12, len(data)):
        if cid == "LIST" and avail >= 4:
            kind = _tag(data, body)
            if kind == "hdrl":
                for sid, _s, sbody, savail in _iter_riff(data, body + 4, body + avail):
                    if sid == "avih" and savail >= 24:
                        declared_frame_count_avih = _u32(data, sbody + 16)
                        width = _u32(data, sbody + 32) or None
                        height = _u32(data, sbody + 36) or None
                    elif sid == "LIST" and savail >= 4 and _tag(data, sbody) == "strl":
                        kind2 = ""
                        handler = ""
                        for tid, _t, tbody, tavail in _iter_riff(
                            data, sbody + 4, sbody + savail
                        ):
                            if tid == "strh" and tavail >= 8:
                                kind2 = _tag(data, tbody)
                                handler = _tag(data, tbody + 4)
                            elif tid == "strf" and tavail >= 8 and kind2 == "vids":
                                video_codec = _tag(data, tbody + 16) or ""
                        if kind2 == "vids":
                            cfg_handler = handler
                        elif kind2 == "auds":
                            audio_tracks += 1
            elif kind == "movi":
                count = 0
                for mid, _m, mbody, mavail in _iter_riff(data, body + 4, body + avail):
                    if mid[2:] in ("dc", "db") and mavail > 0:
                        count += 1
                declared_frames = count
        elif cid == "idx1":
            has_index = True
        elif cid in ("INFO",):
            for iid, _i, ibody, iavail in _iter_riff(data, body, body + avail):
                if iid == "ISFT" and iavail:
                    encoder = data[ibody:ibody + iavail].decode(
                        "latin-1", errors="replace"
                    ).strip("\x00").strip()
                    tags.append(f"ISFT={encoder}")

    # 取帧：只有 MJPEG 家族的 AVI 才谈得上「零解码取帧」
    frames: list[ExtractedFrame] = []
    framing = ""
    if cfg_handler in MJPEG_FOURCCS or video_codec in MJPEG_FOURCCS:
        frames = _avi_frames(data)
        framing = "mjpeg_samples" if frames else ""

    facts = VideoFacts(
        media_type="video/x-msvideo",
        container=f"RIFF AVI（视频编码 {video_codec or cfg_handler or '未知'}）",
        actual_size=len(data),
        declared_size=facts_declared,
        truncated=truncated,
        appended_bytes=appended,
        duration_seconds=None,
        tracks=tuple(
            [TrackFacts(handler="video", codec=video_codec or cfg_handler,
                        coded_width=width, coded_height=height,
                        sample_count=declared_frames)]
            + [TrackFacts(handler="sound", codec="") for _ in range(audio_tracks)]
        ),
        declared_frame_count=declared_frame_count_avih,
        actual_frame_chunks=declared_frames,
        has_index=has_index,
        encoder_hint=encoder,
        tags=tuple(tags),
        notes=tuple(notes),
        parse_depth="full",
    )
    return facts, frames, framing


def _avi_frames(data: bytes) -> list[ExtractedFrame]:
    """从 AVI 的 `movi` 里取首 / 中 / 末三帧。**每个视频块本来就是一张 JPEG。**"""
    chunks: list[tuple[int, bytes]] = []
    for cid, _size, body, avail in _iter_riff(data, 12, len(data)):
        if cid != "LIST" or avail < 4 or _tag(data, body) != "movi":
            continue
        for mid, _m, mbody, mavail in _iter_riff(data, body + 4, body + avail):
            if mid[2:] not in ("dc", "db") or mavail <= 0:
                continue
            payload = data[mbody:mbody + mavail]
            if _looks_like_frame(payload) and len(payload) <= MAX_FRAME_BYTES:
                chunks.append((len(chunks), payload))
        break

    if not chunks:
        return []
    picks = sorted({0, len(chunks) // 2, len(chunks) - 1, 1 if len(chunks) > 3 else 0})
    picks = [i for i in picks if 0 <= i < len(chunks)][:MAX_FRAMES]
    return [
        ExtractedFrame(
            index=chunks[i][0],
            mime_type=_looks_like_frame(chunks[i][1]) or "image/jpeg",
            data=chunks[i][1],
            source="mjpeg_samples",
        )
        for i in picks
    ]


# ---------------------------------------------------------------------------
# Matroska / WebM（EBML）
# ---------------------------------------------------------------------------
#
# EBML 的元素头是「可变长 ID + 可变长长度」（VINT）。这里只实现到能读
# Info / Tracks / Cluster 这一层 —— 够回答「时长声明是多少、有几条轨、
# 编码是什么、有几个簇」，而这几件事正是时间线自洽检查需要的。

_EBML_IDS = {
    0x1A45DFA3: "EBML",
    0x18538067: "Segment",
    0x1549A966: "Info",
    0x1654AE6B: "Tracks",
    0x1F43B675: "Cluster",
    0x1C53BB6B: "Cues",
    0x114D9B74: "SeekHead",
    0xEC: "Void",
    0xBF: "CRC-32",
    0xE7: "Timecode",
    0x2AD7B1: "TimecodeScale",
    0x4489: "Duration",
    0x4D80: "MuxingApp",
    0x5741: "WritingApp",
    0x7BA9: "Title",
    0xAE: "TrackEntry",
    0xD7: "TrackNumber",
    0x83: "TrackType",
    0x86: "CodecID",
    0x23E383: "DefaultDuration",
    0xE0: "Video",
    0xB0: "PixelWidth",
    0xBA: "PixelHeight",
}

#: EBML 里的**主元素**（Master element）—— 它们的载荷还是元素，要往下走。
_EBML_MASTERS = frozenset(
    {"EBML", "Segment", "Info", "Tracks", "Cluster", "Cues", "SeekHead",
     "TrackEntry", "Video"}
)


def _ebml_vint(data: bytes, off: int, end: int, *, keep_marker: bool) -> tuple[int, int] | None:
    """读一个 VINT，返回 `(值, 消耗字节数)`；读不出返回 None。"""
    if off >= end:
        return None
    first = data[off]
    if first == 0:
        return None
    length = 8 - first.bit_length() + 1
    if off + length > end:
        return None
    value = first if keep_marker else (first & ((1 << (8 - length)) - 1))
    for i in range(1, length):
        value = (value << 8) | data[off + i]
    return value, length


def _parse_ebml(
    data: bytes, start: int, end: int, want: set[str], out: dict[str, Any],
    *, depth: int = 0,
) -> None:
    """递归扫 EBML，把 `want` 里那些元素的值记进 `out`。

    ⚠️ 深度受限（8 层）：EBML 是**可嵌套的通用容器**，一个畸形的文件
    可以让「元素里套元素」无限下去，而递归没有上限就是一次栈溢出 ——
    那种崩溃发生在常驻服务里，表现是「这个角色时不时挂一次」。
    """
    if depth > 8:
        return
    off = start
    while off < end:
        ident = _ebml_vint(data, off, end, keep_marker=True)
        if ident is None:
            return
        elem_id, id_len = ident
        size_info = _ebml_vint(data, off + id_len, end, keep_marker=False)
        if size_info is None:
            return
        size = size_info[0]
        body = off + id_len + size_info[1]
        if body + size > end:
            # 未知长度（流式写入的 Segment 会用全 1）⇒ 当作「到末尾」
            size = end - body
        name = _EBML_IDS.get(elem_id)
        if name is None:
            off = body + size
            continue
        if name == "Cluster":
            out["cluster_count"] = int(out.get("cluster_count") or 0) + 1
        if name in want and name not in _EBML_MASTERS:
            raw = data[body:body + size]
            if name == "Duration":
                out[name] = _ebml_float(raw)
            elif name in ("MuxingApp", "WritingApp", "Title"):
                out[name] = raw.decode("utf-8", errors="replace").strip()
            else:
                out[name] = int.from_bytes(raw, "big") if raw else None
        if name in _EBML_MASTERS:
            _parse_ebml(data, body, body + size, want, out, depth=depth + 1)
        off = body + size


def _ebml_float(raw: bytes) -> float | None:
    import struct as _struct

    try:
        if len(raw) == 4:
            return float(_struct.unpack(">f", raw)[0])
        if len(raw) == 8:
            return float(_struct.unpack(">d", raw)[0])
    except _struct.error:
        return None
    return None


def _parse_matroska(data: bytes, media_type: str) -> tuple[VideoFacts, list[ExtractedFrame], str]:
    out: dict[str, Any] = {}
    notes: list[str] = []
    try:
        _parse_ebml(
            data, 0, len(data),
            {"TimecodeScale", "Duration", "MuxingApp", "WritingApp", "Title",
             "TrackType", "CodecID", "PixelWidth", "PixelHeight"},
            out,
        )
    except Exception as exc:  # noqa: BLE001 - EBML 是通用容器，畸形输入不该炸掉上层
        notes.append(f"EBML 解析在某个元素处停下：{stable_reason(exc)}")

    scale = int(out.get("TimecodeScale") or 1_000_000)
    duration_raw = out.get("Duration")
    duration = (duration_raw * scale / 1e9) if duration_raw else None
    codec = out.get("CodecID")
    notes.append(
        "EBML 是通用容器：本次只读到 Info / Tracks 层的字段，"
        "**没有解析每个簇里的块结构**（那需要一个完整的 Matroska 解析器）。"
    )

    facts = VideoFacts(
        media_type=media_type,
        container=(
            "WebM（Matroska 的 Web 子集）" if media_type == "video/webm"
            else "Matroska（.mkv）"
        ),
        actual_size=len(data),
        declared_size=len(data),      # EBML 没有「总长度」字段，按实际长度算
        duration_seconds=duration,
        tracks=tuple(
            [TrackFacts(handler="video", codec=str(codec or ""),
                        coded_width=out.get("PixelWidth"),
                        coded_height=out.get("PixelHeight"))]
        ) if codec else (),
        muxing_app=out.get("MuxingApp"),
        writing_app=out.get("WritingApp"),
        cluster_count=int(out.get("cluster_count") or 0),
        encoder_hint=str(out.get("WritingApp") or out.get("MuxingApp") or "") or None,
        tags=tuple(
            f"{k}={out[k]}" for k in ("MuxingApp", "WritingApp", "Title") if out.get(k)
        ),
        notes=tuple(notes),
        parse_depth="full",
    )
    return facts, [], "none"


# ---------------------------------------------------------------------------
# GIF（动画 GIF 的块结构；逐帧光栅化在 frames.py）
# ---------------------------------------------------------------------------

def _parse_gif(data: bytes) -> tuple[VideoFacts, list[ExtractedFrame], str]:
    notes: list[str] = []
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    version = data[3:6].decode("latin-1")
    packed = data[10]

    # ⚠️ **必须跳过全局色表（GCT）**，否则第一个被读到的"块标识"会是色表的第一字节。
    #    这里原先直接 `off = 13`（= 签名 6 + 逻辑屏描述符 7）就开始读块，
    #    于是**任何带 GCT 的 GIF**（也就是几乎所有真实 GIF）都在第一字节上翻车：
    #    报「出现了未在规范里的块标识 0x0」，帧数 0、时长未知。
    #    后果不是"少一条线索"，而是**把一段 30 帧的动图写成「0 帧」** ——
    #    一个看起来很正常的具体数字，只是错的。
    #    （2026-10-02 用第一份**手工合成**的 GIF 喂进来时才暴露：
    #      此前所有测试路径都停在"取不到帧"就返回，从没走到块结构这一层。）
    gct_size = 3 * (2 ** ((packed & 0x07) + 1)) if packed & 0x80 else 0
    colors_note = (
        f"全局色表有 {2 ** ((packed & 0x07) + 1)} 色（{gct_size} 字节）。"
        if gct_size else "这份 GIF **没有**全局色表（每帧自带的局部色表）。"
    )

    frames = 0
    delays: list[int] = []
    loop: int | None = None
    has_gce = 0
    off = 13 + gct_size
    while off < len(data):
        block = data[off]
        if block == 0x3B:                       # trailer
            break
        if block == 0x21:                       # extension
            if off + 2 >= len(data):
                break
            label = data[off + 1]
            off += 2
            if label == 0xF9:                   # graphic control extension
                has_gce += 1
                if off + 6 <= len(data):
                    delays.append(int.from_bytes(data[off + 2:off + 4], "little"))
            elif label == 0xFF:                 # application extension
                if off < len(data):
                    size = data[off]
                    name = data[off + 1:off + 1 + size]
                    if name.startswith(b"NETSCAPE"):
                        p = off + 1 + size
                        if p + 3 <= len(data) and data[p] == 3:
                            loop = int.from_bytes(data[p + 2:p + 4], "little")
                    off += 1 + size
            # 跳过数据子块
            while off < len(data) and data[off]:
                off += data[off] + 1
            off += 1
        elif block == 0x2C:                     # image descriptor
            frames += 1
            if off + 10 > len(data):
                break
            off += 10
            if off < len(data) and data[off] & 0x80:    # local color table
                off += 3 * (2 ** ((data[off] & 0x07) + 1))
            off += 1                            # LZW minimum code size
            while off < len(data) and data[off]:
                off += data[off] + 1
            off += 1
        else:
            notes.append(
                f"偏移 {off} 处出现了未在 GIF89a 规范里的块标识 {block:#x}，"
                "后续块结构无法继续解析。"
            )
            break

    duration = (sum(delays) / 100.0) if delays else None
    notes.append(colors_note)
    notes.append(
        "GIF 的帧延迟是**每帧一个**（单位 1/100 秒），没有全局帧率概念；"
        "本报告里的时长是各帧延迟之和。"
    )
    facts = VideoFacts(
        media_type="image/gif",
        container=f"GIF（{version}，{'动画' if frames > 1 else '单帧'}）",
        actual_size=len(data),
        declared_size=len(data),
        duration_seconds=duration,
        tracks=tuple(
            [TrackFacts(handler="video", codec="gif",
                        coded_width=width, coded_height=height, sample_count=frames)]
        ),
        declared_frame_count=None,
        actual_frame_chunks=frames,
        encoder_hint=None,
        tags=(f"loop={loop}" if loop is not None else "",) if loop is not None else (),
        notes=tuple(notes),
        parse_depth="full",
    )
    return facts, [], "none"


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def decode_video(data: bytes) -> Decoded:
    """字节 → 容器事实 + 可分析帧。**不联网、不抛判定性异常。**

    认不出容器时返回 `media_type="application/octet-stream"`，
    由 `forensics.analyze()` 决定要不要拒绝（判据放在检测层，不散在解码层）。
    """
    media_type = sniff_media_type(data)

    if media_type == "video/mp4" or media_type == "video/quicktime":
        facts, frames, source = _parse_bmff(data)
    elif media_type == "video/x-msvideo":
        facts, frames, source = _parse_avi(data)
    elif media_type in ("video/x-matroska", "video/webm"):
        facts, frames, source = _parse_matroska(data, media_type)
    elif media_type == "image/gif":
        facts, frames, source = _parse_gif(data)
    elif media_type in _OTHER_CONTAINERS:
        facts = VideoFacts(
            media_type=media_type,
            container=_OTHER_CONTAINERS[media_type],
            actual_size=len(data),
            notes=(identify_only_note(media_type),),
            parse_depth="identify_only",
        )
        frames, source = [], "none"
    else:
        return Decoded(
            media_type=media_type,
            byte_size=len(data),
            facts=VideoFacts(
                media_type=media_type, container="未能识别",
                actual_size=len(data), parse_depth="unknown",
            ),
            frames_reason="按魔术字节判定，这份字节不是本项目能识别的视频容器。",
            frame_source="none",
        )

    note_extra = "；".join(facts.notes) if facts.notes else ""
    if frames:
        frames_reason = None
        window_note = (
            f"本次从容器里**零解码取出**了 {len(frames)} 帧"
            f"（{source}）用于像素域分析。"
            "⚠️ 这是**抽样**，不是逐帧 —— 报告里的结论只覆盖这几帧。"
        )
    else:
        frames_reason = frames_unavailable_text(media_type, facts.parse_depth)
        # ⚠️ 这里的半句理由与长原因**共用一个定义**（`frames_absent_clause`）：
        # 两处各写一句的话，改一处必漏另一处，而漏的那一处就是仍在说假话的那处。
        window_note = (
            f"本次没有取到可分析的帧（{frames_absent_clause(facts.parse_depth)}），"
            "因此**帧层检查整项没有执行**。"
        )
    if note_extra:
        window_note += f"（解析提示：{note_extra}）"

    return Decoded(
        media_type=media_type,
        byte_size=len(data),
        facts=facts,
        frames=tuple(frames),
        frames_reason=frames_reason,
        frame_source=source if frames else "none",
        window_note=window_note,
    )


__all__ = [
    "Decoded",
    "EMPTY_EDIT",
    "ExtractedFrame",
    "JPEG_SAMPLE_FORMATS",
    "MAX_FRAMES",
    "MAX_FRAME_BYTES",
    "MJPEG_FOURCCS",
    "PARSED_CONTAINERS",
    "TrackFacts",
    "VideoFacts",
    "decode_video",
    "frames_absent_clause",
    "frames_unavailable_text",
    "identify_only_note",
    "sniff_media_type",
]
