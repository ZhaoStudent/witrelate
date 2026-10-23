"""Build the 法前哨 technical proposal slide deck."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt
from pptx.oxml.ns import qn

ROOT = Path(__file__).resolve().parents[2]
AICOMP = ROOT / "docs" / "aicomp"
OUT = AICOMP / "法前哨-答辩PPT.pptx"

BG = RGBColor(0xF7, 0xF9, 0xFC)
NAVY = RGBColor(0x12, 0x3B, 0x5D)
BLUE = RGBColor(0x1F, 0x4E, 0x79)
TEAL = RGBColor(0x2A, 0x9D, 0x8F)
ORANGE = RGBColor(0xE9, 0xA2, 0x3B)
RED = RGBColor(0xC0, 0x5A, 0x4A)
GRAY = RGBColor(0x5B, 0x6B, 0x7C)
MID = RGBColor(0x7B, 0x8B, 0x9B)
LIGHT = RGBColor(0xE8, 0xF1, 0xFB)
LIGHT2 = RGBColor(0xEE, 0xF3, 0xF8)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
FONT = "微软雅黑"

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
BLANK = prs.slide_layouts[6]
TOTAL = 16


def set_run_font(run, size=16, bold=False, color=GRAY, name=FONT):
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    rpr = run._r.get_or_add_rPr()
    ea = rpr.find(qn("a:ea"))
    if ea is None:
        ea = rpr.makeelement(qn("a:ea"), {})
        rpr.append(ea)
    ea.set("typeface", name)


def new_slide():
    s = prs.slides.add_slide(BLANK)
    bg = s.background
    bg.fill.solid()
    bg.fill.fore_color.rgb = BG
    return s


def add_text(slide, text, x, y, w, h, size=16, bold=False, color=GRAY,
             align=PP_ALIGN.LEFT, line=1.0, anchor=MSO_ANCHOR.TOP, name=FONT):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    for i, line_text in enumerate(str(text).split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = line
        r = p.add_run()
        r.text = line_text
        set_run_font(r, size=size, bold=bold, color=color, name=name)
    return box


def add_bullets(slide, items, x, y, w, h, size=15, color=GRAY, bullet_color=TEAL,
                line=1.15, gap=6):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.line_spacing = line
        p.space_after = Pt(gap)
        r1 = p.add_run()
        r1.text = "• "
        set_run_font(r1, size=size, bold=True, color=bullet_color)
        r2 = p.add_run()
        r2.text = str(item)
        set_run_font(r2, size=size, color=color)
    return box


def add_rect(slide, x, y, w, h, fill, line=None, radius=True, line_w=1.0):
    shape_type = MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE
    shp = slide.shapes.add_shape(shape_type, Inches(x), Inches(y), Inches(w), Inches(h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill
    if line is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = line
        shp.line.width = Pt(line_w)
    shp.shadow.inherit = False
    if radius:
        try:
            shp.adjustments[0] = 0.08
        except Exception:
            pass
    return shp


def add_arrow(slide, x, y, w, h, fill=RGBColor(0xB9, 0xCB, 0xDA)):
    shp = slide.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, Inches(x), Inches(y), Inches(w), Inches(h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill
    shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def add_card(slide, x, y, w, h, title, body, fill=WHITE, line=RGBColor(0xD4, 0xE0, 0xEA),
             title_color=NAVY, body_color=GRAY, title_size=15, body_size=11.5):
    add_rect(slide, x, y, w, h, fill, line)
    add_text(slide, title, x + 0.16, y + 0.12, w - 0.32, 0.4,
             size=title_size, bold=True, color=title_color)
    add_text(slide, body, x + 0.16, y + 0.52, w - 0.32, h - 0.62,
             size=body_size, color=body_color, line=1.1)


def add_title(slide, kicker, title):
    add_text(slide, kicker, 0.6, 0.28, 9.0, 0.28, size=10, bold=True, color=TEAL)
    add_text(slide, title, 0.6, 0.55, 12.1, 0.65, size=25, bold=True, color=NAVY)
    add_rect(slide, 0.6, 1.18, 12.13, 0.012, RGBColor(0xD4, 0xE0, 0xEA), radius=False)


def add_footer(slide, index):
    add_rect(slide, 0.6, 7.06, 12.13, 0.008, RGBColor(0xD4, 0xE0, 0xEA), radius=False)
    add_text(slide, "法前哨 · 技术方案 v1.0", 0.6, 7.12, 6.0, 0.22, size=8, color=MID)
    add_text(slide, f"{index:02d} / {TOTAL:02d}", 11.35, 7.12, 1.35, 0.22,
             size=8, color=MID, align=PP_ALIGN.RIGHT)


def add_image_fit(slide, path, x, y, w, h, border=True):
    path = Path(path)
    iw, ih = Image.open(path).size
    scale = min(w / iw, h / ih)
    dw, dh = iw * scale, ih * scale
    dx = x + (w - dw) / 2
    dy = y + (h - dh) / 2
    pic = slide.shapes.add_picture(str(path), Inches(dx), Inches(dy), Inches(dw), Inches(dh))
    if border:
        pic.line.color.rgb = RGBColor(0xD4, 0xE0, 0xEA)
        pic.line.width = Pt(0.75)
    return pic


def add_table(slide, rows, cols, x, y, w, h, widths=None, header_size=11, body_size=10.5):
    shape = slide.shapes.add_table(rows, cols, Inches(x), Inches(y), Inches(w), Inches(h))
    table = shape.table
    table.first_row = True
    table.horz_banding = False
    if widths:
        for i, cw in enumerate(widths):
            table.columns[i].width = Inches(cw)
    for r in range(rows):
        table.rows[r].height = Inches(h / rows)
    return table


def set_cell(cell, text, size=10.5, bold=False, color=GRAY, fill=None,
             align=PP_ALIGN.LEFT):
    if fill is not None:
        cell.fill.solid()
        cell.fill.fore_color.rgb = fill
    cell.margin_left = Inches(0.07)
    cell.margin_right = Inches(0.07)
    cell.margin_top = Inches(0.04)
    cell.margin_bottom = Inches(0.04)
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf = cell.text_frame
    tf.clear()
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = align
    r = p.add_run()
    r.text = str(text)
    set_run_font(r, size=size, bold=bold, color=color)


def add_notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


# ---------------------------------------------------------------- 01 cover
s = new_slide()
add_rect(s, 0.62, 1.15, 0.08, 3.15, TEAL, None, radius=False)
add_text(s, "第八届全球校园人工智能算法精英大赛 · 算法主题赛 · 智能体互联", 0.95, 0.52, 10.8, 0.3,
         size=10.5, color=MID)
add_text(s, "法前哨", 0.95, 1.05, 8.0, 0.9, size=54, bold=True, color=NAVY)
add_text(s, "面向法律纠纷“事前准备”的可审计多智能体系统", 0.98, 2.0, 8.6, 0.5,
         size=21, bold=True, color=BLUE)
add_text(s, "技术方案", 0.98, 2.55, 5.0, 0.35, size=14, bold=True, color=TEAL)
add_text(s, "不是“遇到官司再问律师”，\n而是“在成为当事人之前，先把事实、证据、话术准备好”。",
         0.98, 3.35, 8.6, 1.0, size=14.5, color=GRAY, line=1.25)
for i, label in enumerate(["梧桐 ACPs", "mTLS + AIC", "两级嵌套 Leader"]):
    x = 0.98 + i * 2.35
    add_rect(s, x, 5.85, 2.05, 0.38, WHITE, RGBColor(0xD4, 0xE0, 0xEA))
    add_text(s, label, x, 5.94, 2.05, 0.24, size=10.5, color=BLUE, align=PP_ALIGN.CENTER)
add_text(s, "平台：梧桐智能体互联平台  wt.ioa.pub     版本：v1.0 · 2026-10-21",
         0.98, 6.55, 10.8, 0.3, size=10, color=MID)
# right-side capability rail (no empty card)
add_rect(s, 9.95, 1.2, 0.012, 4.3, RGBColor(0xD4, 0xE0, 0xEA), None, radius=False)
rail = [("01", "事实核查", "说法可不可信"), ("02", "证据鉴伪", "图/音/视频有没有被动过"),
        ("03", "法律方向", "下一步该做什么"), ("04", "话术演练", "对方在带什么节奏")]
for i, (num, title, sub) in enumerate(rail):
    y = 1.3 + i * 1.05
    add_text(s, num, 10.15, y, 0.35, 0.2, size=8.5, bold=True, color=TEAL)
    add_text(s, title, 10.55, y - 0.04, 2.3, 0.28, size=12, bold=True, color=NAVY)
    add_text(s, sub, 10.55, y + 0.27, 2.3, 0.22, size=8.8, color=GRAY)
add_notes(s, "开场 30 秒：法前哨不是又一个法律问答机器人，而是一套在‘成为当事人之前’做准备的系统：事实、证据、法律方向、话术，四条能力由不同 Agent 协作完成，每一步留痕。")

# ---------------------------------------------------------------- 02 one line
s = new_slide()
add_title(s, "01 / 作品概述", "一句话：把“事前准备”做成一条可审计的链")
add_rect(s, 0.6, 1.42, 7.4, 3.75, WHITE, RGBColor(0xD4, 0xE0, 0xEA))
add_text(s, "“", 0.9, 1.55, 0.6, 0.6, size=52, bold=True, color=TEAL)
add_text(s, "很多人不是输在官司里，而是输在事发前：\n转了一条假消息、信了一张改过的图、\n在群里说错一句话、该留的证据没留。",
         1.35, 1.85, 6.3, 1.55, size=18, color=NAVY, line=1.35)
add_text(s, "法前哨把“事前”拆成四件事，由多个 Agent 协作完成；\n最终交付一份可回溯的准备清单。",
         1.35, 3.55, 6.2, 0.9, size=12.5, color=GRAY, line=1.3)
cards = [
    ("事实", "这个说法是真的吗？", "网络裁判 + 正反辩论 / 快车道"),
    ("证据", "图 / 音频 / 视频被动过吗？", "证据鉴证 Leader + 三模态专家"),
    ("法律", "下一步该做什么？", "法律顾问：库内法条逐字引用"),
    ("话术", "对方在带什么节奏？", "演练场：信息三桶 + 复盘"),
]
for i, (tag, q, cap) in enumerate(cards):
    y = 1.42 + i * 1.13
    add_rect(s, 8.35, y, 4.35, 0.98, WHITE, RGBColor(0xD4, 0xE0, 0xEA))
    add_rect(s, 8.35, y, 0.08, 0.98, TEAL if i % 2 == 0 else ORANGE, None, radius=False)
    add_text(s, tag, 8.58, y + 0.10, 0.8, 0.3, size=13, bold=True, color=NAVY)
    add_text(s, q, 9.35, y + 0.10, 3.15, 0.3, size=11.5, color=GRAY)
    add_text(s, cap, 8.58, y + 0.48, 3.9, 0.3, size=10.5, color=BLUE)
add_rect(s, 0.6, 5.55, 12.1, 0.72, RGBColor(0xE8, 0xF1, 0xFB), None)
add_text(s, "最终交付：FinalVerdict + 可回溯准备清单     |     不做法律定性，不替代执业律师",
         0.85, 5.73, 11.6, 0.35, size=13, bold=True, color=NAVY)
add_footer(s, 2)
add_notes(s, "用一句话把产品讲清楚：四个问题，四条能力，一条可审计的链；不是把四个工具拼在一起，而是每个角色只交一种结构化结论。")

# ---------------------------------------------------------------- 03 moments
s = new_slide()
add_title(s, "01 / 作品概述", "四个真实时刻，四种“来不及”")
moments = [
    ("转发前", "群里看到“某地爆炸 / 某银行倒闭”，转还是不转？", "要的是：这条说法可不可信、转了会不会出事"),
    ("被挂截图", "被人挂了一张聊天记录截图，是真的吗？该不该回应？", "要的是：截图/录音有没有被动过、怎么接话"),
    ("签约前", "租房、借款、合作合同，哪些条款要警惕？", "要的是：哪些证据先留、哪些话不能说"),
    ("找律师前", "材料一团乱，说不清时间线。", "要的是：先整理事实、证据、诉求，再决定找谁"),
]
for i, (tag, scene, need) in enumerate(moments):
    x = 0.6 + (i % 2) * 6.15
    y = 1.45 + (i // 2) * 2.05
    add_rect(s, x, y, 5.9, 1.83, WHITE, RGBColor(0xD4, 0xE0, 0xEA))
    add_text(s, tag, x + 0.2, y + 0.16, 2.0, 0.32, size=16, bold=True, color=NAVY)
    add_text(s, scene, x + 0.2, y + 0.57, 5.45, 0.55, size=11.5, color=GRAY, line=1.2)
    add_text(s, need, x + 0.2, y + 1.25, 5.45, 0.32, size=10.5, color=BLUE)
add_rect(s, 0.6, 5.95, 12.1, 0.62, RGBColor(0xFF, 0xF6, 0xE6), RGBColor(0xF0, 0xD2, 0x9A))
add_text(s, "共同点：信息不全、证据没留、话术说错——用户要的不是“法律意见”，是“事前准备”。",
         0.85, 6.12, 11.6, 0.3, size=13, bold=True, color=RGBColor(0x7A, 0x4A, 0x12))
add_footer(s, 3)
add_notes(s, "四个时刻不是四个产品，而是同一条链的四个入口。真正难的是：信息不全时也要能给出诚实的‘无法判定’，并把下一步动作说清楚。")

# ---------------------------------------------------------------- 04 gaps
s = new_slide()
add_title(s, "02 / 需求分析", "真正的难题：异构智能体互联的五个缺口")
add_text(s, "不是“再做一个法律 Agent”，而是：当能力分散在不同账号、机器、团队的 Agent 上，怎么安全地发现、委派、回传、留痕。",
         0.6, 1.28, 12.1, 0.35, size=12.5, color=GRAY)
headers = [("#", 0.62, 0.5), ("缺口", 1.05, 1.9), ("具体表现", 3.05, 4.7), ("本作品的回答", 8.0, 4.6)]
for text, x, w in headers:
    add_text(s, text, x, 1.78, w, 0.25, size=10.5, bold=True, color=MID)
rows = [
    ("01", "身份与信任", "跨账号调用，怎么确认“你是谁、能不能代表这个 Agent”", "mTLS + AIC；平台审核；账号前缀闸门"),
    ("02", "发现", "地址写死就失去互联意义；按 AIC 搜是语义检索", "acps.discover 按能力/名称；AIC 只作身份"),
    ("03", "契约", "各家用自然语言回传，Leader 无法稳定解析", "每角色一个 pydantic 交付物；@capability 是提交通道"),
    ("04", "调度与安全", "多跳委派容易越界、成环、把任务派给错人", "两级嵌套；MAX_CHAIN_DEPTH=3；环检测"),
    ("05", "可观测与合规", "出了事只有聊天记录，无法证明哪一跳用了什么材料", "任务库 + sidecar + 可点击溯源表"),
]
for i, (num, gap, pain, answer) in enumerate(rows):
    y = 2.12 + i * 0.86
    add_rect(s, 0.6, y, 12.1, 0.74, WHITE if i % 2 == 0 else LIGHT2,
             RGBColor(0xD4, 0xE0, 0xEA), radius=False)
    add_text(s, num, 0.72, y + 0.20, 0.4, 0.3, size=11, bold=True, color=TEAL)
    add_text(s, gap, 1.08, y + 0.16, 1.85, 0.35, size=13, bold=True, color=NAVY)
    add_text(s, pain, 3.08, y + 0.14, 4.65, 0.45, size=10.5, color=GRAY, line=1.05)
    add_text(s, answer, 8.0, y + 0.14, 4.55, 0.45, size=10.5, color=BLUE, line=1.05)
add_text(s, "这五个缺口决定了：不能只做“一个 Leader 调几个 Partner”的 demo，必须把链深、身份、载荷、终止、留痕做成结构。",
         0.6, 6.55, 12.1, 0.3, size=11.5, bold=True, color=TEAL)
add_footer(s, 4)
add_notes(s, "这一页是需求分析的核心：不是讲法律行业多大，而是讲异构互联到底难在哪。五个缺口一一对应后面的五个机制。")

# ---------------------------------------------------------------- 05 architecture
s = new_slide()
add_title(s, "03 / 技术方案", "总体架构：两级嵌套 + 一条主链")
add_image_fit(s, AICOMP / "架构图.png", 0.4, 1.28, 9.85, 5.55)
add_text(s, "图 1 · 法前哨两级嵌套 Leader/Partner 架构", 0.6, 6.86, 9.4, 0.2,
         size=8.5, color=MID)
add_bullets(s, [
    "两级嵌套：主控 → 内层 Leader → 叶子；裁判/证据各自组织下游。",
    "链深上限 3；扇出不等于链深；越界/成环结构化拒绝。",
    "s1 主链 7 角色，s2 证据三叶子；DataPart 跨进程传链。",
    "演练场独立进程，不注册 ACPs，不计平台访问。",
], 10.35, 1.48, 2.55, 4.5, size=10.5, line=1.15, gap=9)
add_rect(s, 10.35, 6.05, 2.45, 0.62, RGBColor(0xE8, 0xF1, 0xFB), None)
add_text(s, "12 角色 · 10 部署\n2 软废弃", 10.48, 6.13, 2.2, 0.44, size=10, bold=True, color=BLUE)
add_footer(s, 5)
add_notes(s, "这张图是全场核心：上面是外层主控，下面两个内层 Leader。注意链深 3 的边界，以及证据三叶子在第二台机器。")

# ---------------------------------------------------------------- 06 roles
s = new_slide()
add_title(s, "03 / 技术方案", "角色与契约：10 个部署，每个只交一种结构化结论")
roles = [
    ("leader（法前哨）", "8445", "FinalVerdict", "外层主控"),
    ("fact-judge", "8448", "FactRuling", "内层裁判"),
    ("debate-pro", "8446", "结构化辩词", "正方"),
    ("debate-con", "8447", "结构化辩词", "反方"),
    ("evidence-leader", "8450", "EvidenceRuling", "证据汇总"),
    ("evidence-image", "8449 · s2", "模态线索", "图像鉴伪"),
    ("evidence-audio", "8451 · s2", "模态线索", "音频鉴伪"),
    ("evidence-video", "8452 · s2", "模态线索", "视频鉴伪"),
    ("law-advisor", "8453", "LegalAdvice", "法律建议"),
    ("lead-intake", "8454", "LeadSubmission", "线索受理"),
]
for i, (name, port, product, desc) in enumerate(roles):
    x = 0.6 + (i % 2) * 6.2
    y = 1.38 + (i // 2) * 0.92
    add_rect(s, x, y, 5.95, 0.78, WHITE, RGBColor(0xD4, 0xE0, 0xEA))
    add_text(s, name, x + 0.16, y + 0.10, 3.0, 0.3, size=12.5, bold=True, color=NAVY)
    add_text(s, port, x + 3.2, y + 0.10, 1.35, 0.3, size=10.5, color=MID)
    add_text(s, product, x + 4.35, y + 0.10, 1.45, 0.3, size=10.5, color=TEAL, align=PP_ALIGN.RIGHT)
    add_text(s, desc, x + 0.16, y + 0.44, 2.5, 0.22, size=9, color=MID)
add_text(s, "契约纪律：@capability 不是计算逻辑，而是模型提交结论的通道；缺字段 / 值域不对 → 模型可读的结构化错误。",
         0.6, 6.2, 12.1, 0.4, size=12, bold=True, color=BLUE)
add_text(s, "FinalVerdict 保留 is_incitement / harm_index 仅作兼容；两个评估角色软废弃后，这两个字段固定 false / 0，不再承担主指标。",
         0.6, 6.58, 12.1, 0.3, size=10.5, color=GRAY)
add_footer(s, 6)
add_notes(s, "10 个部署角色，每个角色只交一种结构化结论；这是 Leader 能稳定汇总的前提。incitement/harm 是软废弃，不部署。")

# ---------------------------------------------------------------- 07 fast lane
s = new_slide()
add_title(s, "03 / 技术方案", "机制 1：检索优先，辩论只在库外发生")
lanes = [
    ("事实库 / 经验库对齐", 1.6, LIGHT, BLUE),
    ("命中：快车道", 1.6, RGBColor(0xE6, 0xF4, 0xF0), TEAL),
    ("FactRuling", 1.6, WHITE, GRAY),
]
# top lane
for i, (text, y, fill, color) in enumerate(lanes):
    x = 0.6 + i * 3.35
    add_rect(s, x, y, 2.65, 0.85, fill, RGBColor(0xD4, 0xE0, 0xEA))
    add_text(s, text, x + 0.10, y + 0.20, 2.45, 0.45, size=11.5, bold=True, color=color, align=PP_ALIGN.CENTER)
for x in (3.3, 6.65):
    add_arrow(s, x, 1.92, 0.5, 0.26)
# bottom lane
for i, text in enumerate(["未命中：正反辩论", "辩手结构化回传", "FactRuling"]):
    x = 0.6 + i * 3.35
    add_rect(s, x, 3.35, 2.65, 0.85, WHITE, RGBColor(0xD4, 0xE0, 0xEA))
    add_text(s, text, x + 0.10, 3.55, 2.45, 0.45, size=11.5, bold=True, color=GRAY, align=PP_ALIGN.CENTER)
for x in (3.3, 6.65):
    add_arrow(s, x, 3.67, 0.5, 0.26, RGBColor(0xE9, 0xA2, 0x3B))
add_text(s, "快车道：跳过辩手，直接出裁定", 0.62, 2.62, 5.2, 0.28, size=10.5, color=TEAL)
add_text(s, "辩论道：正反并发扇出，回齐后裁定", 0.62, 4.38, 5.6, 0.28, size=10.5, color=ORANGE)
add_rect(s, 10.3, 1.55, 2.45, 3.85, RGBColor(0xFF, 0xF6, 0xE6), RGBColor(0xF0, 0xD2, 0x9A))
add_text(s, "防自我确认", 10.5, 1.72, 2.0, 0.3, size=12.5, bold=True, color=RGBColor(0x7A, 0x4A, 0x12))
add_bullets(s, [
    "只有辩论道产出或人工核 gt 的裁定，才有快裁资格。",
    "快裁自己的裁定进池，但永不触发快裁。",
    "库外事件允许“无法判定”，不硬判。",
], 10.45, 2.12, 2.15, 3.0, size=9.0, color=RGBColor(0x7A, 0x5A, 0x32), line=1.1, gap=5)
add_text(s, "检索不是“替代思考”，而是把有限的辩论预算留给库外新案。",
         0.6, 6.55, 11.8, 0.3, size=11.5, bold=True, color=TEAL)
add_footer(s, 7)
add_notes(s, "机制 1 解决效率：库内命中走快车道，库外才组织辩论。右侧是防自我确认：快裁自己的裁定不能反过来证明自己。")

# ---------------------------------------------------------------- 08 chain guard
s = new_slide()
add_title(s, "03 / 技术方案", "机制 2：委托链安全（身份、链深、跨进程）")
cols = [
    ("身份闸门", "mTLS + AIC：证书 CN/SAN 写身份", [
        "目标 AIC 必须属于本账号前缀",
        "拦 discovery 召回到的外部团队 Agent",
        "委托链白名单 + 平台审核",
    ]),
    ("链深与环", "MAX_CHAIN_DEPTH = 3", [
        "主控(1) → 内层 Leader(2) → 叶子(3)",
        "扇出不等于链深；失败重试放行",
        "越界 / 成环 → 结构化错误",
    ]),
    ("跨进程链", "DataPart 携带 debate_chain", [
        "出站把链并进 acps_delegate 的 request",
        "入站由 Hook 挑回，不认正文自报",
        "跨机 / 跨进程都不丢链",
    ]),
]
for i, (title, sub, bullets) in enumerate(cols):
    x = 0.6 + i * 4.1
    add_rect(s, x, 1.4, 3.9, 4.45, WHITE, RGBColor(0xD4, 0xE0, 0xEA))
    add_rect(s, x, 1.4, 3.9, 0.09, TEAL if i != 1 else ORANGE, None, radius=False)
    add_text(s, title, x + 0.22, 1.63, 3.4, 0.35, size=16, bold=True, color=NAVY)
    add_text(s, sub, x + 0.22, 2.05, 3.4, 0.3, size=10.5, color=TEAL if i != 1 else ORANGE)
    add_bullets(s, bullets, x + 0.22, 2.55, 3.45, 2.8, size=11.5, line=1.15, gap=9)
add_rect(s, 0.6, 6.15, 12.1, 0.55, RGBColor(0xE8, 0xF1, 0xFB), None)
add_text(s, "把安全做成结构，不做成纪律。", 0.85, 6.28, 11.6, 0.3,
         size=13, bold=True, color=NAVY)
add_footer(s, 8)
add_notes(s, "机制 2 是创新性重点：身份、链深、跨进程链三件事。尤其是 debate_chain 通过 DataPart 跨进程传递，入站不信任正文自报。")

# ---------------------------------------------------------------- 09 termination
s = new_slide()
add_title(s, "03 / 技术方案", "机制 3：五层终止，保证“一定交付”")
steps = [
    ("工具收敛", "本轮可用工具收敛到只剩结论工具"),
    ("强提示", "system 提示：必须现在交结论"),
    ("预算兜底", "专家未回 + 只剩 1–2 步：强制，并标注信息不完整"),
    ("提交后清空", "结论提交后清空工具，只能文本收尾"),
    ("程序兜底", "从专家载荷拼 FinalVerdict，标注“程序兜底生成”"),
]
for i, (title, desc) in enumerate(steps):
    y = 1.38 + i * 0.96
    add_rect(s, 0.6, y, 7.55, 0.82, WHITE, RGBColor(0xD4, 0xE0, 0xEA))
    circle = s.shapes.add_shape(MSO_SHAPE.OVAL, Inches(0.78), Inches(y + 0.16), Inches(0.46), Inches(0.46))
    circle.fill.solid(); circle.fill.fore_color.rgb = TEAL
    circle.line.fill.background()
    add_text(s, str(i + 1), 0.78, y + 0.23, 0.46, 0.22, size=11, bold=True, color=WHITE, align=PP_ALIGN.CENTER)
    add_text(s, title, 1.42, y + 0.12, 1.6, 0.3, size=12.5, bold=True, color=NAVY)
    add_text(s, desc, 3.0, y + 0.12, 5.0, 0.45, size=10.5, color=GRAY, line=1.05)
add_rect(s, 8.55, 1.4, 4.15, 3.75, RGBColor(0xE8, 0xF1, 0xFB), RGBColor(0xC9, 0xDC, 0xEC))
add_text(s, "裁判定向追问", 8.78, 1.58, 3.6, 0.3, size=14, bold=True, color=NAVY)
add_bullets(s, [
    "双方回齐后、裁定前，允许一次追问。",
    "默认关闭；裁判 followup_rounds=1。",
    "追问不透露对方论点。",
    "预算余量不足 2 步，不 offer。",
], 8.78, 2.05, 3.6, 2.2, size=10.5, line=1.1, gap=7)
add_text(s, "追问不是新任务，只补一个事实缺口；\n仍不交 → 回到五层兜底。",
         8.78, 4.45, 3.55, 0.6, size=9.5, color=GRAY, line=1.15)
add_text(s, "目标不是“让模型更自觉”，而是让它在任何情况下都能交出一个可审计的结果。",
         0.6, 6.5, 11.9, 0.3, size=11.5, bold=True, color=TEAL)
add_footer(s, 9)
add_notes(s, "机制 3 是交付确定性：五层从提示到程序兜底。裁判追问是唯一的额外交互，但默认关闭且不泄露对方论点。")

# ---------------------------------------------------------------- 10 evidence + law + trace
s = new_slide()
add_title(s, "03 / 技术方案", "机制 4/5：鉴伪给线索，法律给方向，全程留痕")
cards = [
    ("证据鉴伪", "只给技术线索，不给“真/假”", [
        "图像：EXIF/XMP、ELA、噪声、JPEG 量化表、复制-移动",
        "音频：容器/编码、频谱、底噪、市电、波形域九项",
        "视频：容器七项 + 可零解码帧、时间线自洽",
        "每条线索带 caveat；没跑成的进 not_run",
    ]),
    ("法律建议", "库内法条，逐字引用", [
        "只引 8 部库内法律，逐字摘录",
        "库内没有的领域：applicable=False",
        "输出行动方向，不替用户定性",
        "不推荐具体律师，只给专业领域",
    ]),
    ("可追溯", "每一跳都能回到任务与材料", [
        "aip-tasks.sqlite3 是权威事实源",
        "verdict-*.meta.json 与平台会话握手",
        "tools.trace 生成 JSON / Markdown / HTML",
        "演练场三桶隔离 + 强制揭示",
    ]),
]
for i, (title, sub, bullets) in enumerate(cards):
    x = 0.6 + i * 4.1
    add_rect(s, x, 1.4, 3.9, 4.45, WHITE, RGBColor(0xD4, 0xE0, 0xEA))
    add_rect(s, x, 1.4, 3.9, 0.09, [TEAL, BLUE, ORANGE][i], None, radius=False)
    add_text(s, title, x + 0.22, 1.63, 3.4, 0.35, size=16, bold=True, color=NAVY)
    add_text(s, sub, x + 0.22, 2.03, 3.4, 0.3, size=10.5, color=[TEAL, BLUE, ORANGE][i])
    add_bullets(s, bullets, x + 0.22, 2.5, 3.45, 2.2, size=10.5, line=1.08, gap=7)
    tag = ["判据：suspicion=无法判定 必须列 not_run",
           "边界：库外 → applicable=False",
           "锚点：task_ref + event_seq 可回放"][i]
    add_rect(s, x + 0.22, 5.0, 3.45, 0.48, LIGHT2, None)
    add_text(s, tag, x + 0.32, 5.12, 3.25, 0.25, size=8.5, bold=True, color=BLUE)
add_text(s, "鉴伪的“不知道”要写出来；法律的“不适用”要说出来；溯源的“哪一跳”要指出来。",
         0.6, 6.3, 12.1, 0.3, size=11.5, bold=True, color=TEAL)
add_footer(s, 10)
add_notes(s, "机制 4/5：证据、法律、溯源三件事的共同点是‘不装懂’。鉴伪给线索和 caveat，法律只引库内条文，溯源每一跳可回。")

# ---------------------------------------------------------------- 11 platform
s = new_slide()
add_title(s, "04 / 项目实施", "平台接入：注册、发现、跨机、访问都留下证据")
checks = [
    ("注册", "10 个部署角色 APPROVED/active；法前哨 v1.1.0"),
    ("发现", "叮当按名称返回 primary 主控 + legal.issue_verdict"),
    ("互联", "s2 证据三叶子；debate_chain 跨机；e2e 探针"),
    ("访问", "改名后外部成功 2 次（同 1 个 AIC）；历史 268 inbound / 成功 1"),
]
for i, (tag, desc) in enumerate(checks):
    y = 1.45 + i * 1.05
    add_text(s, "✓", 0.65, y, 0.35, 0.35, size=15, bold=True, color=TEAL)
    add_text(s, tag, 1.1, y, 0.8, 0.3, size=13, bold=True, color=NAVY)
    add_text(s, desc, 1.95, y, 4.3, 0.5, size=10.5, color=GRAY, line=1.1)
add_image_fit(s, AICOMP / "平台验证截图-2026-10-21" / "平台验证-拼图.png",
              6.55, 1.38, 6.2, 2.75)
add_text(s, "图 2 · 平台详情（新 AIC）与叮当按名称发现", 6.65, 4.18, 6.0, 0.2,
         size=8.5, color=MID)
add_rect(s, 6.55, 4.55, 6.2, 1.55, RGBColor(0xFF, 0xF6, 0xE6), RGBColor(0xF0, 0xD2, 0x9A))
add_text(s, "平台侧注意", 6.75, 4.68, 1.4, 0.28, size=10.5, bold=True, color=RGBColor(0x7A, 0x4A, 0x12))
add_text(s, "“输入 AIC 查询在线状态”会假阴性：Discovery 是语义检索，不是 AIC 精确目录；查 AIC 用 registry / tools.list_agents。改名后 Agent 程序化 acps_discover 的 ADP 索引仍有滞后——本文不写成“已解决”。",
         6.75, 4.98, 5.8, 1.0, size=8.8, color=RGBColor(0x7A, 0x5A, 0x32), line=1.05)
add_text(s, "AIC 1.2.156.3088.1.BUPT.81M3FX.6CKYE5.0ZV54U.0IPO", 0.65, 6.3, 5.5, 0.25,
         size=8.2, color=MID)
add_footer(s, 11)
add_notes(s, "平台四件事都有证据：注册、发现、跨机、访问。注意底部这条 caveat：程序化 discovery 索引还在滞后，我们不把它写成已解决。")

# ---------------------------------------------------------------- 12 external call
s = new_slide()
add_title(s, "05 / 应用成效", "真实外部调用：不是自测，是外部账号打进来的")
add_image_fit(s, AICOMP / "外部调用-2026-10-21" / "01-法前哨-业主群回扣-结论.png",
              0.45, 1.25, 4.1, 5.4)
add_text(s, "图 3 · 返回原文（节选）", 0.55, 6.7, 4.0, 0.2, size=8.2, color=MID)
add_text(s, "外部 AIC · …EZTK03.BS0XTB…（不同账号前缀）", 5.1, 1.4, 7.5, 0.3,
         size=11, bold=True, color=TEAL)
add_bullets(s, [
    "链路：外部 AIC → 叮当 → 法前哨 → 产品回传。",
    "task_96fd0329ecbb（10-21）+ task_917b67602160（10-23），均 completed。",
    "结论：无法判定 + 谁主张谁举证；两次来自同一外部 AIC。",
    "另有 2 条 canceled（含一次空正文重试）；6 条内部自测不计外部。",
], 5.1, 1.9, 7.6, 2.3, size=12, line=1.15, gap=8)
add_rect(s, 5.1, 4.35, 7.55, 1.75, RGBColor(0xE8, 0xF1, 0xFB), None)
add_text(s, "“谁主张谁举证……要求你‘自证清白’，属于举证倒置。”",
         5.35, 4.55, 7.1, 0.55, size=17, bold=True, color=NAVY, line=1.1)
add_text(s, "这就是“事前准备”，而不是“法律定性”。", 5.35, 5.35, 7.0, 0.3,
         size=11.5, color=GRAY)
add_footer(s, 12)
add_notes(s, "这两条都是真实外部 AIC 打进来的，不是我们自己刷的；两次来自同一外部 AIC。结论恰好展示了产品价值：证据不足时敢说无法判定，并把举证责任讲清楚。")

# ---------------------------------------------------------------- 13 metrics
s = new_slide()
add_title(s, "05 / 应用成效", "应用成效：有数字，也有边界")
kpis = [
    ("12 / 10 / 2", "角色 / 部署 / 软废弃", "端口 8443–8454"),
    ("23 轮", "真实案例轮", "最新 10-23 四检全绿"),
    ("12 / 12", "ACS 指纹未漂移", "check_local 21 节通过"),
    ("2 次", "改名后成功外部调用", "同 1 个外部 AIC"),
]
for i, (num, label, sub) in enumerate(kpis):
    x = 0.6 + i * 3.1
    add_rect(s, x, 1.4, 2.85, 1.75, WHITE, RGBColor(0xD4, 0xE0, 0xEA))
    add_text(s, num, x + 0.18, 1.55, 2.5, 0.6, size=32, bold=True, color=TEAL if i != 3 else ORANGE)
    add_text(s, label, x + 0.18, 2.18, 2.5, 0.3, size=10.5, bold=True, color=NAVY)
    add_text(s, sub, x + 0.18, 2.52, 2.5, 0.3, size=9.2, color=MID)
add_rect(s, 0.6, 3.55, 5.9, 1.55, RGBColor(0xE8, 0xF1, 0xFB), None)
add_text(s, "历史快照（旧 AIC）", 0.82, 3.7, 4.5, 0.25, size=9, bold=True, color=BLUE)
add_text(s, "268 inbound / 外部 4 / 成功 1", 0.82, 4.0, 5.2, 0.35, size=16, bold=True, color=NAVY)
add_text(s, "不和新 AIC 的 2 次相加；两次成功仍是同一外部 AIC。", 0.82, 4.45, 5.4, 0.25, size=8.5, color=GRAY)
add_rect(s, 6.8, 3.55, 5.9, 1.55, RGBColor(0xFF, 0xF6, 0xE6), RGBColor(0xF0, 0xD2, 0x9A))
add_text(s, "评测口径", 7.02, 3.7, 4.5, 0.25, size=9, bold=True, color=RGBColor(0x7A, 0x4A, 0x12))
add_text(s, "21/22（95.45%）", 7.02, 4.0, 5.2, 0.35, size=16, bold=True, color=RGBColor(0x7A, 0x4A, 0x12))
add_text(s, "22 条同源基准，不是泛化准确率；无“断章取义/无法判定”基准。", 7.02, 4.45, 5.2, 0.35, size=8.8, color=RGBColor(0x7A, 0x5A, 0x32), line=1.05)
add_text(s, "把“历史快照 / 改名后新增 / 内部自测”三本账分开，是这份材料最重要的诚实。",
         0.6, 5.65, 12.1, 0.35, size=12, bold=True, color=TEAL)
add_text(s, "所有数字都有任务库 / 截图 / 命令输出对应；没有证据的数字不写。",
         0.6, 6.15, 12.1, 0.3, size=10, color=MID)
add_footer(s, 13)
add_notes(s, "数字只报有证据的：12/10/2 角色、23 轮、12/12 指纹、2 次外部成功（同 1 个外部 AIC）。95.45% 必须说明是同源基准，不是泛化准确率。")

# ---------------------------------------------------------------- 14 summary
s = new_slide()
add_title(s, "06 / 总结与展望", "复盘：四点经验 + 四条标准化建议")
lessons = [
    ("先定契约，再写代码", "每个 Agent 的交付物先钉死，Leader 不解析自然语言。"),
    ("不要相信模型自觉", "终止、追问、工具选择、鉴伪结论都要结构约束。"),
    ("判据要能证伪", "completed 不算成功；output 新文件才算；not_run 必须显式。"),
    ("平台适配是硬功夫", "Discovery filter、并发登录、软删除、AIC 轮换，都是真坑。"),
]
for i, (title, desc) in enumerate(lessons):
    x = 0.6 + (i % 2) * 3.2
    y = 1.4 + (i // 2) * 1.65
    add_rect(s, x, y, 3.0, 1.42, WHITE, RGBColor(0xD4, 0xE0, 0xEA))
    add_text(s, title, x + 0.15, y + 0.14, 2.7, 0.3, size=12, bold=True, color=NAVY)
    add_text(s, desc, x + 0.15, y + 0.52, 2.7, 0.75, size=9.5, color=GRAY, line=1.1)
add_rect(s, 7.0, 1.4, 5.75, 3.95, RGBColor(0xE8, 0xF1, 0xFB), RGBColor(0xC9, 0xDC, 0xEC))
add_text(s, "标准化建议", 7.22, 1.58, 5.2, 0.3, size=14, bold=True, color=NAVY)
add_bullets(s, [
    "AIP/AAC 的多级委派需要显式链深约束；本项目在应用层补 MAX_CHAIN_DEPTH=3。",
    "Discovery 需要 AIC 精确目录；语义检索不能替代身份查找。",
    "软删除 / 可见性语义应在发现索引中一致传播。",
    "AMP 的 Access / Message 口径应在控制台写清楚。",
], 7.22, 2.05, 5.25, 2.5, size=10.3, line=1.1, gap=7)
add_text(s, "把踩过的坑，变成下一支队伍\n不用再踩的适配层。", 7.22, 4.55, 5.2, 0.6,
         size=11, bold=True, color=BLUE, line=1.15)
add_text(s, "未来：20 条 holdout / 鉴伪 ground truth / MCP 能力开放 / 角色收敛赛后做。",
         0.6, 5.85, 12.1, 0.35, size=11.5, bold=True, color=TEAL)
add_footer(s, 14)
add_notes(s, "最后复盘四点经验，并把标准化建议落在协议/平台层面。未来工作只列能验证的，不画大饼。")

# ---------------------------------------------------------------- 15 Q&A backup: mobile / hidden cards
s = new_slide()
add_title(s, "Q&A 备用 · 产品化", "手机端：把他“有什么”藏起来")
add_image_fit(s, ROOT / "docs" / "product" / "界面设计" / "m3-screen.png",
              0.55, 1.22, 2.7, 5.55)
add_text(s, "手机端 · 对方打出一张牌", 0.65, 6.82, 2.7, 0.2, size=8, color=MID)
add_text(s, "本页不讲 · Q&A 备用", 3.95, 1.28, 2.6, 0.25, size=9, bold=True, color=ORANGE)
add_bullets(s, [
    "主入口是手机 PWA，不是桌面 exe：消息、截图、录音都在手机上。",
    "对方只有一个牌背 + “数量未知”；声称是虚线标签，不是牌。",
    "只有“已打出”的牌才进入核验；复盘才给实际手牌。",
    "桌面只做深读、整理、导出；手机负责“发生的那一刻”。",
], 3.95, 1.75, 8.85, 2.8, size=14, line=1.2, gap=10)
add_rect(s, 3.95, 4.75, 8.8, 1.15, RGBColor(0xFF, 0xF6, 0xE6), RGBColor(0xF0, 0xD2, 0x9A))
add_text(s, "“他用了哪一张，哪一张才显示；否则用户就成上帝了。”",
         4.15, 4.95, 8.35, 0.4, size=16, bold=True, color=RGBColor(0x7A, 0x4A, 0x12))
add_text(s, "产品原则：声称 ≠ 手牌；手牌 ≠ 已打出；已打出 ≠ 真实；真实 ≠ 能证明。",
         3.95, 6.25, 8.8, 0.3, size=11, color=TEAL)
add_footer(s, 15)
add_notes(s, "备用页，只在评委问产品形态时讲：手机 PWA 优先，对方手牌是隐藏信息，只有打出的牌才核验；桌面退到深读/导出。")

# ---------------------------------------------------------------- 16 closing
s = new_slide()
add_rect(s, 0, 0, 13.333, 7.5, NAVY, None, radius=False)
add_rect(s, 0.65, 1.15, 0.08, 3.1, TEAL, None, radius=False)
add_text(s, "法前哨", 0.95, 1.0, 8.5, 0.9, size=54, bold=True, color=WHITE)
add_text(s, "把事实、证据、话术，\n准备在成为当事人之前。", 0.98, 2.15, 9.0, 1.4,
         size=24, bold=True, color=RGBColor(0xD7, 0xE6, 0xF3), line=1.3)
add_text(s, "技术方案 · 2026-10-21 · 梧桐智能体互联平台", 0.98, 4.2, 8.0, 0.3,
         size=11, color=RGBColor(0xA9, 0xC3, 0xD8))
add_text(s, "谢谢", 0.98, 5.6, 3.0, 0.5, size=18, bold=True, color=TEAL)
add_text(s, "12 角色 · 10 部署 · 两级嵌套 · 全链留痕", 9.0, 6.55, 3.8, 0.3,
         size=9, color=RGBColor(0xA9, 0xC3, 0xD8), align=PP_ALIGN.RIGHT)
add_notes(s, "收尾：法前哨不是替代律师，而是让用户在成为当事人之前，把事实、证据、话术准备好。谢谢。")

prs.save(OUT)
print("saved", OUT, "slides", len(prs.slides))
