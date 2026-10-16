"""取视频：把「调用方给的那个引用」变成 `analyze()` 能吃的字节。

════════════════════════════════════════════════════════════════════════
与音频侧**逐条相同**，与图像侧**刻意不对称**：一条出网通道都没有
════════════════════════════════════════════════════════════════════════

图像侧 `evidence/fetch.py` 有一条白名单出网通道（`EVIDENCE_IMAGE_ALLOWED_HOSTS`），
因为模型工具层的 schema 只允许传 URL（`TOOL_CONTENT_INPUT_SCHEMA` 的 uri 字段
原文是 "Absolute remotely reachable content URI."），而**看图**这件事的前提是
把图弄进来，所以那条出网口子是「不得已而开」的。

视频侧**不但没有这个不得已，而且比音频侧更没有**：

  · 视频证据的正规来路本来就只能是「任务附件」（`VideoPart` 带字节 →
    收件箱句柄）—— 文件动辄几十上百 MB，内联 base64 会让请求体膨胀约 1/3，
    工具调用的参数字符串根本承载不了；
  · 而每开一条出网通道就多一个 SSRF 面：**任何能给我发任务的对象，
    都能驱使本进程去访问任意内网地址**。

于是这里的策略与音频侧一字不差 —— 不是「白名单」，而是**一条出口都不留**：

    · `inbox:<sha256>`            无条件放行（字节已在本进程；句柄是**内容哈希不是路径**）
    · `data:video/*;base64,...`   无条件放行（自带载荷，不出网）
    · 其余一切（https / http / file / 裸路径 / ftp …）—— **一律拒绝**

⚠️ 但要说清代价，别把「没有」说成「不用」：调用方拿到「拒绝」时**必须知道该改什么**，
所以每条拒绝都带一个**机器可读的分类码**（`FETCH_REFUSAL_KINDS`），
由 `pipeline.fix_for()` 配一条可执行的出路，而不是一句「不支持」。

════════════════════════════════════════════════════════════════════════
体积上限为什么是 64 MiB，以及它与收件箱上限的**关系**
════════════════════════════════════════════════════════════════════════

与音频侧不同，视频这个上限**不能**由「分析窗有多长」推出来 ——
因为它要的是**完整文件**：

    `container` 那两项判据（截断 / 尾部多余数据）靠的就是
    「容器声明的长度」与「实际拿到的字节数」之差；
    少给一个字节，这两项就一起失效，而报告上看不出它是失效还是真的没问题。

所以上限的约束来自另一头：**愿不愿意把整个文件读进内存**。

    手机 1 分钟 1080p  ≈ 60–100 MB
    行车记录仪 1 分钟  ≈ 100 MB（且常按固定时长分段保存）
    转发件（微信 / 平台二次压缩）通常反而更小

64 MiB 是折中：覆盖短片段、全部 MJPEG / 动画 GIF、以及大多数转发件。
再大的**不要为了塞进来而重编码**（重编码会重写容器与样本表，
正好抹掉本能力要看的那批证据）—— 出路见 `pipeline._FIX_BY_KIND["too_large"]`。

⚠️ **不变量（必须与 `evidence/inbox.MAX_STAGE_BYTES` 一起看）**：

    收件箱是三条链共用的落点 ⇒ `MAX_STAGE_BYTES ≥ MAX_VIDEO_BYTES`

两个数字并列写在这里会被后人只改一个，所以这条关系由
`legal_acps/check_local.py` **现场派生**地断言（枚举三条链的上限取最大值）。

⚠️ 由此还有一个**必须说清、不能假装不存在**的后果：当前 `MAX_VIDEO_BYTES`
与落点上限**相等**，所以 `too_large` 这个分类码在现行配置下**够不到**
（超限的附件在落点那一层就被拒了，理由同样是"不做截断"）。
它保留下来是**纵深防御**（谁把落点抬上去，它就立刻生效）。
而"一个永远走不到的分支"和"不存在"没有区别 ⇒ `tools/verify_video.py`
**把这个上限临时改小**，把 `too_large` 真的走一遍 ——
判据是"这条路真的会响"，不是"代码里有这么一段"。
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Literal

from ..evidence import inbox

#: 视频字节上限。口径见模块 docstring。
#: ⚠️ 它必须 ≤ `evidence.inbox.MAX_STAGE_BYTES`（由 `check_local` 现场派生断言）。
MAX_VIDEO_BYTES = 64 * 1024 * 1024

#: `data:` URI 允许的 MIME 前缀。
#: ⚠️ `image/gif` 出现在视频能力里**不是笔误**：动画 GIF 是本能力明确支持的
#: 容器之一（逐帧光栅化后接进图像链），它在 MIME 上却是 `image/*`。
#: 这里把「本能力认它」这件事写在入口，而不是等 `analyze()` 去 sniff ——
#: 入口早一步拒掉，调用方拿到的是一句「类型不对」，而不是一个更晚、更含糊的失败。
DATA_URI_MIME_PREFIXES = ("video/", "image/gif")

#: 允许的 scheme 全集。**这是本模块唯一的「能力清单」**，
#: 加一项就等于开一条新的取件路，所以它刻意短得能一眼看完。
ALLOWED_SCHEMES = ("inbox", "data")


#: 拒绝原因的**机器可读分类码**。
#:
#: ⚠️ 为什么要有它（与音频侧同一条教训）：上层（`pipeline.outcome`）必须给每种拒绝
#: 配一条**可执行的补救建议**，否则调用方只会想办法绕开边界。图像链是用「在原因文本里
#: 找关键词」来配的 —— 那要求「报错的文案」与「配建议的表」**持续保持一致**，
#: 而这两件事写在不同文件里；音频侧初版就是这么漂的（文案写「没有任何出网通道」，
#: 表里找的是「没有出网通道」），于是**那条最要紧的拒绝静默降级成了兜底建议**。
#:
#: 所以：本元组是分类码的**唯一定义**，抛异常时必带其一；
#: `verify_video` 会**逐码**断言「每个码都有专属建议」且「每个码都能被真实构造出来」。
FETCH_REFUSAL_KINDS = (
    "unsupported_scheme",   # 不是 inbox:/data: —— 含 https / http / file / 裸路径
    "data_uri_malformed",   # 没有逗号，载荷段缺失
    "data_uri_not_base64",  # 声明了 base64 之外的编码
    "data_uri_mime",        # 声明的类型不是 video/*（也不是 image/gif）
    "data_uri_too_large",   # 解码前就按上界判超限
    "data_uri_bad_base64",  # 载荷本身解不开
    "data_uri_empty",       # 解码后为空
    "inbox_invalid",        # 句柄不合法 / 没登记 / 字节不可读
    "too_large",            # 拿到字节后才发现超限
)


class VideoFetchRefused(Exception):
    """取视频被策略拒绝（不是网络故障、不是格式故障）。

    ⚠️ **调用方必须把它如实转述**，不要吞掉换成「视频无法分析」 ——
    两者的补救动作完全不同：前者是「换个方式把字节给我」，后者是「这段视频本身有问题」。

    `kind` 取自 `FETCH_REFUSAL_KINDS`，供上层配补救建议用（见那里的注释）。
    """

    def __init__(self, reason: str, kind: str = "unsupported_scheme") -> None:
        super().__init__(reason)
        self.kind = kind


@dataclass(frozen=True)
class FetchedVideo:
    """取到的视频字节 + **溯源**。

    ⚠️ `source` 的取值域与 `VideoEvidenceResult.fetched_from` 相同，
    且**不许含糊**：在这里它只可能是 `inline_base64` 或 `inbox` ——
    本模块结构上产不出 `network` / `local_file`（那两种取值留在枚举里
    只是为了三条链接口同形）。把「以附件进来」写成「network」，
    等于在证据溯源那一栏写了一句假话，而它看起来完全正常。
    """

    data: bytes
    source: Literal["inline_base64", "inbox"]
    declared_mime: str | None
    #: 调用方给的原始引用（报告里回显，便于复核「这份字节是从哪个引用取来的」）。
    source_uri: str
    #: 收件箱里登记的名字（若有）。**仅作提示，不参与任何判定。**
    name: str | None = None


def classify_uri(uri: str) -> str:
    """把引用归成 `'inbox' | 'data' | 'other'`。**纯函数，判据全在这里。**

    刻意不返回主机名：本模块没有任何按主机判定的分支，
    返回一个永远用不上的字段只会让人以为「还有个白名单要看」。
    """
    text = str(uri or "").strip()
    low = text.lower()
    if low.startswith(inbox.HANDLE_PREFIX):
        return "inbox"
    if low.startswith("data:"):
        return "data"
    return "other"


def check_policy(uri: str) -> str:
    """判定该引用能不能取；不能取就抛 `VideoFetchRefused`。返回归一后的 scheme。"""
    kind = classify_uri(uri)
    if kind in ALLOWED_SCHEMES:
        # ⚠️ 这里**不校验句柄是否真的登记过** —— 那是 `inbox.resolve()` 的职责。
        #    「句柄合不合法」若有第二个判据，早晚会与收件箱不一致（同一件事两处判定）。
        return kind
    text = str(uri or "").strip()
    raise VideoFetchRefused(
        f"不支持的视频引用 {text[:48]!r}：本入口只认 inbox:<sha256> 与 "
        "data:video/...;base64,... 两种引用 —— **视频侧没有任何出网通道，"
        "也不读本机文件**。把字节交给本进程（任务附件或内联），"
        "而不是把地址交给我们。",
        "unsupported_scheme",
    )


def _decode_data_uri(uri: str) -> tuple[bytes, str | None]:
    header, comma, payload = uri.partition(",")
    if not comma:
        raise VideoFetchRefused(
            "data: URI 里没有逗号分隔的载荷部分 —— 这个 URI 是残缺的。",
            "data_uri_malformed",
        )
    meta = header[len("data:"):]
    declared = meta.split(";")[0].strip() or None
    if ";base64" not in meta.lower():
        raise VideoFetchRefused(
            "data: URI 必须是 base64 编码（本项目只接受二进制视频）。",
            "data_uri_not_base64",
        )
    if declared and not declared.lower().startswith(DATA_URI_MIME_PREFIXES):
        raise VideoFetchRefused(
            f"data: URI 声明的类型 {declared!r} 既不是视频，也不是本能力支持的动画 GIF。",
            "data_uri_mime",
        )
    # ⚠️ 大小检查必须在**解码之前**。先 `b64decode` 再查上限，等于「判它超限」这件事
    #    本身就要先把整个载荷解码进内存（64 MiB 的载荷 = 85 MB 字符串 + 64 MiB 解码结果），
    #    上限形同虚设。base64 是 3 字节 → 4 字符，`len(payload) * 3 // 4` 是解码后长度的
    #    **上界**；用上界做闸门是刻意的：方向 fail-closed，代价只是要求调用方少传两个字节。
    estimated = len(payload) * 3 // 4
    if estimated > MAX_VIDEO_BYTES:
        raise VideoFetchRefused(
            f"data: URI 载荷过大（base64 {len(payload)} 字符，解码后约 {estimated} 字节，"
            f"超过 {MAX_VIDEO_BYTES} 字节上限）—— **不做截断**，截断会让容器声明的长度"
            "与实际字节数对不上，而「截断 / 尾部多余数据」正是本能力的两条判据。",
            "data_uri_too_large",
        )
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VideoFetchRefused(
            f"data: URI 的 base64 无法解码：{exc}", "data_uri_bad_base64"
        ) from None
    if not data:
        raise VideoFetchRefused("data: URI 解码后为空。", "data_uri_empty")
    if len(data) > MAX_VIDEO_BYTES:
        raise VideoFetchRefused(
            f"视频 {len(data)} 字节，超过 {MAX_VIDEO_BYTES} 字节上限。",
            "data_uri_too_large",
        )
    return data, declared


def _load_inbox(uri: str) -> tuple[bytes, str | None, str | None]:
    """从收件箱按句柄取字节 —— 即「随任务附件进来的那一份」。"""
    try:
        data, declared, name = _read_inbox(uri)
    except inbox.InboxRefused as exc:
        # 归一到同一族异常：调用方只需要记住「取视频失败一律是 VideoFetchRefused」，
        # 不必知道收件箱这个内部机构的存在。
        raise VideoFetchRefused(f"收件箱引用无效：{exc}", "inbox_invalid") from None
    if len(data) > MAX_VIDEO_BYTES:
        raise VideoFetchRefused(
            f"收件箱里的视频 {len(data)} 字节，超过 {MAX_VIDEO_BYTES} 字节上限。",
            "too_large",
        )
    return data, declared, name


def _read_inbox(handle: str) -> tuple[bytes, str | None, str | None]:
    """`inbox.load()` 加上名字。**名字只用于回显**，不参与任何判定。

    ⚠️ 字节必须经 `inbox.load()` 读 —— 「字节不可读 ⇒ `InboxRefused`」那条判据
    **只住在那里**。（2026-10-03 复查夹出：原写法在这里自己
    `item.path.read_bytes()`，把 inbox 的包装**抄丢了** —— 文件在 resolve 之后、
    读取之前被清掉时，跑出来的是裸 `OSError`，落进 `outcome()` 的兜底分支拿
    **通用**建议，而不是 `inbox_invalid` 那条专属出路 ⇒ `FETCH_REFUSAL_KINDS`
    注释里承诺的「字节不可读」成了空话。图像侧一直是走 `inbox.load` 的 ——
    同一份逻辑抄三处，抄着抄着就漂了，正是「同一判定写两处必然漂移」的现行犯。）
    """
    data, declared = inbox.load(handle)
    item = inbox.resolve(handle)
    return data, (declared or None), item.name


def load(uri: str) -> FetchedVideo:
    """按策略取视频字节。被拒绝时抛 `VideoFetchRefused`，调用方必须如实转述。"""
    kind = check_policy(uri)
    text = str(uri or "").strip()
    if kind == "data":
        data, declared = _decode_data_uri(text)
        return FetchedVideo(
            data=data, source="inline_base64", declared_mime=declared,
            source_uri=text,
        )
    data, declared, name = _load_inbox(text)
    return FetchedVideo(
        data=data, source="inbox", declared_mime=declared,
        source_uri=text, name=name,
    )


__all__ = [
    "ALLOWED_SCHEMES",
    "DATA_URI_MIME_PREFIXES",
    "FETCH_REFUSAL_KINDS",
    "MAX_VIDEO_BYTES",
    "FetchedVideo",
    "VideoFetchRefused",
    "check_policy",
    "classify_uri",
    "load",
]
