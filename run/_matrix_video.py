"""临时样本矩阵（用完即删）—— 只为确认 `duration_mismatch` 修好没有。"""

from __future__ import annotations

from legal_acps.video import forensics
from run import video_samples as vs

CASES = {
    "clean": lambda: vs.clean_mp4(),
    "dur_mismatch": lambda: vs.duration_mismatch(),
    "fps_jump": lambda: vs.frame_rate_jump(),
    "elst_empty": lambda: vs.with_elst_skip(),
    "trailing": lambda: vs.trailing_garbage(),
    "moov_last": lambda: vs.moov_after_mdat(),
    "two_mdats": lambda: vs.two_mdats(),
    "free": lambda: vs.free_padding(),
    "gif3": lambda: vs.animated_gif(3),
    "not_video": vs.not_a_video,
}

for name, make in CASES.items():
    data = make()
    try:
        report = forensics.analyze(data)
    except ValueError as exc:
        print(f"{name:14s} 拒绝：{exc}")
        continue
    triggered = [f.check for f in report.findings if f.verdict == "提示异常"]
    abstained = [f.check for f in report.findings if f.verdict == "无法判定"]
    print(f"{name:14s} 等级={report.suspicion} 触发={triggered} 弃权={abstained}")
    if name == "dur_mismatch":
        for f in report.findings:
            if f.check == "timeline":
                print("   timeline detail:", f.detail[:400])
