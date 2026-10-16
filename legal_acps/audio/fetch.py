"""取音频：把「调用方给的那个引用」变成 `analyze()` 能吃的字节。

════════════════════════════════════════════════════════════════════════
与图像侧**刻意不对称**：这里一条出网通道都没有
════════════════════════════════════════════════════════════════════════

图像侧 `evidence/fetch.py` 有一条白名单出网通道（`EVIDENCE_IMAGE_ALLOWED_HOSTS`），
因为模型工具层的 schema 只允许传 URL（`TOOL_CONTENT_INPUT_SCHEMA` 的 uri 字段
原文是 "Absolute remotely reachable content URI."），而**看图**这件事的前提是
把图弄进来，所以那条出网口子是"不得已而开"的。

音频侧**没有这个不得已**：证据音频的正规来路是
「任务附件」（`AudioPart` 带字节 → 收件箱句柄）或「调用方直接内联 base64」，
两者都在 AIP 的双向 mTLS 通道内，都不需要本进程去访问调用方给的地址。
于是这里的策略不是"白名单",而是**一条出口都不留**：

    · `inbox:<sha256>`  —— 无条件放行（字节已在本进程，句柄是**内容哈希不是路径**）
    · `data:audio/*;base64,...` —— 无条件放行（自带载荷，不出网）
    · 其余一切（https / http / file / 裸路径 / ftp …）—— **一律拒绝**

## 为什么不"顺手也加一条白名单"

因为加一条出网通道，就要在**同一台机器上**多维护一份"哪些主机可信"的配置，
而它的收益只覆盖一类场景：「调用方只肯给地址，不肯给字节」。
音频这种场景本来就少（音频文件动辄几 MB，内联 base64 会让请求体膨胀，
所以调用方通常直接当附件发）；而它的代价与图像侧同源：
**任何能给我发任务的对象，都能驱使本进程去访问任意地址**（SSRF / 内网探测）。
更关键的是：这个口子一旦存在，就会有人开始依赖它，
而音频链的"测不了"本来就是常态（压缩格式解不出波形），
多一条取件路并不会把结论从"测不了"变成"能测"。

结论：**能力做小，比做全更符合这条链的实际**。
调用方要做的是把字节交给我们（附件或内联），而不是把地址交给我们。

⚠️ 但要说清代价，别把"没有"说成"不用"：
调用方拿到"拒绝"时，**必须知道该改什么**。所以每条拒绝都附带可执行的出路
（见 `pipeline._FIXES`），而不是一句"不支持"。

════════════════════════════════════════════════════════════════════════
体积上限为什么是 16 MiB（而图像侧是 8 MiB）
════════════════════════════════════════════════════════════════════════

两个数字必须与 `decode.MAX_ANALYSIS_FRAMES` **同量级**，否则会出现
"上限卡在分析窗的腰上"这种荒唐情形：文件允许通过，但一进来就被截断分析。

    16 MiB 的 16 位单声道 PCM = 8,388,608 个采样
    44.1 kHz 下 = 190.2 秒
    而 `MAX_ANALYSIS_FRAMES = 8_000_000` = 181.4 秒

⇒ **未压缩录音的字节上限与波形域的分析窗几乎同长**，两者都不至于静默砍掉
另一半。刻意不为压缩格式放大上限：那些格式我们本来就没解码器，
放进来也只是多读几 MB 字节去数帧头。
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Literal

from ..evidence import inbox

#: 音频字节上限。口径见模块 docstring（与 `decode.MAX_ANALYSIS_FRAMES` 同量级）。
MAX_AUDIO_BYTES = 16 * 1024 * 1024

#: `data:` URI 允许的 MIME 前缀。`audio/` 与 `application/ogg`（Ogg 家族的常见写法）
#: 都在内 —— 后者不是笔误：`.ogg` 在浏览器与不少工具里就报这个类型。
DATA_URI_MIME_PREFIXES = ("audio/", "application/ogg")

#: 允许的 scheme 全集。**这是本模块唯一的"能力清单"**，
#: 加一项就等于开一条新的取件路，所以它刻意短得能一眼看完。
ALLOWED_SCHEMES = ("inbox", "data")


#: 拒绝原因的**机器可读分类码**。
#:
#: ⚠️ 为什么要有它：上层（`pipeline.outcome`）必须给每种拒绝配一条**可执行的补救
#: 建议**，否则调用方只会想办法绕开边界。图像链是用"在原因文本里找关键词"来配的
#: —— 那要求"报错的文案"与"配建议的表"**持续保持一致**，而这两件事写在不同文件里。
#: 本模块自己的初版就是这么漂的：文案写的是"没有任何出网通道"，而上层的表里
#: 找的是"没有出网通道"，于是**那条最要紧的拒绝会静默降级成兜底建议**。
#:
#: 所以改成：本元组是分类码的**唯一定义**，抛异常时必带其一；
#: `verify_audio` 会断言"每个分类码都有专属建议"，漂移在断开的那一刻就被抓住。
FETCH_REFUSAL_KINDS = (
    "unsupported_scheme",   # 不是 inbox:/data: —— 含 https / http / file / 裸路径
    "data_uri_malformed",   # 没有逗号，载荷段缺失
    "data_uri_not_base64",  # 声明了 base64 之外的编码
    "data_uri_mime",        # 声明的类型不是 audio/*
    "data_uri_too_large",   # 解码前就按上界判超限
    "data_uri_bad_base64",  # 载荷本身解不开
    "data_uri_empty",       # 解码后为空
    "inbox_invalid",        # 句柄不合法 / 没登记 / 字节不可读
    "too_large",            # 拿到字节后才发现超限
)


class AudioFetchRefused(Exception):
    """取音频被策略拒绝（不是网络故障、不是格式故障）。

    ⚠️ **调用方必须把它如实转述**，不要吞掉换成"音频无法分析" ——
    两者的补救动作完全不同：前者是"换个方式把字节给我"，后者是"这段音频本身有问题"。

    `kind` 取自 `FETCH_REFUSAL_KINDS`，供上层配补救建议用（见那里的注释）。
    """

    def __init__(self, reason: str, kind: str = "unsupported_scheme") -> None:
        super().__init__(reason)
        self.kind = kind


@dataclass(frozen=True)
class FetchedAudio:
    data: bytes
    #: 与 `AudioEvidenceResult.fetched_from` 同一取值域 —— **溯源字段，不许含糊**。
    #: 注意这里只有两种取值：本模块结构上不可能产出 `network`。
    source: Literal["inline_base64", "inbox"]
    declared_mime: str | None
    #: 调用方给的原始引用（报告里回显，便于复核"这份字节是从哪个引用取来的"）。
    source_uri: str
    #: 收件箱里登记的名字（若有）。仅作提示，不参与任何判定。
    name: str | None = None


def classify_uri(uri: str) -> str:
    """把引用归成 `'inbox' | 'data' | 'other'`。**纯函数，判据全在这里。**

    刻意不返回主机名：本模块没有任何按主机判定的分支，
    返回一个永远用不上的字段只会让人以为"还有个白名单要看"。
    """
    text = str(uri or "").strip()
    low = text.lower()
    if low.startswith(inbox.HANDLE_PREFIX):
        return "inbox"
    if low.startswith("data:"):
        return "data"
    return "other"


#: ⚠️ 这里**刻意不给"可以这样改"的建议文案**。理由：补救建议必须只有一处定义，
#: 否则"报错时说的话"与"工具返回的 how_to_fix"会各自演化，调用方看到两份不同的出路。
#: 出路统一由 `pipeline._FIXES` 按 `kind` 配（`verify_audio` 断言一个字都不漏）。
#: 本模块只负责把**事实**说准：哪个引用、为什么不收、支持的是哪两种。


def check_policy(uri: str) -> str:
    """判定该引用能不能取；不能取就抛 `AudioFetchRefused`。返回归一后的 scheme。"""
    kind = classify_uri(uri)
    if kind in ALLOWED_SCHEMES:
        # ⚠️ 这里**不校验句柄是否真的登记过** —— 那是 `inbox.resolve()` 的职责。
        #    "句柄合不合法"若有第二个判据，早晚会与收件箱不一致（同一件事两处判定）。
        return kind
    text = str(uri or "").strip()
    raise AudioFetchRefused(
        f"不支持的音频引用 {text[:48]!r}：本入口只认 inbox:<sha256> 与 "
        "data:audio/...;base64,... 两种引用 —— **音频侧没有任何出网通道，"
        "也不读本机文件**。把字节交给本进程（任务附件或内联），"
        "而不是把地址交给我们。",
        "unsupported_scheme",
    )


def _decode_data_uri(uri: str) -> tuple[bytes, str | None]:
    header, comma, payload = uri.partition(",")
    if not comma:
        raise AudioFetchRefused(
            "data: URI 里没有逗号分隔的载荷部分 —— 这个 URI 是残缺的。",
            "data_uri_malformed",
        )
    meta = header[len("data:"):]
    declared = meta.split(";")[0].strip() or None
    if ";base64" not in meta.lower():
        raise AudioFetchRefused(
            "data: URI 必须是 base64 编码（本项目只接受二进制音频）。",
            "data_uri_not_base64",
        )
    if declared and not declared.lower().startswith(DATA_URI_MIME_PREFIXES):
        raise AudioFetchRefused(
            f"data: URI 声明的类型 {declared!r} 不是音频。",
            "data_uri_mime",
        )
    # ⚠️ 大小检查必须在**解码之前**。先 `b64decode` 再查上限，等于
    #    "判它超限"这件事本身就要先把整个载荷解码进内存，上限形同虚设。
    #    base64 是 3 字节 → 4 字符，`len(payload) * 3 // 4` 是解码后长度的**上界**；
    #    用上界做闸门是刻意的：方向 fail-closed，代价只是要求调用方少传两个字节。
    estimated = len(payload) * 3 // 4
    if estimated > MAX_AUDIO_BYTES:
        raise AudioFetchRefused(
            f"data: URI 载荷过大（base64 {len(payload)} 字符，解码后约 {estimated} 字节，"
            f"超过 {MAX_AUDIO_BYTES} 字节上限）—— **不做截断**，截断会让时长、"
            "容器长度、尾部标签全部失真。",
            "data_uri_too_large",
        )
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AudioFetchRefused(
            f"data: URI 的 base64 无法解码：{exc}", "data_uri_bad_base64"
        ) from None
    if not data:
        raise AudioFetchRefused("data: URI 解码后为空。", "data_uri_empty")
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioFetchRefused(
            f"音频 {len(data)} 字节，超过 {MAX_AUDIO_BYTES} 字节上限。",
            "data_uri_too_large",
        )
    return data, declared


def _load_inbox(uri: str) -> tuple[bytes, str | None, str | None]:
    """从收件箱按句柄取字节 —— 即"随任务附件进来的那一份"。"""
    try:
        data, declared, name = _read_inbox(uri)
    except inbox.InboxRefused as exc:
        # 归一到同一族异常：调用方只需要记住"取音频失败一律是 AudioFetchRefused"，
        # 不必知道收件箱这个内部机构的存在。
        raise AudioFetchRefused(f"收件箱引用无效：{exc}", "inbox_invalid") from None
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioFetchRefused(
            f"收件箱里的音频 {len(data)} 字节，超过 {MAX_AUDIO_BYTES} 字节上限。",
            "too_large",
        )
    return data, declared, name


def _read_inbox(handle: str) -> tuple[bytes, str | None, str | None]:
    """`inbox.load()` 加上名字。**名字只用于回显**，不参与任何判定。

    ⚠️ 字节必须经 `inbox.load()` 读 —— 「字节不可读 ⇒ `InboxRefused`」那条判据
    **只住在那里**。（2026-10-03 复查夹出：原写法在这里自己
    `item.path.read_bytes()`，把 inbox 的包装**抄丢了** —— 文件在 resolve 之后、
    读取之前被清掉时，跑出来的是裸 `OSError`，落进 `outcome()` 的兜底分支拿
    **通用**建议，而不是句柄无效那条专属出路。图像侧一直是走 `inbox.load` 的 ——
    同一份逻辑抄三处，抄着抄着就漂了，正是「同一判定写两处必然漂移」的现行犯。
    与 `video/fetch._read_inbox` 同一天、同一条修法。）
    """
    data, declared = inbox.load(handle)
    item = inbox.resolve(handle)
    return data, (declared or None), item.name


def load(uri: str) -> FetchedAudio:
    """按策略取音频字节。被拒绝时抛 `AudioFetchRefused`，调用方必须如实转述。"""
    kind = check_policy(uri)
    text = str(uri or "").strip()
    if kind == "data":
        data, declared = _decode_data_uri(text)
        return FetchedAudio(
            data=data, source="inline_base64", declared_mime=declared,
            source_uri=text,
        )
    data, declared, name = _load_inbox(text)
    return FetchedAudio(
        data=data, source="inbox", declared_mime=declared,
        source_uri=text, name=name,
    )


__all__ = [
    "ALLOWED_SCHEMES",
    "DATA_URI_MIME_PREFIXES",
    "FETCH_REFUSAL_KINDS",
    "MAX_AUDIO_BYTES",
    "AudioFetchRefused",
    "FetchedAudio",
    "check_policy",
    "classify_uri",
    "load",
]
