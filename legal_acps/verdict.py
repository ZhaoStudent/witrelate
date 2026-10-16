"""线索汇总的**唯一定义** —— 图像鉴伪与音频鉴伪两条链共用它。

════════════════════════════════════════════════════════════════════════
为什么必须抽出来
════════════════════════════════════════════════════════════════════════

`evidence/forensics.summarize()` 里那条判据（"前提不满足时不允许出「低」"）
是本项目**最危险缺陷**的代码化：把一张 92% 纯色的截图报成「低」——
读起来就是"没问题"，而真相是内容层检查**一项都没跑**。

音频那条链有一模一样的需求（ENF 检不出市电痕迹、音轨近乎静音、
样本过短……都只能弃权），而且**弃权的比例只会更高**（手机录音里
市电哼声的检出率本来就不高）。

如果两边各写一份，就会出现项目里已经踩过的那类漂移 ——
「识别侧的认可集合」与「抹除侧的认可集合」各自演化
（见 `evidence/inbox.HANDLE_IN_TEXT_RE` 的教训）。
这里的漂移后果更重：**两条链对同一种失败给出不同的档位**，
而报告表面上看不出任何异样。所以：

    · 判据只写一次（本模块）；
    · 两条链各自只提供**数据**（权重表、弃权前缀、措辞回调）。

⚠️ 本模块**不 import 任何东西**（连 pydantic 都不），只按属性访问
（`f.check` / `f.verdict` / `f.detail`）—— 这样图像侧的 Pillow、
音频侧的 numpy 都进不来，也就不会因为"多导一个模块"把
已经过审的 6 个 Agent 拖下水。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

#: 报告里**不许出现**的那类volatile 片段：`<_io.BytesIO object at 0x000002…>`。
#:
#: ⚠️ 它为什么必须是这副形状而不是"看到 0x 就抹掉"：
#:    检测措辞里本来就有十六进制（「出现了未在规范里的块标识 0x2c」、
#:    「size 字段 0x0fffffff」）—— 一锅抹掉会把**有用信息**一起删掉。
#:    容易变的只有"<对象名 at 0x地址>"这一种，它不是证据，只是某次运行的内存地址。
_VOLATILE_OBJECT = re.compile(r"<[^<>]{0,120}\s+at\s+0x[0-9a-fA-F]{4,}>")


def stable_reason(exc: BaseException) -> str:
    """把异常折成**同一份字节两次跑都长一样**的一行报告文案。

    ⚠️ 这不是洁癖，是取证可用性的问题：Pillow 的报错带着
    `<_io.BytesIO object at 0x00000212F958C770>` 这种内存地址 ⇒
    同一份视频前后跑两次，报告文本不一样。旁证复核时"两份报告不一致"
    是最容易被理解成"材料被改过"的那件事 —— 而它其实什么都没说。

    （2026-10-03 用不变量探针逮到：`cover` / `mjpeg` 两个样本的
       `model_dump_json()` 两次不同，差异字段正是 `frames_note` 与 `findings`。）
    """
    text = _VOLATILE_OBJECT.sub("<对象>", f"{type(exc).__name__}: {exc}")
    return " ".join(text.split())

#: 单项取值的两个字面量。**与 `evidence/contracts.FindingVerdict` 同源**，
#: 写在这里是因为本模块不 import pydantic（见模块 docstring）。
ABNORMAL = "提示异常"
ABSTAIN = "无法判定"

#: 加权分级的两条阈值。**只在这里定义一次** —— 两条链必须用同一套刻度，
#: 否则"同样的证据在图像链是「中」、在音频链是「低」"。
LEVEL_MID_AT = 1.0
LEVEL_HIGH_AT = 2.0

#: 没登记权重的检查项按这个值算。宁可小（少报）也不要大 ——
#: 漏登记一项就让等级虚高，等于让一个没人审过的检查项有了一票否决权。
DEFAULT_WEIGHT = 0.25

_ABSTAINED_ALL = "本次没有任何一项检查能给出有效结论（样本不足或格式不适用）。"


def summarize_findings(
    findings: Sequence[Any],
    *,
    weights: Mapping[str, float],
    gate_prefixes: Sequence[str],
    gate_rationale: Callable[[list[str]], str],
    subject: str = "该材料",
    finding_noun: str = "篡改认定",
    clean_phrase: str = "真实",
) -> tuple[str, str]:
    """把逐项发现折成综合线索等级。**纯函数、可断言、与模型无关。**

    判定顺序本身就是正确性（与 `termination.py` 同源）：顺序写反了，
    结果会从"无法判定"变成"低"，而两者读起来一个像谨慎、一个像安全。

    ① **有正向触发要最先判** —— 触发本身就是证据，不受前提是否满足影响，
       也不能因为"前提不满足"就把已看到的异常一笔勾销。
    ② **无触发、但有"前提不满足"式弃权 ⇒ 无法判定**（不是"低"）。
    ③ **全部弃权 ⇒ 无法判定**。
    ④ 才轮到加权分级。

    参数
    ----
    weights
        检查项 → 权重。**只对"提示异常"的项求和**，与 image 侧同源。
    gate_prefixes
        "整项没跑"的 `detail` 前缀集合。**多个前缀要被同一条规则覆盖** ——
        音频侧的弃权原因是复数（市电痕迹检不出 / 近乎静音 / 纯音为主 / 样本过短），
        只认一个前缀就会把其余几种悄悄降级成"低"。
    gate_rationale
        由调用方提供的措辞回调：给定"整项没跑的检查项名"，返回一段人话。
        把措辞留给调用方，是因为"这些项本来能发现什么"每个模态各不相同；
        但**触发这一段的条件由本模块统一判定**。
    subject
        报告主语（"该图" / "该音轨"），只影响措辞。
    finding_noun
        "不是……的认定"里的那个名词。图像侧用「伪造认定」（原文措辞，
        **不得改动** —— 它已被既有断言锚定），音频侧用「篡改认定」。
    clean_phrase
        "这不代表「该材料」____" 里的那个形容词。图像侧是「真实」（原文，
        **不得改动**），音频侧是「未经剪辑」。
    """
    triggered = [f.check for f in findings if f.verdict == ABNORMAL]
    decisive = [f for f in findings if f.verdict != ABSTAIN]

    # ① 有触发就先算账：不因为"前提不满足"把已看到的异常抹掉。
    if not triggered:
        gated = [
            f.check for f in findings
            if f.verdict == ABSTAIN and any(str(f.detail).startswith(p) for p in gate_prefixes)
        ]
        if gated:
            return ABSTAIN, gate_rationale(gated)

    # ② 全部弃权（格式不适用 / 样本不足）—— 与①不同，这里连正向触发的机会都没有。
    if not decisive:
        return ABSTAIN, _ABSTAINED_ALL

    total = sum(
        weights.get(f.check, DEFAULT_WEIGHT)
        for f in findings
        if f.verdict == ABNORMAL
    )
    if total >= LEVEL_HIGH_AT:
        level = "高"
    elif total >= LEVEL_MID_AT:
        level = "中"
    else:
        level = "低"

    if triggered:
        why = (
            f"触发 {len(triggered)} 项（{'、'.join(triggered)}），加权 {total:.1f}。"
            f"**这些是「值得进一步核查」的信号，不是{finding_noun}。**"
        )
    else:
        why = (
            "所有可执行的检查项均未提示异常。"
            f"**这不代表{subject}{clean_phrase}**，只代表本地这些手段没看到问题。"
        )
    return level, why


__all__ = [
    "ABNORMAL",
    "ABSTAIN",
    "DEFAULT_WEIGHT",
    "LEVEL_HIGH_AT",
    "LEVEL_MID_AT",
    "stable_reason",
    "summarize_findings",
]
