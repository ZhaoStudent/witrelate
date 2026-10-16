"""元验证：`tools/verify_video.py` 与 `tools/verify_video_mcp.py` 的断言**不是空转的**。

做法与前几个元验证一致：把每处关键实现**退回旧形态**，要求对应断言立刻报失败。
判据只有一条：**退回后仍然语法正确**（否则证明不了任何事）。

用法（在 witrelate 目录下）：
    ./.venv/Scripts/python.exe -m run.meta_verify_video

════════════════════════════════════════════════════════════════════════
本文件同时覆盖**两层**，以及为什么可以把两层放进一份元验证
════════════════════════════════════════════════════════════════════════

视频这条链有两层判据，缺陷形态完全不同：

    `tools/verify_video.py`      检测层 —— 缺陷会**改变报告**（能力整项死掉、
                                 报告照样出得来，只是每份都变成「无法判定」）
    `tools/verify_video_mcp.py`  MCP 层  —— 缺陷**不改变报告**，它改变"能不能用"
                                 （通知回了应答、工具表与实现不一致、出口各写一份）

音频侧这两层是两份元验证（`meta_verify_audio.py` / `meta_verify_audio_mcp.py`），
那是它们先后写就的历史结果。这里**刻意合成一份**，理由有二：

  1. 两层的锚点合在一处，才能让"检测层退回旧形态"与"MCP 层退回旧形态"
     在同一份报告里被对照着看 —— 而它们的**受害面**恰好相反：前者让结论变假，
     后者让服务用不了。分开跑容易只注意到其中一类。
  2. 每个用例都带 `target` 字段，**只重跑它自己那一层** ——
     于是合成一份不会让运行时间翻倍（`verify_video` 约 10 秒、
     `verify_video_mcp` 约 20 秒）。

════════════════════════════════════════════════════════════════════════
A 组（MCP 层）—— **处数不写在这里**（下面 `CASES` 里逐条列着；
  数字一旦写死就会与 `len(CASES)` 分家，而跑步时打印的是**派生**的那个）
════════════════════════════════════════════════════════════════════════

  A1. `handle_message` 退回「不接受族参数、写死音频族」
      ⇒ 视频入口的每一条报文都调不动（`family` 是 keyword-only，传了就 TypeError）。
      这一条是**"两个入口共用同一份协议实现"这句话的唯一动态证据**：
      退回之后视频侧立刻报失败，就说明它真的走的是同一份代码，
      而不是作者口头上说"没复制粘贴"。

  A2. `tools/list` 写死音频 `TOOLS`
      ⇒ 视频入口列出的是音频那三个工具。客户端调它，得到"没有这个工具"。

  A3. `tools/call` 不看族、写死音频模块
      ⇒ 与 A2 同形，只是发生在调用侧：列表对了、调用仍打到音频工具上。

  A4. `-h` 的模块名写死音频入口
      ⇒ 用户照着一句**看起来完全正常**的提示去启动另一个服务。
      （这是"假话"而非"崩溃"，所以只能靠断言抓。）

  A5. `_HANDLERS` 少一项（`TOOLS` 里还有它）
      ⇒ `tools/list` 会把它列出去，`tools/call` 调它却失败。

  A6. `_video_module()` 的导出面少一个符号（静默变 `None`）
      ⇒ 不在导入期出声，要到调用那一刻才炸。

  A7. `video_probe` 的字段名改回 `frames_available`
      ⇒ 一份**动画 GIF** 会被读成"帧层跑不了"，而它其实跑得了（帧要光栅化）。
      这正是"判据的说法与出口文案不对应"的老毛病。

  A8. 非视频字节不再拒绝
      ⇒ 一段 PDF 的字节会被"照常出结果"。⚠️ 这一条只对
      `video_probe` / `video_sample_table` 有效（`analyze` 走的是 `outcome()`，
      它自带拒绝）—— 所以 `verify_video_mcp.py` 里那两条**补出来的**断言
      是这条用例的前提。

  A9. `INSTRUCTIONS` 不提「帧层整项没有执行」是常态
      ⇒ 模型读到的是"七项检测"，于是把「容器层没发现问题」转述成"没问题"。

  A10. `video_uri` 的描述不提「不出网」
       ⇒ 调用方会以为可以传 URL（而被拒绝时的补救成本远高于一开始就说清）。

  A11. 未知工具名改为**抛异常**
       ⇒ 调用方拿到的是一句不透明的失败，而不是"有哪些工具可用"。

  A12. 视频族挂的是**音频**模块
       ⇒ 两个入口混成一个：视频客户端看到六个不存在的工具。

  ⋯⋯ 以下三条是 2026-10-04 体检查出的缺陷（补齐的旧形态）：

  A13. 工具层的把关退回**手抄的前缀测试**
       ⇒ `application/ogg` 被自家三个工具拒了，理由是一句假话
       「不是可识别的视频容器」。同族的 `video/mp2t` 只因为字符串带
       `video/` 前缀就通行无阻 —— **差别只在字符串前缀**。
       ⚠️ A8 退的是「有没有把关」，这一条退的是「关是怎么把的」：一处实现
       两种缺陷，必须拆两条，否则退回其中一种时另一条没人管。

  A14. `read_message` 退回 `errors="replace"`
       ⇒ 报文里一个 `0xFF` 被静默替换成 `U+FFFD`，`json.loads` **照样成功**，
       工具收到一个**被改过的**参数（`inbox:<sha256>` 句柄、或一段案情说明），
       而调用方永远不知道自己的字节被动了。**这是"静默改数据"而非崩溃，
       只能靠断言抓。**

  A15. `_non_video_result` 退回**手抄一句原因**
       ⇒ 同一句话又有两个来源。⚠️ 退回去的那句**与检测层一字不差** ——
       所以「子句是否同时出现在两条路上」那条断言**照样绿**，
       真正抓住它的是本轮新加的**行为化单一来源证明**（把检测层那句话
       换成哨兵，MCP 的出口必须跟着变）。这条用例的存在，本身就是
       「一致 ≠ 单一来源」这句话的证据。

════════════════════════════════════════════════════════════════════════
B 组（检测层）—— 五处旧形态，**把 3b 节里只"写了描述"的那些缺陷变成真判据**
════════════════════════════════════════════════════════════════════════

⚠️ `tools/verify_video.py` 第 3b 节当时写的是：

> 这一节的五条**全都是**「能力整项死掉、而报告照样出得来」的形态 …
> 所以每一条都必须能回答同一个问题：**退回旧写法，这条断言会红吗？**

而它当时**只是把答案写在了注释里**（"退回旧形态 ⇒ 0 帧"之类），
**没有真的回退过一次**。也就是说那五条在写下时的身份是**说明**，不是**判据** ——
"修好了 ≠ 有判据"，而"描述了这个回退的后果"与"验证过这个回退"之间的差别，
恰好就是本项目最不放心的地方。B 组把那五条补成真判据：

  B1. `_children` 只给 `meta` 开 FullBox 特例（`ilst` 读不到子盒）
      ⇒ 内嵌封面帧 0 张、`©too` 编码工具读不到、而 `check_metadata`
      会给出一句"没命中常见剪辑软件"的**看起来正常的正面结论**。

  B2. `stsz` 当成非 FullBox 读（`sample_count` 落在 `body+4` = `sample_size`）
      ⇒ `sample_count` 变成 0 ⇒ MJPEG 的 MP4 **一帧都取不到**。

  B3. 4CC 判据收紧成"全 ASCII"
      ⇒ 真实 MP4 里的 `©too` / `©nam` 一整个家族（首字节 0xA9）让解析提前收工，
      它后面的全部字节被算成"尾部多余数据" ⇒ 对一份**完全正常**的 iPhone
      视频报「容器声明范围之外还有 N 字节额外数据」。**假阳性比漏报更伤。**

  B4. 「取到帧但一帧都没能送进图像链」时 `reason` 落回空串
      ⇒ 报告里那条「无法判定」的 `detail` 是**空串**、`frames_note` 也是空串 ⇒
      只剩一个孤零零的等级，**一个字的原因都没有**。

  B5. GIF 光栅化失败却沿用 MP4 那句原因
      ⇒ 调用方照着"这个容器不支持取帧"去换文件，而真正该做的是装 Pillow
      或换一份能打开的 GIF。**溯源字段不许含糊。**

════════════════════════════════════════════════════════════════════════
第 3c 组（检测层，2026-10-04 体检）—— 「谁算视频」曾经写在两个地方
════════════════════════════════════════════════════════════════════════

⚠️ 这一组的共同点是：**能力整项死掉，而报告照样出得来 / 工具照样返回**。
退回之后没有任何崩溃、没有任何异常，只有一句**看起来完全正常的假话**。

  B6. `forensics.analyze()` 的把关退回前缀测试
      ⇒ Ogg 在**检测层**就被拒（报告根本出不来）。
      ⚠️ 与 A13 是同一件缺陷的**两个位置**（检测层一份判据、工具层一份判据），
      所以两条都必须有 —— 只退一处时另一层照样能服务 Ogg，
      那恰好是"两处判据只改一处"的活样本。

  B7. `is_supported_video()` 不再读解码层那张容器表
      ⇒ 「认得」（解码层有中文名）与「收不收」（判据）再次分家。
      本轮修复的核心就是让判据**现场读那张表** ⇒ 以后加容器只改解码层一处。

  B8. 别名表少了 Ogg 那一族
      ⇒ 一份**声明完全正确**的 `.ogv`（标准 MIME 就是 `video/ogg`）立刻吃到
      一条假线索「声明与真实容器不一致」，综合等级从「无法判定」被推成「低」。
      ⚠️ 这条是**放开 Ogg 之后才暴露**的（"连通面"）：进门之前它走不到
      `check_container`，所以"别名表里没有 Ogg"一直没人发现 ——
      **修一个缺陷会露出下一个**，这正是本轮必须先跑判据再改代码的理由。

════════════════════════════════════════════════════════════════════════
跑完之后
════════════════════════════════════════════════════════════════════════

⚠️ 以上只证明「在跑的时候还原过」。**真正的判据是跑完之后**：

  ① `python -m tools.tree_diff save` 在跑之前记全树指纹
  ② `python -m tools.tree_diff diff` 在跑完之后比对（**不 diff 等于没跑过**）
  ③ `python -m run.check_meta_residue` 核对每处锚点都在新形态上
     （本模块已在它的 `SOURCES` 里登记 —— 不登记的话"没人核"与"核过了"外表一样）

⚠️ 必须**串行**跑：它会在运行期间修改 6 个源文件
（`MCP_SERVER` / `MCP_VIDEO_FORENSICS` / `MCP_VIDEO_SERVER` /
`VIDEO_DECODE` / `VIDEO_FRAMES` / `VIDEO_FORENSICS` —— 以 `CASES` 里出现过的
`file` 为准，**这一句不许当成数字来源**），与其它元验证并发会互相踩。
"""

from __future__ import annotations

import contextlib
import hashlib
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MCP_SERVER = ROOT / "legal_acps" / "mcp" / "server.py"
MCP_VIDEO_FORENSICS = ROOT / "legal_acps" / "mcp" / "video_forensics.py"
MCP_VIDEO_SERVER = ROOT / "legal_acps" / "mcp" / "video_server.py"
VIDEO_DECODE = ROOT / "legal_acps" / "video" / "decode.py"
VIDEO_FRAMES = ROOT / "legal_acps" / "video" / "frames.py"
VIDEO_FORENSICS = ROOT / "legal_acps" / "video" / "forensics.py"
VIDEO_PIPELINE = ROOT / "legal_acps" / "video" / "pipeline.py"

#: 目标 → 模块。-m 名，别写路径（与项目"一律模块方式跑"一致）。
TARGETS = {
    "verify_video": "tools.verify_video",
    "verify_video_mcp": "tools.verify_video_mcp",
}

#: 每个目标里"这一条没通过"的标记。
#: ⚠️ 两份 verify 用的都是 `[未达预期]`；但仍然**按目标分别写** ——
#: 统一判一个词会在将来某一份改了标记时让步骤 0 / 步骤 4 的绿判定失效
#: （前一个元验证踩过：`[未达预期]` 与 `[失败]` 被混成一个）。
FAIL_MARKERS = {
    "verify_video": ("[未达预期]",),
    "verify_video_mcp": ("[未达预期]",),
}

CASES: list[dict[str, object]] = [
    # -----------------------------------------------------------------------
    # A 组：MCP 层
    # -----------------------------------------------------------------------
    {
        "name": "A1 协议层退回「不接受族参数、写死音频族」（⇒ 视频入口一条报文都调不动）",
        "target": "verify_video_mcp",
        "file": MCP_SERVER,
        "new": "def handle_message(message: Any, *, family: ToolFamily = AUDIO_FAMILY) -> dict[str, Any] | None:\n",
        "old": ("def handle_message(message: Any) -> dict[str, Any] | None:"
                "  # 退回旧形态：不接受族参数\n"
                "    family = AUDIO_FAMILY  # 退回旧形态：写死成音频族\n"),
        # ⚠️ 这一条是"两个入口共用同一份协议实现"的**唯一动态证据**。
        "needle": "视频 `tools/list` 吐出的就是视频",
    },
    {
        "name": "A2 `tools/list` 写死音频 `TOOLS`（⇒ 视频客户端看到的是音频那三个工具）",
        "target": "verify_video_mcp",
        "file": MCP_SERVER,
        "new": '        reply = _result(msg_id, {"tools": list(family.module.TOOLS)})\n',
        "old": ('        reply = _result(msg_id, {"tools": list(audio_forensics.TOOLS)})'
                "  # 退回旧形态：写死音频族\n"),
        "needle": "视频 `tools/list` 吐出的就是视频",
    },
    {
        "name": "A3 `tools/call` 不看族、写死音频模块（⇒ 列表对了、调用仍打到音频工具上）",
        "target": "verify_video_mcp",
        "file": MCP_SERVER,
        "new": ('            reply = _result(msg_id, family.module.call_tool(\n'
                '                params["name"], params.get("arguments")))\n'),
        "old": ('            reply = _result(msg_id, audio_forensics.call_tool(\n'
                '                params["name"], params.get("arguments")))'
                "  # 退回旧形态：不看族\n"),
        "needle": "**成功侧：MCP 的 JSON 与 ACPs 的 JSON 逐字段相等**",
    },
    {
        # ⚠️ 2026-10-05 锚点已跟着实现搬家（体检项 B5 把这一行改成了
        #    `return family.entry`）：**锚点不搬家的话，元验证会对着一份已经
        #    不存在的文本报「前置不成立」—— 那不是"抓到了回归"，是判据自己失效了。**
        #    它与 A17 **共用同一个 `new`**（同一行的两种退化）：
        #      A4  = 写死成音频入口 ⇒ **视频**的 `-h` 指错；
        #      A17 = 按"是不是音频族"二选一 ⇒ **第三族**的 `-h` 指错。
        #    两者退回的是同一行、不同的错法 ⇒ 必须各有一条（"一处实现两种缺陷"）。
        "name": "A4 `-h` 的模块名写死音频入口（⇒ 用户照着一句看起来正常的假话去启动别的服务）",
        "target": "verify_video_mcp",
        "file": MCP_SERVER,
        "new": "    return family.entry\n",
        "old": ('    return "legal_acps.mcp.server"'
                "  # 退回旧形态：不看族、一律回显音频那个入口\n"),
        "needle": "`-h` 里回显的启动模块是**视频**那个",
    },
    {
        "name": "A5 `_HANDLERS` 少一项（⇒ 列表里有、调不到）",
        "target": "verify_video_mcp",
        "file": MCP_VIDEO_FORENSICS,
        "new": '    "video_sample_table": _handle_sample_table,\n',
        "old": '    # "video_sample_table": _handle_sample_table,  # 退回旧形态：列表里有、调不到\n',
        "needle": "`TOOLS` 与处理器表**一一对应**",
    },
    {
        "name": "A6 `_video_module()` 导出面少一个符号、静默变 None（⇒ 到调用那一刻才炸）",
        "target": "verify_video_mcp",
        "file": MCP_VIDEO_FORENSICS,
        "new": "        sha256_of=_forensics.sha256_of,\n",
        "old": "        sha256_of=None,  # 退回旧形态：导出面少一个符号时静默变 None\n",
        "needle": "`_video_module()` 显式给出**列明依赖面**的命名空间",
    },
    {
        "name": "A7 `video_probe` 的字段名改回 `frames_available`"
                "（⇒ 动画 GIF 被读成「帧层跑不了」，而它其实跑得了）",
        "target": "verify_video_mcp",
        "file": MCP_VIDEO_FORENSICS,
        "new": "        \"container_frames\": len(decoded.frames),\n",
        "old": ("        \"frames_available\": len(decoded.frames),"
                "  # 退回旧形态：名字含糊（GIF 会被读成帧层跑不了）\n"),
        "needle": "**没有** `frames_available`",
    },
    {
        "name": "A8 非视频字节不再拒绝（⇒ 一段 PDF 的字节被「照常出结果」）",
        "target": "verify_video_mcp",
        "file": MCP_VIDEO_FORENSICS,
        # ⚠️ 2026-10-04 这一处改过一次形态：判据从**手抄的前缀测试**换成了
        #    `core.is_supported_video()`（单一来源）。A8 只负责「有没有把关」这一半，
        #    「把的关是不是代理判据」那一半由 A13 负责 —— 一处实现两种缺陷，
        #    必须拆成两条，否则退回其中一种时另一条判据的事没人管。
        "new": ('    media_type = str(decoded.media_type)\n'
                '    if core.is_supported_video(media_type):\n'
                '        return None\n'),
        "old": ('    return None  # 退回旧形态：非视频字节也照跑\n'
                '    media_type = ""  # （下面这段变成死代码）\n'),
        "needle": "也不许对非视频字节照跑",
    },
    {
        "name": "A9 `INSTRUCTIONS` 不提「帧层整项没有执行」是常态"
                "（⇒ 模型把「容器层没发现问题」转述成「没问题」）",
        "target": "verify_video_mcp",
        "file": MCP_VIDEO_FORENSICS,
        "new": '    "H.264 / H.265 一律解不出帧 ⇒「**帧层整项没有执行**」是常态而非例外，"\n',
        "old": '    "H.264 / H.265 一律解不出帧。"  # 退回旧形态：不提「帧层整项没跑」是常态\n',
        "needle": "点明「帧层整项没有执行」是**常态**",
    },
    {
        "name": "A10 `video_uri` 的描述不提「不出网」（⇒ 调用方以为可以传 URL）",
        "target": "verify_video_mcp",
        "file": MCP_VIDEO_FORENSICS,
        "new": '    "⚠️ **本服务不出网、也不读本机文件**：https 地址、file://、裸路径一律被拒绝"\n',
        "old": '    "⚠️ https 地址、file://、裸路径一律被拒绝"  # 退回旧形态：不提「不出网」\n',
        "needle": "描述都写明「不出网、不读本机文件」",
    },
    {
        "name": "A11 未知工具名改为抛异常（⇒ 调用方拿不到「有哪些工具可用」）",
        "target": "verify_video_mcp",
        "file": MCP_VIDEO_FORENSICS,
        "new": "    handler = _HANDLERS.get(name)\n    if handler is None:\n",
        # ⚠️ 退回形态**必须不含新形态**（`new in broken` 即判失败）。初版写成
        #    "原两行 + 一行 raise"，而原两行恰恰**就是** `new` ⇒
        #    [11.1] 报「替换未按预期发生」，看起来像锚点写错，
        #    实际是"旧形态包含了新形态"。所以第一行也要跟着变（加注释即可）。
        "old": ("    handler = _HANDLERS.get(name)"
                "  # 退回旧形态：未知工具名抛异常\n"
                "    if handler is None:\n"
                "        raise ValueError(f\"没有名为 {name} 的工具\")\n"),
        "needle": "未知工具名会列出**视频**那三个可用工具",
    },
    {
        "name": "A12 视频族挂的是**音频**模块（⇒ 两个入口混成一个）",
        "target": "verify_video_mcp",
        "file": MCP_VIDEO_SERVER,
        "new": "    module=video_forensics,\n",
        "old": "    module=_server.AUDIO_FAMILY.module,  # 退回旧形态：视频入口挂音频那三个工具\n",
        "needle": "视频 `tools/list` 吐出的就是视频",
    },
    {
        "name": "A13 把关的判据退回**手抄的前缀测试**"
                "（⇒ `application/ogg` 被自家三个工具拒了，理由还是假话）",
        "target": "verify_video_mcp",
        "file": MCP_VIDEO_FORENSICS,
        # ⚠️ 这一条与 A8 **互补**：A8 退的是「有没有把关」，本条退的是「关是怎么把的」。
        #    退回形态是**修复前的那两行原文**（`startswith("video/")`），
        #    而它恰好只让 `application/ogg` 这一族翻车 —— 解码层认得它
        #    （`_OTHER_CONTAINERS` 里有中文名、`sniff_media_type` 有专门分支），
        #    字符串却不带 `video/` 前缀。同族的 `video/mp2t` 照样通行 ⇒
        #    「差别只在字符串前缀」。
        #    ⚠️ `verify_video_mcp` 第 4 节那组「三个工具都要服务它」是这条的前提
        #    （它按 `_OTHER_CONTAINERS` 现场派生，以后加容器不许漏）。
        "new": '    if core.is_supported_video(media_type):\n        return None\n',
        "old": ('    if media_type.startswith("video/") or media_type == "image/gif":'
                "  # 退回旧形态：手抄的前缀测试\n"
                "        return None\n"),
        "needle": "`application/ogg`（认得但没解析器）· 三个工具**都要服务它**",
    },
    {
        "name": "A14 `read_message` 退回 `errors=\"replace\"`"
                "（⇒ 报文里一个 0xFF 被静默替换，工具收到**被改过的**参数）",
        "target": "verify_video_mcp",
        "file": MCP_SERVER,
        # ⚠️ 这条缺陷的形态是「静默改数据」，不是崩溃：`json.loads` 照样解析成功，
        #    调用方永远不知道自己的字节被动了。所以只能靠断言抓。
        # ⚠️ 退回形态**不含新形态**：新形态是 try/except + 一句报错，旧形态是一行
        #    `replace` 解码 —— 两者无公共子串。
        "new": ('        try:\n'
                '            text = line.decode("utf-8").strip()\n'
                '        except UnicodeDecodeError as exc:\n'
                '            # 与"不是 JSON"分开报：前者是**编码/传输**问题，后者是**内容**问题，\n'
                '            # 调用方要改的地方不一样。\n'
                '            return None, f"报文不是合法 UTF-8（传输层违规）：{exc}"\n'),
        "old": ('        text = line.decode("utf-8", errors="replace").strip()'
                '  # 退回旧形态：静默替换非法字节\n'),
        "needle": "报文含非法 UTF-8 ⇒ **报错**",
    },
    {
        "name": "A15 `_non_video_result` 退回**手抄一句原因**"
                "（⇒ 同一句话又有两个来源；改一处另一处不动）",
        "target": "verify_video_mcp",
        "file": MCP_VIDEO_FORENSICS,
        # ⚠️ 这一条的判据是**那一刻**新加的「行为化单一来源证明」（把检测层那句话
        #    换成哨兵，MCP 必须跟着变）。**不能**用「子句是否同时出现」那条当
        #    needle —— 手抄一份、抄得一字不差时它照样绿，那正是修复前的实况。
        #    所以这里的 `old` 抄的就是**修复前那句原文**（一字不差），
        #    它恰好构成对新增那条判据的真检验。
        "new": '    reason = core.non_video_clause(media_type)\n',
        "old": ('    reason = f"不是可识别的视频容器（按魔术字节判定为 {media_type}）"'
                "  # 退回旧形态：手抄一份\n"),
        "needle": "**行为化**的单一来源证明",
    },
    {
        # A16 与 A15 是**同一处修复的两个使用者**：A15 是"那句话"，A16 是
        # 「把字节族失败路由到「你给的不是视频」那条建议」的**关键词**。
        # 它原先也是手写字面量；坏了**不报错**，只是静默降级成兜底那句
        # 「请确认提交的是完整、未截断的原始视频字节」—— 调用方照着错误的归因去改。
        # ⚠️ 因此必须**另开一条**用例（一处实现两种缺陷 ⇒ 两条退回用例）。
        "name": "A16 路由关键词退回手写字面量"
                "（⇒ 改句子会静默改丢路由，而正文看起来完全正常）",
        "target": "verify_video_mcp",
        "file": VIDEO_PIPELINE,
        "new": "    return _forensics.NON_VIDEO_CLAUSE_PREFIX\n",
        "old": "    return \"不是可识别的视频容器\"  # 退回旧形态：手写字面量\n",
        # ⚠️ 必须是「证明②」那条（换**共用前缀**、要求原因与出路同时跟着变）；
        #    「证明①」换的是函数本身，命中的是 A15 那处，抓不到这里。
        "needle": "单一来源证明②",
    },
    {
        # 体检项 B5（2026-10-05）：入口名由 `_module_of()` 按"是不是音频族"二选一
        # 得出来 ⇒ 只有两个族时看着也对，**第三个族会拿到视频那个入口**。
        # ⚠️ 判据必须**造一个真的第三族**去问它，而不是断言现有两族的值 ——
        #    后者只是两张快照，"按族猜"这个缺陷本身**一条都碰不到**。
        "name": "A17 `-h` 的启动入口退回「按是不是音频族二选一」（⇒ 第三族指错路）",
        "target": "verify_video_mcp",
        "file": MCP_SERVER,
        "new": "    return family.entry\n",
        "old": ('    return ("legal_acps.mcp.server"\n'
                '            if family is AUDIO_FAMILY\n'
                '            else "legal_acps.mcp.video_server")  '
                '  # 退回旧形态：按族猜\n'),
        "needle": "第三族必须回显自己的入口",
    },
    {
        # 体检项 B7（2026-10-05）：未知参数（`-help` 拼错成单横线）会掉进
        # `serve()` ⇒ 在 stdin 上阻塞等报文，而用户看到的是"敲了回车没反应、
        # 像卡住了" —— **看不出是自己敲错还是服务坏了**，比报错更难诊断。
        "name": "A18 未知参数退回「不拦、继续往下走」（⇒ 拼错 `-help` 看起来像服务卡住）",
        "target": "verify_video_mcp",
        "file": MCP_SERVER,
        "new": "            return 2\n",
        "old": "            pass  # 退回旧形态：未知参数不拦\n",
        "needle": "未知参数 ⇒ **退出码 2**",
    },
    # -----------------------------------------------------------------------
    # B 组：检测层（把 verify_video 第 3b 节只"写了描述"的五条变成真判据）
    # -----------------------------------------------------------------------
    {
        "name": "B1 `_children` 只给 `meta` 开 FullBox 特例（⇒ 封面帧 0 张、`©too` 读不到）",
        "target": "verify_video",
        "file": VIDEO_DECODE,
        "new": ("    first = list(_iter_boxes(data, box.body, box.end))\n"
                "    if _tiles_exactly(first, box):\n"
                "        return first\n"
                "    if box.body + 4 <= box.end:\n"
                "        shifted = list(_iter_boxes(data, box.body + 4, box.end))\n"
                "        if _tiles_exactly(shifted, box) or (not first and shifted):\n"
                "            return shifted\n"),
        "old": ("    first = list(_iter_boxes(data, box.body, box.end))"
                "  # 退回旧形态：只给 meta 开 FullBox 特例\n"
                "    if first:\n"
                "        return first\n"
                "    if box.body + 4 <= box.end:\n"
                "        return list(_iter_boxes(data, box.body + 4, box.end))\n"
                "    return []\n"),
        "needle": "缺陷 1 · `ilst` 也是 FullBox",
    },
    {
        "name": "B2 `stsz` 当成非 FullBox 读（⇒ `sample_count` 变 0、MJPEG 一帧都取不到）",
        "target": "verify_video",
        "file": VIDEO_DECODE,
        # ⚠️ 锚点里**必须带上函数定义那一行**：`off = box.body + 4` 这个形状在
        #    `_parse_hdlr` 里也有一份（那里是对的 —— `hdlr` 同样是 FullBox）。
        #    只锚那两行会得到 `count == 2`，于是 `replace` 只换第一处 ⇒
        #    **退错了函数**，而元验证照样"通过"（本项目的老账：锚点必须唯一）。
        "new": ("def _parse_stsz(data: bytes, box: _Box) -> dict[str, Any]:\n"
                "    off = box.body + 4\n"
                "    if off + 8 > box.end:\n"),
        "old": ("def _parse_stsz(data: bytes, box: _Box) -> dict[str, Any]:"
                "  # 退回旧形态：stsz 当成非 FullBox 读\n"
                "    off = box.body\n"
                "    if off + 8 > box.end:\n"),
        "needle": "缺陷 2 · `stsz` 是 FullBox",
    },
    {
        "name": "B3 4CC 判据收紧成全 ASCII（⇒ 正常 iPhone 视频报「尾部多余数据」）",
        "target": "verify_video",
        "file": VIDEO_DECODE,
        "new": '        ch == " " or 0x21 <= ord(ch) <= 0x7E or 0x80 <= ord(ch) <= 0xFF\n',
        "old": ('        ch == " " or 0x21 <= ord(ch) <= 0x7E'
                "  # 退回旧形态：要求全 ASCII（非 ASCII 的 4CC 一律翻车）\n"),
        "needle": "缺陷 3 · 4CC 不全是 ASCII",
    },
    {
        "name": "B4 「取到帧但一帧都没送进图像链」时 reason 落回空串"
                "（⇒ 报告里一条原因都没有）",
        "target": "verify_video",
        "file": VIDEO_FRAMES,
        "new": "    if not levels:\n",
        "old": ("    if False:  # 退回旧形态：这一支不接住，reason 落回默认空串\n"),
        "needle": "缺陷 4 · 「取到帧但一帧都送不进图像链」必须留下",
    },
    {
        "name": "B5 GIF 光栅化失败却沿用 MP4 那句原因（⇒ 调用方照着错的原因去换文件）",
        "target": "verify_video",
        "file": VIDEO_FORENSICS,
        "new": "            frame_reason = why\n",
        "old": "            pass  # 退回旧形态：不换成 GIF 自己的原因，沿用上面那句 MP4 原因\n",
        "needle": "缺陷 5 · 手拼 GIF 光栅化失败时，理由必须是**它自己的**",
    },
    # -----------------------------------------------------------------------
    # 第 3c 组：2026-10-04 体检查出的 Ogg 缺陷（「谁算视频」写了两处）
    # -----------------------------------------------------------------------
    # ⚠️ 这一组的共同点：**能力整项死掉、而报告照样出得来 / 工具照样返回**。
    #    退回之后没有任何崩溃，只有一句**看起来正常的假话**
    #    （「不是可识别的视频容器」）。所以只能靠断言抓。
    {
        "name": "B6 `analyze()` 的把关退回前缀测试"
                "（⇒ Ogg 在检测层就被拒，报告出不来、理由还是假话）",
        "target": "verify_video",
        "file": VIDEO_FORENSICS,
        # ⚠️ 与 A13 是**同一件缺陷的两个位置**（检测层 / 工具层各写了一份判据），
        #    所以两条都要有：只退检测层时 MCP 的工具层仍能服务 Ogg（probe 走的是
        #    `core.is_supported_video`），反之亦然 —— 那是"两处判据只改一处"的活样本。
        "new": ('    if not is_supported_video(decoded.media_type):\n'
                '        raise ValueError(\n'
                '            non_video_clause(decoded.media_type)\n'),
        "old": ('    if not (decoded.media_type.startswith("video/")\n'
                '            or decoded.media_type == "image/gif"):'
                "  # 退回旧形态：前缀测试\n"
                '        raise ValueError(\n'
                '            non_video_clause(decoded.media_type)\n'),
        "needle": "application/ogg · 认得就该**出报告**",
    },
    {
        "name": "B7 `is_supported_video()` 不读解码层那张容器表"
                "（⇒ 「认得」这件事与「收不收」又分家了）",
        "target": "verify_video",
        "file": VIDEO_FORENSICS,
        # ⚠️ 这是本轮修复的**核心**：判据现场读 `_OTHER_CONTAINERS` ⇒
        #    「加一个容器」只需改解码层那张表，接受判据自动跟上。
        #    退回成写死的两个前缀分支，保护就会**静默**失效。
        "new": ('    return (\n'
                '        media_type.startswith("video/")\n'
                '        or media_type == "image/gif"\n'
                '        or media_type in _OTHER_CONTAINERS\n'
                '    )\n'),
        "old": ('    return (  # 退回旧形态：判据里不读解码层那张表\n'
                '        media_type.startswith("video/")\n'
                '        or media_type == "image/gif"\n'
                '    )\n'),
        "needle": "接纳判据覆盖解码层**两张表的每一个容器**",
    },
    {
        "name": "B8 别名表少了 Ogg 那一族"
                "（⇒ 一份**声明完全正确**的 `.ogv` 立刻吃一条假线索）",
        "target": "verify_video",
        "file": VIDEO_FORENSICS,
        # ⚠️ 这条缺陷是让 Ogg 进门**之后**才暴露的（"连通面"）：
        #    进门之前它走不到 `check_container` 那一步，所以"别名表里没有 Ogg"
        #    一直没人发现。一旦放行，`normalize_mime("video/ogg")` 原样返回
        #    ⇒ `video/ogg` ≠ `application/ogg` ⇒ 一份合规文件被报「声明与真实
        #    容器不一致」，综合等级从「无法判定」被推成「低」。
        #    **这正是"修一个缺陷会露出下一个"的现行犯。**
        "new": ('    "application/ogg": "application/ogg",\n'
                '    "video/ogg": "application/ogg",\n'
                '    "audio/ogg": "application/ogg",\n'
                '    "application/x-ogg": "application/ogg",\n'),
        "old": ("    # 退回旧形态：别名表里没有 Ogg 那一族\n"),
        "needle": "Ogg 的三种等价声明都归到同一个容器名",
    },
    {
        # 体检项 B6（2026-10-05）：「容器里**也没有**逐帧存着的 JPEG/PNG」原先
        # **不分容器**地写着 —— 对只识别不解析的容器（TS / FLV / WMV / Ogg …），
        # 本进程压根没往里看 ⇒ 那是把**没查过写成查过了**。
        # 与音频侧 `declared_size is None` 那次同族：报告写得很具体，读者无从怀疑。
        # ⚠️ 判据是**成对**的：只识别的容器不许说那句话，**解析过**的（裸 MP4）
        # 必须仍然说 —— 否则"一律改成无法判断"也能把这条蒙过去。
        "name": "B9 帧层理由退回「不分容器」（⇒ 没解析过也说「里面没有逐帧 JPEG/PNG」）",
        "target": "verify_video",
        "file": VIDEO_DECODE,
        "new": ('    if parse_depth == "full":\n'
                '        return "容器里也没有逐帧存着的 JPEG/PNG"\n'),
        "old": ('    if True:  # 退回旧形态：不分情形\n'
                '        return "容器里也没有逐帧存着的 JPEG/PNG"\n'),
        "needle": "帧层**不许**说",
    },
]


def syntax_ok(path: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)], capture_output=True
    ).returncode == 0


def run_target(which: str) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", str(TARGETS[which])],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def is_green(which: str, code: int, out: str) -> bool:
    return code == 0 and not any(m in out for m in FAIL_MARKERS[which])


def digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def install_restore_guard(originals: dict[Path, bytes]) -> None:
    """收到 SIGTERM / SIGINT / SIGHUP 时也要先把文件还原再退出。

    ⚠️ Windows 上被强杀时挂不上 `signal`（进程直接没了，不做栈展开）⇒
    文件会**静静留在旧形态**，而它看起来完全正常、照样能跑。
    所以真正可靠的判据不是这个守卫，而是**跑完之后的残留核对**：
    `python -m tools.tree_diff diff` 与 `python -m run.check_meta_residue`。
    本函数只是第一道防线。
    """
    def handler(signum: int, _frame: object) -> None:
        for path, original in originals.items():
            try:
                if path.read_bytes() != original:
                    path.write_bytes(original)
                    print(f"[信号 {signum}] 已把 {path.name} 恢复为原始内容", flush=True)
            except Exception:  # noqa: BLE001
                pass
        raise SystemExit(1)

    # ⚠️ 不能写 `signal.SIGHUP` 字面量：Windows 上没有这个属性，而且它是在 try
    #    之外求值的 —— `except AttributeError` 根本拦不住（本地崩、服务器正常）。
    for sig in [s for s in (getattr(signal, n, None)
                            for n in ("SIGTERM", "SIGINT", "SIGHUP"))
                if s is not None]:
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handler)


def main() -> int:
    # ⚠️ 行缓冲：被掐断时也必须看得见"跑到第几条了"（本模块约 5 分钟）。
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

    paths = {c["file"] for c in CASES}  # type: ignore[misc]
    originals = {p: p.read_bytes() for p in paths}
    install_restore_guard(originals)

    # 读字节 → 显式 utf-8 解码。目标必须本来就是纯 LF：否则"退回 → 复原"这一趟
    # 有可能顺手改掉换行符，而元验证的成功/失败会被这个副作用污染。
    texts: dict[Path, str] = {}
    for p in originals:
        text = originals[p].decode("utf-8")
        if "\r" in text:
            print(f"[步骤0] ❌ {p.name} 不是纯 LF —— 先规范化再跑元验证")
            return 1
        texts[p] = text

    # [0] 前提：每一处都必须是"新形态"
    for c in CASES:
        text = texts[c["file"]]  # type: ignore[index]
        if text.count(c["new"]) != 1 or c["old"] in text:  # type: ignore[arg-type]
            print(f"[步骤0] ❌ 前置不成立：{c['name']} 不是预期的'新形态'"
                  f"（new×{text.count(c['new'])}、old 出现={c['old'] in text}）")
            return 1

    # [0b] 前提：两个目标当前都必须绿的 —— 否则"退回后报失败"什么都证明不了。
    for which in TARGETS:
        code, out = run_target(which)
        green = is_green(which, code, out)
        print(f"[步骤0] {'✅' if green else '❌'} {TARGETS[which]} 当前为绿 = {green}")
        if not green:
            print(out[-3000:])
            return 1

    a_cases = [c for c in CASES if c["target"] == "verify_video_mcp"]
    b_cases = [c for c in CASES if c["target"] == "verify_video"]
    print(f"[步骤0] ✅ {len(CASES)} 处关键实现均为新形态"
          f"（MCP 层 {len(a_cases)} 条 / 检测层 {len(b_cases)} 条）；"
          f"指纹 {[f'{p.name}={d[:8]}' for p, d in ((p, digest(p)) for p in paths)]}")

    try:
        for i, c in enumerate(CASES, 1):
            path, name = c["file"], str(c["name"])  # type: ignore[index]
            which = str(c["target"])
            original = texts[path]
            new, old, needle = str(c["new"]), str(c["old"]), str(c["needle"])

            # [1] 退回旧形态
            broken = original.replace(new, old)
            if broken.count(old) != 1 or new in broken:
                print(f"[{i}.1] ❌ {name}：替换未按预期发生")
                return 1
            path.write_bytes(broken.encode("utf-8"))
            if not syntax_ok(path):
                print(f"[{i}.1] ❌ {name}：退回后语法错误 —— 本次元验证证明不了任何事")
                return 1
            print(f"[{i}.1] ✅ {name}：已退回旧形态（{path.name}），且仍语法正确")

            # [2] 断言必须报失败
            code, out = run_target(which)
            hit = needle in out
            print(f"[{i}.2] {'✅' if hit else '❌'} {name}："
                  f"{TARGETS[which]} 报出该失败 = {hit}（退出码 {code}）")
            for line in out.splitlines():
                if needle in line:
                    print(f"          {line.strip()}")
            if not hit:
                print(f"[{i}.2] ⚠️ {name} 的断言没响 —— 它是空转的（或匹配词写错了）")
                return 1

            # [3] 复原
            path.write_bytes(original.encode("utf-8"))
            if not syntax_ok(path):
                print(f"[{i}.3] ❌ {name}：复原后语法错误")
                return 1
            print(f"[{i}.3] ✅ {name}：已复原")

        # [4] 全部复原后，指纹必须一致，且两个目标都整体转绿
        stale = [p for p in originals if digest(p) != digest_from(originals[p])]
        if stale:
            print(f"[步骤4] ❌ 复原后指纹不一致：{[p.name for p in stale]}")
            return 1
        all_green = True
        for which in TARGETS:
            code, out = run_target(which)
            green = is_green(which, code, out)
            all_green = all_green and green
            print(f"[步骤4] {'✅' if green else '❌'} 复原后 {TARGETS[which]} 转绿 = {green}")
            if not green:
                print(out[-3000:])
        print()
        print(f"⚠️ 以上只证明「在跑的时候还原过」（{len(CASES)} 处）。"
              "**真正的判据是跑完之后**：")
        print("   ① `python -m tools.tree_diff save` 在跑之前记全树指纹")
        print("   ② `python -m tools.tree_diff diff` 在跑完之后比对（**不 diff 等于没跑过**）")
        print("   ③ `python -m run.check_meta_residue` 核对每处锚点都在新形态上")
        return 0 if all_green else 1
    finally:
        for path, original in originals.items():
            if path.read_bytes() != original:
                path.write_bytes(original)
                print(f"[清理] 已把 {path.name} 恢复为原始内容")


def digest_from(raw: bytes) -> str:
    return hashlib.md5(raw).hexdigest()


if __name__ == "__main__":
    sys.exit(main())
