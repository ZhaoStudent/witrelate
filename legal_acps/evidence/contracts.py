"""图像鉴伪的证据契约。

设计原则（与项目既有契约一致）：
  * **判定字段一律由代码填**，不交给模型 —— 与「轮次号必须由服务端账本决定」同源。
    模型可以写解释，但 `verdict` / `suspicion` / `upgrades` 都由程序产生。
  * **每条发现必须自带 `caveat`** —— 写明"它不能证明什么"。
    一个只会说"异常"、不说"异常不等于篡改"的鉴伪工具，比没有更危险。
  * **不出现"真/假"这种二值结论**。只有线索强度 + 不确定度 + 升级建议。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: 单项检查的结论。刻意**不用 true/false**，避免被当成"是伪造"。
FindingVerdict = Literal["未提示异常", "提示异常", "无法判定"]

#: 综合线索等级。**只在这里定义一次** —— `ImageForensicsReport.suspicion`
#: 与鉴证 Leader 的 `EvidenceRuling.suspicion` 都引它。
#: 写第二份的后果不是"多打几个字"，而是**两侧取值域会各自漂**：
#: 检测层新增一档、汇总层还是老三样 ⇒ 模型只能把新值硬塞进旧档，
#: 而离线断言看不出来（提示词与契约都是自由文本，没有 schema 可校）。
#:
#: ⚠️ 取值域里**没有**"真/假"这一对，这是本能力最重要的一条纪律。
SuspicionLevel = Literal["低", "中", "高", "无法判定"]

#: 复检档位，成本从低到高
UpgradeTier = Literal["online_free", "online_paid", "offline_forensic"]


class Finding(BaseModel):
    """一条检测发现。"""

    check: str = Field(description="检查项标识，如 exif / ela / noise_residual")
    verdict: FindingVerdict = Field(description="该项的结论")
    strength: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="线索强度 0~1（不是篡改概率，只是该项信号的相对强弱）",
    )
    detail: str = Field(description="人话解释：看到了什么")
    caveat: str = Field(description="**不能证明什么** —— 这一项为什么不足以单独定论")


class UpgradePath(BaseModel):
    """更准、但更贵的复检建议。由程序按线索等级生成，不由模型即兴发挥。"""

    tier: UpgradeTier = Field(description="复检档位")
    name: str = Field(description="方法/机构名称")
    cost: str = Field(description="成本口径")
    turnaround: str = Field(description="耗时口径")
    when_to_use: str = Field(description="什么情况下值得用它")


class ImageForensicsReport(BaseModel):
    """图像鉴伪的交付物。"""

    media_type: str = Field(description="识别的真实容器类型，如 image/jpeg")
    byte_size: int = Field(description="字节数")
    sha256: str = Field(description="原文件 SHA-256 —— 证据保全用，事后无法补算")
    width: int = Field(description="像素宽")
    height: int = Field(description="像素高")
    crop_note: str = Field(description="像素域检查是否用了中心裁剪（如实标注）")
    findings: list[Finding] = Field(default_factory=list, description="逐项发现")
    suspicion: SuspicionLevel = Field(
        description="综合线索等级 —— **不是** 伪造概率"
    )
    suspicion_rationale: str = Field(description="为什么给这个等级")
    upgrades: list[UpgradePath] = Field(
        default_factory=list, description="更准但更贵的复检路径（按成本递增）"
    )
    chain_of_custody: list[str] = Field(
        default_factory=list, description="证据保全提示（原始载体 / 哈希 / 只读介质）"
    )
    disclaimer: str = Field(description="效力声明：本报告是技术线索，不是司法鉴定意见")


class ImageEvidenceRequest(BaseModel):
    """调用方（主控 / 其他 Agent）递过来的鉴伪请求。

    ⚠️ **为什么只有一个 `image_uri` 而没有图片字节字段** —— 这不是设计选择，是约束：

        AIP 协议层  FileDataItem.uri **或** bytes(base64)   ← 都支持，但二者互斥
        wit 代码层  delegate(..., request=ContentInput)      ← ImagePart.data 可传字节
        模型工具层  TOOL_CONTENT_INPUT_SCHEMA                ← **只暴露 uri**

    模型工具层的 schema 是 `required: [type, uri, mime_type]`、
    `additionalProperties: false`，uri 的原文描述是 "Absolute remotely reachable
    content URI." ⇒ **模型发起的委派只能给 URL，只有代码发起的才能给字节。**

    所以本契约以 URI 为准；「把 URL 变成字节」由 `evidence/fetch.py` 按策略完成。

    ⚠️ **2026-10-01 补测补上的一句**：AIP 协议层的 `FileDataItem.uri` 必须是
    「远程可达」的 —— `data:` / `file://` / 裸路径**全部被拒**，内联内容只能走
    `bytes` 字段。也就是说上面那张表里，**"`data:` 是合法 URI"只在模型工具层
    这一行成立**。用户上传的图走的是 `bytes` 那条路，wit 把它转成
    `ImagePart(data=…)` 放在 `state.messages` 里 —— 由 `evidence/inbox.py`
    收进收件箱、发一个 `inbox:<sha256>` 句柄出来。模型只需照抄这个句柄。
    """

    image_uri: str = Field(
        description="图片的绝对可达 URI：inbox:<sha256> 句柄（随任务附件进来的图，首选）、"
                    "data:image/jpeg;base64,... 或白名单内的 https 地址。"
                    "**不要传本地路径、不要传相对路径、不要传网页链接。**"
    )
    declared_mime: str | None = Field(
        default=None, description="调用方声明的 MIME（用于与真实容器比对，可不填）"
    )
    context_note: str | None = Field(
        default=None,
        description="这张图在案情里的分量（如「据称是案发现场照片」），仅用于措辞，不参与判定",
    )


class ImageEvidenceResult(BaseModel):
    """鉴伪交付物 + 取图溯源。

    `report` 里的每个判定字段都由代码产生（`analyze()` 是纯函数）；
    `summary_for_leader` 也是**程序拼的固定文案**，不是模型即兴发挥 ——
    主控可以直接引用它，不必（也不该）自己复述检测结论。
    """

    source_uri: str = Field(description="本次实际取图的 URI（原文照抄，便于复核）")
    fetched_from: Literal["inbox", "inline_base64", "network", "local_file"] = Field(
        description="字节来源：inbox = 随任务附件进来的字节（`inbox:<sha256>` 句柄，"
                    "由 `evidence/inbox.py` 登记，首发也是最推荐的一条）；"
                    "inline_base64 = 自带字节的 data: URI；network = 白名单内出网抓取；"
                    "local_file = 本机文件（仅在入口代码显式开启本地读取时可能）"
    )
    context_note: str | None = Field(
        default=None,
        description="调用方提供的案情说明（**原样回显**）—— 供转述时组织语言用，不参与任何判定",
    )
    report: ImageForensicsReport = Field(description="逐项检测报告")
    summary_for_leader: str = Field(description="给主控的一句话摘要（程序生成，可直接引用）")


class ImageEvidenceOutcome(BaseModel):
    """工具的实际返回。

    **为什么要有 `refused` 这一支**：没有产出报告时（取图被策略拒绝、字节不是可识别的
    图像、图像损坏无法解码、像素数过大），如果直接抛异常，调用方看到的是一句不透明的
    错误，最可能的反应是"换个路子把图弄进来" —— 那正好绕过了刚设的边界。
    所以把这类失败**做成正常返回值**，并附上 `how_to_fix`，
    让模型能如实转述"没看成，因为……"，而不是自己想办法绕。

    ⚠️ `refused` 覆盖的不只是"策略拒绝"，而是**一切没能产出报告的情形**；
    判据是「有没有报告」而不是「谁的责任」—— 调用方要的是"还能怎么办"。
    """

    status: Literal["ok", "refused"] = Field(
        description="ok = 已完成初检（见 result）；refused = 本次没有产出报告（原因见 refusal_reason）"
    )
    result: ImageEvidenceResult | None = Field(default=None, description="status=ok 时的完整交付物")
    refusal_reason: str | None = Field(
        default=None,
        description="status=refused 时的原因（原文转述给调用方，不要改写成自己的话）",
    )
    how_to_fix: str | None = Field(default=None, description="status=refused 时可执行的补救办法")


__all__ = [
    "Finding",
    "FindingVerdict",
    "ImageEvidenceOutcome",
    "ImageEvidenceRequest",
    "ImageEvidenceResult",
    "ImageForensicsReport",
    "SuspicionLevel",
    "UpgradePath",
    "UpgradeTier",
]
