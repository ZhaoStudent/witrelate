"""图像鉴伪的检测核心 —— **纯函数、无模型、可离线断言**。

设计取舍（三条，都是踩过坑之后的规矩）：

1. **不让模型看图，让代码算图。**
   模型看图的结论既不可复现、也无法审计；而且生产模型是否支持视觉输入本身是未知数。
   这里所有判定都由确定性代码产生，模型只负责解释与组织语言。

2. **不输出「真 / 假」。**
   独立测评显示顶级检测器对确认的 AI 图仍有 15–20% 假阴性、对压缩过的真实照片有同量级
   假阳性。所以本模块只输出：线索强度 + 逐项 caveat + 综合等级 + 升级建议。

3. **每条发现必须自带 caveat。**
   一个只说「异常」、不说「异常不等于篡改」的鉴伪工具，比没有更危险。

依赖：Pillow + numpy（**不引入 opencv / torch** —— 服务器只有 3.6 GB 内存且无 GPU）。
"""

from __future__ import annotations

import hashlib
import io
import warnings
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

from ..verdict import summarize_findings
from .contracts import Finding, ImageForensicsReport, UpgradePath

#: 像素域检查的最大边长。**用中心裁剪而不是缩放** ——
#: 缩放会把 JPEG 的 8×8 块网格与重采样伪影一起抹掉，那正是要测的东西。
ANALYSIS_MAX_SIDE = 1600

#: **解码前**的像素数上限。
#:
#: 理由**是内存，不是洁癖**：Pillow 的 `load()` 会把整幅图解压进内存（RGB 约 3 字节/像素），
#: 而服务器只有 3.6 GB 内存、可用约 2.5 GB，**还同时跑着 7 个角色的进程** ——
#: 一张 400 MP 的图就能把整台机器拖垮，连带让已经过审的 6 个 Agent 一起不可用。
#: 100 MP ≈ 300 MB 峰值，给并发留了余量，也覆盖了现实中的取证图片。
#:
#: ⚠️ 检查必须发生在 `load()` **之前**：`Image.open()` 只读文件头就能给出尺寸，
#: 而 `load()` 才是真正解压的那一步 —— 等解压完再检查，内存已经花掉了。
MAX_IMAGE_PIXELS = 100_000_000

#: JPEG 标准量化表（ITU-T T.81 Annex K，quality=50 基准）
STD_LUMA_QT = [
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
]
STD_CHROMA_QT = [
    17, 18, 24, 47, 99, 99, 99, 99,
    18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99,
    47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
]

#: 编辑软件 / 平台常见的 EXIF Software 取值（命中即提示「经过软件处理」）
EDITOR_SOFTWARE_HINTS = (
    "photoshop", "gimp", "lightroom", "affinity", "paint.net", "pixlr",
    "snapseed", "meitu", "美图", "beautyplus", "stable diffusion",
    "midjourney", "dall", "firefly", "comfyui", "invoke", "canva", "figma",
    "screenshot", "截图", "snipaste", "微信", "wechat", "qq",
)

#: 各检查项的权重 —— 综合等级由它算出，**不是模型给的**。
CHECK_WEIGHTS = {
    "exif": 1.0,
    "quantization": 1.0,
    "noise_residual": 1.0,
    "copy_move": 1.0,
    "flat_noise": 0.5,
    "block_grid": 0.5,
    "ela": 0.5,
    "resampling": 0.5,
}

DISCLAIMER = (
    "本报告是基于公开图像取证方法得出的**技术线索**，不是司法鉴定意见，"
    "不能单独作为认定事实的依据。所列「未提示异常」**不等于**该图真实；"
    "所列「提示异常」**不等于**该图伪造 —— 例如平台重压缩、裁剪、降噪、"
    "格式转换都会产生同类痕迹。如需具备法律效力的结论，请走司法鉴定路径。"
)

CHAIN_OF_CUSTODY = [
    "立即保全**原始载体**（原始存储卡 / 手机 / 只读镜像），不要用微信、截图、转发件转存。",
    "对原文件计算 SHA-256 并记录时间与操作人；本次报告的 sha256 字段即为该值。",
    "任何后续取证都在**只读副本**上进行，原件封装保存 —— 司法鉴定通常要求提交原始载体。",
]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sniff_media_type(data: bytes) -> str:
    """按**魔术字节**判类型，不信任文件扩展名或调用方声明。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[:4] == b"%PDF":
        return "application/pdf"
    return "application/octet-stream"


def pixel_budget_exceeded(size: tuple[int, int]) -> bool:
    """这张图的像素数是否超出 `MAX_IMAGE_PIXELS`。**纯函数**，便于直接断言边界。

    ⚠️ 这里刻意**不做** try/except 兜底。第一版写的是"解析失败就返回 False（= 不拦）"，
    那正是本项目已经定性过的那个反模式：**护栏在拿不准的时候选择了放行**。
    它还有第二个坏处 —— 真要是调用方传错类型，`TypeError` 会被这里悄悄吃掉，
    于是我们自己的 bug 变成了"闸门莫名其妙不生效"。所以让它**直接炸**：
    闸门只负责判断大小，输入不合法是另一回事，不该由它来掩盖。
    """
    w, h = int(size[0]), int(size[1])
    return w * h > MAX_IMAGE_PIXELS


def open_image(data: bytes) -> tuple[Image.Image, dict[int, list[int]], tuple[int, int]]:
    """打开图像。**同时**取回量化表与原始尺寸 —— 两者都会在裁剪/转换后丢失。

    ⚠️ 血泪点：Pillow 的 `img.crop()` 与 `img.convert()` 都返回**新的普通 Image**，
    而 JPEG 的量化表是 `JpegImageFile` 的实例属性（不在 `info` 里）→ 裁完就读不到了。
    所以量化表必须在裁剪**之前**、从**原始**对象上取。

    ⚠️ 尺寸检查必须排在 `load()` 之前（见 `MAX_IMAGE_PIXELS`）；
    同时把 Pillow 的「解压炸弹」异常统一收敛成 `ValueError` ——
    它继承自 `Exception` 而**不是** `OSError`，漏掉它的话
    `pipeline.outcome()` 那圈"输入不合法"的捕获就接不住它，
    结果又变回一句不透明的异常。
    """
    try:
        # ⚠️ 这里屏蔽 Pillow 自己的「解压炸弹」**警告**，有两个理由，都不是洁癖：
        #   ① 我们**紧接着就有更严的像素上限**（100 MP < Pillow 的 179 MP 报错线），
        #      警告要说的那件事由我们自己的结论负责，重复一遍只会污染输出去；
        #   ② 更实际的：`python -W error` / `PYTHONWARNINGS=error` 下 Warning 会被
        #      **升级成异常** —— 而它是 `Warning` 而不是 `OSError`/`ValueError`，
        #      `pipeline.outcome()` 那圈"输入不合法"的捕获接不住，于是又穿透成
        #      一句不透明的异常。实测在 (89.5, 179] MP 这个区间正好会踩到。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            raw = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:  # 像素数远超上限，Pillow 自己先拦下了
        raise ValueError(f"图像像素数过大（{exc}）—— 请先等比缩小再传。") from None
    size = (int(raw.size[0]), int(raw.size[1]))
    if pixel_budget_exceeded(size):
        raise ValueError(
            f"图像像素数过大（{size[0]}×{size[1]} = {size[0] * size[1] // 1_000_000} MP，"
            f"上限 {MAX_IMAGE_PIXELS // 1_000_000} MP）—— 请先等比缩小再传。"
        )
    raw.load()
    qt: dict[int, list[int]] = {}
    table = getattr(raw, "quantization", None)
    if table:
        for key, values in table.items():
            try:
                qt[int(key)] = [int(v) for v in values]
            except (TypeError, ValueError):
                continue
    img = raw if raw.mode in ("RGB", "L") else raw.convert("RGB")
    return img, qt, size


def analysis_crop_box(size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """算出像素域检查要用的中心裁剪框；不需要裁剪时返回 None。

    **纯函数、可断言**（`tools/verify_evidence.py` 直接验它，不必去猜裁剪结果）。
    三条不变量，每条都对应一次踩坑：

      1. **起点与边长都是 8 的倍数** —— 否则我们自己就把 JPEG 的 8×8 网格相位挪走了，
         于是每张大于 1600px 的正常图都会被自己制造的裁剪判成「网格偏移」。
      2. **裁剪框不越界** —— 用 `min(ANALYSIS_MAX_SIDE, w, h)` 取边长。
         初版写死 `side = 800×2 = 1600`，遇到 `2004×1508` 这种长短边悬殊的图，
         下边就伸到画面外，Pillow 会**补黑边** —— 等于我们亲手往图里塞了一道强边，
         所有基于边缘的检查项（ELA / 噪声 / 块网格）全部被污染。
      3. **只裁不缩** —— 缩放会把 8×8 块网格与重采样伪影一起抹掉，而那正是要测的东西。
    """
    w, h = size
    if max(w, h) <= ANALYSIS_MAX_SIDE:
        return None
    side = (min(ANALYSIS_MAX_SIDE, w, h) // 8) * 8
    x0 = min(max((w - side) // 2 // 8 * 8, 0), w - side)
    y0 = min(max((h - side) // 2 // 8 * 8, 0), h - side)
    return x0, y0, x0 + side, y0 + side


def crop_for_analysis(img: Image.Image) -> tuple[Image.Image, bool]:
    """按 `analysis_crop_box` 中心裁剪。返回 (图, 是否裁剪过)。

    ⚠️ **裁剪框的起点与尺寸都必须落在 8 的整数倍上**（血泪点）。
    初版用 `cx - half` 直接当起点，而 `half = 800` 与奇偶宽度一组合，起点
    `% 8` 就不为 0 —— 于是**我们自己把 JPEG 的 8×8 网格相位挪走了**，
    `check_block_grid` 随即报「网格偏移」。也就是说：**一个完全正常的图，
    只要尺寸大于 1600px，就会被我们自己制造的裁剪判成「有过几何处理」。**
    """
    box = analysis_crop_box(img.size)
    if box is None:
        return img, False
    return img.crop(box), True


def _scale_qt(std: list[int], quality: int) -> list[int]:
    q = max(1, min(100, quality))
    scale = 5000 // q if q < 50 else 200 - 2 * q
    return [max(1, min(255, (v * scale + 50) // 100)) for v in std]


def match_standard_quality(luma_qt: list[int], chroma_qt: list[int] | None) -> int | None:
    """量化表是否等于某档标准表（⇒ 未被编辑软件重写过）。"""
    for q in range(1, 101):
        if _scale_qt(STD_LUMA_QT, q) != list(luma_qt):
            continue
        if chroma_qt is not None and _scale_qt(STD_CHROMA_QT, q) != list(chroma_qt):
            continue
        return q
    return None


def _block_reduce(arr: np.ndarray, size: int, fn: Any) -> np.ndarray:
    """非重叠分块归约。返回 (rows, cols)。"""
    h, w = arr.shape
    h2, w2 = (h // size) * size, (w // size) * size
    if h2 == 0 or w2 == 0:
        return np.empty((0, 0), dtype=np.float32)
    view = arr[:h2, :w2].reshape(h2 // size, size, w2 // size, size)
    return np.asarray(fn(view, axis=(1, 3)), dtype=np.float32)


def _residual(img: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    """返回 (亮度数组, 去低频后的残差) —— 噪声类检查的公共输入。"""
    gray = img.convert("L")
    arr = np.asarray(gray).astype(np.float32)
    blurred = np.asarray(gray.filter(ImageFilter.GaussianBlur(1.0))).astype(np.float32)
    return arr, arr - blurred


#: 像素域检查的**适用前提**闸门参数（见 `flat_fraction`）
NATURAL_BLOCK = 16
NATURAL_FLAT_STD = 2.0
NATURAL_MAX_FLAT = 0.50


def flat_fraction(
    img: Image.Image,
    *,
    block: int = NATURAL_BLOCK,
    flat_std: float = NATURAL_FLAT_STD,
) -> float:
    """「几乎没有纹理」的区块占比 —— 像素域检查**适用前提**的度量。

    `ela` / `noise_residual` / `copy_move` 的共同前提是**自然摄影内容**：
    画面各处都布满传感器噪声与纹理。截图、纯色背景、纯文字画面
    （拍白墙、拍文件、UI 截图）**不满足这个前提** ——
    实测一张 92.3% 区块无纹理的纯色图会让 `noise_residual` 报「噪声不均」、
    `copy_move` 报「克隆」：两个都是纯粹的假阳性（同类样本在真实取证里并不少见）。

    → **前提不满足就判「无法判定」**，与"用噪声缺失去判 JPEG 是无效的"是同一条规矩：
      方法有自己的适用范围，出了范围不要硬给结论。

    实测分离度：七张自然内容样本 0.000–0.001，纯色图 0.923 —— 阈值 0.50 余量很大。
    """
    arr = np.asarray(img.convert("L")).astype(np.float32)
    blocks = _block_reduce(arr, block, np.std)
    if blocks.size == 0:
        return 1.0
    return float((blocks < flat_std).mean())


#: 与 `_not_natural_finding` **共用**的前缀。用途：让 `summarize()` 能区分
#: 「这项检查跑了、没发现问题」与「这项检查**根本没跑**」。
#: 两者在报告里都是"没有异常字样"，但含义完全相反，不能混为一谈。
GATE_ABSTAIN_PREFIX = "画面以平坦 / 纯色区域为主"


def _not_natural_finding(check: str, fraction: float) -> Finding:
    """构造"前提不满足"的统一结论 —— 三个检查项共用，措辞一致。"""
    return Finding(
        check=check,
        verdict="无法判定",
        detail=f"{GATE_ABSTAIN_PREFIX}（{fraction:.0%} 的区块几乎没有纹理），本项不适用。",
        caveat="本项的前提是**自然摄影内容**：画面各处都该有传感器噪声与纹理。"
               "截图、纯色背景、纯文字画面（拍白墙、拍文件）不满足这个前提 —— "
               "硬判会把这类图报成异常。前提不满足时不给结论。",
    )


# ---------------------------------------------------------------------------
# 逐项检查
# ---------------------------------------------------------------------------

#: 调用方常见的 MIME 写法 → 本项目按魔术字节判出的规范名。
#:
#: ⚠️ 这是一张**显式别名表，不是"模糊匹配"**。它只吸收**同一个容器**的不同写法
#: （`image/jpg` 与 `image/jpeg` 是同一个东西，浏览器、手机与不少客户端就是这么写的）。
#: **绝不能**写成"前缀相同就算一致"或"只比 `image/` 后面的大类"：
#: 那会把 `image/jpeg2000`、`image/tiff` 这类**确实不同的容器**一并放过，
#: 于是这个检查就废了 —— 而它本来就是为了抓"声明与真实容器不符"。
#: 实测代价：`image/jpg` 是极常见的写法，不归一化时每一张这样的正常图都会
#: 被报「容器不一致」—— 一个纯假阳性，正是本能力最怕的那种失败。
MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
    "image/tif": "image/tiff",
    "image/x-tif": "image/tiff",
    "image/x-bmp": "image/bmp",
    "image/x-ms-bmp": "image/bmp",
    "image/x-webp": "image/webp",
}


def normalize_mime(value: str | None) -> str | None:
    """规范化 MIME：去参数、去空白、转小写、再查别名表。空值返回 None。"""
    if not value:
        return None
    base = value.split(";")[0].strip().lower()
    if not base:
        return None
    return MIME_ALIASES.get(base, base)


def check_container(media_type: str, declared: str | None) -> Finding:
    """容器一致性：声明的 MIME 与真实魔术字节是否一致。"""
    want = normalize_mime(declared)
    if want is None:
        # ⚠️ 调用方**没声明**时不能报「与声明一致」—— 那是在跟一个不存在的声明比对，
        #    是一句听起来让人放心的假话。没有可比对象就如实说没结论。
        return Finding(
            check="container",
            verdict="无法判定",
            detail=f"调用方未声明类型；按魔术字节判出的真实容器是 {media_type}，本项无对比对象。",
            caveat="未声明类型不说明任何问题（调用方常常只给字节或只给 URI）—— "
                   "本项只是丧失了一次交叉验证的机会。",
        )
    if want != media_type:
        return Finding(
            check="container",
            verdict="提示异常",
            strength=0.6,
            detail=f"调用方声明 {declared!r}，但文件真实容器是 {media_type}（按魔术字节判定）。",
            caveat="扩展名 / 声明不符常因转发或另存导致，本身不说明内容被篡改。",
        )
    return Finding(
        check="container",
        verdict="未提示异常",
        detail=f"真实容器 {media_type}，与声明一致。",
        caveat="容器一致只能说明封装没换过，与画面内容真伪无关。",
    )


def check_exif(img: Image.Image, media_type: str) -> Finding:
    """元数据一致性与编辑软件痕迹。"""
    try:
        exif = img.getexif()
        raw_items = {k: v for k, v in exif.items() if not isinstance(v, (bytes, bytearray))}
    except Exception:  # noqa: BLE001
        raw_items = {}

    if not raw_items:
        return Finding(
            check="exif",
            verdict="无法判定",
            strength=None,
            detail=(
                f"该 {media_type} 不含可读 EXIF。"
                + ("（PNG 本就不带 EXIF，属正常）" if media_type == "image/png" else "")
            ),
            caveat="**元数据缺失不等于伪造** —— 社交平台、截图、转存都会剥掉 EXIF。"
                    "它只是缺失，不是证据。",
        )

    hits: list[str] = []
    software = str(raw_items.get(0x0131) or "").strip()
    if software:
        low = software.lower()
        for hint in EDITOR_SOFTWARE_HINTS:
            if hint in low:
                hits.append(f"Software={software!r}")
                break
    make = str(raw_items.get(0x010F) or "")
    model = str(raw_items.get(0x0110) or "")
    if software and not make and not model:
        hits.append(f"有 Software={software!r} 却没有相机 Make/Model")
    if bool(make) ^ bool(model):
        hits.append(f"Make={make!r} 与 Model={model!r} 只出现其一")

    dt, dto = raw_items.get(0x0132), raw_items.get(0x9003)
    if dt and dto and str(dt) != str(dto):
        hits.append(f"DateTime={dt} 与 DateTimeOriginal={dto} 不一致")

    if hits:
        return Finding(
            check="exif",
            verdict="提示异常",
            strength=min(1.0, 0.5 + 0.25 * len(hits)),
            detail="元数据存在编辑 / 矛盾痕迹：" + "；".join(hits) + "。",
            caveat="相机与相册应用本身也会写 Software 字段，且元数据可被任意改写 —— "
                   "**它能被伪造，也能被误伤**，只能作为线索之一。",
        )
    return Finding(
        check="exif",
        verdict="未提示异常",
        detail=f"元数据自洽（Make={make or '—'} / Model={model or '—'}）。",
        caveat="元数据自洽**不等于**画面未经篡改 —— 编辑器与生成模型都能写入完整的相机字段。",
    )


def check_quantization(qt: dict[int, list[int]], media_type: str) -> Finding:
    """JPEG 量化表：是否标准（⇒ 未被编辑软件重写过）。"""
    if media_type != "image/jpeg":
        return Finding(
            check="quantization",
            verdict="无法判定",
            detail=f"{media_type} 不使用 JPEG 量化表，本项不适用。",
            caveat="无损格式没有量化表可查，需改用像素域检查。",
        )
    luma = list(qt.get(0) or [])
    chroma = list(qt.get(1) or []) or None
    if not luma:
        return Finding(
            check="quantization",
            verdict="无法判定",
            detail="未能从该 JPEG 读出亮度量化表。",
            caveat="读不到就不作判断，不用默认值凑数。",
        )
    std_q = match_standard_quality(luma, chroma)
    if std_q is not None:
        return Finding(
            check="quantization",
            verdict="未提示异常",
            detail=f"量化表与标准 JPEG 表一致（≈ quality {std_q}），未被重写过。",
            caveat="标准表说明封装侧没被改写，**不能排除**内容层拼接。",
        )
    return Finding(
        check="quantization",
        verdict="提示异常",
        strength=0.6,
        detail="量化表**非标准** —— 不符合任何一档标准 JPEG 表，"
               "通常意味着图像被编辑软件重新编码（或由非相机管线生成）。",
        caveat="非标准表也可能来自手机厂商私有编码、云相册转码或平台压缩 —— "
               "**只说明它有编码历史，不说明内容被改**。",
    )


def check_ela(img: Image.Image) -> Finding:
    """误差水平分析：重压缩前后差异的分布。"""
    fraction = flat_fraction(img)
    if fraction > NATURAL_MAX_FLAT:
        return _not_natural_finding("ela", fraction)
    arr = np.asarray(img.convert("RGB")).astype(np.float32)
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, "JPEG", quality=90)
    recompressed = np.asarray(Image.open(buf).convert("RGB")).astype(np.float32)
    ela = np.abs(arr - recompressed).max(axis=2)

    blocks = _block_reduce(ela, 16, np.mean).ravel()
    if blocks.size < 16:
        return Finding(
            check="ela", verdict="无法判定",
            detail="图像过小，ELA 分块统计不足。",
            caveat="样本不足时不出结论。",
        )
    p50 = float(np.percentile(blocks, 50))
    p95 = float(np.percentile(blocks, 95))
    ratio = p95 / max(p50, 1e-6)
    hi_frac = float((blocks > max(12.0, p50 * 4)).mean())

    if hi_frac > 0.06 and ratio > 6.0:
        return Finding(
            check="ela", verdict="提示异常", strength=min(1.0, hi_frac * 6),
            detail=f"ELA 分布高度不均：{hi_frac:.1%} 的区块显著高于整体水平"
                   f"（p95/p50={ratio:.1f}）。",
            caveat="**重新保存、裁剪、加水印都会造成同样现象**。ELA 是首轮筛查手段，"
                   "单独命中不构成任何结论。",
        )
    return Finding(
        check="ela", verdict="未提示异常",
        detail=f"ELA 分布较均匀（p95/p50={ratio:.1f}，高值区块 {hi_frac:.1%}）。",
        caveat="分布均匀只说明没有明显的局部重压缩差异，"
               "**不能排除**整图级生成或整体改色。",
    )


def check_noise_residual(img: Image.Image) -> Finding:
    """噪声残差的空间一致性 —— 拼接 / 克隆会在噪声层留下边界。"""
    fraction = flat_fraction(img)
    if fraction > NATURAL_MAX_FLAT:
        return _not_natural_finding("noise_residual", fraction)
    _, res = _residual(img)
    cells = _block_reduce(res, 64, np.std).ravel()
    cells = cells[np.isfinite(cells)]
    if cells.size < 9:
        return Finding(
            check="noise_residual", verdict="无法判定",
            detail="图像过小，噪声分区统计不足。",
            caveat="样本不足时不出结论。",
        )
    p05 = float(np.percentile(cells, 5))
    p95 = float(np.percentile(cells, 95))
    if p95 < 0.6:
        return Finding(
            check="noise_residual", verdict="无法判定",
            detail=f"全图高频残差都很低（p95={p95:.3f}），没有可用于比较的噪声层"
                   "（可能是极平滑的图、或压缩把噪声抹平了）。",
            caveat="没有噪声可比时，本项无法提供信息 —— 不出结论比给个假的比值好。",
        )
    # ⚠️ 分母必须设**绝对下限**。初版直接用 p05 做分母，而拼接图的一侧残差趋近 0，
    #   于是比值炸到 4×10⁶ —— 数字离谱到没法写进报告，也没法设阈值。
    #   0.25 是 JPEG 量化噪声的量级：低于它的残差不算「传感器噪声」。
    spread = p95 / max(p05, 0.25)
    if spread > 6.0:
        return Finding(
            check="noise_residual", verdict="提示异常", strength=min(1.0, spread / 20.0),
            detail=f"噪声强度在画面内高度不均（分区噪声 p95/p05={spread:.1f}），"
                   "存在与拼接 / 克隆一致的空间不连续性。",
            caveat="**真实的明暗过渡、景深虚化、强降噪、局部保边平滑都会造成噪声不均** —— "
                   "本项须与 ELA、块效应网格同时命中才有参考价值。",
        )
    return Finding(
        check="noise_residual", verdict="未提示异常",
        detail=f"噪声强度空间分布较一致（p95/p05={spread:.1f}）。",
        caveat="一致只说明噪声层没有明显断层，**不能排除**整图级生成。",
    )


def _avg_ranks(values: np.ndarray) -> np.ndarray:
    """**平均秩**（并列取平均），归一化到 [0, 1]。

    秩的好处：**有界**。一个极端离群值只能拿到秩 1.0，不能像在均值里那样
    把整档拉走；而大片"恰好为 0"的平坦位置统一拿到同一个较低的秩，
    也就不会像低分位数归一那样被整体压成 0。
    """
    n = values.size
    if n < 2:
        return np.zeros(n, dtype=np.float64)
    ordered = np.sort(values)
    lo = np.searchsorted(ordered, values, side="left")
    hi = np.searchsorted(ordered, values, side="right")
    return (lo + hi - 1) / 2.0 / (n - 1)


def _grid_level(
    energy: np.ndarray,
    idx: np.ndarray,
    how: str,
    cap: float,
    ranks: np.ndarray,
) -> float:
    """一档（一组位置）的「梯度能量水平」。

    候选统计量**全部实测过**（见 `run/diag_grid_samples.py`），淘汰理由记在这里，
    免得以后有人再"优化"回去：

      * `mean`   —— 初版。被**单个强边**带偏：合成图在 `xx=396` 有一道台阶边，
                    `396 % 8 == 4` → offset 4 那一档跳到 2.36 倍 → 报「网格偏移 5 像素」。
      * `median` / `cap90` / `cap95` —— **在本类图上整体塌成 0**：
                    约九成位置的梯度**恰好为 0**（JPEG 量化把平滑块压平），
                    于是任何"低分位数归一"的除数都是 0，比值退化成 0/0。
      * `rank`（**采用**）—— 平均秩，**有界**，既不被极值放大、也不依赖尺度，
                    且"大片 0"只会统一拿到同一个低秩，不会污染比值。
    """
    if how == "mean":
        return float(np.mean(energy[idx]))
    if how == "median":
        return float(np.median(energy[idx]))
    if how == "trim90":
        ordered = np.sort(energy[idx])
        k = max(1, int(round(ordered.size * 0.10)))
        return float(np.mean(ordered[: ordered.size - k]))
    if how in ("cap90", "cap95", "cap98", "cap99"):
        return float(np.mean(np.minimum(energy[idx], cap)))
    if how == "rank":
        return float(np.mean(ranks[idx]))
    if how == "frac":
        # 该档中"梯度能量超过基准线"的位置占比。**这既有抗离群性、又有动态范围**：
        # 一根极端强边只能把占比抬高 1/N（N≈90 → 0.011），而真实网格会让整档
        # 大部分位置的梯度一起抬高。`cap` 在这里复用为"基准线"。
        return float(np.mean(energy[idx] > cap))
    raise ValueError(f"未知统计量 {how!r}")


def block_grid_ratios(
    arr: np.ndarray,
    *,
    stat: str = "rank",
) -> list[tuple[str, dict[int, float]]]:
    """每轴的 8 个偏移 → 「该相位梯度能量 / 其余相位能量」比值。

    **这是块网格判定的唯一计算入口**（`check_block_grid` 与 `run/diag_grid_samples.py`
    都调它）—— 诊断脚本另写一份必然与检测代码漂移，那就等于没诊断。

    索引约定（最容易搞反的地方）：因为用 `np.diff` 求梯度，`diff[i]` 是像素 i 与 i+1
    之间的跳变。JPEG 块边界在像素 7|8、15|16… 之间 ⇒ **对齐时能量峰值落在 offset 7**。

    统计量默认 `rank`，其余取值只作校准旋钮（`stat=` 是给诊断脚本用的）。
    """
    out: list[tuple[str, dict[int, float]]] = []
    for axis, label in ((1, "横向"), (0, "纵向")):
        energy = np.abs(np.diff(arr, axis=axis)).mean(axis=1 - axis)
        if energy.size < 64:
            continue
        idx_groups = {off: np.arange(off, energy.size, 8) for off in range(8)}
        if any(g.size < 4 for g in idx_groups.values()):
            continue
        pct = {"cap98": 98.0, "cap99": 99.0, "cap90": 90.0, "cap95": 95.0}.get(stat)
        if stat == "frac":
            positive = energy[energy > 0]
            cap = float(np.median(positive)) if positive.size else 0.0
        elif pct is not None:
            cap = float(np.percentile(energy, pct))
        else:
            cap = float(np.max(energy))
        ranks = _avg_ranks(energy)
        levels: dict[int, float] = {}
        for offset in range(8):
            levels[offset] = _grid_level(energy, idx_groups[offset], stat, cap, ranks)
        if max(levels.values()) <= 0:      # 全档都是 0 → 本轴无信号，不参与断言
            continue
        ratios: dict[int, float] = {}
        for offset in range(8):
            rest = np.concatenate([idx_groups[o] for o in range(8) if o != offset])
            base = _grid_level(energy, rest, stat, cap, ranks)
            ratios[offset] = levels[offset] / max(base, 1e-9)
        out.append((label, ratios))
    return out


def _block_grid_phase(
    profiles: list[tuple[str, dict[int, float]]],
) -> tuple[int | None, int | None]:
    """把两轴比值折成 (横向相位, 纵向相位)；不足以判定时对应位置为 None。"""
    by_label = {label: ratios for label, ratios in profiles}
    horizons = []
    for label in ("横向", "纵向"):
        ratios = by_label.get(label)
        horizons.append(max(ratios, key=lambda k: ratios[k]) if ratios else None)
    return horizons[0], horizons[1]


#: 块网格判据的四个阈值 —— **全部由 `run/diag_grid_samples.py` 的实测数据定**，
#: 不是拍脑袋。改任何一个都要重跑那个脚本复核全部样本。
GRID_SIGNAL_MIN = 1.03      # 秩口径下，峰值低于此值视为「没有可判的网格」
GRID_RANK_MARGIN = 1.20     # 秩口径下，非对齐峰须高出对齐位置 20%
GRID_RANK_ABS = 1.05        # 且绝对值不低于 1.05
GRID_FRAC_MIN = 1.30        # 比例口径下，非对齐峰的绝对水平
GRID_FRAC_MARGIN = 1.30     # 且须高出对齐位置 30%

#: 复制-移动判据：**三个条件必须同时成立**（票数 + 空间聚集 + 二维性）。
CM_MIN_VOTES = 12           # 支持同一位移向量的高纹理块对数下限
CM_CLUSTER_RADIUS = 8       # 「聚集」的判定半径（块），±8 块 ≈ ±64 像素
CM_MIN_CLUSTER = 12         # 该向量在某个半径内能凑出的最多点数下限
CM_MIN_SPAN = 3             # 该簇在**两轴**上的跨度下限（块）—— 见下
CM_MIN_BLOCK_STD = 1.5      # 块内残差标准差下限（低于此视为"平坦块"，没有可比内容）
CM_FEATURE_STEP = 0.5       # 块特征量化步长 —— **必须细**，见 check_copy_move 里的说明

#: 复制-移动检测的块参数
CM_BLOCK = 16               # 特征块边长（像素）
CM_BLOCK_STRIDE = 8         # 块步长（像素）—— 也就是"位移必须是它的整数倍"的那个步长


def copy_move_cluster(
    points: list[tuple[int, int]],
    stride: int,
    radius: int = CM_CLUSTER_RADIUS,
) -> tuple[int, int, int]:
    """把一列同位移的匹配点归成「最密的一簇」，返回 (点数, 横向跨度, 纵向跨度)。

    ⚠️ **为什么必须看两轴跨度**（第三轮校准的关键修正）：
    合成图在 `xx≈396` 有一道**贯穿整幅的竖直边缘**。直线沿自身方向平移任意距离
    都与自己重合 —— 于是"沿边缘的几十个块"在某个横向位移向量下全部配对成功，
    被报成克隆（实测 N1/N2/P3 全部误报，位移向量清一色是 `(Δx, 0)`）。
    区分点很干净：**被克隆的区域是 2-D 面片，而自相似的边缘是 1-D 直线**。
    所以除了"凑得够多"，还要求这一簇在**两个方向上都铺开**。
    """
    if len(points) < 2:
        return len(points), 0, 0
    pts = np.asarray(points, dtype=np.int64) // stride
    dist = np.abs(pts[:, None, :] - pts[None, :, :]).max(axis=2)
    counts = (dist <= radius).sum(axis=1)
    center = int(np.argmax(counts))
    members = pts[dist[center] <= radius]
    span_x = int(members[:, 0].max() - members[:, 0].min())
    span_y = int(members[:, 1].max() - members[:, 1].min())
    return int(counts[center]), span_x, span_y


def check_block_grid(img: Image.Image, media_type: str) -> Finding:
    """JPEG 8×8 块效应网格：位置是否对齐、是否存在**第二个非对齐相位**。

    ## 判据为什么要两套口径 × 两轴一致

    这一项前后校准了四轮，每一轮的失败原因都记在下面，因为它决定了实现形状：

      1. **对齐索引**：`np.diff` 的索引 7、15、23… 才对应块边界 ⇒ 对齐时 offset 必须是 7。
      2. **均值口径会被单个强边带偏**：合成图在 `xx=396` 有一道台阶边，`396 % 8 == 4`
         → offset 4 那一档跳到 2.36 倍 → 报「偏移 5 像素」。**假阳性**。
      3. **中位数 / 低分位截尾会整体塌成 0**：约九成位置的梯度**恰好为 0**
         （JPEG 量化把平滑块压平），任何"低分位数归一"的除数都是 0 → 0/0。
      4. 于是分成**两套口径**，各用各的长处：
         * `rank`（平均秩，**有界**）→ 相位可信、从不误报，但动态范围被压扁（1.00–1.08）；
         * `frac`（超过基准线的位置**占比**）→ 动态范围够、能抓真偏移，但计数小时噪声大。
      5. **最后一道闸门：两轴必须一致。** 真实的块网格相位是 2-D 一致的；
         内容噪声不是。实测：P4（裁剪 4px 后重编码）两轴 frac 都落在 offset 3（真偏移），
         而 N2/N3/P3/P5 的噪声例两轴 argmax 互不一致（5vs2、7vs0、7vs3、7vs3）
         —— **这一条把全部假阳性一次清掉**。
      6. **偏移量是 `(7 - φ) % 8`**，不是 `(φ + 1) % 8`：两者在 φ=3 时同为 4
         （所以早期没暴露），φ=6 时分别给 1 与 7，后者是错的。

    结论口径：**只在证据成立时断言**；测不出就判「无法判定」，并说明为什么。
    """
    if media_type not in ("image/jpeg", "image/webp"):
        return Finding(
            check="block_grid", verdict="无法判定",
            detail=f"{media_type} 无 DCT 块网格，本项不适用。",
            caveat="本项只对**有损 DCT 编码**有意义（JPEG / 有损 WebP）。"
                   "⚠️ 无损格式（含**无损 WebP**）没有块网格 —— 这类图不会命中判据，"
                   "会走到「信号太弱 → 无法判定」，结论是对的，"
                   "但**别把它读成「验过了、没问题」**。",
        )
    arr = np.asarray(img.convert("L")).astype(np.float32)
    if arr.shape[0] < 64 or arr.shape[1] < 64:
        return Finding(
            check="block_grid", verdict="无法判定",
            detail="图像过小，网格统计不可靠。",
            caveat="样本不足时不出结论。",
        )

    rank_profiles = block_grid_ratios(arr, stat="rank")
    frac_profiles = block_grid_ratios(arr, stat="frac")
    if not rank_profiles:
        return Finding(
            check="block_grid", verdict="无法判定",
            detail="两轴都不足以计算网格比值。",
            caveat="样本不足时不出结论。",
        )

    rank_peak = max(max(r.values()) for _l, r in rank_profiles)
    rank_h, rank_v = _block_grid_phase(rank_profiles)
    frac_h, frac_v = _block_grid_phase(frac_profiles)
    frac_by = {label: r for label, r in frac_profiles}
    rank_by = {label: r for label, r in rank_profiles}

    def _rank_shift_ok(label: str, phase: int | None) -> bool:
        if phase is None or phase == 7:
            return False
        r = rank_by.get(label)
        return bool(r) and r[phase] >= max(GRID_RANK_ABS, r[7] * GRID_RANK_MARGIN)  # type: ignore[operator]

    def _frac_shift_ok(label: str, phase: int | None) -> bool:
        if phase is None or phase == 7:
            return False
        r = frac_by.get(label)
        return bool(r) and r[phase] >= GRID_FRAC_MIN and r[phase] >= r[7] * GRID_FRAC_MARGIN

    # ---- 判据 A（主）：秩口径 + 两轴一致 ----
    if rank_h is not None and rank_h == rank_v and _rank_shift_ok("横向", rank_h) and _rank_shift_ok("纵向", rank_v):
        shift = (7 - rank_h) % 8
        return Finding(
            check="block_grid", verdict="提示异常", strength=0.4,
            detail=f"两轴的块网格峰值都落在**非对齐位置**（偏移 {shift} 像素，"
                   f"横向比值 {rank_by['横向'][rank_h]:.3f}、纵向 {rank_by['纵向'][rank_v]:.3f}），"
                   "与「裁剪 / 平移后重新编码」一致。",
            caveat="裁剪与平移都是完全正常的编辑操作；本项只说明**有过几何处理**，"
                   "不说明内容被改。",
        )

    # ---- 判据 B（补充）：比例口径 + 两轴一致 ----
    # 抓的是「新旧两个网格并存」：新网格对齐、旧网格是非对齐的残余 ——
    # 这正是裁剪后重编码的指纹，而新网格太弱时秩口径看不见它（P4 就是这种情况）。
    if (
        frac_h is not None and frac_h == frac_v and _frac_shift_ok("横向", frac_h) and _frac_shift_ok("纵向", frac_v)
    ):
        shift = (7 - frac_h) % 8
        return Finding(
            check="block_grid", verdict="提示异常", strength=0.4,
            detail=f"检出**两个不同相位的块网格**：主网格对齐在标准位置（较弱），"
                   f"另有一个相位偏移 {shift} 像素的残余网格（横向 {frac_by['横向'][frac_h]:.2f}、"
                   f"纵向 {frac_by['纵向'][frac_v]:.2f}，均显著高于对齐位置）。"
                   "与「图像被裁剪 / 平移后**再次编码**」一致。",
            caveat="裁剪与平移都是完全正常的编辑操作；本项只说明**有过几何处理**，"
                   "不说明内容被改。局部拼接造成的**局部**网格偏移是本项的盲区"
                   "（全局统计会被未位移的多数区域稀释）。",
        )

    if rank_peak < GRID_SIGNAL_MIN:
        return Finding(
            check="block_grid", verdict="无法判定",
            detail=f"块网格信号太弱（秩口径峰值仅 {rank_peak:.3f}），无法据此判断网格是否对齐 —— "
                   "高质量 JPEG 的网格本就微弱，强判会把正常图报成「网格偏移」。",
            caveat="**信号弱不等于有异常**：这里刻意不出结论。",
        )

    # ⚠️ 只有**两轴都给出 offset 7** 才敢说「对齐」。一轴说 7、另一轴说 2 时，
    #   既不能断言偏移（两轴不一致，够不上判据），**也不能断言对齐**
    #   —— 初版在这里写了"在标准位置对齐"，与同一句话里的相位数值自相矛盾。
    #   宁可判「无法判定」：对齐结论同样是一种"安全结论"，不能凑合。
    if rank_h != 7 or rank_v != 7:
        return Finding(
            check="block_grid", verdict="无法判定",
            detail=f"两轴的相位估计不一致（横向 {rank_h}、纵向 {rank_v}），"
                   "够不上「非对齐」的判据，也不足以认定「对齐」—— 本项不给出结论。",
            caveat="相位估计不一致通常意味着该图的网格信号被画面内容淹没。",
        )

    return Finding(
        check="block_grid", verdict="未提示异常",
        detail=f"8×8 块网格在标准位置对齐（秩口径峰值 {rank_peak:.3f}，两轴相位均为 7）。",
        caveat="网格对齐只说明压缩结构未被破坏，**不能排除**内容层拼接或生成。",
    )


def check_resampling(img: Image.Image) -> Finding:
    """重采样痕迹：二阶差分频谱里的周期性峰（缩放 / 旋转 / 拼接的副产物）。"""
    arr = np.asarray(img.convert("L")).astype(np.float32)
    if arr.shape[0] < 128 or arr.shape[1] < 128:
        return Finding(
            check="resampling", verdict="无法判定",
            detail="图像过小，频谱峰检测不可靠。",
            caveat="样本不足时不出结论。",
        )
    row = arr[arr.shape[0] // 4]
    d2 = np.diff(row, n=2)
    if d2.size < 128:
        return Finding(
            check="resampling", verdict="无法判定",
            detail="可用扫描行太短。", caveat="样本不足时不出结论。",
        )
    spectrum = np.abs(np.fft.rfft(d2 * np.hanning(d2.size)))
    band = spectrum[8:]
    if band.size < 16:
        return Finding(
            check="resampling", verdict="无法判定",
            detail="有效频段过窄。", caveat="样本不足时不出结论。",
        )
    prominence = float(band.max() / max(float(np.median(band)), 1e-9))
    if prominence > 40.0:
        return Finding(
            check="resampling", verdict="提示异常", strength=0.4,
            detail=f"频谱中检出强周期峰（峰/中位数={prominence:.1f}），与插值重采样一致。",
            caveat="**这条噪声很大**：周期性纹理（栏杆、条纹、摩尔纹）本身就有强峰。"
                   "仅作方向性参考，不作为判定依据。",
        )
    return Finding(
        check="resampling", verdict="未提示异常",
        detail=f"未检出显著周期峰（峰/中位数={prominence:.1f}）。",
        caveat="未检出只说明在该扫描行上没看到，**不代表**全图未重采样。",
    )


def check_flat_noise(
    img: Image.Image,
    media_type: str,
    jpeg_quality: int | None = None,
) -> Finding:
    """平坦区噪声是否「过于干净」—— 传感器读数噪声缺失是 AI 生成图的已知线索。

    ⚠️ **本项必须先过一道闸门**：JPEG 的量化过程**本身就会抹掉平坦区噪声**。
    初版没这道闸门，于是每一张正常 JPEG 都被报成「噪声极低」——
    用「噪声缺失」去判 JPEG 是无效的，这是本项最容易误报的地方。
    所以：JPEG 且质量档位 <95（或读不出档位）时，直接判「无法判定」。
    """
    if media_type == "image/jpeg" and (jpeg_quality is None or jpeg_quality < 95):
        return Finding(
            check="flat_noise", verdict="无法判定",
            detail=(
                "该 JPEG 的压缩过程本身就会抹掉平坦区噪声"
                + (f"（量化表档位 ≈ quality {jpeg_quality}）"
                   if jpeg_quality else "（量化表非标准，档位未知）")
                + "，本项不适用。"
            ),
            caveat="用「噪声缺失」判断 JPEG 是无效的 —— 压缩已经先把噪声去掉了。"
                   "本项只在无损容器（或 ≥95 的高质量 JPEG）上才有意义。",
        )
    arr, res = _residual(img)
    block_std = _block_reduce(arr, 16, np.std)
    noise_std = _block_reduce(res, 16, np.std)
    if block_std.size < 16 or block_std.shape != noise_std.shape:
        return Finding(
            check="flat_noise", verdict="无法判定",
            detail="图像过小，平坦区统计不足。",
            caveat="样本不足时不出结论。",
        )
    flat_mask = block_std < 2.0
    if int(flat_mask.sum()) < 4:
        return Finding(
            check="flat_noise", verdict="无法判定",
            detail="几乎找不到平坦区域（画面处处有纹理），本项不适用。",
            caveat="没有平坦区可比时不作判断。",
        )
    flat_noise = float(np.median(noise_std[flat_mask]))
    if flat_noise < 0.35:
        return Finding(
            check="flat_noise", verdict="提示异常", strength=0.5,
            detail=f"平坦区的噪声水平极低（中位残差 {flat_noise:.3f}），"
                   "缺少真实传感器应有的读数噪声。",
            caveat="**低质量 JPEG、强降噪、纯色背景都会造成同样结果** —— "
                   "这是已知的 AI 线索，但绝不是充分条件。",
        )
    return Finding(
        check="flat_noise", verdict="未提示异常",
        detail=f"平坦区保有读数噪声（中位残差 {flat_noise:.3f}）。",
        caveat="有噪声也不能排除生成 —— 现代生成模型与后处理都能补回噪声。",
    )


def check_copy_move(img: Image.Image) -> Finding:
    """复制-移动检测：是否存在**占支配地位的位移向量**。

    判据刻意**不是**「有重复块」—— 天空、墙面、渐变区本来就有海量相似块
    （初版就是这样：一张干净的合成图被报出 397 组「克隆」）。
    真正稳健的判据是经典 CMFD 的做法：**同一个位移向量被足够多的高纹理块共同支持**。
    平坦块直接剔除（它没有可比对的「内容」），超过 64 成员的桶也剔除（那是均匀纹理）。
    """
    fraction = flat_fraction(img)
    if fraction > NATURAL_MAX_FLAT:
        return _not_natural_finding("copy_move", fraction)
    _, res = _residual(img)
    bs, stride = CM_BLOCK, CM_BLOCK_STRIDE
    h, w = res.shape
    if h < bs * 6 or w < bs * 6:
        return Finding(
            check="copy_move", verdict="无法判定",
            detail="图像过小，块特征匹配不可靠。",
            caveat="样本不足时不出结论。",
        )

    keys: dict[tuple[int, ...], list[tuple[int, int]]] = {}
    for y in range(0, h - bs + 1, stride):
        for x in range(0, w - bs + 1, stride):
            blk = res[y:y + bs, x:x + bs]
            if float(blk.std()) < CM_MIN_BLOCK_STD:
                continue
            # ⚠️ **特征必须够细**（第四轮校准，也是这一项最后才修对的地方）。
            #   取 16 个 4×4 子块均值作特征，再按 `CM_FEATURE_STEP` 量化。
            #   原来量化步长写的是 `/ 2.0` —— 残差本身只有 2~4 的标准差，
            #   4×4 均值再除以 2 之后几乎全塌进同一个桶：实测**干净样本上有
            #   368~835 个多成员桶**，全是低对比块的偶然碰撞，于是干净图被报克隆。
            #   改成 `/ 0.5`（步长 0.5）后：**四张负对照的碰撞桶数变成 0**，
            #   而真克隆（P2）仍有 25 组密集匹配 —— 判据的分离度来自特征粒度，
            #   不是来自调阈值（这是两种完全不同的修法，别搞混）。
            q = blk.reshape(4, 4, 4, 4).mean(axis=(1, 3)) / CM_FEATURE_STEP
            pts = keys.setdefault(tuple(int(round(float(v))) for v in q.ravel()), [])
            if len(pts) < 64:
                pts.append((x, y))

    votes: dict[tuple[int, int], int] = {}
    sources: dict[tuple[int, int], list[tuple[int, int]]] = {}
    budget = 200_000
    for pts in keys.values():
        if len(pts) < 2:
            continue
        for i in range(len(pts)):
            xi, yi = pts[i]
            for j in range(i + 1, len(pts)):
                dx, dy = pts[j][0] - xi, pts[j][1] - yi
                if abs(dx) + abs(dy) < 64:
                    continue
                vec = (int(round(dx / stride)), int(round(dy / stride)))
                votes[vec] = votes.get(vec, 0) + 1
                sources.setdefault(vec, []).append((xi, yi))
                budget -= 1
            if budget <= 0:
                break
        if budget <= 0:
            break

    if not votes:
        return Finding(
            check="copy_move", verdict="未提示异常",
            detail="未检出空间远离的同特征块对（画面内没有可支持克隆判定的重复纹理）。",
            caveat="未检出只说明该尺度下没有明显克隆，**不代表**没有做过局部修改。",
        )

    # ⚠️ **不能只看票数最高的那个向量**（第二轮校准的关键修正）。
    #   票数最高的往往是**周期纹理**造成的弥散向量（实测：干净图上 Δ=(-112,8) 得 39 票），
    #   而真正的克隆向量票数更少却**空间高度集中**，排在后面 ——
    #   初版只对 argmax 做校验，于是真克隆被一个噪声向量挡住了。
    #
    # ⚠️⚠️ **也不能用「总票数 ÷ 包围盒」当聚集度**（第三轮校准的修正）。
    #   实测 P2：真位移 Δ=(312,0) 有 52 票，但包围盒被**全图碰巧同位移的噪声配对**撑满，
    #   密度只有 0.03 —— 真克隆被自己的噪声稀释掉了。
    #   正确判据是**最密簇**：在某个半径内能同时凑出多少个点。真克隆的源块挤在
    #   一个 64×64 的小区域里，半径内能凑出几十个；周期纹理的点则铺满全图，凑不出几个。
    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    best_cluster = 0
    best_span = 0
    best_info: tuple[tuple[int, int], int, int, int, int] | None = None
    for vec, count in ranked[:20]:
        if count < CM_MIN_VOTES:
            break
        cluster, span_x, span_y = copy_move_cluster(sources[vec][:500], stride)
        # 取"三维度都达标"的里面簇最大的那个；只看 2-D 面片，不看直线
        if cluster >= CM_MIN_CLUSTER and min(span_x, span_y) >= CM_MIN_SPAN:
            if cluster > best_cluster:
                best_cluster, best_span = cluster, min(span_x, span_y)
                best_info = (vec, count, cluster, span_x, span_y)
        elif cluster > best_cluster:
            # 记下最好的一次（用于"未提示异常"的说明文案）
            best_cluster, best_span = cluster, min(span_x, span_y)

    if best_info is not None:
        vec, count, cluster, span_x, span_y = best_info
        return Finding(
            check="copy_move", verdict="提示异常",
            strength=min(1.0, cluster / 40.0),
            detail=f"位移向量 Δ=({vec[0] * stride}, {vec[1] * stride}) 像素获得 {count} 组高纹理块支持，"
                   f"其中 **{cluster} 组集中在同一个 2-D 区域**（{span_x}×{span_y} 块，"
                   f"半径 ±{CM_CLUSTER_RADIUS * stride} 像素内）—— 与「整块复制-移动」一致。",
            caveat="**规则性重复的天然纹理**（砖墙、栏杆、织物、成排树叶）也可能产生"
                   "单一位移向量 —— 本项必须配合视觉核查，不能单独定论。"
                   "另注意本实现以 8 像素为块步长匹配，**位移不是 8 的整数倍时灵敏度会下降**。",
        )

    vec, best = ranked[0]
    return Finding(
        check="copy_move", verdict="未提示异常",
        detail=f"最强位移向量 Δ=({vec[0] * stride}, {vec[1] * stride}) 有 {best} 组支持，"
               f"但最密的簇只有 {best_cluster} 组、两轴跨度仅 {best_span} 块"
               f"（阈值：簇 ≥{CM_MIN_CLUSTER}、跨度 ≥{CM_MIN_SPAN}）—— "
               "匹配点要么太散、要么呈直线（与被自身方向平移重合的边缘一致），"
               "而非整块克隆。",
        caveat="周期性纹理与长直边缘会使本项沉默（宁可漏报，也不要在这类图上误报）。",
    )


# ---------------------------------------------------------------------------
# 汇总与升级建议
# ---------------------------------------------------------------------------

def _gate_rationale(gated: list[str]) -> str:
    """像素域检查前提不满足时的措辞。

    ⚠️ 单独抽出来是为了让它能被**逐字**保留：这段文案是拆出
    `legal_acps.verdict` 之前就在用的原文，一个字都不该改
    （它已被既有断言与文档锚定）。
    """
    return (
        f"本图不满足像素域检查的前提：{len(gated)} 项因"
        f"「非自然摄影内容」而无法执行（{'、'.join(gated)}）。"
        "而这些恰恰是能发现内容层拼接 / 克隆的项目，"
        "**所以本报告既不能说「正常」也不能说「异常」**。"
        "截图、拍白板、拍文件、界面截图这类输入应当直接走复检路径。"
    )


def summarize(findings: list[Finding]) -> tuple[str, str]:
    """把逐项发现折成综合线索等级。**纯函数、可断言、与模型无关**。

    ⚠️ 有一条**必须排在"算分数"之前**的规则：**前提不满足时不允许出「低」**。

    只数"未提示异常"的条数，会把一张 92% 纯色的截图报成「低」——
    而「低」读起来就是"没问题"。真相是：**能发现内容层拼接 / 克隆的那几项
    （`ela` / `noise_residual` / `copy_move`）整项没跑**，剩下还能出结论的
    只有 `container` / `quantization` / `exif` 这类**封装层**检查，
    而它们本来就不检测画面内容（这一点每条 caveat 里都写着）。

    把「测不了」说成「没问题」，是本能力最危险的失败形态 ——
    它会给用户一个**看起来很安全、实际什么也没验过**的结论。

    ⚠️ **判据本身不在本函数里**，在 `legal_acps.verdict.summarize_findings()`：
    音频鉴伪链有一模一样的需求（弃权比例只会更高），两边各写一份就会漂移成
    "同一种失败在图像链是「无法判定」、在音频链是「低」"，而报告表面看不出异样。
    本函数只负责把图像侧的**数据**（权重表 / 弃权前缀 / 措辞）喂给那份唯一实现。
    """
    return summarize_findings(
        findings,
        weights=CHECK_WEIGHTS,
        gate_prefixes=(GATE_ABSTAIN_PREFIX,),
        gate_rationale=_gate_rationale,
        subject="该图",
        finding_noun="伪造认定",
    )


def plan_upgrades(level: str) -> list[UpgradePath]:
    """更准但更贵的复检路径。**由程序按固定阶梯生成**，不由模型即兴发挥。

    成本口径取自公开资料（2026-09）；厂商自报准确率仅供参考。

    ⚠️ **每一档都返回完整阶梯，不随等级裁剪**（这一条是踩过才改的）：

    早先的实现按等级裁：低 → 前 2 条、中 → 前 3 条、高 → 全部。
    结果是**「线下司法鉴定」这一档在最常见的「低」里被悄悄删掉了** ——
    而它恰恰是**唯一有法律证明力的路径**，也是用户最需要提前知道的两件事
    （要多少钱、要等多久、以及"原始载体现在就必须保全"）。

    `level` 参数保留下来是为了**将来**可能的分级措辞（例如对「高」加一句
    "建议尽快咨询律师"），但**绝不用来删减条目**：删条目等于对用户隐藏
    唯一可用的救济路径 —— 那不是"精简"，是失职。
    """
    ladder = [
        UpgradePath(
            tier="online_free",
            name="C2PA Content Credentials Verify / Google SynthID",
            cost="免费",
            turnaround="秒级",
            when_to_use=(
                "**先做这一步**。若图像带 C2PA 签名且签名有效，可得到接近确定的来源结论；"
                "但注意「没有签名 ≠ 是假的」—— 截图、转存、平台压缩都会剥掉签名。"
            ),
        ),
        UpgradePath(
            tier="online_paid",
            name="Hive Moderation / Optic (AI or Not)",
            cost="有免费额度，付费约 $29–199/月起",
            turnaround="秒级～分钟级",
            when_to_use="需要快速批量筛查、或要识别**具体是哪个生成模型**时使用。",
        ),
        UpgradePath(
            tier="online_paid",
            name="Truepic / deepidv 等逐次计费 API",
            cost="Truepic 约 $0.02/次；deepidv 约 $0.50/次",
            turnaround="分钟级",
            when_to_use=(
                "单张图需要一份**可留存、可复核**的第三方检测报告时使用。"
                "⚠️ 厂商自报准确率 96–99%，但独立测评显示顶级检测器仍有 15–20% 假阴性。"
            ),
        ),
        UpgradePath(
            tier="offline_forensic",
            name="声像资料司法鉴定（图像真实性鉴定）",
            cost="约 2500–4000 元/组（发改价格规〔2020〕1632 号）",
            turnaround="一般 30 个工作日",
            when_to_use=(
                "**涉及法律责任时唯一有证明力的路径**。须委托省级以上司法行政机关"
                "登记在册、业务范围含「图像鉴定」或「电子数据鉴定」的机构，"
                "并提交**原始载体**（只读介质、保留序列号）—— "
                "所以原始文件现在就要保全，事后无法补。"
            ),
        ),
    ]
    return list(ladder)


def analyze(
    data: bytes,
    *,
    declared_mime: str | None = None,
    name: str | None = None,
) -> ImageForensicsReport:
    """对一张图的原始字节做本地初检，返回结构化报告。

    只接受字节 —— 下载 / 取图是调用方的事，本函数**不联网**（因此可离线断言）。
    """
    media_type = sniff_media_type(data)
    if not media_type.startswith("image/"):
        raise ValueError(
            f"不是可识别的图像容器（按魔术字节判定为 {media_type}）"
            + (f"，文件名 {name}" if name else "")
        )

    full, qt, size = open_image(data)
    img, cropped = crop_for_analysis(full)
    # JPEG 的质量档位（读不出档位时留 None）—— 用来决定「平坦区噪声」这一项是否适用。
    luma_qt = list(qt.get(0) or [])
    jpeg_quality = (
        match_standard_quality(luma_qt, list(qt.get(1) or []) or None)
        if media_type == "image/jpeg" and luma_qt
        else None
    )
    findings: list[Finding] = [
        check_container(media_type, declared_mime),
        check_exif(full, media_type),
        check_quantization(qt, media_type),
        check_ela(img),
        check_noise_residual(img),
        check_block_grid(img, media_type),
        check_resampling(img),
        check_flat_noise(img, media_type, jpeg_quality),
        check_copy_move(img),
    ]
    level, rationale = summarize(findings)
    return ImageForensicsReport(
        media_type=media_type,
        byte_size=len(data),
        sha256=sha256_of(data),
        width=int(size[0]),
        height=int(size[1]),
        crop_note=(
            f"像素域检查使用**中心裁剪**至最长边 {ANALYSIS_MAX_SIDE}px（刻意不缩放，"
            "以免抹掉块网格与重采样伪影）；上列 width/height 是**原图**尺寸。"
            if cropped
            else "像素域检查在**原尺寸**上进行。"
        ),
        findings=findings,
        suspicion=level,  # type: ignore[arg-type]
        suspicion_rationale=rationale,
        upgrades=plan_upgrades(level),
        chain_of_custody=list(CHAIN_OF_CUSTODY),
        disclaimer=DISCLAIMER,
    )


__all__ = [
    "ANALYSIS_MAX_SIDE",
    "CHAIN_OF_CUSTODY",
    "CHECK_WEIGHTS",
    "DISCLAIMER",
    "EDITOR_SOFTWARE_HINTS",
    "MAX_IMAGE_PIXELS",
    "MIME_ALIASES",
    "STD_CHROMA_QT",
    "STD_LUMA_QT",
    "analysis_crop_box",
    "analyze",
    "block_grid_ratios",
    "crop_for_analysis",
    "flat_fraction",
    "match_standard_quality",
    "normalize_mime",
    "open_image",
    "pixel_budget_exceeded",
    "plan_upgrades",
    "sha256_of",
    "sniff_media_type",
    "summarize",
]
