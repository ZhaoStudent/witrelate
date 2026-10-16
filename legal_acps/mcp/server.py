"""MCP server 的**协议层**：JSON-RPC 2.0 over **stdio**（换行分隔）。

启动方式（**必须模块方式**，理由见项目 README 的"一律模块方式跑"）：

    python -m legal_acps.mcp.server         # 音频鉴伪（本模块的默认族）
    python -m legal_acps.mcp.video_server   # 视频鉴伪（同一份协议代码）

════════════════════════════════════════════════════════════════════════
协议层与模态**无关**，所以它只实现一次
════════════════════════════════════════════════════════════════════════

报文分帧、生命周期、错误码、通知语义、stdout 纪律 —— 这些**没有一条**与
"被鉴伪的是什么媒体"有关。所以第二个 MCP 入口**不是**把本文件复制一份，
而是把它**参数化**：一个 `ToolFamily`（服务标识 + 开场白 + 工具表 + 调用函数）
把差异全部装下，协议代码一行不变。

⚠️ 判据：`run/meta_verify_video.py` 里有一条用例，把本文件的
    `family` 参数化**退回旧形态**（写死成音频族），要求视频侧立刻报失败 ——
    也就是说"两个入口共用同一份协议实现"这件事**是被钉住的**，
    而不是靠读者相信作者没有复制粘贴。

⚠️ 默认族是**音频**：`handle_message()` / `serve()` / `main()` 不带 `family`
    时的行为与本文件只有音频时**逐字相同** —— 于是音频侧那批已经过审的断言
    （`tools/verify_audio_mcp.py` 里"`tools/list` 吐出的就是音频 `TOOLS` 本尊"、
    "返回 3 个工具"、`SERVER_NAME` 含 `audio`）**一个字都不用改**。
    这一点是刻意保的：**改判据让它通过**，正是本项目最忌讳的那类修法。

════════════════════════════════════════════════════════════════════════
协议事实（照 MCP 规范，逐条落在代码里）
════════════════════════════════════════════════════════════════════════

传输
    stdin/stdout，**UTF-8**，**每条消息一行**（消息内部不得含裸换行）。
    stdout 是**协议专用**：本进程除协议消息外，一个字节都不往 stdout 写，
    所有日志走 stderr。写脏了 stdout 的后果不是"多一行日志"，
    而是客户端解析失败、整个 server 变砖，且现场看不出原因。
    ⚠️ 解码是**严格**的：非法 UTF-8 回一条 `-32700`，**绝不静默替换** ——
    `errors="replace"` 会让一个被改过的参数（句柄、案情说明）看起来完全正常
    （2026-10-04 实测：0xFF 变 U+FFFD 之后 `json.loads` 照样成功）。

生命周期
    initialize  →  notifications/initialized  →  tools/list  →  tools/call
    `initialize` 的应答里必须给 `protocolVersion`；客户端给的那版如果我们
    支持就**原样回它**（回一个更高的版本会让客户端按新语义解析而实际不是），
    否则回我们自己的最新版 —— 这一步由 `negotiate_protocol()` 单独判定，
    因为"版本协商"是最容易在客户端换版之后悄悄错掉的一环。

⚠️ 没有 `id` 的报文是**通知**：必须执行、**绝不回应**。
    回一条 `id: null` 的应答会让客户端把它当成一次应答不上的请求
    （多数实现会报 "unexpected response"），而 `notifications/initialized`
    恰恰是客户端**每次**都会发的第一条通知 —— 所以这里错了就是"连不上"。
    注意判据是「`id` 这个键**在不在**」，不是「`id` 是不是 None」：
    `{"id": null}` 是**请求**（规范允许 null id），必须回应。
    这条判据**对两个方向都成立**：带 `id` 的 `notifications/*` 就是请求，
    必须给它一条应答（本 server 回 -32601 —— 通知类方法我们不作为服务方法实现）。

错误码（只在**协议层**用，工具失败不走这里）
    -32700 解析失败    -32600 不是合法请求    -32601 方法不存在
    -32602 参数不合法  -32603 内部错误
    ⚠️ 工具执行失败**不用错误码**，走 `result.isError = true`
    —— 理由见 `audio_forensics._fail()`：模型需要读到"为什么不行、怎么改"。

`capabilities`
    只声明 `tools`（我们只有工具）。声明了却没实现的能力会让客户端
    去调一个不存在的方法，然后收到 -32601。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, BinaryIO

from . import audio_forensics

#: 本 server 的标识与版本（会出现在 `initialize` 的应答里）。
#: ⚠️ 这两个名字**必须留在本模块**（音频侧 `verify_audio_mcp.py` 直接 import 它们）。
SERVER_NAME = "legal-acps-audio-forensics"
SERVER_VERSION = "1.0.0"

#: `initialize` 的 `instructions`（音频族）。
#: 内容由 `verify_audio_mcp.py` 断言（必须点明"无法判定"不许转述成"没问题"）。
AUDIO_INSTRUCTIONS = (
    "离线音频编辑痕迹初检。三个工具：`audio_probe`（只看容器与波形可用性）、"
    "`audio_forensics_analyze`（九项检测 + 综合线索等级）、"
    "`audio_enf_track`（电网频率逐窗轨迹）。"
    "⚠️ 输入只接受 `inbox:<sha256>` 句柄或 `data:audio/...;base64,...` 内联字节："
    "**本服务不出网、不读本机文件**。"
    "⚠️ 输出没有「真/假」，只有线索与「无法判定」——"
    "后者是合法且经常正确的答案，不要把它转述成「没问题」。"
)

#: `-h` 里那一行（音频族）。
AUDIO_USAGE = (
    "本服务不出网、不读本机文件；音频引用只接受 inbox:<sha256> "
    "与 data:audio/...;base64,...。\n"
)

#: 我们**实现**的协议版本（新→旧）。协商时按这个列表判断能不能满足客户端。
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

#: 客户端没给版本（或不认识）时回哪个
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

#: JSON-RPC 错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass(frozen=True)
class ToolFamily:
    """一个「工具族」= 一个工具模块 + 它自己的服务标识与开场白。

    ⚠️ 协议层对**这里面的任何内容都一无所知**（它只负责把报文搬对地方）。
    这正是本类型存在的意义：把"与模态有关的全部差异"收进一个数据对象，
    于是协议代码只实现一次 —— 而不是每个模态抄一份 `server.py`。

    ⚠️ **`module` 存的是模块本身，不是 `module.TOOLS` / `module.call_tool`。**
    这一条是 2026-10-04 实测出来的，代价不小：

        `tools/verify_audio_mcp.py` 里有一对成对断言 —— "同一次内部崩溃，
        通知不许应答、请求必须回一条 `-32603`" —— 它是靠
        `AF.call_tool = _boom` **monkeypatch 模块属性**来制造那次崩溃的。
        而本类型的初版在**导入期**就把 `call_tool` 绑成了函数引用 ⇒
        patch 打在模块上、协议层却还在调那个旧引用 ⇒ **崩溃根本没发生**。
        于是那对断言里**前一条照样报「通过」**（没崩，通知自然没有应答 ——
        结论对、理由全错），后一条才失败。

        「判据静默降级、而成对断言里有一条仍绿」是最难发现的一类退化。
        所以这里**一律按模块访问**（`family.module.TOOLS` /
        `family.module.call_tool`），让 patch 始终生效。

    ⚠️ 另一个好处：`TOOLS` 也因此是**每次现取**的，不存快照 ——
    "`tools/list` 吐出的就是 `TOOLS` 本尊"这条断言才有意义。
    """

    name: str
    version: str
    instructions: str
    #: 提供 `TOOLS` 与 `call_tool` 的模块。**只存模块、不存它的属性。**
    module: Any
    #: **这个族自己的 `python -m` 入口名**（`-h` 里回显"该怎么启动"）。
    #:
    #: ⚠️ 它必须是**族自带的一个必填字段**，不能由 `_module_of()` 按"是不是音频族"
    #:    去猜（2026-10-05 实测的缺陷）：那种写法只有两个族时看着也对，
    #:    一旦加第三个族 ⇒ 它的 `-h` 会告诉用户去启动**视频**那个入口 ——
    #:    一句看起来完全正常的假话，而用户照它敲下去会起错服务。
    #:    做成**必填字段**之后，"新建一个族却忘了给入口"在构造期就不成立。
    entry: str
    #: `-h` 里那一行"引用只接受…"
    usage: str


#: 默认族。⚠️ 不带 `family` 参数时的一切行为都与"本文件只有音频"时逐字相同。
AUDIO_FAMILY = ToolFamily(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    instructions=AUDIO_INSTRUCTIONS,
    module=audio_forensics,
    entry="legal_acps.mcp.server",
    usage=AUDIO_USAGE,
)


def negotiate_protocol(requested: Any) -> str:
    """版本协商。**纯函数** —— 它是这一层唯一有分支的判定，所以单独可测。

    · 客户端给了我们支持的版本 → 回它自己那一版（不是最新版！）
    · 给了我们不认识的版本     → 回我们的最新版，由客户端决定要不要继续
    · 没给 / 不是字符串        → 回我们的最新版
    """
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return DEFAULT_PROTOCOL_VERSION


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def _result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _initialize_result(requested_version: Any, family: ToolFamily = AUDIO_FAMILY) -> dict[str, Any]:
    return {
        "protocolVersion": negotiate_protocol(requested_version),
        "capabilities": {
            # 只声明 tools，且 `listChanged: False`（我们的工具表是编译期常量）
            "tools": {"listChanged": False},
        },
        "serverInfo": {"name": family.name, "version": family.version},
        "instructions": family.instructions,
    }


def handle_message(message: Any, *, family: ToolFamily = AUDIO_FAMILY) -> dict[str, Any] | None:
    """处理一条**已解析**的报文。返回要写回去的应答；`None` = 这是通知，不回应。

    ⚠️ 这个函数是纯的（不发 IO），所以整个协议语义都能被逐条断言 ——
    见 `tools/verify_audio_mcp.py`。把"收发"与"语义"分开，是为了让
    "没有 id 的通知不回应"这种细节能被钉住，而不是靠肉眼读循环。

    ⚠️ `family` 只影响**工具表与开场白**，一个协议分支都不影响 ——
    这正是"协议与模态无关"这句话在签名上的样子。
    """
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "报文必须是 JSON 对象。")
    if message.get("jsonrpc") != "2.0":
        return _error(message.get("id"), INVALID_REQUEST, "缺失或错误的 jsonrpc 版本字段（必须是 \"2.0\"）。")

    method = message.get("method")
    if not isinstance(method, str) or not method:
        return _error(message.get("id"), INVALID_REQUEST, "缺失或非法的 method 字段。")

    # ⚠️ 判据是「`id` 这个键在不在」，不是「是不是 None」：
    #    `{"id": null, "method": ...}` 是**请求**，必须回应（规范允许 null id）；
    #    而完全没有 `id` 键的是通知，**绝不回应**。
    is_notification = "id" not in message
    msg_id = message.get("id")
    params = message.get("params")

    if method.startswith("notifications/") and is_notification:
        # 通知一律不回应，且**未知通知也不报错**（规范明确要求）。
        # ⚠️ 条件里的 `and is_notification` 不是多余的：本函数的判据是
        #    「`id` 这个键在不在」（见上），那么**带 id 的 `notifications/*`
        #    就是请求**，必须回应 —— 原先这里无条件返回 None，于是那种报文
        #    会**一条应答都拿不到**，客户端一直等。而它自己的文档与代码
        #    在这一点上互相矛盾（文档说有 id 就是请求）。这类报文现在
        #    落到下面的 else 分支，回 -32601（我们确实不把它当服务方法实现）。
        return None

    if method == "initialize":
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        reply: dict[str, Any] | None = _result(msg_id, _initialize_result(version, family))
    elif method == "ping":
        reply = _result(msg_id, {})
    elif method == "tools/list":
        # ⚠️ 吐的是 `family.module.TOOLS` **本尊**（不是另抄一份清单，也不是
        #    导入期存的快照）—— 音频侧那条"逐项相等"的断言正是靠这一点才有意义。
        reply = _result(msg_id, {"tools": list(family.module.TOOLS)})
    elif method == "tools/call":
        if not isinstance(params, dict):
            reply = _error(msg_id, INVALID_PARAMS, "tools/call 的 params 必须是对象。")
        elif not isinstance(params.get("name"), str):
            reply = _error(msg_id, INVALID_PARAMS, "tools/call 缺少 name 字段。")
        else:
            # ⚠️ 工具级失败**不走错误码**：`call_tool` 永远返回 `content` + `isError`。
            # ⚠️ 按**模块属性**取 `call_tool`（不是早先绑好的引用）—— 理由见
            #    `ToolFamily` 的 docstring：monkeypatch 必须仍然生效。
            reply = _result(msg_id, family.module.call_tool(
                params["name"], params.get("arguments")))
    else:
        # 未知方法：请求回 -32601；通知不回应（否则客户端会看到一条无主的应答）。
        reply = None if is_notification else _error(
            msg_id, METHOD_NOT_FOUND, f"本 server 不实现方法 {method!r}。",
            {"implemented": ["initialize", "ping", "tools/list", "tools/call"]},
        )

    if is_notification:
        return None
    return reply


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------

def read_message(stream: BinaryIO) -> tuple[dict[str, Any] | None, str | None]:
    """读一条消息。返回 `(报文, 错误文本)`；两者都为 `None` 表示流结束。

    ⚠️ 只按**换行分隔**（MCP stdio 传输的规定），不实现 LSP 那套
    `Content-Length:` 头。理由：两种分帧混在一个流里判错时，
    错误会表现成"消息内容被截断"，而截断的 JSON 有时**恰好能解析成功**
    （比如只吃掉了首尾各一小段）—— 那比解析失败危险得多。
    宁可对着一个明确的格式，不要对着两种格式的含糊合集。
    """
    while True:
        line = stream.readline()
        if not line:
            return None, None            # EOF
        # ⚠️ **严格解码**。原先写的是 `errors="replace"`，而它的实际表现是
        #    「静默改数据」：2026-10-04 实测 —— 报文里一个 0xFF 被替换成
        #    U+FFFD 之后 `json.loads` **照样解析成功**，于是工具收到一个被改过
        #    的参数（`inbox:<sha256>` 句柄、或一段案情说明），而调用方永远
        #    不知道自己的字节被动了。传输层违规必须**报错**，不许"修好再往下传"。
        try:
            text = line.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            # 与"不是 JSON"分开报：前者是**编码/传输**问题，后者是**内容**问题，
            # 调用方要改的地方不一样。
            return None, f"报文不是合法 UTF-8（传输层违规）：{exc}"
        if not text:
            continue                     # 空行不是消息，忽略而不是报错
        try:
            parsed = json.loads(text)
        except (ValueError, UnicodeDecodeError) as exc:
            return None, f"JSON 解析失败：{exc}"
        return parsed, None


def write_message(stream: BinaryIO, payload: dict[str, Any]) -> None:
    """写一条消息：**紧凑 JSON + 一个换行 + 立刻 flush**。

    ⚠️ `ensure_ascii=False`：我们的正文全是中文，转义成 `\\uXXXX` 会让
    token 数翻倍、可读性归零。JSON 允许 UTF-8 原文，MCP 也要求 UTF-8。
    ⚠️ 必须 flush：管道是块缓冲的，不 flush 的表现是"客户端一直等"，
    而服务端看起来一切正常。
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    stream.write(body.encode("utf-8") + b"\n")
    stream.flush()


def serve(
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    *,
    family: ToolFamily = AUDIO_FAMILY,
) -> int:
    """主循环。读到 EOF 就正常退出（父进程关管道 = 该结束了）。"""
    in_stream = stdin if stdin is not None else sys.stdin.buffer
    out_stream = stdout if stdout is not None else sys.stdout.buffer

    while True:
        message, error = read_message(in_stream)
        if message is None and error is None:
            return 0
        if error is not None:
            write_message(out_stream, _error(None, PARSE_ERROR, error))
            continue
        try:
            reply = handle_message(message, family=family)
        except Exception as exc:  # noqa: BLE001 - 协议层兜底：一条坏消息不能弄死整个 server
            if isinstance(message, dict) and "id" not in message:
                # ⚠️ **通知出错也不能回应。** 这一支原先会走下面的 `_error(None, …)`，
                #    也就是往流里写一条 `id: null` 的错误 —— 而客户端会把它当成
                #    "一条应答不上的请求"（多数实现报 "unexpected response"），
                #    正是本文件开头那条规则要防的事。既然判据是"没有 id 就是通知"，
                #    那**成功与失败都不该有应答**。只在 stderr 留一条痕迹。
                sys.stderr.write(
                    f"[{family.name}] 处理通知时出错（按规范不回应）："
                    f"{type(exc).__name__}: {exc}\n"
                )
                reply = None
            else:
                # 拿得到 id 就带着 id 回（客户端才能把错误对应到自己的请求上）。
                msg_id = message.get("id") if isinstance(message, dict) else None
                reply = _error(
                    msg_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}",
                    {"hint": "这是本 server 的缺陷，请连同触发它的那条报文一起反馈。"},
                )
        if reply is not None:
            write_message(out_stream, reply)


def main(argv: list[str] | None = None, *, family: ToolFamily = AUDIO_FAMILY) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        if args[0] not in ("-h", "--help"):
            # ⚠️ 未知参数**不许默默进 `serve()`**（2026-10-05 体检 B7）：
            # `serve()` 会立刻在 stdin 上阻塞读 JSON-RPC 报文，而用户只是把
            # `-help` 拼成了单横线（或想看版本）⇒ 表现是"敲了回车没反应、
            # 像卡住了"，而它其实在等你发一条 protocol 报文。
            # 那比报错更难诊断：**看不出是自己敲错了，还是服务坏了**。
            sys.stderr.write(
                f"无法识别的参数 {args[0]!r}。本入口不接受任何参数，"
                f"直接运行 `python -m {family.entry}` 即可（它是 stdio 服务，"
                f"会等你发 JSON-RPC 报文）。要看说明用 `-h` / `--help`。\n"
            )
            return 2
        sys.stderr.write(
            f"{family.name} {family.version}\n"
            f"用法：python -m {_module_of(family)}\n"
            "传输：JSON-RPC 2.0 over stdio（每条消息一行 UTF-8）。\n"
            + family.usage
        )
        return 0
    return serve(family=family)


def _module_of(family: ToolFamily) -> str:
    """`-h` 里回显"该怎么启动"。**取自族自己的 `entry` 字段**。

    ⚠️ 初版写的是 `if family is AUDIO_FAMILY else "…video_server"`：只有两个族时
    看着也对，可**第三个族会拿到视频那个入口** —— 一句看起来正常、实际指错路的
    假话，而用户照它敲下去会起错服务。（2026-10-05 体检 B5）
    ⇒ 现在入口名是**构造族时必须给的东西**，本函数只剩下"把它拿出来"。
    """
    return family.entry


if __name__ == "__main__":  # pragma: no cover - 由 `python -m` 触发
    raise SystemExit(main())
