"""离线验证：视频鉴伪的 **MCP 入口**（`legal_acps/mcp/video_forensics.py` +
`video_server.py` + 共用的协议层）。

**不联网、不需要 API key、不碰平台、不调模型** —— 全部是确定性函数的断言。
判据只有一个：`exit 0 = 全绿`。

用法（在 witrelate 目录下）：
    ./.venv/Scripts/python.exe -m tools.verify_video_mcp

════════════════════════════════════════════════════════════════════════
这一层测的是什么（与检测层完全不同）
════════════════════════════════════════════════════════════════════════

检测层的缺陷会**改变报告**；MCP 层的缺陷**不改变报告，它改变"能不能用"**。
所以本文件的重点不是"检测对不对"，而是三件事：

  ① **出口同源**：同一个 uri，MCP 调出来的 JSON 与 ACPs 那条路
     （`legal_acps.video.pipeline.outcome`）**必须逐字相同**。
     这一条是整份文件里最有价值的：MCP 层最容易的走偏方式就是
     "看起来一样的第二份实现"，而它在外表上与正确实现没有区别。
     ⚠️ 它**只对 `video_forensics_analyze` 成立** —— 另两个工具
     （`video_probe` / `video_sample_table`）是 MCP 侧独有的"看一眼"视图，
     ACPs 链路上没有对应能力。这一条是**数出来的**，不是漏了。

  ② **协议层共用**：视频入口与音频入口走的是**同一份** `server.py`。
     判据不是"读代码看出来"，而是把 `family` 参数化**退回旧形态**时
     视频侧立刻报失败（见 `run/meta_verify_video.py`）；此处先钉住
     "默认族是音频、视频族是视频、两者工具表不相交"。

  ③ **字段口径不许含糊**：`video_probe` 报的那个数字**必须叫
     `container_frames`**。叫 `frames_available` 会让一份动画 GIF 被读成
     "帧层跑不了" —— 而它其实跑得了（GIF 的帧要光栅化，那在分析里做）。
     这正是本项目「判据的说法与出口文案不对应」的老毛病（图像链踩过一次、
     音频链踩过两次）。

节次安排：

  第 1 节  工具清单：三个工具、schema 自洽、`TOOLS` 与处理器一一对应
  第 2 节  协议语义：默认族未变 / 视频族分派正确 / 通知与 id=null 的判据
  第 3 节  **出口同源**：MCP 的 JSON ≡ ACPs 的 JSON（成功与拒绝两侧）
  第 4 节  工具语义：失败一律结构化返回，且**两族建议不串台**
  第 5 节  字段口径：`container_frames` / 声明值措辞 / 帧层后果
  第 6 节  真子进程端到端（stdio、stdout 无脏字节、`-h` 走 stderr）
  第 7 节  按需导入面：只导入入口不许拉起检测核心
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from legal_acps.mcp import audio_forensics as AF
from legal_acps.mcp import server as S
from legal_acps.mcp import video_forensics as VF
from legal_acps.mcp import video_server as VS
from legal_acps.video import fetch as video_fetch
from legal_acps.video import forensics as video_forensics
from legal_acps.video import pipeline as video_pipeline
from run import video_samples as VSAMP

ROOT = Path(__file__).resolve().parent.parent
FAILED: list[str] = []


def attempt(fn, *args: object, **kwargs: object) -> tuple[Any, Exception | None]:
    """把「抛出来的异常」折成**可比较的返回值**。

    ⚠️ 与音频侧同一条纪律：被测代码抛异常时，断言脚本会带着 traceback 崩掉，
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
    tail = body.split(marker, 1)[1].split("\n```", 1)[0]
    try:
        return json.loads(tail)
    except ValueError:
        return None


def data_uri(data: bytes, mime: str = "video/mp4") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


#: 走视频入口发一条 `tools/call`。
#: ⚠️ `family` 是 **keyword-only** —— 这里必须按关键字传。
#: 传位置会得到一句 `TypeError: takes 1 positional argument but 2 were given`，
#: 而它**恰好会被外层的失败兜底折成"结构化返回"**，于是表面上"没崩"、
#: 实际一个工具都没跑到。本文件第一版就踩了这个坑（15 条断言一起红）。
def vcall(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
           "params": {"name": name, "arguments": args if args is not None else {}}}
    got, err = attempt(S.handle_message, msg, family=VS.FAMILY)
    if err is not None:
        return {"content": [], "isError": True, "_raised": f"{type(err).__name__}: {err}"}
    return got["result"] if isinstance(got, dict) and "result" in got else {"content": [], "isError": True}


def vreply(msg: dict[str, Any]) -> Any:
    got, _err = attempt(S.handle_message, msg, family=VS.FAMILY)
    return got


#: 合成样本（与 `tools/verify_video.py` 共用同一个库 ⇒ 两处的"事实"必然一致）。
CLEAN = VSAMP.clean_mp4()
CLEAN_URI = data_uri(CLEAN)
NOT_VIDEO_URI = data_uri(b"this is definitely not a video container", "video/mp4")


def main() -> int:  # noqa: C901 - 逐节断言，长是它的本分
    # =======================================================================
    print("=" * 74)
    print("第 1 节 · 工具清单：三个工具、schema 自洽、与处理器一一对应")
    print("=" * 74)
    names = [str(t.get("name", "")) for t in VF.TOOLS]
    check("恰好三个工具，且名字就是约定的那三个",
          names == ["video_probe", "video_forensics_analyze", "video_sample_table"],
          f"实得 {names}")
    check("工具名一律以 `video_` 打头（免得与音频侧看起来像同一族）",
          all(n.startswith("video_") for n in names))
    check("视频工具名与音频工具名**不相交**（两族不许混进彼此的 `tools/list`）",
          not (set(names) & {str(t["name"]) for t in AF.TOOLS}),
          f"交集 {sorted(set(names) & {str(t['name']) for t in AF.TOOLS})}")

    handlers = getattr(VF, "_HANDLERS", None)
    check("`TOOLS` 与处理器表**一一对应**（多了少了都会出现「列表里有、调不到」的空工具）",
          isinstance(handlers, dict) and sorted(handlers) == sorted(names),
          f"TOOLS={sorted(names)} / 处理器={sorted(handlers or {})}")

    for tool in VF.TOOLS:
        schema = tool.get("inputSchema") or {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        check(f"`{tool['name']}` 的 schema 自洽（required 都在 properties 里、"
              "且 additionalProperties 关掉）",
              bool(props) and set(required) <= set(props)
              and schema.get("additionalProperties") is False
              and isinstance(tool.get("title"), str) and bool(tool.get("description")),
              f"required={required} props={sorted(props)}")

    uri_desc = str((VF.TOOLS[0]["inputSchema"]["properties"].get("video_uri") or {}).get("description", ""))
    check("三个工具的 `video_uri` 描述都写明「不出网、不读本机文件」与两种引用",
          all(
              "不出网" in str((t["inputSchema"]["properties"].get("video_uri") or {}).get("description", ""))
              and "inbox" in str((t["inputSchema"]["properties"].get("video_uri") or {}).get("description", ""))
              for t in VF.TOOLS
          ), f"首个描述：{uri_desc[:60]!r}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 2 节 · 协议语义：默认族未变 / 视频族分派正确 / 通知与 id=null")
    print("=" * 74)
    check("`SERVER_NAME` 里含 `video` 且**不含** `audio`（两个身份不许看起来像一个）",
          "video" in VF.SERVER_NAME and "audio" not in VF.SERVER_NAME,
          f"{VF.SERVER_NAME!r}")
    check("视频族指向的模块就是 `video_forensics`，也就是**存模块不存属性**",
          VS.FAMILY.module is VF and S.AUDIO_FAMILY.module is AF)
    check("视频族没有把音频那三个工具混进来（`family.module` 一换，工具表必须整换）",
          [t["name"] for t in VS.FAMILY.module.TOOLS] == names)

    listed = vreply({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    got_tools = ((listed or {}).get("result") or {}).get("tools")
    check("视频 `tools/list` 吐出的就是视频 `TOOLS` 本尊（逐项相等，不是另一份手抄的清单）",
          isinstance(got_tools, list) and got_tools == list(VF.TOOLS),
          f"实得 {[t.get('name') for t in (got_tools or [])]}")

    init = vreply({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    info = ((init or {}).get("result") or {}).get("serverInfo")
    check("视频 `initialize` 声明的是视频自己的 serverInfo",
          info == {"name": VF.SERVER_NAME, "version": VF.SERVER_VERSION}, f"实得 {info}")
    instr = str(((init or {}).get("result") or {}).get("instructions", ""))
    check("`instructions` 点明「帧层整项没有执行」是**常态**（不是例外）",
          "帧层整项没有执行" in instr and "常态" in instr)
    check("`instructions` 点明「无法判定」是合法答案、不许转述成「没问题」",
          "无法判定" in instr and "没问题" in instr)
    check("`instructions` 点明 `H.264 / H.265` 解不出帧这件事",
          "H.264" in instr and "H.265" in instr)

    # -- `{"id": null}` 是**请求**（规范允许），必须回应 -----------------------
    null_id = vreply({"jsonrpc": "2.0", "id": None, "method": "ping"})
    check("`{\"id\": null, \"method\": \"ping\"}` 是**请求** ⇒ 必须回一条 `id: null` 的应答"
          "（判据是「`id` 这个键在不在」，不是「是不是 None」）",
          isinstance(null_id, dict) and "result" in null_id and null_id.get("id") is None,
          f"实得 {null_id!r}")

    # -- 带 `id` 的 `notifications/*` 也是请求，必须回应 -----------------------
    notif_with_id = vreply({"jsonrpc": "2.0", "id": 5, "method": "notifications/initialized"})
    check("带 `id` 的 `notifications/*` **也是请求** ⇒ 必须回一条应答（-32601）",
          isinstance(notif_with_id, dict) and (notif_with_id.get("error") or {}).get("code") == -32601,
          f"实得 {notif_with_id!r}")

    # -- 通知一律不回应 --------------------------------------------------------
    pure_notif = attempt(S.handle_message,
                         {"jsonrpc": "2.0", "method": "notifications/initialized"},
                         family=VS.FAMILY)
    check("**没有** `id` 键的通知 ⇒ 一条应答都不许有（回了 `id: null` 的错误，"
          "客户端会把它当成「应答不上的请求」）",
          pure_notif == (None, None), f"实得 {pure_notif!r}")

    unknown = vreply({"jsonrpc": "2.0", "id": 6, "method": "tools/bogus"})
    check("未知方法 ⇒ `-32601`，且 `data.implemented` 列出我们真的实现的那四个",
          (unknown or {}).get("error", {}).get("code") == -32601
          and sorted(((unknown or {}).get("error", {}).get("data") or {}).get("implemented") or [])
          == ["initialize", "ping", "tools/call", "tools/list"],
          f"实得 {unknown!r}")

    # -- 传输层：非法 UTF-8 必须报错，**不许静默替换** -------------------------
    # ⚠️ 这条来自 2026-10-04 的体检：`read_message` 原先用 `errors="replace"`，
    #    而它的实际表现是"静默改数据"——报文里一个 0xFF 被替换成 U+FFFD 之后
    #    `json.loads` **照样解析成功**，于是工具收到一个被改过的参数
    #    （`inbox:<sha256>` 句柄、或一段案情说明），调用方根本不知道自己被动了字节。
    bad_utf8 = b'{"jsonrpc":"2.0","id":7,"method":"tools/list","params":{"x":"\xff"}}\n'
    msg, err = S.read_message(io.BytesIO(bad_utf8))
    check("报文含非法 UTF-8 ⇒ **报错**（解析不成功），且错误文本点名 UTF-8",
          msg is None and err is not None and "UTF-8" in err,
          f"实得 msg={msg!r} err={str(err)[:60]!r}")
    check("……负对照：**合法的** UTF-8（含中文正文）一条都不许被这次的严格化误伤",
          S.read_message(io.BytesIO(
              '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"x":"案情说明"}}\n'
              .encode("utf-8")))[1] is None)
    out_stream = io.BytesIO()
    S.serve(io.BytesIO(bad_utf8), out_stream, family=VS.FAMILY)
    lines = out_stream.getvalue().decode("utf-8").strip().splitlines()
    reply = json.loads(lines[0]) if lines else {}
    check("……协议层把它折成**一条** `-32700`（`id: null`）——既不崩掉，也不是一声不响",
          len(lines) == 1 and (reply.get("error") or {}).get("code") == -32700
          and reply.get("id") is None,
          f"实得 {lines!r}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 3 节 · **出口同源**：MCP 的 JSON ≡ ACPs 的 JSON")
    print("=" * 74)
    print("  ⚠️ 只对 `video_forensics_analyze` 成立 —— 另两个工具是 MCP 侧独有的"
          "「看一眼」视图，ACPs 链路上没有对应能力。这一条是**数出来的**。")
    print()

    # -- 成功侧 --------------------------------------------------------------
    mcp_ok = vcall("video_forensics_analyze",
                   {"video_uri": CLEAN_URI, "declared_mime": "video/mp4",
                    "context_note": "自检用样本"})
    acps_ok, acps_err = attempt(
        video_pipeline.outcome, CLEAN_URI,
        declared_mime="video/mp4", context_note="自检用样本")
    check("成功侧：ACPs 的 `outcome()` 也产出了报告（否则这条比对无从谈起）",
          acps_err is None and acps_ok is not None and acps_ok.status == "ok",
          f"异常={acps_err!r}")
    mcp_json = payload_of(mcp_ok)
    acps_json = (
        json.loads(acps_ok.result.report.model_dump_json())
        if (acps_ok is not None and acps_ok.result is not None) else None
    )
    check("**成功侧：MCP 的 JSON 与 ACPs 的 JSON 逐字段相等**"
          "（不是「看起来一样」，是同一份 `model_dump_json`）",
          mcp_json is not None and mcp_json == acps_json,
          f"MCP keys={sorted((mcp_json or {}).keys())[:6]}… "
          f"ACPs keys={sorted((acps_json or {}).keys())[:6]}…")
    check("……而且 `context_note` 在**正文里看得见**（收下了却看不见 = 向调用方撒谎）",
          "自检用样本" in text_of(mcp_ok))
    check("……`frames_note` 也必须出现在正文里（帧层后果不许只在 JSON 里）",
          str((mcp_json or {}).get("frames_note", ""))[:20] in text_of(mcp_ok)
          if (mcp_json or {}).get("frames_note") else False)

    # -- 拒绝侧（取件族）-----------------------------------------------------
    bad_uri = "https://example.com/x.mp4"
    mcp_bad = vcall("video_forensics_analyze", {"video_uri": bad_uri})
    acps_bad = video_pipeline.outcome(bad_uri)
    check("拒绝侧：ACPs 也拒绝了同一个引用（status=refused）",
          acps_bad.status == "refused", f"实得 {acps_bad.status!r}")
    check("**拒绝侧：原因原文逐字相同**（`refusal_reason` 是同一句话，不许各写一份）",
          acps_bad.refusal_reason is not None
          and acps_bad.refusal_reason in text_of(mcp_bad),
          f"ACPs={str(acps_bad.refusal_reason)[:60]!r}")
    check("……`how_to_fix` 也逐字相同",
          acps_bad.how_to_fix is not None and acps_bad.how_to_fix in text_of(mcp_bad),
          f"ACPs={str(acps_bad.how_to_fix)[:60]!r}")
    check("……且这一次失败**是 `isError=true` 的正常返回**，不是 JSON-RPC 错误"
          "（协议层不该出现 `error` 字段）",
          mcp_bad.get("isError") is True)

    # -- 拒绝侧（字节族）：同一个 uri 两族建议必须指向同一条出路 --------------
    acps_nb = video_pipeline.outcome(NOT_VIDEO_URI)
    mcp_nb = vcall("video_forensics_analyze", {"video_uri": NOT_VIDEO_URI})
    check("字节族：ACPs 也拒绝（并非只有 MCP 侧挑剔）",
          acps_nb.status == "refused" and "不是可识别的视频容器" in str(acps_nb.refusal_reason),
          f"实得 {str(acps_nb.refusal_reason)[:60]!r}")
    check("……字节族的 `how_to_fix` 也逐字相同 —— 它由 `pipeline` **单一处**定义，"
          "MCP 不许另写一句",
          acps_nb.how_to_fix is not None and acps_nb.how_to_fix in text_of(mcp_nb),
          f"ACPs={str(acps_nb.how_to_fix)[:60]!r}")

    # ⚠️ 下面这两条是 2026-10-04 补上的**判据漏洞**：原先只比了「出路」
    #    （`how_to_fix`），**没比「原因」**—— 而原因当时是**手抄的两份**
    #    （检测层 `analyze()` 抛异常时拼一次，MCP 的 `_non_video_result()` 再抄一次）。
    #    两句话一模一样纯属运气：改一处、另一处不动，**整条判据链照样全绿**。
    #    现在原因只有一个来源（`forensics.non_video_clause`），所以这里能直接断言
    #    "同一条子句同时出现在两条路上"，而且先断言"MCP 用的就是那个函数本身"。
    core = VF._video_module()
    check("字节族的**原因子句**由检测层**单一处**定义（MCP 不许另写一句）",
          core.non_video_clause is video_forensics.non_video_clause,
          f"MCP 用的是 {getattr(core.non_video_clause, '__module__', '?')}."
          f"{getattr(core.non_video_clause, '__name__', '?')}")
    clause = core.non_video_clause("application/octet-stream")
    check("……同一条子句必须**同时**出现在 ACPs 的 `refusal_reason` 与 MCP 的正文里"
          "（比「出路相同」更强：原因也不许各写一份）",
          acps_nb.refusal_reason is not None and clause in str(acps_nb.refusal_reason)
          and clause in text_of(mcp_nb),
          f"子句={clause!r}")

    # ⚠️ 上面两条钉的是「同一个函数对象被导出」，**钉不住手抄一份、而且抄得一字不差**：
    #    那种形态下函数对象确实被导出了、正文里那句话也确实出现了 ⇒ 两条全绿，
    #    而缺陷（同一句话有两个来源）仍在，下一次改一个字就分家。
    #    所以这里做**行为化**的单一来源证明：把检测层那句话临时换成哨兵，
    #    MCP 的出口**必须跟着变**。手抄的实现不会跟着变 —— 哪怕抄得一模一样。
    # ⚠️ 能这么测的前提是「不许在 import 期绑定函数引用」（`_video_module()` 每次
    #    调用时才去读 `_forensics.non_video_clause`）。绑定在 import 期的写法会让
    #    这条判据**静默变成永远通过**，那正是本项目踩过的那种假绿。
    _orig_clause = video_forensics.non_video_clause
    try:
        video_forensics.non_video_clause = (  # type: ignore[assignment]
            lambda mt: f"【哨兵-单一来源】{mt}")
        sentinel_ana = vcall("video_forensics_analyze", {"video_uri": NOT_VIDEO_URI})
        sentinel_probe = vcall("video_probe", {"video_uri": NOT_VIDEO_URI})
    finally:
        video_forensics.non_video_clause = _orig_clause  # type: ignore[assignment]
    check("**行为化**的单一来源证明：把检测层那句话临时换成哨兵 ⇒ MCP 的两条出口"
          "（`analyze` 走检测层、`probe` 走工具层自己的 `_non_video_result`）"
          "正文**都必须跟着变**",
          "哨兵-单一来源" in text_of(sentinel_ana)
          and "哨兵-单一来源" in text_of(sentinel_probe),
          f"analyze 含哨兵={('哨兵-单一来源' in text_of(sentinel_ana))}；"
          f"probe 含哨兵={('哨兵-单一来源' in text_of(sentinel_probe))}")
    check("……哨兵撤掉后正文立刻恢复（证明上一行不是被别的原因凑出来的）",
          "哨兵-单一来源" not in text_of(vcall("video_probe", {"video_uri": NOT_VIDEO_URI})))

    # ⚠️ 哨兵② 钉的是**另一处**（2026-10-05 补）：`pipeline` 那条**路由关键词**
    #    （「字节族失败 → 你给错了东西」那条建议靠关键词匹配路由）。
    #    它原先也是手写的字面量。与"那句话"不同，它坏了**不会报错** ——
    #    后果是「你给的不是视频」被答成兜底那句「请提交完整未截断的原始视频字节」，
    #    调用方照着错误的归因去改，而正文看起来完全正常；所有"字符串相等"的判据
    #    也照绿不误。前缀是**一个值、两个使用者**（`non_video_clause()` 拼句子 +
    #    `pipeline._non_video_keyword()` 做路由）⇒ 换掉它，原因与出路都得跟着变。
    _orig_prefix = video_forensics.NON_VIDEO_CLAUSE_PREFIX
    stale_clause = clause  # 打哨兵**之前**取到的那句话，下面当"旧文本"用
    try:
        video_forensics.NON_VIDEO_CLAUSE_PREFIX = "【哨兵-关键词】"
        sentinel_kw = text_of(vcall("video_probe", {"video_uri": NOT_VIDEO_URI}))
        stale_fix = str(video_pipeline.fix_for_input(stale_clause))
    finally:
        video_forensics.NON_VIDEO_CLAUSE_PREFIX = _orig_prefix  # type: ignore[assignment]
    check("**行为化**的单一来源证明②：把**共用前缀**换成哨兵 ⇒ 原因与**出路**"
          "必须**同时**跟着变（一个值、两个使用者：一个拼句子、一个做路由）",
          "哨兵-关键词" in sentinel_kw and "视频文件本身" in sentinel_kw,
          f"正文={sentinel_kw.splitlines()[0][:90] if sentinel_kw else '-'}")
    check("　└ 而**旧那句话**这时候**不再**命中路由 ⇒ 证明关键词真的换了源"
          "（手写的字面量做不到这一点：它会对旧文本一直匹配下去）",
          "视频文件本身" not in stale_fix and "完整、未截断的原始视频字节" in stale_fix,
          f"旧文本现在的出路：{stale_fix[:70]}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 4 节 · 工具语义：失败一律结构化返回，两族建议不串台")
    print("=" * 74)
    refusals = {
        "取件族 · 网络地址": "https://example.com/a.mp4",
        "取件族 · 本机路径": "C:/tmp/a.mp4",
        "取件族 · data 非 base64": "data:video/mp4,hello",
        "取件族 · data 类型不对": "data:text/plain;base64,aGk=",
        "取件族 · 坏 base64": "data:video/mp4;base64,!!!!",
        "取件族 · 空载荷": "data:video/mp4;base64,",
        "取件族 · 句柄没登记": "inbox:" + "0" * 64,
        "字节族 · 不是视频": NOT_VIDEO_URI,
    }
    for label, uri in refusals.items():
        res = vcall("video_forensics_analyze", {"video_uri": uri})
        body = text_of(res)
        check(f"{label} ⇒ `isError=true` + 一句「可以这样改」",
              res.get("isError") is True and "可以这样改" in body,
              f"{body.splitlines()[0][:70] if body else '（空）'}")
        if "取件族" in label:
            check(f"　└ {label} 的出路**不是**字节族那条（两族不串台）",
                  "视频文件本身" not in body)

    # ⚠️ 另两个工具**也要**拒绝非视频字节。这一条是补出来的：
    #    `video_forensics_analyze` 走 `outcome()`（那条路自带拒绝），而
    #    `video_probe` / `video_sample_table` 走的是本模块自己的取件函数 ——
    #    如果把 `_non_video_result()` 的判断拆掉，**只有这两个工具**会开始
    #    对一段 PDF 字节"照常出结果"。元验证（`run/meta_verify_video.py`）
    #    要求这条断言在那种回退下报失败，所以它必须存在。
    for tool in ("video_probe", "video_sample_table"):
        res = vcall(tool, {"video_uri": NOT_VIDEO_URI})
        check(f"`{tool}` 也不许对非视频字节照跑"
              "（要说「你给错了东西」，并给出**字节族**那条出路）",
              res.get("isError") is True
              and "不是可识别的视频容器" in text_of(res)
              and "视频文件本身" in text_of(res),
              text_of(res).splitlines()[0][:80] if text_of(res) else "（空）")

    # ── 正面对照（与上面那组**成对**）：一段「认得但没解析器」的视频**必须被服务** ──
    # ⚠️ 2026-10-04 体检出来的真缺陷就在这里：`application/ogg` **在解码层的容器表里**
    #    （`_OTHER_CONTAINERS` 给它配了中文名、`sniff_media_type` 有专门的 `OggS` 分支、
    #    `identify_only_note` 的 docstring 更把它列为适用对象），却被自家工具拒了，
    #    理由还是假话「不是可识别的视频容器」；同族的 `video/mp2t` 只因为字符串带
    #    `video/` 前缀就通行无阻。**差别只在字符串前缀** —— 这就是"判据是代理的"的代价。
    #    样本表按 `_OTHER_CONTAINERS` **现场派生**校验，所以以后加容器不许漏。
    ts_buf = bytearray(565)
    for _off in (0, 188, 376):
        ts_buf[_off] = 0x47
    other_samples = {
        "video/mp2t": bytes(ts_buf),
        "video/x-flv": b"FLV\x01" + bytes(300),
        "video/mpeg": b"\x00\x00\x01\xba" + bytes(300),
        "video/x-ms-wmv": b"\x30\x26\xb2\x75\x8e\x66\xcf\x11" + bytes(300),
        "application/ogg": b"OggS\x00\x02" + bytes(300),
        "video/x-msvideo-unknown":
            b"RIFF" + (400).to_bytes(4, "little") + b"XXXX" + bytes(400),
    }
    check("六个「只识别」容器**逐个**造得出魔术字节（否则下面那组是空转）",
          set(other_samples) == set(video_forensics._OTHER_CONTAINERS),
          f"表={sorted(video_forensics._OTHER_CONTAINERS)}")
    for media_type, sample in other_samples.items():
        # ⚠️ Ogg 在这里刻意声明 `video/ogg` —— 那正是 .ogv 的**标准 MIME**，
        #    也是当初那条假线索的触发条件（`video/ogg` ≠ `application/ogg`）。
        mime = "video/ogg" if media_type == "application/ogg" else media_type
        uri = data_uri(sample, mime)
        results = {tool: vcall(tool, {"video_uri": uri})
                   for tool in ("video_probe", "video_sample_table",
                                "video_forensics_analyze")}
        bad = [t for t, r in results.items()
               if r.get("isError") is not False
               or "不是可识别的视频容器" in text_of(r)]
        check(f"`{media_type}`（认得但没解析器）· 三个工具**都要服务它**"
              "（说「你给错了东西」才是错的）",
              not bad, f"仍拒的工具：{bad}")
        check(f"　└ `{media_type}` · 探针正文写出它的**容器中文名**（认得就要说出来）",
              video_forensics._OTHER_CONTAINERS[media_type] in text_of(results["video_probe"]),
              f"找 {video_forensics._OTHER_CONTAINERS[media_type]!r}")
        check(f"　└ `{media_type}` · 分析工具的等级必须是**无法判定**"
              "（内部一层都没解析就给等级 = 静默给结论）",
              "综合线索等级：无法判定"
              in text_of(results["video_forensics_analyze"]),
              text_of(results["video_forensics_analyze"]).splitlines()[0][:60]
              if text_of(results["video_forensics_analyze"]) else "（空）")

    # 取件族 9 个分类码**逐码**有专属出路（与检测层同一条"逐码"纪律）
    kinds = list(video_fetch.FETCH_REFUSAL_KINDS)
    missing = [k for k in kinds if video_pipeline.fix_for(k) == video_pipeline._DEFAULT_FIX_POLICY]
    check(f"取件族 {len(kinds)} 个分类码**逐个**都有专属出路（没有谁掉进兜底）",
          missing == [], f"掉进兜底的码：{missing}")

    # 三个工具的失败形态都必须是结构化返回（一个异常都不许漏）
    leaky: list[str] = []
    for tool in names:
        for uri in ("", "   ", "https://x/a.mp4", NOT_VIDEO_URI, "inbox:zzz"):
            res = vcall(tool, {"video_uri": uri})
            if res.get("_raised"):
                leaky.append(f"{tool}/{uri[:16]}:{res['_raised']}")
    check("三个工具 × 五种畸形引用：**一个异常都不许漏出来**（全部结构化返回）",
          leaky == [], f"漏出来的：{leaky}")

    bad_args = {
        "未知工具名": ("video_bogus", {}),
        "缺 `video_uri`": ("video_probe", {}),
        "`video_uri` 不是字符串": ("video_probe", {"video_uri": 42}),
        "`declared_mime` 类型不对": ("video_probe", {"video_uri": CLEAN_URI, "declared_mime": 7}),
        "`context_note` 类型不对": ("video_forensics_analyze",
                                    {"video_uri": CLEAN_URI, "context_note": []}),
    }
    for label, (tool, args) in bad_args.items():
        res = vcall(tool, args)
        check(f"参数问题 · {label} ⇒ `isError=true`（**不是** -32602）",
              res.get("isError") is True and "error" not in res,
              f"{text_of(res).splitlines()[0][:70] if text_of(res) else '（空）'}")
    check("未知工具名会列出**视频**那三个可用工具",
          "video_sample_table" in text_of(vcall("video_bogus", {})),
          text_of(vcall("video_bogus", {}))[:90])

    # `arguments` 是 null / 不是对象
    res_null = S.handle_message(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
         "params": {"name": "video_probe", "arguments": None}}, family=VS.FAMILY)["result"]
    check("`arguments` 为 null ⇒ 当作空对象走正常参数校验（不是崩掉）",
          res_null.get("isError") is True and "video_uri" in text_of(res_null))
    res_arr = S.handle_message(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
         "params": {"name": "video_probe", "arguments": [1, 2]}}, family=VS.FAMILY)["result"]
    check("`arguments` 不是对象 ⇒ 点名类型（不是崩掉）",
          res_arr.get("isError") is True and "必须是 JSON 对象" in text_of(res_arr),
          text_of(res_arr)[:90])
    print()

    # =======================================================================
    print("=" * 74)
    print("第 5 节 · 字段口径：`container_frames` / 声明值措辞 / 帧层后果")
    print("=" * 74)
    probe = vcall("video_probe", {"video_uri": CLEAN_URI, "declared_mime": "video/mp4"})
    pl = payload_of(probe) or {}
    check("`video_probe` 的 JSON 里有 `container_frames`",
          "container_frames" in pl, f"keys={sorted(pl)[:8]}")
    check("……而且**没有** `frames_available` —— 否则一份动画 GIF 会被读成"
          "「帧层跑不了」（它其实跑得了：帧要光栅化，那在分析里做）",
          "frames_available" not in pl)
    check("……正文也交代了「0 帧不等于帧层跑不了」",
          "不等于" in text_of(probe) and "GIF" in text_of(probe))
    check("……`video_probe` 明确**不给结论**，并指向另两个工具",
          "不给结论" in text_of(probe)
          and "video_forensics_analyze" in text_of(probe)
          and "video_sample_table" in text_of(probe))
    check("……`media_type` 是**按魔术字节**判的（合成 MP4 ⇒ video/mp4）",
          pl.get("media_type") == "video/mp4", f"实得 {pl.get('media_type')!r}")
    check("……`fetched_from` 如实写 `inline_base64`（溯源字段不许含糊）",
          pl.get("fetched_from") == "inline_base64", f"实得 {pl.get('fetched_from')!r}")

    table = vcall("video_sample_table", {"video_uri": CLEAN_URI})
    tp = payload_of(table) or {}
    check("`video_sample_table` 给出逐轨声明数字（tracks 非空、含 stts 分布与 chunk 偏移）",
          bool(tp.get("tracks")) and all(
              k in tp["tracks"][0]
              for k in ("timescale", "sample_count", "delta_kinds", "chunk_count", "edit_list")
          ), f"tracks={len(tp.get('tracks') or [])}")
    check("……正文把「这些是**容器声明的**数字，不是实测的」写清楚",
          "声明" in text_of(table) and "不是实测" in text_of(table))
    check("……并且写明**不下结论**、**不解码任何帧**（免得被当成画面层的证据）",
          "不下结论" in text_of(table) and "不解码任何帧" in text_of(table))
    check("……`parse_depth` 与 `declared_size` / `actual_size` 都在（截断判据的依据）",
          "parse_depth" in tp and "declared_size" in tp and "actual_size" in tp)

    # 帧层后果：一份解不出帧的 MP4，报告**不许**写「没问题」，且必须点名缺席项
    ana = vcall("video_forensics_analyze", {"video_uri": CLEAN_URI})
    ajson = payload_of(ana) or {}
    check("解不出帧的 MP4 ⇒ `frames_available=false` 且 `frames_note` 写明后果",
          ajson.get("frames_available") is False
          and "帧层检查整项没有执行" in str(ajson.get("frames_note", "")),
          f"frames_note={str(ajson.get('frames_note',''))[:70]!r}")
    check("……摘要**绝不**出现「均未提示异常」（那读起来就是「没问题」）",
          "均未提示异常" not in text_of(ana))
    check("……摘要写明「不能据此认为「没有问题」」",
          "不能据此认为" in text_of(ana))

    # -- B4：探针收下的 `declared_mime` **只回显、不参与判定** -------------------
    # 原 schema 写的是「用于与真实容器比对」，而 `_handle_probe` 一行都没比 ——
    # 承诺一件没做的事，与「收下了却看不见」同族：调用方会以为探针已经替它比过了。
    # ⚠️ 主判据是**行为**，不是 grep 那句描述：描述改回原样而代码一字不动时，
    #    「描述里有没有某个词」照样绿（本项目「判据是代理的」那条）。
    # ⚠️ 也不能只断言「换个错 MIME 仍然成功」——那样把比对**彻底删掉**也照样绿。
    #    所以两侧都要钉：探针**不比**（无「不一致」），analyze **必须比**（点名）。
    probe_props = next(
        t for t in VF.TOOLS if t["name"] == "video_probe"
    )["inputSchema"]["properties"]
    wrong = vcall("video_probe", {"video_uri": CLEAN_URI, "declared_mime": "audio/mpeg"})
    wp = payload_of(wrong) or {}
    check("探针喂一个**错的** `declared_mime`（字节是 MP4、声明 audio/mpeg）⇒ "
          "照样成功，且 `media_type` 仍按**魔术字节**给出 `video/mp4`",
          wrong.get("isError") is False and wp.get("media_type") == "video/mp4",
          f"isError={wrong.get('isError')!r} media_type={wp.get('media_type')!r}")
    check("……正文里**没有**任何比对措辞（探针不比对 ⇒ 说它比过就是撒谎）",
          "不一致" not in text_of(wrong))
    check("……但 `declared_mime` 仍然**原样回显**（不比对 ≠ 吞掉）",
          wp.get("declared_mime") == "audio/mpeg", f"实得 {wp.get('declared_mime')!r}")
    check("……schema 描述如实写「仅原样回显，不参与任何判定」（描述与行为必须一致）",
          "不参与任何判定" in str(
              (probe_props.get("declared_mime") or {}).get("description", "")))

    mism = vcall("video_forensics_analyze",
                 {"video_uri": CLEAN_URI, "declared_mime": "audio/mpeg"})
    mp = payload_of(mism) or {}
    mism_c = next(
        (f for f in (mp.get("findings") or []) if f.get("check") == "container"), {})
    check("同一个错声明交给 `video_forensics_analyze` ⇒ **container 项点名**"
          "「调用方声明的是 audio/mpeg，而**字节本身**是 video/mp4」"
          "（比对只在 analyze 这一处做）",
          mism.get("isError") is False
          and "字节本身" in str(mism_c.get("detail", ""))
          and "audio/mpeg" in str(mism_c.get("detail", "")),
          f"container={str(mism_c.get('detail', ''))[:110]!r}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 6 节 · 真子进程端到端（stdio、stdout 无脏字节、-h 走 stderr）")
    print("=" * 74)
    msgs: list[dict[str, Any]] = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "video_probe", "arguments": {"video_uri": CLEAN_URI}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "video_forensics_analyze",
                    "arguments": {"video_uri": "https://example.com/x.mp4"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled"},
    ]
    stdin_data = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs)
    proc, proc_err = attempt(subprocess.run,
                            [sys.executable, "-m", "legal_acps.mcp.video_server"],
                            input=stdin_data.encode("utf-8"), cwd=str(ROOT),
                            capture_output=True, timeout=180)
    check("`python -m legal_acps.mcp.video_server` 能作为子进程跑完（stdin EOF ⇒ 正常退出）",
          proc_err is None and proc is not None and proc.returncode == 0,
          f"异常={proc_err!r} rc={getattr(proc, 'returncode', None)}")
    lines = [ln for ln in ((proc.stdout or b"").decode("utf-8") if proc else "").splitlines() if ln.strip()]
    check("7 条报文（含 2 条通知）⇒ 恰 5 条应答",
          len(lines) == 5, f"实得 {len(lines)} 条：{[ln[:40] for ln in lines][:3]}")
    parsed: list[Any] = []
    for ln in lines:
        try:
            parsed.append(json.loads(ln))
        except ValueError:
            parsed.append(None)
    check("**stdout 只有协议消息**（每一行都是合法 JSON，没有日志混进去）",
          all(isinstance(p, dict) for p in parsed) and len(parsed) == len(lines))
    check("应答的 id 依次是 1 / 2 / 3 / 4 / 5（两条通知被吞掉，没有 id:null 的应答）",
          [p.get("id") for p in parsed if isinstance(p, dict)] == [1, 2, 3, 4, 5],
          f"实得 {[p.get('id') for p in parsed if isinstance(p, dict)]}")
    if len(parsed) == 5 and all(isinstance(p, dict) for p in parsed):
        check("子进程的 `tools/list` 也是视频那三个",
              [t["name"] for t in (parsed[1].get("result") or {}).get("tools", [])] == names)
        check("子进程的 `video_probe` 结果拿得到 `container_frames`",
              "container_frames" in text_of(parsed[2].get("result")))
        check("子进程的拒绝侧：`isError=true` + 「可以这样改」",
              (parsed[3].get("result") or {}).get("isError") is True
              and "可以这样改" in text_of(parsed[3].get("result")))

    helped, help_err = attempt(subprocess.run,
                              [sys.executable, "-m", "legal_acps.mcp.video_server", "-h"],
                              cwd=str(ROOT), capture_output=True, timeout=180)
    check("`-h` 的说明走 **stderr**、stdout 一个字节都不许有"
          "（stdout 是协议专用，写脏了客户端解析失败）",
          help_err is None and helped is not None and helped.stdout == b""
          and b"video_server" in helped.stderr,
          f"stdout={getattr(helped, 'stdout', b'')[:40]!r}")
    check("……`-h` 里回显的启动模块是**视频**那个（不许告诉用户去启动音频服务）",
          help_err is None and helped is not None
          and b"legal_acps.mcp.video_server" in helped.stderr)

    # ── 第三族：`-h` 回显的入口必须**随族走**（2026-10-05 体检 B5）────────────
    #    缺陷形态：入口名由 `_module_of()` 按"是不是音频族"二选一得出来 ⇒
    #    只有两个族时看着也对，**第三个族会拿到视频那个入口** —— 一句看起来
    #    完全正常、实际指错路的假话（用户照它敲下去会起错服务）。
    #    ⇒ 判据必须**造一个真的第三族**，而不是断言现有两族的值（那只是快照）。
    third = S.ToolFamily(
        name="第三族探针", version="0.0.0-probe",
        instructions="…", module=VF, entry="legal_acps.mcp.third_probe", usage="…",
    )
    check("`-h` 的启动模块取自**族自己的 `entry`**（第三族必须回显自己的入口）",
          S._module_of(third) == "legal_acps.mcp.third_probe",
          f"实得 {S._module_of(third)!r}")
    _buf = io.StringIO()
    with contextlib.redirect_stderr(_buf):
        rc_h = S.main(["-h"], family=third)
    h_text = _buf.getvalue()
    check("……第三族的 `-h` 正文里**只有它自己的**入口"
          "（不许出现音频/视频那两个 ⇒ 不许再按「是不是某一个族」去猜）",
          rc_h == 0 and "legal_acps.mcp.third_probe" in h_text
          and "legal_acps.mcp.video_server" not in h_text
          and "legal_acps.mcp.server" not in h_text,
          f"rc={rc_h}；stderr={h_text[:80]!r}")
    check("……现有两族的入口名仍然对（改一处不许把另两个带偏）",
          S._module_of(S.AUDIO_FAMILY) == "legal_acps.mcp.server"
          and S._module_of(VS.FAMILY) == "legal_acps.mcp.video_server")

    # ── 未知参数：不许默默进 `serve()`（2026-10-05 体检 B7）──────────────────
    #    `serve()` 会立刻在 stdin 上阻塞读报文 ⇒ 用户只是把 `-help` 拼成单横线，
    #    看到的是"敲了回车没反应、像卡住了"，**看不出是自己敲错还是服务坏了**。
    _err_buf = io.StringIO()
    with contextlib.redirect_stderr(_err_buf):
        rc_bad = S.main(["-help"], family=VS.FAMILY)
    bad_text = _err_buf.getvalue()
    check("未知参数 ⇒ **退出码 2** 并说明「无法识别的参数」，**不进** `serve()`",
          rc_bad == 2 and "无法识别的参数" in bad_text,
          f"rc={rc_bad}；stderr={bad_text[:80]!r}")
    bad_run, bad_err = attempt(subprocess.run,
                               [sys.executable, "-m", "legal_acps.mcp.video_server", "-help"],
                               cwd=str(ROOT), capture_output=True, timeout=60)
    check("……真跑子进程也一样：退出码 2、不挂在 stdin 上"
          "（挂在 stdin 的话这里会超时，或退出码 0 而什么都不说）",
          bad_err is None and bad_run is not None and bad_run.returncode == 2
          # ⚠️ 别写 `b"中文"` —— bytes 字面量里只能有 ASCII（SyntaxError）。
          and "无法识别的参数" in (bad_run.stderr or b"").decode("utf-8", "replace"),
          f"rc={getattr(bad_run, 'returncode', None)}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 7 节 · 按需导入面：只导入入口不许拉起检测核心")
    print("=" * 74)
    probe_src = (
        "import json, sys\n"
        "import legal_acps.mcp.video_server as vs\n"
        "print(json.dumps({\n"
        "    'video': 'legal_acps.video' in sys.modules,\n"
        "    'numpy': 'numpy' in sys.modules,\n"
        "    'tools': sorted(t['name'] for t in vs.FAMILY.module.TOOLS),\n"
        "}, ensure_ascii=False))\n"
    )
    sub, sub_err = attempt(subprocess.run, [sys.executable, "-c", probe_src],
                           cwd=str(ROOT), capture_output=True, timeout=180)
    state: dict[str, Any] = {}
    if sub is not None and not sub_err:
        try:
            state = json.loads((sub.stdout or b"").decode("utf-8").strip().splitlines()[-1])
        except (ValueError, IndexError):
            state = {}
    check("**只导入视频入口不会拉起检测核心，更不会拉起 numpy**："
          "服务器上没装 numpy 时 `tools/list` 仍然可用 —— "
          "否则客户端看到的是「连不上」，而不是一句「缺个依赖」",
          not sub_err and state.get("video") is False and state.get("numpy") is False,
          f"实得：{state or sub_err!r}")
    check("……而即便没导入核心，`TOOLS` 的三个 schema 已经在手（schema 常驻、实现按需）",
          state.get("tools") == sorted(names), f"实得：{state.get('tools')!r}")

    core, core_err = attempt(VF._video_module)
    required = ("decode_video", "load", "VideoFetchRefused", "analyze",
                "sha256_of", "is_supported_video", "non_video_clause",
                "outcome", "fix_for", "fix_for_input")
    missing = [k for k in required
               if core is None or not callable(getattr(core, k, None))]
    # ⚠️ 数量**从那个元组现场派生** —— 上一版把「八个符号」写死在标签里，
    #    而这一轮恰恰要新加两个符号：写死的数字会当场变成假话（本项目老账）。
    check(f"`_video_module()` 显式给出**列明依赖面**的命名空间"
          f"（{len(required)} 个符号都真的可用）",
          core_err is None and not missing, f"缺/不可用：{missing}；异常={core_err!r}")
    check("……其中 `outcome` / `fix_for` / `fix_for_input` 必须来自 `pipeline`"
          "（MCP 不许另写一份出路表）",
          core is not None
          and core.outcome is video_pipeline.outcome
          and core.fix_for is video_pipeline.fix_for
          and core.fix_for_input is video_pipeline.fix_for_input)
    check("……`is_supported_video` / `non_video_clause` 必须来自 `forensics`"
          "（「谁算视频」与「不是视频那句话」都只有一个来源）",
          core is not None
          and core.is_supported_video is video_forensics.is_supported_video
          and core.non_video_clause is video_forensics.non_video_clause)
    check("……`analyze` / `sha256_of` 来自 `forensics`，`decode_video` 来自 `decode`",
          core is not None
          and core.decode_video.__module__ == "legal_acps.video.decode"
          and core.analyze.__module__ == "legal_acps.video.forensics"
          and core.sha256_of.__module__ == "legal_acps.video.forensics")

    # ⚠️ 这一条是**协议层共用**的静态半张脸：视频入口里不许出现协议件。
    #    动态的那半张脸在 `run/meta_verify_video.py`（把参数化退回旧形态 ⇒ 视频报失败）。
    #
    # ⚠️ **必须走 AST，不能用文本子串查。** 本文件的第一版就是文本查，
    #    结果命中的是 `video_server.py` 里那句**警告本身**
    #    （docstring 写着"一旦本文件开始出现 `is_notification` / `read_message`
    #    这类东西，就说明有人把协议抄了第二份"）—— 于是**越把规矩写清楚、判据越红**。
    #    这是本项目「判据不能退化成 grep 源码」的现行犯：文本查既会误报（注释里提到），
    #    也会漏报（改名成 `_read` 就查不出）。AST 里注释与文档字符串**天然不存在**。
    import ast  # noqa: PLC0415

    src = (ROOT / "legal_acps" / "mcp" / "video_server.py").read_text(encoding="utf-8")
    tree, ast_err = attempt(ast.parse, src)
    proto_names = {
        "is_notification", "read_message", "write_message", "handle_message",
        "negotiate_protocol", "serve", "PARSE_ERROR", "INVALID_REQUEST",
        "METHOD_NOT_FOUND", "INVALID_PARAMS", "INTERNAL_ERROR",
        "SUPPORTED_PROTOCOL_VERSIONS", "DEFAULT_PROTOCOL_VERSION",
        "json", "dumps", "loads",
    }
    hits: list[str] = []
    funcs: list[str] = []
    if ast_err is None and tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.append(node.name)
            elif isinstance(node, ast.Name) and node.id in proto_names:
                hits.append(f"Name:{node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in proto_names:
                hits.append(f"Attr:{node.attr}")
            elif isinstance(node, ast.alias) and node.name.split(".")[-1] in proto_names:
                hits.append(f"Import:{node.name}")
    else:
        hits.append(f"（源码不是合法 Python：{ast_err!r}）")
    check("`video_server.py` 的**代码**里不许出现任何协议件"
          "（出现了就说明有人把协议抄了第二份）—— 走 AST 判，注释与文档字符串不算",
          hits == [], f"实得：{hits}")
    check("……而且它只该有一个函数：`main`（把族交给协议层就够了，没有别的活儿）",
          funcs == ["main"], f"实得：{funcs}")
    check("……它 import 的也只有协议层与自己的工具模块（没有第三样东西）",
          not ast_err and [
              a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
              for a in n.names
          ] == []
          and sorted(
              a.name for n in ast.walk(tree)
              if isinstance(n, ast.ImportFrom) and n.module != "__future__"
              for a in n.names
          ) == ["server", "video_forensics"],
          f"实得：{[a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]}"
          f" / {sorted(a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names)}")

    print()
    print("=" * 74)
    if FAILED:
        print(f"❌ 共 {len(FAILED)} 条未达预期：")
        for name in FAILED:
            print(f"   · {name}")
        return 1
    print("✅ 全部用例符合预期：")
    print("   第 1 节 工具清单与 schema 自洽（三个工具，与音频侧不相交）")
    print("   第 2 节 协议语义（默认族未变 / 视频族分派 / 通知与 id=null 的判据）")
    print("   第 3 节 **出口同源**（MCP 的 JSON ≡ ACPs 的 JSON，成功与拒绝两侧）")
    print("   第 4 节 工具语义（失败一律结构化返回、两族建议不串台）")
    print("   第 5 节 字段口径（`container_frames` / 声明值 / 帧层后果）")
    print("   第 6 节 真子进程端到端（7 报文 5 应答、stdout 无脏字节）")
    print("   第 7 节 按需导入面 + 入口里不许有协议件")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
