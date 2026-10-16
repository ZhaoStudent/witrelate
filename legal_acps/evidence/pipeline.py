"""把「取图」与「离线检测」接起来 —— 并**把措辞也交给程序**。

分层的原因不是好看，是为了守住两条已经写进契约的规矩：

  * `forensics.analyze()` **不联网**（入参只有字节）⇒ 它可以完全离线断言；
  * `fetch.load()` **不做任何检测** ⇒ 取图策略可以独立断言。
    两者拼在一起才是"看一眼这张图"，而拼接点只有这一个文件。

`summary_for_leader` 由**固定模板**拼出，不是让模型复述检测结论。
理由与「轮次号必须由服务端账本决定」完全同源：**数字与结论不能经手被评价的那一方。**
模型可以解释、可以组织语言，但"偏移 4 像素"这种量值必须原样来自程序。
"""

from __future__ import annotations

from .contracts import (
    ImageEvidenceOutcome,
    ImageEvidenceResult,
    ImageForensicsReport,
)
from .fetch import FetchRefused, load
from .forensics import GATE_ABSTAIN_PREFIX, analyze

#: 触发项的中文名（给摘要用；没登记的项直接用英文 id，宁可难看也不要编一个不准的名字）
CHECK_LABELS = {
    "container": "容器一致性",
    "exif": "元数据",
    "quantization": "JPEG 量化表",
    "ela": "重压缩差异分布",
    "noise_residual": "噪声层一致性",
    "block_grid": "JPEG 块网格相位",
    "resampling": "重采样痕迹",
    "flat_noise": "平坦区噪声",
    "copy_move": "复制-移动",
}


def summary_line(report: ImageForensicsReport) -> str:
    """给主控的一句话摘要（**程序生成**，主控可直接引用）。

    ⚠️ 这条文案是**最容易被原样转述给用户**的一行，所以它必须与 `summarize()` 的
    判据完全同源 —— 否则就会出现"报告内部的结论是「无法判定」，
    而给主控的那句话读起来是「没问题」"这种自相矛盾的交付。

    实测踩到的原形态：一张 92% 纯色的截图（`ela` / `noise_residual` / `copy_move`
    **三项整项没跑**），本函数输出
    「综合线索等级：无法判定。4 项可执行检查均未提示异常。」
    —— 前半句说测不了，后半句说没问题。`summarize()` 那条规则是在报告内部生效的，
    而这一行**没有跟着改**，于是最危险的那句话恰恰漏在外面。
    教训：改判据时要顺着**所有出口**走一遍，特别是"照抄给下游"的那一个。
    """
    fired = [f for f in report.findings if f.verdict == "提示异常"]
    usable = [f for f in report.findings if f.verdict != "无法判定"]
    gated = [
        f.check for f in report.findings
        if f.verdict == "无法判定" and f.detail.startswith(GATE_ABSTAIN_PREFIX)
    ]
    if fired:
        names = "、".join(CHECK_LABELS.get(f.check, f.check) for f in fired)
        head = f"综合线索等级：{report.suspicion}。有 {len(fired)} 项提示异常（{names}）。"
    elif gated:
        # 不得出现在「无法判定」后面接「均未提示异常」——那读起来就是"没问题"。
        head = (
            f"综合线索等级：无法判定。本图不满足像素域检查的前提，"
            f"{len(gated)} 项（{'、'.join(CHECK_LABELS.get(c, c) for c in gated)}）"
            "**整项没有执行**；其余项目只覆盖封装层，**不能据此认为「没有问题」**。"
        )
    elif report.suspicion == "无法判定" or not usable:
        head = "综合线索等级：无法判定。本次没有任何一项检查能给出有效结论。"
    else:
        head = (f"综合线索等级：{report.suspicion}。"
                f"{len(usable)} 项可执行检查均未提示异常。")
    tail = (
        "**这既不是「伪造认定」也不是「真实性认定」**；"
        f"若涉及法律责任，建议走复检路径（已给出 {len(report.upgrades)} 条，按成本递增）。"
    )
    return head + tail


def inspect(
    uri: str,
    *,
    declared_mime: str | None = None,
    context_note: str | None = None,
) -> ImageEvidenceResult:
    """取图 → 离线初检 → 拼摘要。取图被策略拒绝时抛 `FetchRefused`（调用方须如实转述）。

    ⚠️ `context_note` 只是**原样带回**给调用方组织语言用（`ImageEvidenceResult` 里回显），
    **不参与任何判定**。初版把它收下了却没有往下传 —— 调用方以为"案情说明"会被用上，
    实际在工具边界就被丢掉了；一个**收下却不用的参数**比没有这个参数更坏。
    """
    fetched = load(uri)
    report = analyze(fetched.data, declared_mime=declared_mime or fetched.declared_mime)
    return ImageEvidenceResult(
        source_uri=uri,
        fetched_from=fetched.source,
        context_note=context_note,
        report=report,
        summary_for_leader=summary_line(report),
    )


#: 拒绝原因 → 可执行的补救建议。**拒绝必须附带出路**，否则调用方只会想办法绕开边界。
#: 匹配规则是「第一条命中的关键词胜出」，所以**越具体的原因要排得越前**。
_FIXES: tuple[tuple[str, str], ...] = (
    ("收件箱", "这个 inbox: 句柄在本进程里没有登记过 —— 请确认它是本次任务附带的图片"
               "（可直接引用系统给出的句柄原文），不要自己拼一个哈希。"),
    ("未配置白名单",
     "请让管理员设置 EVIDENCE_IMAGE_ALLOWED_HOSTS=<逗号分隔的主机名>，"
     "或改传 data:image/...;base64,... 形式的自带字节。"),
    ("不在白名单内", "换用白名单内的图床，或把图片以 data: URI 直接内联传入。"),
    ("明文 http", "改用 https 地址。"),
    ("本地文件", "把图片作为任务附件提交（走 inbox 句柄）；"
                 "如需在本机自测中读本地文件，须在启动入口显式开启。"),
    ("重定向", "请直接给出最终图片地址（本入口刻意不跟随重定向，以免白名单被绕开）。"),
    ("载荷过大", "先把图片降到 8MB 以内再传（**不做截断**：截断会让所有统计失真）。"),
    ("超过", "先把图片降到 8MB 以内再传（**不做截断**：截断会让所有统计失真）。"),
    ("base64", "检查 data: URI 的 base64 载荷是否完整。"),
    ("解码后为空", "data: URI 的 base64 载荷是空的 —— 请确认这个 URI 没有被截断。"),
    ("像素数过大", "请先**等比缩小**到 1 亿像素以内再提交（解压超大图会占满服务器内存，"
                   "连带影响其它角色的可用性）。"),
    # ⚠️ 下面这条是**调用方自己的错**，绝不能兜底成"请联系管理员" ——
    #    那会把一个 5 秒钟就能改对的问题，变成一次没人能处理的上报。
    ("不是可识别的图像容器",
     "传给 image_uri 的字节不是图片：请确认给的是**图片文件本身**的字节，"
     "而不是网页、PDF、或一段文字。"),
    ("不是图像", "data: URI 里声明的类型不是 image/*：请把类型段改对（如 image/jpeg），或留空由本入口按字节判定。"),
)

#: 两类失败的兜底建议。**兜底也要分清是谁的责任** —— 见上面最后一条的注释。
_DEFAULT_FIX_POLICY = "请联系管理员确认取图策略。"
_DEFAULT_FIX_INPUT = (
    "请确认提交的是**完整、未截断的原始图片字节**；若这张图本身已损坏，"
    "请改用能正常打开的原始文件重新提交。"
)


def _refused(reason: str, default_fix: str) -> ImageEvidenceOutcome:
    """把一次失败折成结构化返回（并挑一条对症的补救建议）。"""
    fix = next((f for key, f in _FIXES if key in reason), default_fix)
    return ImageEvidenceOutcome(status="refused", refusal_reason=reason, how_to_fix=fix)


def outcome(
    uri: str,
    *,
    declared_mime: str | None = None,
    context_note: str | None = None,
) -> ImageEvidenceOutcome:
    """工具层的入口：**永远返回结构化的结果**，失败也走正常返回。

    这样模型能读到"没看成，因为 X，可以这样补救 Y"，
    而不是收到一句不透明的异常 —— 后者最可能的后果是它自己另找路子把图弄进来。

    ⚠️ **"永远"这两个字是补过一次的**：初版只捕获了 `FetchRefused`，
    于是"字节不是图像"（`analyze` 抛 `ValueError`）和"图像被截断"
    （Pillow 抛 `OSError: Truncated File Read`）会**穿透过去**，调用方拿到的
    正是本函数声称要避免的那种不透明异常。实测四种畸形输入全部穿透（已补断言）。
    捕获得**只覆盖"输入不合法"这两族**，不用 `except Exception`：
    我们自己代码里的 `TypeError` / `KeyError` 必须继续炸出来，
    否则这一层就从"边界"变成了"掩盖 bug 的毯子"。
    """
    try:
        result = inspect(uri, declared_mime=declared_mime, context_note=context_note)
    except FetchRefused as exc:
        return _refused(str(exc), _DEFAULT_FIX_POLICY)
    except (ValueError, OSError) as exc:
        return _refused(f"无法对这张图完成初检：{exc}", _DEFAULT_FIX_INPUT)
    return ImageEvidenceOutcome(status="ok", result=result)


__all__ = ["CHECK_LABELS", "inspect", "outcome", "summary_line"]
