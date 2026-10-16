"""离线验证：视频鉴伪初检（`legal_acps/video/`）+ 两张 PPT 的降级机制。

**不联网、不需要 API key、不碰平台、不调模型** —— 全部是确定性纯函数的断言。
判据只有一个：`exit 0 = 全绿`。

用法（在 witrelate 目录下）：
    ./.venv/Scripts/python.exe -m tools.verify_video

════════════════════════════════════════════════════════════════════════
它到底在测什么
════════════════════════════════════════════════════════════════════════

**负对照比正样本重要。** 鉴伪工具最危险的失败不是漏报，是误报：它会把一段
正常视频说成"有篡改嫌疑"，而使用者没有任何手段复核我们。

视频链还有一个**音频链没有的**处境，它决定了本文件的一半篇幅：

    这份能力的两条主要通路（`moov` 内的一切 / MJPEG 的逐帧取出）
    **此前从来没有被任何样本喂到过**。

不是"测得不够"，是"一次都没测过"：在写本文件之前，`decode.py` 里
`_children`、`_bmff_frames`、`_looks_like_box_type` 三处缺陷**同时存在**，
而每一处都表现为"读不到东西" —— 报告照样出得来，只是每份都「无法判定」。
第 3b 节逐条把它们钉死，每条都要求**退回旧形态即报失败**。

节次安排：

  第 1 节  契约：七项齐全、每条自带 caveat、取值域里没有「真 / 假」
  第 2 节  **负对照（最重要的一节）**：三份干净样本必须零触发
  第 3 节  正样本：该抓的抓到，报出的量值就是真实量值
  第 3b 节 **本轮夹出来的五个缺陷**：逐条钉死，退回旧形态即报失败
  第 4 节  弃权的两层：材料级闸门 ≠ 逐项不适用
  第 5 节  出口同源：`summary_line()` 不得与 `summarize()` 互相矛盾
  第 6 节  取件族：**逐码**都有专属出路，且每条拒绝都真的能构造出来
  第 7 节  字节族：工具入口一个异常都不许漏，且两族的建议不许串台
  第 8 节  畸形字节：`decode_video` 承诺「从不抛异常」，把它兑现一遍
  第 9 节  复检阶梯不随等级裁剪 + 上限不变量
  第 10 节 **两张 PPT 的机制**：`ContentTransform` 降级 + `GatewayAdapter` 适配
  第 11 节 接线：模态枚举 + 三条链的角色各收各的模态、各挂各的 Hook
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import re
import sys
import tempfile
from pathlib import Path

import numpy as np

import legal_acps.video.decode as video_decode
import legal_acps.video.fetch as video_fetch
import legal_acps.video.frames as video_frames
import legal_acps.video.pipeline as video_pipeline
import legal_acps.video_modality as vm
from legal_acps.contracts import EvidenceModality
from legal_acps.evidence import inbox
from legal_acps.evidence.contracts import Finding
from legal_acps.verdict import ABSTAIN, ABNORMAL
from wit import OpenAIChatProvider
from wit.kernel.content import ImagePart, TextPart, VideoPart
from legal_acps.video import (
    FETCH_REFUSAL_KINDS,
    GATE_ABSTAIN_PREFIX,
    MAX_VIDEO_BYTES,
    NON_VIDEO_CLAUSE_PREFIX,
    NOT_APPLICABLE_PREFIX,
    VideoFetchRefused,
    analyze,
    check_policy,
    classify_uri,
    decode_video,
    fix_for,
    fix_for_input,
    is_supported_video,
    load,
    non_video_clause,
    outcome,
    plan_upgrades,
    sha256_of,
    summarize,
    summary_line,
)
from legal_acps.video.decode import MAX_FRAME_BYTES
# ⚠️ 这两个判据函数**从子模块直接取**（不走 `legal_acps.video` 那个惰性包）：
#    包级 `__getattr__` 只认 `_LAZY` 表里的名字，"能不能取到"会依赖导入顺序 ——
#    与本项目"显式导入子模块"的那条纪律一致（见 `mcp/video_forensics.py` 的说明）。
from legal_acps.video.forensics import CHECK_WEIGHTS, normalize_mime
from run import samples as IMG
from run import video_samples as V

FAILED: list[str] = []

#: `analyze` 结果缓存。同一条视频会被好几节反复取报告，而最贵的一步是
#: 帧层的**图像鉴伪链**（每帧一次 FFT 家族）。不缓存时整体运行时间成倍上升，
#: 而元验证与真机复跑都要反复执行本脚本。
#:
#: ⚠️ 键用**完整 sha256** 而不是 `hash()`：后者是 64 位、没有跨进程保证，
#: 万一撞一次就会让某条断言拿到**另一条视频的报告** —— 测试给出的答案会错得
#: 无声无息（图像侧的 `verify_evidence.py` 里写着同一条理由）。
_CACHE: dict[str, object] = {}


def analyzed(data: bytes):  # type: ignore[no-untyped-def]
    key = hashlib.sha256(data).hexdigest()
    if key not in _CACHE:
        _CACHE[key] = analyze(data)
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
    """材料级闸门：`GATE_ABSTAIN_PREFIX` 那一族（帧层整层没跑）。"""
    return {
        f.check for f in report.findings
        if f.verdict == ABSTAIN and str(f.detail).startswith(GATE_ABSTAIN_PREFIX)
    }


def skipped(report) -> set[str]:  # type: ignore[no-untyped-def]
    """逐项弃权：`NOT_APPLICABLE_PREFIX` 那一族（这一项对这段视频无从执行）。"""
    return {
        f.check for f in report.findings
        if f.verdict == ABSTAIN and str(f.detail).startswith(NOT_APPLICABLE_PREFIX)
    }


def finding_of(report, name: str):  # type: ignore[no-untyped-def]
    return next(f for f in report.findings if f.check == name)


def _mk(check_name: str, verdict: str, detail: str) -> Finding:
    """造一条发现。**在内部补全基线字段**（`caveat` 是必填的）。"""
    return Finding(
        check=check_name, verdict=verdict,  # type: ignore[arg-type]
        detail=detail, caveat="构造用；本用例不考察这一句。",
    )


# ---------------------------------------------------------------------------
# 样本：**全部来自 run/video_samples.py**（断言脚本不另造一份）
# ---------------------------------------------------------------------------
#
# `run/video_samples.py` 是纯字节构造器（零第三方依赖）—— 那正是它能在
# 任何 Python 上逐位复现的前提。唯一需要 Pillow 的是 `rasterizable_gif()`
# （真编码器产出的动画 GIF），它在那里**按需导入**。
#
# 帧层的正样本需要一份**真的能被图像链解码**的 JPEG —— 那来自
# `run/samples.py`（图像链的样本库，本来就用 numpy + Pillow）。


def _photo(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    return IMG.jpeg((rng.random((64, 64, 3)) * 60 + 100).astype("uint8"), quality=90)


JPEG_A, JPEG_B, JPEG_C = _photo(1), _photo(2), _photo(3)

#: 有 JPEG 的魔术字节（所以 `decode.py` 会把它当帧取出来）、
#: 但**不是**可解码的 JPEG（图像链会拒绝它）。这一对样本钉住的是
#: 「取到了帧」与「帧被看过了」**不是同一件事**。
FAKE_JPEG = b"\xff\xd8\xff" + b"\x00" * 64

CLEAN = V.clean_mp4()
CLEAN_MJPEG = V.motion_jpeg_mp4([JPEG_A, JPEG_B, JPEG_C])
CLEAN_COVER = V.with_cover(JPEG_A)
GIF_REAL = V.rasterizable_gif(3)
GIF_FAKE = V.animated_gif(3)

DUP_MJPEG = V.motion_jpeg_mp4([JPEG_A, JPEG_A, JPEG_A])
FAKE_FRAME_MP4 = V.motion_jpeg_mp4([FAKE_JPEG])

ELST = V.with_elst_skip()
TRAILING = V.trailing_garbage(64)
MOOV_LAST = V.moov_after_mdat()
TWO_MDATS = V.two_mdats()
FREE = V.free_padding(4096)
FPS_JUMP = V.frame_rate_jump()
DUR_MISMATCH = V.duration_mismatch()
ENCODER = V.with_cover(JPEG_A, encoder="Lavf58.76.100")

NOT_A_VIDEO = V.not_a_video()

#: 七项检查的名字与顺序。**这是"报告写了哪些项"的唯一判据** ——
#: 少一项、或者顺序变了，都必须立刻报出来（顺序变了意味着汇总口径变了）。
EXPECTED_CHECKS = [
    "container",
    "structure",
    "timeline",
    "samples",
    "edits",
    "metadata",
    "frames",
]

VERDICT_DOMAIN = {"未提示异常", "提示异常", "无法判定"}
LEVEL_DOMAIN = {"低", "中", "高", "无法判定"}

_LEVEL_RE = re.compile(r"综合线索等级：([^。]+)。")


def _level_in(text: str) -> str | None:
    m = _LEVEL_RE.search(text)
    return m.group(1) if m else None


def _uri_of(data: bytes, mime: str = "video/mp4") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _ts_sample(size: int = 565) -> bytes:
    """能过 MPEG-TS 同步字节判定的最小样本（`0x47` 出现在 0 / 188 / 376）。

    ⚠️ `sniff_media_type` 判的是**三处**同步字节、且要求 `len > 564`，
    所以这份样本的长度与偏移是**判据的一部分**，不是随手填的。
    """
    buf = bytearray(size)
    for off in (0, 188, 376):
        if off < size:
            buf[off] = 0x47
    return bytes(buf)


def main() -> int:  # noqa: PLR0915 - 逐节展开，便于逐条回读断言的理由
    # =======================================================================
    print("=" * 74)
    print("第 1 节 · 契约：七项齐全、每条自带 caveat、取值域里没有「真 / 假」")
    print("=" * 74)
    # =======================================================================

    report, err = attempt(analyzed, CLEAN_MJPEG)
    check("干净的 MJPEG 样本能产出报告（不抛异常）", err is None, f"异常：{err!r}")
    check(
        "检查项恰好是约定的七项，且顺序不变",
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
    check("综合等级落在固定取值域内", report.suspicion in LEVEL_DOMAIN,
          f"实得：{report.suspicion}")
    check(
        "SHA-256 / 字节数由代码复算，与源字节一致",
        report.sha256 == sha256_of(CLEAN_MJPEG) and report.byte_size == len(CLEAN_MJPEG),
        f"报告 {report.sha256[:16]}… / {report.byte_size} 字节",
    )
    check(
        "效力声明在位（报告不得自称鉴定意见）",
        "不是司法鉴定意见" in report.disclaimer and bool(report.chain_of_custody),
        f"保全提示 {len(report.chain_of_custody)} 条",
    )
    check(
        "`frames_available` 是**显式字段**（它决定哪些检查项能跑，不许藏着）",
        isinstance(report.frames_available, bool) and bool(report.frames_note.strip()),
        f"frames_available={report.frames_available}；frames_note 非空",
    )
    check(
        "容器层读出的轨道信息进了报告（视频轨的编码 / 宽高 / 帧数）",
        bool(report.tracks) and report.video_codec == "jpeg"
        and report.width == 640 and report.height == 480 and report.frame_count == 3,
        f"codec={report.video_codec} {report.width}x{report.height} 帧数={report.frame_count}",
    )
    print()

    # =======================================================================
    print("=" * 74)
    print("第 2 节 · 负对照（**最重要的一节**）：干净样本必须零触发")
    print("=" * 74)
    print("  说明：三份样本都是「结构自洽、没有剪辑痕迹」的合成文件，造法各不相同：")
    print("        裸 MP4（无帧可取）/ MJPEG 逐帧真 JPEG / 内嵌封面帧。")
    print("        它们**一个异常都不该触发**。而它们的弃权项各不相同 ——")
    print("        那正是本链最危险的失败形态：**某一项不适用 ≠ 整份报告测不了。**")
    print()

    for name, data in (
        ("裸 MP4（容器里没有 JPEG/PNG 帧）", CLEAN),
        ("MJPEG（逐帧就是完整 JPEG）", CLEAN_MJPEG),
        ("内嵌封面帧的 MP4", CLEAN_COVER),
        ("真动画 GIF（三帧互不相同）", GIF_REAL),
    ):
        rep, err = attempt(analyzed, data)
        check(f"负对照 · {name} → 零触发（一项都不许报异常）", fired(rep) == set(),
              f"触发：{sorted(fired(rep))}，异常：{err!r}")

    # 有帧可用的三份：**不许出现材料级闸门**，等级必须是「低」。
    # ⚠️ 这一对断言是本节的核心：帧层跑起来了 = 没有闸门；没有闸门 = 等级照常给出。
    for name, data in (
        ("MJPEG（逐帧就是完整 JPEG）", CLEAN_MJPEG),
        ("内嵌封面帧的 MP4", CLEAN_COVER),
        ("真动画 GIF（三帧互不相同）", GIF_REAL),
    ):
        rep = analyzed(data)
        check(f"负对照 · {name} → 无材料级闸门，且等级「低」",
              gated(rep) == set() and rep.suspicion == "低",
              f"等级 {rep.suspicion}；闸门 {sorted(gated(rep))}",
              "（帧层一旦真的跑了，就不该再有任何一项整层缺席）")

    # 裸 MP4：**只有它**该出现材料级闸门（容器里确实没有可零解码取出的帧）。
    bare = analyzed(CLEAN)
    check("负对照 · 裸 MP4 → **恰好**只有 frames 走材料级闸门 ⇒ 无法判定",
          gated(bare) == {"frames"} and bare.suspicion == ABSTAIN,
          f"等级 {bare.suspicion}；闸门 {sorted(gated(bare))}",
          "（服务器上没有 ffmpeg ⇒ H.264 一律解不出帧，这一支是**常态**）")
    check("负对照 · 裸 MP4 → 没有任何一项被写成「提示异常」", fired(bare) == set(),
          f"触发：{sorted(fired(bare))}")
    check("负对照 · 裸 MP4 → 其余各项都真的给出了结论（不许一弃了之）",
          {f.check for f in bare.findings if f.verdict != ABSTAIN}
          == set(EXPECTED_CHECKS) - {"frames", "metadata"},
          f"给出了结论的项：{sorted({f.check for f in bare.findings if f.verdict != ABSTAIN})}",
          "（metadata 也弃权：这份合成样本没写创建时间、也没有编码工具字符串）")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 3 节 · 正样本：该抓的抓到，且报出的量值就是真实量值")
    print("=" * 74)

    rep = analyzed(ELST)
    ed = finding_of(rep, "edits")
    check("编辑列表里的空编辑（media_time=-1）→ edits 触发（等级仍是「低」）",
          "edits" in fired(rep) and rep.suspicion == "低",
          f"等级 {rep.suspicion}；触发 {sorted(fired(rep))}",
          "（0.5 权重的弱线索不足以升级 —— 报「低」是正确行为，不是漏报）")
    check("……而且必须说成**空编辑**，不许折算成「跳过 0 秒」（那就等于没看见）",
          "空编辑" in ed.detail, f"detail：{ed.detail[:90]}")

    rep = analyzed(TRAILING)
    ct = finding_of(rep, "container")
    check("尾部多出 64 字节 → container 触发，且报出的字节数就是 64",
          "container" in fired(rep) and "**64 字节**" in ct.detail,
          f"detail：{ct.detail[:90]}")

    rep = analyzed(MOOV_LAST)
    check("`moov` 在 `mdat` 之后 → structure 触发",
          "structure" in fired(rep) and "之后" in finding_of(rep, "structure").detail,
          f"等级 {rep.suspicion}")
    check("……而 `moov` 位置**不是**伪造证据（caveat 必须把正常成因说全）",
          "边写边录" in finding_of(rep, "structure").caveat,
          "（手机相机默认就是这种布局）")

    rep = analyzed(TWO_MDATS)
    check("两段 `mdat` → structure 触发，且报出的段数就是 2",
          "structure" in fired(rep) and "**2 段**" in finding_of(rep, "structure").detail,
          f"detail：{finding_of(rep, 'structure').detail[:80]}")

    rep = analyzed(FREE)
    # ⚠️ 真值**不写死**：从"样本是怎么造出来的"独立算 —— `box("free", 4096 个零)`
    #    的 size 字段 = 8（盒头）+ 4096（载荷）。ISO BMFF 的 size **本来就含盒头**，
    #    所以报告里必须是 4104；若照构造入参写 4096，等于用"我传了什么"代替
    #    "文件里真的有多少字节" —— 那正是「写死的期望值」最容易骗人的地方。
    free_true_bytes = 8 + 4096
    check("`free` 填充块 → structure 触发，且报告里的字节数就是盒的 size 字段（含 8 字节盒头）",
          "structure" in fired(rep)
          and video_decode.decode_video(FREE).facts.free_bytes == free_true_bytes
          and f"**{free_true_bytes} 字节**" in finding_of(rep, "structure").detail,
          f"detail：{finding_of(rep, 'structure').detail[:80]}",
          f"（容器自己解析出的 free_bytes = "
          f"{video_decode.decode_video(FREE).facts.free_bytes}）")

    rep = analyzed(TRAILING)
    check("尾部多余数据**不许**被诊断成「截断」（那是把病因说反了）",
          video_decode.decode_video(TRAILING).facts.truncated is False
          and video_decode.decode_video(TRAILING).facts.appended_bytes == 64,
          "truncated=False / appended_bytes=64")

    rep = analyzed(FPS_JUMP)
    tl = finding_of(rep, "timeline")
    check("帧间隔跳变 20 倍 → timeline 触发，且倍率就是 20.0",
          "timeline" in fired(rep) and "20.0 倍" in tl.detail,
          f"detail：{tl.detail[:100]}")

    rep = analyzed(DUR_MISMATCH)
    tl = finding_of(rep, "timeline")
    check("`mvhd` 声明 20 秒而轨道只有 1 秒 → timeline 触发，差值是 19.000 秒",
          "timeline" in fired(rep) and "19.000 秒" in tl.detail and "20.000 秒" in tl.detail,
          f"detail：{tl.detail[:120]}")

    rep = analyzed(ENCODER)
    md = finding_of(rep, "metadata")
    check("容器自报的编码工具命中剪辑软件 → metadata 触发，并**原文照抄**那个值",
          "metadata" in fired(rep) and "lavf" in md.detail
          and "'Lavf58.76.100'" in md.detail,
          f"detail：{md.detail[:110]}")

    rep = analyzed(DUP_MJPEG)
    fr = finding_of(rep, "frames")
    check("三帧**字节完全相同** → frames 触发，且点名是哪两帧",
          "frames" in fired(rep) and "字节完全相同" in fr.detail and "第 0 帧" in fr.detail,
          f"detail：{fr.detail[-120:]}")

    dec = video_decode.decode_video(GIF_FAKE)
    check("手拼的动画 GIF → 容器层的帧数就是写死的 3（块结构真的被数了）",
          dec.facts.actual_frame_chunks == 3 and dec.facts.media_type == "image/gif",
          f"actual_frame_chunks={dec.facts.actual_frame_chunks}")
    check("GIF 的全局色表被跳过（跳过之后才读得到块结构）",
          any("全局色表" in n for n in dec.facts.notes)
          and not any("未在 GIF89a 规范里" in n for n in dec.facts.notes),
          f"notes={dec.facts.notes}",
          "（不跳色表 ⇒ 第一个字节就翻车 ⇒ 帧数 0 —— 一个看起来正常的错数字）")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 3b 节 · **本轮夹出来的五个缺陷**：逐条钉死（退回旧形态即报失败）")
    print("=" * 74)
    print("  ⚠️ 这一节的五条**全都是**「能力整项死掉、而报告照样出得来」的形态：")
    print("     它们不会抛异常、不会有明显错字，只会让每一份报告都变成「无法判定」。")
    print("     所以每一条都必须能回答同一个问题：**退回旧写法，这条断言会红吗？**")
    print()

    # -- 缺陷 1：`_children` 只给 `meta` 开了 FullBox 特例 --------------------
    dec_cover = video_decode.decode_video(CLEAN_COVER)
    check("缺陷 1 · `ilst` 也是 FullBox ⇒ 内嵌封面帧必须读得到",
          len(dec_cover.frames) == 1 and dec_cover.frame_source == "cover_art",
          f"帧数 {len(dec_cover.frames)}；来源 {dec_cover.frame_source}",
          "退回旧形态（只给 meta 开特例）⇒ 0 帧：`_children(ilst)` 在"
          "偏移 0 处读到的是**非空但错位**的一串，"
          "于是「先试 0、空再试 4」的条件根本不成立")

    # -- 缺陷 2：`stsz` 的 sample_count 偏移 --------------------------------
    dec_mjpeg = video_decode.decode_video(CLEAN_MJPEG)
    mjpeg_track = next(t for t in dec_mjpeg.facts.tracks if t.framing == "bmff_mjpeg")
    check("缺陷 2 · `stsz` 是 FullBox ⇒ sample_count 在 `body+8`，不是 `body+4`",
          len(dec_mjpeg.frames) == 3 and dec_mjpeg.frame_source == "mjpeg_samples"
          and mjpeg_track.sample_count == 3,
          f"帧数 {len(dec_mjpeg.frames)}；sample_count={mjpeg_track.sample_count}",
          "退回旧形态（读 body+4 = sample_size）⇒ count=0 ⇒ 直接 continue ⇒"
          " MJPEG 的 MP4 **一帧都取不到**，报告还说「容器里没有逐帧存着的 JPEG/PNG」")

    # -- 缺陷 3：`_looks_like_box_type` 要求 ASCII ---------------------------
    dec_enc = video_decode.decode_video(ENCODER)
    check("缺陷 3 · 4CC 不全是 ASCII（`©too` 首字节 0xA9）⇒ 判据只挡控制字符",
          dec_enc.facts.encoder_hint == "Lavf58.76.100"
          and any("©too" in t for t in dec_enc.facts.tags),
          f"encoder_hint={dec_enc.facts.encoder_hint!r}；tags={dec_enc.facts.tags}",
          "退回旧形态（isascii）⇒ 解析在第一个 `©` 原子处提前收工")
    check("……而且**不许**把 `©too` 之后的字节算成「尾部多余数据」（那是一次假阳性）",
          dec_enc.facts.appended_bytes == 0 and dec_enc.facts.truncated is False,
          f"appended_bytes={dec_enc.facts.appended_bytes}；truncated={dec_enc.facts.truncated}",
          "（iPhone / QuickTime / 剪映导出的 MP4 **几乎必然**含 `©` 系元数据 ——"
          " 这条假阳性会落在一大批完全正常的文件上）")

    # -- 缺陷 4：帧取到了、却一帧都没送进图像链 ⇒ reason 是空串 -------------
    rep_fake = analyzed(FAKE_FRAME_MP4)
    fr_fake = finding_of(rep_fake, "frames")
    check("缺陷 4 · 「取到帧但一帧都送不进图像链」必须留下**非空的、可读的原因**",
          bool(str(fr_fake.detail).strip())
          and str(fr_fake.detail).startswith(NOT_APPLICABLE_PREFIX)
          and "一帧都没能送进图像鉴伪链" in str(fr_fake.detail),
          f"detail={str(fr_fake.detail)[:100]!r}",
          "退回旧形态 ⇒ detail 是空串（`FrameLayerResult.reason` 默认），"
          "而 `frames_note` 也是空串 ⇒ 报告里只剩一个孤零零的等级")
    check("……逐帧的失败原因必须**看得见**（原来只躺在 `result.notes` 里没人读）",
          "第 0 帧" in str(fr_fake.detail) and "没能送进图像链" in str(fr_fake.detail),
          f"detail 尾部：{str(fr_fake.detail)[-90:]}")
    check("……而且这类弃权**不许**把整份报告压成「无法判定」（帧层确实跑了）",
          rep_fake.suspicion != ABSTAIN and "frames" not in gated(rep_fake),
          f"等级 {rep_fake.suspicion}；闸门 {sorted(gated(rep_fake))}")

    # -- 缺陷 5：GIF 光栅化失败却沿用 MP4 的原因 ----------------------------
    rep_gif = analyzed(GIF_FAKE)
    fr_gif = finding_of(rep_gif, "frames")
    check("缺陷 5 · 手拼 GIF 光栅化失败时，理由必须是**它自己的**",
          "光栅化" in str(fr_gif.detail)
          and "容器里没有逐帧存着的 JPEG/PNG" not in str(fr_gif.detail),
          f"detail={str(fr_gif.detail)[:110]}",
          "退回旧形态 ⇒ 沿用 MP4 那句「容器里没有逐帧存着的 JPEG/PNG」——"
          " 对 GIF 而言是**错的原因**（GIF 每帧本来就是一张完整位图）")
    check("……但它仍然是**材料级闸门**（一帧都没取到的语义没变）",
          gated(rep_gif) == {"frames"} and rep_gif.suspicion == ABSTAIN,
          f"闸门 {sorted(gated(rep_gif))}；等级 {rep_gif.suspicion}")

    # -- 样本侧：那两个「从来跑不到」的构造器 --------------------------------
    check("样本 · 4CC 用 latin-1 编码 ⇒ 编码工具项造得出来（`©too` 首字节 0xA9）",
          V.box("\xa9too", b"") == b"\x00\x00\x00\x08\xa9too"
          and V.encoder_item("Lavf").startswith(b"\x00\x00\x00\x1c\xa9too"),
          f"首 8 字节：{V.encoder_item('Lavf')[:8]!r}",
          "退回 ascii ⇒ 造样本时直接 UnicodeEncodeError，这条路径永远没人走")
    check("样本 · `udta` 是**单数**结构 ⇒ 封面帧与编码工具必须在同一个 `udta` 里",
          dec_enc.facts.encoder_hint is not None and len(dec_enc.frames) == 1,
          f"encoder={dec_enc.facts.encoder_hint!r}；帧数={len(dec_enc.frames)}",
          "退回旧形态（各造一个 udta）⇒ moov 里出现两个并列 udta，"
          "而 `_find` 只取第一个 ⇒ 后加进去的那一项永远读不到")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 3c 节 · **「谁算视频」只有一个判据**：接受集合跟着容器表现场长出来")
    print("=" * 74)
    print("  ⚠️ 2026-10-04 体检测出的真缺陷：`application/ogg` **就在解码层的容器表里**"
          "（`_OTHER_CONTAINERS` 给它配了中文名，")
    print("     `sniff_media_type` 有专门的 `OggS` 分支，`identify_only_note` 的 docstring"
          "更把它列为适用对象），")
    print("     却被 `startswith(\"video/\")` 这个**代理判据**拦在家门口，"
          "拒绝理由是假话「不是可识别的视频容器」。")
    print("     同族的 `video/mp2t` 只因为字符串带 `video/` 前缀就通行无阻 —— 差别只在字符串前缀。")
    print("  ⇒ 判据收进 `forensics.is_supported_video()`（唯一入口）；"
          "本节按**两张容器表现场派生**地验它。")
    print()

    all_containers = list(video_decode.PARSED_CONTAINERS) + list(video_decode._OTHER_CONTAINERS)
    unsupported = [mt for mt in all_containers if not is_supported_video(mt)]
    check("接纳判据覆盖解码层**两张表的每一个容器**（现场派生：以后加容器不许漏）",
          not unsupported, f"未被接纳：{unsupported}")
    bad_alias = [(mt, normalize_mime(mt)) for mt in all_containers
                 if normalize_mime(mt) != mt]
    check("……每个容器的**规范名都归一到它自己**（别名表把规范名指到别处 ⇒ "
          "每一份文件都会凭空报一条「声明与真实容器不一致」）",
          not bad_alias, f"归一异常：{bad_alias}")
    check("……而**认不出来**的字节仍然一律不接纳"
          "（负对照：判据没被放宽成「什么都收」）",
          not any(is_supported_video(mt) for mt in
                  ("application/octet-stream", "application/pdf", "text/plain", "")),
          "octet-stream / pdf / text / 空串都必须不接纳")
    # ⚠️ 2026-10-05 补：这两个符号原先**只从 `forensics` 子模块能拿到**，
    #    而 MCP 层走的正是「显式取子模块属性」⇒ 从包顶层取不到**不会报错**，
    #    只会让下一个想复用这个判据的人手抄第二份（Ogg 那件事的复发路径）。
    #    音频侧的同一条纪律见 `verify_audio` 第 8 节（`enf_track` 的前身）。
    _video_pkg = importlib.import_module("legal_acps.video")
    check("……「谁算视频」的判据与「不是视频」那句话还必须**从包顶层可达**"
          "（登记进 `_LAZY` 与 `__all__`；只从子模块能拿到就是个陷阱）",
          {"is_supported_video", "non_video_clause", "NON_VIDEO_CLAUSE_PREFIX"}
          <= set(_video_pkg.__all__)
          and _video_pkg.is_supported_video is is_supported_video
          and _video_pkg.non_video_clause is non_video_clause,
          f"__all__ 里缺：{sorted({'is_supported_video', 'non_video_clause', 'NON_VIDEO_CLAUSE_PREFIX'} - set(_video_pkg.__all__))}")

    # -- 「认得但没解析器」的容器：逐个造真字节，驱动被测代码 ------------------
    # ⚠️ 只断言 `is_supported_video()` 返回 True 是**不够的**：那只证明"表里有它"，
    #    不证明"它真能出报告"。所以这里逐个造魔术字节、喂进 `analyze()`。
    other_samples = {
        "video/mp2t": _ts_sample(),
        "video/x-flv": b"FLV\x01" + bytes(300),
        "video/mpeg": b"\x00\x00\x01\xba" + bytes(300),
        "video/x-ms-wmv": b"\x30\x26\xb2\x75\x8e\x66\xcf\x11" + bytes(300),
        "application/ogg": b"OggS\x00\x02" + bytes(300),
        "video/x-msvideo-unknown":
            b"RIFF" + (400).to_bytes(4, "little") + b"XXXX" + bytes(400),
    }
    check("六个「只识别」容器都能造出魔术字节（否则下面那组断言是空转）",
          set(other_samples) == set(video_decode._OTHER_CONTAINERS),
          f"表={sorted(video_decode._OTHER_CONTAINERS)}；样本={sorted(other_samples)}")
    for mt, sample in other_samples.items():
        got, err = attempt(analyze, sample, declared_mime=mt)
        check(f"{mt} · 认得就该**出报告**（判据是「认不认得」，不是「媒体类型前缀对不对」）",
              err is None and got is not None and got.media_type == mt,
              f"异常={err!r}；media_type={getattr(got, 'media_type', None)!r}")
        if err is not None or got is None:
            continue
        detail = str(finding_of(got, "container").detail)
        check(f"　└ {mt} · 容器项写明「没有为这种容器写解析器」（认得 ≠ 看过里面）",
              "没有为这种容器写解析器" in detail, f"detail={detail[:90]}")
        check(f"　└ {mt} · 综合等级必须是**无法判定**"
              "（一层都没解析就给等级 = 静默给结论）",
              got.suspicion == ABSTAIN, f"等级={got.suspicion}")
        # ⚠️ 帧层同一句"没查过写成查过"（2026-10-05 体检 B6）：
        # 「容器里**也没有**逐帧存着的 JPEG/PNG」原先**不分容器**地写着，
        # 而对只识别不解析的容器，本进程压根没往里看 ⇒ 说不出"有没有"。
        fr_detail = str(finding_of(got, "frames").detail)
        check(f"　└ {mt} · 帧层**不许**说「容器里**也没有**逐帧存着的 JPEG/PNG」"
              "（没解析过 ⇒ 那是把没查写成查过）",
              "也没有逐帧存着" not in fr_detail, f"detail={fr_detail[:110]}")
        check(f"　└ {mt} · 帧层必须写明「无法判断」，且说清是**没有为它写解析器**",
              "无法判断" in fr_detail and "没有为它写解析器" in fr_detail,
              f"detail={fr_detail[:110]}")

    # 正对照：**解析过**的容器（裸 MP4）**仍然**要说那句查出来的话 ——
    # 两种输入必须给出**不同**结论，否则这条判据只是"一律改成无法判断"也能过。
    clean_frames = str(finding_of(analyzed(CLEAN), "frames").detail)
    check("……而**解析过**的容器（裸 MP4）**必须**说「容器里也没有逐帧存着的 JPEG/PNG」"
          "（把两种情形合并成一句「无法判断」 = 丢掉已经查到的信息）",
          "也没有逐帧存着" in clean_frames, f"detail={clean_frames[:110]}")

    # 「两处写着同一句话」≠「单一来源」：手抄一份（哪怕一字不差）时，
    # 改一处另一处不动 ⇒ 照样全绿。⇒ 换掉**共用定义**，两个出口必须**同时**变。
    _orig_clause = video_decode.frames_absent_clause
    video_decode.frames_absent_clause = lambda _pd: "【哨兵-帧层理由】"
    try:
        _ts_dec = video_decode.decode_video(_ts_sample())
        _both = f"{_ts_dec.frames_reason}{_ts_dec.window_note}"
    finally:
        video_decode.frames_absent_clause = _orig_clause
    check("帧理由的**两处**出口共用同一个定义"
          "（换掉 `frames_absent_clause` ⇒ 长原因与报告短句**同时**跟着变）",
          _both.count("【哨兵-帧层理由】") >= 2,
          f"命中 {_both.count('【哨兵-帧层理由】')} 次")
    check("……撤掉哨兵后恢复原话（判据自己没把实现改坏）",
          video_decode.frames_absent_clause is _orig_clause
          and "【哨兵" not in str(finding_of(analyzed(CLEAN), "frames").detail))

    check("Ogg 的三种等价声明都归到同一个容器名"
          "（.ogv 的标准 MIME 就是 `video/ogg`；不归一 ⇒ **声明正确**的文件吃假线索）",
          normalize_mime("video/ogg") == "application/ogg"
          and normalize_mime("audio/ogg") == "application/ogg"
          and normalize_mime("application/x-ogg") == "application/ogg")
    ogg = analyze(other_samples["application/ogg"], declared_mime="video/ogg")
    ogg_container = finding_of(ogg, "container")
    check("……因此一份声明 `video/ogg` 的真 Ogg **不许**吃到「声明与真实容器不一致」，"
          "容器项应当是「未提示异常」且写明一致性",
          ogg_container.verdict == "未提示异常"
          and "与真实容器一致" in str(ogg_container.detail),
          f"verdict={ogg_container.verdict}；detail={str(ogg_container.detail)[:80]}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 4 节 · 弃权的两层：材料级闸门 ≠ 逐项不适用")
    print("=" * 74)

    check("裸 MP4 → frames 走**材料级闸门**（帧层整层没有执行）",
          gated(analyzed(CLEAN)) == {"frames"}, f"闸门 {sorted(gated(analyzed(CLEAN)))}")
    fake_frame = analyzed(FAKE_FRAME_MP4)
    check("假 JPEG 帧 → frames 走**逐项弃权**（帧层跑了，只是没结论），**不是**材料级闸门",
          "frames" in skipped(fake_frame) and gated(fake_frame) == set(),
          f"逐项 {sorted(skipped(fake_frame))}；闸门 {sorted(gated(fake_frame))}")
    # ⚠️ 这一层也必须点名：metadata **同列**弃权了，但那是另一件事（样本里压根没写
    #    元数据）。不把它钉死，以后「frames 弃权」与「metadata 弃权」会一起变宽，
    #    到时谁也说不清这条 barrels 到底是帧层造成的，还是元数据缺失造成的。
    check("……metadata 同时弃权，理由是**样本里没有元数据**，与帧层无关",
          "metadata" in skipped(fake_frame)
          and "没有可检查的元数据" in finding_of(fake_frame, "metadata").detail,
          f"metadata 理由：{finding_of(fake_frame, 'metadata').detail[:70]}")
    check("**同一项**为什么走了两个前缀：一个一帧都没取到，一个取到了但看不了",
          video_decode.decode_video(CLEAN).frames == ()
          and len(video_decode.decode_video(FAKE_FRAME_MP4).frames) == 1,
          "裸 MP4 取到 0 帧 / 假 JPEG 取到 1 帧")

    for name, data in (("裸 MP4", CLEAN), ("假 JPEG 帧", FAKE_FRAME_MP4),
                       ("手拼 GIF", GIF_FAKE), ("MJPEG 干净", CLEAN_MJPEG)):
        rep = analyzed(data)
        abstained = {f.check for f in rep.findings if f.verdict == ABSTAIN}
        check(f"关系断言 · {name}：闸门 ∩ 逐项 = ∅，且两者并集 = 全部弃权项",
              gated(rep).isdisjoint(skipped(rep)) and (gated(rep) | skipped(rep)) == abstained,
              f"闸门 {len(gated(rep))} 项、逐项 {len(skipped(rep))} 项、"
              f"弃权共 {len(abstained)} 项")

    base = [_mk(c, "未提示异常", "（构造）") for c in EXPECTED_CHECKS if c != "frames"]
    level_na, _ = summarize(base + [_mk("frames", ABSTAIN, NOT_APPLICABLE_PREFIX + "：帧都看不了")])
    check("**成对断言** · 帧层「逐项不适用」→ 等级照常给出，**不是**「无法判定」",
          level_na == "低", f"实得：{level_na}")
    level_gate, why_gate = summarize(
        base + [_mk("frames", ABSTAIN, GATE_ABSTAIN_PREFIX + "：一帧都没取到")]
    )
    check("**成对断言** · 同一项改成材料级闸门前缀 → 必须变成「无法判定」",
          level_gate == ABSTAIN, f"实得：{level_gate}")
    check("……而且理由里必须点名「整层没有执行」", "整层没有执行" in why_gate,
          f"理由：{why_gate[:80]}")
    level_fired, why_fired = summarize(
        base + [_mk("frames", ABSTAIN, GATE_ABSTAIN_PREFIX + "：一帧都没取到"),
                _mk("structure", ABNORMAL, "（构造）盒树布局异常")]
    )
    check("**成对断言** · 有正向触发时，闸门**不得**把已看到的异常一笔勾销",
          level_fired == "低" and "缺席" in why_fired,
          f"实得：{level_fired}",
          "（这一组合最容易漏交代：判据走「有触发就先算账」，闸门理由根本不会被调用）")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 5 节 · 出口同源：`summary_line()` 不得与 `summarize()` 的判断互相矛盾")
    print("=" * 74)
    print("  说明：`summary_for_leader` 是整份报告里**最容易被原样转述给用户的一行**。")
    print("        图像链在这里踩过一次（内部判「无法判定」、摘要说「均未提示异常」）。")
    print()

    corpus = {
        "裸 MP4": CLEAN,
        "MJPEG 干净": CLEAN_MJPEG,
        "封面帧": CLEAN_COVER,
        "手拼 GIF": GIF_FAKE,
        "真动画 GIF": GIF_REAL,
        "时序矛盾": DUR_MISMATCH,
        "帧率跳变": FPS_JUMP,
        "尾部多余": TRAILING,
        "编辑列表": ELST,
        "假 JPEG 帧": FAKE_FRAME_MP4,
        "编码工具": ENCODER,
        "重复帧": DUP_MJPEG,
    }
    for name, data in corpus.items():
        rep = analyzed(data)
        line = summary_line(rep)
        check(f"摘要里的等级与报告的等级一致 · {name}",
              _level_in(line) == rep.suspicion,
              f"报告 {rep.suspicion} / 摘要 {_level_in(line)!r}")

    gated_line = summary_line(analyzed(CLEAN))
    check("闸门那一支：**不得**出现「均未提示异常」（那读起来就是「没问题」）",
          "均未提示异常" not in gated_line and "整项没有执行" in gated_line,
          f"摘要：{gated_line[:100]}")
    check("闸门那一支：必须写明「不能据此认为「没有问题」」",
          "不能据此认为" in gated_line, f"摘要：{gated_line[95:200]}")

    clean_line = summary_line(analyzed(CLEAN_MJPEG))
    check("干净那一支：报等级 + 说明有几项未参与判级，且**不等于它们也合格**",
          "均未提示异常" in clean_line and "这不等于它们也合格" in clean_line,
          f"摘要：{clean_line[:120]}")

    fired_line = summary_line(analyzed(DUR_MISMATCH))
    check("有触发那一支：必须点名触发项，并交代「这个等级是在谁缺席下算的」",
          "三处时长自洽性" in fired_line and "缺席" in fired_line,
          f"摘要：{fired_line[:150]}")

    check("两处措辞都由代码产生，摘要里不含「真 / 假」这样的断言",
          "篡改认定" in summary_line(analyzed(CLEAN))
          and "未经剪辑的认定" in summary_line(analyzed(CLEAN)),
          f"摘要尾部：{summary_line(analyzed(CLEAN))[-70:]}")
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

    # ⚠️ `too_large` 与 `data_uri_too_large` 都是**按上限**判的，而上限是模块常量。
    #    现行配置下 `MAX_VIDEO_BYTES`（64 MiB）**与落点上限相等**，所以 `too_large`
    #    本来是够不到的（`fetch.py` 的 docstring 里写明了这一点）。
    #    「一个永远走不到的分支」和「不存在」没有区别 ⇒ 这里**把上限临时改小**，
    #    把这两条路真的走一遍。判据是「这条路真的会响」，不是「代码里有这么一段」。
    #    ⚠️ 上限**先**改小再构造载荷：按 64 MiB 造一个 base64 串要 89 MB，
    #    而按改小后的上限造只要几 KB —— 断言不该比被测代码还吃内存。
    produced: set[str] = set()
    original_limit = video_fetch.MAX_VIDEO_BYTES
    small_limit = 4096
    video_fetch.MAX_VIDEO_BYTES = small_limit
    try:
        constructed: dict[str, str] = {
            "unsupported_scheme": "https://example.com/a.mp4",
            "data_uri_malformed": "data:video/mp4;base64",
            "data_uri_not_base64": "data:video/mp4,AAAA",
            "data_uri_mime": "data:image/png;base64,AAAA",
            "data_uri_bad_base64": "data:video/mp4;base64,!!!!",
            "data_uri_empty": "data:video/mp4;base64,",
            "inbox_invalid": "inbox:" + "0" * 64,
            "data_uri_too_large": "data:video/mp4;base64," + "A" * (small_limit * 4 // 3 + 8),
        }
        for kind, uri in constructed.items():
            _, exc = attempt(load, uri)
            got = getattr(exc, "kind", None)
            if isinstance(got, str):
                produced.add(got)
            check(f"拒绝码可构造 · {kind}", got == kind,
                  f"实得 kind={got!r}（异常 {type(exc).__name__}）")

        # -- `too_large`：只能走**收件箱**那条路（`data:` 那条被解码前的上界先拦了）--
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = inbox.bind_root(root)
            try:
                item = inbox.stage(b"\x00" * 8192, root=root, mime_type="video/mp4",
                                   name="big.mp4")
                _, exc = attempt(load, item.handle)
                got = getattr(exc, "kind", None)
                if isinstance(got, str):
                    produced.add(got)
                check("拒绝码可构造 · too_large（收件箱收得下、取用方吃不下）",
                      got == "too_large",
                      f"实得 kind={got!r}",
                      f"构造方式：把取用方上限临时降到 {small_limit}，再放 8192 字节进去"
                      f"（收件箱上限 {inbox.MAX_STAGE_BYTES} 收得下）")
            finally:
                inbox.unbind_root(token)
    finally:
        video_fetch.MAX_VIDEO_BYTES = original_limit

    not_constructed = sorted(set(FETCH_REFUSAL_KINDS) - produced)
    check(
        "**把「能构造出来的码」数一遍**：每一个码都被真实构造出来了（不是「除某个之外」）",
        not_constructed == [],
        f"清单 {len(FETCH_REFUSAL_KINDS)} 个 / 已构造 {len(produced)} 个；"
        f"未构造：{not_constructed}",
    )

    # ⚠️ 2026-10-03 复查夹出的缺陷：`_read_inbox` 自己 `item.path.read_bytes()`，
    #    把 `inbox.load()` 的 OSError 包装**抄丢了** ⇒ 「resolve 时还在、读取时没了」
    #    的字节会以裸 `FileNotFoundError` 逃出去，落进 `outcome()` 的兜底分支拿
    #    通用建议，而不是 `inbox_invalid` 那条专属出路 —— `FETCH_REFUSAL_KINDS`
    #    注释里承诺的「字节不可读」成了空话。
    #    ⚠️ 构造方式必须打准那扇**窗口**：先把文件删掉再调 `load` 是打不中的
    #    （`inbox.resolve()` 自己的 `is_file()` 检查会先拦住，旧代码也能给出
    #    `inbox_invalid` —— 那是别人的判据在兜底，不是被测的那一条在响）。
    #    所以这里把 `inbox.resolve` 包一层：**条目取到之后立刻删字节**，
    #    精确复现「resolve 与 read 之间被清掉」的时序。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        token = inbox.bind_root(root)
        real_resolve = inbox.resolve

        def _resolve_then_vanish(handle: str, **kw: object):  # type: ignore[no-untyped-def]
            item = real_resolve(handle, **kw)  # type: ignore[arg-type]
            item.path.unlink()
            return item

        try:
            gone = inbox.stage(b"\x00" * 512, root=root, mime_type="video/mp4",
                               name="gone.mp4")
            inbox.resolve = _resolve_then_vanish  # type: ignore[assignment]
            try:
                _, exc = attempt(load, gone.handle)
            finally:
                inbox.resolve = real_resolve  # type: ignore[assignment]
            check("退回旧形态即报失败 · resolve 后、读取前字节被清 ⇒ `inbox_invalid`（不是裸 OSError）",
                  isinstance(exc, VideoFetchRefused)
                  and getattr(exc, "kind", None) == "inbox_invalid",
                  f"实得：{type(exc).__name__}，kind={getattr(exc, 'kind', None)!r}",
                  "（退回 `_read_inbox` 自己 read_bytes 的旧写法 ⇒ 裸 FileNotFoundError "
                  "逃过归类、拿不到专属建议，本条断言变红）")
        finally:
            inbox.unbind_root(token)

    missing_fix = [k for k in FETCH_REFUSAL_KINDS if not fix_for(k) or fix_for(k) == fallback]
    check("每一个分类码都有**专属**补救建议（不是兜底那一句）",
          missing_fix == [], f"缺专属建议：{missing_fix}")

    rejects = ("https://a.example.com/x.mp4", "http://a.example.com/x.mp4",
               "file:///etc/passwd", "C:/tmp/x.mp4", "./rel.mp4", "/abs/x.mp4",
               "ftp://a.example.com/x.mp4")
    bad = []
    for uri in rejects:
        _, exc = attempt(check_policy, uri)
        if not isinstance(exc, VideoFetchRefused) or getattr(exc, "kind", None) != "unsupported_scheme":
            bad.append(uri)
    check("出网 / 本机路径**一律拒绝**（本链一条出网通道都没有）", bad == [], f"漏放：{bad}")
    check("能力清单只列了两种引用（`.ALLOWED_SCHEMES`），对大小写不敏感",
          tuple(video_fetch.ALLOWED_SCHEMES) == ("inbox", "data")
          and classify_uri("DATA:video/mp4;base64,AA") == "data"
          and classify_uri("INBOX:" + "A" * 64) == "inbox"
          and classify_uri("file:///x") == "other",
          f"实得：{tuple(video_fetch.ALLOWED_SCHEMES)}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 7 节 · 字节族：工具入口一个异常都不许漏，且两族的建议不许串台")
    print("=" * 74)

    out_bad, err_bad = attempt(outcome, _uri_of(NOT_A_VIDEO))
    reason_bad = str(getattr(out_bad, "refusal_reason", None) or "")
    fix_bad = str(getattr(out_bad, "how_to_fix", None) or "")
    check("非视频字节 → `status=refused`，不是抛异常",
          getattr(out_bad, "status", None) == "refused" and err_bad is None,
          f"status={getattr(out_bad, 'status', None)!r}，异常={err_bad!r}")
    check("非视频字节 → 原因里说清「按魔术字节判定为什么」",
          "不是可识别的视频容器" in reason_bad, f"原因：{reason_bad[:90]}")
    check("非视频字节 → 补救建议指向**调用方**（不是「联系管理员」）",
          "管理员" not in fix_bad and "视频文件本身" in fix_bad,
          f"建议：{fix_bad[:80]}")
    # ⚠️ 这两条是 2026-10-05 补的（与音频链同一处判据漏洞）：「原因那句话」原先
    #    在检测层与 MCP 各写一份，而 `pipeline._FIXES` 里的**路由关键词**又是第三份
    #    手写字面量。三者只要有一处改了、别处不改，后果都是**静默降级** ——
    #    调用方为「你给的不是视频」收到的却是兜底那句「请提交完整未截断的原始视频字节」，
    #    照着错误的归因去改，而**所有"字符串相等"的判据都是绿的**。
    check("字节族那条原因**只能来自** `non_video_clause()`："
          "① 子句必须**派生自**共用前缀；② ACPs 这条路交回的原因必须**以它结尾**",
          non_video_clause("application/octet-stream").startswith(NON_VIDEO_CLAUSE_PREFIX)
          and reason_bad.endswith(non_video_clause("application/octet-stream")),
          f"实得原因={reason_bad!r}")
    check("……而**同一句原因**必须真的**路由到**「你给的不是视频」那条建议"
          "（关键词与子句不同源 ⇒ 悄悄掉进兜底那句，而「路是通的」看起来毫无异常）",
          "视频文件本身" in fix_bad and "完整、未截断的原始视频字节" not in fix_bad,
          f"实得建议：{fix_bad[:80]}")
    check("　└ 反面对照：一句**不含**那个关键词的原因仍然走兜底"
          "（证明上一行是真的匹配上了，不是无条件返回同一句）",
          "视频文件本身" not in fix_for_input("容器结构本身有问题：某个内部错误"),
          f"兜底结果：{fix_for_input('容器结构本身有问题：某个内部错误')[:60]}")

    out_scheme, _ = attempt(outcome, "https://example.com/a.mp4")
    check("出网引用 → `status=refused`，且建议就是取件族那一句",
          getattr(out_scheme, "status", None) == "refused"
          and getattr(out_scheme, "how_to_fix", None) == fix_for("unsupported_scheme"),
          f"建议：{str(getattr(out_scheme, 'how_to_fix', None))[:70]}")

    # ⚠️ 一律加 `or ""`：`fix_for` 万一返回 None（有人加了码却忘配建议），
    #    `in` 会抛 TypeError ⇒ 脚本崩掉，而"崩掉"看起来只是"少打了几行"。
    fix_b64 = fix_for("data_uri_bad_base64") or ""
    check(
        "**两族不许串台**：参数写错（坏 base64）不许被答成「容器结构本身有问题」",
        "base64" in fix_b64 and "容器结构" not in fix_b64,
        f"坏 base64 的建议：{fix_b64[:60]}",
        "（取件族走分类码、字节族才走关键词 —— 靠调关键词治不了串台）",
    )
    check("**两族不许串台（反向）**：字节族的原因不许被答成取件族的建议",
          fix_for_input("不是可识别的视频容器…") != fix_for("unsupported_scheme"),
          "字节族与取件族各自返回自己的那一句")

    good_uri = _uri_of(CLEAN_MJPEG)
    out_ok, err_ok = attempt(outcome, good_uri, context_note="据称是行车记录仪导出")
    res_ok = getattr(out_ok, "result", None)
    check("正常输入 → `status=ok`",
          getattr(out_ok, "status", None) == "ok" and err_ok is None,
          f"status={getattr(out_ok, 'status', None)!r}，异常={err_ok!r}")
    check("**收下的参数必须往下传**：`context_note` 原样回显，不被静默丢弃",
          getattr(res_ok, "context_note", None) == "据称是行车记录仪导出",
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
            item = inbox.stage(CLEAN_MJPEG, root=root, mime_type="video/mp4", name="a.mp4")
            out_inbox, err_inbox = attempt(outcome, item.handle)
            res_inbox = getattr(out_inbox, "result", None)
            check("随任务附件进来的视频 → `fetched_from=inbox`（首发也是最推荐的一条）",
                  err_inbox is None and getattr(res_inbox, "fetched_from", None) == "inbox",
                  f"实得：{getattr(res_inbox, 'fetched_from', None)!r}")
            bad_handle, _ = attempt(outcome, "inbox:" + "0" * 64)
            check("未登记的句柄 → refused + 「请确认它是本次任务附带的视频」这一类可执行出路",
                  getattr(bad_handle, "status", None) == "refused"
                  and bad_handle.how_to_fix == fix_for("inbox_invalid"),
                  f"建议：{(bad_handle.how_to_fix or '')[:70]}")
        finally:
            inbox.unbind_root(token)
    print()

    # =======================================================================
    print("=" * 74)
    print("第 8 节 · 畸形字节：`decode_video` 承诺「从不抛异常」，把它兑现一遍")
    print("=" * 74)
    print("  说明：`pipeline.py` 的注释里写着「`decode.py` 从不抛异常 ⇒ 整条链上只剩"
          "一个抛点」。")
    print("        那条论证是 `outcome()` 的 `except` 表能覆盖全部失败路径**唯一**的"
          "依据 ——")
    print("        如果它不成立，调用方拿到的就是本函数声称要避免的那种不透明异常。")
    print()

    malformed: dict[str, bytes] = {
        "空字节": b"",
        "全零 32 字节": b"\x00" * 32,
        "干净样本截一半": CLEAN[: len(CLEAN) // 2],
        "干净样本截到只剩 12 字节": CLEAN[:12],
        "ftyp 之后直接断掉": b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom",
        "盒长声明成 1（64 位）但不足 16 字节": b"\x00\x00\x00\x01moov\x00\x00",
        "盒长小于盒头自身": b"\x00\x00\x00\x04moov",
        "盒长声明得比文件还大": b"\x00\x00\xff\xffmoov" + b"\x00" * 32,
        "GIF 头之后断掉": b"GIF89a\x10\x00\x10\x00",
        "GIF 只有签名": b"GIF89a",
        "RIFF AVI 头 + 乱码": b"RIFF\xff\xff\xff\xffAVI " + b"\x41" * 40,
        "EBML 头 + 乱码": b"\x1aE\xdf\xa3" + b"\x42" * 40,
        "magic 是 mp4 但没有 moov": b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
                                    + b"\x00\x00\x00\x08mdat",
        "一段散文": NOT_A_VIDEO,
    }
    raises: list[str] = []
    for name, blob in malformed.items():
        _, exc = attempt(decode_video, blob)
        if exc is not None:
            raises.append(f"{name}（{type(exc).__name__}: {exc}）")
    check(
        "`decode_video` 对**这批畸形字节**一律不抛异常（一条都没有）",
        raises == [],
        f"抛了的：{raises}",
        "（`decode.py` 的纪律是：任何畸形字节都折成 `facts.notes` + `frames_note`）",
    )

    analyze_raises: list[str] = []
    unexpected: list[str] = []
    for name, blob in malformed.items():
        res, exc = attempt(analyze, blob)
        if exc is None:
            continue
        if isinstance(exc, ValueError):
            analyze_raises.append(name)
        else:
            unexpected.append(f"{name}（{type(exc).__name__}: {exc}）")
    check("`analyze` 只允许抛 `ValueError`（「不是可识别的视频容器」这一条）",
          unexpected == [], f"抛了别的：{unexpected}")
    check("……而且真的有一批畸形字节走进了那条拒绝分支（不是全靠 `except` 蒙混）",
          len(analyze_raises) >= 3, f"被拒绝的：{analyze_raises}")

    outcome_raises: list[str] = []
    for name, blob in malformed.items():
        res, exc = attempt(outcome, _uri_of(blob))
        if exc is not None:
            outcome_raises.append(f"{name}（{type(exc).__name__}: {exc}）")
        elif getattr(res, "status", None) not in ("ok", "refused"):
            outcome_raises.append(f"{name}（status={getattr(res, 'status', None)!r}）")
    check("**工具入口一个异常都不漏**：畸形字节全部折成 `status ∈ {ok, refused}`",
          outcome_raises == [], f"漏出来的：{outcome_raises}")

    # ⚠️ 2026-10-03 不变量探针逮到的缺陷：**同一份字节两次跑出的报告不一样**
    #    —— Pillow 的报错带着 `<_io.BytesIO object at 0x000002…>` 这种内存地址，
    #    于是 `frames_note` 与 `findings` 每次都不同。
    #    这不是洁癖：旁证复核时"两份报告不一致"最容易被读成"材料被改过"，
    #    而它其实什么都没说。报告必须是**同一份字节的可重复产物**。
    volatile: list[str] = []
    for label, blob in (("封面帧（不可解码的 JPEG）", V.with_cover(FAKE_JPEG)),
                        ("MJPEG（不可解码）", FAKE_FRAME_MP4),
                        ("裸 MP4", CLEAN), ("真 GIF", GIF_REAL)):
        first = analyze(blob).model_dump_json()
        second = analyze(blob).model_dump_json()
        if first != second:
            volatile.append(label)
        # 副作用 purify：报告全文里不许再出现内存地址
        if re.search(r"0x[0-9a-fA-F]{8,}", first):
            volatile.append(label + "（报告里仍有内存地址）")
    check("同一份字节两次跑出的报告**逐字节相同**（报告必须是可重复产物）",
          volatile == [],
          f"不一致的：{volatile}",
          "（退回 `{type(exc).__name__}: {exc}` 的旧写法 ⇒ 地址不同而以为是 malware）")

    # `stable_reason` 只抹"对象 at 0x 地址"，**不许**顺手删掉有用的十六进制
    from legal_acps.verdict import stable_reason as sanitize

    kept = sanitize(ValueError("未在规范里的块标识 0x2c"))
    scrubbed = sanitize(
        OSError("cannot identify image file <_io.BytesIO object at 0x00000212F958C770>")
    )
    check("净化器只抹内存地址，**保留**措辞里本来就有用的十六进制",
          "0x2c" in kept and "0x00000212" not in scrubbed and "<对象>" in scrubbed,
          f"保留：{kept}",
          f"净化：{scrubbed}",
          "（一刀切地把 0x 抹掉 ⇒ 「块标识 0x2c」变成「块标识 」，好心删了证据）")
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
              f"{len(ladder) if ladder else 0} 档：{' / '.join(n[:20] for n in names)}",
              "（每一档都返回，不随等级裁剪 —— 从「低」里删掉司法鉴定等于对用户隐藏唯一救济路径）")
    check("复检档位的取值域固定",
          tiers_seen == {"online_free", "online_paid", "offline_forensic"},
          f"实得：{sorted(tiers_seen)}")
    check("第一档必须给出**可立刻执行**的免费工具（否则它只是一句安慰）",
          "MediaInfo" in plan_upgrades("低")[0].name, plan_upgrades("低")[0].name[:50])

    check("上下限不变量：**落点上限 ≥ 取用方上限**（不然视频永远撞不到自己的上限）",
          inbox.MAX_STAGE_BYTES >= MAX_VIDEO_BYTES,
          f"收件箱 {inbox.MAX_STAGE_BYTES} ≥ 视频 {MAX_VIDEO_BYTES}")
    limits = (MAX_VIDEO_BYTES, vm.MAX_DEGRADE_BYTES)
    check("取用方上限与降级器上限**同量级**（两者面向同一类输入，不该差一个数量级）",
          max(limits) <= min(limits) * 8,
          f"取用方 {MAX_VIDEO_BYTES} / 降级器 {vm.MAX_DEGRADE_BYTES}",
          "（两条路进来的都是「整段视频」：一条经取件层、一条直接来自入站附件）")
    check("单帧上限**远小于**整段上限（一帧只是抽样，不该与整个文件同量级）",
          MAX_FRAME_BYTES < MAX_VIDEO_BYTES // 8,
          f"单帧 {MAX_FRAME_BYTES} / 整段 {MAX_VIDEO_BYTES}")
    check("权重量表覆盖全部七项、且都是正数（漏一项 ⇒ 它的线索永远升不了级）",
          set(CHECK_WEIGHTS) == set(EXPECTED_CHECKS)
          and all(w > 0 for w in CHECK_WEIGHTS.values()),
          f"实得：{CHECK_WEIGHTS}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 10 节 · 两张 PPT 的机制：`ContentTransform` 降级 + `GatewayAdapter` 适配")
    print("=" * 74)
    print("  说明：这一节测的是**机制真的接上了**，不是「代码里有这么个类」。")
    print("        判据沿用本项目那条：先问「谁来调它」，再问「调了管不管用」。")
    print()

    # -- ContentTransform：注册形状 -------------------------------------
    transform = vm.video_content_transform()
    check("`video_content_transform()` 认的是 `source_type='video'`",
          transform.source_type == "video" and callable(transform.handler),
          f"source_type={transform.source_type!r}")

    hooks = vm.VideoFallbackHooks()
    got = hooks.content_transforms(None)
    check("`VideoFallbackHooks` 真的把这条 Transform 交了出去（不是空列表）",
          len(got) == 1 and got[0].source_type == "video",
          f"id={hooks.id!r} priority={hooks.priority} 条数={len(got)}")

    # -- 降级产物：四支出口，一支一支数 ---------------------------------
    plain = VideoPart(mime_type="video/mp4", data=CLEAN_MJPEG)
    parts = vm.degraded_parts(plain, None)
    check("有帧可抽 ⇒ 「说明 + 图」两段，且**说明在前**（顺序本身是判据）",
          len(parts) == 2 and isinstance(parts[0], TextPart)
          and isinstance(parts[1], ImagePart),
          f"实得：{[type(p).__name__ for p in parts]}")
    notice = parts[0].text
    check("说明以固定前缀开头（断言能钉住「说明必须跟着图走」）",
          notice.startswith(vm.DEGRADE_NOTICE_PREFIX), f"前缀：{notice[:20]!r}")
    check("说明里必须写「一帧 ≠ 全片」与「容器线索请走视频鉴伪链路」",
          "一帧 ≠ 全片" in notice and "视频鉴伪" in notice, f"说明：{notice[-90:]}")
    check("图就是容器里那一帧的**原始字节**（不经任何重编码）",
          parts[1].data == JPEG_A, f"{len(parts[1].data)} 字节，与源 JPEG 逐字节相同")

    gif_parts = vm.degraded_parts(
        VideoPart(mime_type="video/gif", data=GIF_REAL), None
    )
    check("GIF（解不出帧）⇒ 直接把**原文件**当图片发，不引入 Pillow、不重编码",
          len(gif_parts) == 2 and isinstance(gif_parts[1], ImagePart)
          and gif_parts[1].mime_type == "image/gif" and gif_parts[1].data == GIF_REAL,
          f"实得：{gif_parts[1].mime_type!r}，字节与源文件相同")

    no_frame = vm.degraded_parts(
        VideoPart(mime_type="video/mp4", data=CLEAN),  # H.264 类：一帧都抽不到
        None,
    )
    check("抽不到帧且不是 GIF ⇒ **只**返回一段文本（绝不返回空、更不编一张图）",
          len(no_frame) == 1 and isinstance(no_frame[0], TextPart)
          and "画面层整项没有进入模型视野" in no_frame[0].text,
          f"实得：{[type(p).__name__ for p in no_frame]}")

    uri_only = vm.degraded_parts(VideoPart(mime_type="video/mp4", uri="https://example.com/a.mp4"), None)
    check("只有地址没有字节 ⇒ 文本 + 明写「系统不会自动去取这个地址」",
          len(uri_only) == 1 and "不会自动去取" in uri_only[0].text
          and "不出网" in uri_only[0].text,
          f"说明：{uri_only[0].text[-100:]}")

    huge = vm.degraded_parts(
        VideoPart(mime_type="video/mp4", data=b"\x00" * (vm.MAX_DEGRADE_BYTES + 1)),
        None,
    )
    check("超过降级器上限 ⇒ 如实拒绝并写明**没有抽帧**（不静默截断）",
          len(huge) == 1 and "没有抽帧" in huge[0].text,
          f"说明：{huge[0].text[:90]}")

    check("**永远不返回空列表**（`TransformHandler` 的空语义会让下游收到原 `VideoPart`）",
          all(bool(vm.degraded_parts(p, None)) for p in (
              VideoPart(mime_type="video/mp4", data=CLEAN_MJPEG),
              VideoPart(mime_type="video/mp4", data=CLEAN),
              VideoPart(mime_type="video/gif", data=GIF_REAL),
              VideoPart(mime_type="video/mp4", uri="https://example.com/a.mp4"),
              VideoPart(mime_type="video/mp4", data=b"\x00" * (vm.MAX_DEGRADE_BYTES + 1)),
          )),
          "送进去五种形态，每一种都必须有输出")

    class _Exploding:
        """一个访问任何属性都炸的对象 —— 验证 handler 的防御层真的在。"""

        def __getattr__(self, name: str):
            raise RuntimeError(f"故意炸：{name}")

    rescued = vm.video_to_media(_Exploding(), None)
    check("handler 对**任何**异常都折成可读文本（这里的失败代价是整个 run 挂掉）",
          len(rescued) == 1 and isinstance(rescued[0], TextPart)
          and rescued[0].text.startswith(vm.DEGRADE_NOTICE_PREFIX),
          f"实得：{rescued[0].text[:80]}")

    # -- GatewayAdapter：能力探测 + 显式传参 ----------------------------
    class _FakeProvider:
        """一个「不支持视频」的 provider —— 只实现探测需要的那一个方法。"""

        def supports_content(self, part, *, role: str = "user"):
            if type(part).__name__ == "VideoPart":
                return False, "本假 provider 不吃视频"
            return True, None

    class _VideoAwareProvider:
        """一个「认视频」的 provider。"""

        def supports_content(self, part, *, role: str = "user"):
            return True, None

    class _NoProbe:
        """没有 `supports_content` 的对象 —— 探测只能失败。"""

    # ⚠️ 「套适配器」那一支**必须用真实的 provider 对象**来测：
    #    `AdaptedModelProvider` 要读 `base.capabilities`，拿一个鸭子类型的假对象
    #    去测，测到的会是 `TypeError`，而不是我们想验的那个分支 —— 断言有"通过
    #    有"失败"，唯独没测到被测代码。
    #    `OpenAIChatProvider` 离线就能构造，且它对视频的回答正是本项目记录在案的
    #    那一条（`Chat user messages have no native video content part`）。
    real = OpenAIChatProvider(model="probe", api_key="not-used")

    ok_aware, _ = vm.probe_video_support(_VideoAwareProvider())
    ok_real, why_real = vm.probe_video_support(real)
    ok_fake, why_fake = vm.probe_video_support(_FakeProvider())
    ok_none, why_none = vm.probe_video_support(_NoProbe())
    check("能力探测只认 provider **自己**的回答，不认我们的猜测",
          ok_aware is True and ok_real is False and ok_fake is False and ok_none is False,
          f"认视频={ok_aware} / 真 provider={ok_real} / 不认={ok_fake} / 没法探={ok_none}")
    check("……真实 provider 的能力表里确实**没有 video**（这是探测结论的依据）",
          "video" not in real.capabilities.input_modalities and ok_real is False,
          f"input_modalities={sorted(real.capabilities.input_modalities)}")
    check("……不认时要把 provider 自报的原因原样带出来（那是能拿去改配置的信息）",
          why_fake == "本假 provider 不吃视频" and bool(why_real)
          and "no native video" in why_real,
          f"真 provider：{why_real!r}；假 provider：{why_fake!r}")
    check("……**探测不出来就当不支持**（当作支持的失败模式是 run 直接挂）",
          ok_none is False and bool(why_none), f"实得：{ok_none}；原因={why_none!r}")

    # ⚠️ 2026-10-03 复查夹出的缺陷：形状不符时旧写法是 `bool(result)` ——
    #    一个答非所问但返回真值的 provider 会被当成「支持视频」放行，
    #    与同一行上方注释「不当成 True」直接矛盾。修法是 fail-closed。
    class _BareTrueProvider:
        """违反契约形状：返回裸 `True`（PPT 4-8 的契约是 `(bool, reason)` 元组）。"""

        def supports_content(self, part, *, role: str = "user"):
            return True

    class _ThreeTupleProvider:
        """违反契约形状：返回三元组（多塞一个字段也算形状不符）。"""

        def supports_content(self, part, *, role: str = "user"):
            return True, None, "extra"

    ok_bare, why_bare = vm.probe_video_support(_BareTrueProvider())
    ok_three, why_three = vm.probe_video_support(_ThreeTupleProvider())
    check("退回旧形态即报失败 · 形状不符（裸 True / 三元组）⇒ **按不支持处理**，不放行",
          ok_bare is False and ok_three is False and bool(why_bare) and bool(why_three),
          f"裸 True：{ok_bare}（{why_bare}）；三元组：{ok_three}",
          "（退回 `bool(result)` 旧写法 ⇒ 裸 True 被放行，本条断言变红 —— "
          "后果是 VideoPart 原样发出去、整个 run 挂掉）")

    check("`model_provider_for(None)` 原样返回（离线假模型不许被造出一个 provider 来）",
          vm.model_provider_for(None) is None)
    base_fake = _FakeProvider()
    check("不认 + 后端也不认 ⇒ 原样返回，由 `ContentTransform` 负责降级",
          vm.model_provider_for(base_fake) is base_fake)
    adapted = vm.model_provider_for(real, backend_reads_video=True)
    check("不认 + **显式确认后端认视频** ⇒ 才套一层 `AdaptedModelProvider`",
          adapted is not real and type(adapted).__name__ == "AdaptedModelProvider",
          f"实得：{type(adapted).__name__}",
          "（`added_input_modalities` 是一句**承诺**：挂错地方 = 模型收到它读不了的东西）")
    base_aware = _VideoAwareProvider()
    check("provider 自己就认视频 ⇒ 什么都不用做（原样返回）",
          vm.model_provider_for(base_aware) is base_aware)

    adapter = vm.VideoGatewayAdapter()
    check("适配器把「加了 video 模态」这件事**显式声明**出来",
          adapter.added_input_modalities == frozenset({"video"})
          and adapter.name == "legal.video.gateway-adapter",
          f"name={adapter.name!r} added={sorted(adapter.added_input_modalities)}")

    # -- 预览帧的挑选与措辞 ---------------------------------------------
    cover_decoded = decode_video(CLEAN_COVER)
    picked = vm.pick_preview_frame(cover_decoded.frames)
    check("预览帧偏好**真第 0 帧**，封面帧（序号 -1）只是退而求其次",
          picked is not None and picked.index == -1
          and "封面" in vm.preview_source_note(picked),
          f"挑中的 index={picked.index if picked else None}；"
          f"说明={vm.preview_source_note(picked)[:40] if picked else '-'}")
    mjpeg_decoded = decode_video(CLEAN_MJPEG)
    picked2 = vm.pick_preview_frame(mjpeg_decoded.frames)
    check("……有真帧时必须挑真帧，并把「第 1 帧」写进说明（不许含糊）",
          picked2 is not None and picked2.index == 0
          and "第 1 帧" in vm.preview_source_note(picked2),
          f"说明={vm.preview_source_note(picked2) if picked2 else '-'}")
    _, why_preview = vm.preview_bytes(CLEAN)
    check("抽不到帧时，`preview_bytes` 的**第二个返回值非空**（原因不能丢）",
          bool(why_preview.strip()), f"原因：{why_preview[:70]}")
    print()

    # =======================================================================
    print("=" * 74)
    print("第 11 节 · 接线：模态枚举 + 三条链的角色各收各的模态、各挂各的 Hook")
    print("=" * 74)

    try:
        from legal_acps.evidence_hooks import KIND_PROFILES, profile_for
    except ImportError as exc:  # pragma: no cover
        check("能导入 `legal_acps.evidence_hooks`", False, f"ImportError：{exc}")
    else:
        prof = profile_for("video")
        check("模态画像 · video：措辞、参数名、上线用的 Part 类都对",
              prof.noun == "视频" and prof.uri_arg == "video_uri"
              and prof.part_class == "VideoPart",
              f"{prof.noun}/{prof.uri_arg}/{prof.part_class}")
        check("画像表覆盖**全部**模态（清单从 `EvidenceModality` 现场派生）",
              {m.kind for m in EvidenceModality} <= set(KIND_PROFILES),
              f"实得：{sorted(KIND_PROFILES)}")
        check("模态枚举的 `kind` 与角色 id 的约定一致（`evidence-<kind>`）",
              EvidenceModality.VIDEO.kind == "video" and EvidenceModality.VIDEO.value == "视频",
              f"kind={EvidenceModality.VIDEO.kind!r} 中文={EvidenceModality.VIDEO.value!r}")

    try:
        import legal_acps.agents as A
    except ImportError as exc:  # pragma: no cover
        check("能导入 `legal_acps.agents`（视频角色已接线）", False, f"ImportError：{exc}")
    else:
        def hooks_named(agent, kind: str) -> list[object]:
            return [h for h in (agent.hooks or []) if type(h).__name__ == kind]

        def build(builder_name: str):  # type: ignore[no-untyped-def]
            agent, err = attempt(getattr(A, builder_name), None)
            if err is not None or agent is None:
                check(f"能构建角色 {builder_name}", False, f"{type(err).__name__}: {err}")
            return agent

        # ⚠️ 角色清单**现场派生**：`VIDEO_CAPABLE_ROLES` 与 `BUILDERS` 两处若漂了，
        #    下面这条会红 —— 而不是靠人去记得同步改两个地方。
        want = set(vm.VIDEO_CAPABLE_ROLES)
        missing_roles = sorted(want - set(A.BUILDERS))
        check("`VIDEO_CAPABLE_ROLES` 里每个角色都真的存在（否则那句声明是空的）",
              missing_roles == [], f"`BUILDERS` 里找不到：{missing_roles}")

        for role in sorted(want):
            agent = build(A.BUILDERS[role].__name__)
            if agent is None:
                continue
            check(f"角色 `{role}` 挂了视频降级 Hook（不挂 ⇒ 一段视频会让整个 run 挂）",
                  len(hooks_named(agent, "VideoFallbackHooks")) == 1,
                  f"实得：{[type(h).__name__ for h in (agent.hooks or [])]}")

        holders = set()
        for role, builder in A.BUILDERS.items():
            agent, err = attempt(builder, None)
            if err is None and agent is not None and hooks_named(agent, "VideoFallbackHooks"):
                holders.add(role)
        check("……而且**只有**它们挂了（挂在别处就是死代码：没有入站视频会到那里）",
              holders == want, f"实得：{sorted(holders)}，应恰好是 {sorted(want)}",
              "（纪律：先问「谁来调它」，再问「调了管不管用」）")

        # ⚠️ 关系断言：挂了 `EvidenceInboxHooks` 且收 video 的角色，必须同时挂降级 Hook。
        #    这是「装了 ≠ 会响」的对偶：**收得进视频、却没人负责降级**，
        #    链路会在模型边界上炸，而每一环看起来都是成功的。
        bad_relations: list[str] = []
        for role, builder in A.BUILDERS.items():
            agent, err = attempt(builder, None)
            if err is not None or agent is None:
                continue
            inbox_hooks = hooks_named(agent, "EvidenceInboxHooks")
            takes_video = any("video" in tuple(getattr(h, "kinds", ()) or ()) for h in inbox_hooks)
            has_fallback = bool(hooks_named(agent, "VideoFallbackHooks"))
            if takes_video and not has_fallback:
                bad_relations.append(role)
        check(
            "**关系断言** · 凡是收得进视频的角色，都必须挂降级 Hook（装了≠会响）",
            bad_relations == [], f"收了视频却没挂降级 Hook 的角色：{bad_relations}",
        )

        video_agent = build("build_evidence_video")
        if video_agent is not None:
            inbox_hooks = hooks_named(video_agent, "EvidenceInboxHooks")
            check("`evidence-video` 只收视频（模态不许在两边一起变宽）",
                  len(inbox_hooks) == 1 and set(inbox_hooks[0].kinds) == {"video"},
                  f"实得：{[tuple(h.kinds) for h in inbox_hooks]}")
            check("`evidence-video` 是叶子角色 ⇒ 收件 Hook 必须关掉「接管委派」",
                  len(inbox_hooks) == 1 and inbox_hooks[0].delegate_tool == "",
                  f"delegate_tool={inbox_hooks[0].delegate_tool!r}" if inbox_hooks else "（没挂上）")
            check("`evidence-video` 必须挂终止强制（交付物只能靠工具产生）",
                  len(hooks_named(video_agent, "LeaderTerminationHooks")) == 1,
                  "`video_forensics_analyze` 出结论后才会收尾")
    print()

    # =======================================================================
    print("=" * 74)
    if FAILED:
        print(f"有 {len(FAILED)} 个用例未达预期：")
        for label in FAILED:
            print(f"  · {label}")
        return 1
    print("全部用例符合预期：")
    print("  · 4 份负对照零触发（含两份真的跑起帧层的样本）—— 误报是最致命的失败")
    print("  · 而已见的正样本分别被**预期的检查项**抓到，报出的量值就是造样本时写死的值")
    print("  · **本轮夹出来的 5 个缺陷逐条钉死**：封面帧、MJPEG 取帧、非 ASCII 4CC、")
    print("    空 reason、GIF 原因错位 —— 每一条都能回答「退回旧写法会红吗」")
    print("  · 「帧层整层没跑」与「某一项不适用」是两层弃权，由成对断言钉住")
    print("  · 每一节的出口文案都与判据同源；闸门那一支绝不会写成「均未提示异常」")
    print("  · 取件族 9 个分类码**逐码**有专属出路，且**全部**已被真实构造出来")
    print("  · 工具入口一个异常都不漏；两族补救建议不串台（坏 base64 ≠ 容器坏了）")
    print("  · 畸形字节从 `decode_video` 里一个异常都不许漏出来")
    print("  · 复检阶梯不随等级裁剪；上下限不变量成立")
    print("  · **两张 PPT 的机制**：ContentTransform 五种形态都有输出、GatewayAdapter")
    print("    只在「探测 + 显式传参」都成立时才挂（`added_input_modalities` 是承诺）")
    print("  · 三个可能收到视频的角色各收各的模态、各挂各的 Hook（装了≠会响）")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
