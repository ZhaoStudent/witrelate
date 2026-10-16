"""离线验证：音频鉴伪初检（`legal_acps/audio/`）。

**不联网、不需要 API key、不碰平台、不调模型** —— 全部是确定性纯函数的断言。
判据只有一个：`exit 0 = 全绿`。

用法（在 witrelate 目录下）：
    ./.venv/Scripts/python.exe -m tools.verify_audio

════════════════════════════════════════════════════════════════════════
它到底在测什么
════════════════════════════════════════════════════════════════════════

**负对照比正样本重要。** 一个鉴伪工具最危险的失败不是漏报，是误报：
它会把一段正常录音说成"有剪辑嫌疑"，而使用者没有任何手段复核我们。

本链还有一个**图像链没有的**误报形态，而且它比误报更隐蔽：

    「某项不适用」被写成了「整份报告测不了」。

音频链的弃权比例天然比图像链高（压缩格式解不出波形、纯音谈不了频谱截止、
ENF 在真实录音里本来就常常检不出）。所以"某一项没跑"是常态；如果它具备把
整份报告压成「无法判定」的权力，那么**每一段正常录音都是「无法判定」**，
报告彻底失去信息量，用户只会绕开它。第 2 节与第 4 节就是为这件事写的。

节次安排：

  第 1 节  契约：九项齐全、每条自带 caveat、取值域里没有「真 / 假」
  第 2 节  **负对照（最重要的一节）**：三个干净样本必须都得到「低」
  第 3 节  正样本：该抓的抓到，报出的量值就是真实量值
  第 3b 节 「没做过的事」不许写成正面结论（本轮修的缺陷）
  第 4 节  弃权的两层：材料级闸门 ≠ 逐项不适用（**本链最核心的设计**）
  第 5 节  出口同源：`summary_line()` 不得与 `summarize()` 的判断互相矛盾
  第 6 节  取件族：**逐码**都有专属出路，且每条拒绝都真的能构造出来
  第 7 节  字节族：工具入口一个异常都不许漏，且两族的建议不许串台
  第 8 节  ENF 轨迹与判据同源，且**必须从包顶层可达**（本轮修的那个缺陷）
  第 9 节  复检阶梯不随等级裁剪 + 上限不变量
  第 10 节 接线：模态画像 + `evidence-audio` 挂的收件 Hook 真的收音频
"""

from __future__ import annotations

import base64
import hashlib
import re
import sys
import tempfile
from pathlib import Path

from legal_acps.audio import (
    FETCH_REFUSAL_KINDS,
    GATE_ABSTAIN_PREFIX,
    MAX_AUDIO_BYTES,
    NON_AUDIO_CLAUSE_PREFIX,
    NOT_APPLICABLE_PREFIX,
    analyze,
    check_policy,
    decode_audio,
    enf_track,
    fix_for,
    fix_for_input,
    is_supported_audio,
    load,
    non_audio_clause,
    normalize_mime,
    outcome,
    plan_upgrades,
    sha256_of,
    summarize,
    summary_line,
)
from legal_acps.audio import decode as decode_mod
from legal_acps.audio.decode import MAX_ANALYSIS_FRAMES
from legal_acps.audio.fetch import ALLOWED_SCHEMES, AudioFetchRefused, classify_uri
from legal_acps.audio.forensics import CHECK_WEIGHTS, MIN_WAVEFORM_SECONDS
from legal_acps.contracts import EvidenceModality
from legal_acps.evidence import inbox
from legal_acps.evidence.contracts import Finding
from legal_acps.verdict import ABSTAIN, ABNORMAL
from run import audio_samples as S

FAILED: list[str] = []

#: `analyze` 结果缓存。同一条音轨会被好几节反复取报告，而波形域里最贵的是
#: FFT（谱截止 / 谱突变 / ENF 各来一遍）。不缓存时整体运行时间成倍上升，
#: 而元验证与真机复跑都要反复执行本脚本。
#:
#: ⚠️ 键用**完整 sha256** 而不是 `hash()`：后者是 64 位、且没有跨进程保证，
#: 万一撞一次就会让某条断言拿到**另一条音轨的报告** —— 测试给出的答案会错得
#: 无声无息（图像侧的 `verify_evidence.py` 里写着同一条理由）。
_CACHE: dict[tuple[str, tuple[tuple[str, str], ...]], object] = {}


def analyzed(data: bytes, **kw: object):  # type: ignore[no-untyped-def]
    key = (hashlib.sha256(data).hexdigest(), tuple(sorted((k, str(v)) for k, v in kw.items())))
    if key not in _CACHE:
        _CACHE[key] = analyze(data, **kw)  # type: ignore[arg-type]
    return _CACHE[key]


def attempt(fn, *args: object, **kwargs: object) -> tuple[object, Exception | None]:
    """把「抛出来的异常」也折成**可比较的返回值**。

    ⚠️ 这是本文件的第一条纪律：被测代码抛异常时，断言脚本会带着 traceback 崩掉，
    而"崩掉（少打几行）"与"通过"在日志里几乎分不出来。把异常折成返回值以后，
    它会走正常的 `check()` 通道，被报成一条**明确的失败**。
    """
    try:
        return fn(*args, **kwargs), None  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 - 边界：测试自己必须先站得稳
        return None, exc


def check(label: str, ok: bool, *notes: str) -> None:
    mark = "通过" if ok else "未达预期"
    if not ok:
        FAILED.append(label)
    print(f"  [{mark}] {label}")
    for note in notes:
        print(f"          {note}")


def fired(report) -> set[str]:  # type: ignore[no-untyped-def]
    """触发集 = 判定为「提示异常」的检查项名。"""
    return {f.check for f in report.findings if f.verdict == ABNORMAL}


def gated(report) -> set[str]:  # type: ignore[no-untyped-def]
    """材料级闸门：`GATE_ABSTAIN_PREFIX` 那一族（整个波形域没跑）。"""
    return {
        f.check for f in report.findings
        if f.verdict == ABSTAIN and str(f.detail).startswith(GATE_ABSTAIN_PREFIX)
    }


def skipped(report) -> set[str]:  # type: ignore[no-untyped-def]
    """逐项弃权：`NOT_APPLICABLE_PREFIX` 那一族（这一项对这条音轨不适用）。"""
    return {
        f.check for f in report.findings
        if f.verdict == ABSTAIN and str(f.detail).startswith(NOT_APPLICABLE_PREFIX)
    }


def _mk(check_name: str, verdict: str, detail: str) -> Finding:
    """造一条发现。**在内部补全基线字段**。

    ⚠️ `caveat` 是必填的：手搓 `Finding` 时漏一个字段会直接 `ValidationError`，
    而那种失败在断言脚本里表现为"恒 False + 一句指向无关方向的理由"
    （图像侧的 `verify_evidence_leader.py` 踩过这个坑）。
    """
    return Finding(
        check=check_name, verdict=verdict,  # type: ignore[arg-type]
        detail=detail, caveat="构造用；本用例不考察这一句。",
    )


# ---------------------------------------------------------------------------
# 样本（**全部取自 run/audio_samples.py**，断言脚本不另造一份）
# ---------------------------------------------------------------------------

N_NOISE = S.wav_bytes(S.white_noise(3.0, level=0.05, seed=1))
N_SPEECH = S.wav_bytes(S.speech_like(3.0))
N_TONE = S.wav_bytes(S.tone(3.0, 440.0))
N_TOO_SHORT = S.wav_bytes(S.white_noise(0.4, seed=6))
N_NEAR_SILENT = S.wav_bytes(S.white_noise(3.0, level=1e-9, seed=6))

P_SILENCE = S.wav_bytes(S.with_digital_silence(S.speech_like(3.0)))
P_SPLICE = S.wav_bytes(S.spliced_noise_floors())
P_CUTOFF = S.wav_bytes(S.hard_cutoff(S.white_noise(3.0, level=0.05, seed=2), cutoff=10000.0))
P_ENF_CLEAN = S.wav_bytes(S.mains_hum(6.0, jump_at=None))
P_ENF_JUMP = S.wav_bytes(S.mains_hum(6.0, jump_at=3.0))
P_MP3_SRJUMP = S.mp3_bytes(40, tail_from=20, tail_sr=48000)

WAV_SMALL = S.wav_bytes(S.white_noise(1.0, seed=5))
WAV_APPENDED = S.append_bytes(WAV_SMALL, b"junk-1234")
WAV_TRUNCATED = S.truncate_bytes(WAV_SMALL, 2000)
WAV_BAD_FMT = S.mismatched_fmt(S.wav_bytes(S.white_noise(2.0, seed=9)), bits=24)
#: 落单字节（data 长度是奇数）：一条"少分析了一点东西"的事实，必须说得出。
WAV_ODD_TAIL = S.wav_odd_tail()
#: `fmt` 头不合法（channels=0 / byte_rate=0 / block_align=0）：**一次对照都做不了**。
WAV_CHANNELS_ZERO = S.wav_channels_zero()
#: FLAC：读得到采样率 / 声道 / 位深，但**读不到**字节率 / 块对齐 / 数据长度。
FLAC_MIN = S.flac_bytes()
#: 0.62 秒：过得了 0.6 秒的材料级闸门，但扣掉首尾各 0.3 秒保护带之后，
#: 数字静音项的**可检查内部区间**不足一次判据（50 毫秒）⇒ 必须弃权。
WAV_EDGE_ONLY = S.wav_bytes(S.white_noise(0.62, seed=4))

MP3_CONST = S.mp3_bytes(40)
#: 声明成 WAV 的 MP3 —— **闸门与正向触发同时出现**的唯一形态。
MP3_AS_WAV = MP3_CONST
NOT_AUDIO = b"this is definitely not audio, pad pad pad pad"

#: 九项检查的名字与顺序。**这是"报告写了哪些项"的唯一判据** ——
#: 少一项、或者顺序变了，都必须立刻报出来（顺序变了意味着汇总口径变了）。
EXPECTED_CHECKS = [
    "container",
    "codec_params",
    "metadata",
    "spectral_cutoff",
    "noise_floor",
    "silence",
    "spectral_flux",
    "sample_discontinuity",
    "enf",
]

VERDICT_DOMAIN = {"未提示异常", "提示异常", "无法判定"}
LEVEL_DOMAIN = {"低", "中", "高", "无法判定"}

_LEVEL_RE = re.compile(r"综合线索等级：([^。]+)。")


def _level_in(text: str) -> str | None:
    """从一句话摘要里把等级抠出来（用来做"两处措辞同源"的比较）。"""
    m = _LEVEL_RE.search(text)
    return m.group(1) if m else None


def _uri_of(data: bytes, mime: str = "audio/wav") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def main() -> int:  # noqa: PLR0915 - 逐节展开，便于逐条回读断言的理由
    # =======================================================================
    print("=" * 74)
    print("第 1 节 · 契约：九项齐全、每条自带 caveat、取值域里没有「真 / 假」")
    print("=" * 74)
    # =======================================================================

    report, err = attempt(analyzed, N_SPEECH)
    check("谐波语音样本能产出报告（不抛异常）", err is None, f"异常：{err!r}")
    check(
        "检查项恰好是约定的九项，且顺序不变",
        [f.check for f in report.findings] == EXPECTED_CHECKS,
        f"实得：{[f.check for f in report.findings]}",
    )
    check(
        "每一项都自带非空 caveat（写明「它不能证明什么」）",
        all(len(str(f.caveat).strip()) >= 8 for f in report.findings),
        f"最短的一条：{min((len(str(f.caveat)) for f in report.findings), default=0)} 字",
    )
    check(
        "逐项结论落在固定取值域内（域里没有「真 / 假」这一对）",
        all(f.verdict in VERDICT_DOMAIN for f in report.findings),
        f"实得：{sorted({f.verdict for f in report.findings})}",
    )
    check(
        "综合等级落在固定取值域内",
        report.suspicion in LEVEL_DOMAIN,
        f"实得：{report.suspicion}",
    )
    check(
        "SHA-256 / 字节数由代码复算，与源字节一致",
        report.sha256 == sha256_of(N_SPEECH) and report.byte_size == len(N_SPEECH),
        f"报告 {report.sha256[:16]}… / {report.byte_size} 字节",
    )
    check(
        "效力声明在位（报告不得自称鉴定意见）",
        "不是司法鉴定意见" in report.disclaimer and bool(report.chain_of_custody),
        f"保全提示 {len(report.chain_of_custody)} 条",
    )
    print()

    # =======================================================================
    print("=" * 74)
    print("第 2 节 · 负对照（**最重要的一节**）：干净样本必须得到「低」")
    print("=" * 74)
    # =======================================================================
    print("  说明：三个样本都是「没有剪辑痕迹」的合成信号。它们里面必然有若干项")
    print("        因为「这条音轨上无从检验」而弃权（白噪没有底噪更低的安静期、")
    print("        合成信号里没有市电痕迹）。这正是本链最危险的失败形态：")
    print("        **某一项不适用，不等于整份报告测不了。**")
    print()

    for name, data in (
        ("白噪（宽带、电平恒定）", N_NOISE),
        ("谐波语音（有包络起伏）", N_SPEECH),
        ("纯音 440 Hz（窄带）", N_TONE),
    ):
        rep, err = attempt(analyzed, data)
        check(f"负对照 · {name} → 综合等级「低」", getattr(rep, "suspicion", None) == "低",
              f"实得：{getattr(rep, 'suspicion', None)!r}，异常：{err!r}")
        check(f"负对照 · {name} → 零触发（一项都不许报异常）", fired(rep) == set(),
              f"触发：{sorted(fired(rep))}")
        check(f"负对照 · {name} → **不出现材料级闸门**（否则整份报告会被压成无法判定）",
              gated(rep) == set(), f"闸门项：{sorted(gated(rep))}")

    rep_tone = analyzed(N_TONE)
    check(
        "纯音同时存在「逐项弃权」与「等级照常给出」—— 成对断言",
        bool(skipped(rep_tone)) and rep_tone.suspicion == "低",
        f"逐项弃权 {sorted(skipped(rep_tone))}；等级 {rep_tone.suspicion}",
        "（若这两项被合并成一种弃权，本行会变成 无法判定 —— 那正是要挡的回归）",
    )
    print()

    # =======================================================================
    print("=" * 74)
    print("第 3 节 · 正样本：该抓的抓到，且报出的量值就是真实量值")
    print("=" * 74)

    rep = analyzed(P_SILENCE)
    sil = next(f for f in rep.findings if f.check == "silence")
    check("数字静音段 → 综合「中」，且 silence 触发", rep.suspicion == "中" and "silence" in fired(rep),
          f"等级 {rep.suspicion}；触发 {sorted(fired(rep))}")
    check("数字静音段：报出的区间与时长就是造样本时写死的 1.20s–1.60s（0.40 秒）",
          "1.20s" in sil.detail and "1.60s" in sil.detail and "0.40 秒" in sil.detail,
          f"detail：{sil.detail[:80]}")

    rep = analyzed(P_SPLICE)
    nf = next(f for f in rep.findings if f.check == "noise_floor")
    check("两段不同底噪拼接 → 综合「中」，且 noise_floor 触发",
          rep.suspicion == "中" and "noise_floor" in fired(rep),
          f"等级 {rep.suspicion}；触发 {sorted(fired(rep))}")
    check("底噪一致性：报出的差值就是样本里的 20 dB（安静 -54 / 响亮 -34 dBFS）",
          "20.0 dB" in nf.detail and "-54.0" in nf.detail and "-34.0" in nf.detail,
          f"detail：{nf.detail[:90]}")

    rep = analyzed(P_CUTOFF)
    check("10 kHz 硬截止 → spectral_cutoff 触发，但**等级仍是「低」**",
          rep.suspicion == "低" and "spectral_cutoff" in fired(rep),
          f"等级 {rep.suspicion}；触发 {sorted(fired(rep))}",
          "（单条 0.5 权重的线索不足以升级 —— 报「低」是正确行为，不是漏报）")

    rep = analyzed(P_ENF_JUMP)
    check("市电相位跳变 → 综合「中」，且 enf 触发",
          rep.suspicion == "中" and "enf" in fired(rep),
          f"等级 {rep.suspicion}；触发 {sorted(fired(rep))}")
    rep_clean = analyzed(P_ENF_CLEAN)
    check("市电相位连续 → enf **不触发**（同一造法、只差一个相位翻转）",
          "enf" not in fired(rep_clean) and rep_clean.suspicion == "低",
          f"等级 {rep_clean.suspicion}；触发 {sorted(fired(rep_clean))}")

    rep = analyzed(P_MP3_SRJUMP)
    cp = next(f for f in rep.findings if f.check == "codec_params")
    check("MP3 采样率跳变 → 综合「中」，且 codec_params 触发",
          rep.suspicion == "中" and "codec_params" in fired(rep),
          f"等级 {rep.suspicion}；触发 {sorted(fired(rep))}")
    check("编码参数：报出的两种采样率就是样本里的 44100 / 48000",
          "44100" in cp.detail and "48000" in cp.detail, f"detail：{cp.detail[:80]}")

    for name, data, expect in (
        ("尾部附加数据", WAV_APPENDED, "9 字节"),
        ("截断", WAV_TRUNCATED, "截断"),
    ):
        rep = analyzed(data)
        ct = next(f for f in rep.findings if f.check == "container")
        check(f"容器层 · WAV + {name} → container 触发（等级仍是「低」）",
              "container" in fired(rep) and rep.suspicion == "低",
              f"等级 {rep.suspicion}；detail：{ct.detail[:70]}")

    rep = analyzed(WAV_BAD_FMT)
    check("位深声明与块对齐不一致 → codec_params 触发（能解出波形，所以**不是**闸门）",
          "codec_params" in fired(rep) and gated(rep) == set(),
          f"等级 {rep.suspicion}；触发 {sorted(fired(rep))}；闸门 {sorted(gated(rep))}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 3b 节 · 「没做过的事」不许写成正面结论（本轮修的缺陷）")
    print("=" * 74)
    print("  ⚠️ 本节的每一条都对应一个**看起来完全正常**的缺陷：报告给出了一句")
    print("     很具体的正面结论，而那句话说的那件事其实没做。")

    # -- 编码参数：一次对照都没做过 -------------------------------------------
    rep_flac = analyzed(FLAC_MIN)
    cp_flac = next(f for f in rep_flac.findings if f.check == "codec_params")
    check("FLAC：编码参数**一次对照都做不了** ⇒ 弃权，且**不许**出现「内部自洽」",
          cp_flac.verdict == ABSTAIN
          and str(cp_flac.detail).startswith(NOT_APPLICABLE_PREFIX)
          and "内部自洽" not in cp_flac.detail,
          f"verdict={cp_flac.verdict}；detail={cp_flac.detail[:70]}")
    check("……FLAC 的 `byte_rate` / `block_align` / `data_bytes` 确实**一个都没有**"
          "（这才是上面那条弃权的理由；三个字段只要有一个能读到，就该真的去比）",
          decode_audio(FLAC_MIN).facts.byte_rate is None
          and decode_audio(FLAC_MIN).facts.block_align is None
          and decode_audio(FLAC_MIN).facts.data_bytes is None,
          "（若有哪个字段其实读得到，本条会失败 —— 那时该改的是判据，不是断言）")
    check("……FLAC 整份报告的等级是「无法判定」（波形域六项全灭 + 容器层读不到参数）",
          rep_flac.suspicion == ABSTAIN,
          f"等级 {rep_flac.suspicion}；闸门 {len(gated(rep_flac))} 项")

    rep_c0 = analyzed(WAV_CHANNELS_ZERO)
    cp_c0 = next(f for f in rep_c0.findings if f.check == "codec_params")
    check("WAV 的 `fmt` 里 channels=0 ⇒ 同样弃权，**不许**说「字节率 / 块对齐 / 数据长度相互吻合」",
          cp_c0.verdict == ABSTAIN
          and str(cp_c0.detail).startswith(NOT_APPLICABLE_PREFIX)
          and "相互吻合" not in cp_c0.detail,
          f"verdict={cp_c0.verdict}；detail={cp_c0.detail[:70]}")

    cp_ok = next(f for f in analyzed(WAV_SMALL).findings if f.check == "codec_params")
    check("正向对照：正常 WAV 的「内部自洽」必须**点名本次真的核对过的那几项**"
          "（文案由 `compared` 现场派生，不是写死一句话）",
          cp_ok.verdict != ABSTAIN and "内部自洽" in cp_ok.detail
          and "块对齐" in cp_ok.detail and "字节率" in cp_ok.detail
          and "数据长度" in cp_ok.detail,
          f"detail={cp_ok.detail[:80]}")

    # -- 数字静音：扣掉保护带之后还剩多少可查区间 -----------------------------
    rep_tiny = analyzed(WAV_EDGE_ONLY)
    sil_tiny = next(f for f in rep_tiny.findings if f.check == "silence")
    check("0.6 秒出头的音轨：扣掉首尾各 0.3 秒保护带后**放不下一次判据** ⇒ 弃权，"
          "**不许**说「没有发现数字静音段…这符合连续录音的形态」"
          "（那是「一次没查」，不是「查过没问题」）",
          sil_tiny.verdict == ABSTAIN
          and str(sil_tiny.detail).startswith(NOT_APPLICABLE_PREFIX)
          and "符合连续录音的形态" not in sil_tiny.detail,
          f"verdict={sil_tiny.verdict}；detail={sil_tiny.detail[:70]}")
    sil_long = next(f for f in analyzed(WAV_SMALL).findings if f.check == "silence")
    check("……正向对照：1 秒的音轨内部区间够长 ⇒ 照常给结论（不许把整项变成永远弃权）",
          sil_long.verdict != ABSTAIN, f"verdict={sil_long.verdict}")

    # -- 落单字节：说出来了没有 -----------------------------------------------
    decoded_odd = decode_audio(WAV_ODD_TAIL)
    rep_odd = analyzed(WAV_ODD_TAIL)
    check("WAV 的 data 长度不是帧长整数倍 ⇒ 「末尾 1 字节被丢弃」必须**进得了 facts**"
          "（它原先 append 进的列表已经拷进 facts ⇒ 永远到不了报告）",
          any("1 字节被丢弃" in n for n in decoded_odd.facts.notes),
          f"facts.notes={decoded_odd.facts.notes}")
    check("……而且必须**看得见**：出现在 `pcm_note` / `analysis_window` 里"
          "（`facts.notes` 不是报告字段 —— 只写进那里等于没写）",
          "1 字节被丢弃" in rep_odd.pcm_note and "1 字节被丢弃" in rep_odd.analysis_window,
          f"pcm_note={rep_odd.pcm_note[:80]}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 3c 节 · **「谁算音频」只有一个判据**：接受集合跟着容器表现场长出来")
    print("=" * 74)
    print("  ⚠️ 2026-10-05 体检测出的三个真缺陷，全都是同一个病的三个面：")
    print("     「认得」与「收不收」写在了两处，或者干脆只写了一处。")
    print("     ① `audio/ac3` **在解码层的容器表里**（配了中文名「AC-3」），"
          "而 `sniff_media_type()`")
    print("        **没有任何分支能返回它** —— 一个**死键**。真的 `.ac3` 因此被"
          " `analyze()` 拒掉，")
    print("        拒绝理由是一句假话「不是可识别的音频容器」（判定结果是 "
          "`application/octet-stream`）。")
    print("     ② `.wma` 在业界通用的 MIME 是 `audio/x-ms-wma`，而别名表里没有它 ⇒")
    print("        一份**声明正确**的 WMA 会吃到「声明与真实容器不一致」的**假线索**，"
          "综合等级")
    print("        还被从「无法判定」抬到「低」（与视频侧 `video/ogg` vs "
          "`application/ogg` 同病）。")
    print("     ③ 接受闸门是 `startswith(\"audio/\")` 这个**代理判据**，"
          "而不是「解码层认不认得」。")
    print("        ⚠️ 音频这张表的键**全部**带 `audio/` 前缀，所以换掉它"
          "**当且仅当**是结构保险；")
    print("        视频那张表里有 `application/ogg` ⇒ 同一个写法真的会把 Ogg 挡在门外。")
    print("  ⇒ 判据收进 `forensics.is_supported_audio()`（唯一入口）；"
          "本节按**容器表现场派生**地验它。")
    print()

    # ── ① 接受集合：逐个键现场派生（以后加容器不许漏）─────────────────────
    all_containers = list(decode_mod._OTHER_CONTAINERS)
    unsupported = [mt for mt in all_containers if not is_supported_audio(mt)]
    check(f"接纳判据覆盖解码层容器表的**每一个**容器（现场派生：现有 "
          f"{len(all_containers)} 个）",
          not unsupported, f"未被接纳：{unsupported}")
    check("……而**认不出来**的字节仍然一律不接纳"
          "（负对照：判据没被放宽成「什么都收」）",
          not any(is_supported_audio(mt) for mt in
                  ("application/octet-stream", "application/pdf", "text/plain", "")),
          "octet-stream / pdf / text / 空串都必须不接纳")
    bad_alias = [(mt, normalize_mime(mt)) for mt in all_containers
                 if normalize_mime(mt) != mt]
    check("……每个容器的**规范名都归一到它自己**（别名表把规范名指到别处 ⇒ "
          "每一份文件都会凭空报一条「声明与真实容器不一致」）",
          not bad_alias, f"归一异常：{bad_alias}")

    # ── ② 表里的每个键都必须**真的到得了**（这一条直接逮住 ①）──────────────
    # ⚠️ 只断言 `is_supported_audio()` 返回 True 是**不够的**：那只证明"表里有它"，
    #    不证明"魔术字节真能走到那个键"。`audio/ac3` 当初就是"表里有、路不通"。
    #    所以这里逐个造魔术字节、喂进 `decode_audio()`，键必须**逐字相等**。
    other_samples = {
        "audio/flac": b"fLaC" + bytes(300),
        "audio/ogg": b"OggS\x00\x02" + bytes(300),
        "audio/mp4": bytes(4) + b"ftypM4A " + bytes(300),
        "audio/aiff": b"FORM" + (300).to_bytes(4, "big") + b"AIFF" + bytes(300),
        "audio/amr": b"#!AMR\n" + bytes(300),
        # ASF 头（.wma / .wmv 共用这一个 GUID）
        "audio/wma": b"0\x26\xb2\x75\x8e\x66\xcf\x11" + bytes(300),
        # AC-3 的帧同步字 0x0B77
        "audio/ac3": b"\x0b\x77" + bytes(300),
    }
    check("七个容器**逐个**造得出魔术字节（否则下面那组断言是空转）",
          sorted(other_samples) == sorted(decode_mod._OTHER_CONTAINERS),
          f"表={sorted(decode_mod._OTHER_CONTAINERS)}；样本={sorted(other_samples)}")
    unreachable = [mt for mt, sample in other_samples.items()
                   if decode_audio(sample).media_type != mt]
    check("**表里每一个键都必须真的有嗅探分支**（死键的后果是：本层宣称「认得」，"
          "真文件却被拒，理由还是假话）",
          unreachable == [],
          "、".join(f"{mt} → {decode_audio(other_samples[mt]).media_type}"
                   for mt in unreachable) or "七个键全部可达")

    # ── ③ 每个容器：出报告 + 说清哪几层没跑 + 等级必须是「无法判定」─────────
    # ⚠️ 与视频侧第 3c 节同一条纪律：**认得 ≠ 看过里面**。
    # ⚠️ 这里**不能**一律断言「没有解析它的内部结构」：七个键里有解析器的（FLAC）走的是
    #    解析那一条路，写死就成了一条打错地方的判据。
    #    也**不能**读 `facts.structure_parsed` 来决定该怎么断 —— 那是**拿被测对象
    #    自己的标记去校准判据**：标记错成 `True` 时判据会跟着一起错，照样全绿
    #    （2026-10-05 第一版正是这么写的，重读时自己抓出来的）。
    #    所以期望从**报告里另一个字段**取：容器项的准话必须与 `codec_params` 的说法
    #    一致 —— 「这一项都没解析出参数」与「容器自洽性相符」是同一次解析的两种说法，
    #    它们互相矛盾就说明有人把"没做过的检查"写成了正面结论。
    for media_type, sample in other_samples.items():
        got, err = attempt(analyze, sample, declared_mime=media_type)
        check(f"{media_type} · 认得就该**出报告**（判据是「认不认得」，"
              "不是「媒体类型前缀对不对」）",
              err is None and got is not None and got.media_type == media_type,
              f"异常={err!r}；media_type={getattr(got, 'media_type', None)!r}")
        if err is not None or got is None:
            continue
        found = next(f for f in got.findings if f.check == "container")
        codec = next(f for f in got.findings if f.check == "codec_params")
        detail = str(found.detail)
        claimed_consistency = "长度与实际字节**相符**" in detail
        parsed_params = "没有解析出编码参数" not in str(codec.detail)
        check(f"　└ {media_type} · 「容器自洽性」的说头必须与**报告里另一个字段**一致："
              "这次连编码参数都没解出来 ⇒ **不许**声称「声明的长度与实际字节相符」"
              "（那三项检查一次都没跑；把没做过的检查写成正面结论是本仓最危险的缺陷）",
              parsed_params or not claimed_consistency,
              f"容器项={detail[:70]}；参数项={str(codec.detail)[:50]}")
        if not parsed_params:
            # ⚠️ 措辞**不写死一种**：有解析器的（MP3 / FLAC）说「没有可比的长度声明」，
            #    只识别的说「没有解析它的内部结构」—— 两句都对，硬要统一反而会
            #    逼着实现去说一句假话。要钉住的是「**必须给出理由**」，不是某个词。
            check(f"　└ {media_type} · 既然没解析出来，就得**说出为什么**"
                  "（不许留白 —— 读者要能一眼看出哪几层没跑）",
                  "没有解析它的内部结构" in detail or "没有可比的长度声明" in detail,
                  f"detail={detail[:90]}")
        check(f"　└ {media_type} · 综合等级必须是**无法判定**"
              "（一层都没解析就给等级 = 静默给结论）",
              got.suspicion == ABSTAIN, f"等级={got.suspicion}")

    # ⚠️ 上面那一对是「同一份报告的**两个字段**必须互相对得上」—— 但还要一条正向对照，
    #    否则"把那句话从所有容器上删掉"也会让上面全绿（删掉 ≠ 讲清楚）。
    wav_clean = analyzed(N_NOISE)
    wav_container = next(f for f in wav_clean.findings if f.check == "container")
    check("正向对照：**解析过**的 WAV（内容干净）仍然照原样说"
          "「声明的长度与实际字节相符」—— 修的是「没做过的检查」，不是把这句话删掉",
          "声明的长度与实际字节**相符**" in str(wav_container.detail),
          f"detail={str(wav_container.detail)[:90]}")
    # ⚠️ 同一处修复的**第二个说法**（metadata 那句「这种容器本次也没有解析内部标签」）：
    #    它对 WAV 来说是**假话**（WAV 的内部标签查过了，只是没有可读的）。
    #    同一字段、两种输入、两句不同的话 —— 成对钉住，否则它会悄悄退回原样。
    wav_meta = next(f for f in wav_clean.findings if f.check == "metadata")
    ogg_meta = next(f for f in analyze(other_samples["audio/ogg"]).findings
                    if f.check == "metadata")
    check("……metadata 那句也要分清：**解析过、但没读出署名** ≠ **压根没解析内部标签**"
          "（对 WAV 说「本次没有解析内部标签」是假话，反之亦然）",
          "没有读出**可读标签" in str(wav_meta.detail)
          and "本次也没有解析内部标签" not in str(wav_meta.detail)
          and "本次也没有解析内部标签" in str(ogg_meta.detail)
          and "没有读出**可读标签" not in str(ogg_meta.detail),
          f"WAV：{str(wav_meta.detail)[:60]}；Ogg：{str(ogg_meta.detail)[:60]}")

    # ⚠️ 同一处修复的**第三种形态**：MP3 / FLAC **有解析器**，但它们**不在文件头里写总长**
    #    （实测 `declared_size is None`），所以"声明长度 vs 实际"这项检查**同样没得比**。
    #    只按"有没有解析器"分档会漏掉这两个 —— 判据因此按**实测事实**写死成对的两句。
    for mt, sample, expect in (("audio/mpeg", MP3_CONST, False),
                               ("audio/flac", b"fLaC" + bytes(300), False)):
        rep = analyzed(sample)
        cont = next(f for f in rep.findings if f.check == "container")
        check(f"{mt} · 头部**不写总长** ⇒ 不许说「声明的长度与实际字节相符」，"
              "要说「没有可比的长度声明」",
              ("长度与实际字节**相符**" in str(cont.detail)) == expect
              and "没有可比的长度声明" in str(cont.detail),
              f"detail={str(cont.detail)[:80]}")

    # ── ④ 按**标准 MIME** 声明时不许吃假线索（这一条直接逮住 ②）────────────
    # ⚠️ 这一组与上面的差别正是它的价值：上面用的是「嗅探结果」当声明（必然一致），
    #    这里用的是**调用方真会写的那种 MIME** —— 第三方库对 .wma 报的就是
    #    `audio/x-ms-wma`。声明正确却吃到假线索，比少报一条线索更糟：
    #    用户会去改一个本来就对的东西。
    declared_by_std_mime = {
        "audio/flac": "audio/flac",
        "audio/ogg": "audio/ogg",
        "audio/mp4": "audio/mp4",
        "audio/aiff": "audio/aiff",
        "audio/amr": "audio/amr",
        "audio/wma": "audio/x-ms-wma",
        "audio/ac3": "audio/ac3",
    }
    check("七个容器的**标准第三方 MIME** 都列出来了（否则下面那组是空转）",
          sorted(declared_by_std_mime) == sorted(other_samples))
    false_clues = []
    for media_type, decl in declared_by_std_mime.items():
        try:
            rep = analyze(other_samples[media_type], declared_mime=decl)
        except ValueError as exc:  # 认不出来 ⇒ 连报告都没有，这一条更要报出来
            false_clues.append(f"{media_type}：声明 {decl} 竟被拒（{exc}）")
            continue
        found = next(f for f in rep.findings if f.check == "container")
        if found.verdict != "未提示异常":
            false_clues.append(f"{media_type}：声明 {decl} ⇒ {found.detail[:60]}")
        if rep.suspicion != ABSTAIN:
            false_clues.append(f"{media_type}：声明 {decl} ⇒ 等级被抬到 {rep.suspicion}")
    check("按**标准 MIME** 声明时**不许吃假线索**、等级不许被抬高"
          "（「声明与真实容器不一致」是对**正确声明**的指控）",
          false_clues == [], "；".join(false_clues) or "七个容器全部「未提示异常」+「无法判定」")
    check("……「换个写法也认得」：WMA / AC-3 的常见别名都归到同一个容器名",
          normalize_mime("audio/x-ms-wma") == "audio/wma"
          and normalize_mime("audio/x-wma") == "audio/wma"
          and normalize_mime("audio/x-ac3") == "audio/ac3"
          and normalize_mime("audio/vnd.dolby.dd-raw") == "audio/ac3")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 4 节 · 弃权的两层：材料级闸门 ≠ 逐项不适用（**本链最核心的设计**）")
    print("=" * 74)

    rep = analyzed(MP3_CONST)
    check("MP3（无解码器）→ 综合「无法判定」", rep.suspicion == ABSTAIN,
          f"实得：{rep.suspicion}")
    check("MP3 → pcm_available=False 且必须显式出现在报告里", rep.pcm_available is False)
    check("MP3 → 六个波形域检查**整项**没跑，且打的都是材料级闸门前缀",
          gated(rep) == {
              "spectral_cutoff", "noise_floor", "silence",
              "spectral_flux", "sample_discontinuity", "enf",
          },
          f"闸门项：{sorted(gated(rep))}")
    check("MP3 → 没有任何一项是「逐项不适用」（两类弃权不许混）", skipped(rep) == set(),
          f"逐项弃权：{sorted(skipped(rep))}")
    check("MP3 → `pcm_note` 必须写清原因与后果，不能只说「不支持」",
          "没有解出波形" in rep.pcm_note and "整项没有执行" in rep.pcm_note,
          f"pcm_note：{rep.pcm_note[:90]}")
    check("MP3 → 综合理由必须点名「整项没有执行」",
          "整项没有执行" in rep.suspicion_rationale,
          f"rationale：{rep.suspicion_rationale[:90]}")

    rep_short, err_short = attempt(analyzed, N_TOO_SHORT)
    check(f"音轨短于 {MIN_WAVEFORM_SECONDS} 秒 → 材料级闸门（样本不足）",
          err_short is None and rep_short.suspicion == ABSTAIN and bool(gated(rep_short)),
          f"等级 {getattr(rep_short, 'suspicion', None)!r}；闸门 {sorted(gated(rep_short)) if rep_short else '-'}")

    rep_silent, err_silent = attempt(analyzed, N_NEAR_SILENT)
    check("整段近乎无声 → 材料级闸门",
          err_silent is None and rep_silent.suspicion == ABSTAIN and bool(gated(rep_silent)),
          f"等级 {getattr(rep_silent, 'suspicion', None)!r}；闸门 {sorted(gated(rep_silent)) if rep_silent else '-'}")

    # -- 关系断言：两类弃权互斥、且覆盖全部弃权项 ---------------------------
    for name, data in (("MP3", MP3_CONST), ("纯音", N_TONE), ("白噪", N_NOISE)):
        rep = analyzed(data)
        abstained = {f.check for f in rep.findings if f.verdict == ABSTAIN}
        check(f"关系断言 · {name}：闸门 ∩ 逐项 = ∅，且两者并集 = 全部弃权项",
              gated(rep).isdisjoint(skipped(rep)) and (gated(rep) | skipped(rep)) == abstained,
              f"闸门 {len(gated(rep))} 项、逐项 {len(skipped(rep))} 项、弃权共 {len(abstained)} 项")

    # -- **负向成对断言**：把"两层"真的钉死 ---------------------------------
    base = [_mk(c, "未提示异常", "（构造）") for c in EXPECTED_CHECKS if c != "enf"]
    level_na, why_na = summarize(base + [_mk("enf", ABSTAIN, NOT_APPLICABLE_PREFIX + "：没有市电痕迹")])
    check("**成对断言** · 只有一项「逐项不适用」时 → 等级照常给出「低」，**不是**「无法判定」",
          level_na == "低",
          f"实得：{level_na}",
          f"理由：{why_na[:70]}",
          "（ENF 在真实录音里本来就常检不出；若它能把整份报告压成无法判定，"
          "每一段正常录音都会得到「无法判定」）")
    level_gate, _ = summarize(base + [_mk("enf", ABSTAIN, GATE_ABSTAIN_PREFIX + "：没有解出波形")])
    check("**成对断言** · 同一项改成材料级闸门前缀 → 必须变成「无法判定」",
          level_gate == ABSTAIN, f"实得：{level_gate}")
    level_fired, _ = summarize(
        base + [_mk("enf", ABSTAIN, GATE_ABSTAIN_PREFIX + "：没有解出波形"),
                _mk("silence", ABNORMAL, "（构造）数字静音段")]
    )
    check("**成对断言** · 有正向触发时，闸门**不得**把已看到的异常一笔勾销",
          level_fired == "中", f"实得：{level_fired}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 5 节 · 出口同源：`summary_line()` 不得与 `summarize()` 的判断互相矛盾")
    print("=" * 74)
    print("  说明：`summary_for_leader` 是整份报告里**最容易被原样转述给用户的一行**。")
    print("        图像链在这里踩过一次（内部判「无法判定」、摘要说「均未提示异常」）。")
    print()

    corpus = {
        "白噪": N_NOISE,
        "纯音": N_TONE,
        "数字静音": P_SILENCE,
        "两段底噪": P_SPLICE,
        "硬截止": P_CUTOFF,
        "相位跳变": P_ENF_JUMP,
        "MP3（无波形）": MP3_CONST,
        "MP3 采样率跳变": P_MP3_SRJUMP,
        "位深不符": WAV_BAD_FMT,
    }
    for name, data in corpus.items():
        rep = analyzed(data)
        line = summary_line(rep)
        check(f"摘要里的等级与报告的等级一致 · {name}",
              _level_in(line) == rep.suspicion,
              f"报告 {rep.suspicion} / 摘要 {_level_in(line)!r}")

    gated_line = summary_line(analyzed(MP3_CONST))
    check("闸门那一支：**不得**出现「均未提示异常」（那读起来就是「没问题」）",
          "均未提示异常" not in gated_line and "整项没有执行" in gated_line,
          f"摘要：{gated_line[:90]}")
    check("闸门那一支：必须写明「不能据此认为没有问题」",
          "不能据此认为" in gated_line, f"摘要：{gated_line[90:170]}")

    tone_line = summary_line(analyzed(N_TONE))
    check("逐项弃权那一支：必须点名未参与判级的项，且写明「这不等于它们也合格」",
          "无从执行" in tone_line and "这不等于它们也合格" in tone_line,
          f"摘要：{tone_line[:120]}")

    mixed, err_mixed = attempt(analyzed, MP3_AS_WAV, declared_mime="audio/wav")
    check("**闸门 + 正向触发同时出现**：等级照常给出，但摘要必须点名缺席的项",
          err_mixed is None and "container" in fired(mixed) and bool(gated(mixed))
          and "整项没有执行" in summary_line(mixed)
          and "缺席" in mixed.suspicion_rationale,
          f"等级 {mixed.suspicion}；触发 {sorted(fired(mixed))}；闸门 {len(gated(mixed))} 项",
          "（这一组合最容易漏交代：共享判据走的是「有触发就先算账」，闸门理由根本不会被调用）")

    check("两处措辞都由代码产生，摘要里不含「真 / 假」这样的断言",
          ("篡改认定" in summary_line(analyzed(N_NOISE)))
          and ("未经剪辑的认定" in summary_line(analyzed(N_NOISE))),
          f"摘要尾部：{summary_line(analyzed(N_NOISE))[-60:]}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 6 节 · 取件族：**逐码**都有专属出路，且每条拒绝都真的能构造出来")
    print("=" * 74)
    print("  说明：分类码清单**现场派生**（`FETCH_REFUSAL_KINDS`），不手抄 ——")
    print("        本模块初版就是靠「在报错文案里找关键词」配建议的，当场漂移过。")
    print()

    fallback = fix_for("__nonexistent_kind_for_probe__")
    check("兜底建议不是「请联系管理员」（那会把 5 秒能改对的事变成一次上报）",
          "管理员" not in fallback, f"兜底：{fallback[:60]}")

    constructible: dict[str, str] = {
        "unsupported_scheme": "https://example.com/a.wav",
        "data_uri_malformed": "data:audio/wav;base64",
        "data_uri_not_base64": "data:audio/wav,AAAA",
        "data_uri_mime": "data:image/png;base64,AAAA",
        "data_uri_bad_base64": "data:audio/wav;base64,!!!!",
        "data_uri_empty": "data:audio/wav;base64,",
        "inbox_invalid": "inbox:" + "0" * 64,
        "data_uri_too_large": "data:audio/wav;base64," + "A" * (MAX_AUDIO_BYTES * 4 // 3 + 8),
    }
    produced: set[str] = set()
    for kind, uri in constructible.items():
        _, exc = attempt(load, uri)
        got = getattr(exc, "kind", None)
        if isinstance(got, str):
            produced.add(got)
        check(f"拒绝码可构造 · {kind}", got == kind,
              f"实得 kind={got!r}（异常 {type(exc).__name__}）")

    # -- `too_large`：只能走**收件箱**那条路真实构造出来 -----------------------
    # ⚠️ 这一条以前是"结构上不可达"的，而那条论证靠的是**两个数字恰好相等**
    #    （落点 16 MiB = 音频上限 16 MiB ⇒ 收件箱先拒，永远轮不到取用方拒）。
    #    "数量相等"是巧合，不是设计 —— 2026-10 加视频链把落点抬到 64 MiB 供
    #    大文件用，这个码**当场变成真实可达**（16 MiB < 64 MiB）。
    #    与其改判据去迁就旧断言，不如趁机把它真的跑一遍：
    #    这正是"失败路径也要一条条数"要的东西 —— 一个永远走不到的分支，
    #    和不存在没有区别，而它看上去又和"验过了"一模一样。
    too_large_ok = False
    staged_refused = ""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        staged, stage_exc = attempt(
            inbox.stage, b"\x00" * (MAX_AUDIO_BYTES + 1), root=root, mime_type="audio/wav"
        )
        if staged is None:
            staged_refused = f"{type(stage_exc).__name__}: {stage_exc}"
        else:
            token = inbox.bind_root(root)
            try:
                _, exc = attempt(load, staged.handle)
            finally:
                inbox.unbind_root(token)
            got = getattr(exc, "kind", None)
            if isinstance(got, str):
                produced.add(got)
            too_large_ok = got == "too_large"
            check("拒绝码可构造 · too_large（收件箱收得下、取用方吃不下）",
                  too_large_ok,
                  f"实得 kind={got!r}（异常 {type(exc).__name__}）",
                  f"构造方式：inbox 里放 {MAX_AUDIO_BYTES + 1} 字节（+1 就够）")
    if staged_refused:
        check("拒绝码可构造 · too_large", False,
              f"连收件箱都没收下（{staged_refused}）—— 落点上限 ≤ 音频上限时，"
              "这个码是**死分支**：它带着专属建议、却永远不会被触发")

    not_constructed = sorted(set(FETCH_REFUSAL_KINDS) - produced)
    check(
        "**把「能构造出来的码」数一遍**：每一个码都被真实构造出来了（不是「除某个之外」）",
        not_constructed == [],
        f"清单 {len(FETCH_REFUSAL_KINDS)} 个 / 已构造 {len(produced)} 个；未构造：{not_constructed}",
        "（含 too_large —— 它以前靠「落点与取用方上限恰好相等」才不可构造，属于巧合而非设计）",
    )
    # ⚠️ "落点上限 ≥ 取用方上限"这条不变量**不在这里断言**（它的家在下面第 9 节，
    #    而且 `check_local` 会对全部三条链现场派生地钉一遍）。这里由上面那次
    #    真实构造间接证明：stage 收下了 > MAX_AUDIO_BYTES 的字节 ⇒ 落点确实更大。
    #    同一个判定写两处必然漂移，所以这里只构造、不重复声明。

    missing_fix = [k for k in FETCH_REFUSAL_KINDS if not fix_for(k) or fix_for(k) == fallback]
    check("每一个分类码都有**专属**补救建议（不是兜底那一句）",
          missing_fix == [], f"缺专属建议：{missing_fix}")
    rejects = ("https://a.example.com/x.wav", "http://a.example.com/x.wav",
               "file:///etc/passwd", "C:/tmp/x.wav", "./rel.wav", "/abs/x.wav",
               "ftp://a.example.com/x.wav")
    bad = []
    for uri in rejects:
        _, exc = attempt(check_policy, uri)
        if not isinstance(exc, AudioFetchRefused) or getattr(exc, "kind", None) != "unsupported_scheme":
            bad.append(uri)
    check("出网 / 本机路径**一律拒绝**（本链一条出网通道都没有）", bad == [], f"漏放：{bad}")
    check("能力清单只列了两种引用（`.ALLOWED_SCHEMES`）",
          tuple(ALLOWED_SCHEMES) == ("inbox", "data"), f"实得：{ALLOWED_SCHEMES}")

    check("引用归类对大小写不敏感（`DATA:` / `INBOX:` 也算）",
          classify_uri("DATA:audio/wav;base64,AA") == "data"
          and classify_uri("INBOX:" + "A" * 64) == "inbox"
          and classify_uri("file:///x") == "other",
          "DATA:→data；INBOX:→inbox；file://→other")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 7 节 · 字节族：工具入口一个异常都不许漏，且两族的建议不许串台")
    print("=" * 74)

    out_bad, err_bad = attempt(outcome, _uri_of(NOT_AUDIO))
    reason_bad = str(getattr(out_bad, "refusal_reason", None) or "")
    fix_bad = str(getattr(out_bad, "how_to_fix", None) or "")
    check("非音频字节 → `status=refused`，不是抛异常",
          getattr(out_bad, "status", None) == "refused" and err_bad is None,
          f"status={getattr(out_bad, 'status', None)!r}，异常={err_bad!r}")
    check("非音频字节 → 原因里说清「按魔术字节判定为什么」",
          "不是可识别的音频容器" in reason_bad, f"原因：{reason_bad[:80]}")
    check("非音频字节 → 补救建议指向**调用方**（不是「联系管理员」）",
          "音频文件本身" in fix_bad and "管理员" not in fix_bad,
          f"建议：{fix_bad[:70]}")

    # ⚠️ 下面三条是 2026-10-05 补上的**判据漏洞**：原先只比了「建议指向调用方」，
    #    而**原因那句话**当时是手抄的两份（检测层一份、MCP 一份），并且
    #    `pipeline._FIXES` 里那条**路由关键词**又是一份手写字面量。
    #    三者只要有一个改了、别的不改，后果都是**静默降级**：
    #    调用方为「你给的不是音频」收到的却是兜底那句「请提交完整未截断的原始音频字节」
    #    —— 照着错误的归因去改，而**所有"字符串相等"的判据都是绿的**。
    clause_bad = non_audio_clause("application/octet-stream")
    check("字节族那条原因**只能来自** `non_audio_clause()`："
          "① 子句必须**派生自**共用前缀；② ACPs 这条路交回的原因必须**以它结尾**",
          clause_bad.startswith(NON_AUDIO_CLAUSE_PREFIX) and reason_bad.endswith(clause_bad),
          f"实得原因={reason_bad!r}")
    check("……而**同一句原因**必须真的**路由到**「你给的不是音频」那条建议"
          "（关键词与子句不同源 ⇒ 悄悄掉进兜底那句，而「路是通的」看起来毫无异常）",
          "音频文件本身" in fix_bad and "完整、未截断的原始音频字节" not in fix_bad,
          f"实得建议：{fix_bad[:70]}")
    check("　└ 反面对照：一句**不含**那个关键词的原因仍然走兜底"
          "（证明上一行是真的匹配上了，不是无条件返回同一句）",
          "音频文件本身" not in fix_for_input("容器结构本身有问题：某个内部错误"),
          f"兜底结果：{fix_for_input('容器结构本身有问题：某个内部错误')[:60]}")

    out_scheme, _ = attempt(outcome, "https://example.com/a.wav")
    check("出网引用 → `status=refused`，且建议就是取件族那一句",
          getattr(out_scheme, "status", None) == "refused"
          and getattr(out_scheme, "how_to_fix", None) == fix_for("unsupported_scheme"),
          f"建议：{str(getattr(out_scheme, 'how_to_fix', None))[:60]}")

    # ⚠️ 一律加 `or ""`：`fix_for` 万一返回 None（有人加了码却忘配建议），
    #    `in` 会抛 TypeError ⇒ 脚本崩掉，而"崩掉"看起来只是"少打了几行"。
    #    让它在**这一条**上报失败，比让整个脚本消失安全得多。
    fix_b64 = fix_for("data_uri_bad_base64") or ""
    check(
        "**两族不许串台**：参数写错（坏 base64）不许被答成「容器结构本身有问题」",
        "base64" in fix_b64 and "容器结构" not in fix_b64,
        f"坏 base64 的建议：{fix_b64[:60]}",
        "（取件族走分类码、字节族才走关键词 —— 靠调关键词治不了串台）",
    )

    good_uri = _uri_of(WAV_SMALL)
    out_ok, err_ok = attempt(outcome, good_uri, context_note="据称是通话录音")
    res_ok = getattr(out_ok, "result", None)
    check("正常输入 → `status=ok`",
          getattr(out_ok, "status", None) == "ok" and err_ok is None,
          f"status={getattr(out_ok, 'status', None)!r}，异常={err_ok!r}")
    check("**收下的参数必须往下传**：`context_note` 原样回显，不被静默丢弃",
          getattr(res_ok, "context_note", None) == "据称是通话录音",
          f"实得：{getattr(res_ok, 'context_note', None)!r}")
    check("`source_uri` 原文照抄（便于复核「这份字节是从哪个引用取来的」）",
          getattr(res_ok, "source_uri", None) == good_uri,
          f"实得：{str(getattr(res_ok, 'source_uri', ''))[:24]}…")
    check("溯源字段如实：`fetched_from=inline_base64`（内联字节不许写成 network）",
          getattr(res_ok, "fetched_from", None) == "inline_base64",
          f"实得：{getattr(res_ok, 'fetched_from', None)}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        token = inbox.bind_root(root)
        try:
            item = inbox.stage(WAV_SMALL, root=root, mime_type="audio/wav", name="a.wav")
            out_inbox, err_inbox = attempt(outcome, item.handle)
            res_inbox = getattr(out_inbox, "result", None)
            check("随任务附件进来的音频 → `fetched_from=inbox`（首发也是最推荐的一条）",
                  err_inbox is None and getattr(res_inbox, "fetched_from", None) == "inbox",
                  f"实得：{getattr(res_inbox, 'fetched_from', None)!r}")
            bad_handle, _ = attempt(outcome, "inbox:" + "0" * 64)
            check("未登记的句柄 → refused + 「请确认它是本次任务附带的音频」这一类可执行出路",
                  getattr(bad_handle, "status", None) == "refused"
                  and bad_handle.how_to_fix == fix_for("inbox_invalid"),
                  f"建议：{(bad_handle.how_to_fix or '')[:60]}")
        finally:
            inbox.unbind_root(token)
    print()

    # =======================================================================
    print("=" * 74)
    print("第 8 节 · ENF 轨迹与判据同源，且必须从包顶层可达")
    print("=" * 74)

    wave_clean = decode_audio(P_ENF_CLEAN).waveform
    track, err_track = attempt(enf_track, wave_clean)
    ok_track = (
        err_track is None and isinstance(track, dict)
        and track["available"] and track["detected"]
        and track["base_hz"] == 50.0 and bool(track["points"])
    )
    check("市电痕迹样本 → ENF 轨迹可用、检出、基频 50 Hz、点数非空", ok_track,
          f"实得：{ {k: v for k, v in (track or {}).items() if k != 'points'} if isinstance(track, dict) else err_track!r }")
    check("轨迹点带时间与频率两个字段（供人工核对）",
          isinstance(track, dict) and bool(track["points"])
          and set(track["points"][0]) == {"t_seconds", "freq_hz"},
          f"首点：{track['points'][0] if isinstance(track, dict) and track['points'] else '-'}")
    check("轨迹必须自带那句「`detected=false` 时看不出任何东西」的说明",
          isinstance(track, dict) and "看不出任何东西" in track["note"],
          f"note：{track['note'][:60] if isinstance(track, dict) else '-'}")

    rep_jump = analyzed(P_ENF_JUMP)
    track_jump = enf_track(decode_audio(P_ENF_JUMP).waveform)
    check("**关系断言** · 判据说「相位跳变」时，轨迹必须 `detected=true`"
          "（看不见的轨迹上不可能判出跳变）",
          "enf" not in fired(rep_jump) or track_jump["detected"] is True,
          f"check_enf 触发={'enf' in fired(rep_jump)}；track.detected={track_jump['detected']}")

    none_track = enf_track(None)
    check("没有波形时 → `available=False` 且点集为空（不是给一串假点）",
          none_track["available"] is False and none_track["points"] == [],
          f"reason：{none_track['reason'][:60]}")

    forensics_module = __import__("legal_acps.audio.forensics", fromlist=["__all__"])
    check("`enf_track` 必须从包顶层可达（本轮修的正是一个「只从子模块能拿到」的导出）",
          callable(enf_track) and "enf_track" in forensics_module.__all__,
          "legal_acps.audio.enf_track ✓ / forensics.__all__ ✓")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 9 节 · 复检阶梯不随等级裁剪 + 上限不变量")
    print("=" * 74)

    tiers_seen: set[str] = set()
    for level in ("低", "中", "高", ABSTAIN):
        ladder, err = attempt(plan_upgrades, level)
        names = [u.name for u in ladder] if ladder else []
        tiers_seen |= {u.tier for u in ladder} if ladder else set()
        check(f"复检阶梯 · {level}：三档齐全，且末档是**有法律证明力**的那一条",
              err is None and len(ladder) == 3
              and ladder[-1].tier == "offline_forensic" and "声像资料" in ladder[-1].name,
              f"{len(ladder) if ladder else 0} 档：{' / '.join(n[:18] for n in names)}",
              "（每一档都返回，不随等级裁剪 —— 从「低」里删掉司法鉴定等于对用户隐藏唯一救济路径）")
    check("复检档位的取值域固定",
          tiers_seen == {"online_free", "online_paid", "offline_forensic"},
          f"实得：{sorted(tiers_seen)}")

    # -- 上限不变量 ---------------------------------------------------------
    check("上下限不变量：**落点上限 ≥ 取用方上限**（不然音频永远撞不到自己的上限）",
          inbox.MAX_STAGE_BYTES >= MAX_AUDIO_BYTES,
          f"收件箱 {inbox.MAX_STAGE_BYTES} ≥ 音频 {MAX_AUDIO_BYTES}")
    seconds_by_bytes = MAX_AUDIO_BYTES / 2.0 / 44100.0          # 16 位单声道 44.1 kHz
    seconds_by_frames = MAX_ANALYSIS_FRAMES / 44100.0
    check("字节上限与波形分析窗**同量级**（否则「上限卡在分析窗的腰上」）",
          abs(seconds_by_bytes - seconds_by_frames) / seconds_by_frames < 0.25,
          f"字节上限 {seconds_by_bytes:.1f}s vs 分析窗 {seconds_by_frames:.1f}s")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 10 节 · 接线：模态画像 + `evidence-audio` 挂的收件 Hook 真的收音频")
    print("=" * 74)

    try:
        from legal_acps.evidence_hooks import KIND_PROFILES, profile_for
    except ImportError as exc:  # pragma: no cover - 依赖缺失必须**出声**
        check("能导入 `legal_acps.evidence_hooks`", False, f"ImportError：{exc}")
    else:
        prof = profile_for("audio")
        check("模态画像 · audio：措辞、参数名、上线用的 Part 类都对",
              prof.noun == "音频" and prof.counter == "段音频"
              and prof.uri_arg == "audio_uri" and prof.part_class == "AudioPart",
              f"{prof.noun}/{prof.counter}/{prof.uri_arg}/{prof.part_class}")
        unknown = profile_for("__no_such_kind__")
        check("未登记的模态走**中性**兜底（不猜成图片、也不猜成音频）",
              unknown.uri_arg == "material_uri" and unknown.part_class == "FilePart",
              f"{unknown.noun}/{unknown.uri_arg}/{unknown.part_class}",
              "（猜错会让注入给模型的指令指向一个不存在的工具参数）")
        check("画像表覆盖**全部**模态（清单从 `EvidenceModality` 现场派生）",
              {m.kind for m in EvidenceModality} <= set(KIND_PROFILES),
              f"实得：{sorted(KIND_PROFILES)}",
              "（原先写死 {'image','audio'}：加视频模态时它照样通过 ⇒ 是个会漂的副本）")

    try:
        import legal_acps.agents as A
        from legal_acps.evidence_hooks import EvidenceInboxHooks  # noqa: F401 - 只为确认依赖在位
    except ImportError as exc:  # pragma: no cover
        check("能导入 `legal_acps.agents`（音频角色已接线）", False, f"ImportError：{exc}")
    else:
        def _hooks_of(builder_name: str, kind: str) -> list[object]:
            """取某角色挂的某类 Hook。

            ⚠️ 用 `type(h).__name__` 比对而不是 `isinstance`：角色清单与 Hook 类
            分处两个模块，这样断言只依赖"名字叫这个"，不依赖导入路径，
            也就不会因为一次无关的重命名把一个接线断言变成导入错误。
            """
            agent, err = attempt(getattr(A, builder_name), None)
            if err is not None or agent is None:
                check(f"能构建角色 {builder_name}", False, f"{type(err).__name__}: {err}")
                return []
            return [h for h in (agent.hooks or []) if type(h).__name__ == kind]

        audio_inbox = _hooks_of("build_evidence_audio", "EvidenceInboxHooks")
        check("`evidence-audio` 挂了收件 Hook，且只收音频",
              len(audio_inbox) == 1 and tuple(audio_inbox[0].kinds) == ("audio",),
              f"实得：{[tuple(h.kinds) for h in audio_inbox]}")
        check("`evidence-audio` 是叶子角色 ⇒ 收件 Hook 必须关掉「接管委派」（它没有那个工具）",
              len(audio_inbox) == 1 and audio_inbox[0].delegate_tool == "",
              f"delegate_tool={audio_inbox[0].delegate_tool!r}" if audio_inbox else "（没挂上）")
        check("`evidence-audio` 必须挂终止强制（交付物只能靠工具产生）",
              len(_hooks_of("build_evidence_audio", "LeaderTerminationHooks")) == 1,
              "`evidence_analyze_audio` 出结论后才会收尾")

        leader_inbox = _hooks_of("build_evidence_leader", "EvidenceInboxHooks")
        # ⚠️ 清单**从模态枚举现场派生**：原先写死 `== ("image", "audio")`，
        #    加视频模态后它报的是"没达预期"而不是"清单过期"，而且**加新模态时
        #    修改它的人只会把视频加进去、不会想到这行本该派生**。
        #    判据方向也必须是"⊇ 全部模态"：漏收一种 = 静默丢一种模态。
        want_kinds = {m.kind for m in EvidenceModality}
        check("`evidence-leader` 的收件 Hook 必须收**全部**模态（漏一种 = 静默丢一种模态）",
              len(leader_inbox) == 1 and want_kinds <= set(leader_inbox[0].kinds),
              f"实得：{[tuple(h.kinds) for h in leader_inbox]}，应含：{sorted(want_kinds)}")
        image_inbox = _hooks_of("build_evidence_image", "EvidenceInboxHooks")
        check("`evidence-image` 只收图片（模态不许在两边一起变宽）",
              len(image_inbox) == 1 and set(image_inbox[0].kinds) == {"image"},
              f"实得：{[tuple(h.kinds) for h in image_inbox]}",
              "（写成正向判据 `== {'image'}`：否定式 `'audio' not in …` 拦不住"
              "下一个模态在两边一起变宽）")
    print()

    # =======================================================================
    print("=" * 74)
    if FAILED:
        print(f"有 {len(FAILED)} 个用例未达预期：")
        for label in FAILED:
            print(f"  · {label}")
        return 1
    print("全部用例符合预期：")
    print("  · 3 个负对照的全部检查项零触发 —— 误报是最致命的失败")
    print("  · 而已见的正样本分别被**预期的检查项**抓到，报出的量值就是造样本时写死的值")
    print("  · **「某一项不适用」不会把整份报告压成「无法判定」**：纯音拿到的是「低」，")
    print("    只有材料级前提（没波形 / 太短 / 近乎无声）才触发闸门 —— 两层弃权由成对断言钉住")
    print("  · 每一节的出口文案都与判据同源；闸门那一支绝不会写成「均未提示异常」")
    print("  · 取件族 9 个分类码**逐码**有专属出路，其中 8 个已被真实构造出来")
    print("  · 工具入口一个异常都不漏；两族补救建议不串台（坏 base64 ≠ 容器坏了）")
    print("  · ENF 轨迹与判据同一实现，且从包顶层可达")
    print("  · 复检阶梯不随等级裁剪；上下限不变量成立")
    print("  · 模态画像与三个角色的收件 Hook 各收各的模态")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
