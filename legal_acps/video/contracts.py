"""视频鉴伪的证据契约。

设计原则（与图像链 / 音频链同源，且**刻意复用**它们已经定好的三个概念）：

  * **判定字段一律由代码填**，不交给模型。
  * **每条发现必须自带 `caveat`** —— 写明「它不能证明什么」。
  * **不出现「真 / 假」这种二值结论**。只有线索强度 + 不确定度 + 升级建议。

⚠️ `Finding` / `SuspicionLevel` / `UpgradePath` **从图像链 import，不重写** ——
   理由与音频链 `audio/contracts.py` 里那段完全一致：这三个概念与模态无关，
   写第二份的后果不是「多打几个字」，而是**两侧取值域会各自漂**。

════════════════════════════════════════════════════════════════════════
本契约里最重要的一个字段是 `frames_available`（与音频链的 `pcm_available` 同源）
════════════════════════════════════════════════════════════════════════

服务器上**没有 ffmpeg / OpenCV**，H.264 / H.265 的帧一律解不出来。
这本身不是缺陷 —— 容器层（盒结构 / 时间线 / 轨道自洽）就已经能给出
有价值的鉴伪线索，而且它是**零依赖**的（纯标准库解析）。
但如果报告只写几个容器层结论就收尾，读起来会像「检查过了、没问题」 ——
而真相是**帧层一项都没跑**，而帧层才是能直接看到画面改动的那一层。

所以这个字段必须显式出现在报告里，且 `frames_note` 要写清后果。
凡是「能零解码取到 JPEG/PNG 帧」的容器（MJPEG / covr 封面帧 / 动画 GIF），
本链**真的会去抽帧**并把帧层结论并进来 —— 那不是「以后再说」，是本次就做的。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: 复用的三个概念 —— 见模块 docstring 里「为什么不重写」。
from ..evidence.contracts import Finding, SuspicionLevel, UpgradePath

__all__ = [
    "TrackSummary",
    "VideoEvidenceOutcome",
    "VideoEvidenceRequest",
    "VideoEvidenceResult",
    "VideoForensicsReport",
]


class TrackSummary(BaseModel):
    """容器里的一条轨道（视频 / 音频 / 其它）的**声明信息**。

    ⚠️ 这些值**全部照抄容器头部**，没有一条是推断出来的。所以它们只能
    用来做「声明之间是否自洽」的比对，**不能**当作「这段媒体真的是这样」。
    一条被改过头的视频，它的 `stsd` 里写着 30 fps 而实际是 25 fps —— 那正是
    `timeline` 那一项要找的东西，所以这一层必须保持「只记录、不判断」。
    """

    handler: str = Field(description="轨道类型：video / sound / other（取自 hdlr）")
    codec: str = Field(description="编码标识（ISO BMFF 的 fourcc，如 avc1 / hvc1 / mjpa）")
    timescale: int | None = Field(default=None, description="该轨自己的时间刻度（每秒多少个单位）")
    duration_seconds: float | None = Field(default=None, description="该轨声明时长（秒）")
    sample_count: int | None = Field(default=None, description="样本数（帧数 / 音频包数）")
    width: int | None = Field(default=None, description="视频轨声明宽度（像素）")
    height: int | None = Field(default=None, description="视频轨声明高度（像素）")


class VideoForensicsReport(BaseModel):
    """视频鉴伪的交付物。

    ⚠️ 字段按「哪一层读出来的」分组，与 `audio/contracts.py` 同一习惯 ——
    读者一眼能看出哪些结论来自容器头、哪些来自真实像素。
    """

    media_type: str = Field(description="按**魔术字节**判定的真实容器类型，如 video/mp4")
    byte_size: int = Field(description="字节数")
    sha256: str = Field(description="原文件 SHA-256 —— 证据保全用，事后无法补算")
    container: str = Field(description="容器的人话描述，如「ISO BMFF（MP4 家族）」")
    brand: str | None = Field(default=None, description="ISO BMFF 的 major_brand（如 mp42 / qt）；非 BMFF 为 null")

    # -- 容器层读到的参数（**声明值，不是实测值**）-----------------------------
    duration_seconds: float | None = Field(default=None, description="容器声明的总时长（秒）")
    video_codec: str | None = Field(default=None, description="主视频轨的编码标识")
    width: int | None = Field(default=None, description="视频轨声明宽度（像素）")
    height: int | None = Field(default=None, description="视频轨声明高度（像素）")
    frame_count: int | None = Field(default=None, description="视频轨声明的样本数（帧数）")
    tracks: list[TrackSummary] = Field(default_factory=list, description="各轨道的声明信息")

    # -- 帧层可用性（决定哪些检查项能跑）------------------------------------
    frames_available: bool = Field(
        description="本次是否真的取到了**可分析的帧**（无需视频解码器即可取出的 JPEG/PNG 帧）。"
                    "**false 时全部帧层检查整项无法执行**"
    )
    frames_note: str = Field(
        description="帧层可用性的人话说明。不可用时必须写清**为什么**以及"
                    "「因此本报告只覆盖容器与时间线层，不能据此认为没有问题」"
    )
    frames_analyzed: int = Field(
        default=0, description="本次**实际分析**了多少帧。⚠️ 抽样分析 ⇒ 它一定远小于总帧数，"
                               "报告必须据实写出这个差，不能让人以为全片都看过了"
    )
    frame_source: str = Field(
        default="none",
        description="帧是从怎么来的：mjpeg_samples（容器里逐帧存的就是 JPEG，零解码可得）/"
                    "cover_art（内嵌封面帧）/ animated_gif（动画 GIF 的帧）/ none",
    )

    findings: list[Finding] = Field(default_factory=list, description="逐项发现")
    suspicion: SuspicionLevel = Field(description="综合线索等级 —— **不是**篡改概率")
    suspicion_rationale: str = Field(description="为什么给这个等级")
    upgrades: list[UpgradePath] = Field(
        default_factory=list, description="更准但更贵的复检路径（按成本递增）"
    )
    chain_of_custody: list[str] = Field(
        default_factory=list, description="证据保全提示（原始载体 / 哈希 / 只读介质）"
    )
    disclaimer: str = Field(description="效力声明：本报告是技术线索，不是司法鉴定意见")


class VideoEvidenceRequest(BaseModel):
    """调用方（鉴证 Leader / 其他 Agent）递过来的视频鉴伪请求。

    ⚠️ **为什么不出网** —— 与音频侧刻意相同、与图像侧刻意不同，这是收缩而非遗漏：

        图像侧  fetch.py 有一条受白名单约束的取图通道（`EVIDENCE_IMAGE_ALLOWED_HOSTS`）
        音频侧  **没有**
        视频侧  **没有**（本文件）

    理由与音频侧同源，而且**在视频上更强**：视频文件动辄几十上百 MB，
    内联 base64 会让请求体膨胀到不可用，所以调用方本来就该走附件通道；
    而每开一条出网通道就多一个 SSRF 面。需要网络地址的视频，
    请调用方先把它作为任务附件提交（走 `inbox:` 句柄）—— 这条路由已经存在且验过。
    """

    video_uri: str = Field(
        description="视频的引用：`inbox:<sha256>` 句柄（随任务附件进来的视频，**首选**）"
                    "或 `data:video/mp4;base64,...`。"
                    "**本能力不出网**：https 地址、本地路径、相对路径一律会被拒绝。"
    )
    declared_mime: str | None = Field(
        default=None, description="调用方声明的 MIME（用于与真实容器比对，可不填）"
    )
    context_note: str | None = Field(
        default=None,
        description="这段视频在案情里的分量（如「据称是行车记录仪导出」），仅用于措辞，不参与判定",
    )


class VideoEvidenceResult(BaseModel):
    """鉴伪交付物 + 取字节溯源。"""

    source_uri: str = Field(description="本次实际取用的引用（原文照抄，便于复核）")
    fetched_from: Literal["inbox", "inline_base64", "network", "local_file"] = Field(
        description="字节来源：inbox = 随任务附件进来的字节（`inbox:<sha256>` 句柄，"
                    "首发也是最推荐的一条）；inline_base64 = 自带字节的 data: URI。"
                    "⚠️ 本能力不出网 ⇒ network / local_file **在视频链上不会出现**；"
                    "枚举保留是为了与图像 / 音频链接口同形，且**溯源字段不许含糊**"
    )
    context_note: str | None = Field(
        default=None, description="调用方提供的案情说明（**原样回显**），不参与任何判定"
    )
    report: VideoForensicsReport = Field(description="逐项检测报告")
    summary_for_leader: str = Field(description="给调用方的一句话摘要（程序生成，可直接引用）")


class VideoEvidenceOutcome(BaseModel):
    """工具的实际返回。

    **为什么要有 `refused` 这一支**（与图像链 / 音频链同源）：没有产出报告时，
    如果直接抛异常，调用方看到的是一句不透明的错误，最可能的反应是
    「换个路子把文件弄进来」 —— 那正好绕过了刚设的边界。
    所以把这类失败**做成正常返回值**，并附上 `how_to_fix`。

    ⚠️ 判据是「有没有报告」，不是「谁的责任」。
    """

    status: Literal["ok", "refused"] = Field(
        description="ok = 已完成初检（见 result）；refused = 本次没有产出报告（原因见 refusal_reason）"
    )
    result: VideoEvidenceResult | None = Field(default=None, description="status=ok 时的完整交付物")
    refusal_reason: str | None = Field(
        default=None,
        description="status=refused 时的原因（原文转述给调用方，不要改写成自己的话）",
    )
    how_to_fix: str | None = Field(default=None, description="status=refused 时可执行的补救办法")
