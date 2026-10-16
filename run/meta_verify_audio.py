"""元验证：音频鉴伪这一轮新增的断言**不是空转的**。

做法与前几个元验证一致：把每处关键实现**退回旧形态**，要求对应断言立刻报失败。
判据只有一条：**退回后仍然语法正确**（否则证明不了任何事）。

════════════════════════════════════════════════════════════════════════
为什么本轮的元验证特别必要
════════════════════════════════════════════════════════════════════════

音频链有一个**图像链没有的**回归形态，而且它比误报更隐蔽：

    把「这一项不适用」写成「整份报告测不了」。

它的后果不是崩溃，也不是报错 —— 而是**每一段正常录音都得到「无法判定」**，
报告看起来"很谨慎"，于是彻底失去信息量，使用者只会绕开它。这类缺陷
只靠读代码看不出来（两种写法都只有一行），所以必须有 C1 这样的用例把它钉住。

**处数不写在这里**（下面 `CASES` 里逐条列着；写死的数字迟早与 `len(CASES)` 分家，
而跑步时打印的是**现场派生**的那个）。逐条对应：

  C1. `NOT_APPLICABLE_PREFIX` 与闸门前缀合并（`= GATE_ABSTAIN_PREFIX`）
      退回之后，纯音/白噪这类**其余各项都跑通了**的音轨也会整份变成「无法判定」。
      这一条同时钉住了"两层弃权互斥且并集覆盖全部弃权项"那条关系断言。

  C2. 底噪一致性的参考电平退回**邻域中位数**
      这是本模块真实踩过的坑：安静期在邻域里往往占一半以上 ⇒ 中位数落在
      安静段自己身上 ⇒ 差值恒为 0 ⇒ **所有样本都报「0 段相对安静区」**。
      退回后"两段不同底噪拼接"必须不再被抓住。

  C3. 取件族漏配一条建议（`data_uri_bad_base64` → `None`）
      本条对应"每个分类码都有专属建议"的逐码断言。用它而不是"删掉整项"，
      是因为**删除不能表达成替换**（空串会让残留核对把每一次都判成残留）——
      `None` 语义等价于"加了码却忘了配建议"，且仍是合法表达式。

  C4. 闸门那一支退回图像链的历史措辞（"N 项检查均未提示异常"）
      那是图像链踩过的原案：内部判「无法判定」、摘要说「没问题」。
      两句话出自不同分支时，前半句与后半句互相打脸，而表面看不出异常。

  C5. `enf_track` 从包的导出面消失
      这正是本轮**真实修掉**的一个缺陷：MCP 侧因为显式 `from ... import
      <子模块>` 才拿得到它，而包顶层拿不到 —— 一个"依赖调用顺序 / 依赖导入
      路径"的导出。退回后 `tools.verify_audio` 必须在导入期就**出声**。

  C6. ENF 的权重从 1.5 调到 0.1
      这一条证明的是"等级由权重表算出来，不是写死的"：权重一改，
      相位跳变样本的等级必须跟着变。

  C7. `evidence-audio` 没登记进端口表
      `check_local` 是"结构是不是我们打算要的样子"的看板，缺一个角色必须报出来
      —— 而不是抛 `KeyError`。这一条同时**修掉了看板自己的一个缺陷**：
      原先直接写 `PORTS[r]`，缺登记时留下的是一段栈，不是一句诊断。

      ⚠️ 2026-10-05：本条**再修了一次**，而且修的是**第一次没修到的那一半**。
      第一次只做到"把诊断写进某一节"，可**哪一段代码先撞上 KeyError 取决于
      `.env` 里有没有 `ACPS_PUBLIC_HOST`**（有 ⇒ 停在第 2 节，只留 78 行栈；
      没有 ⇒ 第 2 节侥幸过去、第 3 节先崩）⇒ **同一份代码在两台机器上停在不同
      地方**，而服务器上恰好是"看不到诊断"的那一半 —— 本条因此在服务器上
      **空转了一整轮**，本地却是绿的（本地绿纯属运气）。
      ⇒ 本条现在在 `PORT_ENVS` 列出的**每一个**环境里各跑一遍、且要求**都**命中：
      只要有一个环境不响，就等于有一台机器上这条断言是空转的。
      这同时也把"闸门装错地方（排在更靠后的节里报）"那一种退化一并钉住了 ——
      它和"根本不报"是**同一处实现的两种退化**，靠"两个环境都跑"这一件事同时覆盖。

  C8. `check_local` 的角色快照没跟着新角色长
      快照是**故意写死**的（它要回答"结构是不是我们打算要的样子"，不能从
      BUILDERS 派生），代价就是"加角色时必然漏"。这条断言保证漏了当场响。

  C9. 图像角色的收件 Hook 跟着一起变宽（`kinds=("image", "audio")`）
      两个模态的 Hook 配置"一起变宽"在外表上与"都配好了"完全一样，
      但图像角色会开始收它用不上的模态、并把清单注入给模型。
      这条断言要求每个角色**只收自己的模态**。

  C10. 主控提示词里少了**录音鉴伪分支**
      角色建好了、Hook 挂对了、端口登记了、断言全绿 —— 而用户带录音来时
      主控**不知道该往哪派**。这是"加一个角色"最容易漏掉的一跳，
      而且**所有其它断言都看不见它**（它们只看"结构里有没有这个角色"，
      不看"顶层知不知道该怎么走"）。

  C11. 主控在音频分支里写回下游注册名
      这一条专门证明新加的 `or "音频鉴伪 Partner" in ...` 在干活：
      原先只查 `"图像鉴伪 Partner"` 那一个，于是"顺手在新写的音频分支里
      写下游名字"**不会被任何断言拦下** —— 后果一模一样（方案 A 被架空）。

  C12. `check_codec_params` 在**一次对照都没做过**时也走到"内部自洽"
      它报的是「编码参数**内部自洽**（字节率 / 块对齐 / 数据长度相互吻合）」，
      而在 FLAC（读得到采样率、读不到那三个字段）与 `fmt` 里 channels=0 的 WAV 上，
      那句点名的三次对照**一次都没发生**。这是"把没测写成没问题"，
      而且写得很具体 ⇒ 读者无从怀疑。

  C13. 「`data` 块长度不是帧长整数倍 ⇒ 末尾 N 字节被丢弃」这条提示不落地
      它原先 append 进的列表**已经拷进 facts** ⇒ 提示永远到不了报告；
      而即便修好顺序、只写进 `facts.notes` 也还不够（那不是报告字段）。
      退回后 `tools.verify_audio` 必须报出"进不了 facts"。

  C14. 数字静音项在"内部可检查区间不够长"时也说「没有发现…这符合连续录音的形态」
      0.6 秒出头的音轨扣掉首尾保护带（各 0.3 秒）之后放不下一次判据，
      于是"一次没查"被写成"查过没问题"。同族的另外三项在窗口切不出来时都弃权。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AUDIO_FORENSICS = ROOT / "legal_acps" / "audio" / "forensics.py"
AUDIO_DECODE = ROOT / "legal_acps" / "audio" / "decode.py"
AUDIO_INIT = ROOT / "legal_acps" / "audio" / "__init__.py"
AUDIO_PIPELINE = ROOT / "legal_acps" / "audio" / "pipeline.py"
AGENTS = ROOT / "legal_acps" / "agents.py"
ACPS_CONFIG = ROOT / "legal_acps" / "acps_config.py"
CHECK_LOCAL = ROOT / "legal_acps" / "check_local.py"

TARGETS = {
    "verify_audio": "tools.verify_audio",
    "check_local": "legal_acps.check_local",
}

#: 目标脚本里"这一条没通过"的标记。两个脚本用词不同，所以要按目标分别判 ——
#: 统一判 `[失败]` 会让 `verify_audio` 的失败**在步骤 0 与步骤 4 被漏判**，
#: 于是"退回后报失败"这件事根本无从证明。
FAIL_MARKERS = {
    "verify_audio": ("[未达预期]", "[失败]"),
    "check_local": ("[失败]", "[未达预期]"),
}

#: 有些缺陷**只在某一种 `.env` 形态下才复现**。判据的输入集合必须与它要判的
#: 现实同宽 ⇒ 这类用例把目标**逐个环境都跑一遍**，并要求**每一个环境都命中**。
#:
#: 2026-10-05 实证（这是加它的理由）：「某个角色没登记端口」这句诊断报不报得
#: 出来，取决于 `.env` 里有没有 `ACPS_PUBLIC_HOST` ——
#:    · 有 ⇒ `build_config()` 走到 `endpoint_for()`，看板止于第 2 节的一段栈；
#:    · 没有 ⇒ 第 2 节侥幸过去，先崩的是第 3 节的 `PORTS[role]`。
#: 于是**同一份代码在两台机器上停在不同地方**。只按本机默认环境跑一次，
#: 服务器上那一半现实就永远测不到（用例 C7 因此在服务器上空转了一整轮，
#: 而本地是绿的 —— 本地绿纯属运气）。
PORT_ENVS: list[dict[str, str]] = [{}, {"ACPS_PUBLIC_HOST": "127.0.0.1"}]

CASES = [
    {
        "name": "两类弃权合并（⇒ 每段正常录音都变成「无法判定」）",
        "file": AUDIO_FORENSICS,
        "new": 'NOT_APPLICABLE_PREFIX = "本项对这段音轨无从执行"\n',
        "old": "NOT_APPLICABLE_PREFIX = GATE_ABSTAIN_PREFIX  # 退回旧形态：两层弃权合并\n",
        "needle": "负对照 · 纯音 440 Hz（窄带） → 综合等级「低」",
        "runner": "verify_audio",
    },
    {
        "name": "底噪参考电平退回邻域中位数（⇒ 所有样本都报「0 段相对安静区」）",
        "file": AUDIO_FORENSICS,
        "new": "NOISE_ENVELOPE_PCT = 90.0\n",
        "old": "NOISE_ENVELOPE_PCT = 50.0\n",
        "needle": "两段不同底噪拼接 → 综合「中」，且 noise_floor 触发",
        "runner": "verify_audio",
    },
    {
        "name": "取件族漏配建议（加了分类码却忘记配出路）",
        "file": AUDIO_PIPELINE,
        "new": (
            '    "data_uri_bad_base64":\n'
            '        "检查 data: URI 的 base64 载荷是否完整'
            '（中间不能有换行、空格或缺失的填充符）。",\n'
        ),
        "old": '    "data_uri_bad_base64": None,  # 退回旧形态：加了码却没配建议\n',
        "needle": "每一个分类码都有**专属**补救建议",
        "runner": "verify_audio",
    },
    {
        "name": "闸门那一支退回图像链的历史措辞（内部判无法判定、摘要说没问题）",
        "file": AUDIO_PIPELINE,
        "new": '            f"综合线索等级：无法判定。本音轨不满足波形域检查的前提，"\n',
        "old": '            f"综合线索等级：无法判定。{len(usable)} 项检查均未提示异常，"\n',
        "needle": "闸门那一支：**不得**出现「均未提示异常」",
        "runner": "verify_audio",
    },
    {
        "name": "`enf_track` 从包的导出面消失（只剩「按导入路径碰运气」）",
        "file": AUDIO_INIT,
        "new": '    "enf_track": "forensics",\n',
        "old": '    # "enf_track": "forensics",  # 退回旧形态：只从子模块能拿到\n',
        "needle": "cannot import name 'enf_track'",
        "runner": "verify_audio",
    },
    {
        "name": "ENF 权重被调低（⇒ 证明等级由权重表算出，不是写死的）",
        "file": AUDIO_FORENSICS,
        "new": '    "enf": 1.5,                   # 最强：相位不连续几乎只可能来自剪辑\n',
        "old": '    "enf": 0.1,                   # 最强：相位不连续几乎只可能来自剪辑\n',
        "needle": "市电相位跳变 → 综合「中」，且 enf 触发",
        "runner": "verify_audio",
    },
    {
        "name": "`evidence-audio` 没登记进端口表（看板必须报出来，而不是抛栈）",
        "file": ACPS_CONFIG,
        "new": '    "evidence-audio": 8451,',
        "old": '    # "evidence-audio": 8451,',
        "needle": "这些角色没有登记端口",
        "runner": "check_local",
        # ⚠️ 两个环境都要命中：见 `PORT_ENVS` 那段说明。只跑默认环境的话，
        #    "设了 ACPS_PUBLIC_HOST 的那台机器上会不会抛栈"这件事没人管。
        "envs": PORT_ENVS,
    },
    {
        "name": "`check_local` 的角色快照没跟着新角色长（结构主张缺一项）",
        "file": CHECK_LOCAL,
        "new": '        "evidence-audio",\n',
        "old": '        # "evidence-audio",\n',
        "needle": "角色集合不符",
        "runner": "check_local",
    },
    {
        "name": "图像角色的收件 Hook 跟着一起变宽（模态配置不许在两边同时放宽）",
        "file": AGENTS,
        "new": '            EvidenceInboxHooks(delegate_tool=""),\n',
        "old": '            EvidenceInboxHooks(kinds=("image", "audio"), delegate_tool=""),\n',
        "needle": "`evidence-image` 只收图片（模态不许在两边一起变宽）",
        "runner": "verify_audio",
    },
    {
        # 21 处里唯一一条针对**提示词路由**的用例：角色建好了、Hook 挂对了、
        # 端口登记了、断言全绿 —— 而用户带录音来时主控不知道该往哪派。
        # 这是"加角色"最容易漏掉的一跳，且**所有其它断言都看不见它**。
        "name": "主控提示词里少了音频鉴伪分支（⇒ 音频链在顶层断掉，而所有离线断言仍全绿）",
        "file": AGENTS,
        "new": "音频鉴伪分支（只在",
        "old": "占位分支（只在",
        "needle": "没有这些鉴伪分支：「音频鉴伪分支」",
        "runner": "check_local",
    },
    {
        # 只有 `"图像鉴伪 Partner" in ...` 那一个反向判据时，
        # "顺手在新写的音频分支里写下游注册名"**不会被任何断言拦下**（同此前的漏洞形态）。
        # 这一条证明新加的 `or "音频鉴伪 Partner" in ...` 真的在干活。
        "name": "主控在音频分支里写回下游注册名（⇒ 只查图像那一个的话，这个漏洞不会响）",
        "file": AGENTS,
        "new": '  所以"检查不了"会经常出现。**绝不能**把「无法判定」转述成「没问题」——\n',
        "old": '  所以"检查不了"会经常出现。下游是「音频鉴伪 Partner」。**绝不能**'
               "把「无法判定」转述成「没问题」——\n",
        "needle": "主控 instructions 里出现了下游专家的注册名",
        "runner": "check_local",
    },
    {
        # 一次对照都没做过时说「编码参数**内部自洽**（字节率 / 块对齐 / 数据长度相互吻合）」
        # —— 属于"把没测写成没问题"，是本项目最忌讳的那一类。
        # FLAC（读得到采样率、读不到那三个字段）与 fmt 里 channels=0 的 WAV
        # 都会走到这里。
        "name": "一次对照都没做过也说「内部自洽」（⇒ 报告点名了三个一个都没读到的字段）",
        "file": AUDIO_FORENSICS,
        "new": "    if not compared:\n",
        "old": "    if False:  # 退回旧形态：没对照过也说「内部自洽」\n",
        "needle": "FLAC：编码参数**一次对照都做不了**",
        "runner": "verify_audio",
    },
    {
        # 落单字节被丢弃这件事写进了一个**已经拷进 facts** 的列表（或干脆不写），
        # 于是它永远到不了报告 —— "静默少分析了一点东西"。
        # ⚠️ 退回形态用 `pass` 而不是 `if False:`：这一段是**整块**被替换掉的，
        #    换成 `if False:` 会留下一个没有语句体的 `if` ⇒ 语法错误 ⇒
        #    元验证直接判"证明不了任何事"（实测踩到一次）。
        #    "删除"不能表达成"替换"，得用一个语义等价且**合法**的写法。
        "name": "「末尾 N 字节被丢弃」不写进 notes（⇒ 报告里看不出少分析了东西）",
        "file": AUDIO_DECODE,
        "new": (
            "    if (effective in (WAV_FORMAT_PCM, WAV_FORMAT_FLOAT)\n"
            "            and available and usable != available):\n"
            '        notes.append(\n'
            '            f"data 块长度 {available} 不是帧长 {block} 的整数倍，"\n'
            '            f"末尾 {available - usable} 字节被丢弃。"\n'
            "        )\n"
        ),
        "old": "    pass  # 退回旧形态：这条提示不再写进 notes\n",
        "needle": "「末尾 1 字节被丢弃」必须**进得了 facts**",
        "runner": "verify_audio",
    },
    {
        # 0.6 秒出头的音轨：扣掉首尾各 0.3 秒保护带之后，数字静音项的**可检查区间**
        # 放不下一次判据（50 毫秒）—— 那是"一次没查"，不是"查过没问题"。
        # 同族的另外三项（底噪 / 谱突变 / 样本不连续）在窗口切不出来时都弃权，
        # 这一项原先却报"这符合连续录音的形态"。
        "name": "内部区间不够长也说「没有发现数字静音段」（⇒ 一次没查被读成查过没问题）",
        "file": AUDIO_FORENSICS,
        "new": "    if x.size - 2 * guard < min_len:\n",
        "old": "    if False:  # 退回旧形态：内部区间不够长也说「没有发现」\n",
        "needle": "0.6 秒出头的音轨：扣掉首尾各 0.3 秒保护带后",
        "runner": "verify_audio",
    },
    {
        # 🔴 2026-10-05 实测的**死键**：`audio/ac3` 挂在解码层的容器表里（还配了
        #    中文名「AC-3」），而 `sniff_media_type()` **没有任何分支能返回它**。
        #    真的 `.ac3` 因此被 `analyze()` 拒掉，理由是假话「不是可识别的音频容器」。
        #    退回这一支之后，"表里每个键都必须真的有嗅探分支"必须报失败。
        "name": "AC-3 的嗅探分支被删掉（⇒ `audio/ac3` 又变回一个**死键**："
                "表里有、路不通，真文件被拒）",
        "file": AUDIO_DECODE,
        "new": '    if data[:2] == b"\\x0b\\x77":\n',
        "old": "    if False:  # 退回旧形态：没有 AC-3 分支（audio/ac3 是个死键）\n",
        "needle": "表里每一个键都必须真的有嗅探分支",
        "runner": "verify_audio",
    },
    {
        # `.wma` 在业界通用的 MIME 正是 `audio/x-ms-wma`，而别名表里原先没有它 ⇒
        # 一份**声明正确**的 WMA 会吃到「声明与真实容器不一致」的假线索，等级还被
        # 从「无法判定」抬到「低」。（与视频侧 `video/ogg` vs `application/ogg` 同病）
        "name": "别名表缺 WMA 的常见写法（⇒ 声明**正确**的 WMA 吃到「与真实容器不一致」）",
        "file": AUDIO_FORENSICS,
        "new": '    "audio/x-ms-wma": "audio/wma",\n',
        "old": "    # 退回旧形态：别名表缺 `audio/x-ms-wma`（.wma 的通用写法）\n",
        "needle": "按**标准 MIME** 声明时**不许吃假线索**",
        "runner": "verify_audio",
    },
    {
        # 「声明的长度与实际字节相符，没有附加数据或尾部标签」原先**不分容器**地写着。
        # 而没有可比长度声明的容器（只识别的六个 + **有解析器的 MP3 / FLAC**，
        # 实测 `declared_size is None`）根本不成立这句话 —— 三项检查一次都没跑，
        # 却写成了正面结论。这是本仓最危险的一类缺陷（第 3b 节整节都在修它）。
        "name": "容器项退回「一律说声明长度相符」（⇒ 没跑过的检查被写成正面结论）",
        "file": AUDIO_FORENSICS,
        "new": "    if facts.declared_size is None:\n",
        "old": "    if False:  # 退回旧形态：没有长度声明可比也说「相符」\n",
        "needle": "头部**不写总长**",
        "runner": "verify_audio",
    },
    {
        # metadata 那句原先对**所有**容器都写「这种容器本次也没有解析内部标签」——
        # 对 WAV 那是假话（它的内部标签查过了，确实一个可读的都没有）。
        # 退回成"不分情形"之后，"同一字段、两种输入、两句不同的话"必须报失败。
        "name": "metadata 退回不分情形（⇒ 对 WAV 说「本次没有解析内部标签」这句假话）",
        "file": AUDIO_FORENSICS,
        "new": '        + ("本能力为这种容器写的解析器**没有读出**可读标签"\n',
        "old": '        + ("这种容器本次也没有解析内部标签"  # 退回旧形态：不分情形\n',
        "needle": "metadata 那句也要分清",
        "runner": "verify_audio",
    },
]


def syntax_ok(path: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)], capture_output=True
    ).returncode == 0


def run_target(which: str, extra_env: dict[str, str] | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(
        [sys.executable, "-m", TARGETS[which]],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
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
    # ⚠️ 行缓冲：被掐断时也必须看得见"跑到第几条了"。默认块缓冲下，
    #    `python -m run.xxx | tail` 被强杀时**一个字都没落盘** ——
    #    于是"中途被杀"与"根本没跑"在日志里长得一模一样。
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

    paths = {c["file"] for c in CASES}  # type: ignore[misc]
    originals = {p: p.read_bytes() for p in paths}
    install_restore_guard(originals)

    texts: dict[Path, str] = {}
    for p in paths:
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

    # [0b] 前提：两个目标当前都得是绿的 —— 否则"退回后报失败"什么都证明不了，
    #      因为旧形态与现状分不开。（这一步同时是"锚点有效"的前置。）
    for which in TARGETS:
        code, out = run_target(which)
        green = is_green(which, code, out)
        print(f"[步骤0] {'✅' if green else '❌'} {TARGETS[which]} 当前为绿 = {green}")
        if not green:
            print(out[-2000:])
            return 1

    befores = {p: digest(p) for p in paths}
    print(f"[步骤0] ✅ {len(CASES)} 处关键实现均为新形态；"
          f"指纹 {[f'{p.name}={d[:8]}' for p, d in befores.items()]}")

    try:
        for i, c in enumerate(CASES, 1):
            path, name = c["file"], str(c["name"])  # type: ignore[index]
            original = texts[path]
            new, old, needle, runner = (
                str(c["new"]), str(c["old"]), str(c["needle"]), str(c["runner"]),
            )

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
            #     ⚠️ 判据的输入集合也是判据的一部分：标了 `envs` 的用例必须
            #     **每个环境都命中** —— 只要有一个环境不响，就等于有一台机器上
            #     这条断言是空转的，而"一半机器绿"正是最难发现的那种缺陷。
            envs = c.get("envs") or [{}]  # type: ignore[union-attr]
            hits: list[bool] = []
            for env in envs:
                code, out = run_target(runner, env)
                hit = needle in out
                hits.append(hit)
                tag = "（默认环境）" if not env else (
                    "（" + " ".join(f"{k}={v}" for k, v in env.items()) + "）"
                )
                print(f"[{i}.2] {'✅' if hit else '❌'} {name}{tag}："
                      f"{TARGETS[runner]} 报出该失败 = {hit}（退出码 {code}）")
                for line in out.splitlines():
                    if needle in line:
                        print(f"          {line.strip()}")
            if not all(hits):
                print(f"[{i}.2] ⚠️ {name} 的断言没响 —— 它是空转的（或匹配词写错了）")
                return 1

            # [3] 复原
            path.write_bytes(original.encode("utf-8"))
            if not syntax_ok(path):
                print(f"[{i}.3] ❌ {name}：复原后语法错误")
                return 1
            print(f"[{i}.3] ✅ {name}：已复原")

        # [4] 全部复原后，指纹必须一致，且两个目标整体转绿
        stale = [p for p in originals if digest(p) != befores[p]]
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
                print(out[-2000:])
        print()
        print("⚠️ 以上只证明「在跑的时候还原过」。**真正的判据是跑完之后**：")
        print("   ① `python -m tools.tree_diff save` 在跑之前记全树指纹")
        print("   ② `python -m tools.tree_diff diff` 在跑完之后比对（**不 diff 等于没跑过**）")
        print("   ③ `python -m run.check_meta_residue` 核对每处锚点都在新形态上")
        return 0 if all_green else 1
    finally:
        for path, original in originals.items():
            if path.read_bytes() != original:
                path.write_bytes(original)
                print(f"[清理] 已把 {path.name} 恢复为原始内容")


if __name__ == "__main__":
    sys.exit(main())
