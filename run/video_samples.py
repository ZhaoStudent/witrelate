"""视频鉴伪的**合成样本库** —— 给 `tools/verify_video.py` 与 `run/meta_verify_video.py` 共用。

为什么要合成而不是拿真实视频：断言需要**知道正确答案**。

    "这份 MP4 的 `moov` 被放在 `mdat` 之后" 是我造出来的事实，
    所以"容器层应该在这一点上报异常"才有意义。
    拿一段真实视频，连它有没有被重写过都不知道，
    断言就退化成了"输出没变" —— 那是本项目最不想要的那种断言。

⚠️ 这些样本**不是**真实视频，也不能用来校准阈值 ——
    它们只用来验证"该抓的抓得到、不该抓的不抓"（行为断言）。

════════════════════════════════════════════════════════════════════════
为什么连容器都要自己拼（而不是用某个库导出）
════════════════════════════════════════════════════════════════════════

`decode.py` 判的是**盒树结构**：`moov` 的位置、`mdat` 的段数、样本表长度与
实际字节的吻合度、`elst` 跳段、尾部多余数据……这些**恰恰是正常导出工具不会产生**的
形态。要断言"它们会被抓出来"，就必须能精确地造出它们；
而能精确造出它们的唯一办法，是自己写字节。

本模块**不依赖任何第三方库**（`avc1` 轨道的样本是占位字节，
真正需要"能解码的画面"时用 `frame_payload` 传 JPEG —— 见 `mjpeg_mp4()`）。
"""

from __future__ import annotations

import io
from typing import Callable, Sequence

# ---------------------------------------------------------------------------
# 盒（box）原子
# ---------------------------------------------------------------------------

def box(type_: str, payload: bytes) -> bytes:
    """ISO/IEC 14496-12：`size(u32) type(4B) payload`。

    ⚠️ 4CC 是**单字节字段**，用 `latin-1` 编码 —— 不能用 `ascii`：
    iTunes 风格元数据的盒名恰恰不是 ASCII（编码工具是 `©too`，首字节 0xA9；
    还有 `©nam` / `©day` 等一整族）。用 `ascii` 会让这些构造器
    **在造样本时就抛 `UnicodeEncodeError`**，于是"自报编码工具"这条路径
    一次都没有被样本走过 —— 而它看起来只是"这条样本还没写"。
    """
    if len(type_) != 4:
        raise ValueError(f"盒类型必须是 4 个字符，收到 {type_!r}（{len(type_)} 个）")
    return (8 + len(payload)).to_bytes(4, "big") + type_.encode("latin-1") + payload


def full_box(type_: str, *, version: int = 0, flags: int = 0, payload: bytes = b"") -> bytes:
    """带 `version(1B) + flags(3B)` 的 FullBox。"""
    return box(type_, bytes([version]) + flags.to_bytes(3, "big") + payload)


#: 单位矩阵（`mvhd` / `tkhd` 都要，共 36 字节）—— 写错会被当成"旋转过"，没必要冒这个险
_UNITY_MATRIX = (
    b"\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x40\x00\x00\x00"
)


def mvhd(timescale: int, duration: int) -> bytes:
    """v0 的 `mvhd`：created/modified/timescale/duration + rate/volume/matrix/..."""
    payload = (
        (0).to_bytes(4, "big")            # creation_time（1904 纪元，0 = 未写）
        + (0).to_bytes(4, "big")          # modification_time
        + timescale.to_bytes(4, "big")
        + duration.to_bytes(4, "big")
        + (0x00010000).to_bytes(4, "big")  # rate = 1.0
        + (0x0100).to_bytes(2, "big")      # volume = 1.0
        + b"\x00\x00"                      # reserved
        + b"\x00" * 8                      # reserved
        + _UNITY_MATRIX
        + b"\x00" * 24                     # pre_defined
        + (2).to_bytes(4, "big")           # next_track_id
    )
    return full_box("mvhd", payload=payload)


def tkhd(track_id: int, duration: int, width: int, height: int) -> bytes:
    payload = (
        (0).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + track_id.to_bytes(4, "big")
        + b"\x00" * 4                      # reserved
        + duration.to_bytes(4, "big")
        + b"\x00" * 8                      # reserved
        + b"\x00\x00"                      # layer
        + b"\x00\x00"                      # alternate_group
        + b"\x00\x00"                      # volume（视频轨恒 0）
        + b"\x00\x00"                      # reserved
        + _UNITY_MATRIX
        + (width << 16).to_bytes(4, "big")   # 16.16 定点
        + (height << 16).to_bytes(4, "big")
    )
    return full_box("tkhd", flags=0x000003, payload=payload)


def mdhd(timescale: int, duration: int) -> bytes:
    payload = (
        (0).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + timescale.to_bytes(4, "big")
        + duration.to_bytes(4, "big")
        + (0x55C4).to_bytes(2, "big")      # language = 'und'
        + b"\x00\x00"
    )
    return full_box("mdhd", payload=payload)


def hdlr(handler: str = "vide") -> bytes:
    payload = (
        b"\x00" * 4
        + handler.encode("ascii")
        + b"\x00" * 12
        + b"VideoHandler\x00"
    )
    return full_box("hdlr", payload=payload)


def stsd_video(codec: str, width: int, height: int) -> bytes:
    """只放**一个** VisualSampleEntry；宽高必须落在偏移 32 / 34（见 `decode._parse_stsd`）。"""
    entry_payload = (
        b"\x00" * 6                        # reserved
        + (1).to_bytes(2, "big")           # data_reference_index
        + b"\x00" * 16                     # pre_defined / reserved
        + width.to_bytes(2, "big")         # ← 偏移 32
        + height.to_bytes(2, "big")        # ← 偏移 34
        + (0x00480000).to_bytes(4, "big")  # horizresolution = 72dpi
        + (0x00480000).to_bytes(4, "big")  # vertresolution
        + b"\x00" * 4                      # reserved
        + (1).to_bytes(2, "big")           # frame_count
        + b"\x00" * 32                     # compressorname
        + (0x0018).to_bytes(2, "big")      # depth = 24
        + (0xFFFF).to_bytes(2, "big")      # pre_defined = -1
    )
    entry = box(codec, entry_payload)
    return full_box("stsd", payload=(1).to_bytes(4, "big") + entry)


def stts(entries: Sequence[tuple[int, int]]) -> bytes:
    """`(样本数, 时长增量)` 序列。帧率跳变就靠这里写两组不同的 delta。"""
    payload = (len(entries)).to_bytes(4, "big") + b"".join(
        n.to_bytes(4, "big") + delta.to_bytes(4, "big") for n, delta in entries
    )
    return full_box("stts", payload=payload)


def stsz(sizes: Sequence[int]) -> bytes:
    payload = (0).to_bytes(4, "big") + (len(sizes)).to_bytes(4, "big") + b"".join(
        s.to_bytes(4, "big") for s in sizes
    )
    return full_box("stsz", payload=payload)


def stco(offsets: Sequence[int]) -> bytes:
    payload = (len(offsets)).to_bytes(4, "big") + b"".join(
        o.to_bytes(4, "big") for o in offsets
    )
    return full_box("stco", payload=payload)


def stsc(entries: Sequence[tuple[int, int, int]]) -> bytes:
    payload = (len(entries)).to_bytes(4, "big") + b"".join(
        a.to_bytes(4, "big") + b.to_bytes(4, "big") + c.to_bytes(4, "big")
        for a, b, c in entries
    )
    return full_box("stsc", payload=payload)


def elst(entries: Sequence[tuple[int, int]]) -> bytes:
    """`(segment_duration, media_time)`；`media_time = -1` 表示空编辑（常见的"静默跳段"）。"""
    payload = (len(entries)).to_bytes(4, "big") + b"".join(
        dur.to_bytes(4, "big") + (time & 0xFFFFFFFF).to_bytes(4, "big") + (0x00010000).to_bytes(4, "big")
        for dur, time in entries
    )
    return full_box("elst", payload=payload)


def covr_item(jpeg: bytes) -> bytes:
    """`ilst` 里的一项：`covr/data` —— 内嵌封面帧（type 13 = JPEG）。

    ⚠️ 它返回的是 **`ilst` 的子项**，不是一整个 `udta`。初版这里（与
    `ilst_encoder`）各返回一个 `box("udta", …)`，于是 `mp4_bytes` 把它们
    直接相加，`moov` 里就出现了**两个并列的 `udta`** —— 而
    `_parse_bmff` 只取**第一个** `udta` ⇒ 后加进去的那一项永远读不到。
    后果：带编码工具的样本里 `encoder_hint` 恒为 `None`，
    「自报编码工具」这条判据一次都没被样本走到过（它看起来只是"没命中"）。
    `udta/meta/ilst` 是**单数**结构，装配必须发生在同一处。
    """
    data = box("data", (13).to_bytes(4, "big") + b"\x00" * 4 + jpeg)
    return box("covr", data)


def encoder_item(name: str) -> bytes:
    """`ilst` 里的一项：`©too` = 编码工具。它本身**不是**异常，只是溯源信息。"""
    data = box("data", (1).to_bytes(4, "big") + b"\x00" * 4 + name.encode("utf-8"))
    return box("\xa9too", data)


def udta_box(items: Sequence[bytes]) -> bytes:
    """把若干 `ilst` 子项装配成**单个** `udta/meta/ilst`。

    `meta` 与 `ilst` 都是 **FullBox**（载荷以 `version + flags` 开头）——
    这一点是 `decode._children` 那段注释里说的那个坑，样本端必须写对，
    否则读样本的人会以为"框架读不了封面帧"。
    """
    return box("udta", full_box("meta", payload=full_box("ilst", payload=b"".join(items))))


# ---------------------------------------------------------------------------
# 主构造器
# ---------------------------------------------------------------------------

def mp4_bytes(
    *,
    frame_count: int = 30,
    fps_delta: int = 1000,
    timescale: int = 30_000,
    declared_duration: int | None = None,
    codec: str = "avc1",
    width: int = 640,
    height: int = 480,
    delta_plan: Sequence[tuple[int, int]] | None = None,
    moov_before_mdat: bool = True,
    mdat_segments: int = 1,
    trailing: bytes = b"",
    free_bytes: int = 0,
    sample_payload: Callable[[int], bytes] | None = None,
    cover_jpeg: bytes | None = None,
    encoder: str | None = None,
    elst_entries: Sequence[tuple[int, int]] | None = None,
) -> bytes:
    """拼一份 MP4。默认参数产出的是**一份结构自洽的干净文件**。

    判据（`forensics.check_*`）要抓的每一样东西，都对应这里的一个旋钮：

        moov_before_mdat=False   → `moov` 在 `mdat` 之后（边写边录 / 重写过）
        mdat_segments=2          → 多段 `mdat`（拼接 / 分段写）
        trailing=b"..."          → 尾部多余数据（长度对不上）
        declared_duration=...    → `mvhd` 声明的总时长与轨道（样本表）不符
        delta_plan=[...]         → 帧间隔跳变
        free_bytes=N             → `free` 填充块
        elst_entries=[...]       → 编辑列表跳段

    ⚠️ `stco` 的偏移**依赖 `moov` 自身的大小**（它装着偏移），而 `moov` 的大小又由
    `stco` 的项数决定 —— 这是个环。解法是**两遍构造**：先拿全 0 偏移量一遍拿到
    `moov` 大小（项数已定 ⇒ 大小不再变），再按真实布局重算偏移。
    只写一遍、把偏移留 0，样本表就会指向文件头 —— 那是"越界"，
    会让**干净样本**被判成异常（假阳性），而假阳性会让人不再信任整份报告。
    """
    payload_of: Callable[[int], bytes] = sample_payload or (lambda _i: bytes.fromhex(
        "0000000c6d646174"  # 一个最小盒，充当"压缩后的帧数据"占位
    ))
    payloads = [payload_of(i) for i in range(frame_count)]
    sizes = [len(p) for p in payloads]

    def build_moov(offsets: Sequence[int], mdat_start: int) -> bytes:
        stbl = box(
            "stbl",
            stsd_video(codec, width, height)
            + stts(delta_plan or [(frame_count, fps_delta)])
            + stsz(sizes)
            + stsc([(1, 1, 1)])
            + stco(offsets),
        )
        total_delta = sum(n * d for n, d in (delta_plan or [(frame_count, fps_delta)]))
        # ⚠️ **两个不同的数，不能合成一个**（这正是这个旋钮存在的理由）：
        #      · `total_delta` = 从**样本表**（`stts`）加出来的真值 ⇒ 写进 `mdhd` / `tkhd`；
        #      · `declared`    = 容器头部**声明**的总时长 ⇒ 只写进 `mvhd`。
        #    初版把 `declared` 同时写进三处 ⇒ 三个数**永远相等**，
        #    `check_timeline`（它比的就是这三者）因此一条线索也抓不到；
        #    而样本名字却叫 `duration_mismatch` —— 一个"造了要抓的形态、
        #    却没造出来"的样本，断言只会绿，且看起来完全正常。
        #    ⚠️ 这也说明「三处时长对不上」与「头部声明 vs 样本表真值」是**同一件事**：
        #    轨道那两个数本来就是从样本表推出来的，所以两条判据在这里是重合的。
        declared = declared_duration if declared_duration is not None else total_delta
        mdia = box("mdia", mdhd(timescale, total_delta) + hdlr() + box("minf", stbl))
        # ⚠️ 编辑列表挂在 **`trak`** 下（`trak/edts/elst`），**不是** `mdia` 下 ——
        #    14496-12 的层级就是如此。放错了不会报错，只是 `elst` 永远读不到，
        #    而"掐掉开头"这个**最常见的剪辑动作**因此一条线索都不留。
        edts = box("edts", elst(elst_entries)) if elst_entries else b""
        items: list[bytes] = []
        if cover_jpeg:
            items.append(covr_item(cover_jpeg))
        if encoder:
            items.append(encoder_item(encoder))
        # ⚠️ **一个 `moov` 只有一个 `udta`**（见 `covr_item` 的注释）。
        udta = udta_box(items) if items else b""
        trak = box("trak", tkhd(1, total_delta, width, height) + edts + mdia)
        return box("moov", mvhd(timescale, declared) + trak + udta)

    ftyp = box("ftyp", b"mp42" + (0).to_bytes(4, "big") + b"mp42isom")
    stub = build_moov([0] * frame_count, 0)

    # 分段：把样本平均切给 N 段 mdat（`mdat_segments=1` 时就是一整段）
    per = max(1, frame_count // mdat_segments)
    bounds: list[tuple[int, int]] = []
    cursor = 0
    for seg in range(mdat_segments):
        stop = frame_count if seg == mdat_segments - 1 else min(frame_count, cursor + per)
        bounds.append((cursor, stop))
        cursor = stop

    def build_mdats(mdat_start: int) -> tuple[bytes, list[int]]:
        blobs: list[bytes] = []
        offsets: list[int] = []
        pos = mdat_start
        for lo, hi in bounds:
            blob = b"".join(payloads[lo:hi])
            for i in range(lo, hi):
                offsets.append(pos + 8 + sum(sizes[lo:i]))
            blobs.append(box("mdat", blob))
            pos += 8 + len(blob)
        return b"".join(blobs), offsets

    if moov_before_mdat:
        # 两遍：`stub` 的项数已定 ⇒ 它的大小就是最终 `moov` 的大小
        mdats, offsets = build_mdats(len(ftyp) + len(stub))
        moov = build_moov(offsets, len(ftyp))
        assert len(moov) == len(stub), "两遍构造出的 moov 大小不一致 ⇒ 偏移会整体错位"
        body = ftyp + moov + mdats
    else:
        mdats, offsets = build_mdats(len(ftyp))
        moov = build_moov(offsets, len(ftyp))
        body = ftyp + mdats + moov

    if free_bytes:
        body += box("free", b"\x00" * free_bytes)
    return body + trailing


# ---------------------------------------------------------------------------
# 具名样本：每一个对应一条判据
# ---------------------------------------------------------------------------

def clean_mp4(**kw) -> bytes:
    """结构自洽的干净样品。**它必须一个异常都不触发** —— 这是最重要的对照。"""
    return mp4_bytes(**kw)


def moov_after_mdat() -> bytes:
    """`moov` 在 `mdat` 之后：手机边写边录、或整文件被重写过时的典型形态。

    ⚠️ 它**本身不是伪造证据** —— 正常导出工具也会这么排。
    所以对应结论只能是"存在与重写一致的线索"，不能是"画面被动过"。
    """
    return mp4_bytes(moov_before_mdat=False)


def two_mdats() -> bytes:
    """多段 `mdat`：拼接两个文件、或分段落盘的痕迹。同样**不是**伪造证据。"""
    return mp4_bytes(mdat_segments=2)


def trailing_garbage(n: int = 64) -> bytes:
    """尾部多出 N 字节：`mdat` 之后不该有东西，而"多出来的字节"没有任何合法解释。"""
    return mp4_bytes(trailing=b"\x00" * n)


def free_padding(n: int = 4096) -> bytes:
    """`free` 填充块：常见于就地重写（改小的数据用空闲块补齐）。"""
    return mp4_bytes(free_bytes=n)


def duration_mismatch(*, declared: int = 30_000 * 20, frames: int = 30) -> bytes:
    """`mvhd` 声明 20 秒，而轨道（`mdhd` / `tkhd`，来自样本表）只有 30 帧 ≈ 1 秒。

    这是「头部被改过、或者文件被截断后没修头」的形态：播放器按 `mvhd` 显示一条
    20 秒的时间轴，而里面只有 1 秒的样本 ⇒ `check_timeline` 应当**报差值**。

    ⚠️ 这里**只**让 `mvhd` 与轨道不一致，**不**去动 `mdhd` 与 `stts` 的关系 ——
    「`mdhd` 时长 vs `stts` 逐样本之和」那条更紧的不变式**故意没有实现**：
    真实文件里编辑列表、音视频轨长不等都会让它合法地不成立（音轨比视频轨长是常态），
    加一条会误报的判据，比不加更糟 —— 它教人忽略这一项。
    """
    return mp4_bytes(frame_count=frames, declared_duration=declared)


def frame_rate_jump(*, base: int = 1000, jump: int = 20_000) -> bytes:
    """前 10 帧正常、之后帧间隔突增 20 倍：`stts` 里两段不同的 delta。"""
    return mp4_bytes(delta_plan=[(10, base), (20, jump)])


def with_elst_skip() -> bytes:
    """编辑列表里有一个空编辑（`media_time = -1`）—— 常见于"掐掉开头"。"""
    return mp4_bytes(elst_entries=[(500, -1), (29_500, 0)])


def with_cover(jpeg: bytes, **kw) -> bytes:
    """带内嵌封面帧的 MP4 —— 这是**唯一**能在没有解码器时取到画面的一类 MP4。

    所以它同时是"帧层能跑起来"的正样本和"视频降级能抽到一帧"的正样本。
    """
    return mp4_bytes(cover_jpeg=jpeg, **kw)


def motion_jpeg_mp4(frames: Sequence[bytes], *, fps_delta: int = 1000) -> bytes:
    """样本本身就是完整 JPEG（Motion JPEG）—— `decode.py` 能**零解码**把它们取出来。

    ⚠️ 这不是"重编码"，是把本来就在容器里的 JPEG 原样取出来；
    与 `frames.py` 那条"绝不为给模型看而重编码"的纪律一致。
    """
    pool = list(frames)

    def payload(i: int) -> bytes:
        return pool[i % len(pool)]

    return mp4_bytes(
        frame_count=len(pool), fps_delta=fps_delta, codec="jpeg", sample_payload=payload
    )


def rasterizable_gif(frames: int = 3, *, size: tuple[int, int] = (16, 16)) -> bytes:
    """用 Pillow 造一份**真的能被光栅化**的动画 GIF（帧层正样本）。

    ⚠️ 它与上面那个 `animated_gif()` **并存是刻意的**，两者管的不是同一层：

        `animated_gif()`    手拼字节：块结构正确、**没有合法的 LZW 流** ⇒
                            容器层（帧数 / 延迟 / 循环）全能读到，
                            而 `extract_gif_frames` 会在光栅化时报 truncated。
        `rasterizable_gif()`  真编码器产出 ⇒ 容器层与帧层**都**应当正常。

    这一对样本证明「容器层」与「帧层」是**分别**在工作，而不是层数不够时
    一起哑掉 —— 如果只有前者，那么"帧层跑不起来"与"帧层实现错了"
    在断言里长得一模一样。

    ⚠️ 这是本模块**唯一**需要第三方库（Pillow）的函数，且**按需导入**：
    其余构造器都是纯字节，能在任何 Python 上逐位复现 —— 那正是
    `decode.py` 那句「样本构造可以完全脱离运行环境」的前提。
    """
    from PIL import Image  # noqa: PLC0415 - 按需导入，理由见上（本模块其余部分零依赖）

    width, height = size
    pictures = []
    for index in range(frames):
        # ⚠️ 用 **RGB** 而不是 `"P"`。`Image.new("P", …)` 给的是**全零调色板**，
        #    `putpixel` 写的是"调色板索引"，于是每一帧光栅化出来都是**同一张全黑图**
        #    ⇒ 帧层会报「第 0 帧与第 1 帧字节完全相同（同一帧被重复写入）」
        #    —— 一条**我们自己造出来的假线索**，而且它看起来完全正常。
        image = Image.new("RGB", (width, height))
        for x in range(width):
            for y in range(height):
                image.putpixel((x, y), (
                    (x * 7 + index * 29) % 256,
                    (y * 13 + index * 53) % 256,
                    (x + y + index * 40) % 256,
                ))
        pictures.append(image)
    buffer = io.BytesIO()
    pictures[0].save(
        buffer, format="GIF", save_all=True, append_images=pictures[1:],
        duration=100, loop=0,
    )
    return buffer.getvalue()


def animated_gif(frames: int = 3, *, width: int = 2, height: int = 2) -> bytes:
    """手拼的 GIF89a（块结构正确、不含真实像素数据）。

    它验证的是**容器层**（帧数 / 延迟 / 循环次数），不是像素域 ——
    像素域要 Pillow 光栅化，见 `rasterizable_gif()` 与 `frames.extract_gif_frames`。
    """
    header = (
        b"GIF89a"
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
        + bytes([0xF0, 0x00, 0x00])                 # GCT 存在 + 2 色
        + b"\x00\x00\x00\xff\xff\xff"               # 全局色表
        + b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"   # 循环 0 = 无限
    )
    body = b""
    for _ in range(frames):
        body += b"\x21\xf9\x04\x00\x0a\x00\x00\x00"        # GCE，延迟 10/100 秒
        body += (
            b"\x2c\x00\x00\x00\x00"
            + width.to_bytes(2, "little") + height.to_bytes(2, "little") + b"\x00"
        )
        body += b"\x02\x02\x44\x01\x00"                    # LZW 最小码长 + 子块
    return header + body + b"\x3b"



def not_a_video() -> bytes:
    """一份根本不是视频容器的字节（用来验证"取件层拒绝"与"检测层拒绝"分工）。"""
    return b"this is definitely not a video container at all, just prose." * 4


__all__ = [
    "animated_gif",
    "box",
    "clean_mp4",
    "covr_item",
    "duration_mismatch",
    "elst",
    "encoder_item",
    "frame_rate_jump",
    "free_padding",
    "full_box",
    "hdlr",
    "mdhd",
    "moov_after_mdat",
    "motion_jpeg_mp4",
    "mp4_bytes",
    "mvhd",
    "not_a_video",
    "rasterizable_gif",
    "stco",
    "stsc",
    "stsd_video",
    "stsz",
    "stts",
    "tkhd",
    "trailing_garbage",
    "two_mdats",
    "udta_box",
    "with_cover",
    "with_elst_skip",
]
