"""离线验证：证据鉴证 Leader（`evidence-leader`）的契约与交付。

这个脚本**不联网、不需要 API key、不碰平台** —— 全部是对纯函数与契约的断言。

用法（在 witrelate 目录下）：
    ./.venv/bin/python -m tools.verify_evidence_leader

════════════════════════════════════════════════════════════════════════
它守的是什么
════════════════════════════════════════════════════════════════════════

`evidence-leader` 是 2026-10-02 新增的一层：主控不再直接对接鉴伪专家，
而是委派给它，由它去组织下游（链深 3，正好等于 `MAX_CHAIN_DEPTH`）。
多这一层的**全部收益**是"主控只认识一个入口"+"编排不占主控的 8 步预算"。

但这一层的**交付物是转载的**：技术结论由 `evidence-image` 用确定性代码算出，
Leader 只负责组织与解释。所以真正会退化的不是"跑不起来"，而是
**转述过程中把话说过头或说漏**：

  · 把「无法判定」润色成「低」            → 静默安全结论（本项目最忌讳的一类）
  · 把"没跑的检查项"混进"未发现问题"里    → 同上，而且**断言全绿**
  · 一项都没检查却给出一个"低"            → 同上，而且是**最隐蔽**的一种：
                                            连一句"我没查"都没有（第 2b 节）
  · 丢掉某条线索的 `caveat`               → 只说异常、不说"异常不等于篡改"
  · 自造一个 `suspicion` 取值（"疑似"）    → 下游没人认得，取值域悄悄漂

上面每一条都**不报错**，只是结论变错。所以这个脚本把它们逐条钉死。

第 5 节还专门证明一件事：**第 5 层程序化兜底不会为证据链拼出一份空壳报告**。
一份 `suspicion=低` 而实际一项都没跑的报告，比"不产文件"危险得多。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from pydantic import ValidationError

from legal_acps import agents as agents_mod
from legal_acps.agents import EVIDENCE_LEADER_INSTRUCTIONS, issue_evidence_report
from legal_acps.contracts import EvidenceCase, EvidenceModality, EvidenceRuling
from legal_acps.termination import write_fallback_verdict

PASSED = 0
FAILED: list[str] = []


def section(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def ok(message: str) -> None:
    global PASSED
    PASSED += 1
    print(f"  [通过] {message}")


def bad(message: str) -> None:
    FAILED.append(message)
    print(f"  [失败] {message}")


def expect_reject(label: str, **kwargs) -> None:
    """断言这份 kwargs **构造不出** EvidenceRuling（契约必须把它挡下来）。"""
    try:
        EvidenceRuling(**kwargs)
    except ValidationError:
        ok(f"契约拒绝了{label}")
    except Exception as exc:  # noqa: BLE001
        bad(f"{label} 抛的是 {type(exc).__name__} 而不是 ValidationError：{exc!r}")
    else:
        bad(f"{label} **没有**被契约拒绝 —— 它会一路走到底，没人发现")


def base_ruling(**overrides):
    """一份各字段都合法的基线报告，用 `overrides` 造出各种畸形输入。

    ⚠️ 基线里**必须**有一条有效线索（`verdict="未提示异常"`），因为契约要求
    "非「无法判定」时至少有一条给出了有效结论"。这不是为了迁就断言：
    真实报告必然如此 —— 检测层每跑一项都会产出一条 `Finding`
    （包括"未提示异常"那种），所以一份诚实的转述**不会**缺这个字段。
    """
    payload = dict(
        subject="群聊截图一张",
        modality="图像",
        suspicion="低",
        findings=[
            {
                "check": "container",
                "verdict": "未提示异常",
                "detail": "真实容器 image/jpeg，与声明一致。",
                "caveat": "容器一致只能说明封装没换过，与画面内容真伪无关。",
            }
        ],
        not_run=[],
        sha256="a" * 64,
        upgrades=[],
        source_uri="inbox:" + "b" * 64,
        fetched_from="inbox",
        conclusion="未发现指向内容层改动的线索。",
        reasoning="容器与元数据层正常，内容层各项均未提示异常。",
    )
    payload.update(overrides)
    return payload


def check_contract() -> None:
    section("1. 综合线索等级的取值域（不许自造）")
    # 取值域来自 evidence.contracts.SuspicionLevel，**只有四态**。
    # 自造值（"疑似""大概率""中等偏上"）会被 pydantic 直接拒绝 ——
    # 而模型很自然会写这些词，所以必须由契约挡，不能靠提示词自觉。
    for value in ("疑似", "大概率", "中等偏上", "真实", "伪造", ""):
        expect_reject(f'suspicion={value!r}', **base_ruling(suspicion=value))
    for value in ("低", "中", "高", "无法判定"):
        try:
            EvidenceRuling(**base_ruling(suspicion=value, not_run=["x"] if value == "无法判定" else []))
        except ValidationError as exc:  # noqa: PERF203
            bad(f"合法取值 {value!r} 被拒绝了：{exc}")
        else:
            ok(f"合法取值 {value!r} 通过")
    # 取值域里**不许**出现真/假 —— 这是本能力最重要的一条纪律，写进断言才守得住。
    from legal_acps.evidence.contracts import SuspicionLevel

    import typing

    allowed = set(typing.get_args(SuspicionLevel))
    forbidden = {"真", "假", "伪造", "P 过", "P过"}
    if allowed & forbidden:
        bad(f"取值域混进了断言性结论：{sorted(allowed & forbidden)}")
    else:
        ok(f"取值域不含断言性结论（{sorted(allowed)}）")


def check_undecidable_needs_reasons() -> None:
    section("2. 「无法判定」必须说清是哪几项没跑")
    # 这一条是 `evidence-image` 缺陷 1 的**契约化**：
    # 当年那张 92% 纯色的图被报成「综合线索等级：低」，真相是内容层的检查
    # （ela / noise_residual / copy_move）因"非自然摄影内容"整项弃权、
    # **一项都没跑**，只剩封装层还能出结论，而封装层从来不检测画面内容。
    # 断言全绿、代码跑得通、结论是错的。
    expect_reject(
        "「无法判定」但 not_run 为空（= 把'没检查'说成'没问题'）",
        **base_ruling(suspicion="无法判定", not_run=[]),
    )
    try:
        EvidenceRuling(
            **base_ruling(
                suspicion="无法判定",
                not_run=["噪声层一致性：样本为大片纯色，前提不成立，整项弃权"],
            )
        )
    except ValidationError as exc:
        bad(f"「无法判定」+ 写明理由却被拒绝：{exc}")
    else:
        ok("「无法判定」+ 写明没跑的项 → 通过（合法的常见形态）")

    # 反向**不**成立：有正向触发的同时也可以有没跑的项，那时等级照样可以是低/中/高。
    # 若把这条写成双向，就会把正常的"部分检查适用"误杀成"无法判定"。
    try:
        EvidenceRuling(**base_ruling(suspicion="低", not_run=["复制-移动：不适用"]))
    except ValidationError as exc:
        bad(f"「低」+ 有未适用项被误杀（矫枉过正）：{exc}")
    else:
        ok("「低」+ 列明未适用项 → 通过（未做双向强制，避免矫枉过正）")


def check_level_needs_effective_findings() -> None:
    section("2b. 给出「低/中/高」必须至少有一条有效结论")
    # 上面那节只挡住了"无法判定却不给理由"**这一半**；这是它的镜像，也是
    # 2026-10-02 复查才发现的缺口：`suspicion="低"` + `findings=[]`
    # （**一项都没检查**）原先照样被接受 —— 同样是静默的安全结论，而且更隐蔽，
    # 它连一句"我没查"都没有。
    #
    # ⚠️ 判据必须是"**有一条 `verdict != 无法判定` 的线索**"，不能是"findings 非空"：
    #    整项弃权的检查项**也会**进 findings（刻意如此，见 `forensics.summarize`），
    #    所以只判非空的写法**挡不住"全部弃权却报低"** —— 那正是这条要防的形状。
    abstained = {
        "check": "ela",
        "verdict": "无法判定",
        "detail": "【前提不满足】（92% 的区块几乎没有纹理），本项不适用。",
        "caveat": "本项的前提是自然摄影内容；截图、纯色背景不满足这个前提。",
    }
    expect_reject(
        "「低」但 findings 为空（= 一项都没检查，却给了个让人安心的档位）",
        **base_ruling(suspicion="低", findings=[]),
    )
    expect_reject(
        "「低」但**全部线索都弃权**（只判「非空」的写法挡不住这一种）",
        **base_ruling(suspicion="低", findings=[abstained]),
    )
    # 反向：部分弃权 + 至少一条有效线索 ⇒ 低/中/高照常合法（不许矫枉过正）
    try:
        EvidenceRuling(
            **base_ruling(suspicion="低", findings=[abstained, *base_ruling()["findings"]])
        )
    except ValidationError as exc:
        bad(f"「低」+ 部分弃权 + 一条有效线索被误杀（矫枉过正）：{exc}")
    else:
        ok("「低」+ 部分弃权 + 一条有效线索 → 通过（不许矫枉过正）")
    # 反向：「无法判定」不受本条约束（它由 not_run 那条管）
    try:
        EvidenceRuling(**base_ruling(suspicion="无法判定", findings=[abstained], not_run=["ela"]))
    except ValidationError as exc:
        bad(f"「无法判定」+ 全弃权 + not_run 被拒绝：{exc}")
    else:
        ok("「无法判定」+ 全弃权 + not_run → 通过（合法的常见形态）")

    # 这条不变式必须与检测层**同源**：`summarize()` 只有在"至少一条非弃权线索"时
    # 才可能给出低/中/高（`if not decisive: return "无法判定"`）。
    # 关系断言：汇总层允许的档位 ⊂ 检测层可能产出的档位。
    from legal_acps.evidence.forensics import summarize

    level, _why = summarize([_F("container", "未提示异常"), _F("ela", "无法判定")])
    if level == "低" and _passes(suspicion="低", findings=[abstained, *base_ruling()["findings"]]):
        ok("不变式与检测层同源：`summarize()` 给「低」的形状，契约也接受")
    else:
        bad(f"契约与检测层的不变式对不上：summarize 给 {level!r}")
    level2, _ = summarize([_F("container", "无法判定"), _F("ela", "无法判定")])
    if level2 == "无法判定":
        ok("不变式与检测层同源：全弃权时 `summarize()` 只给「无法判定」")
    else:
        bad(f"检测层全弃权却给了 {level2!r} —— 契约的判据就不再是它的镜像了")


def _F(check: str, verdict: str):
    """造一条最小 Finding（给同源断言用）。"""
    from legal_acps.evidence.contracts import Finding

    return Finding(check=check, verdict=verdict, detail="x", caveat="y")


def _passes(**overrides) -> bool:
    """`base_ruling(**overrides)` 能不能构造出 `EvidenceRuling`。

    ⚠️ 刻意收 `**overrides` 并**在内部补全基线**，而不是收一份现成的 payload：
    后者被自己坑过一次 —— 调用处只传了 `suspicion` / `findings`，`subject` /
    `conclusion` / `reasoning` 全缺，于是 `_passes` 恒为 False，而断言打印的却是
    "**契约与检测层的不变式对不上**"，指向完全无关的方向。
    经 `base_ruling` 走一遍，必填字段就不可能漏。
    """
    try:
        EvidenceRuling(**base_ruling(**overrides))
    except Exception:  # noqa: BLE001
        return False
    return True


def check_caveat_required() -> None:
    section("3. 每条线索必须自带 caveat（不能证明什么）")
    good_finding = {
        "check": "noise_residual",
        "verdict": "提示异常",
        "strength": 0.6,
        "detail": "左右两半噪声方差差异明显。",
        "caveat": "降噪、强压缩或拼接都会造成同类差异，本项不足以单独定论。",
    }
    try:
        EvidenceRuling(**base_ruling(findings=[good_finding]))
    except ValidationError as exc:
        bad(f"带 caveat 的线索被拒绝：{exc}")
    else:
        ok("带 caveat 的线索 → 通过")

    missing = {k: v for k, v in good_finding.items() if k != "caveat"}
    expect_reject("缺 caveat 的线索（只说异常、不说异常不等于篡改）", **base_ruling(findings=[missing]))

    # verdict 的取值域同样不许出现 true/false
    expect_reject(
        "verdict=True（二值真伪结论）",
        **base_ruling(findings=[{**good_finding, "verdict": True}]),
    )


def check_persisted() -> None:
    section("4. 交付物真的落盘 output/evidence-*.json")
    tmp = Path(tempfile.mkdtemp(prefix="evidence-leader-verify-"))
    original = agents_mod.OUTPUT_DIR
    try:
        agents_mod.OUTPUT_DIR = tmp
        result = issue_evidence_report(EvidenceRuling(**base_ruling()))
        path = Path(result["saved_to"])
        if not path.exists():
            bad(f"报告没落盘：{path}")
        elif path.name.startswith("evidence-") and path.suffix == ".json":
            ok(f"已落盘 {path.name}（{path.stat().st_size} 字节）")
        else:
            bad(f"落盘文件名不符合约定：{path.name}")
        # 回执要能直接看出结论档位与检查项 —— 不然后续排障只能去翻文件
        for key in ("saved_to", "suspicion", "checks", "not_run_count"):
            if key not in result:
                bad(f"回执缺字段 {key}")
                break
        else:
            ok(f"回执字段齐全：{sorted(result)}")
    finally:
        agents_mod.OUTPUT_DIR = original
        shutil.rmtree(tmp, ignore_errors=True)


def check_no_fake_shell() -> None:
    section("5. 第 5 层兜底**不会**为证据链拼出一份空壳报告")

    class FakeMessage:
        """最小的 role=tool 消息：`.text()` 返回一份观测 JSON。"""

        role = "tool"
        name = "acps_task"

        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def text(self) -> str:
            import json

            return json.dumps(self._payload, ensure_ascii=False)

    # `collect_partner_payloads` 只认三种形状（is_incitement / overall_truth_score /
    # harm_index+dimensions）。鉴伪结果里**一个都没有** ⇒ 载荷为空 ⇒ 不产文件。
    evidence_shaped = FakeMessage(
        {
            "status": "success",
            "result": {
                "suspicion": "低",
                "findings": [{"check": "exif", "verdict": "未提示异常"}],
                "sha256": "c" * 64,
            },
        }
    )
    tmp = Path(tempfile.mkdtemp(prefix="evidence-leader-fallback-"))
    try:
        path = write_fallback_verdict(
            messages=[evidence_shaped], reason="模型拒不交报告", output_dir=tmp
        )
        if path is None:
            ok("鉴伪载荷拼不出 FinalVerdict ⇒ 兜底返回 None（不产文件）")
        else:
            bad(f"兜底为证据链产出了文件：{path} —— 那是一份没有依据的空壳")
        leftovers = sorted(p.name for p in tmp.iterdir())
        if leftovers:
            bad(f"目录里被写出了文件：{leftovers}")
        else:
            ok("输出目录保持为空（宁可'不产文件'这个可检测的明确失败）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_instructions() -> None:
    section("6. 提示词铁律与「钉住自己人」")
    text = EVIDENCE_LEADER_INSTRUCTIONS
    # ① discovery 是全网检索：必须按 name 钉住自己人，否则会抓到别的团队的鉴伪 Agent。
    #    ⚠️ 名单**从模态枚举现场派生**（取各 Partner **自己注册的** `display_name`），
    #    不手抄 —— 原先这里只查「图像鉴伪 Partner」那一个，于是加音频 / 视频模态时
    #    "提示词里漏钉一个新模态"**不会被任何断言拦下**。后果不是报错，而是那条
    #    分支在 discovery 的全网检索里抓到别人的同类 Agent、白烧 Leader 的 8 步预算。
    partner_names: list[str] = []
    for m in EvidenceModality:
        builder = agents_mod.BUILDERS.get(f"evidence-{m.kind}")
        agent = builder(None) if builder is not None else None
        partner_names.append(str(getattr(agent, "display_name", "") or ""))
    unnamed = [m.value for m, n in zip(EvidenceModality, partner_names) if not n]
    unpinned = [n for n in partner_names if n and n not in text]
    if unnamed:
        # ⚠️ 空名字必须单独报：`"" in text` 恒为真，一条空名字会让这条断言**静默通过**。
        bad(
            f"这些模态的下游取不到注册名：{unnamed}"
            "（角色没建起来 / 没有 display_name ⇒ 空名字在子串判定里恒为真）"
        )
    if unpinned:
        bad(
            f"提示词没有按 name 钉住下游：缺 {unpinned} —— "
            "discovery 会返回别人的同类 Agent，白烧步数"
        )
    if not unnamed and not unpinned:
        ok(f"已按 name 钉住全部 {len(partner_names)} 个模态的下游：{partner_names}")

    # ② 措辞铁律必须**写出来**（这是本角色最重要的一条）
    for needle, why in (
        ("不允许出现", "没有明写'不允许出现'这类禁令"),
        ("伪造", "禁令没有点名'伪造'这个最容易被说出口的词"),
        ("caveat", "没要求转述每条线索的 caveat"),
        ("无法判定", "没说清'无法判定'是合法答案"),
        ("not_run", "没要求把没跑成的检查项单独列出来"),
    ):
        if needle not in text:
            bad(f"提示词缺关键要求：{why}")
        else:
            ok(f"提示词含关键要求：{needle!r}")

    # ③ 「测不了 ≠ 没问题」必须被显式区分 —— 这是本项目最难发现的一类缺陷
    if "没检查" in text and "没问题" in text:
        ok("提示词显式区分「没检查」与「没问题」")
    else:
        bad("提示词没有区分「没检查」与「没问题」—— 静默安全结论会从这里长出来")

    # ④ 派发时**必须**带上材料引用，否则下游收不到图
    if "source_refs" in text:
        ok("提示词要求把 source_refs 原文写进委派请求")
    else:
        bad("提示词没要求带上材料引用 —— 下游会回一句'没有图可看'，整条链白跑")

    # ⑤ 必须声明"不直接联系下游专家"的边界（AIP §3.2）
    if "不是你的" in text:
        ok("提示词声明了「下游专家不是你的 Partner」这一条边界")
    else:
        bad("提示词没有声明越层调用的边界（会破坏 AIP §3.2 的独立性）")


def main() -> int:
    print("证据鉴证 Leader 离线验证（不联网 / 不需要 API key / 不碰平台）")
    check_contract()
    check_undecidable_needs_reasons()
    check_level_needs_effective_findings()
    check_caveat_required()
    check_persisted()
    check_no_fake_shell()
    check_instructions()

    print()
    if FAILED:
        print(f"有 {len(FAILED)} 项未通过：")
        for item in FAILED:
            print(f"  · {item}")
        print(f"\n通过 {PASSED} 项 / 失败 {len(FAILED)} 项")
        return 1
    print(f"全部通过（{PASSED} 项断言）")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
