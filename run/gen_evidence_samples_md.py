"""生成 `docs/aicomp/evidence-image-样例输出.md` —— **可复现，不要手改那份文档**。

为什么要有这个脚本（而不是当初那条一次性命令）：
文档里写着"自动生成，请勿手改"，但如果**生成它的东西不存在**，那句话就是空头支票 ——
下一次代码改了行为，谁也不知道该拿什么重新生成，于是文档会静静地留在旧结论上。
（本轮就真的发生了：`container` 项从"未提示异常"改成了"未提示异常 / 无法判定"，
样例文档里那一段立刻变成了过期事实，而它看起来完全正常。）

用法（在 witrelate 目录下）：
    PYTHONPATH=. ./.venv/Scripts/python.exe -m run.gen_evidence_samples_md
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from legal_acps.evidence import analyze
from legal_acps.evidence.pipeline import outcome, summary_line
from run.samples import W, adobe_exif, flat_area_image, jpeg, oversized_png, photo

OUT = Path("docs/aicomp/evidence-image-样例输出.md")


def _fmt_strength(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def main() -> int:
    out: list[str] = []
    w = out.append

    def block(title: str, data: bytes):  # type: ignore[no-untyped-def]
        rep = analyze(data)
        fired = [f.check for f in rep.findings if f.verdict == "提示异常"]
        w(f"### {title}")
        w("")
        w(f"- 容器 `{rep.media_type}` ｜ 尺寸 {rep.width}×{rep.height} ｜ {rep.byte_size} 字节")
        w(f"- SHA-256 `{rep.sha256}`")
        w(f"- **综合线索等级：{rep.suspicion}** ｜ 触发项：{'、'.join(fired) if fired else '（无）'}")
        w(f"- 等级理由：{rep.suspicion_rationale}")
        w("")
        w("| 检查项 | 判定 | 强度 | 说明 |")
        w("|---|---|---|---|")
        for f in rep.findings:
            w(f"| `{f.check}` | {f.verdict} | {_fmt_strength(f.strength)} | {f.detail} |")
        w("")
        w("**每条线索各自的 caveat（它不能证明什么）**：")
        w("")
        for f in rep.findings:
            w(f"- `{f.check}`：{f.caveat}")
        w("")
        return rep

    w("# `evidence-image` 样例输出（自动生成，请勿手改）")
    w("")
    w("> 生成方式：`PYTHONPATH=. ./.venv/Scripts/python.exe -m run.gen_evidence_samples_md`")
    w("> 样本由 `run/samples.py` **合成** —— 不涉及任何真实个人图像。")
    w("> 下面刻意覆盖四种**报告**结局（正常 / 提示异常 / 无法判定）")
    w("> 与三种**工具边界**形态（取图被拒 / 输入不是图像 / 输入过大）。")
    w("")
    w("---")
    w("")
    w("## 1. 干净 JPEG —— 预期「正常」")
    w("")
    block("N2. 干净 JPEG q85（自然摄影内容）", jpeg(photo("structured"), 85))
    w("---")
    w("")
    w("## 2. 元数据矛盾 —— 预期「提示异常」")
    w("")
    rep_meta = block("P3. Software=Photoshop、无相机信息、时间戳不一致",
                     jpeg(photo("structured"), 88, exif=adobe_exif()))
    w("---")
    w("")
    w("## 3. 复制-移动（克隆）—— 预期「提示异常」")
    w("")
    clone = photo("structured").copy()
    clone[150:214, 160:224] = clone[150:214, 472:536]
    block("P2. 64×64 面片被贴到 312px 之外", jpeg(clone, 92))
    w("---")
    w("")
    w("## 4. 大片纯色 —— 预期「无法判定」（而不是「正常」）")
    w("")
    block("N5. 大片纯色 JPEG q70（截图 / 拍白墙 / 拍文件这类输入）", jpeg(flat_area_image(), 70))
    w("> 这一例是**最重要的输出形态**。像素域检查（ELA / 噪声残差 / 克隆）的前提是")
    w("> 自然摄影内容；大面积纯色不满足前提，所以必须说「无法判定」。")
    w(">")
    w("> 注意「给主控的那一句话」（`summary_for_leader`）—— 它是会被原样转述给用户的那句：")
    w(">")
    w(f"> 「{summary_line(analyze(jpeg(flat_area_image(), 70)))}」")
    w(">")
    w("> 这一行的要求是：**不允许出现「N 项检查均未提示异常」**。")
    w("> 那读起来就是「没问题」，而真相是能发现内容层拼接 / 克隆的三项**整项没有执行**，")
    w("> 剩下能出结论的只有封装层检查。**把「测不了」说成「没问题」，是本能力最危险的失败形态。**")
    w("")
    w("---")
    w("")
    w("## 5. 复检路径（`upgrades`）—— 交付给用户的「更准但更贵」选项")
    w("")
    w("这是本能力对用户最核心的产出：**本地初检免费且秒级，但它只能给线索；")
    w("要出可举证的结论就必须升级**。以下为程序按成本递增生成的完整清单：")
    w("")
    w("> ⚠️ 这份清单一律**完整给出，不随线索等级裁剪**。早先按等级裁剪（低→前 2 条）时，")
    w("> **「线下司法鉴定」在最常见的「低」里被静默删掉了** —— 而它是唯一有法律证明力的路径。")
    w("")
    for u in rep_meta.upgrades:
        w(f"- **{u.tier} ｜ {u.name}** — 成本：{u.cost} ｜ 时限：{u.turnaround}")
        w(f"  - 何时值得用：{u.when_to_use}")
    w("")
    w("---")
    w("")
    w("## 6. 证据保全与免责")
    w("")
    w("- `chain_of_custody`：")
    for c in rep_meta.chain_of_custody:
        w(f"  - {c}")
    w(f"- `disclaimer`：{rep_meta.disclaimer}")
    w("")
    w("---")
    w("")
    w("## 7. 工具边界：**失败也要给得出东西**")
    w("")
    w("工具入口（`pipeline.outcome`）唯一的承诺是：**永远返回结构化结果，一个异常都不漏。**")
    w("理由不是好看 —— 调用方（模型）收到一句不透明的异常时，最可能的反应是")
    w("**换个路子把图弄进来**，正好绕过刚设的边界。所以失败一律折成")
    w("`status=refused` + `refusal_reason` + `how_to_fix`：")
    w("")
    w("| 情形 | `refusal_reason` | `how_to_fix` |")
    w("|---|---|---|")

    import base64 as b64
    import tempfile as _tempfile

    from legal_acps.evidence import inbox as _inbox

    # 绑定一个**空**的收件箱：这样示例展示的是「句柄没登记过」这条**典型**分支，
    # 而不是「这个进程压根没绑收件箱」那条只可能出现在测试 / 一次性工具里的分支。
    # （两条都会如实拒绝，但示例要让读者看到真实服务进程里会碰到的那一条。）
    _inbox.bind_root(Path(_tempfile.mkdtemp()) / _inbox.INBOX_DIRNAME)

    cases = [
        ("出网但未配白名单", "https://img.example.com/a.jpg"),
        ("明文 http", "http://img.example.com/a.jpg"),
        ("本地文件（默认禁止）", "file:///etc/shadow"),
        ("伪造 / 未登记的收件箱句柄", "inbox:" + "0" * 64),
        ("声明是图片、字节是垃圾", "data:image/png;base64,AAAA"),
        ("JPEG 只有文件头（截断）",
         "data:image/jpeg;base64," + b64.b64encode(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00").decode()),
        ("超大图（只有文件头、无像素数据）",
         "data:image/png;base64," + b64.b64encode(oversized_png()).decode()),
    ]
    for desc, uri in cases:
        o = outcome(uri)
        w(f"| {desc} | `{o.refusal_reason}` | {o.how_to_fix} |")
    w("")
    w("> ⚠️ 最后三种**不是策略拒绝，而是输入本身不可用** —— 它们曾经会**直接抛异常**穿透出去。")
    w("> 补救建议也必须分清是谁的责任：输入不可用时写「请确认给的是图片本身的字节」，")
    w("> 而不是兜底成「请联系管理员」—— 后者会把一个五秒钟能改对的问题变成一次没人能处理的上报。")
    w("")
    w("---")
    w("")
    w("## 8. 复现方式")
    w("")
    w("```bash")
    w("# 10 节断言：负对照 / 正样本 / 参数归属 / 边界输入 / 取图策略 / Agent 接线 /")
    w("#            附件通道（两种形态）/ 委派链等价性与字节保真")
    w("PYTHONPATH=. ./.venv/Scripts/python.exe -m tools.verify_evidence")
    w("# 20 用例元验证：把关键实现逐处退回旧形态，要求上面那些断言立刻报失败")
    w("PYTHONPATH=. ./.venv/Scripts/python.exe -m run.meta_verify_evidence")
    w("```")
    w("")

    OUT.write_bytes(("\n".join(out) + "\n").encode("utf-8"))
    size = OUT.stat().st_size
    crlf = OUT.read_bytes().count(b"\r\n")
    print(f"written: {OUT} ({size} bytes) | CRLF={crlf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
