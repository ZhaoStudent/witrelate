"""扫.`analyze()` 的输出不变量（fuzz + 全样本）。不抛异常 ≠ 没问题 ——
这里套的是本项目反复踩过的那几条：**出口文案不许说出与其判据矛盾的话**。

运行：./.venv/Scripts/python.exe -m run._probe_invariants
"""

from __future__ import annotations

import random

import run.video_samples as V
from legal_acps.verdict import ABNORMAL, ABSTAIN
from legal_acps.video import forensics as VF
from legal_acps.video import pipeline as VP

PROBLEMS: list[str] = []


def complain(sample: str, what: str) -> None:
    PROBLEMS.append(f"{sample}: {what}")


def check_sample(name: str, data: bytes) -> None:
    try:
        report = VF.analyze(data)
    except ValueError:
        return  # 不是视频容器 —— 拒绝是对的，不算缺陷
    except Exception as exc:  # 其它任何异常都是缺陷
        complain(name, f"analyze 抛了 {type(exc).__name__}: {exc}")
        return

    # -- 不变量 1：每一条发现都必须有 readable 的 detail 与 caveat -------------
    for f in report.findings:
        if not str(f.detail).strip():
            complain(name, f"「{f.check}」的 detail 是空的（= 没说自己为什么这么判）")
        if not str(f.caveat).strip():
            complain(name, f"「{f.check}」的 caveat 是空的（= 读者会把它当结论用）")

    # -- 不变量 2：等级的决定必要条件 ---------------------------------------
    fired = [f.check for f in report.findings if f.verdict == ABNORMAL]
    gated = [f.check for f in report.findings
             if f.verdict == ABSTAIN and str(f.detail).startswith(VF.GATE_ABSTAIN_PREFIX)]
    na = [f.check for f in report.findings
          if f.verdict == ABSTAIN and str(f.detail).startswith(VF.NOT_APPLICABLE_PREFIX)]
    if not fired and report.suspicion != ABSTAIN:
        if not report.suspicion:
            complain(name, "没有触发项、等级却不是「未提示异常」系列（suspicion 为空）")

    # -- 不变量 3：闸门存在且无任何触发 ⇒ 必须是「无法判定」 ------------------
    if gated and not fired and report.suspicion != ABSTAIN:
        complain(name, f"有材料级闸门 {gated} 却给了等级 {report.suspicion}（应该无法判定）")

    # -- 不变量 4（最要紧）：出口文案不许说出矛盾的话 ------------------------
    summary = VP.summary_line(report)
    if "均未提示异常" in summary:
        if gated:
            complain(name, f"摘要说「均未提示异常」，但闸门项 {gated} 整项没跑")
        if na and "未有参与判级" not in summary and "无从执行" not in summary:
            complain(name, f"摘要说「均未提示异常」，却没交代 {na} 未参与判级")
    if report.suspicion == ABSTAIN and "皆未提示异常" in summary:
        complain(name, "内部判无法判定、摘要说了「未提示异常」")
    if not fired and report.suspicion != ABSTAIN and na and "无从执行" not in summary:
        complain(name, f"有 {na} 未参与判级，摘要却一个字没提")

    # -- 不变量 5：_summary_line_ 里的复检条数必须是真的 ----------------------
    ladder = VF.plan_upgrades(report.suspicion)
    if f"已给出 {len(report.upgrades)} 条" not in summary or len(report.upgrades) != len(ladder):
        complain(name, f"摘要里的复检条数与实际不符（摘要 {summary.count('已给出')} / "
                       f"报告 {len(report.upgrades)} / 阶梯 {len(ladder)}）")
    if not ladder:
        complain(name, "复检阶梯为空 —— 等于把唯一有法律效力的救济路径藏掉了")

    # -- 不变量 6：确定性（同一份字节两次分析必须逐字节相同） -----------------
    again = VF.analyze(data)
    if again.model_dump_json() != report.model_dump_json():
        complain(name, "同一份字节两次分析结果不一致（报告里有不确定成分）")

    # -- 不变量 7：数字不许自相矛盾（`facts` 在解码层，报告层不含它） --------
    size = len(data)
    facts = VF.decode_video(data).facts
    if facts.appended_bytes and facts.appended_bytes > size:
        complain(name, f"尾部多余数据 {facts.appended_bytes} > 文件总长 {size}")
    if facts.actual_size and facts.actual_size > size:
        complain(name, f"actual_size {facts.actual_size} > 实际字节 {size}")
    if report.duration_seconds is not None and report.duration_seconds < 0:
        complain(name, f"时长为负：{report.duration_seconds}")
    if facts.declared_size is not None and facts.declared_size < 0:
        complain(name, "声明长度为负")
    if report.byte_size != size:
        complain(name, f"报告里的 byte_size={report.byte_size} 与实际 {size} 不符（溯源字段错了）")
    if report.frame_count is not None and report.frame_count < 0:
        complain(name, f"帧数为负：{report.frame_count}")


def main() -> None:
    samples = {
        "clean": V.clean_mp4(),
        "moov_last": V.moov_after_mdat(),
        "two_mdats": V.two_mdats(),
        "trailing": V.trailing_garbage(64),
        "elst": V.with_elst_skip(),
        "fps": V.frame_rate_jump(),
        "dur": V.duration_mismatch(),
        "cover": V.with_cover(b"\xff\xd8\xff" + b"\x00" * 32),
        "gif": V.animated_gif(3),
        "gif_real": V.rasterizable_gif(3),
        "mjpeg": V.motion_jpeg_mp4([b"\xff\xd8\xff" + b"\x00" * 40] * 2),
        "free": V.free_padding(64),
    }

    rng = random.Random(20261003)
    base = V.clean_mp4()
    mutations: dict[str, bytes] = {}
    for i in range(60):
        b = bytearray(base)
        kind = i % 3
        if kind == 0 and len(b) > 12:                      # 随机截断
            b = b[: rng.randrange(8, len(b))]
        elif kind == 1:                                     # 随机字节翻转
            for _ in range(rng.randrange(1, 6)):
                pos = rng.randrange(0, len(b))
                b[pos] = rng.randrange(0, 256)
            b = b
        else:                                               # 随机插点儿 things
            pos = rng.randrange(0, max(1, len(b)))
            b[pos:pos] = bytes(rng.randrange(0, 32) for _ in range(rng.randrange(1, 12)))
        mutations[f"mut{i:02d}"] = bytes(b)

    print(f"样本 {len(samples)} 个 + 变异 {len(mutations)} 个")
    for name, data in {**samples, **mutations}.items():
        check_sample(name, data)

    print()
    if PROBLEMS:
        print(f"⚠️ 发现 {len(PROBLEMS)} 处违规：")
        for p in PROBLEMS[:40]:
            print("  ·", p)
    else:
        print("✅ 全部字节（含变异）都满足七条不变量")


if __name__ == "__main__":
    main()
