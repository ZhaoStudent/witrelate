"""音频鉴伪（`evidence-audio`）的本地初检能力。

分层（与图像链 `legal_acps/evidence/` **逐层同构**，便于两条链各自断言、互不越界）：

  * `contracts`  —— 证据契约（请求 / 报告 / 结果 / 拒绝）。**只依赖 pydantic**，
                    并且**复用**图像侧已经定义的 `Finding` / `SuspicionLevel` /
                    `UpgradePath` —— 这三个概念与模态无关，写第二份必然漂移。
  * `decode`     —— 字节 → 波形 + 容器事实。**只做解码与事实提取，不做任何判定。**
                    纯 numpy + 标准库（**本机与服务器都没有 ffmpeg / sox / librosa**）。
  * `forensics`  —— 纯函数检测核心（numpy，无模型、**不联网**、可离线断言）。
  弃权分**两层**：`GATE_ABSTAIN_PREFIX`（材料级：没解出波形 / 太短 / 近乎无声
  ⇒ 波形域整域没跑 ⇒ 综合等级「无法判定」）与 `NOT_APPLICABLE_PREFIX`
  （逐项：纯音谈不了频谱截止、只有一处安静区谈不了底噪一致性 ⇒ 只在 findings
  里留一条「无法判定」，不改变综合等级，但会在综合理由里点名交代）。
  汇总判据本身在 `legal_acps/verdict.py` —— **与图像链共用同一段逻辑**。
  * `fetch`      —— 取字节入口。⚠️ 与图像侧**刻意不同**：本能力**不出网**
                    （只认 `inbox:` 句柄与内联 base64），出网面为零。
  * `pipeline`   —— 把上面几层接起来，并用**固定模板**拼给调用方的摘要

## ⚠️ 导出是**惰性**的（PEP 562），理由与图像包完全相同

`legal_acps/agents.py` 需要 `audio.contracts` 里的请求模型来生成工具 schema。
若本文件在模块级 `from .decode import decode_audio`，那么**连只需要 pydantic 的
契约**也会顺带把 numpy 拉进来 —— 而"服务器上没装 numpy 时 8 个已过审 Agent
全部起不来"是不能接受的耦合（加分项不该有这种风险）。

    from legal_acps.audio import analyze      # 到这一行才 import forensics

## ⚠️ 本能力**刻意不做**的三件事

1. **不出网**。图像侧有一条受白名单约束的取图通道；音频侧**一条都没有**。
   理由：音频证据的现实形态是"用户上传的字节"，而每开一条出网通道就多一个
   SSRF 面。需要网络地址的音频，请调用方先下载并以附件形式提交。
2. **不判断"是不是 AI 合成的"**。本模块做的是**编辑痕迹**（剪辑 / 拼接 /
   重编码 / 底噪突变）的线索提取，**不是**合成语音检测 —— 后者需要声学模型，
   服务器既无 GPU 也没装推理框架，硬做只会给出看起来专业、实际不可复现的分数。
   （市面上的合成语音 MCP 都是联网黑盒服务，与本项目"离线可断言"的纪律冲突。）
3. **不输出「真 / 假」**。与图像链同源：只有线索强度 + 逐项 caveat + 综合等级 +
   升级建议。**「无法判定」是合法且经常正确的答案。**

方法学来源（都是公开发表的音频取证手段，本项目用 numpy 自行实现，未调用其代码）：
电网频率（ENF）相位连续性、背景噪声一致性、谱不连续、数字静音段、
重采样 / 有损编码截止痕迹、容器与编码参数自洽性。逐条见 `forensics.py` 的注释。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅供静态检查，运行时不执行
    from .contracts import (
        AudioEvidenceOutcome,
        AudioEvidenceRequest,
        AudioEvidenceResult,
        AudioForensicsReport,
    )
    from .decode import (
        Decoded,
        Waveform,
        decode_audio,
        pcm_unavailable_text,
        sniff_media_type,
    )
    from .fetch import (
        FETCH_REFUSAL_KINDS,
        MAX_AUDIO_BYTES,
        AudioFetchRefused,
        FetchedAudio,
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
        enf_track,
        plan_upgrades,
        sha256_of,
        summarize,
    )
    from .pipeline import CHECK_LABELS, fix_for, fix_for_input, inspect, outcome, summary_line

#: 名字 → 所在子模块。惰性导入表（新增导出只要加一行，别写成 if/elif 链）。
_LAZY: dict[str, str] = {
    # contracts：纯 pydantic（agents.py 只需要这一层的请求模型）
    "AudioEvidenceOutcome": "contracts",
    "AudioEvidenceRequest": "contracts",
    "AudioEvidenceResult": "contracts",
    "AudioForensicsReport": "contracts",
    # 解码：需要 numpy
    "Decoded": "decode",
    "Waveform": "decode",
    "decode_audio": "decode",
    "pcm_unavailable_text": "decode",
    "sniff_media_type": "decode",
    # 取字节：只用标准库 + 复用图像侧收件箱
    "FETCH_REFUSAL_KINDS": "fetch",
    "MAX_AUDIO_BYTES": "fetch",
    "AudioFetchRefused": "fetch",
    "FetchedAudio": "fetch",
    "check_policy": "fetch",
    "classify_uri": "fetch",
    "load": "fetch",
    # 检测核心：需要 numpy
    "CHAIN_OF_CUSTODY": "forensics",
    "CHECK_WEIGHTS": "forensics",
    "DISCLAIMER": "forensics",
    "GATE_ABSTAIN_PREFIX": "forensics",
    "NON_AUDIO_CLAUSE_PREFIX": "forensics",
    "NOT_APPLICABLE_PREFIX": "forensics",
    "analyze": "forensics",
    "enf_track": "forensics",
    # 「谁算音频」的**唯一判据**与「不是音频」那句话的**唯一来源**（2026-10-05）。
    # ⚠️ 必须登记在这里才算"从包顶层可达" —— `__getattr__` 只认这张表，
    #    不登记的后果是 `from legal_acps.audio import is_supported_audio` 直接
    #    ImportError，而 MCP 层是靠 `_audio_module()` 显式取子模块属性绕开的
    #    ⇒ 两条路会表现不一致（`verify_audio` 第 8 节正是为这条纪律设的）。
    "is_supported_audio": "forensics",
    "non_audio_clause": "forensics",
    "normalize_mime": "forensics",
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
