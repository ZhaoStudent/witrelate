"""帧层：把容器里零解码取到的帧**接进已有的图像鉴伪链**，并做跨帧比对。

════════════════════════════════════════════════════════════════════════
为什么这一层要单独一个文件（而不是塞进 `decode.py` 或 `forensics.py`）
════════════════════════════════════════════════════════════════════════

因为它是**唯一**需要 Pillow / numpy 的一层：

    `decode.py`   纯标准库      —— 容器解析 + 零解码取帧（MJPEG / covf 封面帧）
    `frames.py`   Pillow+numpy  —— 动画 GIF 的逐帧光栅化 + 起图像链做像素域分析
    `forensics.py` numpy        —— 判定（其中帧层那一项委托给本模块）

把 Pillow 的导入关在这一个文件里，`legal_acps/video/__init__.py` 的惰性导出
才有意义：`agents.py` 只取 `video.contracts` 的请求模型，永远不会走到这里。

════════════════════════════════════════════════════════════════════════
两条纪律
════════════════════════════════════════════════════════════════════════

1. **绝不为「给模型看」而重编码**。取到什么字节就分析什么字节。
   把一帧重新编码成 JPEG 再送进图像链，会让它的量化表 / 块网格两项
   量到**我们自己编码器的痕迹** —— 那是纯人造的假线索，而它看起来
   和真线索一模一样。CC 这条对 GIF 尤其重要：GIF 帧是调色板 + LZW，
   光栅化之后必须用 **PNG（无损）** 落地，不能用 JPEG。

2. **「抽了 4 帧」必须说成「抽了 4 帧」**。抽样不是全覆盖：
   一段 10 分钟的视频里，被剪掉的那 3 秒大概率不在这 4 帧上。
   所以帧层结论的 caveat 里必须写死这一条，且 `analyzed` 与总帧数的
   差距要能被读者看见。
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any, Sequence

from ..evidence.contracts import Finding
from ..verdict import ABNORMAL, ABSTAIN, stable_reason
from .decode import ExtractedFrame, MAX_FRAMES, MAX_FRAME_BYTES

#: 帧层的**材料级闸门前缀** —— 与图像链 / 音频链同一套语义：
#: 取不到任何可分析的帧 ⇒ 这一整层没跑 ⇒ 综合等级「无法判定」（在 `summarize` 里）。
GATE_ABSTAIN_PREFIX = "这段视频不满足帧层检查的前提"

#: 帧层的**逐项弃权前缀** —— 单帧不适用（例如画面以纯色为主）时用它。
#: 它**不改变综合等级**，只在 findings 里留一条如实的「无法判定」。
NOT_APPLICABLE_PREFIX = "本项对这一段视频无从执行"


@dataclass(frozen=True)
class FrameLayerResult:
    """帧层分析结果。**全部由代码算出，不含任何模型输出。**"""

    analyzed: int = 0
    source: str = "none"
    #: 逐帧触发项：`((帧序号, (检查名, …)), …)`
    fired: tuple[tuple[int, tuple[str, ...]], ...] = ()
    #: 逐帧综合等级：`((帧序号, 等级), …)`
    levels: tuple[tuple[int, str], ...] = ()
    #: 内容完全相同的帧对（字节级相同 ⇒ 同一帧被重复写入）
    duplicate_pairs: tuple[tuple[int, int], ...] = ()
    #: 逐帧的「哪几项没跑」（图像链的弃权项），用于汇总
    abstained: tuple[tuple[int, tuple[str, ...]], ...] = ()
    notes: tuple[str, ...] = ()
    reason: str = ""

    @property
    def any_fired(self) -> bool:
        return any(checks for _, checks in self.fired)

    @property
    def total_frames_hint(self) -> str:
        return f"{self.analyzed} 帧"


def extract_gif_frames(data: bytes, *, limit: int = MAX_FRAMES) -> tuple[list[ExtractedFrame], str]:
    """把动画 GIF 逐帧光栅化成 PNG 字节；返回 `(帧, 说明)`。

    ⚠️ 落地格式必须是 **PNG**（无损），不能是 JPEG。理由见模块 docstring 第 1 条：
    GIF 帧本来没有 JPEG 的 8x8 块与量化表，硬转成 JPEG 会**凭空造出**这两样，
    图像链随后报出的「块网格相位异常」就是纯粹的自作自受。

    ⚠️ Pillow 是**按需导入**的：服务器上没装时，返回空帧 + 一句说清原因的话，
    而不是让整个视频链 import 失败。
    """
    try:
        from PIL import Image  # noqa: PLC0415 - 按需导入，理由见上
    except ImportError:
        return [], (
            "本进程没有 Pillow，动画 GIF 的逐帧光栅化做不到 —— "
            "容器层结论（块结构 / 帧数 / 延迟）不受影响，帧层本次没有执行。"
        )

    frames: list[ExtractedFrame] = []
    try:
        with Image.open(io.BytesIO(data)) as image:
            total = int(getattr(image, "n_frames", 1) or 1)
            if total <= 1:
                return [], "这份 GIF 只有一帧（不是动画），没有可比对的帧序列 ⇒ 帧层没有执行。"
            # 首 / 中 / 末三帧：与 MJPEG 路径取的位置一致，便于横向比较两条路。
            picks = sorted({0, total // 2, total - 1})[:limit]
            for index in picks:
                image.seek(index)
                buffer = io.BytesIO()
                image.convert("RGBA").save(buffer, format="PNG")
                payload = buffer.getvalue()
                if len(payload) > MAX_FRAME_BYTES:
                    continue
                frames.append(
                    ExtractedFrame(index=index, mime_type="image/png",
                                   data=payload, source="animated_gif")
                )
    except (OSError, ValueError, EOFError) as exc:
        return [], f"这份 GIF 的光栅化在中途失败：{stable_reason(exc)}"
    return frames, ""


def analyze_frames(
    frames: Sequence[ExtractedFrame], *, unavailable_reason: str | None = None,
) -> FrameLayerResult:
    """对取到的帧逐帧跑**已有的图像鉴伪链**，并做跨帧比对。

    ⚠️ 复用而不是重写：`evidence.forensics.analyze()` 已经是那条链上
    被断言钉过很多次的那一份。在这里另写一套「简化的帧检查」，
    两边的判据（尤其「前提不满足时必须判无法判定」那条）会立刻开始漂。

    ⚠️ `unavailable_reason` 由调用方（`forensics.analyze`）传**它自己那条路
    给出的真实原因**。不传也能跑，但那样一来「一帧都没取到」的理由会被
    写死成 MP4 那一句（「容器里没有逐帧存着的 JPEG/PNG」）——
    对动画 GIF 而言那是**错的原因**（GIF 每帧本来就是一张完整位图，
    只是要 LZW 解出来），而调用方会照着错的原因去"换个文件试试"。
    """
    if not frames:
        return FrameLayerResult(
            reason=unavailable_reason or (
                f"{GATE_ABSTAIN_PREFIX}（容器里没有逐帧存着的 JPEG/PNG，"
                "本进程也没有视频解码器）—— 帧层整层没有执行。"
            )
        )

    try:
        from ..evidence.forensics import analyze as analyze_image  # noqa: PLC0415
    except ImportError as exc:
        return FrameLayerResult(
            reason=(
                f"{GATE_ABSTAIN_PREFIX}（本进程缺 Pillow / numpy，"
                f"图像鉴伪核心导入失败：{type(exc).__name__}）—— 帧层整层没有执行。"
            )
        )

    fired: list[tuple[int, tuple[str, ...]]] = []
    levels: list[tuple[int, str]] = []
    abstained: list[tuple[int, tuple[str, ...]]] = []
    notes: list[str] = []
    digests: dict[str, int] = {}
    duplicates: list[tuple[int, int]] = []

    for frame in frames:
        digest = hashlib.sha256(frame.data).hexdigest()
        if digest in digests:
            # 字节级相同 ⇒ 同一帧被写了两遍。真实拍摄不可能出现（传感器噪声会变），
            # 所以在 MJPEG / GIF 这种「逐帧独立编码」的容器里，它是一条**强**线索。
            duplicates.append((digests[digest], frame.index))
        else:
            digests[digest] = frame.index

        try:
            report = analyze_image(frame.data)
        except (ValueError, OSError) as exc:
            # 单帧读不出来**不该**把整层判死：它是「这一帧没看成」，
            # 而不是「整个帧层没跑」。如实记一条，继续看下一帧。
            notes.append(
                f"第 {frame.index} 帧（{frame.mime_type}，{len(frame.data)} 字节）"
                f"没能送进图像链：{stable_reason(exc)}"
            )
            continue

        checks = tuple(f.check for f in report.findings if f.verdict == ABNORMAL)
        skipped = tuple(f.check for f in report.findings if f.verdict == ABSTAIN)
        if checks:
            fired.append((frame.index, checks))
        if skipped:
            abstained.append((frame.index, skipped))
        levels.append((frame.index, str(report.suspicion)))

    if not levels:
        # ⚠️ **取到了帧、却一帧都没能送进图像链**。这一支原先落到默认的
        #    `reason=""` ⇒ `frame_finding` 产出一条 `detail` 为**空串**的
        #    「无法判定」，`frames_note` 也是空串 ⇒ 报告里只剩一个孤零零的
        #    等级，**一个字的原因都没有**；而 `notes` 里逐帧的原因被整个丢掉。
        #    这正是本项目最怕的形态：不撒谎，但也没说任何话，而它看起来
        #    与「真的没什么可说的」完全一样。
        #    用**逐项弃权**前缀（不是材料级闸门）：帧层确实跑了 ——
        #    取帧成功、逐帧尝试都做过，只是每一帧都没给出结论。
        return FrameLayerResult(
            analyzed=0,
            source=frames[0].source,
            duplicate_pairs=tuple(duplicates),
            notes=tuple(notes),
            reason=(
                f"{NOT_APPLICABLE_PREFIX}：容器里取到了 {len(frames)} 帧，"
                "但它们**一帧都没能送进图像鉴伪链**，因此帧层这一次"
                "**没有给出有效结论** —— 它既不构成「提示异常」，"
                "**也不构成「未提示异常」**。逐帧原因："
                + "；".join(notes)
            ),
        )

    return FrameLayerResult(
        analyzed=len(levels),
        source=frames[0].source,
        fired=tuple(fired),
        levels=tuple(levels),
        duplicate_pairs=tuple(duplicates),
        abstained=tuple(abstained),
        notes=tuple(notes),
    )


#: 帧层结论的 caveat。**这一段是本层最重要的一句话** ——
#: 抽 4 帧不是全片检查，而「没看到」最容易被读成「没问题」。
FRAME_CAVEAT = (
    "本次只分析了**从容器里零解码取出的少数几帧**（首 / 中 / 末抽样），"
    "**不是逐帧检查全片**。因此：这一层「没有提示异常」**不等于**全片没有被改动过 —— "
    "被剪掉的那几秒大概率不在这几帧上。要覆盖全程必须逐帧解码（需要 ffmpeg，"
    "服务器上没有），或走线下司法鉴定。"
)


def frame_layer_note(result: FrameLayerResult, *, total_frames: int | None) -> str:
    """帧层的如实说明（进报告的 `frames_note`）。

    ⚠️ `total_frames` 与 `analyzed` 的差距**必须写出来**。只写「分析了 3 帧」
    而全片有 18 万帧，读者不会自己去做这个除法。
    """
    if result.analyzed == 0:
        return result.reason
    head = (
        f"本次分析了 **{result.analyzed} 帧**"
        f"（来源 {result.source}）"
    )
    if total_frames and total_frames > result.analyzed:
        head += (
            f"，占容器声明的总帧数（{total_frames} 帧）的 **"
            f"{result.analyzed / total_frames:.4%}** —— 这是抽样，不是全片。"
        )
    else:
        head += "。"
    return head + " " + FRAME_CAVEAT


def frame_finding(result: FrameLayerResult, *, total_frames: int | None) -> Finding:
    """把帧层结果折成**一条** `Finding`（视频报告的第七项）。

    设计取舍：**不把每帧的 findings 逐条摊进视频报告**。
    那样一份报告会有几十条 check 名字（`block_grid` / `ela` / …），
    而它们全都是**图像域**的名字，混在视频域的 `structure` / `timeline` 里
    会让读者以为视频链自己实现了那几项。这里只做两件事：
    ★ 点名「哪一帧的哪一项被触发」，★ 说清抽样边界。
    """
    if result.analyzed == 0:
        return Finding(
            check="frames",
            verdict=ABSTAIN,
            detail=result.reason,
            caveat=FRAME_CAVEAT,
        )

    bits: list[str] = []
    if result.fired:
        for index, checks in result.fired:
            bits.append(f"第 {index} 帧触发 {'、'.join(checks)}")
    if result.duplicate_pairs:
        for first, second in result.duplicate_pairs:
            bits.append(
                f"第 {first} 帧与第 {second} 帧**字节完全相同**"
                "（同一帧被重复写入 / 冻结帧的形态）"
            )

    head = frame_layer_note(result, total_frames=total_frames)
    if result.notes:
        # ⚠️ 「抽了 4 帧、其中 2 帧没看成」与「抽了 4 帧、4 帧都看了」会写出
        #    几乎一样的一句话（后者只是名单短一点）。逐帧失败必须点名，
        #    否则「少看了两帧」这件事在报告里**看不见** —— 而它看起来
        #    与「一切都好」完全一样。
        head += (
            f" ⚠️ 另有 **{len(result.notes)} 帧**没能送进图像鉴伪链，"
            "**不计入**上面的分析范围：" + "；".join(result.notes)
        )
    if not bits:
        # ⚠️ 「这几帧没有提示异常」这句正面结论，**只在图像链真的给出了结论时**才成立。
        #    2026-10 实测过一次这个形态：4 帧全都因「画面以纯色 / 平滑区域为主」被图像链
        #    判成「无法判定」，而这里照样输出「各项检查都没有提示异常」——
        #    于是「测不了」在**这一层**被翻译成了「没问题」，与视频链顶层用
        #    `GATE_ABSTAIN_PREFIX` 想挡住的是同一件事，只是下沉了一层。
        #    判据定成一句反问：**那句结论点名的动作，本次真的做了吗？**
        abstained_frames = [i for i, lv in result.levels if lv == ABSTAIN]
        if result.levels and len(abstained_frames) == len(result.levels):
            # 用**逐项弃权**前缀（不是材料级闸门）：帧层确实跑了，
            # 只是每一帧都没给出结论 ⇒ 它不该把整份报告压成「无法判定」，
            # 但必须在综合理由里被点名（`summarize()` 会数进去）。
            return Finding(
                check="frames",
                verdict=ABSTAIN,
                detail=(
                    f"{NOT_APPLICABLE_PREFIX}："
                    f"图像链在本次取到的 {result.analyzed} 帧上**全部**判为「无法判定」"
                    "（画面以纯色 / 平滑区域为主，内容层检查在它们身上无从下手）"
                    "⇒ 帧层这一次**没有给出有效结论** —— 它既不构成「提示异常」，"
                    "**也不构成「未提示异常」**。"
                ),
                caveat=FRAME_CAVEAT,
            )
        detail = (
            f"{head} 这几帧里，图像链的各项检查都没有提示异常"
            f"（逐帧等级：{'、'.join(f'第{i}帧 {lv}' for i, lv in result.levels)}）。"
        )
        if abstained_frames:
            detail += (
                f" ⚠️ 其中 **{len(abstained_frames)} 帧**"
                f"（{'、'.join(f'第{i}帧' for i in abstained_frames)}）"
                "的图像链结论是「无法判定」，**不计入上面那句「未提示异常」**。"
            )
        return Finding(
            check="frames",
            verdict="未提示异常",
            detail=detail,
            caveat=FRAME_CAVEAT,
        )
    return Finding(
        check="frames",
        verdict=ABNORMAL,
        detail=(
            f"{head} 线索：{'；'.join(bits)}。"
            f"（逐帧综合等级：{'、'.join(f'第{i}帧 {lv}' for i, lv in result.levels)}）"
        ),
        caveat=FRAME_CAVEAT,
    )


def load_frame_dependencies() -> dict[str, Any]:
    """给排障用：如实报告帧层的依赖到底齐不齐。**不参与判定。**"""
    status: dict[str, Any] = {}
    for name in ("PIL", "numpy"):
        try:
            __import__(name)
            status[name] = True
        except ImportError:
            status[name] = False
    return status


__all__ = [
    "FRAME_CAVEAT",
    "GATE_ABSTAIN_PREFIX",
    "NOT_APPLICABLE_PREFIX",
    "FrameLayerResult",
    "analyze_frames",
    "extract_gif_frames",
    "frame_finding",
    "frame_layer_note",
    "load_frame_dependencies",
]
