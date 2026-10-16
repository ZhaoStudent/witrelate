"""视频鉴伪 MCP 工具的定义与实现 —— **复用 `legal_acps.video` 的同一套纯函数**。

════════════════════════════════════════════════════════════════════════
为什么工具层这里只做"翻译"，不重做任何判定
════════════════════════════════════════════════════════════════════════

与 `audio_forensics.py` **同一条纪律**（那边吃过亏，这边直接照抄结论）：

    工具层**只做三件事**：解析参数 → 调 `legal_acps.video` → 把结果翻译成
    MCP 能表达的形状（文本 + JSON）。**一行判据都不在这里。**

直接后果：MCP 调出来的结论与 ACPs 链路上 `evidence-video` 调出来的结论
**必然逐字相同** —— 因为它们是同一个函数。这一点由
`tools/verify_video_mcp.py` 直接断言（同一条 uri 两边跑，比对 JSON）。

⚠️ 三个工具都**不出网**，也**不读本机磁盘**：只认 `inbox:<sha256>` 句柄与
`data:video/...;base64,...`。这条限制继承自 `legal_acps.video.fetch`，
本模块不去、也不能绕过它（不提供任何"顺手读个本地文件"的开关）。

════════════════════════════════════════════════════════════════════════
与音频侧的两个**实质差异**（都是数出来的，不是漏了）
════════════════════════════════════════════════════════════════════════

1. **本族没有 enum 型参数。** 音频侧有 `base_hz ∈ {50, 60}`，那需要在 schema
   之外**自己再判一次**（JSON Schema 的 `enum` 不是所有客户端都会执行），
   于是音频模块里多出一个 `_ENF_BASES` 需要与核心的 `ENF_BASE_FREQS` 钉住。
   视频三个工具的参数只有字符串与"字符串或空"两种 ⇒ **没有第二处取值域定义**。
   这一条是**数出来的**：`_HANDLERS` 的表里没有第三种参数形态。

2. **第三个工具摊开的是"声明数字"，不是"实测波形"。** 音频侧的 ENF 是从
   波形上算出来的**实测**轨迹；视频侧没有可比的对象 —— 帧层要么有像素
   （那条路已经在 `video_forensics_analyze` 的 `frames` 项里如实报告了），
   要么一整层没跑。所以第三个工具选的是**容器里声明的**时间线 / 样本表 /
   编辑列表数字：它们正是权重最高的那两项检查（`samples` / `timeline`）
   所依据的原始材料，而且**零依赖**（`decode.py` 是纯标准库）。
   ⚠️ 它的正当性来自一个具体事实：这两项判的是"声明之间自不自洽"，
   而**人工复核时唯一能拿到的就是这几个数字** —— 不摊开，读者只能选择信或不信。

⚠️ 所以本模块刻意**不给帧层单独做一个工具**：帧层的一切
（`frames_available` / `frames_note` / `frames_analyzed` / `frame_source`
以及 `frames` 那一项 finding）都已经在 `video_forensics_analyze` 的报告里。
再开一个工具只会给出第二份措辞，而"同一判定写两处必然漂移"是本项目的老账。
"""

from __future__ import annotations

import json
from typing import Any, Callable

#: ⚠️ **不在模块级 import `legal_acps.video`。**
#:
#: 理由与音频侧逐字相同：`tools/list` 只用到下面的 schema（纯数据），
#: 而 schema 与 numpy 无关。放在模块级 import，会让"服务器上没装 numpy"
#: 直接变成**进程启动失败** —— MCP 客户端看到的是一个"连不上/无响应"的服务器，
#: **没有任何提示**，而真正的原因（少一个可选依赖）本来一句话就能说清。
_MISSING_DEP_HINT = (
    "本工具需要 `numpy` 与 `legal_acps.video`（视频检测核心）。"
    "当前进程里导入失败 —— 请确认在该 Python 环境里装了 numpy，"
    "并且是以**项目根目录**为工作目录启动（`legal_acps` 是相对包名）。"
    "⚠️ 动画 GIF 的逐帧光栅化还要 `Pillow`，但那只影响 GIF 的帧层，"
    "容器层与时间线层不受影响。"
)


def _video_module() -> Any:
    """按需导入检测核心，返回一个**显式列明依赖面**的命名空间。

    ⚠️ **必须显式 `from legal_acps.video import <子模块>`，不许走包属性。**
    理由是音频侧实测出来的、一模一样的那一条：`legal_acps.video` 是 PEP 562
    惰性包，`__getattr__` 只认 `_LAZY` 表里的名字，而"能取到某个名字"有时
    只是因为**别处先导入过那个子模块**。于是一个依赖调用顺序的导入会表现出
    "先调 A 就报 AttributeError、先调过 B 却能跑" —— 本项目最忌讳的那种
    "看起来正常"。显式导入子模块没有这个歧义。
    """
    from types import SimpleNamespace  # noqa: PLC0415

    from legal_acps.video import decode as _decode  # noqa: PLC0415
    from legal_acps.video import fetch as _fetch  # noqa: PLC0415
    from legal_acps.video import forensics as _forensics  # noqa: PLC0415
    from legal_acps.video import pipeline as _pipeline  # noqa: PLC0415

    return SimpleNamespace(
        decode_video=_decode.decode_video,
        load=_fetch.load,
        VideoFetchRefused=_fetch.VideoFetchRefused,
        analyze=_forensics.analyze,
        sha256_of=_forensics.sha256_of,
        # 「谁算视频」的判据与「不是视频」那句话，**只从检测层取**。
        # ⚠️ 这不是"顺手再抄一份"：抄一份就是 Ogg 那件事的复发路径
        #    （MCP 当初抄了前缀测试，于是解码层认得、表里有中文名的
        #    `application/ogg` 被自家工具拒了）。见 `_non_video_result`。
        is_supported_video=_forensics.is_supported_video,
        non_video_clause=_forensics.non_video_clause,
        outcome=_pipeline.outcome,
        fix_for=_pipeline.fix_for,
        fix_for_input=_pipeline.fix_for_input,
    )


# ---------------------------------------------------------------------------
# 服务标识与开场白（协议层从这两个常量与 `INSTRUCTIONS` 取，不看模态）
# ---------------------------------------------------------------------------

#: 会出现在 `initialize` 的应答里。
SERVER_NAME = "legal-acps-video-forensics"
SERVER_VERSION = "1.0.0"

#: `initialize` 的 `instructions`。**必须点明那两句最容易转述错的话**
#: （"无法判定"是合法答案、帧层整层没跑是常态）——
#: 它们由 `tools/verify_video_mcp.py` 断言，不是文案偏好。
INSTRUCTIONS = (
    "离线视频编辑痕迹初检。三个工具：`video_probe`（只看容器与「容器里零解码可取到的帧」）、"
    "`video_forensics_analyze`（七项检测 + 综合线索等级）、"
    "`video_sample_table`（逐轨声明的时间线 / 样本表 / 编辑列表数字）。"
    "⚠️ 输入只接受 `inbox:<sha256>` 句柄或 `data:video/...;base64,...` 内联字节："
    "**本服务不出网、不读本机文件**。"
    "⚠️ 输出没有「真/假」，只有线索与「无法判定」——"
    "后者是合法且经常正确的答案，不要把它转述成「没问题」。"
    "⚠️ 服务器上**没有视频解码器**（没有 ffmpeg / OpenCV / PyAV）："
    "H.264 / H.265 一律解不出帧 ⇒「**帧层整项没有执行**」是常态而非例外，"
    "而帧层恰恰是唯一能直接看到画面改动的那一层，所以「容器层没发现问题」"
    "**不等于**「这段视频没有问题」。"
)

#: `-h` 里那一行。与音频侧同形。
USAGE = (
    "本服务不出网、不读本机文件；视频引用只接受 inbox:<sha256> "
    "与 data:video/...;base64,...。\n"
)


# ---------------------------------------------------------------------------
# 工具定义（纯数据）
# ---------------------------------------------------------------------------

_URI_DESCRIPTION = (
    "视频引用。**只接受两种**：① `inbox:<sha256>` —— 随任务附件进来的字节，"
    "句柄是**内容哈希**（64 位小写十六进制），不接受路径；"
    "② `data:video/mp4;base64,...` —— 自带字节（动画 GIF 写成 "
    "`data:image/gif;base64,...` 也接受）。"
    "⚠️ **本服务不出网、也不读本机文件**：https 地址、file://、裸路径一律被拒绝"
    "（拒绝时会说明该怎么改）。"
    "⚠️ 视频动辄几十 MB，内联 base64 在实践中基本不可用 —— 首选附件通道。"
)

#: 工具的**唯一来源**。`tools/list` 直接吐它，`tools/call` 按 `name` 找它。
#: 加一个工具 = 在这里加一项 + 在 `_HANDLERS` 加一行，两处都由
#: `verify_video_mcp.py` 断言存在（免得出现"列表里有、调不到"的空工具）。
TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "video_probe",
        "title": "视频快速探查（只看容器与帧层来源）",
        "description": (
            "**不判定的**第一眼：这段字节到底是什么容器、声明了哪些轨道与参数、"
            "解析深度到哪一层（完整解析 / 只识别容器），以及**容器里零解码可取到几帧**"
            "（MJPEG 逐帧 / 内嵌封面帧）。用途是在跑完整检测之前先确认"
            "「这份东西我们能不能看、能看到哪一层」。"
            "⚠️ 动画 GIF 的帧要光栅化才有，那属于分析步骤，**本工具不代劳** —— "
            "所以 GIF 在这里报 0 帧是**正常的**，不代表帧层跑不了。"
            "本工具不返回任何异常/正常结论。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_uri": {"type": "string", "description": _URI_DESCRIPTION},
                "declared_mime": {
                    "type": "string",
                    # ⚠️ 这句原先写的是「用于与真实容器比对」，而本处理器**只回显**。
                    #    承诺一件没做的事，与"收下了却看不见"同族 —— 调用方会以为
                    #    探针已经替它比过了。真正比对的地方是 analyze 的 `container` 项，
                    #    在那里再比一次就是"同一判定写两处"。
                    "description": "调用方声明的 MIME（如 video/mp4）。"
                                   "**仅原样回显，不参与任何判定** —— "
                                   "「声明与真实容器是否一致」由 `video_forensics_analyze` "
                                   "的 `container` 一项报出；可不填。",
                },
            },
            "required": ["video_uri"],
            "additionalProperties": False,
        },
    },
    {
        "name": "video_forensics_analyze",
        "title": "视频编辑痕迹初检（七项，离线）",
        "description": (
            "对一段视频做**离线**编辑痕迹初检，返回七项逐项发现 + 综合线索等级 + "
            "复检阶梯。线索包括：容器声明一致性、盒树 / 分段布局、三处时长自洽性、"
            "样本表自洽性与偏移越界、编辑列表（跳段 / 变速）、时间戳与自报编码工具、"
            "抽样帧（接图像鉴伪链）。"
            "**输出没有「真/假」**：只有线索强度、逐项 caveat 与综合等级"
            "（低/中/高/无法判定）。等级由程序按固定权重算出，不是模型给的。"
            "任一检查项因前提不满足而没跑时，会在「无法判定」里如实出现，"
            "绝不会被写成「没问题」。"
            "⚠️ 解不出 H.264 / H.265 的帧 ⇒ 帧层整项没跑是**常态**；"
            "报告里的 `frames_note` 会写清后果。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_uri": {"type": "string", "description": _URI_DESCRIPTION},
                "declared_mime": {
                    "type": "string",
                    "description": "调用方声明的 MIME。与真实容器不符时会作为一条线索报出。",
                },
                "context_note": {
                    "type": "string",
                    "description": "这段视频在案情里的分量（如「据称是行车记录仪导出」）。"
                                   "**仅原样回显**，不参与任何判定。",
                },
            },
            "required": ["video_uri"],
            "additionalProperties": False,
        },
    },
    {
        "name": "video_sample_table",
        "title": "逐轨声明数字（时间线 / 样本表 / 编辑列表）",
        "description": (
            "把**容器里声明的**逐轨数字摊开，供人工核查 `samples`、`timeline`、`edits` "
            "三项结论：每轨的时间刻度、声明时长、样本数、编码/展示尺寸、"
            "`stts` 采样间隔分布、`stsz` 样本尺寸、chunk 偏移范围、`elst` 编辑列表；"
            "以及容器层的声明长度与实际字节数之差、`moov`/`mdat` 布局、分段数。"
            "⚠️ 这些**全部是容器自己写的数字**，没有一条是实测的 —— "
            "一条被改过头的视频，它的 `stsd` 里写着 30 fps 而实际是 25 fps，"
            "那正是 `timeline` 那一项要找的东西。所以本工具**只摊开数字、不下结论**。"
            "⚠️ 它同样是**离线**的，且不解码任何帧。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_uri": {"type": "string", "description": _URI_DESCRIPTION},
            },
            "required": ["video_uri"],
            "additionalProperties": False,
        },
    },
)


# ---------------------------------------------------------------------------
# 结果形状：MCP 要求 `content` 是 part 数组
# ---------------------------------------------------------------------------

def _text_part(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _ok(parts: list[dict[str, str]], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """成功结果。**正文永远包含一份可机读的 JSON**（放在显眼的标记之间）。

    为什么同一份内容既给人话又给 JSON：MCP 的消费方既可能是模型
    （读人话更省 token、也更不容易误读量值），也可能是脚本
    （要结构化字段）。只给人话则脚本要写正则，只给 JSON 则模型容易
    把"声明 30 fps"这类数字复述成"实测 30 fps"。
    """
    if payload is not None:
        parts = parts + [
            _text_part(
                "```json\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)
                + "\n```"
            )
        ]
    return {"content": parts, "isError": False}


def _fail(reason: str, fix: str | None = None) -> dict[str, Any]:
    """工具级失败。**是正常返回（`isError=true`），不是 JSON-RPC error。**

    与音频侧同一条理由：调用方（模型）需要读到"为什么不行 + 可以怎么改"。
    抛 JSON-RPC 错误的话，客户端往往只显示"tool failed"。
    """
    body = reason if not fix else f"{reason}\n\n**可以这样改**：{fix}"
    return {"content": [_text_part(body)], "isError": True}


def _missing_dep() -> dict[str, Any]:
    return _fail(f"本服务缺少运行依赖：{_MISSING_DEP_HINT}")


# ---------------------------------------------------------------------------
# 参数解析：**自己校验，不信任入参**
# ---------------------------------------------------------------------------

class _BadArgs(Exception):
    """参数不合法。**与"检测失败"分开** —— 前者要改的是调用方的请求。"""


def _require_str(args: dict[str, Any], key: str, *, tool: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _BadArgs(f"参数 `{key}` 必须是非空字符串（工具 {tool}）。")
    return value.strip()


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _BadArgs(f"参数 `{key}` 必须是字符串。")
    return value.strip() or None


# ---------------------------------------------------------------------------
# 三个处理函数
# ---------------------------------------------------------------------------

def _fetch_and_decode(core: Any, uri: str) -> tuple[Any, Any] | dict[str, Any]:
    """取件 + 解码。**成功返回 `(fetched, decoded)`，失败返回一条现成的失败结果。**

    ⚠️ 抽出来是因为 `video_probe` 与 `video_sample_table` 的这两步一字不差，
    而"同一件事写两处必然漂移"是本项目的老账（两条鉴伪链已经在
    "_read_inbox 抄丢 inbox.load 的 OSError 包装"上吃过一次）。
    失败结果的**措辞只有这一处定义**。

    ⚠️ 两段 try 的分工与音频侧一致：取件失败走**分类码**（`fix_for`），
    字节族失败走**关键词**（`fix_for_input`）—— 两族不共用一个判定入口。

    ⚠️ 关于 `(ValueError, OSError)` 这一支：`decode_video` 被写成了
    **从不抛异常**（任何畸形字节都折成 `facts.notes`，由
    `verify_video.py` 第 8 节钉住这个前提）。所以这一支覆盖的是
    **取件层的裸 `OSError`**（收件箱落点在本进程不知情的时刻被清掉一类），
    它是"工具入口一个异常都不许漏出去"这条纪律的**兜底**，
    而不是某条已知必然可达的路径 —— 这里如实写成兜底。
    """
    try:
        fetched = core.load(uri)
    except core.VideoFetchRefused as exc:
        return _fail(str(exc), core.fix_for(exc.kind))
    except (ValueError, OSError) as exc:
        return _fail(f"这份字节取不出来：{exc}")
    try:
        decoded = core.decode_video(fetched.data)
    except (ValueError, OSError) as exc:  # 兜底：见 docstring
        return _fail(f"这份字节读不出视频结构：{exc}")
    return fetched, decoded


def _non_video_result(core: Any, decoded: Any, *, tool: str) -> dict[str, Any] | None:
    """字节不是视频容器时，给"你给错了东西"的失败；是视频就返回 `None`。

    ⚠️ 三个工具的真前提都是"输入是视频"，不满足时要说的是**送进来的东西不对**，
    而不是"我们分析不了"。这条与 `video_forensics_analyze` 的处置同源
    （那边由 `forensics.analyze()` 抛 `ValueError` ⇒ `outcome()` 折成
    `refused` + 字节族建议）。

    ⚠️ **判据与那句话都必须从检测层取**（`core.is_supported_video` /
    `core.non_video_clause`），本函数一行判定都不许自己写。两个理由：

      1. 2026-10-04 实测的真缺陷：这里原先是**手抄**的前缀测试
         （`startswith("video/")`），于是 `application/ogg` —— 解码层认得它
         （`_OTHER_CONTAINERS` 里有中文名、`sniff_media_type` 有专门分支）、
         `identify_only_note` 的 docstring 还把它列为适用对象 ——
         被自家工具拒了，理由是一句假话："不是可识别的视频容器"。
      2. 这句话原先也是手抄的第二份（检测层一份、这里一份），而当时的判据
         只比对「出路文案」不比「原因」⇒ 改一处、另一处不动照样全绿。

    出路文案**复用** `pipeline.fix_for_input` —— 在这里另写一句就会与 ACPs
    那条路各自演化。
    """
    media_type = str(decoded.media_type)
    if core.is_supported_video(media_type):
        return None
    reason = core.non_video_clause(media_type)
    return _fail(
        f"传给 `video_uri` 的字节{reason} —— 工具 `{tool}` 只能对视频算。",
        core.fix_for_input(reason),
    )


def _handle_probe(args: dict[str, Any]) -> dict[str, Any]:
    uri = _require_str(args, "video_uri", tool="video_probe")
    declared = _optional_str(args, "declared_mime")
    try:
        core = _video_module()
    except ImportError:
        return _missing_dep()
    got = _fetch_and_decode(core, uri)
    if isinstance(got, dict):
        return got
    fetched, decoded = got
    not_video = _non_video_result(core, decoded, tool="video_probe")
    if not_video is not None:
        return not_video

    facts = decoded.facts
    payload = {
        "media_type": decoded.media_type,
        "byte_size": decoded.byte_size,
        "sha256": core.sha256_of(fetched.data),
        "fetched_from": fetched.source,
        "container": {
            "name": facts.container,
            "brand": facts.brand,
            "declared_size": facts.declared_size,
            "truncated": facts.truncated,
            "appended_bytes": facts.appended_bytes,
            "duration_seconds": facts.duration_seconds,
            "top_level_boxes": list(facts.top_level_boxes),
            "moov_before_mdat": facts.moov_before_mdat,
            "mdat_count": facts.mdat_count,
            "fragmented": facts.fragmented,
            "parse_depth": facts.parse_depth,
        },
        "tracks": [
            {
                "handler": t.handler,
                "codec": t.codec,
                "timescale": t.timescale,
                "duration_seconds": t.duration_seconds,
                "sample_count": t.sample_count,
                "width": t.coded_width,
                "height": t.coded_height,
            }
            for t in facts.tracks
        ],
        # ⚠️ 字段名刻意**不叫** `frames_available`：本工具只数"容器里零解码能取到的帧"，
        #    而动画 GIF 的帧要光栅化（那是分析步骤）。叫 `frames_available` 会让
        #    一份动画 GIF 被读成"帧层跑不了"，而它其实跑得了 —— 那正是本项目
        #    「判据的说法与出口文案不对应」的老毛病。
        "container_frames": len(decoded.frames),
        "frame_source": decoded.frame_source,
        "container_frames_note": decoded.frames_reason or decoded.window_note,
        "declared_mime": declared,
    }
    human = (
        f"**识别结果**：`{decoded.media_type}`（{facts.container}），"
        f"{decoded.byte_size} 字节，SHA-256 `{payload['sha256']}`。\n"
        f"- 来源：`{fetched.source}`（本服务不出网、不读本机文件）\n"
        f"- 解析深度：`{facts.parse_depth}`\n"
        + (f"- 声明品牌：`{facts.brand}`\n" if facts.brand else "")
        + f"- 轨道：{len(facts.tracks)} 条"
        + (
            "（" + "、".join(f"{t.handler}/{t.codec or '?'}" for t in facts.tracks) + "）"
            if facts.tracks else ""
        )
        + "\n"
        f"- 时长（声明）：{facts.duration_seconds}\n"
        f"- 容器**零解码可取到的帧**：**{len(decoded.frames)}**"
        + (f"（来源 `{decoded.frame_source}`）" if decoded.frames else "")
        + "\n"
        + (
            f"- 没有取到帧的原因：{decoded.frames_reason or decoded.window_note}\n"
            if not decoded.frames else ""
        )
        + "\n⚠️ 「0 帧」**不等于**「帧层跑不了」：动画 GIF 的帧要经逐帧光栅化，"
          "那一步在 `video_forensics_analyze` 里做。\n"
          "⚠️ 本工具**不给结论**，只说「这份字节能看哪一层」。"
          "要线索请调 `video_forensics_analyze`；要声明数字请调 `video_sample_table`。"
    )
    return _ok([_text_part(human)], payload)


def _handle_analyze(args: dict[str, Any]) -> dict[str, Any]:
    uri = _require_str(args, "video_uri", tool="video_forensics_analyze")
    declared = _optional_str(args, "declared_mime")
    note = _optional_str(args, "context_note")
    try:
        core = _video_module()
    except ImportError:
        return _missing_dep()
    out = core.outcome(uri, declared_mime=declared, context_note=note)

    if out.status != "ok" or out.result is None:
        # `outcome()` 已经保证"永远结构化返回"，所以这里拿到的一定是原因 + 出路。
        return _fail(out.refusal_reason or "未产出报告。", out.how_to_fix)

    result = out.result
    report = result.report
    lines = [
        f"### 综合线索等级：{report.suspicion}",
        "",
        result.summary_for_leader,
        "",
        f"- 真实容器（按字节判定）：`{report.media_type}`"
        + (f"（{report.container}）" if report.container else ""),
        f"- SHA-256：`{report.sha256}`",
        f"- 帧层可用：{'是' if report.frames_available else '否'} —— {report.frames_note}",
        f"- 本次实际分析帧数：**{report.frames_analyzed}**"
        + (
            f"（来源 `{report.frame_source}`"
            + (
                f"；⚠️ 这是**抽样**，总声明帧数 {report.frame_count}，报告只覆盖这几帧"
                if report.frame_count else ""
            )
            + "）"
            if report.frames_available else ""
        ),
        f"- 字节来源：`{result.fetched_from}`",
        f"- 引用（原文照抄，便于复核）：`{result.source_uri}`",
        # ⚠️ `context_note` **必须出现在这里**。schema 里对它承诺的是"仅原样回显"，
        #    而 `report` 里没有这个字段 —— 只 dump `report` 的话，调用方会
        #    收下一句承诺却看不到任何回显。**收下了却看不见，与收下却不用一样，
        #    都是向调用方撒谎。**
        f"- 案情说明（**原样回显，不参与任何判定**）："
        f"{result.context_note if result.context_note else '（未提供）'}",
        "",
        "#### 逐项发现",
        "",
    ]
    for item in report.findings:
        lines.append(
            f"- **{item.check}** — {item.verdict}\n"
            f"  - 依据：{item.detail}\n"
            f"  - 它不能证明什么：{item.caveat}"
        )
    if report.chain_of_custody:
        lines += ["", "#### 证据保全提示", ""] + [f"- {x}" for x in report.chain_of_custody]
    if report.upgrades:
        lines += ["", "#### 复检路径（按成本递增）", ""]
        for path in report.upgrades:
            lines.append(
                f"- **{path.name}**（{path.cost}，{path.turnaround}）"
                f"—— 适用：{path.when_to_use}"
            )
    lines += ["", f"> {report.disclaimer}"]
    return _ok([_text_part("\n".join(lines))], json.loads(report.model_dump_json()))


def _handle_sample_table(args: dict[str, Any]) -> dict[str, Any]:
    uri = _require_str(args, "video_uri", tool="video_sample_table")
    try:
        core = _video_module()
    except ImportError:
        return _missing_dep()
    got = _fetch_and_decode(core, uri)
    if isinstance(got, dict):
        return got
    _fetched, decoded = got
    not_video = _non_video_result(core, decoded, tool="video_sample_table")
    if not_video is not None:
        return not_video

    facts = decoded.facts
    payload = {
        "media_type": decoded.media_type,
        "container": facts.container,
        "parse_depth": facts.parse_depth,
        "declared_size": facts.declared_size,
        "actual_size": facts.actual_size,
        "truncated": facts.truncated,
        "appended_bytes": facts.appended_bytes,
        "top_level_boxes": list(facts.top_level_boxes),
        "moov_before_mdat": facts.moov_before_mdat,
        "mdat_count": facts.mdat_count,
        "mdat_ranges": [list(r) for r in facts.mdat_ranges],
        "free_bytes": facts.free_bytes,
        "fragmented": facts.fragmented,
        "has_mvex": facts.has_mvex,
        # AVI 专有（其它容器为 None）
        "declared_frame_count": facts.declared_frame_count,
        "actual_frame_chunks": facts.actual_frame_chunks,
        "has_index": facts.has_index,
        # Matroska 专有
        "muxing_app": facts.muxing_app,
        "writing_app": facts.writing_app,
        "cluster_count": facts.cluster_count,
        # 元数据（**自报**，零成本可改写）
        "creation_time": facts.creation_time,
        "modification_time": facts.modification_time,
        "encoder_hint": facts.encoder_hint,
        "tags": list(facts.tags),
        "tracks": [
            {
                "handler": t.handler,
                "codec": t.codec,
                "timescale": t.timescale,
                "duration_seconds": t.duration_seconds,
                "track_header_duration_seconds": t.track_header_duration_seconds,
                "sample_count": t.sample_count,
                "coded_width": t.coded_width,
                "coded_height": t.coded_height,
                "display_width": t.display_width,
                "display_height": t.display_height,
                # `stts`：采样间隔的**分布**（不是全表 —— 可以有上百万项）
                "delta_kinds": list(t.delta_kinds),
                "delta_sample_total": t.delta_sample_total,
                "delta_min": t.delta_min,
                "delta_max": t.delta_max,
                # `stsz` / `stco` 的聚合量
                "sample_size_constant": t.sample_size_constant,
                "sample_size_max": t.sample_size_max,
                "chunk_count": t.chunk_count,
                "chunk_offset_min": t.chunk_offset_min,
                "chunk_offset_max": t.chunk_offset_max,
                # `edts/elst`：`(段时长秒, 媒体起点秒, 速率)`
                "edit_list": [list(e) for e in t.edit_list],
                "framing": t.framing,
            }
            for t in facts.tracks
        ],
        "notes": list(facts.notes),
    }
    human = (
        "### 逐轨声明数字（**容器自己写的，不是实测的**）\n\n"
        f"- 真实容器（按字节判定）：`{decoded.media_type}`（{facts.container}）\n"
        f"- 解析深度：`{facts.parse_depth}`"
        + (
            "（⚠️ 这类容器本进程**没有写解析器** ⇒ 盒结构 / 时间线 / 样本表本次没有检查）"
            if facts.parse_depth != "full" else ""
        )
        + "\n"
        f"- 容器声明长度：{facts.declared_size} / 实际字节数：{facts.actual_size}"
        + ("　⚠️ **声明 > 实际 ⇒ 被截断**" if facts.truncated else "")
        + "\n"
        f"- 声明范围之外的多余字节：{facts.appended_bytes}\n"
        f"- `moov` 在 `mdat` 之前：{facts.moov_before_mdat}"
        "（false 本身不是缺陷，只说明这份文件被写/被改过一遍）\n"
        f"- `mdat` 段数：{facts.mdat_count}\n"
        f"- 轨道：{len(facts.tracks)} 条\n"
    )
    for index, track in enumerate(facts.tracks):
        human += (
            f"\n#### 轨道 {index} · {track.handler}"
            + (f"/{track.codec}" if track.codec else "")
            + "\n"
            f"- 时间刻度 {track.timescale}；`mdhd` 声明时长 {track.duration_seconds} 秒；"
            f"`tkhd` 声明时长 {track.track_header_duration_seconds} 秒\n"
            f"- 样本数 {track.sample_count}；`stts` 间隔种类 "
            f"{len(track.delta_kinds)} 种，最小 {track.delta_min} / 最大 {track.delta_max}"
            f"（总计 {track.delta_sample_total} 个样本）\n"
            f"- 样本尺寸：恒定 {track.sample_size_constant} / 最大 {track.sample_size_max}\n"
            f"- chunk：{track.chunk_count} 个，偏移 {track.chunk_offset_min} … "
            f"{track.chunk_offset_max}\n"
            f"- 编码尺寸 {track.coded_width}×{track.coded_height}；"
            f"展示尺寸 {track.display_width}×{track.display_height}\n"
            f"- 编辑列表：{len(track.edit_list)} 条"
            + ("（**有跳段 / 变速**）" if track.edit_list else "（无）")
            + "\n"
        )
    human += (
        "\n⚠️ 以上全部是**容器里的声明值**：改写它们的成本是零。"
        "本工具**只摊开数字、不下结论** —— "
        "「声明之间对不上」才是线索，而判断那件事的是 `video_forensics_analyze`。\n"
        "⚠️ 本工具**不解码任何帧**，所以它给出的东西**不能用来说明画面里有没有改动**。"
    )
    return _ok([_text_part(human)], payload)


#: 工具名 → 处理函数。**与 `TOOLS` 必须一一对应**（`verify_video_mcp.py` 断言）。
_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "video_probe": _handle_probe,
    "video_forensics_analyze": _handle_analyze,
    "video_sample_table": _handle_sample_table,
}


def call_tool(name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """按名字执行工具。返回 MCP 的 `CallToolResult` 形状（**永不抛业务异常**）。

    三种非法输入各有明确去向（与音频侧逐条相同）：

        未知工具名    → `isError=true` + 列出可用工具（**不是** JSON-RPC -32602）
        参数不是对象  → 同上
        参数不合法    → 同上，并点名是哪个参数
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return _fail(
            f"没有名为 `{name}` 的工具。可用工具："
            + "、".join(f"`{t['name']}`" for t in TOOLS)
            + "。"
        )
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _fail(f"`arguments` 必须是 JSON 对象，收到的是 {type(args).__name__}。")
    try:
        return handler(args)
    except _BadArgs as exc:
        return _fail(str(exc), "请按该工具 `inputSchema` 的描述修正参数后重试。")
    except Exception as exc:  # noqa: BLE001 - 边界层：必须把内部异常也翻成人话
        # ⚠️ 刻意捕获所有异常：MCP 客户端拿到一个未捕获的堆栈时，
        #    最可能的反应是重试或换个工具，而**不是**去看服务端日志。
        return _fail(
            f"工具 `{name}` 执行时发生内部错误：{type(exc).__name__}: {exc}",
            "这是本服务的缺陷，请把这条消息（含工具名与参数）反馈给维护者。",
        )


__all__ = [
    "INSTRUCTIONS",
    "SERVER_NAME",
    "SERVER_VERSION",
    "TOOLS",
    "USAGE",
    "call_tool",
]
