"""离线验证：音频鉴伪 **MCP server**（`legal_acps/mcp/`）。

**不联网、不需要 API key、不碰平台、不调模型。** 判据只有一个：`exit 0 = 全绿`。

用法（在 witrelate 目录下）：
    ./.venv/Scripts/python.exe -m tools.verify_audio_mcp

════════════════════════════════════════════════════════════════════════
它到底在测什么 —— 这一层的缺陷长什么样
════════════════════════════════════════════════════════════════════════

`tools/verify_audio.py` 已经把检测核心测穿了。本脚本测的是它**外面那一圈**：
把检测能力包成一个能被任意 MCP 客户端接进去的服务。这一圈的危险与检测层完全不同：

  · **看不见**。协议的错不会让报告变难看，它让**客户端连不上**或者**理解错**。
    最典型的一个：`notifications/initialized` 是客户端每次都会发的**第一条**通知，
    而我们回了一条 `id: null` 的应答 —— 多数客户端会报 "unexpected response"，
    现象是"这个服务器用不了"，而服务器日志里一片正常。
  · **容易被当成"翻译层不重要"**。所以真正要钉住的不是"翻译对不对"，
    而是**"工具层没有重新实现判据"**：同一条 uri，MCP 这条路与 ACPs 那条路
    必须给出**逐字相同**的结论。两处各写一份判据的后果，本项目已经吃过很多次
    （识别侧与抹除侧的认可集合漂移、两条鉴伪链对同一种失败给出不同档位）。

节次安排：

  第 1 节  工具清单：三个工具、schema 自洽、`TOOLS` 与处理器一一对应
  第 2 节  协议语义：逐条喂 `handle_message`（**它是纯函数，不发 IO**）
  第 3 节  工具语义：**所有失败都是正常返回**（`isError`），一个异常都不许漏
  第 3b 节 参数校验与失败归因：schema 的 `enum` 必须自己判、失败不许归错因
  第 4 节  **出口同源（本脚本最重要的一节）**：MCP 的 JSON ≡ ACPs 的 JSON
  第 5 节  真子进程端到端：stdio 传输 + 通知不回应 + stdout 不许有脏字节
  第 6 节  版本协商、传输层纯函数，以及**通知出错时不许有应答**
  第 7 节  按需导入面：不许出现"按导入路径碰运气"的符号
  第 8 节  **严格 UTF-8 这一条不在这里测**（`read_message` 与视频族**共用同一份**
            `legal_acps/mcp/server.py`，判据写在 `tools/verify_video_mcp.py` 第 2 节。
            在这里再写一遍就是"同一个判定写两处"，必然漂移 —— 所以此处只有一句指向）
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from legal_acps.audio import fix_for, outcome
from legal_acps.audio import forensics as audio_forensics
from legal_acps.audio.forensics import ENF_BASE_FREQS
from legal_acps.mcp import audio_forensics as AF
from legal_acps.mcp.server import (
    DEFAULT_PROTOCOL_VERSION,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SERVER_NAME,
    SERVER_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    handle_message,
    negotiate_protocol,
    read_message,
    serve,
)
from run import audio_samples as S

ROOT = Path(__file__).resolve().parent.parent
FAILED: list[str] = []


def attempt(fn, *args: object, **kwargs: object) -> tuple[Any, Exception | None]:
    """把「抛出来的异常」也折成**可比较的返回值**。

    ⚠️ 本文件的第一条纪律：被测代码抛异常时，断言脚本会带着 traceback 崩掉，
    而"崩掉（少打几行）"与"通过"在日志里几乎分不出来。折成返回值以后，
    它会走正常的 `check()` 通道，被报成一条**明确的失败**。
    """
    try:
        return fn(*args, **kwargs), None  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 - 边界：测试自己必须先站得稳
        return None, exc


def check(label: str, ok: bool, *notes: str) -> None:
    mark = "通过" if ok else "未达预期"
    if not ok:
        FAILED.append(label)
    print(f"  [{mark}] {label}")
    for note in notes:
        print(f"          {note}")


def text_of(result: Any) -> str:
    """把一条 `CallToolResult` 的正文拼起来（只看 `type == "text"` 的那些 part）。"""
    if not isinstance(result, dict):
        return ""
    parts = result.get("content")
    if not isinstance(parts, list):
        return ""
    return "\n".join(
        str(p.get("text", "")) for p in parts
        if isinstance(p, dict) and p.get("type") == "text"
    )


def payload_of(result: Any) -> Any:
    """取出 `_ok(...)` 附在末尾的那份可机读 JSON。取不到返回 `None`。"""
    body = text_of(result)
    marker = "```json\n"
    if marker not in body:
        return None
    tail = body.split(marker, 1)[1]
    tail = tail.split("\n```", 1)[0]
    try:
        return json.loads(tail)
    except ValueError:
        return None


def data_uri(data: bytes, mime: str = "audio/wav") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# 样本（**全部取自 `run/audio_samples.py`**，断言脚本不另造一份）
# ---------------------------------------------------------------------------

WAV_SMALL = S.wav_bytes(S.white_noise(1.0, seed=5))
WAV_SPEECH = S.wav_bytes(S.speech_like(2.0))
MP3_CONST = S.mp3_bytes(40)
WAV_URI = data_uri(WAV_SMALL)
MP3_URI = data_uri(MP3_CONST, "audio/mpeg")
#: 一份**不是音频**的字节（PNG 头），用来测"字节族"的失败路径。
NOT_AUDIO_URI = data_uri(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "audio/wav")


def main() -> int:
    # =======================================================================
    print("=" * 74)
    print("第 1 节 · 工具清单与 schema 自洽")
    print("=" * 74)

    names = [str(t.get("name", "")) for t in AF.TOOLS]
    check("三个工具、名字唯一且非空", len(names) == 3 and len(set(names)) == 3,
          f"实得：{names}")
    check("工具名与实现名一致（不是「计划要做的三个」）",
          set(names) == {"audio_probe", "audio_forensics_analyze", "audio_enf_track"},
          f"实得：{sorted(names)}")

    handlers = getattr(AF, "_HANDLERS", None)
    check("`TOOLS` 与处理器表**一一对应**（多了少了都会出现「列表里有、调不到」的空工具）",
          isinstance(handlers, dict) and set(handlers) == set(names),
          f"TOOLS={sorted(names)} / 处理器={sorted(handlers or {})}")

    schema_bad: list[str] = []
    for tool in AF.TOOLS:
        schema = tool.get("inputSchema")
        props = (schema or {}).get("properties") or {}
        if not (
            isinstance(schema, dict) and schema.get("type") == "object"
            and schema.get("required") == ["audio_uri"]
            # ⚠️ `additionalProperties: false` 不是洁癖：模型多写一个参数时，
            #    放开的话会被静默忽略 —— 而"收下了却没往下传"比没有这个参数更坏。
            and schema.get("additionalProperties") is False
            and set(props) >= {"audio_uri"}
            and (props.get("audio_uri") or {}).get("type") == "string"
            and len(str(tool.get("description", ""))) >= 20
            and len(str(tool.get("title", ""))) >= 2
        ):
            schema_bad.append(str(tool.get("name")))
    check("每个工具的 schema 自洽：object / required=[audio_uri] / 不许额外参数 / 有描述",
          not schema_bad, f"不合格：{schema_bad}")

    offline_ok = all(
        "不出网" in str((t.get("inputSchema", {}).get("properties", {}).get("audio_uri", {})).get("description", ""))
        for t in AF.TOOLS
    )
    check("`audio_uri` 的参数说明里**明写**「不出网、不读本机文件」（调用方要先知道边界在哪）",
          offline_ok)
    print()

    # =======================================================================
    print("=" * 74)
    print("第 2 节 · 协议语义（`handle_message` 是纯函数，逐条喂）")
    print("=" * 74)

    def reply_of(message: Any) -> tuple[Any, Exception | None]:
        return attempt(handle_message, message)

    # -- 请求/通知的判据是「`id` 这个键在不在」，不是「是不是 None」------------
    got, err = reply_of({"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("**没有 `id` 键的通知：绝不回应**（回了就是「连不上」—— 客户端每次都发这一条）",
          err is None and got is None, f"实得：{got!r} / 异常={err!r}")

    got, err = reply_of({"jsonrpc": "2.0", "method": "some/unknown/notification"})
    check("未知通知同样不回应、也**不报错**（规范明确要求）",
          err is None and got is None, f"实得：{got!r}")

    got, err = reply_of({"jsonrpc": "2.0", "id": None, "method": "ping"})
    check("`{\"id\": null}` 是**请求**、必须回应，且原样带回 `id: null`（规范允许 null id）",
          err is None and isinstance(got, dict) and got.get("id", "缺") is None
          and "result" in got, f"实得：{got!r}")

    # -- initialize / 版本协商 -------------------------------------------------
    got, err = reply_of({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2024-11-05"}})
    init = (got or {}).get("result") or {}
    check("`initialize` 回**客户端给的那一版**（回更高的版本会让它按新语义解析）",
          init.get("protocolVersion") == "2024-11-05",
          f"实得：{init.get('protocolVersion')!r}")
    check("`initialize` 声明 serverInfo 与 capabilities，且**只声明 tools**"
          "（声明了没实现的能力 ⇒ 客户端去调一个不存在的方法）",
          init.get("serverInfo") == {"name": SERVER_NAME, "version": SERVER_VERSION}
          and sorted(init.get("capabilities") or {}) == ["tools"],
          f"实得：{init.get('capabilities')!r}")
    check("`initialize` 的 instructions 点明「无法判定」是合法答案、不许转述成「没问题」",
          "无法判定" in str(init.get("instructions", ""))
          and "没问题" in str(init.get("instructions", "")))

    # -- tools/list -----------------------------------------------------------
    got, err = reply_of({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    listed = ((got or {}).get("result") or {}).get("tools")
    check("`tools/list` 吐出的就是 `TOOLS` 本尊（逐项相等，不是「另一份手抄的清单」）",
          isinstance(listed, list) and listed == list(AF.TOOLS),
          f"实得 {len(listed or [])} 个")

    # -- 错误码 ---------------------------------------------------------------
    # ⚠️ 每条都带**期望的 id**：报文本 Not-a-dict 时拿不到 id，只能回 `null` ——
    #    这与"把请求的 id 弄丢了"是两件不同的事，所以必须逐条写清期望值，
    #    而不是笼统地断言"都带回 3"。
    cases: list[tuple[str, Any, int, Any]] = [
        ("报文不是对象", ["not", "an", "object"], INVALID_REQUEST, None),
        ("jsonrpc 字段缺失", {"id": 3, "method": "ping"}, INVALID_REQUEST, 3),
        ("jsonrpc 版本错", {"jsonrpc": "1.0", "id": 3, "method": "ping"}, INVALID_REQUEST, 3),
        ("method 缺失", {"jsonrpc": "2.0", "id": 3}, INVALID_REQUEST, 3),
        ("method 为空串", {"jsonrpc": "2.0", "id": 3, "method": ""}, INVALID_REQUEST, 3),
        ("未知方法（有 id）", {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
         METHOD_NOT_FOUND, 3),
        ("tools/call 的 params 不是对象",
         {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": []},
         INVALID_PARAMS, 3),
        ("tools/call 缺少 name",
         {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}}, INVALID_PARAMS, 3),
        ("tools/call 的 name 不是字符串",
         {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": 7}},
         INVALID_PARAMS, 3),
    ]
    code_bad: list[str] = []
    for label, message, want, want_id in cases:
        got, err = reply_of(message)
        got_code = ((got or {}).get("error") or {}).get("code")
        got_has_id = isinstance(got, dict) and "id" in got
        if (err is not None or got_code != want
                or not got_has_id or (got or {}).get("id") != want_id):
            code_bad.append(
                f"{label}→code={got_code!r} id={(got or {}).get('id')!r}"
                f"（期望 {want}/{want_id}，异常={err!r}）"
            )
    check("九种协议层畸形输入，错误码逐条正确，且应答把请求的 id 带回去"
          "（拿不到 id 的那一种才回 `null`）",
          not code_bad, f"不符：{code_bad}")

    got, _ = reply_of({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    impl = ((got or {}).get("error") or {}).get("data") or {}
    check("未知方法的错误里**列出已实现的方法**（调用方要知道该改成什么，而不是收到一句「不支持」）",
          sorted(impl.get("implemented") or []) == ["initialize", "ping", "tools/call", "tools/list"],
          f"实得：{impl!r}")

    # -- 未知方法 + 无 id ⇒ 与未知通知同一条路：不回应 -------------------------
    got, err = reply_of({"jsonrpc": "2.0", "method": "resources/list"})
    check("未知方法但**没有 id** ⇒ 不回应（否则客户端会收到一条无主的应答）",
          err is None and got is None, f"实得：{got!r}")

    # -- 判据的另一半：**带 id 的 `notifications/*` 是请求** -------------------
    got, err = reply_of({"jsonrpc": "2.0", "id": 9, "method": "notifications/initialized"})
    check("**带 `id` 的 `notifications/*` 是请求、必须给它一条应答**"
          "（判据「`id` 键在不在」对两个方向都成立；不回应的话客户端会一直等）",
          err is None and isinstance(got, dict) and got.get("id") == 9
          and ((got.get("error") or {}).get("code")) == METHOD_NOT_FOUND,
          f"实得：{got!r}",
          "（回 -32601 是因为通知类方法我们不作为服务方法实现 —— 关键是"
          "「必须有一条应答」，而不是沉默地把它当成通知）")

    # -- 工具名未知**不是** JSON-RPC 错误 -------------------------------------
    got, err = reply_of({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "nope", "arguments": {}}})
    body = text_of((got or {}).get("result"))
    check("未知**工具名**走 `result.isError=true` 而**不是** -32601 ——"
          "客户端对协议错误的显示是一句通用的「tool failed」，那句话对模型没有任何指导作用",
          err is None and "error" not in (got or {})
          and ((got or {}).get("result") or {}).get("isError") is True,
          f"实得：{json.dumps(got, ensure_ascii=False)[:120]}")
    check("……并且正文里**列出可用工具**（这才是调用方真正需要的信息）",
          all(n in body for n in names), f"正文：{body[:100]}")

    # -- 报文本身永远不许把 handle_message 弄崩 -------------------------------
    weird: list[Any] = [
        None, 0, "", [], {}, {"jsonrpc": "2.0"},
        {"jsonrpc": "2.0", "id": 1, "method": None},
        {"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "audio_probe",
                                                                      "arguments": "字符串"}},
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": "不是对象"},
    ]
    boom = [repr(m)[:40] for m in weird if attempt(handle_message, m)[1] is not None]
    check("九种更离谱的报文也不许让协议层抛异常（它会变成一条 INTERNAL_ERROR，或把 server 弄死）",
          not boom, f"抛了：{boom}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 3 节 · 工具语义：**所有失败都是正常返回**，一个异常都不许漏")
    print("=" * 74)

    # -- 成功路径 -------------------------------------------------------------
    res, err = attempt(AF.call_tool, "audio_probe", {"audio_uri": WAV_URI})
    probe = payload_of(res)
    check("`audio_probe` 对一份 WAV：成功、`isError=false`、且附上可机读 JSON",
          err is None and isinstance(res, dict) and res.get("isError") is False
          and isinstance(probe, dict),
          f"实得：{err!r}")
    check("……`pcm_available=true`、采样率 44100、字节数与 sha256 都能复算",
          isinstance(probe, dict)
          and probe.get("pcm_available") is True
          and (probe.get("container") or {}).get("sample_rate") == S.DEFAULT_SR
          and probe.get("byte_size") == len(WAV_SMALL)
          and probe.get("sha256") == hashlib.sha256(WAV_SMALL).hexdigest(),
          f"实得：rate={(probe or {}).get('container', {}).get('sample_rate')} "
          f"size={(probe or {}).get('byte_size')}")

    res, err = attempt(AF.call_tool, "audio_probe", {"audio_uri": MP3_URI})
    probe_mp3 = payload_of(res)
    check("`audio_probe` 对 MP3：**能读容器、但 `pcm_available=false`**，"
          "且 reason 非空（本机没有解码器 ⇒ 波形域整项跑不了，这一点必须说出来）",
          isinstance(probe_mp3, dict) and probe_mp3.get("pcm_available") is False
          and bool(str(probe_mp3.get("pcm_note") or "").strip()),
          f"实得：{str((probe_mp3 or {}).get('pcm_note'))[:70]}")

    # -- 四条失败路径（**一条条列出来数**，不能只测其中一条）------------------
    # ⚠️ 为什么要带"族"这一列：本项目曾因为"只测了策略拒绝那条路"而让另外四种
    #    畸形输入**全部穿透**（见 `verify_audio.py` 第 7 节的记录）。
    #    族是独立的判定入口（`fix_for` 走分类码 / `_fix_for_input` 走关键词 /
    #    `_BadArgs` 走参数校验），**一个族通了不代表另一个族通了**。
    fail_cases: list[tuple[str, str, str, dict[str, Any] | str, str]] = [
        # (族, 标签, 工具, 参数, 正文里必须出现的关键词)
        ("策略", "出网地址 https", "audio_forensics_analyze",
         {"audio_uri": "https://example.com/a.wav"}, "不支持的音频引用"),
        ("策略", "本机路径 file://", "audio_forensics_analyze",
         {"audio_uri": "file:///etc/passwd"}, "不支持的音频引用"),
        ("策略", "裸路径", "audio_forensics_analyze",
         {"audio_uri": "C:/secret/audio.wav"}, "不支持的音频引用"),
        ("取件解码", "data: 载荷不是 base64", "audio_forensics_analyze",
         {"audio_uri": "data:audio/wav,raw"}, "base64 编码"),
        ("取件解码", "data: 缺少逗号", "audio_forensics_analyze",
         {"audio_uri": "data:audio/wav;base64"}, "残缺"),
        ("取件解码", "声明的类型不是音频", "audio_forensics_analyze",
         {"audio_uri": "data:text/plain;base64,QUJD"}, "不是音频"),
        ("取件解码", "base64 载荷损坏", "audio_forensics_analyze",
         {"audio_uri": "data:audio/wav;base64,!!!!"}, "base64 无法解码"),
        ("取件解码", "未登记的 inbox 句柄", "audio_forensics_analyze",
         {"audio_uri": "inbox:" + "0" * 64}, "收件箱引用无效"),
        ("字节", "字节根本不是音频", "audio_forensics_analyze",
         {"audio_uri": NOT_AUDIO_URI}, "不是可识别的音频容器"),
        ("参数", "参数是空串", "audio_forensics_analyze", {"audio_uri": "   "}, "`audio_uri`"),
        ("参数", "参数不是字符串", "audio_forensics_analyze", {"audio_uri": 12345}, "`audio_uri`"),
        ("参数", "`audio_uri` 整个缺失", "audio_forensics_analyze", {}, "`audio_uri`"),
        ("参数", "`arguments` 不是对象", "audio_forensics_analyze", "字符串", "JSON 对象"),
        ("参数", "`base_hz` 不是数字", "audio_enf_track",
         {"audio_uri": WAV_URI, "base_hz": "50"}, "`base_hz`"),
        ("参数", "`window_seconds` 越界", "audio_enf_track",
         {"audio_uri": WAV_URI, "window_seconds": 99.0}, "`window_seconds`"),
    ]
    bad_fail: list[str] = []
    for family, label, tool, args, needle in fail_cases:
        res, err = attempt(AF.call_tool, tool, args)
        body = text_of(res)
        ok = (
            err is None and isinstance(res, dict) and res.get("isError") is True
            and needle in body and bool(body.strip())
        )
        if not ok:
            bad_fail.append(f"[{family}]{label}(异常={err!r}, 命中={needle in body})")
    check("十五种失败输入：**每一种都返回结构化结果**（`isError=true` + 正文），"
          "且正文含该给调用方的那句话 —— 一条异常都不许穿透",
          not bad_fail, f"不符：{bad_fail}")
    families = {c[0] for c in fail_cases}
    check("……并且**四个族一个不漏**（策略拒绝 / 取件解码 / 参数非法 / 字节不可识别）"
          "—— 每族是独立的判定入口，一个族通过了不代表另一个族通过了",
          families == {"策略", "取件解码", "参数", "字节"},
          f"实得：{sorted(families)}")

    # -- 建议不许串台 ---------------------------------------------------------
    body_b64 = text_of(attempt(AF.call_tool, "audio_forensics_analyze",
                              {"audio_uri": "data:audio/wav;base64,!!!!"})[0])
    check("坏 base64 的建议说的是 base64，**不是**「容器结构本身有问题」"
          "（两族的建议表不许串台：把 5 秒能改对的参数错说成文件的问题）",
          "base64" in body_b64 and "容器结构" not in body_b64,
          f"正文：{body_b64.splitlines()[0][:90] if body_b64 else '-'}")

    # ⚠️ 建议文案**只有一处定义**（`pipeline.fix_for`）。工具层若自己另写一份，
    #    两条路就会各自演化 —— 所以这里不是"看看像不像"，而是逐字比对。
    body_net = text_of(attempt(AF.call_tool, "audio_forensics_analyze",
                               {"audio_uri": "https://example.com/a.wav"})[0])
    check("出网被拒时给出的建议**逐字等于** `fix_for(\"unsupported_scheme\")` "
          "（补救建议只有一处定义，工具层不许另写一份）",
          fix_for("unsupported_scheme") in body_net,
          f"正文：{body_net[:120]}")

    # -- ENF 工具的成功路径 ---------------------------------------------------
    res, err = attempt(AF.call_tool, "audio_enf_track", {"audio_uri": WAV_URI})
    track = payload_of(res)
    # ⚠️ 白噪里检不出市电痕迹是**常态**（真实录音本来就这样），所以这里只要求
    #    `detected` / `points` / `reason` / `note` 四个字段齐 —— **不要求 `base_hz`**：
    #    检不出时它根本没有意义，强行要求会让"如实不报"被当成失败。
    check("`audio_enf_track` 成功：返回轨迹 dict（`detected`/`points`/`reason`/`note` 齐备）",
          err is None and isinstance(track, dict)
          and {"detected", "points", "reason", "note"} <= set(track),
          f"键={sorted(track or {})}")
    check("……白噪上**检不出就如实说检不出**（不硬凑一个基频出来）",
          isinstance(track, dict) and (
              track.get("detected") is True
              or (track.get("detected") is False and not track.get("base_hz"))
          ),
          f"detected={track.get('detected') if isinstance(track, dict) else err!r}"
          f" base_hz={track.get('base_hz') if isinstance(track, dict) else '-'}")
    check("……轨迹自带那句「`detected=false` 时看不出任何东西」的说明"
          "（不写清的话，一串噪声峰会被读成证据）",
          isinstance(track, dict) and "看不出任何东西" in str(track.get("note", "")))
    res, err = attempt(AF.call_tool, "audio_enf_track",
                       {"audio_uri": data_uri(S.wav_bytes(S.mains_hum(6.0, jump_at=None)))})
    track_hum = payload_of(res)
    check("市电痕迹样本：`detected=true`、基频 50 Hz、点数非空",
          isinstance(track_hum, dict) and track_hum.get("detected") is True
          and track_hum.get("base_hz") == 50.0 and bool(track_hum.get("points")),
          f"实得：{ {k: v for k, v in (track_hum or {}).items() if k != 'points'} }")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 3b 节 · 「收下的参数必须校验」与「失败不许归错因」（本轮修的缺陷）")
    print("=" * 74)

    def call(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        """→ `(isError, 正文)`。调用本身抛异常时按 `isError=True`、正文为空处理。"""
        res_k, _ = attempt(AF.call_tool, tool, args)
        if not isinstance(res_k, dict):
            return True, ""
        return bool(res_k.get("isError")), text_of(res_k)

    # -- `base_hz` 只允许 50 / 60 ---------------------------------------------
    passed_bad = [
        repr(v) for v in (123, 7.5, True, "50", 100)
        if not call("audio_enf_track", {"audio_uri": WAV_URI, "base_hz": v})[0]
    ]
    check("`base_hz` 只能是 50 / 60：其余取值（123 / 7.5 / True / \"50\" / 100）**必须拒绝**"
          "—— schema 里的 `enum` 不是所有客户端都会执行，而收下 123 的后果是"
          "照它算一条**没有科学含义**的谐波梳（123/246/369/492 Hz），"
          "再如实报「未检出」，调用方读到的却是「没有市电痕迹」",
          not passed_bad, f"被放过的：{passed_bad}")

    not_passed_ok = [
        repr(v) for v in (50, 60, 50.0)
        if call("audio_enf_track", {"audio_uri": WAV_URI, "base_hz": v})[0]
    ]
    check("……正向对照：50 / 60（含 50.0）必须照常放行（别把一个合法参数连根砍掉）",
          not not_passed_ok, f"被误拒的：{not_passed_ok}")
    check("……且 `_ENF_BASES` 必须与检测核心的 `ENF_BASE_FREQS` **一致**"
          "（这是「同一件事写在两处」—— 工具层判据放宽/收紧而核心没跟着动，"
          "或反过来，都会让两边对同一个请求给出不同答复）",
          tuple(AF._ENF_BASES) == tuple(ENF_BASE_FREQS),
          f"工具层 {tuple(AF._ENF_BASES)} / 核心 {tuple(ENF_BASE_FREQS)}")

    err_w, body_w = call("audio_enf_track", {"audio_uri": WAV_URI, "window_seconds": True})
    check("`window_seconds` 是布尔值 ⇒ 必须拒绝（`isinstance(True, int)` 为真，"
          "不显式挡掉就静默变成 1.0 秒 —— 收下了，却做了另一件事）",
          err_w and "`window_seconds`" in body_w, f"正文：{body_w[:70]}")

    # -- 没解出波形：不许一律归因成「没有解码器」 -----------------------------
    err_nofmt, body_nofmt = call("audio_enf_track",
                                 {"audio_uri": data_uri(S.wav_without_fmt())})
    check("WAV 头坏了（没有 `fmt` 块）⇒ 原因必须指向**这个文件**，"
          "**不许**沿用 `enf_track` 那句「压缩 / 带封装的格式、本进程没有解码器」"
          "（照着错误的归因去改，只会白折腾一遍转格式；而真正要改的是输入）",
          err_nofmt and "fmt" in body_nofmt and "没有解码器" not in body_nofmt,
          f"正文：{body_nofmt.splitlines()[0][:90] if body_nofmt else '-'}")

    err_mp3, body_mp3 = call("audio_enf_track", {"audio_uri": MP3_URI})
    check("……而**真的**是压缩格式时要如实说「没有解码器」（别为了改归因把它反过来也抹掉）",
          err_mp3 and "解码器" in body_mp3,
          f"正文：{body_mp3.splitlines()[0][:90]}")

    err_na, body_na = call("audio_enf_track", {"audio_uri": NOT_AUDIO_URI})
    check("非音频字节 ⇒ 失败，且原因说「不是可识别的音频容器」"
          "（与 `audio_forensics_analyze` 对非音频的处置同源：工具的真前提是输入是音频）",
          err_na and "不是可识别的音频容器" in body_na,
          f"正文：{body_na.splitlines()[0][:90] if body_na else '-'}")

    # ⚠️ 下面这一组是 2026-10-05 补上的**判据漏洞**（B2）。
    #    原先这里只断言「正文里有那句话」与「出路指向调用方」—— 而**原因**当时是
    #    **手抄的两份**（检测层 `analyze()` 抛异常时拼一次，MCP 的 `_handle_enf_track`
    #    再抄一次）。两句话一模一样纯属运气：改一处、另一处不动，**整条判据链照样全绿**。
    audio_core = AF._audio_module()
    check("「谁算音频」的判据与「不是音频」那句话，**两条都只有一个来源**"
          "（MCP 不许再写一份 `startswith(\"audio/\")`，也不许再抄一句话）",
          audio_core.non_audio_clause is audio_forensics.non_audio_clause
          and audio_core.is_supported_audio is audio_forensics.is_supported_audio,
          f"MCP 用的是 {getattr(audio_core.non_audio_clause, '__module__', '?')}"
          f".{getattr(audio_core.non_audio_clause, '__name__', '?')}")
    clause = audio_core.non_audio_clause("application/octet-stream")
    _, body_ana_na = call("audio_forensics_analyze", {"audio_uri": NOT_AUDIO_URI})
    check("……同一条子句必须**同时**出现在**两条出口**的正文里"
          "（一条走检测层的 `analyze()`、一条走工具层自己的 `_handle_enf_track`；"
          "比「出路相同」更强：原因也不许各写一份）",
          clause in body_ana_na and clause in body_na,
          f"子句={clause!r}；analyze 含={clause in body_ana_na}；enf_track 含={clause in body_na}")

    # ⚠️ 上面两条钉的是「同一个函数对象被导出」，**钉不住手抄一份、而且抄得一字不差**：
    #    那种形态下函数对象确实被导出了、正文里那句话也确实出现了 ⇒ 两条全绿，
    #    而缺陷（同一句话有两个来源）仍在，下一次改一个字就分家。
    #    所以这里做**行为化**的单一来源证明：把检测层那句话临时换成哨兵，
    #    两条出口的正文**都必须跟着变**。手抄的实现不会跟着变 —— 哪怕抄得一模一样。
    # ⚠️ 能这么测的前提是「不许在 import 期绑定函数引用」（`_audio_module()` 每次
    #    调用时才去读 `_forensics.non_audio_clause`）。绑定在 import 期的写法会让
    #    这条判据**静默变成永远通过**，那正是本项目踩过的那种假绿。
    _orig_clause = audio_forensics.non_audio_clause
    try:
        audio_forensics.non_audio_clause = (  # type: ignore[assignment]
            lambda mt: f"【哨兵-单一来源】{mt}")
        _, sentinel_ana = call("audio_forensics_analyze", {"audio_uri": NOT_AUDIO_URI})
        _, sentinel_enf = call("audio_enf_track", {"audio_uri": NOT_AUDIO_URI})
    finally:
        audio_forensics.non_audio_clause = _orig_clause  # type: ignore[assignment]
    check("**行为化**的单一来源证明①：把检测层那句话临时换成哨兵 ⇒ 两条出口"
          "（`analyze` 走检测层、`enf_track` 走工具层自己的判定）正文**都必须跟着变**",
          "哨兵-单一来源" in sentinel_ana and "哨兵-单一来源" in sentinel_enf,
          f"analyze 含哨兵={('哨兵-单一来源' in sentinel_ana)}；"
          f"enf_track 含哨兵={('哨兵-单一来源' in sentinel_enf)}")
    check("……哨兵撤掉后正文立刻恢复（证明上一行不是被别的原因凑出来的）",
          "哨兵-单一来源" not in call("audio_enf_track", {"audio_uri": NOT_AUDIO_URI})[1])

    # ⚠️ 哨兵② 钉的是**另一处**：`pipeline` 那条**路由关键词**。
    #    它原先也是手写的字面量。与"那句话"不同，它坏了**不会报错** ——
    #    后果是「你给的不是音频」被答成兜底那句「请提交完整未截断的原始音频字节」，
    #    调用方照着错误的归因去改，而正文看起来完全正常。
    #    前缀是**一个值、两个使用者**（`non_audio_clause()` 拼句子 +
    #    `pipeline._non_audio_keyword()` 做路由）⇒ 换掉它，**原因与出路都必须跟着变**。
    _orig_prefix = audio_forensics.NON_AUDIO_CLAUSE_PREFIX
    stale_clause = clause  # 打哨兵**之前**取到的那句话，下面当"旧文本"用
    try:
        audio_forensics.NON_AUDIO_CLAUSE_PREFIX = "【哨兵-关键词】"
        _, sentinel_kw = call("audio_enf_track", {"audio_uri": NOT_AUDIO_URI})
        stale_fix = str(audio_core.fix_for_input(stale_clause))
    finally:
        audio_forensics.NON_AUDIO_CLAUSE_PREFIX = _orig_prefix  # type: ignore[assignment]
    check("**行为化**的单一来源证明②：把**共用前缀**换成哨兵 ⇒ 原因与**出路**"
          "必须**同时**跟着变（一个值、两个使用者：一个拼句子、一个做路由）",
          "哨兵-关键词" in sentinel_kw and "音频文件本身" in sentinel_kw,
          f"正文={sentinel_kw.splitlines()[0][:90] if sentinel_kw else '-'}")
    check("　└ 而**旧那句话**这时候**不再**命中路由 ⇒ 证明关键词真的换了源"
          "（手写的字面量做不到这一点：它会对旧文本一直匹配下去）",
          "音频文件本身" not in stale_fix and "完整、未截断的原始音频字节" in stale_fix,
          f"旧文本现在的出路：{stale_fix[:70]}")

    err_short, body_short = call(
        "audio_enf_track",
        {"audio_uri": data_uri(S.wav_bytes(S.white_noise(1.2, seed=8)))},
    )
    check("对照：**波形正常但检不出市电痕迹** ⇒ `isError=false`"
          "（ENF 检不出是常态，调用方没有任何东西要改；把它报成失败会让"
          "「这个工具在真实录音上基本用不了」）",
          not err_short, f"正文：{body_short.splitlines()[0][:70] if body_short else '-'}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 4 节 · 出口同源：MCP 的 JSON ≡ ACPs 的 JSON（**本节是核心**）")
    print("=" * 74)
    print("  ⚠️ 工具层只做翻译、不重做判定 —— 判据只有 `legal_acps.audio` 里那一份。")

    corpora: list[tuple[str, str]] = [
        ("干净白噪", data_uri(WAV_SPEECH)),
        ("MP3（解不出波形）", MP3_URI),
        ("数字静音拼接", data_uri(S.wav_bytes(S.with_digital_silence(S.speech_like(3.0))))),
        ("两段底噪拼接", data_uri(S.wav_bytes(S.spliced_noise_floors()))),
        ("市电相位跳变", data_uri(S.wav_bytes(S.mains_hum(6.0, jump_at=3.0)))),
    ]
    diff_bad: list[str] = []
    for label, uri in corpora:
        mcp_res, mcp_err = attempt(AF.call_tool, "audio_forensics_analyze",
                                   {"audio_uri": uri})
        acps_out, acps_err = attempt(outcome, uri)
        if mcp_err is not None or acps_err is not None:
            diff_bad.append(f"{label}: 异常 mcp={mcp_err!r} acps={acps_err!r}")
            continue
        if getattr(acps_out, "status", None) != "ok":
            diff_bad.append(f"{label}: ACPs 侧没出报告（{getattr(acps_out, 'status', None)}）")
            continue
        mcp_payload = payload_of(mcp_res)
        acps_payload = json.loads(acps_out.result.report.model_dump_json())
        if mcp_payload != acps_payload:
            diff_bad.append(f"{label}: JSON 不一致")
            continue
        # 给人看的那一行摘要也必须同源（它是整份报告里最容易被原样转述的一句）
        if acps_out.result.summary_for_leader not in text_of(mcp_res):
            diff_bad.append(f"{label}: 摘要没出现在 MCP 正文里")
    check("五个语料：MCP 的 JSON 与 ACPs 的 JSON **逐字相同**，且摘要一字不差地出现在正文里",
          not diff_bad, f"不符：{diff_bad}")

    # -- 失败路径也必须同源（只测成功路径的断言 ≈ 什么都没测）----------------
    refuse_diff: list[str] = []
    for label, uri in [("出网地址", "https://example.com/a.wav"),
                       ("未登记句柄", "inbox:" + "f" * 64),
                       ("非音频字节", NOT_AUDIO_URI),
                       ("坏 base64", "data:audio/wav;base64,!!!!")]:
        mcp_res, _ = attempt(AF.call_tool, "audio_forensics_analyze", {"audio_uri": uri})
        acps_out, _ = attempt(outcome, uri)
        body = text_of(mcp_res)
        ok = (
            getattr(acps_out, "status", None) == "refused"
            and isinstance(mcp_res, dict) and mcp_res.get("isError") is True
            # ⚠️ 逐字包含，不是"差不多"：调用方拿到的原因必须是**原文**，
            #    不允许工具层替它改写（改写了就没人能判断责任在哪一侧）。
            and str(acps_out.refusal_reason) in body
            and str(acps_out.how_to_fix) in body
        )
        if not ok:
            refuse_diff.append(f"{label}(status={getattr(acps_out, 'status', None)})")
    check("四条拒绝路径：MCP 的 `isError` ⇔ ACPs 的 `refused`，"
          "且 `refusal_reason` / `how_to_fix` **原文照抄**",
          not refuse_diff, f"不符：{refuse_diff}")

    # -- 溯源字段与「收下的参数必须看得见」 -----------------------------------
    res, _ = attempt(AF.call_tool, "audio_forensics_analyze",
                     {"audio_uri": WAV_URI, "context_note": "据称是通话录音",
                      "declared_mime": "audio/mpeg"})
    probe_ctx = payload_of(res)
    body_ctx = text_of(res)
    # ⚠️ 这一条是写本脚本时**新发现的缺陷**：`context_note` 被收下、也一路传到了
    #    `AudioEvidenceResult.context_note`，但结果只 dump 了 `report`（report 里没这个
    #    字段）⇒ 调用方读到了 schema 里的承诺「仅原样回显」，却看不到任何回显。
    #    项目判据：**收下却看不见，与收下却不用一样，都是向调用方撒谎。**
    check("`context_note` **原样回显在结果正文里**（schema 承诺了「仅原样回显」，"
          "那就必须看得见 —— 收下了却看不见等于撒谎）",
          "据称是通话录音" in body_ctx, f"正文前 200 字：{body_ctx[:200]!r}")
    check("……`declared_mime` 真的参与了判定：声明 audio/mpeg 而字节是 WAV ⇒ "
          "「容器一致性」这一项被点名，而不是被静默忽略",
          isinstance(probe_ctx, dict)
          and any(f.get("check") == "container" and "字节本身" in str(f.get("detail", ""))
                  and "audio/mpeg" in str(f.get("detail", ""))
                  for f in probe_ctx.get("findings", [])),
          f"容器项：{[f.get('detail') for f in (probe_ctx or {}).get('findings', [])
                     if f.get('check') == 'container']}")
    # -- B4（音频侧同步）：`audio_probe` 的 `declared_mime` **只回显、不参与判定** ---
    # ⚠️ 上一条钉的是 **analyze 会比对**，这一条钉的是 **probe 不比对**。两侧都必须钉：
    #    只留上一条 ⇒ 把探针的 schema 描述改回「用于与真实容器比对」没人会发现；
    #    只留下一条 ⇒ 把比对从两条路上一并删掉也照样绿。
    # ⚠️ 主判据是**行为**，不是 grep 那句描述（描述可以改回去而代码一字不动）。
    a_wrong, a_err = attempt(AF.call_tool, "audio_probe",
                             {"audio_uri": WAV_URI, "declared_mime": "audio/mpeg"})
    a_wp = payload_of(a_wrong) or {}
    a_props = next(t for t in AF.TOOLS if t["name"] == "audio_probe")["inputSchema"]["properties"]
    check("音频探针喂一个**错的** `declared_mime`（字节是 WAV、声明 audio/mpeg）⇒ "
          "照样成功，`media_type` 仍按**魔术字节**给出 `audio/wav`，正文里无对比措辞",
          a_err is None and isinstance(a_wrong, dict) and a_wrong.get("isError") is False
          and a_wp.get("media_type") == "audio/wav"
          and "不一致" not in text_of(a_wrong),
          f"isError={a_wrong.get('isError') if isinstance(a_wrong, dict) else a_err!r} "
          f"media_type={a_wp.get('media_type')!r}")
    check("……`declared_mime` 仍**原样回显**（不比对 ≠ 吞掉），"
          "且 schema 描述如实写「仅原样回显，不参与任何判定」",
          a_wp.get("declared_mime") == "audio/mpeg"
          and "不参与任何判定" in str(
              (a_props.get("declared_mime") or {}).get("description", "")),
          f"实得 {a_wp.get('declared_mime')!r}")
    check("`source_uri` 与 `fetched_from` 也在正文里（复核「字节是从哪个引用取来的」）",
          WAV_URI[:40] in body_ctx and "inline_base64" in body_ctx,
          "")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 5 节 · 真子进程端到端（stdio 传输）")
    print("=" * 74)

    lines: list[tuple[str, Any]] = [
        ("#1 initialize（客户端给 2024-11-05）",
         {"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {}}}),
        ("#2 通知（必须**没有**应答）",
         {"jsonrpc": "2.0", "method": "notifications/initialized"}),
        ("#3 tools/list", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ("#4 ping（id 是 null，必须应答）",
         {"jsonrpc": "2.0", "id": None, "method": "ping"}),
        ("#5 tools/call 成功", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "audio_probe",
                                          "arguments": {"audio_uri": WAV_URI}}}),
        ("#6 tools/call 出网被拒", {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                   "params": {"name": "audio_forensics_analyze",
                                              "arguments": {"audio_uri": "http://x/a.wav"}}}),
        ("#7 tools/call 未知工具", {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                   "params": {"name": "nope", "arguments": {}}}),
        ("#8 tools/call 缺 name", {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                                   "params": {"arguments": {}}}),
        ("#9 未知方法（有 id）", {"jsonrpc": "2.0", "id": 7, "method": "resources/list"}),
        ("#10 未知通知（必须**没有**应答）",
         {"jsonrpc": "2.0", "method": "notifications/cancelled"}),
    ]
    payload = "".join(
        json.dumps(m, ensure_ascii=False) + "\n" for _, m in lines
    ) + "这不是 JSON\n"   # #11 解析失败 → -32700

    proc, proc_err = attempt(subprocess.run,
                             [sys.executable, "-m", "legal_acps.mcp.server"],
                             cwd=str(ROOT), input=payload.encode("utf-8"),
                             capture_output=True, timeout=180)
    check("`python -m legal_acps.mcp.server` 能作为子进程跑完（stdin 读到 EOF 就正常退出）",
          proc_err is None and proc is not None and proc.returncode == 0,
          f"异常={proc_err!r} rc={getattr(proc, 'returncode', None)}")

    stdout_raw = (getattr(proc, "stdout", b"") or b"").decode("utf-8", errors="replace")
    out_lines = [x for x in stdout_raw.split("\n") if x.strip()]
    parsed: list[dict[str, Any]] = []
    dirty: list[str] = []
    for raw in out_lines:
        try:
            item = json.loads(raw)
        except ValueError:
            dirty.append(raw[:60])
            continue
        if not isinstance(item, dict) or item.get("jsonrpc") != "2.0":
            dirty.append(raw[:60])
            continue
        parsed.append(item)

    check("**stdout 只有协议消息**：11 条报文逐行都是合法 JSON-RPC（写脏一个字节就是客户端解析失败、"
          "整个 server 变砖，而现场看不出原因）",
          not dirty, f"脏行：{dirty}")

    # ⚠️ 11 条报文 → **9 条应答**：两条通知（#2/#10）一条都不回。
    check("11 条报文 → **9 条应答**：两条通知一条都不回（#2 是客户端每次都发的第一条，"
          "回了它 = 「这个服务器用不了」）",
          len(parsed) == 9, f"实得 {len(parsed)} 条：{[p.get('id') for p in parsed]}")

    # ⚠️ 用**顺序对齐**而不是按 id 建字典：这里有两个应答的 id 都是 `null`
    #    （`ping` 那条、以及最后那条解析失败的），按 id 建字典会让后一条
    #    覆盖前一条 —— 于是 `ping` 的断言实际测到的是解析错误那条，
    #    表现为"ping 的应答不对劲"。这是本脚本自己踩过的坑。
    expected_ids = [1, 2, None, 3, 4, 5, 6, 7, None]
    got_ids = [p.get("id") for p in parsed]
    check("应答的 id **逐个对齐**请求（顺序与归属都不许错位）",
          got_ids == expected_ids, f"实得：{got_ids} / 期望：{expected_ids}")

    if len(parsed) != 9:  # 结构不符时后面按下标取会 IndexError，先兜住
        return 1
    (r_init, r_list, r_ping, r_probe, r_refuse,
     r_unknown_tool, r_no_name, r_unknown_method, r_parse) = parsed

    init_s = r_init.get("result") or {}
    check("端到端：`initialize` 协商回客户端那一版 2024-11-05",
          init_s.get("protocolVersion") == "2024-11-05",
          f"实得：{init_s.get('protocolVersion')!r}")
    tools_s = r_list.get("result") or {}
    check("端到端：`tools/list` 返回 3 个工具",
          len(tools_s.get("tools") or []) == 3, f"实得 {len(tools_s.get('tools') or [])}")
    check("端到端：`ping`（`id=null`）被当作**请求**处理并回了应答",
          r_ping.get("id", "缺") is None and "result" in r_ping,
          f"实得：{json.dumps(r_ping, ensure_ascii=False)[:80]}")
    probe_s = r_probe.get("result") or {}
    check("端到端：成功调用 `isError=false` 且正文里有一份可机读 JSON",
          probe_s.get("isError") is False and payload_of(probe_s) is not None)
    refuse_s = r_refuse.get("result") or {}
    check("端到端：出网地址被拒 —— 是 `isError=true` 的正常返回，不是 JSON-RPC 错误",
          refuse_s.get("isError") is True and "error" not in r_refuse,
          f"正文：{text_of(refuse_s)[:70]}")
    check("端到端：未知工具名同样是 `isError=true` 的正常返回（不是 -32601）",
          (r_unknown_tool.get("result") or {}).get("isError") is True
          and "error" not in r_unknown_tool,
          f"正文：{text_of(r_unknown_tool.get('result'))[:70]}")
    check("端到端：`tools/call` 缺 name 走 -32602（**协议层**的参数错误）",
          ((r_no_name.get("error") or {}).get("code")) == INVALID_PARAMS)
    check("端到端：未知方法走 -32601",
          ((r_unknown_method.get("error") or {}).get("code")) == METHOD_NOT_FOUND)
    check("端到端：不可解析的一行回 -32700（**不是**把 server 弄死），"
          "且 id 只能是 null（那一条里根本没有 id 可回）",
          ((r_parse.get("error") or {}).get("code")) == PARSE_ERROR
          and r_parse.get("id", "缺") is None,
          f"实得：{json.dumps(r_parse, ensure_ascii=False)[:100]}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 6 节 · 版本协商（纯函数）与常量")
    print("=" * 74)

    check("协商：客户端给了我们支持的版本 → **回它自己那一版**（不是最新版）",
          negotiate_protocol("2024-11-05") == "2024-11-05"
          and negotiate_protocol("2025-03-26") == "2025-03-26")
    check("协商：不认识的版本 / 没给 / 不是字符串 → 回我们最新的一版",
          negotiate_protocol("1999-01-01") == DEFAULT_PROTOCOL_VERSION
          and negotiate_protocol(None) == DEFAULT_PROTOCOL_VERSION
          and negotiate_protocol(2024) == DEFAULT_PROTOCOL_VERSION)
    check("`SUPPORTED_PROTOCOL_VERSIONS` 按新→旧排列，且 `DEFAULT` 就是最新的那个",
          tuple(sorted(SUPPORTED_PROTOCOL_VERSIONS, reverse=True))
          == tuple(SUPPORTED_PROTOCOL_VERSIONS)
          and DEFAULT_PROTOCOL_VERSION == SUPPORTED_PROTOCOL_VERSIONS[0],
          f"实得：{SUPPORTED_PROTOCOL_VERSIONS}")
    check("服务标识里含 `audio`（同机上跑多个 MCP server 时，日志里要分得清是谁）",
          "audio" in SERVER_NAME and bool(SERVER_VERSION))

    # -- 传输层的两个纯函数 ---------------------------------------------------
    import io
    check("`read_message` 忽略空行（不是报错）",
          read_message(io.BytesIO(b"\n\n{\"a\":1}\n"))[0] == {"a": 1})
    check("`read_message` 对坏 JSON 返回错误文本而不是抛异常",
          read_message(io.BytesIO(b"{bad}\n"))[1] is not None)
    check("`read_message` 在 EOF 时返回 (None, None)（父进程关管道 = 该结束了）",
          read_message(io.BytesIO(b"")) == (None, None))

    # -- 传输层：**同一次内部崩溃，通知不许应答、请求必须应答** ----------------
    # ⚠️ 这一条只能在这一层测：`handle_message` 是纯函数，它**抛异常**这件事
    #    由 `serve()` 的兜底处理决定结果。而两种报文形态的义务恰好相反 ——
    #    请求必须收到一条 `-32603`（否则客户端一直等），通知**一条都不许发**
    #    （回了就是那条 `id: null` 的错误，客户端当成"应答不上的请求"）。
    #    现场唯一能看出差别的就是它，所以必须把两种形态放在同一次运行里对照。
    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("模拟工具实现崩了")

    original_call_tool = AF.call_tool
    AF.call_tool = _boom  # type: ignore[assignment]
    try:
        payload = (
            b'{"jsonrpc":"2.0","method":"tools/call",'
            b'"params":{"name":"audio_probe","arguments":{}}}\n'
            b'{"jsonrpc":"2.0","id":9,"method":"tools/call",'
            b'"params":{"name":"audio_probe","arguments":{}}}\n'
        )
        out_buf = io.BytesIO()
        run_err: Exception | None = None
        try:
            serve(io.BytesIO(payload), out_buf)
        except Exception as exc:  # noqa: BLE001 - 传输层不许把异常漏出来
            run_err = exc
        replies = [x for x in out_buf.getvalue().decode("utf-8").split("\n") if x.strip()]
    finally:
        AF.call_tool = original_call_tool  # type: ignore[assignment]

    check("通知（无 `id`）里工具实现崩了 ⇒ **一条应答都不许有**"
          "（回了 `id: null` 的错误，客户端会把它当成「应答不上的请求」；"
          "而成功与失败都不该有应答，才叫「没有 id 就是通知」）",
          run_err is None and len(replies) == 1, f"实得 {len(replies)} 条：{replies}")
    reply_obj: dict[str, Any] = {}
    if len(replies) == 1:
        try:
            reply_obj = json.loads(replies[0])
        except ValueError:
            reply_obj = {}
    check("……同一次崩溃、同一条报文，只因为它**带 id**，就必须回一条 `-32603` 并带回该 id",
          ((reply_obj.get("error") or {}).get("code")) == INTERNAL_ERROR
          and reply_obj.get("id") == 9,
          f"实得：{replies[0][:110] if replies else '-'}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 7 节 · 按需导入面（不许「按导入路径碰运气」）")
    print("=" * 74)

    core, core_err = attempt(AF._audio_module)
    need = {"decode_audio", "load", "AudioFetchRefused", "enf_track", "analyze", "outcome",
            "fix_for", "fix_for_input", "sha256_of",
            "is_supported_audio", "non_audio_clause"}
    # ⚠️ 判据是「**每个符号都真的可用**」，不是「每个键都在」。
    #    后者会被 `sha256_of=None` 这种写法骗过去 —— 而"导出面少一个符号、
    #    结果静默变成 None"正是这条断言要防的事（它要到调用那一刻才炸，
    #    而调用它的地方是 `except Exception` 兜底的边界层 ⇒ 只变成一句
    #    「内部错误」，没有指向真正的原因）。
    # ⚠️ 符号**个数**由 `need` 现场派生、写进标签里，不许写成字面数字：
    #    2026-10-05 之前这里硬写着「八个符号」，加了两个符号之后它就**成了一句假话**
    #    （而假话不会报失败，只会让下一个人以为导出面只有八个）。
    missing = (
        sorted(k for k in need if not callable(getattr(core, k, None)))
        if core is not None else sorted(need)
    )
    check(f"`_audio_module()` 显式给出**列明依赖面**的命名空间"
          f"（{len(need)} 个符号都**真的可用**，不只是「键在」）",
          core_err is None and not missing,
          f"缺/不可用：{missing}；异常={core_err!r}")
    check("……其中 `enf_track` 必须真的可取（它的前身只存在于子模块里 ⇒ "
          "`legal_acps.audio.enf_track` 取不到，MCP 侧靠「别人先导入过」才侥幸能用）",
          core is not None and callable(getattr(core, "enf_track", None)))
    check("……而「谁算音频」那两个符号的**来处**必须是检测层"
          "（不是 MCP 自己定义一份、也不是从别处顺手拿的）",
          all(getattr(getattr(core, k, None), "__module__", "") == "legal_acps.audio.forensics"
              for k in ("is_supported_audio", "non_audio_clause")),
          "、".join(f"{k}={getattr(getattr(core, k, None), '__module__', '?')}"
                    for k in ("is_supported_audio", "non_audio_clause")))

    # ⚠️ 这一条**必须是行为断言**，不能靠翻模块 `__dict__` 找 "numpy" 字样 ——
    #    那种写法既可能漏（子模块间接引入）也可能误报，而且它证明不了
    #    "没装 numpy 时 `tools/list` 还能用" 这个真正的性质。
    #    做一个干净的子进程：**只**导入 server，然后看有没有谁把音频核心拉进来。
    probe_src = (
        "import json, sys\n"
        "import legal_acps.mcp.server as srv\n"
        "print(json.dumps({\n"
        "    'audio': 'legal_acps.audio' in sys.modules,\n"
        "    'numpy': 'numpy' in sys.modules,\n"
        "    'tools': sorted(t['name'] for t in srv.audio_forensics.TOOLS),\n"
        "}, ensure_ascii=False))\n"
    )
    probe, probe_err = attempt(subprocess.run, [sys.executable, "-c", probe_src],
                               cwd=str(ROOT), capture_output=True, timeout=180)
    state: dict[str, Any] = {}
    if probe is not None and not probe_err:
        try:
            state = json.loads((probe.stdout or b"").decode("utf-8").strip().splitlines()[-1])
        except (ValueError, IndexError):
            state = {}
    check("**只导入 server 不会拉起音频核心，更不会拉起 numpy**："
          "服务器上没装 numpy 时 `tools/list` 仍然可用 —— "
          "否则客户端看到的是「连不上」，而不是一句「缺个依赖」",
          not probe_err and state.get("audio") is False and state.get("numpy") is False,
          f"实得：{state or probe_err!r}")
    check("……而即便没导入核心，`TOOLS` 的三个 schema 已经在手（schema 常驻、实现按需）",
          state.get("tools") == sorted(names), f"实得：{state.get('tools')!r}")

    print()
    print("=" * 74)
    if FAILED:
        print(f"❌ 共 {len(FAILED)} 条未达预期：")
        for name in FAILED:
            print(f"   · {name}")
        return 1
    print("✅ 全部用例符合预期：")
    print("   第 1 节 工具清单与 schema 自洽")
    print("   第 2 节 协议语义（通知不回应 / id=null 是请求 / 错误码 / 工具失败走 isError）")
    print("   第 3 节 工具语义（十五种失败输入全部结构化返回，且建议不串台）")
    print("   第 4 节 出口同源（MCP 的 JSON ≡ ACPs 的 JSON，成功与拒绝两侧都测）")
    print("   第 5 节 真子进程端到端（stdio、11 报文 9 应答、stdout 无脏字节）")
    print("   第 6 节 版本协商与常量")
    print("   第 7 节 按需导入面")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
