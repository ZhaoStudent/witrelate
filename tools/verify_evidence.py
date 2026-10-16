"""离线验证：图像鉴伪初检（`legal_acps/evidence/`）。

**不联网、不需要 API key、不碰平台、不调模型** —— 全部是确定性纯函数的断言。
判据只有一个：`exit 0 = 全绿`。

用法（在 witrelate 目录下）：
    ./.venv/Scripts/python.exe -m tools.verify_evidence

════════════════════════════════════════════════════════════════════════
它到底在测什么
════════════════════════════════════════════════════════════════════════

**负对照比正样本重要。** 一个鉴伪工具最危险的失败不是漏报，是误报：
它会把一张正常照片说成"有篡改嫌疑"，而使用者没有任何手段复核我们。
所以本脚本里 3/4 的样本是"**不该被触发**"的：

  负对照                        为什么必须干净
  ─────────────────────────────────────────────────────────────
  干净 PNG（有真实传感器噪声）   最普通的输入
  干净 JPEG q85                 最常见的输入，块网格检查最容易在这里乱报
  纯周期纹理 JPEG q92           砖墙/栏杆/条纹；复制-移动检查的经典假阳性源
  大图 2004x1508（会被我们裁剪） 我们自己的裁剪不得伪造出"网格偏移"痕迹
  大片纯色 JPEG q70             截图 / 拍白墙 / 拍文件；像素域方法的适用前提

然后才验证"该抓的要抓到"（拼接 / 克隆 / 元数据矛盾 / 块网格相位偏移），
以及**参数归属**：结论等级、升级建议、哈希、尺寸都必须由代码算，
不能有一项来自模型或调用方。

最后三节守的是**边界**，它们的共同判据是"**出错时要给得出东西，而不是给出一句报错**"：

  第 6 节  畸形字节 / 截断图像 / 超大图 / MIME 别名 / 未声明类型
           —— 工具入口承诺"永远返回结构化结果"，一个异常都不许漏给调用方；
              漏出去的后果不是"报错"，而是调用方**换个路子把图弄进来**，正好绕过刚设的边界。
  第 7 节  取图策略 fail-closed（未配白名单=禁止一切出网）、来源标注如实、超限判定在解码之前
  第 8 节  Agent 接线：新角色不得把 Pillow/numpy 的依赖风险引到已经过审的主链路上
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from legal_acps.evidence import analyze, inbox, plan_upgrades, summarize
from legal_acps.evidence.contracts import Finding
from legal_acps.evidence.forensics import analysis_crop_box, pixel_budget_exceeded
from legal_acps.evidence.pipeline import summary_line
from run.samples import (
    adobe_exif,
    copy_move_photo,
    flat_area_image,
    jpeg,
    oversized_png,
    png,
    photo,
    recrop_jpeg,
    splice_photo,
)

FAILED: list[str] = []

#: `analyze` 结果缓存。整份脚本要反复对同一批样本取报告，而 `analyze` 里最贵的是
#: 复制-移动的块配对（O(n²) 有预算上限）。不缓存时单次运行约 39 秒，
#: 而 `run/meta_verify_evidence.py` 要跑它很多遍 —— 缓存后单次降到个位数秒，
#: 元验证才不会被工具超时掐断。
#:
#: ⚠️ 键必须用**完整摘要**而不是 `hash()`：`hash()` 是 64 位、且没有跨版本/跨进程保证，
#: 万一撞一次就会让某条断言拿到**另一个样本的报告** —— 测试给出的答案会错得无声无息。
_CACHE: dict[tuple[str, tuple[tuple[str, str], ...]], object] = {}


def analyzed(data: bytes, **kw: object):  # type: ignore[no-untyped-def]
    key = (hashlib.sha256(data).hexdigest(), tuple(sorted((k, str(v)) for k, v in kw.items())))
    if key not in _CACHE:
        _CACHE[key] = analyze(data, **kw)  # type: ignore[arg-type]
    return _CACHE[key]


def check(label: str, ok: bool, *notes: str) -> None:
    mark = "通过" if ok else "未达预期"
    if not ok:
        FAILED.append(label)
    print(f"  [{mark}] {label}")
    for note in notes:
        print(f"          {note}")


def fired(report) -> set[str]:  # type: ignore[no-untyped-def]
    """触发集 = 判定为「提示异常」的检查项名。"""
    return {f.check for f in report.findings if f.verdict == "提示异常"}


# ---------------------------------------------------------------------------
# 样本（与 run/probe_evidence.py 同源，全部来自 run/samples.py）
# ---------------------------------------------------------------------------

N_CLEAN_PNG = png(photo("structured"))
N_CLEAN_JPEG = jpeg(photo("structured"), 85)
N_PERIODIC = jpeg(photo("periodic"), 92)
N_BIG_CROPPED = jpeg(photo("structured", w=2004, h=1508), 60)
N_FLAT_AREA = jpeg(flat_area_image(), 70)

# ⚠️ 正样本一律从 run/samples.py 取 —— 断言脚本自己另造一份样本，
#   就会变成"看起来检测器坏了、其实是样本写错了"（初版 P1 就栽在这里）。
P_CLONE = jpeg(copy_move_photo(), 92)
P_SPLICE = jpeg(splice_photo(), 92)
P_EXIF = jpeg(photo("structured"), 88, exif=adobe_exif())
P_GRIDSHIFT = recrop_jpeg(jpeg(photo("structured"), 85), dx=4, dy=4)

ALL_SAMPLES = {
    "N1 干净PNG": N_CLEAN_PNG,
    "N2 干净JPEG q85": N_CLEAN_JPEG,
    "N3 纯周期纹理 q92": N_PERIODIC,
    "N4 大图被裁剪 2004x1508": N_BIG_CROPPED,
    "N5 大片纯色 JPEG q70": N_FLAT_AREA,
    "P1 拼接 q92": P_SPLICE,
    "P2 复制-移动 q92": P_CLONE,
    "P3 元数据矛盾 q88": P_EXIF,
    "P4 裁剪错位4px+q95": P_GRIDSHIFT,
}


def main() -> int:
    import base64 as _b64

    print("=" * 74)
    print("第 1 节 · 契约：每条发现都必须自带 caveat，且不得输出「真 / 假」")
    print("=" * 74)
    rep = analyzed(N_CLEAN_JPEG)
    legal_verdicts = {"未提示异常", "提示异常", "无法判定"}
    check("Finding.verdict 的取值域恰好是三个（**没有**「真」「假」「伪造」）",
          legal_verdicts == {"未提示异常", "提示异常", "无法判定"},
          f"契约侧允许：{sorted(legal_verdicts)}")
    bad = [f.check for f in rep.findings if f.verdict not in legal_verdicts]
    check("实际输出的每条 verdict 都在取值域内", not bad, f"越界项：{bad}")
    check("每条发现都带非空 caveat（说明「它不能证明什么」）",
          all(f.caveat.strip() for f in rep.findings),
          f"{len(rep.findings)} 条发现，caveat 全部非空")
    check("每条发现的 check / detail 都非空",
          all(f.check.strip() and f.detail.strip() for f in rep.findings))
    check("strength 若非空则落在 [0,1]",
          all(f.strength is None or 0.0 <= f.strength <= 1.0 for f in rep.findings))
    check("免责声明明确「不是司法鉴定意见」",
          "不是司法鉴定" in rep.disclaimer)

    print()
    print("=" * 74)
    print("第 2 节 · 负对照：这些图**不得**被触发任何一项（误报是致命缺陷）")
    print("=" * 74)
    check("N1 干净 PNG（有真实传感器噪声）不触发任何项",
          fired(analyzed(N_CLEAN_PNG)) == set(),
          f"触发集 = {sorted(fired(analyzed(N_CLEAN_PNG))) or '空'}")
    f_n2 = fired(analyzed(N_CLEAN_JPEG))
    check("N2 干净 JPEG q85 不触发任何项（块网格检查的主要假阳性来源）",
          f_n2 == set(), f"触发集 = {sorted(f_n2) or '空'}")
    f_n3 = fired(analyzed(N_PERIODIC))
    check("N3 纯周期纹理不触发任何项（尤其不得报「复制-移动」）",
          f_n3 == set(), f"触发集 = {sorted(f_n3) or '空'}")
    rep_big = analyzed(N_BIG_CROPPED)
    grid_big = [f for f in rep_big.findings if f.check == "block_grid"][0]
    check("N4 大图（会被我们自己中心裁剪）不报「块网格偏移」—— 裁剪相位回归",
          grid_big.verdict != "提示异常",
          f"block_grid 判定 = {grid_big.verdict}；{grid_big.detail[:70]}")
    f_n5 = fired(analyzed(N_FLAT_AREA))
    check("N5 大片纯色 JPEG（截图 / 拍白墙）不触发任何项 —— 平坦区噪声闸门",
          f_n5 == set(),
          f"触发集 = {sorted(f_n5) or '空'}"
          "（闸门去掉时这里会报 flat_noise：JPEG 量化本来就把平坦区噪声抹平了）")

    print()
    print("=" * 74)
    print("第 3 节 · 正样本：该抓的要抓到，且要抓在**预期的检查项**上")
    print("=" * 74)
    f_p1 = fired(analyzed(P_SPLICE))
    check("P1 拼接 → noise_residual 触发", "noise_residual" in f_p1,
          f"触发集 = {sorted(f_p1)}")
    rep_p2 = analyzed(P_CLONE)
    f_p2 = fired(rep_p2)
    cm = [f for f in rep_p2.findings if f.check == "copy_move"][0]
    check("P2 复制-移动 → copy_move 触发", "copy_move" in f_p2,
          f"触发集 = {sorted(f_p2)}")
    check("……且报出的位移量就是真实的 312 像素",
          "312" in cm.detail, cm.detail[:100])
    f_p3 = fired(analyzed(P_EXIF))
    check("P3 元数据矛盾 → exif 触发", "exif" in f_p3, f"触发集 = {sorted(f_p3)}")
    rep_p4 = analyzed(P_GRIDSHIFT)
    f_p4 = fired(rep_p4)
    bg = [f for f in rep_p4.findings if f.check == "block_grid"][0]
    check("P4 裁剪错位 4px 后重编码 → block_grid 触发", "block_grid" in f_p4,
          f"触发集 = {sorted(f_p4)}")
    check("……且报出的偏移量就是真实的 4 像素（判据用 (7-φ)%8 而非 (φ+1)%8）",
          "偏移 4 像素" in bg.detail, bg.detail[:110])

    print()
    print("=" * 74)
    print("第 4 节 · 参数归属：等级 / 升级建议 / 哈希 / 尺寸一律由代码算")
    print("=" * 74)
    mismatched = []
    for name, data in ALL_SAMPLES.items():
        r = analyzed(data)
        if summarize(r.findings)[0] != r.suspicion:
            mismatched.append(name)
    check("综合等级的复算与报告一致（= 等级不是模型给的，是 summarize() 算的）",
          not mismatched, f"不一致：{mismatched}")
    # ⚠️ 「前提不满足」必须是「无法判定」，**不是「低」**。
    #    这是本项最危险的失败形态：一张 92% 纯色的截图会让 ela / noise_residual /
    #    copy_move 三项**整项没跑**，而它们是唯一能发现内容层拼接/克隆的项目；
    #    此时若把等级判成「低」，用户读到的是"没问题"。
    #    断言成对：纯色图 → 无法判定；干净自然图 → 仍然是低（不能矫枉过正把正常图也说成测不了）。
    check("大片纯色图 → 综合等级「无法判定」（不能因为没看到异常就说「低」）",
          analyzed(N_FLAT_AREA).suspicion == "无法判定",
          f"实得：{analyzed(N_FLAT_AREA).suspicion}")
    check("……且理由说清了「是前提不满足，不是没问题」",
          "不满足像素域检查的前提" in analyzed(N_FLAT_AREA).suspicion_rationale,
          analyzed(N_FLAT_AREA).suspicion_rationale[:60])
    check("干净自然图不受这条规则影响（仍然是「低」）",
          analyzed(N_CLEAN_PNG).suspicion == "低",
          f"实得：{analyzed(N_CLEAN_PNG).suspicion}")

    # ⚠️ 每一档等级都必须给出**完整阶梯**。早先按等级裁剪条数 ⇒
    #    「线下司法鉴定」在最常见的「低」里被悄悄删掉 —— 而它是唯一有法律证明力的路径。
    #    断言 ① 四种等级条数一致 ② 末档一定是线下司法鉴定 ③ 报价区间确实写出来了。
    tiers = {
        lv: [u.tier for u in plan_upgrades(lv)]
        for lv in ("低", "中", "高", "无法判定")
    }
    check("升级建议条数不随等级裁剪（四种等级完全一致）",
          len({tuple(v) for v in tiers.values()}) == 1, str(tiers))
    check("……且末档必是线下司法鉴定（唯一有法律证明力的路径）",
          all(v[-1] == "offline_forensic" for v in tiers.values()),
          str({k: (v[-1] if v else None) for k, v in tiers.items()}))
    check("……且把「要多少钱 / 要等多久」写清楚了（用户要的就是这个）",
          all(any("2500" in u.cost and "工作日" in u.turnaround for u in plan_upgrades(lv))
              for lv in ("低", "中", "高", "无法判定")),
          str([(u.tier, u.cost) for u in plan_upgrades("低")]))
    check("upgrades 全部来自固定阶梯（免费线上 → 付费 → 线下司法鉴定）",
          {u.tier for u in analyzed(P_EXIF).upgrades}
          == {"online_free", "online_paid", "offline_forensic"},
          f"P3 拿到 {len(analyzed(P_EXIF).upgrades)} 条："
          f"{[u.tier for u in analyzed(P_EXIF).upgrades]}")
    hash_wrong = [n for n, d in ALL_SAMPLES.items()
                  if analyzed(d).sha256 != hashlib.sha256(d).hexdigest()]
    check("sha256 等于对原始字节的真实哈希（保全原件的凭据）", not hash_wrong,
          f"不一致：{hash_wrong}")
    check("报告里的 width/height 是**原图**尺寸，不是裁剪后尺寸",
          analyzed(N_BIG_CROPPED).width == 2004 and analyzed(N_BIG_CROPPED).height == 1508,
          f"报告 {analyzed(N_BIG_CROPPED).width}x{analyzed(N_BIG_CROPPED).height}，"
          f"实际裁剪到 {analysis_crop_box((2004, 1508))}")
    check("被裁剪时 crop_note 必须说清楚（否则用户会误以为看的是全图）",
          "裁剪" in analyzed(N_BIG_CROPPED).crop_note)

    # ⚠️ 「给主控的那一句话」是**最容易被原样转述给用户**的出口，它必须与 `summarize()`
    #    的判据同源。这里真的踩过：报告内部判「无法判定」，而这行文案写的是
    #    「N 项可执行检查均未提示异常」—— 前半句说测不了、后半句说没问题。
    #    改判据时只改了报告内部那一处，最危险的这句话漏在了外面。
    #    断言成对：纯色图不得出现那句话；干净图必须仍然正常出。
    s_flat = summary_line(analyzed(N_FLAT_AREA))
    check("前提不满足时，「给主控的一句话」不得出现「均未提示异常」",
          "均未提示异常" not in s_flat and "整项没有执行" in s_flat, s_flat[:92])
    check("……且点名了是哪些检查项没跑（否则主控没法向用户解释）",
          "噪声层一致性" in s_flat and "复制-移动" in s_flat, s_flat[:92])
    s_clean = summary_line(analyzed(N_CLEAN_JPEG))
    check("干净自然图的这句话仍然正常（不能矫枉过正，把正常图也说成测不了）",
          "均未提示异常" in s_clean and "整项没有执行" not in s_clean, s_clean[:92])

    print()
    print("=" * 74)
    print("第 5 节 · 裁剪框不变量（纯函数，直接验它，不去猜裁剪结果）")
    print("=" * 74)
    check("小图不裁剪", analysis_crop_box((720, 540)) is None
          and analysis_crop_box((1600, 1200)) is None)
    bad_align, bad_inside = [], []
    for w, h in [(2004, 1508), (1601, 1601), (4000, 900), (900, 4000), (2400, 1800), (1601, 900)]:
        box = analysis_crop_box((w, h))
        assert box is not None, (w, h)
        x0, y0, x1, y1 = box
        if x0 % 8 or y0 % 8 or (x1 - x0) % 8 or (y1 - y0) % 8:
            bad_align.append((w, h, box))
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            bad_inside.append((w, h, box))
    check("裁剪框起点与边长都是 8 的倍数（否则我们自己会挪走块网格相位）",
          not bad_align, f"越界样例：{bad_align}")
    check("裁剪框不越界（越界会被 Pillow 补黑边 = 亲手往图里塞一道强边）",
          not bad_inside, f"越界样例：{bad_inside}")

    print()
    print("=" * 74)
    print("第 6 节 · 边界输入：畸形 / 超大 / 别名 —— 一律**不得抛异常**出去")
    print("=" * 74)
    try:
        analyzed(b"%PDF-1.7 not an image")
        raised = False
    except ValueError:
        raised = True
    check("非图像字节 → analyze 抛 ValueError（按魔术字节判定，不信任扩展名）", raised)
    rep_dec = analyzed(N_CLEAN_PNG, declared_mime="image/jpeg")
    cont = [f for f in rep_dec.findings if f.check == "container"][0]
    check("声明的 MIME 与真实容器不符 → container 项提示异常",
          cont.verdict == "提示异常", cont.detail[:80])

    # ⚠️ 别名不是"不符"。`image/jpg` 极常见（浏览器与不少客户端就这么写），
    #    不归一化时**每一张这样的正常图**都会被报「容器不一致」—— 纯假阳性。
    alias_bad = []
    for alias, real in (("image/jpg", N_CLEAN_JPEG), ("image/pjpeg", N_CLEAN_JPEG),
                        ("image/x-png", N_CLEAN_PNG)):
        r = analyzed(real, declared_mime=alias)
        if [f for f in r.findings if f.check == "container"][0].verdict != "未提示异常":
            alias_bad.append(alias)
    check("MIME 常见别名（image/jpg 等）按「同一个容器」处理，不算不符",
          not alias_bad, f"被误报：{alias_bad}")
    check("……但真的换过容器（PNG 声明成 JPEG）仍然要报 —— 归一化没有把检查废掉",
          [f for f in analyzed(N_CLEAN_PNG, declared_mime="image/png").findings
           if f.check == "container"][0].verdict == "未提示异常"
          and [f for f in analyzed(N_CLEAN_PNG, declared_mime="image/jpeg").findings
               if f.check == "container"][0].verdict == "提示异常")
    cont_none = [f for f in analyzed(N_CLEAN_PNG).findings if f.check == "container"][0]
    check("调用方未声明类型时不得说「与声明一致」（那是跟一个不存在的声明比对）",
          cont_none.verdict == "无法判定" and "与声明一致" not in cont_none.detail,
          cont_none.detail[:72])

    # ⚠️ 工具边界**唯一的承诺**是"永远返回结构化结果"。下面四种畸形输入都曾经
    #    直接穿透出去（ValueError / OSError），调用方收到的正是一句不透明的异常 ——
    #    而它接下来最可能的动作是"换个路子把图弄进来"，正好绕过刚设的边界。
    from legal_acps.evidence.pipeline import outcome as _outcome

    def _probe(u: str) -> tuple[str, str, str]:
        """调工具入口，**把"抛出来的异常"也折成一个可断言的返回值**。

        ⚠️ 这一段不是防御性编程的洁癖：如果回归真的让 `outcome()` 抛异常，
        裸着调它会让**整个断言脚本带着 traceback 崩掉** ——
        而崩掉的样子（少打几行）与"通过"在日志里几乎分不出来，
        元验证也会因为找不到匹配词而误判成"这条断言是空转的"。
        测试自己必须先站得稳，才有资格去测别人。
        """
        try:
            o = _outcome(u)
            return o.status, o.refusal_reason or "", o.how_to_fix or ""
        except Exception as exc:  # noqa: BLE001 —— 这里就是要抓"漏出去"
            return f"抛出{type(exc).__name__}", str(exc), ""

    malformed = {
        "声明 png 其实是垃圾字节": "data:image/png;base64,AAAA",
        "JPEG 只有文件头（截断）":
            "data:image/jpeg;base64," + _b64.b64encode(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00").decode(),
        "PNG 头正确、内容是垃圾":
            "data:image/png;base64," + _b64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).decode(),
        "120 MP 但只有文件头（无像素数据）":
            "data:image/png;base64," + _b64.b64encode(oversized_png()).decode(),
    }
    leaked: list[str] = []
    for desc, u in malformed.items():
        status, reason, fix = _probe(u)
        if status != "refused" or not reason or not fix:
            leaked.append(f"{desc}（status={status}）")
    check("四种畸形输入全部返回结构化结果，**一个异常都不许漏出去**",
          not leaked, "；".join(leaked) or "四种都如实返回了（均带 refusal_reason + how_to_fix）")
    st_ov, rs_ov, _ = _probe(malformed["120 MP 但只有文件头（无像素数据）"])
    check("……超大图报的是「像素数过大」，且闸门发生在解压之前",
          st_ov == "refused" and "像素数过大" in rs_ov, rs_ov[:76])
    check("像素上限闸门是可断言的纯函数（边界两侧都要对）",
          pixel_budget_exceeded((12000, 10000)) and not pixel_budget_exceeded((4000, 3000)),
          "12000×10000 超限 / 4000×3000 不超限")

    print()
    print("=" * 74)
    print("第 7 节 · 取图策略：本项目**唯一一处主动向外部地址发起请求**的环节")
    print("        （风险在出站方向 SSRF，不是「被外部连进来」—— 没有新增无 mTLS 的入站端口）")
    print("=" * 74)
    from legal_acps.evidence import fetch as fetch_mod
    from legal_acps.evidence.contracts import ImageEvidenceOutcome  # noqa: F401
    from legal_acps.evidence.fetch import FetchRefused, classify_uri
    from legal_acps.evidence.pipeline import outcome

    kinds = {
        "data:image/png;base64,AAAA": "data",
        "inbox:" + "a1" * 32: "inbox",
        "https://img.example.com/a.jpg": "https",
        "http://img.example.com/a.jpg": "http",
        "file:///etc/passwd": "file",
        "/tmp/a.jpg": "other",
        "img/a.jpg": "other",
    }
    wrong = {u: classify_uri(u) for u, want in kinds.items() if classify_uri(u)[0] != want}
    check("URI 分类正确（data / inbox / https / http / file / 其它）", not wrong, f"不符：{wrong}")
    check("主机名解析去掉端口与 userinfo",
          classify_uri("https://user@Img.Example.com:8443/a.jpg")[1] == "img.example.com",
          str(classify_uri("https://user@Img.Example.com:8443/a.jpg")))

    import os as _os
    from legal_acps.evidence.fetch import allowed_hosts, check_policy

    # ⚠️ 白名单这个开关仍要从环境里摘掉再测，否则这台机器上的环境变量会把断言
    #    变成**环境依赖**：测试结果会跟着"谁在这台机器上设过什么"变。
    saved = _os.environ.pop("EVIDENCE_IMAGE_ALLOWED_HOSTS", None)
    # ⚠️ 本地文件开关已经是**进程内开关**（见 fetch 模块 docstring）：
    #    环境变量会被 systemd / shell rc / 父进程一路继承，
    #    "服务模式不该读本地文件"这条纪律就守不住了。这里前后都显式复位。
    saved_local = fetch_mod.local_file_enabled()
    fetch_mod.enable_local_file(False)
    try:
        # ① 未配白名单 → **必须拒绝**（fail-closed）。这一点与委派护栏的 fail-open 相反，
        #    判据不是"统一风格"，而是**放行与拦截各自的最坏后果**：这里放行一次的代价
        #    是 SSRF / 内网探测，而拦截一次的代价只是"用户被要求先配置"。
        #
        #    ⚠️ 这条断言刻意走 `check_policy()`（**纯策略判定、不联网**），不走 `outcome()`。
        #    理由不是洁癖：`outcome()` 会把请求真的发出去 —— 一旦 fail-closed 被改坏，
        #    这条断言自己就变成了一次真实的对外请求，还会被 DNS 和 10 秒超时牵着走，
        #    失败的样子也从"断言红了"变成"跑得特别慢"。
        #    通则：**测护栏的那行代码，自己不要先触发它要拦的那个危险动作。**
        check("未配置白名单时，白名单为空集（是「禁止一切」而不是「允许一切」）",
              allowed_hosts() == frozenset(), str(sorted(allowed_hosts())))
        try:
            check_policy("https://img.example.com/a.jpg")
            refused_no_allow = False
        except FetchRefused:
            refused_no_allow = True
        check("未配置白名单时，出网取图被拒绝（fail-closed，不是默认放行）",
              refused_no_allow,
              f"check_policy 的实际行为：{'放行（缺陷）' if not refused_no_allow else '拒绝'}")

        # 拒绝必须是**正常返回值**而不是抛异常：拒绝也要能交给模型说人话
        # （refusal_reason 原文转述 + how_to_fix 补救建议）。
        # 这里刻意用 `file://`（**策略层拒绝，且在任何状态下都不会联网**）来验这条性质 ——
        # 如果拿 https 地址验，一旦护栏被改坏，这行就会真的发出一次对外请求。
        o_no_allow = outcome("file:///etc/passwd")
        check("……且拒绝以结构化结果返回（status=refused，带 refusal_reason 与 how_to_fix）",
              o_no_allow.status == "refused"
              and bool(o_no_allow.refusal_reason) and bool(o_no_allow.how_to_fix),
              f"status={o_no_allow.status} reason={(o_no_allow.refusal_reason or '')[:50]}")

        _os.environ["EVIDENCE_IMAGE_ALLOWED_HOSTS"] = "img.example.com"
        check("白名单内主机放行", check_policy("https://img.example.com/a.jpg")[1] == "img.example.com")
        try:
            check_policy("https://evil.example.net/a.jpg")
            blocked = False
        except FetchRefused:
            blocked = True
        check("白名单外主机被拒绝", blocked)
        try:
            check_policy("http://img.example.com/a.jpg")
            plain_ok = True
        except FetchRefused:
            plain_ok = False
        check("即主机在白名单内，明文 http 仍被拒绝", not plain_ok)
        try:
            check_policy("file:///etc/passwd")
            file_ok = True
        except FetchRefused:
            file_ok = False
        check("默认拒绝 file://（等于把本机文件系统暴露给调用方）", not file_ok)

        # ⚠️ 显式开启本地文件时，来源必须标注成 `local_file`。初版图省事复用了
        #    `network` —— 溯源字段是报告里回答"这份字节从哪来"的地方，
        #    写成"网络"而实际来自本机磁盘，是一条**看起来无害但会误导复核**的假信息。
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "ev.jpg"
            fp.write_bytes(N_CLEAN_JPEG)
            fetch_mod.enable_local_file(True)
            try:
                ol = outcome("file://" + fp.as_posix(), context_note="据称是案发现场照片")
            finally:
                fetch_mod.enable_local_file(False)
        check("入口显式开启本地文件后，来源如实标注为 local_file（不是 network）",
              ol.status == "ok" and ol.result is not None
              and ol.result.fetched_from == "local_file",
              f"status={ol.status}、fetched_from="
              f"{ol.result.fetched_from if ol.result else None}")
        check("context_note 原样回显（收下了就必须往下传，不能悄悄丢掉）",
              ol.result is not None and ol.result.context_note == "据称是案发现场照片",
              f"context_note={ol.result.context_note if ol.result else None}")

        # ── "服务模式不读本地文件"必须是**结构上做不到**，不是"记得别那么做" ──
        # 判据落在纯函数上：规则写在一处，且能被断言钉住。
        from legal_acps.evidence.fetch import (
            local_file_enabled,
            resolve_local_file_flag,
        )
        check("服务模式下即使调用方要求，本地读取也不开启（结构上不可达）",
              resolve_local_file_flag(requested=True, serving=True) is False)
        check("非服务模式下，请求才会被照办（否则这条规则等于把功能全关了）",
              resolve_local_file_flag(requested=True, serving=False) is True
              and resolve_local_file_flag(requested=False, serving=False) is False)
        check("开关默认是关的 —— 上一条用例复位之后必须回到 False",
              local_file_enabled() is False, f"实际={local_file_enabled()}")

        # ── 收件箱：随任务附件进来的字节，句柄是**内容哈希而不是路径** ──
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "evidence-inbox"
            token = inbox.bind_root(root)
            try:
                item = inbox.stage(N_CLEAN_JPEG, root=root, mime_type="image/jpeg", name="现场.jpg")
                check("收件箱句柄 = inbox: + 内容 sha256",
                      item.handle == "inbox:" + hashlib.sha256(N_CLEAN_JPEG).hexdigest(),
                      item.handle)
                check("收件箱落盘幂等（同一份字节写两次只留一份）",
                      inbox.stage(N_CLEAN_JPEG, root=root).handle == item.handle
                      and len(list(root.glob("*.bin"))) == 1,
                      f"{len(list(root.glob('*.bin')))} 个 .bin")

                oi = outcome(item.handle)
                check("inbox: 句柄 → status=ok，且来源如实标注为 inbox",
                      oi.status == "ok" and oi.result is not None
                      and oi.result.fetched_from == "inbox",
                      f"status={oi.status} fetched_from="
                      f"{oi.result.fetched_from if oi.result else None}")
                check("……且 sha256 与收件箱里那份字节一致（句柄只是换了字节来源，检测是同一条路）",
                      oi.result is not None
                      and oi.result.report.sha256 == hashlib.sha256(N_CLEAN_JPEG).hexdigest(),
                      oi.result.report.sha256[:16] if oi.result else "")

                # ⚠️ 失败路径：伪造的句柄必须拒绝，而不是"退化成路径解析"。
                #    这是整个模块的安全依据 —— 一旦允许把句柄当路径解释，
                #    "引用内容而不是引用位置"就全作废了。
                #
                #    `inbox:../secret` 是这组里**唯一一条真能穿透**的用例：
                #    它解析出的路径是 `root/../secret.bin`，也就是收件箱的**上一级**，
                #    而那里确实放着一个文件。所以它证明的是"64 位哈希的格式校验
                #    真的在拦人"，而不是"恰好找不到文件"。别的用例（穿越到
                #    /etc/passwd、编一个未登记的哈希）在两种写法下都会被拒，
                #    只能证明"没读到"，证明不了"为什么没读到"。
                secret = Path(td) / "secret.bin"
                secret.write_bytes(N_CLEAN_JPEG)
                # ⚠️ 两组**必须分开**（2026-10-04 补）：格式违规 vs 格式合法但没登记。
                #    混在一张表里时，"入口校验点还在不在"是测不出来的 —— 见下面 ⓪。
                forged = {  # 格式违规：`parse_handle` 这一步就该拒
                    "相对路径够上一级（真能穿透的那条）": "inbox:../secret",
                    "路径穿越": "inbox:../../../etc/passwd",
                    "短哈希": "inbox:abc",
                    "带分隔符": "inbox:aa/bb" + "0" * 60,
                    "裸路径": "/etc/passwd",
                    "file 方案": "file:///etc/passwd",
                }
                unregistered = {  # 格式合法、只是收件箱里没这份：格式校验**不该**管它
                    "未登记的 64 位哈希": "inbox:" + "0" * 64,
                }
                all_bad = {**forged, **unregistered}

                # ⓪ **入口校验点必须有自己的正面判据**（2026-10-04 补，实测踩到）。
                #    原先这里只有"伪造句柄不得被放行"这一条**端到端**断言。加了
                #    **归属闸门**之后，`inbox:../secret` 会因为"**没有归属记录**"被拒
                #    —— 症状消失了，而"`parse_handle` 的格式校验是不是还在"再也看不出来。
                #    实测：把格式校验整个退回成 `if False:`，本脚本**整节全绿**、
                #    一条 `[未达预期]` 都不出（`parse_handle` 照样返回 `'../secret'`，
                #    只是后面那道闸门先拦住了）。
                #    ⇒ **一条端到端断言会被后加的第二道防线掩盖**：判据不是变红，
                #      而是**变空** —— 还在、还在跑，但它测的东西已经没了。
                #      所以入口校验点必须**直断**，并且**合法句柄必须被接受**
                #      （少了那一半，一个"永远返回 False"的实现也能过）。
                missed = [u for u in forged.values() if inbox.is_handle(u)]
                check("`is_handle` 只认 64 位十六进制：合法的接、伪造的拒（入口校验点直断）",
                      inbox.is_handle(item.handle) and not missed,
                      f"合法句柄被判 False = {not inbox.is_handle(item.handle)}；"
                      f"被误收 = {missed}")

                leaked = {k: v for k, v in all_bad.items() if outcome(v).status == "ok"}
                check("伪造 / 穿越 / 未登记的句柄一律拒绝（不得退化成路径解析）",
                      not leaked, f"被放行：{leaked}")
                check("……且拒绝仍是结构化结果（不是抛异常），并带上补救建议",
                      all(outcome(u).status == "refused"
                          and bool(outcome(u).refusal_reason)
                          and bool(outcome(u).how_to_fix) for u in all_bad.values()), "")

                # 收件箱也守字节上限：一条远超上限的附件不该把磁盘写满。
                # ⚠️ 与取图入口**同一条规矩**：超限就拒绝，**不做截断** ——
                #    截断过的图会让所有像素域统计失真，而失真的结论和真结论长得一模一样。
                try:
                    inbox.stage(N_CLEAN_JPEG + b"\x00" * inbox.MAX_STAGE_BYTES, root=root)
                    too_big_ok = True
                except inbox.InboxRefused:
                    too_big_ok = False
                check("超过上限的附件被拒（**不截断**）", not too_big_ok)

                # 收件箱里塞一个符号链接指向外部文件：即便名字对得上也不能读。
                # `is_file()` 对指向文件的符号链接同样返回 True，所以必须单独判。
                link = root / f"{hashlib.sha256(N_CLEAN_JPEG).hexdigest()}.bin"
                link.unlink(missing_ok=True)
                try:
                    link.symlink_to(secret)
                except (OSError, NotImplementedError):
                    sym_ok = False  # 平台不支持建符号链接（如未开权的 Windows）→ 无从测起
                else:
                    sym_ok = outcome(item.handle).status == "ok"
                    link.unlink(missing_ok=True)
                check("收件箱里的符号链接不被当作有效字节读走", not sym_ok,
                      f"symlink 被放行={sym_ok}")
            finally:
                inbox.unbind_root(token)

            # 未绑定收件箱时（其它进程 / 其它任务）句柄不可用 —— 证据字节不该跨任务泄漏
            unbound = outcome(item.handle)
            check("未绑定收件箱时句柄不可用（字节不跨任务泄漏）",
                  unbound.status == "refused" and "收件箱" in (unbound.refusal_reason or ""),
                  (unbound.refusal_reason or "")[:60])
    finally:
        _os.environ.pop("EVIDENCE_IMAGE_ALLOWED_HOSTS", None)
        # 旧名字顺手清掉：万一这台机器上还留着上一版设过的环境变量，
        # 保证它从此不再影响任何行为（本地读取已改为进程内开关）。
        _os.environ.pop("EVIDENCE_IMAGE_ALLOW_FILE", None)
        fetch_mod.enable_local_file(saved_local)
        if saved is not None:
            _os.environ["EVIDENCE_IMAGE_ALLOWED_HOSTS"] = saved

    o = outcome("data:image/png,abc")           # 非 base64
    check("data: URI 未用 base64 → 拒绝，且**仍然返回结构化结果**（不是抛异常）",
          o.status == "refused" and bool(o.refusal_reason) and bool(o.how_to_fix),
          f"status={o.status} fix={o.how_to_fix}")
    o2 = outcome("data:application/pdf;base64,AAAA")
    check("data: URI 声明为非图片 → 拒绝", o2.status == "refused")
    o3 = outcome("data:text/plain;base64,AAAA")
    check("data: URI 声明为 text/plain → 拒绝", o3.status == "refused")

    small_uri = "data:image/png;base64," + _b64.b64encode(N_CLEAN_PNG).decode()
    o4 = outcome(small_uri)
    check("自带字节的 data: URI → status=ok，且摘要由程序生成",
          o4.status == "ok" and bool(o4.result and o4.result.summary_for_leader),
          (o4.result.summary_for_leader[:78] if o4.result else "无结果"))
    check("……且 fetched_from 如实标注为 inline_base64",
          bool(o4.result and o4.result.fetched_from == "inline_base64"))

    real_max = fetch_mod.MAX_FETCH_BYTES
    try:
        fetch_mod.MAX_FETCH_BYTES = 16
        o5 = outcome("data:image/png;base64," + _b64.b64encode(N_CLEAN_PNG).decode())
        check("超过字节上限 → 拒绝（**不截断**：截断会让所有统计失真）",
              o5.status == "refused" and "超过" in (o5.refusal_reason or ""),
              (o5.refusal_reason or "")[:70])
        # ⚠️ 两条文案对应两条不同的代码路径：`载荷过大` 是**解码前**的预检，
        #    `图片 N 字节` 是解码后的复检。断言前者，才证明"判超限"没有先吃内存。
        check("……且超限判定发生在**解码之前**（否则判它超限本身就要先解进内存）",
              "载荷过大" in (o5.refusal_reason or ""),
              (o5.refusal_reason or "")[:70])
    finally:
        fetch_mod.MAX_FETCH_BYTES = real_max

    print()
    print("=" * 74)
    print("第 8 节 · Agent 接线：新角色不得把风险引到已过审链路")
    print("=" * 74)
    from legal_acps.acps_config import PORTS
    from legal_acps.agents import BUILDERS, build_evidence_image

    check("BUILDERS 已含 evidence-image", "evidence-image" in BUILDERS,
          f"现有角色：{sorted(BUILDERS)}")
    check("端口已分配且全局唯一",
          PORTS.get("evidence-image") == 8449
          and len(set(PORTS.values())) == len(PORTS),
          f"evidence-image -> {PORTS.get('evidence-image')}")
    agent = build_evidence_image(model=None)
    # ⚠️ `agent.capabilities` 里躺的是**被装饰后的函数**，不是 Capability 对象 ——
    #    id 挂在 `fn.capability` 上（实战踩过：直接 `c.id` 会 AttributeError 且被
    #    管道里的 tail 吞掉，整节"看起来没跑"）。这里刻意读 `to_acps_skill()`，
    #    因为那才是**真正会进注册指纹**的形态，比读装饰器局部变量更有说服力。
    skills = [fn.capability.to_acps_skill() for fn in (agent.capabilities or [])]
    ids = [s.get("id") for s in skills]
    check("对外技能恰好是 evidence.inspect_image（按注册指纹那一层取）",
          ids == ["evidence.inspect_image"], str(ids))
    hook_ids = [getattr(h, "hook_id", None) or type(h).__name__ for h in (agent.hooks or [])]
    check("挂了终止强制（叶子角色也必须保证交付物交回）",
          any("Termination" in str(h) for h in hook_ids), str(hook_ids))
    check("**没有**挂委派 Hook —— 它根本没有 acps_delegate 这个工具，挂了就是死对象",
          not any("Delegation" in str(h) for h in hook_ids), str(hook_ids))
    term = [h for h in (agent.hooks or []) if type(h).__name__ == "LeaderTerminationHooks"]
    check("终止强制的交付物工具名指向本角色自己的工具",
          bool(term) and getattr(term[0], "conclusion_tool", "") == "evidence_inspect_image",
          f"conclusion_tool = {getattr(term[0], 'conclusion_tool', None) if term else None}")
    check("……且明确禁止走第 5 层程序兜底（那层只会拼 FinalVerdict 形状）",
          bool(term) and getattr(term[0], "fallback_on_failure", True) is False)

    # ── 收件箱 Hook：既要在，又要挂对位置 ─────────────────────────────────
    # ⚠️ 这里刻意用**关系断言**而不是"挂了没有"：`EvidenceInboxHooks` 里有一段
    #    「接管 acps_delegate、把字节随请求送下去」的逻辑，挂在一个**没有这个工具**
    #    的角色上就是死代码。
    #    纪律是：**先问"谁来调它"，再问"调了管不管用"** ——
    #    `DebateChainGuardHooks` 曾挂在没有 acps_delegate 的辩手身上，真机空转一整轮。
    box = [h for h in (agent.hooks or []) if type(h).__name__ == "EvidenceInboxHooks"]
    check("evidence-image 挂了收件箱 Hook（否则随任务附件来的图它根本收不到）",
          len(box) == 1, str(hook_ids))
    check("……且关掉「接管委派」那一段：本角色没有 acps_delegate，那段逻辑在这里是死代码",
          bool(box) and box[0].delegate_tool == "",
          f"delegate_tool={box[0].delegate_tool!r}" if box else "")

    from legal_acps.agents import build_leader

    leader = build_leader(model=None)
    lb = [h for h in (leader.hooks or []) if type(h).__name__ == "EvidenceInboxHooks"]
    check("主控挂了收件箱 Hook（用户上传的图先落到它这里）", len(lb) == 1)
    check("……且**开启**了接管委派：主控有 acps_delegate，字节只能由代码送下去",
          bool(lb) and lb[0].delegate_tool == "acps_delegate",
          f"delegate_tool={lb[0].delegate_tool!r}" if lb else "")

    # ── "服务模式不读本地文件"必须是结构性的，不是"记得别那么做" ──────────────
    # 判据是**谁调用了 `enable_local_file`**：它一旦出现在会被服务加载的模块里，
    # 常驻进程就多出一个"任意文件读取"原语 —— 而这件事**没有任何运行期迹象**。
    callers: dict[str, list[int]] = {}
    for src in sorted((ROOT / "legal_acps").rglob("*.py")):
        rel = src.relative_to(ROOT).as_posix()
        if rel == "legal_acps/evidence/fetch.py":  # 定义处，允许
            continue
        try:
            lines = src.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for no, line in enumerate(lines, 1):
            if line.strip().startswith("#"):
                continue
            if "enable_local_file(" in line:
                callers.setdefault(rel, []).append(no)
    check("`enable_local_file` 不出现在会被服务加载的模块里（服务模式结构上读不到本地文件）",
          not callers, f"意外调用点：{callers}")

    # 惰性导入隔离：把 Pillow / numpy 挡在导入系统之外，agents 仍必须能加载。
    # 这不是洁癖 —— 服务器上没装图像库时，模块级导入会让 6 个已过审 Agent 一起起不来。
    import subprocess as _sp

    probe = (
        "import sys\n"
        "class B:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('PIL', 'numpy'):\n"
        "            raise ImportError('blocked ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, B())\n"
        "import legal_acps.agents as A\n"
        "from legal_acps.evidence.contracts import ImageEvidenceRequest\n"
        # ⚠️ 这里原来是 `assert len(A.BUILDERS) == 7` —— 一个**写死的角色数**。
        #    加第 8 个角色（evidence-leader）时它立刻过期，而失败文案只说
        #    "缺 Pillow/numpy 时加载失败"，指向完全无关的方向。
        #    改成**现场派生**：判据是"部署清单与构建器一一对应"，
        #    加角色时两边同时长，断言不会变成"过期事实"。
        #    （同一个坑本项目已踩过：`verify.sh` 曾把角色表写死 5 个，
        #      第 6 个跑完像不存在 —— 错误事实被"通过"盖章，比没有看板更糟。）
        "from legal_acps.acps_config import PORTS\n"
        "mismatch = sorted(set(PORTS) ^ set(A.BUILDERS))\n"
        "assert not mismatch, mismatch\n"
        "assert not [m for m in sys.modules if m.split('.')[0] in ('PIL', 'numpy')]\n"
        "print('ISOLATED-OK')\n"
    )
    r = _sp.run([sys.executable, "-c", probe], cwd=str(ROOT), capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                env={**_os.environ, "PYTHONPATH": "."})
    check("缺 Pillow/numpy 时 agents.py 仍可加载（重依赖只在使用时才导入）",
          "ISOLATED-OK" in (r.stdout or ""),
          (r.stdout or r.stderr or "").strip().splitlines()[-1:] or [""])

    print()
    print("=" * 74)
    print("第 9 节 · 附件通道：附件进来 → 句柄出去 → 委派由**代码**带字节")
    print("        （模型工具层只认 uri，字节只能由代码塞 —— 这一节就是在验那句“只能”）")
    print("=" * 74)
    import asyncio as _asyncio
    import tempfile as _tf
    from types import SimpleNamespace as _NS

    from legal_acps.evidence_hooks import EvidenceInboxHooks, plan_delegation, strip_handles
    from legal_acps.stub_model import StubModel

    # ── ① 真跑一次 run ────────────────────────────────────────────────────
    # 刻意用**真实的 WitRuntime**而不是自己拼一个 ctx：这条链的全部价值就在于
    # "wit 真的会把入站附件放进 state.messages"，绕过框架就正好绕过了要验的那一环。
    from wit import ModelResponse, WitAgent, WitRuntime
    from wit.kernel.content import ImagePart, TextPart

    class _SpyModel(StubModel):
        """只做两件事：记下收到的 system 提示、直接收尾（不调任何工具）。"""

        def __init__(self) -> None:
            super().__init__()
            self.systems: list[str] = []

        async def complete(self, request):  # type: ignore[no-untyped-def]
            for message in request.messages:
                if message.role == "system":
                    self.systems.append(
                        "\n".join(str(getattr(p, "text", "")) for p in message.content)
                    )
            return ModelResponse.final("（探针）收到。")

    with _tf.TemporaryDirectory() as td1:
        spy = _SpyModel()
        rt = WitRuntime(
            WitAgent(
                id="inbox-probe",
                instructions="探针",
                model=spy,
                hooks=[EvidenceInboxHooks(delegate_tool="")],
            ),
            workspace=td1,
        )
        run = _asyncio.run(
            rt.run([TextPart(text="请看这张图"), ImagePart(mime_type="image/jpeg", data=N_CLEAN_JPEG)])
        )
        expect_handle = inbox.handle_for(N_CLEAN_JPEG)
        staged = list(inbox.inbox_root(td1).glob("*.bin"))
        check("入站附件（ImagePart 内联字节）被收进收件箱",
              len(staged) == 1 and staged[0].stat().st_size == len(N_CLEAN_JPEG),
              f"{[p.name for p in staged]}")
        check("……且句柄被注入到 system 提示里（模型只需照抄，不必经手字节）",
              any(expect_handle in s for s in spy.systems), f"句柄={expect_handle[:24]}…")
        check("……且提示里写明了“句柄是哈希不是路径”（避免模型把它当文件路径改写）",
              any("不是文件路径" in s for s in spy.systems), "")
        check("run 正常收尾（收件箱逻辑不许影响主流程）", run.status == "completed", run.status)

    # ── ② 纯文本任务必须**一个字都不注入** ────────────────────────────────
    with _tf.TemporaryDirectory() as td2:
        spy2 = _SpyModel()
        rt2 = WitRuntime(
            WitAgent(
                id="inbox-probe-plain",
                instructions="探针",
                model=spy2,
                hooks=[EvidenceInboxHooks(delegate_tool="")],
            ),
            workspace=td2,
        )
        _asyncio.run(rt2.run("这是一段纯文本，没有任何附件。"))
        check("纯文本任务：一条附件提示都不注入（对已过审的纯文本链路零影响）",
              not any("收件箱" in s or "inbox:" in s for s in spy2.systems),
              f"{len(spy2.systems)} 条 system 全干净")

    # ── ③ 委派接管：模型写句柄，代码补字节 ────────────────────────────────
    with _tf.TemporaryDirectory() as td3:
        root3 = inbox.inbox_root(td3)
        item = inbox.stage(N_CLEAN_JPEG, root=root3, mime_type="image/jpeg", name="现场.jpg")
        token3 = inbox.bind_root(root3)
        try:
            class _FakeRuntime:
                def __init__(self) -> None:
                    self.calls: list[dict] = []

                async def aip_delegate(self, state, *, partner_aic, request):  # type: ignore[no-untyped-def]
                    self.calls.append({"partner_aic": partner_aic, "request": list(request)})
                    return {"status": "accepted"}

            class _FakeCall:
                name = "acps_delegate"

                def __init__(self, arguments: dict) -> None:
                    self.arguments = arguments

            handled: list[str] = []

            async def _handler(tool_call):  # type: ignore[no-untyped-def]
                handled.append("handler")
                return {"via": "handler"}

            hook = EvidenceInboxHooks()
            ctx = _NS(
                runtime=_FakeRuntime(),
                state=_NS(messages=[]),
                private_state={"legal.evidence-inbox.items": [item]},
            )

            # (a) 请求里写了句柄 → 必须由代码接管、把字节附上
            _asyncio.run(
                hook.wrap_tool_call(
                    ctx,
                    _FakeCall({"partner_aic": "aic:example", "request": f"请检查这张图 {item.handle}"}),
                    _handler,
                )
            )
            calls = ctx.runtime.calls
            sent = calls[0]["request"] if calls else []
            images = [p for p in sent if type(p).__name__ == "ImagePart"]
            check("请求里带句柄 → 由**代码**把真实字节附上（模型工具层给不了字节，这是唯一通路）",
                  len(images) == 1 and images[0].data == N_CLEAN_JPEG,
                  f"{len(images)} 个 ImagePart，共 {sum(len(getattr(p, 'data', b'') or b'') for p in images)} 字节")
            check("……接管后不再走原 handler（否则会变成两次委派）",
                  not handled, f"handler 被调用 {len(handled)} 次")
            check("……文字部分保留，只把句柄换成人话（不吞掉请求内容，也不把内部哈希发给下游）",
                  "请检查这张图" in "".join(str(getattr(p, "text", "")) for p in sent)
                  and item.handle not in "".join(str(getattr(p, "text", "")) for p in sent), "")

            # ── (a2) 句柄的**大小写形态**：识别侧与抹除侧必须给出同一个答案 ────
            # ⚠️ 这是 2026-10-02 实测缺陷的回归防线。旧实现里
            #    `find_handles` 用正则（**前缀大小写敏感**）、`strip_handles` 用朴素
            #    replace（只试整串小写 / 整串大写），两侧的"什么算一个句柄"各自漂 ⇒
            #    模型只要把前缀写成大写，`plan_delegation` 就落空 ⇒ 走原生委派、
            #    只送出文本 ⇒ **字节静默丢失、下游回"没有图"、整条链白跑且不报错**。
            #
            # 判据是**关系断言**而不是"某个具体形态对不对"：凡 `find_handles` 认出的，
            # `strip_handles` 必须也抹掉。两侧共用 `inbox.HANDLE_IN_TEXT_RE` 后才成立。
            #
            # ⚠️ 刻意要求 **两个都成立**（而不是"两侧一致即可"）：
            #    两侧共用一个**过窄**的定义时它们照样"一致"，而后果是**静默丢字节** ——
            #    一致性只能防"各自漂移"，防不了"一起变窄"，所以这里必须正面要求"认得出"。
            _prefix, _digest = item.handle.split(":", 1)
            _variants = {
                "原样": f"{_prefix}:{_digest}",
                "摘要大写": f"{_prefix}:{_digest.upper()}",
                "前缀大写": f"{_prefix.upper()}:{_digest.upper()}",
                "前缀首字母大写": f"{_prefix.capitalize()}:{_digest}",
                "交替大小写": "".join(c.upper() if i % 2 else c for i, c in enumerate(item.handle)),
            }
            _bad: list[str] = []
            for _label, _token in _variants.items():
                _probe = f"请检查这张图 {_token}"
                _seen = bool(inbox.find_handles(_probe))
                _gone = _token not in strip_handles(_probe, [item.handle])
                # 认不出 ⇒ 接管落空、字节静默丢失；抹不掉 ⇒ 内部哈希泄漏给下游。都不许。
                if not (_seen and _gone):
                    _bad.append(f"{_label}(识别={_seen},抹掉={_gone})")
            check("句柄的大小写形态：`find_handles` 必须认出 且 `strip_handles` 必须抹掉",
                  not _bad, "、".join(_bad) or f"{len(_variants)} 种形态全部认得出、抹得掉")

            # (b) 没写句柄 → 必须原样放行，不许自作主张塞图
            _asyncio.run(
                hook.wrap_tool_call(
                    ctx, _FakeCall({"partner_aic": "aic:example", "request": "看这段文字"}), _handler
                )
            )
            check("请求里没句柄 → 原样放行（不自动附带：图文无关的 Partner 不该收到几 MB 字节）",
                  handled == ["handler"] and len(ctx.runtime.calls) == 1,
                  f"handler={len(handled)} 次，接管仍为 {len(ctx.runtime.calls)} 次")

            # (c) 纯函数侧：句柄不在本次收件箱里 → 不成立
            miss = plan_delegation(
                {"partner_aic": "aic:example", "request": "看图 inbox:" + "0" * 64}, items=[item]
            )
            check("句柄不在本次收件箱里 → 计划不成立（模型编一个哈希也拿不到任何字节）",
                  miss is None)
            check("request 写成 parts 形式也能识别（模型不一定给字符串）",
                  plan_delegation(
                      {
                          "partner_aic": "aic:example",
                          "request": [{"type": "text", "text": f"看图 {item.handle}"}],
                      },
                      items=[item],
                  ) is not None)
            check("partner_aic 缺失 → 不接管（宁可放行，也不要造一次没有目标的委派）",
                  plan_delegation({"request": f"看图 {item.handle}"}, items=[item]) is None)
        finally:
            inbox.unbind_root(token3)

    # ── ④ 入站附件的**第二种形态**（uri）不许被静默丢弃 ────────────────────
    # AIP 层允许调用方在 bytes / uri 之间二选一（见第 10 节的实测）。所以"只认 data"
    # 等于把另一种形态**无声丢掉** —— 而"没收到图"与"收到了但没人理"在结论里
    # 长得一模一样，这正是本项目最忌讳的那类缺陷。
    with _tf.TemporaryDirectory() as td4:
        spy4 = _SpyModel()
        rt4 = WitRuntime(
            WitAgent(
                id="inbox-probe-uri",
                instructions="探针",
                model=spy4,
                hooks=[EvidenceInboxHooks(delegate_tool="")],
            ),
            workspace=td4,
        )
        _asyncio.run(
            rt4.run(
                [
                    TextPart(text="看这张"),
                    ImagePart(mime_type="image/png", uri="https://cdn.example.com/a.png"),
                ]
            )
        )
        joined4 = "\n".join(spy4.systems)
        check("入站附件的 uri 形态：地址被如实转述给模型（不再被静默丢弃）",
              "https://cdn.example.com/a.png" in joined4, "提示里出现了该地址")
        check("……且写明「没有字节 / 系统不会自动去取」（一张图里塞个 URL ≠ 一次自动出网）",
              "没有字节" in joined4 and "不会自动去取" in joined4, "")
        check("……且要求取不到时如实写「未能取得」，不许凭地址推断图片内容",
              "未能取得" in joined4 and "推断图片内容" in joined4, "")
        check("……uri 形态不进收件箱（没有字节可落盘）",
              list(inbox.inbox_root(td4).glob("*.bin")) == [], "")

    both = [
        ImagePart(mime_type="image/jpeg", data=b"XYZ"),
        ImagePart(mime_type="image/png", uri="https://cdn.example.com/b.png"),
    ]
    check("两种形态各自归位（一份带字节、一份只有地址 ⇒ 各归各的清单）",
          len(inbox.image_attachments(both)) == 1 and len(inbox.image_references(both)) == 1,
          f"attachments={len(inbox.image_attachments(both))} references={len(inbox.image_references(both))}")

    # ⚠️ 上面那条**测不出**「有字节就跳过」这条规则：wit 的 `ImagePart` 在
    #    `__post_init__` 里就强制「uri 与 data 恰有其一」（实测报错原文
    #    `ImagePart requires exactly one of uri or data`），于是带 data 的 part
    #    天然没有 uri —— 删掉跳过照样只归一份，断言在两种写法下都通过。
    #    但本模块是**鸭子类型**的（刻意不 import wit，只看 `.type/.data/.uri`），
    #    它的契约允许"两样都有"的输入 ⇒ 跳过是**对不受 wit 校验约束的输入**的防御。
    #    要验证它，就必须喂一个绕开 wit 校验的 duck-typed part。
    #    （元验证第 ⑲ 条正是这样发现上面那条断言是空转的。）
    class _DuckBoth:  # 不走 wit 校验：字节与地址同时给
        type = "image"
        mime_type = "image/jpeg"
        data = b"XYZ"
        uri = "https://cdn.example.com/c.jpg"
        name = None

    check("两种形态互斥：字节与地址同时出现时只归内联字节那一份（同一张图不许被报两次）",
          len(inbox.image_attachments([_DuckBoth()])) == 1
          and inbox.image_references([_DuckBoth()]) == [],
          f"attachments={len(inbox.image_attachments([_DuckBoth()]))} "
          f"references={len(inbox.image_references([_DuckBoth()]))}")

    # ── ⑤ 回到“纯文本零注入”：加了 uri 分支之后必须仍然成立 ────────────────
    with _tf.TemporaryDirectory() as td5:
        spy5 = _SpyModel()
        rt5 = WitRuntime(
            WitAgent(
                id="inbox-probe-plain2",
                instructions="探针",
                model=spy5,
                hooks=[EvidenceInboxHooks(delegate_tool="")],
            ),
            workspace=td5,
        )
        _asyncio.run(rt5.run("又是一段纯文本，仍然没有任何附件。"))
        check("新增 uri 分支后，纯文本任务依旧零注入（对已过审链路仍是零影响）",
              not any("收件箱" in s or "inbox:" in s or "仅有地址" in s for s in spy5.systems),
              f"{len(spy5.systems)} 条 system 全干净")

    # ── ⑥ 真链路里的接管顺序：接管**不许**把边界闸门短路掉 ────────────────────
    #
    # ③ 是**直接调** `hook.wrap_tool_call(...)` —— 它绕过了框架的 Hook 链。
    # 而 `priority` 只在真链路里才有意义，"接管会不会把别的 Hook 短路掉"**正是被
    # 绕掉的那一环**（与本模块 docstring 里那句"绕过框架就正好绕过了要验的那一环"同源）。
    #
    # 要同时成立两件事：
    #   ① 委派请求真的带上原始字节（路二端到端成立，而不只是"纯函数算对了"）；
    #   ② 一个 `priority = 50` 的外层 Hook 仍然**包住**了这次调用 ——
    #      真链路上 50 就是 `DebateChainGuardHooks`（目标白名单 / 链深 / 环检测）。
    #      它一旦被短路，discovery 捞到的外部 Agent 就能直接拿到材料字节，
    #      而且**不报任何错**：断言全绿、链路照跑、边界没了。
    with _tf.TemporaryDirectory() as td6:
        from legal_acps.debate import DebateChainGuardHooks
        from wit import ToolCall as _ToolCall
        from wit import capability as _capability
        from wit.kernel.hooks import WitHooks as _WitHooks

        @_capability(id="acps_delegate", description="探针：占住这个工具名")
        def _probe_delegate(partner_aic: str, request: str) -> dict:
            """真的被调用 ⇒ 说明接管没生效（本该由 EvidenceInboxHooks 截住）。"""
            return {"probe": "原始 handler 被调用", "partner_aic": partner_aic}

        HANDLER_HITS: list[str] = []

        @_capability(id="acps_delegate", description="探针：占住这个工具名")
        def _probe_delegate(partner_aic: str, request: str) -> dict:
            """真的被调用 ⇒ 说明接管没生效（本该由 EvidenceInboxHooks 截住）。"""
            HANDLER_HITS.append(partner_aic)
            return {"probe": "原始 handler 被调用", "partner_aic": partner_aic}

        class _OfflineRuntime(WitRuntime):
            """把接管后的**唯一出口** `aip_delegate` 换成本地记录 —— 全程不出网。"""

            def __init__(self, *a, **kw):  # type: ignore[no-untyped-def]
                super().__init__(*a, **kw)
                self.delegated: list[dict] = []

            async def aip_delegate(self, state, **kw):  # type: ignore[no-untyped-def]
                self.delegated.append(dict(kw))
                return {"status": "accepted", "task_ref": "T-probe", "state": "submitted"}

        class _OuterSpy(_WitHooks):
            """`priority = 50` 的替身：它看得见这次委派 ⇒ 闸门没被短路。"""

            id = "verify.outer-spy"
            priority = 50

            def __init__(self) -> None:
                self.seen: list[str] = []

            def state(self, ctx):  # type: ignore[no-untyped-def]
                return {}

            async def wrap_tool_call(self, ctx, tool_call, handler):  # type: ignore[no-untyped-def]
                self.seen.append(str(getattr(tool_call, "name", "")))
                return await handler(tool_call)

        class _DelegatingModel(StubModel):
            """第 1 轮就把句柄写进 `acps_delegate` 的请求里。"""

            def __init__(self, handle: str) -> None:
                super().__init__()
                self.handle = handle

            async def complete(self, request):  # type: ignore[no-untyped-def]
                if any(m.role == "tool" for m in request.messages):
                    return ModelResponse.final("（探针）结束。")
                names = [str(getattr(t, "name", "")) for t in request.tools]
                if "acps_delegate" not in names:
                    return ModelResponse.final("（探针）没看到 acps_delegate 工具。")
                return ModelResponse.tools(
                    [
                        _ToolCall(
                            id="probe-delegate-1",
                            name="acps_delegate",
                            arguments={
                                "partner_aic": "aic:probe-not-a-real-peer",
                                "request": f"请检查这张图有无篡改痕迹 {self.handle}",
                            },
                        )
                    ]
                )

        root6 = inbox.inbox_root(td6)
        item6 = inbox.stage(N_CLEAN_JPEG, root=root6, mime_type="image/jpeg", name="现场.jpg")
        token6 = inbox.bind_root(root6)
        try:
            spy6 = _OuterSpy()
            model6 = _DelegatingModel(item6.handle)
            rt6 = _OfflineRuntime(
                WitAgent(
                    id="inbox-chain-order-probe",
                    instructions="探针：把句柄写进 acps_delegate 的请求里。",
                    model=model6,
                    # `acps_delegate` 这个工具由**本探针自己**声明（而不是挂
                    # `AipDelegationHooks` 让它注册）——
                    # ⚠️ 实测：`AipDelegationHooks.on_run_start` 会要一个真的
                    #    `AcpsConfig`（`RuntimeError: AipDelegationHooks requires
                    #    AcpsDirect with rpc mode`），而那是运行期才有的东西。
                    #    本节要验的是 **Hook 链的顺序**，而 `AipDelegationHooks`
                    #    只实现 `on_run_start`/`context`、**没有** `wrap_tool_call`
                    #    ⇒ 它在不在场都不影响顺序。所以这里只保留真正参与
                    #    `wrap_tool_call` 的那两层，顺序与真实角色一致。
                    capabilities=[_probe_delegate],
                    hooks=[
                        spy6,                                   # priority = 50（闸门位置）
                        DebateChainGuardHooks(self_aic=None),    # priority = 50（真闸门）
                        EvidenceInboxHooks(),                    # priority = 100（接管）
                    ],
                ),
                workspace=td6,
            )
            run6 = _asyncio.run(
                rt6.run(
                    [TextPart(text="请看这张图"), ImagePart(mime_type="image/jpeg", data=N_CLEAN_JPEG)]
                )
            )
            coord6 = rt6.delegated
            calls6 = list(coord6)
        finally:
            inbox.unbind_root(token6)

        images6 = [
            part
            for call in calls6
            for part in (call["request"] if not isinstance(call["request"], str) else [])
            if type(part).__name__ == "ImagePart"
        ]
        check("真链路（真 WitRuntime + 真 Hook 链）：委派请求由**代码**带上了原始图片字节",
              any(getattr(p, "data", None) == N_CLEAN_JPEG for p in images6),
              f"run={run6.status}，接管 {len(calls6)} 次，ImagePart {len(images6)} 个"
              f"，原 handler 被调用 {len(HANDLER_HITS)} 次")
        check("……且 `priority = 50` 的外层 Hook 仍然**包住**了这次委派（接管没有把它短路掉）",
              "acps_delegate" in spy6.seen,
              f"外层探针看到 {spy6.seen or '（空 —— 整段被短路了）'}")
        check("接管 Hook 必须在边界闸门**之内**"
              "（`EvidenceInboxHooks.priority` > `DebateChainGuardHooks.priority`）",
              EvidenceInboxHooks.priority > DebateChainGuardHooks.priority,
              f"inbox={EvidenceInboxHooks.priority} 闸门={DebateChainGuardHooks.priority}"
              "（取小了 ⇒ 身份/链深/环检测一次都不会执行）")

    # ── ⑦ 句柄的**归属**：一份字节只属于带来它的那一轮 ────────────────────
    #    句柄是**内容哈希** ⇒「这份字节在」与「这份字节归我」是两件事。
    #    原先 `resolve()` 只在**文件不存在**时才拒绝，于是 B 轮可以取走 A 轮落下的
    #    字节当证据 —— 一次**溯源错误**，而报告上看不出任何异常（#95 第 3 项）。
    #
    #    ⚠️ 这里必须断**正反两面**：只断"跨轮次被拒"会被"**永远拒绝**"的实现骗过，
    #       所以"同归属必须取得回来"与"没有归属的世界必须照常通过"要一起断
    #       （后者是离线核对脚本与本脚本自己的世界；把那条关掉，就是判据成了故障源）。
    def _resolve_as(handle: str, root, owner: str) -> str:  # type: ignore[no-untyped-def]
        token = inbox.bind_owner(owner)
        try:
            inbox.resolve(handle, root=root)
            return "ok"
        except inbox.InboxRefused as exc:
            return f"拒绝：{exc}"
        finally:
            inbox.unbind_owner(token)

    with _tf.TemporaryDirectory() as td7:
        root7 = inbox.inbox_root(td7)
        handle7 = inbox.handle_for(N_CLEAN_JPEG)
        stage_token7 = inbox.bind_owner("run:A")
        try:
            inbox.stage(N_CLEAN_JPEG, root=root7, mime_type="image/jpeg")
        finally:
            inbox.unbind_owner(stage_token7)

        same7 = _resolve_as(handle7, root7, "run:A")
        check("同一轮次：自己落的字节自己取得回来（闸门不许把正常链路也一起挡住）",
              same7 == "ok", same7)
        cross7 = _resolve_as(handle7, root7, "run:B")
        check("跨轮次：B 轮**取不走** A 轮落下的字节"
              "（否则一份证据的归属被跨轮次推断，而报告上看不出任何异常）",
              cross7.startswith("拒绝："), cross7[:120])

    with _tf.TemporaryDirectory() as td8:
        root8 = inbox.inbox_root(td8)
        handle8 = inbox.handle_for(N_CLEAN_JPEG)
        # 落盘时**没有归属**（=「不属于任何一次 run」）：离线核对脚本就是这种情形。
        inbox.stage(N_CLEAN_JPEG, root=root8, mime_type="image/jpeg")
        unbound8 = _resolve_as(handle8, root8, "")
        check("没有归属的世界（离线核对 / 本脚本自己）照常通过"
              "（把这条一起关掉 = 判据自己成了故障源）",
              unbound8 == "ok", unbound8)

    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 74)
    print("第 10 节 · 委派两条链的等价性 + 字节保真（都在离线可证范围内）")
    print("        （上一轮把这两条记成「必须真机验」。这一节就是在说：不必等真机）")
    print("=" * 74)
    from types import SimpleNamespace as _NS10

    import wit.acps.aip as _aip
    import wit.hooks.acps.delegation as _deleg
    from wit.kernel.capabilities import ToolManager as _ToolManager

    def _fake_coord():  # type: ignore[no-untyped-def]
        class _Coord:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def start_background_services(self) -> None:
                return None

            def ensure_session(self, state) -> None:  # type: ignore[no-untyped-def]
                return None

            async def discover(self, **kw):  # type: ignore[no-untyped-def]
                return {"partners": []}

            async def delegate(  # type: ignore[no-untyped-def]
                self, state, partner_aic, request, *, delivery=None, notify_on_states=None
            ):
                self.calls.append(
                    {
                        "partner_aic": partner_aic,
                        "request": request,
                        "delivery": delivery,
                        "notify_on_states": notify_on_states,
                    }
                )
                return {"status": "accepted", "task_id": "T1", "partner_aic": partner_aic}

        return _Coord()

    coord = _fake_coord()
    hook_d = _deleg.AipDelegationHooks(delivery="rpc")
    added: dict = {}

    class _Ctx10:
        def __init__(self) -> None:
            self.runtime = _NS10(_ensure_aip_coordinator=lambda: coord)
            self.state = _NS10(messages=[])
            self.tool_manager = _ToolManager()
            self.private_state = {}

        def add_tool(self, tool, source="hook"):  # type: ignore[no-untyped-def]
            added[tool.name] = tool
            self.tool_manager.add_tool(tool)
            return tool

    _asyncio.run(hook_d.on_run_start(_NS10(context=_Ctx10())))
    tool_d = added.get("acps_delegate")
    check("AipDelegationHooks.on_run_start 真的注入了 acps_delegate（先答“谁来调它”）",
          tool_d is not None, f"注入的工具：{sorted(added)}")

    ret_tool = _asyncio.run(tool_d.invoke({"partner_aic": "aic:p", "request": "请看图"}, None))
    ret_api = _asyncio.run(
        WitRuntime.aip_delegate(
            _NS10(_aip_delegation_hook=lambda: hook_d, _ensure_aip_coordinator=lambda: coord),
            _NS10(messages=[]),
            partner_aic="aic:p",
            request=[TextPart(text="请看图")],
        )
    )
    call_a, call_b = coord.calls[0], coord.calls[1]
    check("接管用的 `aip_delegate` 与原生 acps_delegate handler 走**同一个** coordinator.delegate",
          len(coord.calls) == 2, f"coordinator.delegate 被调用 {len(coord.calls)} 次")
    check("……且 partner_aic / delivery / notify_on_states 三项逐一对齐 ⇒ 返回结构必然一致",
          (call_a["partner_aic"], call_a["delivery"], call_a["notify_on_states"])
          == (call_b["partner_aic"], call_b["delivery"], call_b["notify_on_states"]),
          f"handler delivery={call_a['delivery']!r} / aip_delegate delivery={call_b['delivery']!r}")
    check("……且两边返回的 dict 形状相同（模型读到的东西一致）",
          isinstance(ret_tool, dict) and isinstance(ret_api, dict) and sorted(ret_tool) == sorted(ret_api))
    check("两条链**唯一**的差别是入参能力：工具层只能给字符串或 uri，代码层能给字节",
          isinstance(call_a["request"], str) and not isinstance(call_b["request"], str),
          f"handler request={type(call_a['request']).__name__} / aip_delegate request={type(call_b['request']).__name__}")

    sample10 = bytes(range(256)) * 3  # 覆盖全部 256 种字节值，任何编码损坏都会露馅
    wire = _aip.content_parts_to_data_items(
        [TextPart(text="请鉴伪"), ImagePart(mime_type="image/jpeg", data=sample10, name="现场.jpg")]
    )
    files = [it for it in wire if it.get("type") == "file"]
    check("出站：ImagePart(内联字节) 上线格式就是 `file` + `bytes`(base64)，不会退化成 uri",
          len(files) == 1 and bool(files[0].get("bytes")) and not files[0].get("uri"),
          f"keys={sorted(files[0]) if files else []}")
    back = [p for p in _aip.data_items_to_content_parts(wire) if getattr(p, "type", None) == "image"]
    check("往返：对端还原成 ImagePart(data=…) —— **一个字节都没变**（这是路二跨进程的协议层证明）",
          len(back) == 1 and back[0].data == sample10,
          f"{len(back[0].data) if back else 0} / {len(sample10)} 字节")
    check("……且 mimeType 与文件名一并保住",
          bool(back) and back[0].mime_type == "image/jpeg" and back[0].name == "现场.jpg", "")

    wire2 = _aip.content_parts_to_data_items(
        [ImagePart(mime_type="image/png", uri="https://cdn.example.com/a.png")]
    )
    check("反面对照：uri 形态出站仍是 uri 形态（不臆造成内联字节，也不被吞掉）",
          bool(wire2) and wire2[0].get("uri") == "https://cdn.example.com/a.png"
          and not wire2[0].get("bytes"),
          f"{dict(wire2[0]) if wire2 else '-'}")

    print()
    if FAILED:
        print(f"有 {len(FAILED)} 个用例未达预期：")
        for label in FAILED:
            print(f"  · {label}")
        return 1
    print("全部用例符合预期：")
    print("  · 5 个负对照（含大图裁剪、大片纯色）的**各项检查**全部零触发 —— 误报是最致命的失败")
    print("  · 4 个正样本各被**预期的检查项**抓到，且报出的量值就是真实量值")
    print("  · 等级 / 升级建议 / 哈希 / 尺寸全部由代码复算，无一来自模型或调用方")
    print("  · **前提不满足时判「无法判定」而不是「低」** —— 把「测不了」说成「没问题」")
    print("    是本项最危险的失败形态；纯色图与干净自然图两侧都断言了（成对断言）")
    print("  · **复检阶梯不随等级裁剪**：线下司法鉴定（唯一有法律证明力的路径）任何等级都在")
    print("  · 每条发现都带 caveat；输出里不存在「真 / 假」这样的断言")
    print("  · 取图入口默认拒绝（fail-closed）：未配白名单=禁止出网，明文 http 与本地路径一律拒")
    print("  · **工具边界一个异常都不漏**：畸形字节 / 截断 / 超大字头，四种输入都返回")
    print("    结构化结论 + 可执行的补救建议（调用方拿到的是「还能怎么办」，不是一句报错）")
    print("  · **「给主控的那句话」与报告同源**：测不了时绝不会被写成「多项均未提示异常」")
    print("  · MIME 常见别名（image/jpg）不误报，真换了容器仍然报；未声明类型时不说「与声明一致」")
    print("  · 超限判定发生在**解码之前**、超大图闸门发生在**解压之前**（不让「判它有问题」先吃满内存）")
    print("  · 来源标注如实（local_file ≠ network）；context_note 收下了就往下传")
    print("  · 新角色没有把 Pillow/numpy 拉进主链路的模块级导入（缺库也不会拖垮其余 6 个 Agent）")
    print("  · **入站附件两种形态都接**：bytes → 收件箱 + 句柄；uri → 如实转述地址、不自动出网")
    print("    （只认一种 = 另一种被静默丢弃，而「没收到图」与「收到了没当回事」在结论里一样）")
    print("  · **委派两条链是同一个实现**：aip_delegate 与原生 acps_delegate handler 都走")
    print("    coordinator.delegate，partner_aic/delivery/notify_on_states 逐项对齐 ⇒ 返回同构")
    print("  · **字节经 AIP 线格式往返一个比特不变**（含全部 256 种字节值）—— 路二跨进程的协议层证明")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
