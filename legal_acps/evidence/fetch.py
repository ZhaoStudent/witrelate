"""取图：把「模型给的 URI」变成 `analyze()` 能吃的字节。

## 为什么需要这个模块（这是整条链路最关键的约束）

AIP 的三层附件能力，只有**协议层与 wit 代码层**支持"直接传字节"：

    AIP 协议      FileDataItem.uri **或** bytes(base64)          ← 都支持
    wit 代码层    delegate(..., request=ContentInput)           ← ImagePart.data 可传字节
    模型工具层    TOOL_CONTENT_INPUT_SCHEMA 只暴露 uri          ← **只有 url**

模型工具层的 schema 是 `required: [type, uri, mime_type]`、`additionalProperties: false`，
uri 的描述原文是 **"Absolute remotely reachable content URI."**
⇒ **模型发起的委派只能给 URL；只有代码发起的委派才能给字节。**
所以只要图片由模型转述进链路，就必然需要一个"把 URL 变成字节"的入口，就是这里。

## ⚠️ 它是本项目**唯一一处主动向外部地址发起请求**的环节

其余 Agent 都只在双向 mTLS 的 ACPs 通道内通信（mTLS 保证"对方是谁"），
而这里要把**调用方给的地址**拿去请求 —— 对方地址由不可信输入决定。
注意它的性质：这**不是**给本项目新开了一个入站端口，**没有**新增无 mTLS 的监听入口；
风险在**出站方向**：SSRF / 内网探测 / 拿本机当跳板。
所以策略是**默认拒绝（fail-closed）**，不是默认放行：

  · 默认**只允许 `data:` URI**（自带字节、不出网、完全可离线断言）；
  · `inbox:` 句柄（随任务附件进来的字节，见 `evidence/inbox.py`）**无条件放行** ——
    它同样不出网，而且只能命中"本进程登记过的那份字节"：句柄是**内容哈希而不是路径**，
    调用方引用的是内容而非位置；
  · 出网必须显式配置 `EVIDENCE_IMAGE_ALLOWED_HOSTS`（逗号分隔）**白名单**；
    白名单为空 = **禁止一切出网取图**，而不是"允许一切"；
  · 只走 https；**不跟随重定向**；连接/读取超时；**下载字节数有硬上限**；
  · 内容按魔术字节复核（`sniff_media_type`），与 Content-Type 声明不符时如实记录。

⚠️ `file://` 本地读取**默认关闭，且只能由入口代码显式打开**（`enable_local_file()`）。
刻意做成**进程内开关而不是环境变量**：环境变量会被 systemd、shell rc、父进程一路
继承下来，"服务模式不该有"这条纪律就守不住了；做成函数以后，**服务入口根本不调它**，
常驻进程里 `file://` 在结构上不可达 —— 这与"句柄只认内容哈希"是同一类做法：
把安全性质落在"结构上做不到"，而不是"记得别那么做"。

⚠️ **残余风险（已知、未消除，如实记下而不是假装没有）**：白名单比的是**主机名**，
而 DNS 解析发生在请求那一刻 —— 一个被攻陷（或管理员误加进白名单）的域名，仍可以把解析
结果指向内网地址（DNS rebinding）。这里刻意**不做 IP 钉扎**：白名单是管理员手工维护的
可信名单，加一层 IP 校验会把「图床换 CDN / 换节点」变成线上故障，得不偿失。
真正降风险的是「白名单默认空 + 只填自家图床」这条纪律，而不是在这里加一层脆弱的校验。

## 为什么这里的 fail-closed 与委派护栏的 fail-open 不矛盾

`DebateChainGuardHooks` 的身份闸门是 **fail-open** 的（拿不到自己身份就不校验），
因为那条护栏**误杀会掐断自己的链路**，代价比少一层保护大。
这里恰恰相反：它是**面向不可信输入的信任边界**，放行一次的代价是被 SSRF/内网探测。
判据不是"统一风格"，而是**放行与拦截各自的最坏后果**。
"""

from __future__ import annotations

import base64
import binascii
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import inbox

#: 下载字节数硬上限。 8 MB 够绝大多数司法证据图；超限即拒绝而不是截断
#: （截断后的图会让所有像素域统计失真，还不如不给结论）。
MAX_FETCH_BYTES = 8 * 1024 * 1024

#: 取图超时（秒）
FETCH_TIMEOUT = 10.0

#: `data:` URI 允许的 image 子类型（其余一律拒绝）
DATA_URI_SCHEMES = ("data:",)

#: `file://` 是否放行。**默认 False，且只由 `enable_local_file()` 打开。**
#: 用模块级开关而不是环境变量，理由见模块 docstring。
_local_file_enabled: bool = False


def enable_local_file(enabled: bool = True) -> None:
    """允许 / 禁止 `file://` 本地读取。

    ⚠️ **只应由 CLI、自测、演示入口调用。** 服务入口（`run_agent.py` 的 serve 分支）
    不调用它，所以常驻进程里 `file://` 不可达 —— 这是本函数存在的全部意义。

    这不是"多余的谨慎"：`file://` 一旦打开，本工具就同时是一个**任意的本地文件
    读取原语**（路径由调用方给）。在 7 个角色共处的那台机器上，
    `.ssh/acps-hk-key.pem`（我们自己的 mTLS 私钥）也在射程内。
    """
    global _local_file_enabled
    _local_file_enabled = bool(enabled)


def local_file_enabled() -> bool:
    """当前是否允许 `file://`（给断言脚本和排障用）。"""
    return _local_file_enabled


def resolve_local_file_flag(*, requested: bool, serving: bool) -> bool:
    """决定本次是否开启本地读取。**纯函数** —— 规则只有一条，但必须只有一处。

        · 服务模式（`serving=True`）**一律不开启**，哪怕调用方要求了；
        · 其余情况照请求执行。

    ⚠️ 为什么把这条规则单独抽成一个函数，而不是写在 `run_agent.py` 的 if 里：
    它是本策略**唯一的安全性质**（"常驻进程里结构上做不到读本地文件"），
    写在分支里就只能靠人工 review 保证；抽成纯函数才能被断言钉住 ——
    而"服务模式不该读本地文件"这种事，恰恰是**最容易在重构时被顺手改坏**的。
    """
    if serving:
        return False
    return bool(requested)


class FetchRefused(Exception):
    """取图被策略拒绝（不是网络故障）。**调用方应把它如实转述，不要吞掉。**"""


@dataclass(frozen=True)
class Fetched:
    data: bytes
    #: 与 `ImageEvidenceResult.fetched_from` 同一取值域 —— 溯源字段，不许含糊。
    source: Literal["inline_base64", "network", "local_file", "inbox"]
    declared_mime: str | None


def allowed_hosts() -> frozenset[str]:
    """出网白名单。**未配置 = 空集 = 禁止一切出网取图**（fail-closed）。"""
    raw = os.environ.get("EVIDENCE_IMAGE_ALLOWED_HOSTS", "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def classify_uri(uri: str) -> tuple[str, str]:
    """把 URI 归成 ('data'|'inbox'|'http'|'https'|'file'|'other', 主机或空串)。

    纯函数、可离线断言 —— 取图策略的判据全在这里，不在网络代码里。
    """
    text = uri.strip()
    low = text.lower()
    if low.startswith(inbox.HANDLE_PREFIX):
        return "inbox", ""
    if low.startswith("data:"):
        return "data", ""
    if low.startswith("https://"):
        return "https", _host_of(text[len("https://"):])
    if low.startswith("http://"):
        return "http", _host_of(text[len("http://"):])
    if low.startswith("file://"):
        return "file", ""
    return "other", ""


def _host_of(rest: str) -> str:
    authority = rest.split("/", 1)[0]
    return authority.rsplit("@", 1)[-1].split(":", 1)[0].strip().lower()


def check_policy(uri: str) -> tuple[str, str]:
    """按策略判定该 URI 能不能取；不能取就抛 `FetchRefused`。返回 (类型, 主机)。"""
    kind, host = classify_uri(uri)
    if kind in ("data", "inbox"):
        # 两者都不来自外部地址：`data:` 自带载荷，`inbox:` 指向本进程已登记的字节。
        # 取图策略真正要拦的是**出网**，所以这两条不走白名单判定 ——
        # 但"这个句柄是否真的登记过"由 `inbox.resolve()` 自己把关，
        # 策略层不重复实现它（同一件事两个判据，早晚会不一致）。
        return kind, host
    if kind == "file":
        # 本地路径读取默认关闭，**只能由入口代码显式打开**。见模块 docstring：
        # 它等于把本机文件系统暴露给调用方，而调用方给的路径无法预先约束。
        if _local_file_enabled:
            return kind, host
        raise FetchRefused(
            "拒绝读取本地文件（file://）—— 本入口默认关闭它，"
            "因为打开后它就是一个「任意本地文件读取」原语。"
            "如需在单机自测 / 演示里读取，只能由入口代码调用 "
            "fetch.enable_local_file() 显式打开（刻意不提供命令行开关与"
            "环境变量：两者都会被服务进程继承，纪律守不住）。"
        )
    if kind == "http":
        raise FetchRefused("拒绝明文 http 取图 —— 明文链路上的图片无法保证未被中途替换。")
    if kind != "https":
        raise FetchRefused(f"不支持的 URI 类型：{uri[:40]!r}")
    allow = allowed_hosts()
    if not allow:
        raise FetchRefused(
            "未配置白名单，禁止出网取图（这是默认行为，不是故障）。"
            "请设置 EVIDENCE_IMAGE_ALLOWED_HOSTS=<逗号分隔的主机名> 后再试。"
        )
    if host not in allow:
        raise FetchRefused(f"主机 {host!r} 不在白名单内。")
    return kind, host


def _decode_data_uri(uri: str) -> Fetched:
    header, _, payload = uri.partition(",")
    meta = header[len("data:"):]
    declared = meta.split(";")[0] or None
    if ";base64" not in meta.lower():
        raise FetchRefused("data: URI 必须是 base64 编码（本项目只接受二进制图像）。")
    if declared and not declared.lower().startswith("image/"):
        raise FetchRefused(f"data: URI 声明的类型 {declared!r} 不是图像。")
    # ⚠️ 大小检查必须在**解码之前**。初版是先 `b64decode` 再查上限 ——
    #    于是"判它超限"这件事本身就要先把整个载荷解码进内存：
    #    一个几百 MB 的 data: URI 会先把内存吃掉再被拒绝，上限形同虚设。
    #
    #    base64 是 3 字节 → 4 字符，所以 `len(payload) * 3 // 4` 是解码后长度的
    #    **上界**（尾部 `=` 填充会让真实长度再少 0~2 字节）。用上界来做闸门是刻意的：
    #    它可能把"刚好差 1~2 字节达标"的载荷也挡掉，但方向是 fail-closed，
    #    代价只是要求调用方少传两个字节，而不是先吃满内存再报错。
    if len(payload) * 3 // 4 > MAX_FETCH_BYTES:
        raise FetchRefused(
            f"data: URI 载荷过大（base64 {len(payload)} 字符，解码后约 {len(payload) * 3 // 4} 字节，"
            f"超过 {MAX_FETCH_BYTES} 字节上限）—— **不做截断**，截断会让所有统计失真。"
        )
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FetchRefused(f"data: URI 的 base64 无法解码：{exc}") from None
    if not data:
        raise FetchRefused("data: URI 解码后为空。")
    if len(data) > MAX_FETCH_BYTES:
        raise FetchRefused(f"图片 {len(data)} 字节，超过 {MAX_FETCH_BYTES} 字节上限。")
    return Fetched(data=data, source="inline_base64", declared_mime=declared)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """**不跟随重定向** —— 否则白名单可以被一次 302 绕过去。"""

    def redirect_request(self, *_args: object, **_kw: object):  # type: ignore[no-untyped-def]
        return None


def _fetch_http(uri: str) -> Fetched:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(uri, headers={"User-Agent": "legal-acps/evidence-image"})
    try:
        with opener.open(req, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip() or None
            declared_len = resp.headers.get("Content-Length")
            if declared_len and declared_len.isdigit() and int(declared_len) > MAX_FETCH_BYTES:
                raise FetchRefused(
                    f"对方声明 {declared_len} 字节，超过 {MAX_FETCH_BYTES} 字节上限。"
                )
            data = resp.read(MAX_FETCH_BYTES + 1)
    except FetchRefused:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            raise FetchRefused(
                f"对方返回重定向（{exc.code}）—— 本入口刻意不跟随重定向，"
                "以免白名单被绕开。请直接给出最终地址。"
            ) from None
        raise FetchRefused(f"取图失败：HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchRefused(f"取图失败：{exc}") from None
    if len(data) > MAX_FETCH_BYTES:
        raise FetchRefused(f"图片超过 {MAX_FETCH_BYTES} 字节上限（**不做截断**，截断会让统计失真）。")
    return Fetched(data=data, source="network", declared_mime=ctype)


def _load_inbox(uri: str) -> Fetched:
    """从收件箱按句柄取字节 —— 即"随任务附件进来的那一份"。"""
    try:
        data, declared = inbox.load(uri)
    except inbox.InboxRefused as exc:
        # 归一到同一族异常：调用方只需要记住"取图失败一律是 FetchRefused"，
        # 不必知道收件箱这个内部机构的存在。
        raise FetchRefused(f"收件箱引用无效：{exc}") from None
    if len(data) > MAX_FETCH_BYTES:
        raise FetchRefused(f"收件箱里的图片 {len(data)} 字节，超过 {MAX_FETCH_BYTES} 字节上限。")
    return Fetched(data=data, source="inbox", declared_mime=declared or None)


def load(uri: str) -> Fetched:
    """按策略取图。被拒绝时抛 `FetchRefused`，调用方必须如实转述。"""
    kind, _host = check_policy(uri)
    if kind == "data":
        return _decode_data_uri(uri)
    if kind == "inbox":
        return _load_inbox(uri)
    if kind == "file":
        path = Path(urllib.request.url2pathname(uri[len("file://"):]))
        data = path.read_bytes()
        if len(data) > MAX_FETCH_BYTES:
            raise FetchRefused(f"文件 {len(data)} 字节，超过上限。")
        # ⚠️ 来源必须标注成 `local_file` 而不是 `network`（初版图省事复用了 network）。
        #    `fetched_from` 是**溯源字段**，报告里要回答"这份字节是从哪来的"——
        #    来源写着"网络"而实际来自本机磁盘，是一条**看起来无害但会误导复核**的假信息。
        return Fetched(data=data, source="local_file", declared_mime=None)
    return _fetch_http(uri)


__all__ = [
    "DATA_URI_SCHEMES",
    "FETCH_TIMEOUT",
    "MAX_FETCH_BYTES",
    "FetchRefused",
    "Fetched",
    "allowed_hosts",
    "check_policy",
    "classify_uri",
    "enable_local_file",
    "load",
    "local_file_enabled",
    "resolve_local_file_flag",
]
