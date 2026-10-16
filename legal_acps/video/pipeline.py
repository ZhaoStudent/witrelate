"""把「取视频」与「离线检测」接起来 —— 并**把措辞也交给程序**。

分层理由与音频链 `audio/pipeline.py`、图像链 `evidence/pipeline.py` 逐条同源：

  * `forensics.analyze()` **不联网**（入参只有字节）⇒ 可以完全离线断言；
  * `fetch.load()` **不做任何检测** ⇒ 取件策略可以独立断言。
    两者拼在一起才是「看一段视频」，而拼接点只有这一个文件。

`summary_for_leader` 由**固定模板**拼出，不是让模型复述检测结论。
理由与「轮次号必须由服务端账本决定」完全同源：**数字与结论不能经手被评价的那一方。**

════════════════════════════════════════════════════════════════════════
`summary_line` 是**唯一容易被原样转述给用户的一行**
════════════════════════════════════════════════════════════════════════

图像链在这里踩过一次，音频链踩过两次，三次都是同一个形态：**判据的说法与出口文案不对应**。

    图像链当时：报告内部判「无法判定」，而 `summary_line` 说
                「N 项可执行检查均未提示异常」—— 前半句说测不了、后半句说没问题。
    音频链第一次：所有逐项弃权都打同一个闸门前缀 ⇒ 一段正常录音也报「无法判定」。
    音频链第二次：闸门收紧之后，等级照常给出了，但 `summary_line` 若只说
                「N 项均未提示异常」，读者就不知道**有几项其实没参与判级**。

视频链**照抄音频链的三支互斥出口**（不重新发明），因为它的处境只会更明显：
H.264 / H.265 一律解不出帧 ⇒「帧层整层没跑」是**常态**而不是例外，
而帧层恰恰是唯一能直接看到画面改动的那一层。所以第二支（有闸门、无触发）
在本链上会被**非常频繁地**用到，而它绝不能出现「均未提示异常」。

三支由 `report` 里的同一批 `findings` 推出来 —— **不重新实现判据，只读它的结论。**
"""

from __future__ import annotations

from .contracts import (
    VideoEvidenceOutcome,
    VideoEvidenceResult,
    VideoForensicsReport,
)
from .fetch import VideoFetchRefused, load
# ⚠️ 刻意 import 的是**子模块本身**：下面 `_non_video_keyword()` 要在**每次调用时**
#    去读 `_forensics.NON_VIDEO_CLAUSE_PREFIX`，而不是在 import 期绑成字符串。
#    理由见那个函数（与音频链同一条）。
from . import forensics as _forensics
from .forensics import (
    GATE_ABSTAIN_PREFIX,
    NOT_APPLICABLE_PREFIX,
    analyze,
)
from ..verdict import ABNORMAL, ABSTAIN

#: 检查项的中文名（给摘要用）。
#: ⚠️ 没登记的项**直接用英文 id**，宁可难看也不要编一个不准的名字 ——
#: 一个「看起来像人话但不准确」的译名，会让读者把线索理解成另一件事。
CHECK_LABELS = {
    "container": "容器声明一致性",
    "structure": "盒树 / 分段布局",
    "timeline": "三处时长自洽性",
    "samples": "样本表自洽性与偏移越界",
    "edits": "编辑列表（跳段 / 变速）",
    "metadata": "时间戳与自报编码工具",
    "frames": "抽样帧（接图像鉴伪链）",
}


def _label(check: str) -> str:
    return CHECK_LABELS.get(check, check)


def summary_line(report: VideoForensicsReport) -> str:
    """给调用方的一句话摘要（**程序生成**，可直接引用）。

    ⚠️ 这条文案必须与 `summarize()` 的判据**同源**：它是整份报告里
    最容易被原样抄进对话、抄进笔录的一行。

    三种情形互斥，判定顺序与 `verdict.summarize_findings()` 一一对应：

      ① 有触发项 → 报等级 + 点名触发项（**触发本身就是证据**，
         不因为同时有「测不了」的项就把已看到的异常一笔勾销）
      ② 无触发 + 有材料级闸门 → 无法判定，且**不得**出现「均未提示异常」
      ③ 其余 → 报等级 + 说明有几项没参与判级
    """
    fired = [f for f in report.findings if f.verdict == ABNORMAL]
    usable = [f for f in report.findings if f.verdict != ABSTAIN]
    gated = [
        f.check for f in report.findings
        if f.verdict == ABSTAIN and str(f.detail).startswith(GATE_ABSTAIN_PREFIX)
    ]
    skipped = [
        f.check for f in report.findings
        if f.verdict == ABSTAIN and str(f.detail).startswith(NOT_APPLICABLE_PREFIX)
    ]

    if fired:
        head = (
            f"综合线索等级：{report.suspicion}。"
            f"有 {len(fired)} 项提示异常"
            f"（{'、'.join(_label(f.check) for f in fired)}）。"
        )
        # ⚠️ 「有触发」这一支同样必须交代「哪些项整项没跑」。
        #    实测形态：`moov` 在 `mdat` 之后 ⇒ `structure` 触发（0.5 ⇒ 低），
        #    而帧层整层没跑。报告给出「低」，读者却看不出这个等级是在
        #    **七项里只有六项参与**的情况下算的。等级不是错的，错的是读者对它的信任。
        if gated:
            head += (
                f"另有 {len(gated)} 项（{'、'.join(_label(c) for c in gated)}）"
                "**整项没有执行**（本次取不到任何可分析的帧），"
                "这个等级是在它们缺席的情况下得出的。"
            )
        if skipped:
            head += (
                f"另有 {len(skipped)} 项（{'、'.join(_label(c) for c in skipped)}）"
                "对这段视频无从执行、未参与判级。"
            )
    elif gated:
        # ⚠️ 这一支**绝不能**出现「均未提示异常」—— 那读起来就是「没问题」。
        #    视频链上它会非常频繁（H.264 / H.265 一律解不出帧）。
        head = (
            "综合线索等级：无法判定。本次**取不到任何可分析的帧**，"
            f"{len(gated)} 项（{'、'.join(_label(c) for c in gated)}）"
            "**整项没有执行**；其余项目只覆盖容器结构、时间线自洽与元数据，"
            "**不能据此认为「没有问题」**。"
        )
    elif report.suspicion == ABSTAIN or not usable:
        head = "综合线索等级：无法判定。本次没有任何一项检查能给出有效结论。"
    else:
        head = (
            f"综合线索等级：{report.suspicion}。"
            f"{len(usable)} 项检查均未提示异常。"
        )
        if skipped:
            head += (
                f"但其中 **{len(skipped)} 项对这段视频无从执行、未参与判级**"
                f"（{'、'.join(_label(c) for c in skipped)}），"
                "**这不等于它们也合格。**"
            )

    tail = (
        "**这既不是「篡改认定」也不是「未经剪辑的认定」**；"
        f"若涉及法律责任，建议走复检路径（已给出 {len(report.upgrades)} 条，按成本递增）。"
    )
    return head + tail


def inspect(
    uri: str,
    *,
    declared_mime: str | None = None,
    context_note: str | None = None,
) -> VideoEvidenceResult:
    """取件 → 离线初检 → 拼摘要。取件被策略拒绝时抛 `VideoFetchRefused`。

    ⚠️ `context_note` 只是**原样带回**给调用方组织语言用（`VideoEvidenceResult`
    里回显），**不参与任何判定**。图像链在这里犯过一个很典型的错：
    把参数收下了却没往下传 —— 调用方以为「案情说明」会被用上，实际在工具边界
    就被丢掉了。**一个收下却不用的参数，比没有这个参数更坏**（它向调用方撒谎）。
    """
    fetched = load(uri)
    report = analyze(
        fetched.data,
        declared_mime=declared_mime or fetched.declared_mime,
        name=fetched.name,
    )
    return VideoEvidenceResult(
        source_uri=fetched.source_uri,
        fetched_from=fetched.source,
        context_note=context_note,
        report=report,
        summary_for_leader=summary_line(report),
    )


#: 取件被拒 → 可执行的补救建议。**键是 fetch 的分类码，不是文案关键词。**
#:
#: ⚠️ 图像链是用「在原因文本里找关键词」来配的，音频链自己的初版照抄了那个写法，
#: 结果是**当场漂移**：fetch 里的文案写「没有任何出网通道」，这张表里找的是
#: 「没有出网通道」 —— 匹配不上，于是最要紧的那条拒绝静默降级成兜底建议。
#: 关键词匹配**要求两处文本持续保持一致**，而它们写在不同文件、不同时间。
#: 改成分类码以后，「每个码都有出路」由 `verify_video` 拿 `FETCH_REFUSAL_KINDS`
#: **现场派生**地逐码断言，而不是手抄一份清单。
_FIX_BY_KIND: dict[str, str] = {
    "unsupported_scheme":
        "本能力刻意不取网络地址、也不读本机磁盘。请把视频**作为任务附件发送**"
        "（然后引用系统给出的 inbox:<sha256> 句柄），"
        "或用 data:video/mp4;base64,... 直接内联字节。",
    "data_uri_malformed":
        "data: URI 必须是 data:<类型>;base64,<载荷> 三段齐全的形式 —— "
        "请检查是否漏了逗号后的载荷段。",
    "data_uri_not_base64":
        "请改成 base64 编码：data:video/mp4;base64,...（本入口不接受百分号编码或明文）。",
    "data_uri_mime":
        "把 data: URI 的类型段改成 video/*（如 data:video/mp4;base64,...）；"
        "动画 GIF 写成 image/gif 也接受。留空类型段则由本入口按字节判定。",
    "data_uri_too_large":
        "本能力一次最多处理 64 MiB。**不要为了塞进来而重编码** —— "
        "重编码会重写容器与样本表，正好抹掉本能力要看的那批证据。"
        "可行的出路见 `too_large` 那条。",
    "data_uri_bad_base64":
        "检查 data: URI 的 base64 载荷是否完整（中间不能有换行、空格或缺失的填充符）。"
        "⚠️ 视频动辄几十 MB，**内联 base64 在实践中基本不可用** —— "
        "请改用任务附件（inbox:<sha256> 句柄）。",
    "data_uri_empty":
        "data: URI 的 base64 载荷是空的 —— 请确认这个 URI 没有被截断。",
    "inbox_invalid":
        "这个 inbox: 句柄在本进程里没有登记过 —— 请确认它是**本次任务附带的视频**"
        "（直接引用系统给出的句柄原文），不要自己拼一个哈希。",
    "too_large":
        "本能力一次最多处理 64 MiB。**先别急着压缩或截取**："
        "重编码会重写容器与样本表（正好是本能力要看的东西），"
        "而截取片段会破坏原始性、让之后的司法鉴定失去比对基准。可行的出路："
        "① 用本地工具（MediaInfo / `MP4Box -info` / `ffprobe -show_packets`）"
        "把容器与逐样本时间戳字段导出来自查（复检阶梯第 1 档，免费）；"
        "② 行车记录仪 / 手机通常按固定时长分段保存 —— 提交**需要核查的那一段原始文件**"
        "（不是压缩后的拼合件）；③ 涉及法律责任时直接走声像资料司法鉴定。",
}

#: 解码 / 检测侧失败 → 补救建议。
#:
#: ⚠️ 视频链的这一族**比音频链短得多，而这不是省略**：`decode.py` 被写成了
#: **从不抛异常**（任何畸形字节都折成 `facts.notes` + `frames_note`，
#: 因为「这份字节哪里不像话」本身就是本能力要报告的内容）。
#: 所以整条链上只剩一个抛点 —— `forensics.analyze()` 在字节根本不是视频时
#: 抛的 `ValueError`。**这条路是数出来的，不是猜出来的**（`verify_video` 有一条
#: 「任意畸形字节都不许从 `decode_video` 抛出来」的断言钉住这个前提）。
#:
#: 兜底必须是「责任在调用方」的那一条（见 `_DEFAULT_FIX_INPUT`），
#: 哪怕匹配全落空，给出的建议也仍然是对的。
_FIXES: tuple[tuple[str, str], ...] = (
    # ⚠️ **这张表现在是空的，而这不是漏写。** 上面那段分析说得很清楚：
    #    `decode.py` 从不抛异常 ⇒ 整条链上只剩一个抛点 —— 字节根本不是视频时
    #    `analyze()` 抛的那条。它的原因文本与检测层那句话同源，因此关键词由
    #    `_non_video_keyword()` 在**运行时**取出，不能写成这里的字面量
    #    （写一个就会变成"另一份事实"：改句子之后路由静默降级，而
    #    "字符串相等"的判据照样全绿）。
    #    留一个空表而不是删掉这个符号：`fix_for_input` 的兜底分支要与"表里有条目"
    #    这种常见形态保持同一个形状，且 `_DEFAULT_FIX_INPUT` 那句本来就是对的。
)

#: 「传给 `video_uri` 的字节不是视频」那一条的补救建议。
#: ⚠️ 与音频链那份**刻意分别写**（两条链的建议文案本来就不同：视频这条要提
#: 容器重写的风险）。这里共用的是**关键词**，不是文案。
_NON_VIDEO_INPUT_FIX = (
    "传给 video_uri 的字节不是视频：请确认给的是**视频文件本身**的字节，"
    "而不是网页、PDF、或一段文字。若它是 .rmvb / .wmv / .flv 这类非常见容器，"
    "请先用工具转成 MP4 再提交（⚠️ 转换会重写容器 —— 若这份视频可能用于举证，"
    "请改为提交**原始文件**，并接受本能力在这类容器上只能给容器层线索）。"
)


def _non_video_keyword() -> str:
    """字节族里「给的不是视频」那一条的**路由关键词** —— **现场**从检测层取。

    ⚠️ 与音频链 `audio/pipeline._non_audio_keyword()` 是同一条纪律：
    写成字面量（或在 import 期绑定）会切断与检测层的联系，后果是**路由静默降级**
    —— 调用方为了「你给的不是视频」的问题，收到的却是「请确认提交的是完整、
    未截断的原始视频字节」；而**所有"字符串相等"的判据都是绿的**。
    现场读还有一个直接好处：`verify_video_mcp` 的哨兵判据能同时钉住原因与出路。
    """
    return _forensics.NON_VIDEO_CLAUSE_PREFIX

#: 兜底建议。**兜底也要分清是谁的责任** —— 见上面那条的注释。
#: 取件层的分类码有专门的表，所以这里的兜底只覆盖「字节本身不合法」这一族。
_DEFAULT_FIX_INPUT = (
    "请确认提交的是**完整、未截断的原始视频字节**；"
    "若这个文件本身已损坏，请改用能正常播放的原始文件重新提交。"
)

#: 分类码表的兜底。**它不该被用到** —— 用到就说明有人加了码却没配建议，
#: 而 `verify_video` 会拿 `FETCH_REFUSAL_KINDS` 逐码断言。留在这里是为了
#: 万一漏了，调用方拿到的仍然是「对调用方说的、可执行的一句」，
#: 而不是一句「请联系管理员」。
_DEFAULT_FIX_POLICY = (
    "请改用本能力支持的两种引用之一：任务附件产生的 inbox:<sha256> 句柄，"
    "或 data:video/<类型>;base64,<载荷>。"
)


def fix_for(kind: str) -> str:
    """取件拒绝的分类码 → 补救建议。**抽成函数是为了能被逐码断言。**"""
    return _FIX_BY_KIND.get(kind, _DEFAULT_FIX_POLICY)


def fix_for_input(reason: str) -> str:
    """**字节族**的原因文本 → 补救建议。`_fix_for_input` 的公开别名。

    为什么要公开：`mcp/video_forensics.py` 也会遇到字节族失败（它按名字调
    具体检查项，走不到 `outcome()` 那条路），而「这份字节不是视频」该给调用方
    说哪句话**只能有一处定义** —— 在 MCP 里另写一句，两条路就会各自演化，
    调用方看到两份不同的出路（本项目已经在「识别侧 / 抹除侧的认可集合」上
    吃过这个亏）。
    """
    return _fix_for_input(reason)


def _refused(reason: str, fix: str) -> VideoEvidenceOutcome:
    """把一次失败折成结构化返回。`fix` 已由调用方按族挑好。"""
    return VideoEvidenceOutcome(status="refused", refusal_reason=reason, how_to_fix=fix)


def _fix_for_input(reason: str) -> str:
    """**字节族**（解码 / 检测抛出的 `ValueError` / `OSError`）→ 补救建议。

    ⚠️ 这个函数**只许处理字节族**。2026-10 音频链实测过一次串台：取件族的
    取件族的「data: URI 的 base64 无法解码：…」里含「无法解码」四个字，
    而字节族的表里有一条关键词正是「无法解码」 ⇒ 一条**参数写错**（调用方 5 秒能改对）
    被答成「容器结构本身有问题」（把责任推给了那份文件）。两族的建议表
    关键词互不设防，只要有一个词撞上就会静默错配。
    修法不是「把关键词调得更刁钻」，而是**让两族走不同的判定入口** ——
    取件族走分类码（`fix_for`），字节族才走关键词（本函数）。
    """
    # 「给的不是视频」优先判 —— 关键词与检测层那句话**同源**（见 `_non_video_keyword()`）。
    if _non_video_keyword() in reason:
        return _NON_VIDEO_INPUT_FIX
    return next((f for key, f in _FIXES if key in reason), _DEFAULT_FIX_INPUT)


def outcome(
    uri: str,
    *,
    declared_mime: str | None = None,
    context_note: str | None = None,
) -> VideoEvidenceOutcome:
    """工具层的入口：**永远返回结构化的结果**，失败也走正常返回。

    这样模型能读到「没看成，因为 X，可以这样补救 Y」，而不是收到一句不透明的
    异常 —— 后者最可能的后果是它自己另找路子把视频弄进来。

    ⚠️ **「永远」这两个字在图像链是补过一次的**：初版只捕获了策略拒绝异常，
    于是「字节不是视频」（`analyze` 抛 `ValueError`）与「文件被截断」
    （底层库抛 `OSError`）会**穿透过去**，调用方拿到的正是本函数声称要避免的
    那种不透明异常 —— 实测四种畸形输入全部穿透。

    所以这里从一开始就把**两族的每一条路**都列出来数一遍：

        取件族（`VideoFetchRefused`）  9 个分类码，逐个配专属出路
        字节族（`ValueError`）         `analyze()` 的「不是可识别的视频容器」
        字节族（`OSError`）            底层字节不可读 / 结构异常

    ⚠️ 仍然刻意**不用 `except Exception`**：我们自己代码里的 `TypeError` /
    `KeyError` 必须继续炸出来，否则这一层就从「边界」变成了「掩盖 bug 的毯子」。
    """
    try:
        result = inspect(uri, declared_mime=declared_mime, context_note=context_note)
    except VideoFetchRefused as exc:
        # 分类码 → 建议。**不再对这段文本做关键词匹配**（理由见 `_fix_for_input`）。
        # 万一有人新加了码却忘了配建议，会落到 `_DEFAULT_FIX_POLICY`
        # （对调用方仍然是对的），而 `verify_video` 会先把「漏配」这件事拦下来。
        return _refused(str(exc), fix_for(exc.kind))
    except (ValueError, OSError) as exc:
        reason = f"无法对这段视频完成初检：{exc}"
        return _refused(reason, _fix_for_input(reason))
    return VideoEvidenceOutcome(status="ok", result=result)


__all__ = ["CHECK_LABELS", "fix_for", "fix_for_input", "inspect", "outcome", "summary_line"]
