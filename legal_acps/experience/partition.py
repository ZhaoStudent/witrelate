"""信息分区的**渲染与隔离** —— 纯函数模块（判据的锚）。

════════════════════════════════════════════════════════════════════════
为什么这个模块必须是纯函数、且必须独立于 session.py
════════════════════════════════════════════════════════════════════════

本模块管的是整个信息分区改造里**唯一不能出错的性质**：诱导方（指控方）
永远看不到 `mine`。它靠三件事成立：

1. **墙在代码层**：诱导方输入的每一个字符都产自 `build_deceiver_prompt`，
   这个函数**根本没有 mine 的引用** —— 不是"叮嘱模型别用"，是拼接处
   就没有。提示词层的墙（"请不要偷看"）在模型不配合时是纸。
2. **判据打在行为上**：`check_local` 用假分区喂这些函数，断言"theirs 在、
   mine 不在"——不是 grep 源码（本项目写死过那条教训）。
3. **零变化退化**：老会话 / stub 模型没有 partition 字段 ⇒ 所有渲染函数
   返回空串、`build_deceiver_prompt` 走与旧版**逐字节相同**的老路 ——
   老判据（前端流程验证等）一个都不破。

依赖纪律：本模块**只依赖标准库 + roles 的契约**，不 import wit / session
—— `check_local` 可以在离线进程里直接 import 它做行为断言。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .roles import InfoPartition

#: 桶显示名（渲染与判据共用同一份 —— 文案改动时判据跟着走，不会漂移）。
BUCKET_LABELS = {
    "shared": "双方都知道的信息",
    "mine": "只有当事人自己知道的信息",
    "theirs": "指控方声称掌握、当事人没见过的材料（只是声称，未必存在）",
}

# ── 信息差教学概念（2026-10-19 功能④收尾：教学话术）────────────────────
#
# 三个概念是**教学设计**（固定文案，不是模型的自由发挥）：
# 概念句只定义在本模块这一处 —— 复盘板块渲染、裁判教学指令的关键词、
# check_local 判据全部引用这里（与 BUCKET_LABELS 同一条单一来源纪律）。
# 模型的职责不是复述概念，而是把它们**落到当事人这局的原话与选择上**
# （见 build_reveal_prompt 的教学指令段）。

#: 复盘板块标题（判据哨兵之一）。
INFO_GAP_TITLE = "信息差复盘 · 这局真正的胜负手"

#: theirs 桶教学点：声称 ≠ 证据（指控方"手里有材料"只是声称）。
INFO_GAP_CLAIM_NOTE = (
    "对方嘴里的「我手里有材料」从头到尾只是一句声称——"
    "声称不等于证据，一直拿不出来就等于没有。他越是不肯亮、"
    "越是用「你心虚什么」反问施压，越说明手里没货。"
)

#: mine 桶教学点：你其实有牌（自己的实情是底牌，先盘牌再决定亮不亮）。
INFO_GAP_CARD_NOTE = (
    "这几条只有你自己知道——你其实一直有牌。被指控时先盘一遍"
    "「我知道什么、他知道什么、大家都能看见什么」，再决定亮哪张、"
    "按住哪张；这一局你没亮不是错，但下次要知道自己手里有什么。"
)

#: shared 桶教学点：公共战场（把争论拉回双方都看得见的信息、举证责任在指控方）。
INFO_GAP_FIELD_NOTE = (
    "双方都看得见的信息才是公共战场。把争论拉回这里、"
    "要求对方把「声称的材料」摆上桌面，比你自己拼命自证有效得多——"
    "举证责任在指控的人，不在你。"
)


def _partition_of(case: Mapping[str, Any]) -> InfoPartition | None:
    """从 case dict 里取分区；取不到或全空 ⇒ None（= 走零变化老路）。"""
    raw = (case or {}).get("partition")
    if not raw:
        return None
    try:
        part = InfoPartition.model_validate(raw)
    except Exception:  # noqa: BLE001 —— 契约不符 ⇒ 当作没有分区，如实退化
        return None
    return part if part else None


def has_partition(case: Mapping[str, Any]) -> bool:
    """这个 case 有没有可用的分区（判据与拼接处共用同一口径）。"""
    return _partition_of(case) is not None


def _item_texts(it: Any) -> tuple[str, str]:
    """取一条信息的 (fact, note)。兼容两种形态：dict（case 直传）与
    PartitionItem 模型实例（InfoPartition 校验后的桶成员）——
    只认一种会在「假 case dict」与「真模型实例」之间静默丢内容。"""
    if isinstance(it, Mapping):
        return str(it.get("fact") or "").strip(), str(it.get("note") or "").strip()
    return (
        str(getattr(it, "fact", "") or "").strip(),
        str(getattr(it, "note", "") or "").strip(),
    )


def _render_items(items: list[Any]) -> str:
    lines: list[str] = []
    for it in items:
        fact, note = _item_texts(it)
        if not fact:
            continue
        lines.append(f"· {fact}" + (f"（{note}）" if note else ""))
    return "\n".join(lines)


def render_accuser_view(case: Mapping[str, Any]) -> str:
    """诱导方（指控方）看得见的分区段。**这里没有 mine —— 墙就在这**。

    返回空串 = 没有分区（调用方走老路）。"""
    part = _partition_of(case)
    if part is None:
        return ""
    sections = []
    if part.shared:
        sections.append(
            f"═══ {BUCKET_LABELS['shared']} ═══\n{_render_items(part.shared)}")
    if part.theirs:
        sections.append(
            "═══ 你声称掌握、对方没见过的材料 ═══\n"
            f"{_render_items(part.theirs)}\n"
            "（你手里其实有没有都行——指控方在现实里也常常只是「声称」。"
            "你可以继续拿它们施压，但它们只是声称。）")
    if not sections:
        # 有分区对象但 shared/theirs 都空：仍不能把 mine 给指控方 ——
        # 返回一句明话而不是空串，否则调用方会退回**整份事实清单**（倒灌）。
        return "═══ 你掌握的信息 ═══\n（除了指控本身，没有更多公开信息）"
    return "\n\n".join(sections)


def render_user_view(case: Mapping[str, Any]) -> str:
    """当事人视角的分区段（shared + mine + theirs-as-声称）。

    当事人当然看得见自己的实情；theirs 以「对方声称」的形态呈现——
    他听过那些声称（对方跟他说过），只是没见过材料。返回空串 = 没有分区。"""
    part = _partition_of(case)
    if part is None:
        return ""
    sections = []
    if part.shared:
        sections.append(
            f"═══ {BUCKET_LABELS['shared']} ═══\n{_render_items(part.shared)}")
    if part.mine:
        sections.append(
            f"═══ {BUCKET_LABELS['mine']} ═══\n{_render_items(part.mine)}")
    if part.theirs:
        sections.append(
            f"═══ {BUCKET_LABELS['theirs']} ═══\n{_render_items(part.theirs)}")
    return "\n\n".join(sections) if sections else ""


def render_full_partition(case: Mapping[str, Any]) -> str:
    """三桶全貌 —— 给裁判（立题 / 揭示）用。裁判是中立方，看得到一切。"""
    part = _partition_of(case)
    if part is None:
        return ""
    sections = []
    for key in ("shared", "mine", "theirs"):
        items = getattr(part, key)
        if items:
            sections.append(
                f"═══ {BUCKET_LABELS[key]} ═══\n{_render_items(items)}")
    if not sections:
        return ""
    return "═══ 信息分区（受理时按「谁知道」整理）═══\n\n" + "\n\n".join(sections)


def build_deceiver_fact_section(case: Mapping[str, Any]) -> str:
    """诱导方输入里的「信息」段：有分区 ⇒ 指控方视图；没有 ⇒ **旧版原样**
    （整份事实清单 JSON）。零变化退化由判据盯着（空分区必须逐字节等于老形态）。"""
    accuser = render_accuser_view(case)
    if accuser:
        return accuser
    # 旧版原样（session._deceiver_round 历史形态，一个字不改）
    return (
        "═══ 可核验事实清单 ═══\n"
        f"{json.dumps((case or {}).get('fact_sheet') or {}, ensure_ascii=False, indent=2)}"
    )


def build_deceiver_prompt(
    case: Mapping[str, Any],
    transcript: str,
    nxt: int,
    difficulty: str,
    difficulty_brief: str,
) -> str:
    """诱导方第 nxt 轮的完整输入（从 session._deceiver_round 抽出的纯函数）。

    🔴 唯一要害：事实段来自 `build_deceiver_fact_section` —— mine 无路可进。
    无分区路径与 session._deceiver_round 的历史形态**逐字节一致**（判据盯着）。
    difficulty / difficulty_brief 由调用方传（口径在 session.py，本模块
    不反向 import 它，避免环）。"""
    case = case or {}
    return (
        "═══ 本局命题 ═══\n"
        f"{case.get('proposition', '（未给出）')}\n\n"
        f"{build_deceiver_fact_section(case)}\n\n"
        "═══ 你方（指控方）立场 ═══\n"
        f"{case.get('pro_position', '那条指控成立')}\n\n"
        "═══ 交锋记录（最新在最后）═══\n"
        f"{transcript or '（还没有交锋，这是你的开场）'}\n\n"
        f"这是第 {nxt} 轮（本局难度：{difficulty}）。请用话术构建你的发言，"
        "并调用 `debate_trap_argument` 提交：message 给用户看、"
        "traps 诚实自曝你用了哪些手法。\n"
        "本局难度的分寸：" + difficulty_brief
    )


def build_info_gap_review(case: Mapping[str, Any]) -> dict[str, Any] | None:
    """信息差复盘板块的**结构化数据**（给前端，由服务端算——不依赖裁判）。

    为什么这块是固定文案而不是让裁判写：三桶对照与三个概念是**教学设计**
    （哪些是事实、概念句怎么措辞，教学侧已经定死）；让模型复述确定性
    事实既浪费又有幻觉风险。裁判的增值部分在 summary / hot_spots / advice
    ——把概念**落到当事人这局的原话上**（教学指令段在 build_reveal_prompt）。

    无分区 ⇒ None（前端不渲染 = 零变化）。"""
    part = _partition_of(case)
    if part is None:
        return None
    return {
        "title": INFO_GAP_TITLE,
        "shared": [t for t, _ in (_item_texts(it) for it in part.shared) if t],
        "theirs": [t for t, _ in (_item_texts(it) for it in part.theirs) if t],
        "mine": [t for t, _ in (_item_texts(it) for it in part.mine) if t],
        "theirs_note": INFO_GAP_CLAIM_NOTE,
        "mine_note": INFO_GAP_CARD_NOTE,
        "shared_note": INFO_GAP_FIELD_NOTE,
    }


def _teaching_section(case: Mapping[str, Any]) -> str:
    """裁判输入里的信息差教学指令段（有分区才出现）。

    概念关键词从上面的常量引用（单一来源：模型的教学口径与复盘页固定
    文案不会各说各话）。哨兵 = 段标题「信息差教学」。"""
    part = _partition_of(case)
    if part is None:
        return ""
    return (
        "═══ 信息差教学（本局三桶分区的用法）═══\n"
        "本局存在信息不对称，这是复盘的必修素材。复盘页会展示固定文案，"
        "你不必逐字复述概念；你的任务是把这些概念**落到当事人这局的原话"
        "与选择上**：\n"
        "- 指控方声称掌握、当事人没见过的材料（theirs 桶）：在 hot_spots 里"
        "点明当事人有没有把「声称」当成「证据」——例如被「我手里有记录」"
        "吓得急着自证，而不是要求对方亮出来。\n"
        "- 只有当事人知道的部分（mine 桶）：在 summary 里替他盘点「他其实"
        "有牌」，并结合他实际亮没亮（没亮不是错，但要知道自己有）。\n"
        "- 双方都知道的部分（shared 桶）：advice 至少一条教他把争论拉回"
        "公共战场——把举证责任还给指控方（要求对方先亮声称的材料，"
        "而不是自己拼命自证）。\n\n"
    )


def build_reveal_prompt(
    case: Mapping[str, Any],
    transcript: str,
    traps_json: str,
) -> str:
    """裁判收尾揭示的完整输入（从 session.reveal 抽出的纯函数）。

    本次改造**只加「三桶全貌」一段**（用户拍板：揭示先带上三桶信息，
    信息差教学话术以后再磨）；没有分区时整段消失 = 旧形态零变化。"""
    case = case or {}
    partition_section = ""
    full = render_full_partition(case)
    if full:
        # 三桶全貌在前（事实），教学指令在后（怎么用这份事实教学）。
        partition_section = full + "\n\n" + _teaching_section(case)
    return (
        "═══ 本局命题 ═══\n"
        f"{case.get('proposition', '（未给出）')}\n\n"
        f"{partition_section}"
        "═══ 可核验事实清单 ═══\n"
        f"{json.dumps(case.get('fact_sheet') or {}, ensure_ascii=False, indent=2)}\n\n"
        "═══ 交锋记录 ═══\n"
        f"{transcript}\n\n"
        "═══ 诱导方逐轮自曝的话术记录（原始数据，可能有遗漏）═══\n"
        f"{traps_json}\n\n"
        "请完成收尾揭示，调用 `case_reveal` 提交。"
        "记住三件事：① 当事人是被模拟的一方，**不要羞辱他**；"
        "② `hot_spots`（他被带偏的地方）与 `solid_moves`（他站住的地方）**都要写**，"
        "只讲前者会把他教成被动的；③ 你给的是话术识别与自保建议，不是法律定性。"
    )


def build_opening_prompt(case: Mapping[str, Any]) -> str:
    """裁判开场立题的输入（从 session._opening 抽出的纯函数）。

    裁判是中立方：有分区时附三桶全貌（立题的「材料够不够」判断需要它）。"""
    case = case or {}
    partition_section = ""
    full = render_full_partition(case)
    if full:
        partition_section = full + "\n\n"
    return (
        "═══ 受理官整理的命题 ═══\n"
        f"{case.get('proposition', '（未给出）')}\n\n"
        f"{partition_section}"
        "═══ 可核验事实清单 ═══\n"
        f"{json.dumps(case.get('fact_sheet') or {}, ensure_ascii=False, indent=2)}\n\n"
        "═══ 两方立场 ═══\n"
        f"指控方：{case.get('pro_position', '（未给出）')}\n"
        f"当事人：{case.get('con_position', '（未给出）')}\n\n"
        "请完成开场立题，调用 `case_open` 提交。"
        "记住：`note` 只写**一句话**，不要给自保建议、不要下真假结论。"
    )


__all__ = [
    "BUCKET_LABELS",
    "INFO_GAP_TITLE",
    "build_deceiver_fact_section",
    "build_deceiver_prompt",
    "build_info_gap_review",
    "build_opening_prompt",
    "build_reveal_prompt",
    "has_partition",
    "render_accuser_view",
    "render_full_partition",
    "render_user_view",
]
