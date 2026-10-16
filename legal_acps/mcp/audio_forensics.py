"""音频鉴伪 MCP 工具的定义与实现 —— **复用 `legal_acps.audio` 的同一套纯函数**。

════════════════════════════════════════════════════════════════════════
为什么工具层这里只做"翻译"，不重做任何判定
════════════════════════════════════════════════════════════════════════

本项目已经因为"同一件事写两处"吃过好几次亏（识别侧与抹除侧的认可集合漂移、
两条鉴伪链对同一种失败给出不同档位）。所以本模块的纪律是：

    工具层**只做三件事**：解析参数 → 调 `legal_acps.audio` → 把结果翻译成
    MCP 能表达的形状（文本 + JSON）。**一行判据都不在这里。**

直接后果：MCP 调出来的结论与 ACPs 链路上 `evidence.analyze_audio` 调出来的
结论**必然逐字相同** —— 因为它们是同一个函数。这一点由
`tools/verify_audio_mcp.py` 直接断言（同一条 uri 两边跑，比对 JSON）。

⚠️ 三个工具都**不出网**，也**不读本机磁盘**：只认 `inbox:<sha256>` 句柄与
`data:audio/...;base64,...`。这条限制继承自 `legal_acps.audio.fetch`，
本模块不去、也不能绕过它（不提供任何"顺手读个本地文件"的开关）。
"""

from __future__ import annotations

import json
from typing import Any, Callable

#: ⚠️ **不在模块级 import `legal_acps.audio`。**
#:
#: 理由：`tools/list` 只用到下面的 schema（纯数据），而 schema 与 numpy 无关。
#: 放在模块级 import，会让"服务器上没装 numpy"直接变成**进程启动失败** ——
#: MCP 客户端看到的是一个"连不上/无响应"的服务器，**没有任何提示**，
#: 而真正的原因（少一个可选依赖）本来一句话就能说清。
#: 所以：schema 常驻，实现按需导入，缺依赖时**在工具结果里如实说**。
_MISSING_DEP_HINT = (
    "本工具需要 `numpy` 与 `legal_acps.audio`（音频检测核心）。"
    "当前进程里导入失败 —— 请确认在该 Python 环境里装了 numpy，"
    "并且是以**项目根目录**为工作目录启动（`legal_acps` 是相对包名）。"
)


def _audio_module() -> Any:
    """按需导入检测核心，返回一个**显式列明依赖面**的命名空间。

    ⚠️ **必须显式 `from legal_acps.audio import <子模块>`，不许走包属性。**
    2026-10 实测踩到：`legal_acps.audio` 是 PEP 562 惰性包，`__getattr__` 只认
    `_LAZY` 表里的名字。`audio.decode_audio` 能用（在表里），`audio.forensics.enf_track`
    也能用 —— 但那**不是因为表里有它**，而是因为别处先导入过 `decode`，
    顺带把 `forensics` 也拉进来了。于是 `audio.fetch.load` 在
    "先调 `audio_probe`"时报 `AttributeError`，在"先调过 `analyze`"时却能跑。
    **一个依赖调用顺序的导入，是本项目最忌讳的那种"看起来正常"。**

    显式导入子模块就没有这个歧义：`from package import submodule` 由导入系统
    保证把子模块取回来，与"谁先跑过"无关。
    """
    from types import SimpleNamespace  # noqa: PLC0415

    from legal_acps.audio import decode as _decode  # noqa: PLC0415
    from legal_acps.audio import fetch as _fetch  # noqa: PLC0415
    from legal_acps.audio import forensics as _forensics  # noqa: PLC0415
    from legal_acps.audio import pipeline as _pipeline  # noqa: PLC0415

    return SimpleNamespace(
        decode_audio=_decode.decode_audio,
        load=_fetch.load,
        AudioFetchRefused=_fetch.AudioFetchRefused,
        enf_track=_forensics.enf_track,
        analyze=_forensics.analyze,
        # 「谁算音频」的判据与「不是音频」那句话，**只从检测层取**。
        # ⚠️ 这不是"顺手再抄一份"：抄一份就是视频侧 Ogg 那件事的复发路径
        #    （MCP 当初抄了前缀测试，于是解码层认得、表里有中文名的
        #    `application/ogg` 被自家工具拒了），也是 B2 本身的病灶
        #    （那句话原先在这里手抄了第二份，而判据只比出路、不比原因）。
        #    见 `_handle_enf_track` 与 `verify_audio_mcp` 第 3 节的哨兵判据。
        is_supported_audio=_forensics.is_supported_audio,
        non_audio_clause=_forensics.non_audio_clause,
        outcome=_pipeline.outcome,
        fix_for=_pipeline.fix_for,
        fix_for_input=_pipeline.fix_for_input,
        sha256_of=_forensics.sha256_of,
    )


# ---------------------------------------------------------------------------
# 工具定义（纯数据）
# ---------------------------------------------------------------------------

_URI_DESCRIPTION = (
    "音频引用。**只接受两种**：① `inbox:<sha256>` —— 随任务附件进来的字节，"
    "句柄是**内容哈希**（64 位小写十六进制），不接受路径；"
    "② `data:audio/wav;base64,...` —— 自带字节。"
    "⚠️ **本服务不出网、也不读本机文件**：https 地址、file://、裸路径一律被拒绝"
    "（拒绝时会说明该怎么改）。"
)

#: 工具的**唯一来源**。`tools/list` 直接吐它，`tools/call` 按 `name` 找它。
#: 加一个工具 = 在这里加一项 + 在 `_HANDLERS` 加一行，两处都由
#: `verify_audio_mcp.py` 断言存在（免得出现"列表里有、调不到"的空工具）。
TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "audio_probe",
        "title": "音频快速探查（只看容器与波形可用性）",
        "description": (
            "**不判定的**第一眼：这段字节到底是什么容器、采样参数是多少、"
            "能不能解出波形（`pcm_available`），以及解不出时的原因。"
            "用途是在跑完整检测之前先确认「这份东西我们能不能看」。"
            "不返回任何异常/正常结论。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "audio_uri": {"type": "string", "description": _URI_DESCRIPTION},
                "declared_mime": {
                    "type": "string",
                    # ⚠️ 这句原先写的是「用于与真实容器比对」，而本处理器**只回显**
                    #    （2026-10-04 体检查出，视频侧同病、一并改）。
                    #    承诺一件没做的事，与"收下了却看不见"同族。真正比对的地方是
                    #    `audio_forensics_analyze` 的容器项，在那里再比一次就是
                    #    "同一判定写两处"。
                    "description": "调用方声明的 MIME（如 audio/wav）。"
                                   "**仅原样回显，不参与任何判定** —— "
                                   "「声明与真实容器是否一致」由 `audio_forensics_analyze` "
                                   "报出；可不填。",
                },
            },
            "required": ["audio_uri"],
            "additionalProperties": False,
        },
    },
    {
        "name": "audio_forensics_analyze",
        "title": "音频编辑痕迹初检（九项，离线）",
        "description": (
            "对一段音频做**离线**编辑痕迹初检，返回九项逐项发现 + 综合线索等级 + "
            "复检阶梯。线索包括：容器自洽性、编码参数（采样率/声道模式跳变）、"
            "署名元数据、频谱硬截止、底噪一致性、数字静音段、谱形状突变、"
            "样本级不连续、电网频率（ENF）相位连续性。"
            "**输出没有「真/假」**：只有线索强度、逐项 caveat 与综合等级"
            "（低/中/高/无法判定）。等级由程序按固定权重算出，不是模型给的。"
            "任一检查项因前提不满足而没跑时，会在「无法判定」里如实出现，"
            "绝不会被写成「没问题」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "audio_uri": {"type": "string", "description": _URI_DESCRIPTION},
                "declared_mime": {
                    "type": "string",
                    "description": "调用方声明的 MIME。与真实容器不符时会作为一条线索报出。",
                },
                "context_note": {
                    "type": "string",
                    "description": "这段录音在案情里的分量（如「据称是通话录音」）。"
                                   "**仅原样回显**，不参与任何判定。",
                },
            },
            "required": ["audio_uri"],
            "additionalProperties": False,
        },
    },
    {
        "name": "audio_enf_track",
        "title": "电网频率（ENF）逐窗轨迹",
        "description": (
            "把 ENF 基频（50 / 60 Hz）的**逐窗轨迹**取出来，供人工核查。"
            "⚠️ 轨迹只有在 `detected=true`（确实检出了市电痕迹）时才有解释力；"
            "`detected=false` 时那些点只是噪声峰的位置，**看不出任何东西**。"
            "本工具不改变任何判定，只是把「结论靠不靠得住」所需的数据摊开。"
            "注意：手机在户外录的音通常测不到市电痕迹，检不出是常态、不是故障。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "audio_uri": {"type": "string", "description": _URI_DESCRIPTION},
                "base_hz": {
                    "type": "number",
                    "description": "要跟踪的基频：50 或 60。不填则按同一套判据自动挑。",
                    "enum": [50, 60],
                },
                "window_seconds": {
                    "type": "number",
                    "description": "分析窗长（秒），默认 1.0。窗越长频率估计越稳、时间分辨率越差。",
                },
            },
            "required": ["audio_uri"],
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
    把"权重 1.0"这类数字复述错。
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

    这是 MCP 的约定，也正好是本项目一贯的做法：调用方（模型）需要读到
    "为什么不行 + 可以怎么改"。抛 JSON-RPC 错误的话，客户端往往只显示
    "tool failed"，那句话对模型没有任何指导作用。
    """
    body = reason if not fix else f"{reason}\n\n**可以这样改**：{fix}"
    return {"content": [_text_part(body)], "isError": True}


def _missing_dep() -> dict[str, Any]:
    return _fail(f"本服务缺少运行依赖：{_MISSING_DEP_HINT}")


# ---------------------------------------------------------------------------
# 参数解析：**自己校验，不信任入参**
# ---------------------------------------------------------------------------

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


class _BadArgs(Exception):
    """参数不合法。**与"检测失败"分开** —— 前者要改的是调用方的请求。"""


#: 电网频率的两个取值。schema 里写的是 `enum: [50, 60]`，**这里必须自己再判一次**。
#:
#: ⚠️ 为什么不靠 schema：JSON Schema 的 `enum` 不是所有客户端都会执行，
#: 而"收下一个 123 Hz"的后果是照它算出一条**没有科学含义**的谐波梳
#: （123/246/369/492 Hz），再如实报出「未检出」—— 调用方读到的结论是
#: "没有市电痕迹"，其实是我们按一个不存在的电网频率找了一通。
#: 本项目对参数的一贯口径就是"自己校验，不信任入参"（`window_seconds` 一直都校验）。
#: ⚠️ 与 `forensics.ENF_BASE_FREQS` 必须一致 —— 那份定义在需要 numpy 的核心模块里，
#: 这里不能 import（见本模块开头的按需导入纪律），所以由
#: `tools/verify_audio_mcp.py` 断言两者相等（"同一件事写两处"必须被钉住）。
_ENF_BASES = (50.0, 60.0)


# ---------------------------------------------------------------------------
# 三个处理函数
# ---------------------------------------------------------------------------

def _handle_probe(args: dict[str, Any]) -> dict[str, Any]:
    uri = _require_str(args, "audio_uri", tool="audio_probe")
    declared = _optional_str(args, "declared_mime")
    # ⚠️ **两段 try**，不是一段。合成一段的话，`except core.AudioFetchRefused`
    #    里的 `core` 在"导入就失败"那条路上是**未绑定名** —— 虽然 Python 的
    #    except 子句按顺序匹配、轮不到它求值，但"能跑"依赖的是这个细节，
    #    而不是结构本身。分两段以后，缺依赖与取件失败各自独立、一眼看得清。
    try:
        core = _audio_module()
    except ImportError:
        return _missing_dep()
    try:
        fetched = core.load(uri)
        decoded = core.decode_audio(fetched.data)
    except core.AudioFetchRefused as exc:
        return _fail(str(exc), core.fix_for(exc.kind))
    except (ValueError, OSError) as exc:
        return _fail(f"这份字节读不出音频结构：{exc}")

    facts = decoded.facts
    payload = {
        "media_type": decoded.media_type,
        "byte_size": decoded.byte_size,
        "sha256": core.sha256_of(fetched.data),
        "fetched_from": fetched.source,
        "container": {
            "sample_rate": facts.sample_rate,
            "channels": facts.channels,
            "bits_per_sample": facts.bits_per_sample,
            "duration_seconds": (
                None if facts.duration_seconds is None else round(facts.duration_seconds, 3)
            ),
            "frame_count": facts.frame_count,
            "truncated": facts.truncated,
            "appended_bytes": facts.appended_bytes,
        },
        "pcm_available": decoded.waveform is not None,
        "pcm_note": decoded.pcm_reason or decoded.window_note,
        "declared_mime": declared,
    }
    human = (
        f"**识别结果**：`{decoded.media_type}`，{decoded.byte_size} 字节，"
        f"SHA-256 `{payload['sha256']}`。\n"
        f"- 来源：`{fetched.source}`（本服务不出网、不读本机文件）\n"
        f"- 波形可用：**{'是' if decoded.waveform is not None else '否'}**"
        + ("" if decoded.waveform is None else f"（{decoded.window_note}）")
        + "\n"
        + ("" if decoded.waveform is not None else f"- 不可用原因：{decoded.pcm_reason}\n")
        + "\n⚠️ 本工具**不给结论**，只说「这份字节能不能看、怎么看」。"
          "要线索请调 `audio_forensics_analyze`。"
    )
    return _ok([_text_part(human)], payload)


def _handle_analyze(args: dict[str, Any]) -> dict[str, Any]:
    uri = _require_str(args, "audio_uri", tool="audio_forensics_analyze")
    declared = _optional_str(args, "declared_mime")
    note = _optional_str(args, "context_note")
    try:
        core = _audio_module()
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
        f"- 真实容器（按字节判定）：`{report.media_type}`",
        f"- SHA-256：`{report.sha256}`",
        f"- 波形可用：{'是' if report.pcm_available else '否'} —— {report.pcm_note}",
        f"- 分析范围：{report.analysis_window}",
        f"- 字节来源：`{result.fetched_from}`",
        f"- 引用（原文照抄，便于复核）：`{result.source_uri}`",
        # ⚠️ `context_note` **必须出现在这里**。schema 里对它承诺的是"仅原样回显"，
        #    而本模块的结果只 dump 了 `report`（report 里没有这个字段）——
        #    于是调用方收下了一句承诺、却看不到任何回显。
        #    本项目对这类事的判据是：**收下了却看不见，与收下却不用一样，
        #    都是向调用方撒谎**。所以要么让它可见，要么把承诺删掉 —— 这里选前者。
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


def _handle_enf_track(args: dict[str, Any]) -> dict[str, Any]:
    uri = _require_str(args, "audio_uri", tool="audio_enf_track")
    # ⚠️ `bool` 是 `int` 的子类 ⇒ `isinstance(True, int)` 为真。不显式挡掉的话
    #    `base_hz=true` 会变成 1.0 Hz、`window_seconds=true` 会变成 1.0 秒 ——
    #    两个都"能跑完"，只是跑的是另一件事。所以布尔值一律按类型错处理。
    raw_base = args.get("base_hz")
    base: float | None = None
    if raw_base is not None:
        if isinstance(raw_base, bool) or not isinstance(raw_base, (int, float)):
            raise _BadArgs("参数 `base_hz` 必须是数字 50 或 60。")
        if float(raw_base) not in _ENF_BASES:
            raise _BadArgs(
                f"参数 `base_hz` 只能是 50 或 60（收到 {raw_base!r}）—— "
                "电网频率只有这两个取值，别的数字算不出任何有意义的东西。"
            )
        base = float(raw_base)
    raw_window = args.get("window_seconds")
    if raw_window is not None and (
        isinstance(raw_window, bool) or not isinstance(raw_window, (int, float))
    ):
        raise _BadArgs("参数 `window_seconds` 必须是数字。")
    window = 1.0 if raw_window is None else float(raw_window)
    if not 0.1 <= window <= 10.0:
        raise _BadArgs("参数 `window_seconds` 必须在 0.1 到 10 秒之间。")

    try:
        core = _audio_module()
    except ImportError:
        return _missing_dep()
    try:
        fetched = core.load(uri)
        decoded = core.decode_audio(fetched.data)
    except core.AudioFetchRefused as exc:
        return _fail(str(exc), core.fix_for(exc.kind))
    except (ValueError, OSError) as exc:
        return _fail(f"这份字节读不出音频结构：{exc}")

    # ⚠️ 两道前置必须**各自出声**，不能都落到 `enf_track(wave=None)` 那一句话上。
    #    那句话写的是"压缩 / 带封装的格式，本进程没有解码器"，它把三种完全不同
    #    的情形**一律归因成"格式不支持"**：
    #      · 这份字节根本不是音频（比如一张 PNG / 一段文字）
    #      · WAV 头坏了（没有 fmt 块 / 被截断）
    #      · 真的是压缩格式、本进程没有解码器
    #    前两种的责任方与补救动作与第三种完全不同：调用方会照着错误的归因去"转格式"，
    #    而真正要改的是他送进来的东西。这与本项目"溯源字段不许含糊"是同一条纪律。
    if not core.is_supported_audio(str(decoded.media_type)):
        # 这一条与 `audio_forensics_analyze` 对非音频的处置**同源**（那边也拒绝）：
        # 工具的真前提是"输入是音频"，不满足时要说的是"你给错了东西"。
        #
        # ⚠️ 判据与那句话**都必须从检测层取**（`core.is_supported_audio` /
        #    `core.non_audio_clause`），本函数一行判定、一个字都不许自己写：
        #    2026-10-05 实测的 B2 —— 这里原先是**手抄**的前缀测试 + **手抄**的整句，
        #    而当时的判据只比对「出路文案」不比「原因」⇒ 改一处、另一处不动照样全绿。
        reason = core.non_audio_clause(str(decoded.media_type))
        return _fail(
            f"传给 `audio_uri` 的字节{reason} —— 本工具只能对音频算 ENF 轨迹。",
            core.fix_for_input(reason),
        )

    if decoded.waveform is None:
        # ⚠️ 这里必须用**解码层给出的真实原因**，不能沿用 `enf_track(wave=None)`
        #    里那句"压缩 / 带封装的格式，本进程没有解码器" —— 它把
        #    「WAV 头坏了（没有 fmt 块）」、「被截断」、「真的是压缩格式」
        #    三种完全不同的情形**一律归因成"格式不支持"**，而三种的补救动作
        #    完全不同：前两种要改的是**送进来的东西**，照着"没有解码器"去改
        #    只会白折腾一遍转格式。溯源字段不许含糊。
        #
        # 为什么这里是 `isError=true`（而不是"成功 + available=false"）：
        #    判据是「调用方**要不要改点什么**」。这份字节根本算不出轨迹、
        #    而且改法明确（换个能解码的输入）⇒ 这是一次失败，就该按失败返回。
        #    对照：波形正常但**检不出市电痕迹** —— 那是 ENF 的常态、
        #    调用方没有任何东西要改 ⇒ 那才是 `isError=false`。
        return _fail(
            f"这份字节没有可用波形，ENF 轨迹无从计算：{decoded.pcm_reason}",
            "本工具需要能解出 PCM 波形的输入（未压缩的 WAV / IEEE float）："
            "压缩格式请先用其它工具转成 WAV 再提交；"
            "WAV 若读不出波形，说明头部本身有问题，请改用能正常播放的原始文件。",
        )

    try:
        track = core.enf_track(decoded.waveform, base=base, window_seconds=window)
    except (ValueError, OSError) as exc:
        return _fail(f"这段波形算不出 ENF 轨迹：{exc}")

    head = (
        f"### ENF 轨迹：{'检出' if track['detected'] else '未检出'}"
        + (f"（基频 {track.get('base_hz')} Hz）" if track.get("base_hz") else "")
        + "\n\n"
        + track["reason"]
        + f"\n\n> {track['note']}"
    )
    return _ok([_text_part(head)], track)


#: 工具名 → 处理函数。**与 `TOOLS` 必须一一对应**（`verify_audio_mcp.py` 断言）。
_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "audio_probe": _handle_probe,
    "audio_forensics_analyze": _handle_analyze,
    "audio_enf_track": _handle_enf_track,
}


def call_tool(name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """按名字执行工具。返回 MCP 的 `CallToolResult` 形状（**永不抛业务异常**）。

    三种非法输入各有明确去向：

        未知工具名    → `isError=true` + 列出可用工具（**不是** JSON-RPC -32602）
        参数不是对象  → 同上
        参数不合法    → 同上，并点名是哪个参数

    为什么不把"未知工具名"做成 JSON-RPC 错误：那是**协议层**的用法错误，
    而 MCP 客户端在处理它时通常只显示一句通用失败。调用方（模型）真正需要
    的信息是"有哪些工具可用" —— 那只有放在 `content` 里才送得到。
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
        #    这与"工具入口永远返回结构化结果"是同一条纪律。
        return _fail(
            f"工具 `{name}` 执行时发生内部错误：{type(exc).__name__}: {exc}",
            "这是本服务的缺陷，请把这条消息（含工具名与参数）反馈给维护者。",
        )


__all__ = ["TOOLS", "call_tool"]
