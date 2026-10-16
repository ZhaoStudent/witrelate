"""视频鉴伪（`evidence-video`）的本地初检能力。

分层（与图像链 `legal_acps/evidence/`、音频链 `legal_acps/audio/` **逐层同构**，
便于三条链各自断言、互不越界）：

  * `contracts`  —— 证据契约（请求 / 报告 / 结果 / 拒绝）。**只依赖 pydantic**，
                    并且**复用**图像侧已经定义的 `Finding` / `SuspicionLevel` /
                    `UpgradePath` —— 这三个概念与模态无关，写第二份必然漂移。
  * `decode`     —— 字节 → 容器事实 + **零解码可取出的帧**。只做事实提取，
                    **不做任何判定**。⚠️ **纯标准库**（见该模块 docstring：
                    容器解析本来就不需要数学库，这让「没装 numpy 也能出容器层结论」
                    成为结构性保证，而不是巧合）。
  * `frames`     —— 帧层：把取到的帧接进**已有的图像鉴伪链**（复用 `evidence`），
                    并做跨帧的比对（同一帧重复出现等）。**需要 Pillow / numpy ⇒ 按需导入。**
  * `forensics`  —— 纯函数检测核心（盒结构 / 时间线 / 样本表 / 编辑列表 / 元数据
                    + 帧层汇总），**不联网**、可离线断言。
  弃权分**两层**：`GATE_ABSTAIN_PREFIX`（材料级：取不到任何可分析的帧
  ⇒ 帧层整层没跑 ⇒ 综合等级「无法判定」）与 `NOT_APPLICABLE_PREFIX`
  （逐项：TS / FLV 这类没写解析器的容器谈不了盒结构 ⇒ 只在 findings 里留一条
  「无法判定」，**不改变综合等级**，但会在综合理由里点名交代）。
  汇总判据本身在 `legal_acps/verdict.py` —— **与图像链、音频链共用同一段逻辑**。
  * `fetch`      —— 取字节入口。⚠️ 与图像侧**刻意不同**：本能力**不出网**
                    （只认 `inbox:` 句柄与内联 base64），出网面为零。
  * `pipeline`   —— 把上面几层接起来，并用**固定模板**拼给调用方的摘要

## ⚠️ 导出是**惰性**的（PEP 562），理由与图像 / 音频包完全相同

`legal_acps/agents.py` 需要 `video.contracts` 里的请求模型来生成工具 schema。
若本文件在模块级 `from .forensics import analyze`，那么**连只需要 pydantic 的
契约**也会顺带把 numpy / Pillow 拉进来 —— 而「服务器上没装这些库时，
9 个已过审 Agent 全部起不来」是不能接受的耦合（加分项不该有这种风险）。

    from legal_acps.video import analyze      # 到这一行才 import forensics

## ⚠️ 本能力**刻意不做**的四件事

1. **不出网**。理由与音频侧同源，且在视频上更强：视频文件动辄上百 MB，
   内联 base64 根本不可用 ⇒ 调用方本来就该走附件通道。
2. **不解码 H.264 / H.265**。没有 ffmpeg / OpenCV，硬做只会给出
   看起来专业、实际不可复现的分数。帧层只做**容器里本来就存着 JPEG/PNG**
   的那几种（MJPEG 逐帧、内嵌封面帧、动画 GIF）。
3. **不做「AI 换脸 / AI 生成」检测**。那需要深度模型与 GPU，与
   「离线可断言」的纪律冲突。
4. **不输出「真 / 假」**。与另两条链同源：只有线索强度 + 逐项 caveat +
   综合等级 + 升级建议。**「无法判定」是合法且经常正确的答案。**

方法学来源（都是公开发表的视频 / 容器取证手段，本项目自行实现，未调用其代码）：
ISO BMFF 盒结构与样本表自洽性、媒体时间线与三处时长比对、编辑列表
（`elst`）语义、容器内自报编码工具、`moov`/`mdat` 布局与分段写入、
AVI 帧数与索引自洽、内嵌封面帧的像素域线索。逐条见 `forensics.py` 的注释。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅供静态检查，运行时不执行
    from .contracts import (
        TrackSummary,
        VideoEvidenceOutcome,
        VideoEvidenceRequest,
        VideoEvidenceResult,
        VideoForensicsReport,
    )
    from .decode import (
        Decoded,
        ExtractedFrame,
        TrackFacts,
        VideoFacts,
        decode_video,
        frames_absent_clause,
        frames_unavailable_text,
        sniff_media_type,
    )
    from .fetch import (
        FETCH_REFUSAL_KINDS,
        MAX_VIDEO_BYTES,
        FetchedVideo,
        VideoFetchRefused,
        check_policy,
        classify_uri,
        load,
    )
    from .forensics import (
        CHAIN_OF_CUSTODY,
        CHECK_WEIGHTS,
        DISCLAIMER,
        GATE_ABSTAIN_PREFIX,
        NOT_APPLICABLE_PREFIX,
        analyze,
        plan_upgrades,
        sha256_of,
        summarize,
    )
    from .frames import analyze_frames, extract_gif_frames, frame_layer_note
    from .pipeline import CHECK_LABELS, fix_for, fix_for_input, inspect, outcome, summary_line

#: 名字 → 所在子模块。惰性导入表（新增导出只要加一行，别写成 if/elif 链）。
_LAZY: dict[str, str] = {
    # contracts：纯 pydantic（agents.py 只需要这一层的请求模型）
    "TrackSummary": "contracts",
    "VideoEvidenceOutcome": "contracts",
    "VideoEvidenceRequest": "contracts",
    "VideoEvidenceResult": "contracts",
    "VideoForensicsReport": "contracts",
    # 解码：纯标准库
    "Decoded": "decode",
    "ExtractedFrame": "decode",
    "TrackFacts": "decode",
    "VideoFacts": "decode",
    "decode_video": "decode",
    # ⚠️ `frames_absent_clause` 是"一帧都没取到"那半句理由的**唯一定义处**
    #    （长原因与报告短句共用它）。登记进来，下一个复用它的人不必手抄第二份。
    "frames_absent_clause": "decode",
    "frames_unavailable_text": "decode",
    "sniff_media_type": "decode",
    # 取字节：只用标准库 + 复用图像侧收件箱
    "FETCH_REFUSAL_KINDS": "fetch",
    "MAX_VIDEO_BYTES": "fetch",
    "FetchedVideo": "fetch",
    "VideoFetchRefused": "fetch",
    "check_policy": "fetch",
    "classify_uri": "fetch",
    "load": "fetch",
    # 帧层：需要 Pillow / numpy（经图像链）
    "analyze_frames": "frames",
    "extract_gif_frames": "frames",
    "frame_layer_note": "frames",
    # 检测核心：需要 numpy（帧层还要 Pillow）
    "CHAIN_OF_CUSTODY": "forensics",
    "CHECK_WEIGHTS": "forensics",
    "DISCLAIMER": "forensics",
    "GATE_ABSTAIN_PREFIX": "forensics",
    # 「谁算视频」的**唯一判据**与「不是视频」那句话的**唯一来源**。
    # ⚠️ 2026-10-05 才登记到这张表上：在此之前它们只从 `forensics` 子模块能拿到，
    #    而 MCP 层走的正是"显式取子模块属性"⇒ **不会报错**，只会让下一个想复用这个
    #    判据的人写出手抄的第二份（那正是 Ogg 那件事的复发路径）。
    #    音频侧的同一条纪律写在 `verify_audio` 第 8 节（`enf_track` 的前身也是
    #    "只从子模块能拿到"）；视频侧由 `verify_video` 第 3c 节钉住。
    "NON_VIDEO_CLAUSE_PREFIX": "forensics",
    "NOT_APPLICABLE_PREFIX": "forensics",
    "analyze": "forensics",
    "is_supported_video": "forensics",
    "non_video_clause": "forensics",
    "plan_upgrades": "forensics",
    "sha256_of": "forensics",
    "summarize": "forensics",
    # 管线
    "CHECK_LABELS": "pipeline",
    "fix_for": "pipeline",
    "fix_for_input": "pipeline",
    "inspect": "pipeline",
    "outcome": "pipeline",
    "summary_line": "pipeline",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    """PEP 562：按名字惰性导入。理由见模块 docstring。"""
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value      # 缓存，后续访问不再走 __getattr__
    return value


def __dir__() -> list[str]:
    return __all__
