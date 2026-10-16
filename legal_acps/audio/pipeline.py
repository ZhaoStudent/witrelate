"""把「取音频」与「离线检测」接起来 —— 并**把措辞也交给程序**。

分层理由与图像链 `evidence/pipeline.py` 逐条同源：

  * `forensics.analyze()` **不联网**（入参只有字节）⇒ 可以完全离线断言；
  * `fetch.load()` **不做任何检测** ⇒ 取件策略可以独立断言。
    两者拼在一起才是"听一段录音"，而拼接点只有这一个文件。

`summary_for_leader` 由**固定模板**拼出，不是让模型复述检测结论。
理由与「轮次号必须由服务端账本决定」完全同源：**数字与结论不能经手被评价的那一方。**

════════════════════════════════════════════════════════════════════════
这个文件存在的最大理由：`summary_line` 是**唯一容易被原样转述给用户的一行**
════════════════════════════════════════════════════════════════════════

图像链在这里踩过一次，而且踩的正是本次音频链**已经出现过两次**的那类错：
判据说法与出口文案不对应。

    图像链当时：报告内部判「无法判定」，而 `summary_line` 说
                「N 项可执行检查均未提示异常」—— 前半句说测不了、后半句说没问题。
    音频链第一次：所有逐项弃权都打同一个闸门前缀 ⇒ 一段正常录音也报「无法判定」。
    音频链第二次：闸门收紧之后，等级照常给出了，但 `summary_line` 若只说
                「N 项均未提示异常」，读者就不知道**有几项其实没参与判级**。

所以本文件把出口分成**三种**（而不是图像链的两种），并且三种互斥、由
`report` 里的同一批 `findings` 推出来 —— **不重新实现判据，只读它的结论。**
"""

from __future__ import annotations

from .contracts import (
    AudioEvidenceOutcome,
    AudioEvidenceResult,
    AudioForensicsReport,
)
from .fetch import AudioFetchRefused, load
# ⚠️ 这里刻意 import 的是**子模块本身**（而不只是几个名字）：下面
#    `_non_audio_keyword()` 要在**每次调用时**去读 `_forensics.NON_AUDIO_CLAUSE_PREFIX`，
#    而不是在 import 期把它绑成一个字符串 —— 理由见那个函数。
from . import forensics as _forensics
from .forensics import (
    GATE_ABSTAIN_PREFIX,
    NOT_APPLICABLE_PREFIX,
    analyze,
)
from ..verdict import ABNORMAL, ABSTAIN

#: 检查项的中文名（给摘要用）。
#: ⚠️ 没登记的项**直接用英文 id**，宁可难看也不要编一个不准的名字 ——
#: 一个"看起来像人话但不准确"的译名，会让读者把线索理解成另一件事。
CHECK_LABELS = {
    "container": "容器一致性",
    "codec_params": "编码参数自洽性",
    "metadata": "署名与元数据",
    "spectral_cutoff": "频谱硬截止",
    "noise_floor": "底噪一致性",
    "silence": "数字静音段",
    "spectral_flux": "谱形状突变",
    "sample_discontinuity": "样本级不连续",
    "enf": "电网频率（ENF）相位连续性",
}


def _label(check: str) -> str:
    return CHECK_LABELS.get(check, check)


def summary_line(report: AudioForensicsReport) -> str:
    """给调用方的一句话摘要（**程序生成**，可直接引用）。

    ⚠️ 这条文案必须与 `summarize()` 的判据**同源**：它是整份报告里
    最容易被原样抄进对话、抄进笔录的一行。

    三种情形互斥，判定顺序与 `verdict.summarize_findings()` 一一对应：

      ① 有触发项 → 报等级 + 点名触发项（**触发本身就是证据**，
         不因为同时有"测不了"的项就把已看到的异常一笔勾销）
      ② 无触发 + 有材料级闸门 → 无法判定，且**不得**出现"均未提示异常"
      ③ 其余 → 报等级 + 说明有几项没参与判级（**这一句是本文件新增的**）
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
        # ⚠️ 「有触发」这一支同样必须交代"哪些项整项没跑"。
        #    实测形态：一个声明成 WAV 的 MP3 —— `container` 因"声明与真实容器不符"
        #    触发（0.5 ⇒ 低），而波形域六项全灭。报告给出「低」，读者却看不出
        #    这个等级是在六项缺席的情况下算的。**等级不是错的，错的是读者对它的信任。**
        if gated:
            head += (
                f"另有 {len(gated)} 项（{'、'.join(_label(c) for c in gated)}）"
                "**整项没有执行**（本音轨不满足波形域检查的前提），"
                "这个等级是在它们缺席的情况下得出的。"
            )
        if skipped:
            head += (
                f"另有 {len(skipped)} 项（{'、'.join(_label(c) for c in skipped)}）"
                "对这段音轨无从执行、未参与判级。"
            )
    elif gated:
        # ⚠️ 这一支**绝不能**出现"均未提示异常"—— 那读起来就是"没问题"。
        head = (
            f"综合线索等级：无法判定。本音轨不满足波形域检查的前提，"
            f"{len(gated)} 项（{'、'.join(_label(c) for c in gated)}）"
            "**整项没有执行**；其余项目只覆盖容器与编码参数层，"
            "**不能据此认为「没有问题」**。"
        )
    elif report.suspicion == ABSTAIN or not usable:
        head = f"综合线索等级：无法判定。本次没有任何一项检查能给出有效结论。"
    else:
        head = (
            f"综合线索等级：{report.suspicion}。"
            f"{len(usable)} 项检查均未提示异常。"
        )
        if skipped:
            head += (
                f"但其中 **{len(skipped)} 项对这段音轨无从执行、未参与判级**"
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
) -> AudioEvidenceResult:
    """取件 → 离线初检 → 拼摘要。取件被策略拒绝时抛 `AudioFetchRefused`。

    ⚠️ `context_note` 只是**原样带回**给调用方组织语言用（`AudioEvidenceResult`
    里回显），**不参与任何判定**。图像链在这里犯过一个很典型的错：
    把参数收下了却没往下传 —— 调用方以为"案情说明"会被用上，实际在工具边界
    就被丢掉了。**一个收下却不用的参数，比没有这个参数更坏**（它向调用方撒谎）。
    """
    fetched = load(uri)
    report = analyze(
        fetched.data,
        declared_mime=declared_mime or fetched.declared_mime,
        name=fetched.name,
    )
    return AudioEvidenceResult(
        source_uri=fetched.source_uri,
        fetched_from=fetched.source,
        context_note=context_note,
        report=report,
        summary_for_leader=summary_line(report),
    )


#: 取件被拒 → 可执行的补救建议。**键是 fetch 的分类码，不是文案关键词。**
#:
#: ⚠️ 图像链是用"在原因文本里找关键词"来配的，本模块自己的初版照抄了那个写法，
#: 结果是**当场漂移**：fetch 里的文案写"没有任何出网通道"，这张表里找的是
#: "没有出网通道" —— 匹配不上，于是最要紧的那条拒绝会静默降级成兜底建议。
#: 关键词匹配**要求两处文本持续保持一致**，而它们写在不同文件、不同时间。
#: 改成分类码以后，"每个码都有出路"由 `verify_audio` 逐码断言
#: （拿 `FETCH_REFUSAL_KINDS` 现场派生，而不是手抄一份清单）。
_FIX_BY_KIND: dict[str, str] = {
    "unsupported_scheme":
        "本能力刻意不取网络地址、也不读本机磁盘。请把音频**作为任务附件发送**"
        "（然后引用系统给出的 inbox:<sha256> 句柄），"
        "或用 data:audio/wav;base64,... 直接内联字节。",
    "data_uri_malformed":
        "data: URI 必须是 data:<类型>;base64,<载荷> 三段齐全的形式 —— "
        "请检查是否漏了逗号后的载荷段。",
    "data_uri_not_base64":
        "请改成 base64 编码：data:audio/wav;base64,...（本入口不接受百分号编码或明文）。",
    "data_uri_mime":
        "把 data: URI 的类型段改成 audio/*（如 data:audio/wav;base64,...），"
        "或留空类型段由本入口按字节判定。",
    "data_uri_too_large":
        "先把音频压到 16 MiB 以内，或**只截取需要核查的那一段**再提交"
        "（不做自动截断：截断会让时长、容器长度、尾部标签全部失真）。",
    "data_uri_bad_base64":
        "检查 data: URI 的 base64 载荷是否完整（中间不能有换行、空格或缺失的填充符）。",
    "data_uri_empty":
        "data: URI 的 base64 载荷是空的 —— 请确认这个 URI 没有被截断。",
    "inbox_invalid":
        "这个 inbox: 句柄在本进程里没有登记过 —— 请确认它是**本次任务附带的音频**"
        "（直接引用系统给出的句柄原文），不要自己拼一个哈希。",
    "too_large":
        "先把音频压到 16 MiB 以内，或只截取需要核查的那一段再提交。",
}

#: 解码 / 检测侧失败（`ValueError` / `OSError` 族）→ 补救建议。
#: 这一族的原因文本来自 `decode.py`，无法给分类码，只能用关键词匹配 ——
#: 所以它的**兜底必须是"责任在调用方"的那一条**（见 `_DEFAULT_FIX_INPUT`），
#: 哪怕匹配全落空，给出的建议也仍然是对的。
#:
#: ⚠️ 「不是音频容器」那一条**不在下面这张表里**，它由 `_non_audio_keyword()`
#:    在**运行时**取出（见那个函数）—— 因为它的关键词必须与检测层那句话**同源**。
_FIXES: tuple[tuple[str, str], ...] = (
    ("截断", "这份字节在传输或保存时就已被截断（容器声明的长度大于实际字节数），"
             "请改用**完整**的原始文件重新提交 —— 截断的音频无法给出可靠结论。"),
    ("无法解码", "容器结构本身有问题。请改用能正常播放的原始文件重新提交。"),
)

#: 「传给 `audio_uri` 的字节不是音频」那一条的补救建议。
#: ⚠️ 它与 `_non_audio_keyword()` 是一对：**路由靠后者、文案是前者**。
#: 这一条**必须**与兜底那句话区分开：兜底说"请提交完整未截断的原始音频字节"
#: （假定调用方给的确实是音频），而这条要说的是"你给的根本不是音频"
#: —— 两句话指向的动作完全不同。
_NON_AUDIO_INPUT_FIX = (
    "传给 audio_uri 的字节不是音频：请确认给的是**音频文件本身**的字节，"
    "而不是网页、PDF、或一段文字。若它是 Mac 的 .caf / 微信的 .silk 这类"
    "非常见容器，请先用工具转成 WAV 再提交。"
)


def _non_audio_keyword() -> str:
    """字节族里「给的不是音频」那一条的**路由关键词** —— **现场**从检测层取。

    ⚠️ 为什么不写成模块级字面量、也不在 import 期绑定：那两种写法都会把关键词
    变成"另一份字面量"。后果不是报错，而是**路由静默降级** ——
    改检测层那句话（或改 `NON_AUDIO_CLAUSE_PREFIX`）之后，`_FIXES` 还按老关键词
    匹配，于是"你给的不是音频"被答成兜底那句"请确认提交的是完整、未截断的原始
    音频字节"：调用方照着错误的归因去改，**而所有"字符串相等"的判据都是绿的**。

    现场读模块属性还有一个直接好处：行为化判据立得住 ——
    `verify_audio_mcp` 第 3 节把 `NON_AUDIO_CLAUSE_PREFIX` 换成哨兵之后，
    **原因与出路必须同时跟着变**（手抄一份的实现不会跟，哪怕抄得一字不差）。
    """
    return _forensics.NON_AUDIO_CLAUSE_PREFIX

#: 兜底建议。**兜底也要分清是谁的责任** —— 见上面最后一条的注释。
#: 取件层的分类码有专门的表，所以这里的兜底只覆盖"字节本身不合法"这一族。
_DEFAULT_FIX_INPUT = (
    "请确认提交的是**完整、未截断的原始音频字节**；"
    "若这个文件本身已损坏，请改用能正常播放的原始文件重新提交。"
)

#: 分类码表的兜底。**它不该被用到** —— 用到就说明有人加了码却没配建议，
#: 而 `verify_audio` 会拿 `FETCH_REFUSAL_KINDS` 逐码断言。留在这里是为了
#: 万一漏了，调用方拿到的仍然是"对调用方说的、可执行的一句"，
#: 而不是一句"请联系管理员"。
_DEFAULT_FIX_POLICY = (
    "请改用本能力支持的两种引用之一：任务附件产生的 inbox:<sha256> 句柄，"
    "或 data:audio/<类型>;base64,<载荷>。"
)


def fix_for(kind: str) -> str:
    """取件拒绝的分类码 → 补救建议。**抽成函数是为了能被逐码断言。**"""
    return _FIX_BY_KIND.get(kind, _DEFAULT_FIX_POLICY)


def fix_for_input(reason: str) -> str:
    """**字节族**的原因文本 → 补救建议。`_fix_for_input` 的公开别名。

    为什么要公开：`mcp/audio_forensics.py` 也会遇到字节族失败（它按名字调
    `enf_track`，走不到 `outcome()` 那条路），而"这份字节不是音频"该给调用方
    说哪句话**只能有一处定义** —— 在 MCP 里另写一句，两条路就会各自演化，
    调用方看到两份不同的出路（本项目已经在"识别侧 / 抹除侧的认可集合"上
    吃过这个亏）。
    """
    return _fix_for_input(reason)


def _refused(reason: str, fix: str) -> AudioEvidenceOutcome:
    """把一次失败折成结构化返回。`fix` 已由调用方按族挑好。"""
    return AudioEvidenceOutcome(status="refused", refusal_reason=reason, how_to_fix=fix)


def _fix_for_input(reason: str) -> str:
    """**字节族**（解码 / 检测抛出的 `ValueError` / `OSError`）→ 补救建议。

    ⚠️ 这个函数**只许处理字节族**。2026-10 实测过一次串台：取件族的
    "data: URI 的 base64 无法解码：…"里含"无法解码"四个字，
    而字节族的表里有一条关键词正是"无法解码" ⇒ 一条**参数写错**（调用方 5 秒能改对）
    被答成"容器结构本身有问题"（把责任推给了那份文件）。两族的建议表
    关键词互不设防，只要有一个词撞上就会静默错配。
    修法不是"把关键词调得更刁钻"，而是**让两族走不同的判定入口** ——
    取件族走分类码（`fix_for`），字节族才走关键词（本函数）。
    """
    # 「给的不是音频」优先判 —— 它与检测层那句话**同源**（见 `_non_audio_keyword()`），
    # 而且它必须抢在下面那张表前面：这条的原因文本里不含下面任何关键词，
    # 顺序其实无关紧要，但把它放第一位能让"责任在调用方"的那条永远先被想到。
    if _non_audio_keyword() in reason:
        return _NON_AUDIO_INPUT_FIX
    return next((f for key, f in _FIXES if key in reason), _DEFAULT_FIX_INPUT)


def outcome(
    uri: str,
    *,
    declared_mime: str | None = None,
    context_note: str | None = None,
) -> AudioEvidenceOutcome:
    """工具层的入口：**永远返回结构化的结果**，失败也走正常返回。

    这样模型能读到"没看成，因为 X，可以这样补救 Y"，而不是收到一句不透明的
    异常 —— 后者最可能的后果是它自己另找路子把音频弄进来。

    ⚠️ **"永远"这两个字在图像链是补过一次的**：初版只捕获了策略拒绝异常，
    于是"字节不是音频"（`analyze` 抛 `ValueError`）与"文件被截断"
    （底层库抛 `OSError`）会**穿透过去**，调用方拿到的正是本函数声称要避免的
    那种不透明异常 —— 实测四种畸形输入全部穿透。

    所以这里从一开始就把**两族的每一条路**都列出来数一遍，而不是只捕一条：

        取件族（`AudioFetchRefused`）   9 个分类码，逐个配专属出路
        字节族（`ValueError`）          不是音频容器 / 容器损坏 / 内部不合法
        字节族（`OSError`）             读不出、结构异常

    ⚠️ 仍然刻意**不用 `except Exception`**：我们自己代码里的 `TypeError` /
    `KeyError` 必须继续炸出来，否则这一层就从"边界"变成了"掩盖 bug 的毯子"。
    """
    try:
        result = inspect(uri, declared_mime=declared_mime, context_note=context_note)
    except AudioFetchRefused as exc:
        # 分类码 → 建议。**不再对这段文本做关键词匹配**（理由见 `_fix_for_input`）。
        # 万一有人新加了码却忘了配建议，会落到 `_DEFAULT_FIX_POLICY`
        # （对调用方仍然是对的），而 `verify_audio` 会先把"漏配"这件事拦下来。
        return _refused(str(exc), fix_for(exc.kind))
    except (ValueError, OSError) as exc:
        reason = f"无法对这段音频完成初检：{exc}"
        return _refused(reason, _fix_for_input(reason))
    return AudioEvidenceOutcome(status="ok", result=result)


__all__ = ["CHECK_LABELS", "fix_for", "fix_for_input", "inspect", "outcome", "summary_line"]
