"""合成样本构造器 —— **纯函数、无副作用**。

抽出来的原因：`run/probe_evidence.py`（冒烟）与 `run/diag_grid_samples.py`（诊断）
必须**用同一批样本**，否则两边看到的数对不上，诊断就没有意义（复制一份样本代码
= 迟早两边漂移，正是本项目反复踩的坑）。

## 两个刻意设计

1. **负对照比正样本重要**。`photo` 默认产出的是"干净照片"，不该被任何一项抓到；
   `periodic` 是专供 `copy_move` 的负对照（周期纹理不得报克隆）。

2. **细纹理层的两种实现**（`texture=`）：
   - `"nearest"`：旧实现 —— 半分辨率噪声 **NEAREST 放大 2 倍**。
     它造出强 **周期 2** 结构，会污染 8×8 块网格的比值统计（实测：`ratios[7]` 被压到 0.4，
     argmax 落到别的 offset 上 → 正常图被报「网格偏移」）。**保留只是为了做对照实验。**
   - `"white"`：新实现 —— **全分辨率白噪声**（振幅 ≈4.2），更接近真实传感器颗粒，
     没有周期性，且足以在 q92 下存活到 `copy_move` 能凑出块。
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

rng = np.random.default_rng(20260930)
W, H = 720, 540


def _smooth_noise(shape: tuple[int, ...], scale: int = 24) -> np.ndarray:
    """低频平滑随机场，**归一到零均值、单位标准差**。

    ⚠️ 血泪点（2026-09-30）：本函数原来返回的是 **0..255** 的场，而 `photo()` 里写的是
    `110 + 34 * field` —— 当成 ±1 的场用了。于是结果恒为 `110 + 34×[0,255]`，
    全图冲到 255 被裁平，**实测 `std=4.3`、逐像素梯度均值 0.016**：
    这根本不是"照片"，是一张白纸。而它"看起来"完全正常 ——
    所有检查项都照常跑完、负对照"全绿"，只是全绿得毫无意义。
    后面几轮的阈值校准全都建立在这张白纸上，直到我把残差打出来才看见。
    → **校准类脚本必须先把样本的统计量打出来核对**，不能只看检测项的结论。
    """
    small = rng.normal(0, 1, (shape[0] // scale + 2, shape[1] // scale + 2))
    img = Image.fromarray(
        ((small - small.min()) / (np.ptp(small) + 1e-9) * 255).astype(np.uint8)
    )
    field = np.asarray(img.resize((shape[1], shape[0]), Image.BICUBIC)).astype(np.float32)
    field -= float(field.mean())
    return field / max(float(field.std()), 1e-6)


def photo(
    name: str = "clean",
    *,
    w: int = W,
    h: int = H,
    texture: str = "white",
) -> np.ndarray:
    """合成一张「像照片」的图：低频明暗 + 局部结构 + 传感器颗粒。"""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    lum = 110 + 34 * _smooth_noise((h, w), 40)
    if name == "structured":
        for _ in range(14):
            cx, cy = rng.integers(40, w - 40), rng.integers(40, h - 40)
            r = rng.integers(12, 46)
            d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
            lum += np.where(d < r, rng.normal(35, 12), 0) * np.clip(1 - d / (r + 1), 0, 1)
        lum += 18 * np.tanh((xx - w * 0.55) / 9.0)
    if name == "periodic":
        lum += 26 * np.sin(xx / 3.1) * np.cos(yy / 4.3)

    if texture == "nearest":
        # 旧的（有问题的）实现：半分辨率 + NEAREST 2× → 强周期 2
        fine = rng.normal(0, 1, (h // 2 + 1, w // 2 + 1)).astype(np.float32)
        fine_img = Image.fromarray(
            np.clip((fine - fine.min()) / (np.ptp(fine) + 1e-9) * 255, 0, 255).astype(np.uint8)
        )
        lum += (np.asarray(fine_img.resize((w, h), Image.NEAREST)).astype(np.float32) - 128) * 0.30
        pixel_sigma = 3.4
    elif texture == "white":
        # 全分辨率颗粒：无周期结构，且能存活到 q92
        lum += rng.normal(0, 4.2, (h, w)).astype(np.float32)
        pixel_sigma = 2.2
    elif texture == "none":
        pixel_sigma = 2.2
    else:
        raise ValueError(f"未知 texture={texture!r}")

    rgb = np.stack([lum, lum * 0.93 + 9, lum * 0.86 + 20], axis=2)
    rgb += rng.normal(0, pixel_sigma, size=rgb.shape)
    return np.clip(rgb, 0, 255).astype(np.uint8)


def jpeg(arr: np.ndarray, quality: int = 85, exif: Image.Exif | None = None) -> bytes:
    buf = io.BytesIO()
    kw = {"exif": exif} if exif is not None else {}
    Image.fromarray(arr, "RGB").save(buf, "JPEG", quality=quality, **kw)
    return buf.getvalue()


def png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, "PNG")
    return buf.getvalue()


def adobe_exif() -> Image.Exif:
    """元数据矛盾样本用的 EXIF：编辑软件 + 无相机 + 两个时间戳打架。"""
    exif = Image.Exif()
    exif[0x0131] = "Adobe Photoshop 25.0 (Windows)"
    exif[0x0132] = "2026:09:30 12:00:00"
    exif[0x9003] = "2024:03:11 08:15:00"
    return exif


def recrop_jpeg(data: bytes, *, dx: int = 4, dy: int = 4, quality: int = 95) -> bytes:
    """**块网格相位偏移的正样本**：裁剪错位几像素后，用高质量重编码。

    机理：新编码的网格（q95，非常弱）对齐在**新**原点；而**旧编码留下的强网格
    仍然留在像素里**，相位被挪了 `dx` 像素。所以 `block_grid` 应当在
    `offset = (7 - dx) % 8` 上看到峰值 —— 这是"裁剪后重编码"的真实指纹，
    也是本项唯一真正的检出力。

    ⚠️ 如果只裁不重编码（或重编码质量很低），旧网格会被新网格盖过去 ——
    那就真的检不出来了，属预期（本项本来就只对部分编辑链路有效）。
    """
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    buf = io.BytesIO()
    img.crop((dx, dy, w - 8 + dx, h - 8 + dy)).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def paste_patch_jpeg(
    data: bytes,
    *,
    dx: int = 5,
    dy: int = 5,
    box: int = 320,
    quality: int = 92,
    patch_quality: int = 60,
) -> bytes:
    """**强相位偏移的正样本**：把一个低质量编码（强网格）的图块，错位贴进高质量底图。

    机理：底图 q92 的网格很弱、且对齐在原点；贴进来的图块带着 **q60 的强网格**，
    相位被 `dx`/`dy` 挪走了 ⇒ `block_grid` 应当在 `offset=(7-dx)%8` 上看到峰值。

    为什么需要这个样本：`recrop_jpeg`（裁 4px 后 q95 重编码）产生的**旧网格太弱**
    （q95 几乎不量化），弱到分不清相位。要验证"相位偏移能不能被检出"，
    必须给一个**信号足够强**的偏移样本，否则测的其实是噪声底。
    """
    base = Image.open(io.BytesIO(data)).convert("RGB")
    patch = Image.open(io.BytesIO(jpeg(photo("structured"), patch_quality))).convert("RGB")
    patch = patch.resize((box, box), Image.BICUBIC)   # 缩放会引入重采样痕迹，属额外噪声
    # 让贴片自身再走一次 q60，把强网格重新写进去（resize 已经把它糊掉了）
    pbuf = io.BytesIO()
    patch.save(pbuf, "JPEG", quality=patch_quality)
    patch = Image.open(io.BytesIO(pbuf.getvalue())).convert("RGB")
    out = base.copy()
    out.paste(patch, (dx, dy))
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def splice_photo(*, name: str = "structured", seed: int = 11) -> np.ndarray:
    """**拼接正样本**：左半降对比 + 抬亮，右半叠一层额外噪声。

    关键是**右半要加噪声**：只改亮度/对比不会在噪声层留下断层，
    `noise_residual` 就（正确地）不触发 —— 初版在断言脚本里漏了这一步，
    于是"正样本没被触发"，看起来像检测器坏了，其实是样本写错了。
    """
    arr = photo(name).copy().astype(np.float32)
    half = arr.shape[1] // 2
    arr[:, :half] = arr[:, :half] * 0.55 + 40
    extra = np.random.default_rng(seed).normal(0, 11.0, size=arr[:, half:].shape)
    arr[:, half:] += extra
    return np.clip(arr, 0, 255).astype(np.uint8)


def copy_move_photo(
    *,
    name: str = "structured",
    dx: int = 312,
    dy: int = 0,
    box: tuple[int, int, int, int] = (150, 214, 160, 224),
) -> np.ndarray:
    """**复制-移动正样本**：把一块 64×64 的内容贴到远处。

    ⚠️ 位移默认取 **312 = 39 × 8**，即块步长的整数倍。本实现以 8 像素为块步长做
    特征匹配，位移不是它的整数倍时源块与目标块落在不同格点上、特征对不上
    （这是已知限制，写在 `check_copy_move` 的 caveat 里，不是靠调参绕过去的）。
    """
    arr = photo(name).copy()
    y0, y1, x0, x1 = box
    arr[y0:y1, x0:x1] = arr[y0 + dy:y1 + dy, x0 + dx:x1 + dx]
    return arr


def flat_area_image(*, h: int = 540, w: int = 720) -> np.ndarray:
    """大片纯色底 + 几个深色方块 —— **截图 / 拍白墙 / 拍文件**的常见形态。

    这是 `check_flat_noise` 那道 JPEG 闸门**唯一的证据样本**，也是它存在的理由：
    这类图的平坦区在 JPEG 压缩后**真的**不含噪声（量化把 AC 系数清零了）。
    闸门若被去掉，它会被报成"缺少传感器读数噪声 ⇒ 疑似生成" —— 一个纯粹的假阳性
    （实测 q70 与 q92 都会中招）。而这类图在真实取证里一点都不少见。
    """
    a = np.full((h, w, 3), 246, np.uint8)
    a[80:200, 100:260] = 30
    a[260:400, 380:560] = 60
    a[440:500, 120:640] = 90
    return a


def oversized_png(*, w: int = 12000, h: int = 10000) -> bytes:
    """**只声明尺寸、不含像素数据**的 PNG（IHDR + IEND，几十字节）。

    为什么要这么造，而不是拿一张真的大图：`analyze` 的像素上限正是要拦"解压前"的图，
    而**造一张 120 MP 的真图本身就要先在内存里放 360 MB** —— 为了测"防止内存被吃满"
    而先把内存吃满，测试比被测对象还危险。

    PNG 的文件头（IHDR）就写着宽高，`Image.open()` **只读头**就能报出尺寸，
    而 `load()` 才会真正解压。所以这个样本正好能把"检查发生在解压之前"钉死：
    如果哪天有人把尺寸检查挪到 `load()` 之后，这个样本会立刻变成
    `Truncated File Read` 而不是"像素数过大"—— 失败的样子就不一样了。

    默认 12000×10000 = 120 MP：**大于本项目的 100 MP 上限，
    但小于 Pillow 自己的解压炸弹阈值（约 179 MP）**，因此测的是我们那道闸门，
    而不是 Pillow 的。
    """
    import struct
    import zlib

    def _chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8bit 真彩，无隔行
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IEND", b"")


__all__ = [
    "H", "W", "adobe_exif", "copy_move_photo", "flat_area_image", "jpeg",
    "oversized_png", "paste_patch_jpeg", "photo", "png", "recrop_jpeg", "splice_photo",
]
