"""音频鉴伪的证据契约。

设计原则（与图像链同源，且**刻意复用**它已经定好的两个概念）：

  * **判定字段一律由代码填**，不交给模型。
  * **每条发现必须自带 `caveat`** —— 写明"它不能证明什么"。
  * **不出现"真 / 假"这种二值结论**。只有线索强度 + 不确定度 + 升级建议。

⚠️ `Finding` / `SuspicionLevel` / `UpgradePath` **从图像链 import，不重写**：
   "一条线索长什么样""线索等级有几档""复检阶梯怎么描述"这三件事与模态无关。
   写第二份的后果不是"多打几个字"，而是**两侧取值域会各自漂** ——
   检测层新增一档、汇总层还是老三样 ⇒ 模型只能把新值硬塞进旧档，
   而离线断言看不出来（提示词与契约都是自由文本，没有 schema 可校）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: 复用的三个概念 —— 见模块 docstring 里"为什么不重写"。
from ..evidence.contracts import Finding, SuspicionLevel, UpgradePath

__all__ = [
    "AudioEvidenceOutcome",
    "AudioEvidenceRequest",
    "AudioEvidenceResult",
    "AudioForensicsReport",
]


class AudioForensicsReport(BaseModel):
    """音频鉴伪的交付物。

    ⚠️ **`pcm_available` 是本契约里最重要的一个字段。**
    服务器上**没有 ffmpeg / sox / librosa**，所以 MP3 / AAC / FLAC / OGG
    一律**解不出波形**。这本身不是缺陷（"能读容器、能报编码参数"就已经是
    有价值的鉴伪线索），但如果报告只写几个容器层结论就收尾，
    读起来会像"检查过了、没问题" —— 而真相是**波形域一项都没跑**。
    所以这个字段必须显式出现在报告里，且 `pcm_note` 要写清后果。
    """

    media_type: str = Field(description="按**魔术字节**判定的真实容器类型，如 audio/wav")
    byte_size: int = Field(description="字节数")
    sha256: str = Field(description="原文件 SHA-256 —— 证据保全用，事后无法补算")
    container: str = Field(description="容器与编码的人话描述，如「WAVE / PCM 16-bit LE」")

    # -- 采样参数（**容器层读到的**，不是推断的）------------------------------
    sample_rate: int | None = Field(default=None, description="采样率（Hz）；读不出时为 null")
    channels: int | None = Field(default=None, description="声道数；读不出时为 null")
    bits_per_sample: int | None = Field(default=None, description="位深；读不出时为 null")
    duration_seconds: float | None = Field(
        default=None, description="时长（秒，按容器声明与采样率算）；读不出时为 null"
    )

    # -- 波形可用性（决定哪些检查项能跑）------------------------------------
    pcm_available: bool = Field(
        description="本次是否成功解出波形。**false 时全部波形域检查整项无法执行**"
    )
    pcm_note: str = Field(
        description="波形可用性的人话说明。不可用时必须写清**为什么**以及"
                    "「因此本报告只覆盖容器与编码参数层，不能据此认为没问题」"
    )
    analysis_window: str = Field(
        description="本次实际分析了多长的音轨（如「全长 42.1 秒」）。"
                    "对超长音轨会**只分析开头一段并如实标注** —— 绝不静默截断"
    )

    findings: list[Finding] = Field(default_factory=list, description="逐项发现")
    suspicion: SuspicionLevel = Field(
        description="综合线索等级 —— **不是**篡改概率"
    )
    suspicion_rationale: str = Field(description="为什么给这个等级")
    upgrades: list[UpgradePath] = Field(
        default_factory=list, description="更准但更贵的复检路径（按成本递增）"
    )
    chain_of_custody: list[str] = Field(
        default_factory=list, description="证据保全提示（原始载体 / 哈希 / 只读介质）"
    )
    disclaimer: str = Field(description="效力声明：本报告是技术线索，不是司法鉴定意见")


class AudioEvidenceRequest(BaseModel):
    """调用方（鉴证 Leader / 其他 Agent）递过来的音频鉴伪请求。

    ⚠️ **为什么不出网** —— 与图像链刻意不同，这是收缩而不是遗漏：

        图像侧  fetch.py 有一条受白名单约束的取图通道（`EVIDENCE_IMAGE_ALLOWED_HOSTS`）
        音频侧  **没有**。只认 `inbox:<sha256>` 句柄与 `data:` 内联字节。

    理由：音频证据的现实形态就是"用户上传的一份文件"，而**每开一条出网通道
    就多一个 SSRF 面**。要把一个网络地址上的音频送进来，请调用方先把它
    作为任务附件提交（走 `inbox:` 句柄），这条路由本来就存在且已验过。
    """

    audio_uri: str = Field(
        description="音频的引用：`inbox:<sha256>` 句柄（随任务附件进来的音频，**首选**）"
                    "或 `data:audio/wav;base64,...`。"
                    "**本能力不出网**：https 地址、本地路径、相对路径一律会被拒绝。"
    )
    declared_mime: str | None = Field(
        default=None, description="调用方声明的 MIME（用于与真实容器比对，可不填）"
    )
    context_note: str | None = Field(
        default=None,
        description="这段录音在案情里的分量（如「据称是通话录音」），仅用于措辞，不参与判定",
    )


class AudioEvidenceResult(BaseModel):
    """鉴伪交付物 + 取字节溯源。"""

    source_uri: str = Field(description="本次实际取用的引用（原文照抄，便于复核）")
    fetched_from: Literal["inbox", "inline_base64", "network", "local_file"] = Field(
        description="字节来源：inbox = 随任务附件进来的字节（`inbox:<sha256>` 句柄，"
                    "首发也是最推荐的一条）；inline_base64 = 自带字节的 data: URI。"
                    "⚠️ 本能力不出网 ⇒ network / local_file **在音频链上不会出现**；"
                    "枚举保留是为了与图像链接口同形，且**溯源字段不许含糊**"
    )
    context_note: str | None = Field(
        default=None, description="调用方提供的案情说明（**原样回显**），不参与任何判定"
    )
    report: AudioForensicsReport = Field(description="逐项检测报告")
    summary_for_leader: str = Field(description="给调用方的一句话摘要（程序生成，可直接引用）")


class AudioEvidenceOutcome(BaseModel):
    """工具的实际返回。

    **为什么要有 `refused` 这一支**（与图像链同源）：没有产出报告时，
    如果直接抛异常，调用方看到的是一句不透明的错误，最可能的反应是
    "换个路子把文件弄进来" —— 那正好绕过了刚设的边界。
    所以把这类失败**做成正常返回值**，并附上 `how_to_fix`。

    ⚠️ `refused` 覆盖的不只是"策略拒绝"，而是**一切没能产出报告的情形**
    （句柄没登记 / 不是可识别的音频容器 / 容器损坏 / 解码失败 / 超过体积上限）；
    判据是「有没有报告」而不是「谁的责任」。
    """

    status: Literal["ok", "refused"] = Field(
        description="ok = 已完成初检（见 result）；refused = 本次没有产出报告（原因见 refusal_reason）"
    )
    result: AudioEvidenceResult | None = Field(default=None, description="status=ok 时的完整交付物")
    refusal_reason: str | None = Field(
        default=None,
        description="status=refused 时的原因（原文转述给调用方，不要改写成自己的话）",
    )
    how_to_fix: str | None = Field(default=None, description="status=refused 时可执行的补救办法")
