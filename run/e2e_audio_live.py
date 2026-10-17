"""**真机端到端 · 主控带音频** —— 四条证据链里**最后一条没有探针**的（#122）。

为什么必须有它：
  · `run/e2e_leader_image.py`（主控带图）与 `run/e2e_video_live.py`（叶子角色直驱）
    各有各的那一份，而 **8451（evidence-audio）此前没有任何真机端到端脚本** ——
    #24 / #91 的验收只覆盖到 image。四条链里三条能自证，一条只能靠"代码看起来一样"。
  · 而"看起来一样"在本项目里已被反复证伪：加一条链时漏掉的那一处，
    恰好就是没人盯着的那一处（音频链的落点上限、`kinds=("audio",)`、
    AGENT 的 `_active_aic` 角色名……都是这么漏的）。

它与 `run_agent leader --input` 的区别只有一个：输入带 `AudioPart`。
    CLI 只有 `--input`（纯字符串）—— 附件**结构上进不去**；
    而 `runtime.run(parts)` 收部件列表，且**不启动监听**（进程内跑）。
⚠️ 它仍然**必须用主控自己的身份**（出站委派要 mTLS + AIC）⇒ 跑之前**必须停掉**
   常驻的 `acps-leader.service`：两个进程共用 `.wit/leader`（同一份 AIP 任务库）会互相抢。
    ⚠️ 要 `systemctl stop`，**不是** `serve.sh stop` —— 单元是 `Restart=always`，
    kill 完 systemd 会立刻把服务拉起来，正好与本次运行抢工作区。

步数预算取**同一份单一事实源**（`run_agent.research_run_config()`），这里不写第二个数：
写死就会漂移，而漂移的那一半恰好是没人盯着的那一半。

════════════════════════════════════════════════════════════════════════
判据怎么取（本章是本脚本唯一值得细读的部分）
════════════════════════════════════════════════════════════════════════

三条判据，**落在三个不同的可观测量上**：

  A′｜附件**逐跳**落盘：那份字节在本轮经过的**每一本**收件箱里都存在
     （`.wit/leader` → `.wit/evidence-leader` → `.wit/evidence-audio`），
     而且是**本轮落的**（落盘时间比跑之前那次快照**晚**）。
     ⚠️ 这条是「链真的走通了」的**磁盘证据**：句柄是**我这次发出去的字节的 sha256**
        （由 `inbox.handle_for()` 现算，不写死）；它唯一的成立方式是每一跳的入站 Hook
        都真的拿到了那串字节并落了盘。**不可能**被模型措辞或提示词污染 ——
        比的是密码学哈希。
     ⚠️ 逐跳而不是只看主控那一本：只看主控，链断在第二跳或第三跳**照样全绿**
        （而「下游其实没收到音频、回一句『没有音频可听』」正是这条链最常见的坏法）。
     ⚠️ **「在」不等于「本轮落的」**：本脚本的样本是**确定性合成**的，同一份字节
        每次跑出来的 sha256 都一样 ⇒ 昨天那一轮落下的文件今天仍在盘上。只看
        「文件在不在」会让**这一轮压根没送出去**的失败被历史文件顶替
        （与判据 C 不许借用历史产物是同一个道理）⇒ 判据取「落盘时间变晚了」。
     ⚠️ **判据问的是「送到没送到」，不是「我这个进程能不能取用」**：早先这三条是
        用 `inbox.resolve()` 查的，而它带**归属**校验（跨轮次 / 跨进程不许互相取用）
        ⇒ 探针进程去解析**常驻服务**落的字节**必然被拒**（2026-10-06 实测三本全拒，
        而字节三跳都在盘上）。那时红的是「判据自己取不到」，不是「链断了」——
        两句话长得一模一样。⇒ 改用 `inbox.landed_at()`（只看盘，不问归属）。

  B｜音频技术线索**交回了鉴证层**：`output/` 新增 `evidence-*.json`
     （`evidence.issue_report`，鉴证 Leader 的交付物），且里面出现**我这批样本独有的
     数字**（数字静音段的起止/时长）。
     ⚠️ 只认这些**不可猜**的数字：不认"数字静音""剪辑痕迹"这类词 ——
        那种词模型自己也能编，属于**会命中自身反面**的判据（本项目为这种判据误判过：
        图像侧曾拿"证据鉴证 Leader"当判据，而模型完全可以写"我**未能**联系上证据鉴证 Leader"）。

  C｜结论**随终稿交付**（被采纳）：`output/` 新增 `verdict-*.json`，
     且它的 `consistency_note` 里出现同一批数字。
     ⚠️ 而且必须是**本轮新落盘**的那一份 —— 借用历史产物 = 假阳性，
        它会替一次真实的失败背书（这条在图像侧被负对照逼出来过）。

⚠️ **B 与 C 是两件事，必须分开看**（本项目纪律：「送出去」与「被采纳」各要一条）：
   `evidence-*.json` 落盘只说明**鉴证层把报告交出来了**；
   它**不等于**主控把它用上了 —— 主控完全可以交一份只谈文本、只字不提音频的终稿，
   此时 B ✅ / C ❌，而"文件都在、看起来正常"。

⚠️ 「证据链到底响没响」的**权威判据在任务库**里
   （`.wit/evidence-leader/acps/aip-tasks.sqlite3` 与 `.wit/evidence-audio/…`）——
   有就是有、没有就是没有。本脚本给的是**产物层**的证据，任务库那一步由脚本之外的审计做。

用法（在 witrelate 目录下，需真实模型凭据）：
    ./.venv/bin/python -m run.e2e_audio_live
    ./.venv/bin/python -m run.e2e_audio_live --brief      # 不打全文，只看结构
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path

from dotenv_lite import load_env

load_env()

from wit import AudioPart, RunConfig, TextPart, WitRuntime  # noqa: E402
from wit.acps.aip import MAX_INLINE_FILE_BYTES  # noqa: E402

from legal_acps import run_ledger  # noqa: E402
from legal_acps.acps_config import build_config  # noqa: E402
from legal_acps.agents import build_agent  # noqa: E402
from legal_acps.evidence.inbox import (  # noqa: E402
    handle_for,
    inbox_root,
    landed_at,
)
from legal_acps.llm import build_model  # noqa: E402
from legal_acps.run_agent import _active_aic, research_run_config  # noqa: E402
from legal_acps.audio_gate import BLOCKS_KEY  # noqa: E402
from legal_acps.token_budget import truncated_of  # noqa: E402

# ⚠️ `from run import audio_samples as A` **刻意不放在模块级**：`run/audio_samples.py`
#    会拉 numpy，而本模块要能被 `legal_acps.check_local` **import 进来**
#    （3c 节用它驱动 `run_probe()`，从而行为断言"探针这一轮真的记了台账"）。
#    自检为了这一条把 numpy 引进自己的进程是不划算的 —— 与 `run/samples.py`
#    （numpy + Pillow）、`legal_acps/evidence/__init__.py`（`if TYPE_CHECKING:`）
#    是同一个理由。⚠️ 挪回模块级**不会报错**，只会让自检在缺 numpy 的机器上起不来
#    ⇒ 由 3c 的一条断言盯着（它比对 import 前后 `numpy` / `PIL` 有没有多出来）。

BAR = "─" * 70

#: 数字静音段的构造参数。**故意不圆整**（1.17 / 1.63 而不是 1.2 / 1.6）：
#: 它们是**我这批样本独有的参数**，模型不可能凭常识写对 —— 与图像侧拿
#: "复制-移动位移 Δ=(96,0)"当判据是同一个作用。
#: ⚠️ 必须落在 `check_silence` 的判据窗内：首尾各 `SILENCE_EDGE_GUARD_SECONDS`（0.3 s）
#:    的保护带**不算**内部区间，所以 t0 要 > 0.3、t1 要 < 总时长 − 0.3。
SILENCE_T0, SILENCE_T1 = 1.17, 1.63
#: 音轨总长。3.0 s ⇒ 内部区间 0.30–2.70 s，上面那段（1.17–1.63）稳落在里面。
AUDIO_SECONDS = 3.0
#: 主控 → 鉴证 Leader → 音频鉴伪。**逐跳**都要查收件箱，少一跳这条判据就退化成
#: "只证明主控自己收到了附件"（而链断在下游时它照样全绿）。
HOPS = ("leader", "evidence-leader", "evidence-audio")

#: #125 跨机部署后，第三跳（evidence-audio）常驻在第二台机器上。设
#: ``E2E_AUDIO_LEAF_REMOTE=<ssh目标>``（如 ubuntu@101.33.124.181）后，A′ 的
#: 第三本收件箱改经 ssh 用**同一份** `landed_at()` 在远端核对（助手
#: ``run/remote_inbox_probe.py``，两台代码同源）；不设则查本地旧目录——
#: 迁移后的已知假红（旧文件 staged_at 不变），两本地跳仍然有效。
REMOTE_LEAF_SSH = os.environ.get("E2E_AUDIO_LEAF_REMOTE", "").strip()
REMOTE_HOPS = ("evidence-audio",) if REMOTE_LEAF_SSH else ()


def markers() -> tuple[str, ...]:
    """本批样本**独有**的数字 —— 派生自**构造参数**，不取自检测报告。

    ⚠️ 期望值不许取自被测对象自己的标记：所以这里算的是"我往波形里插了什么"，
       而不是"检测器报了什么"。（两者在正确实现下应当一致；不一致时这条判据
       报的是"对不上"，而不是"检测器说什么就是什么"。）
    """
    return (
        f"{SILENCE_T0:.2f}",
        f"{SILENCE_T1:.2f}",
        f"{SILENCE_T1 - SILENCE_T0:.2f}",
    )


def make_clip() -> tuple[bytes, str, str]:
    """造一段**真**音频：谐波「语音」+ 一段精确为零的区间。返回 `(字节, MIME, 怎么造的)`。

    ⚠️ 必须是**真编码**的 WAV（`wave` 模块写出来的 RIFF/PCM），不是手拼的块结构：
        容器层那两项判据（声明长度 vs 实际长度）靠的正是真头。
    ⚠️ 选 WAV 而不是 MP3/AAC：本机**没有 ffmpeg / sox**，压缩格式**解不出波形**
        ⇒ 波形域整域弃权 ⇒ 综合等级只会是「无法判定」，判据 B/C 就退化成
        "什么都证明不了"。WAV 是唯一能在这台机器上**真的跑出数字**的容器。
    """
    from run import audio_samples as A  # 惰性：见文件顶部关于 numpy 的说明

    track = A.with_digital_silence(
        A.speech_like(AUDIO_SECONDS), t0=SILENCE_T0, t1=SILENCE_T1
    )
    how = (
        f"{AUDIO_SECONDS:.1f} s 谐波「语音」（确定性合成）+ "
        f"一段数字静音（{SILENCE_T0:.2f}–{SILENCE_T1:.2f} s，采样值精确为 0）"
    )
    return A.wav_bytes(track), "audio/wav", how


def task_text() -> str:
    """提问文案必须**同时**满足提示词里的两个触发条件：

      ① 用户提供了录音 / 音频文件 —— 由 `AudioPart` 满足；
      ② 明确要求"听听这段录音**有没有被剪过** / **是不是拼接的** / **有没有编辑痕迹**"。
    少任何一条，音频鉴伪分支都不会走 —— 那这一轮就白跑了（纯文本轮次里这条分支
    **结构上不可能亮**，与图像侧同款）。
    第二段是给**标准流程**（三项判定）用的，提示词明确要求本分支不得替代它们。
    """
    return (
        "我收到一段别人转来的通话录音（随本消息附上），对方说这是某商场负责人"
        "承认封锁消息的对话。请帮我**听听这段录音有没有被剪过**、**是不是拼接的**、"
        "**有没有改动过的痕迹**。\n"
        "另外请按你的标准流程，对随附这段文字做一次完整研判："
        "「网传某商场在促销活动中出现踩踏，现场视频是上周拍的，"
        "商场为了压消息把视频都删了。」"
    )


def newest_output(prefix: str) -> Path | None:
    """最新一份 `output/<prefix>-*.json`（**排除 `.meta.json` sidecar**）。

    ⚠️ 早先图像侧的取法是 `ls -t verdict-*.json | head -1`，它会先撞上几百字节的
        `.meta.json`（名字同样以 `verdict-` 开头）⇒ 判定随之全 0。
    """
    files = [
        p
        for p in Path("output").glob(f"{prefix}-*.json")
        if not p.name.endswith(".meta.json")
    ]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def marker_hits(text: str, marks: tuple[str, ...]) -> list[tuple[str, bool]]:
    """逐个报出样本数字有没有出现在这段文本里（**不做"至少一个"的兜底**）。

    ⚠️ 三个数字是**同一件事的三种表示**（起点 / 终点 / 时长），所以判据取
       "至少一项命中"而不是"全部命中"：主控只转述了时长或只转述了起止，
       都说明那份报告确实回到了它手上。要求全中会把**正确**的轮次判红。
    """
    return [(m, m in text) for m in marks]


def _box_of(role: str) -> Path:
    return inbox_root(Path(f".wit/{role}"))


def _landed_at_for(role: str, handle: str) -> str:
    """A′ 的逐跳落盘核对入口：本地跳直接查盘；远端跳经 ssh 查**同一份**判据。

    远端失败（ssh 不通 / 远端脚本报错）如实返回空串 ——「查不到」不许被
    静默当成「通过」，由 landed_inboxes 的三态逻辑当「不在」报红并带原因。
    handle 是 sha256 十六进制、role 来自本模块常量 ⇒ 无注入面。
    """
    if role in REMOTE_HOPS:
        try:
            cp = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", REMOTE_LEAF_SSH,
                 "cd ~/witrelate && ./.venv/bin/python -m "
                 f"run.remote_inbox_probe {role} {handle}"],
                capture_output=True, text=True, timeout=45,
            )
        except subprocess.TimeoutExpired:
            return ""
        if cp.returncode != 0:
            return ""
        lines = [ln for ln in (cp.stdout or "").splitlines() if ln.strip()]
        return lines[-1].strip() if lines else ""
    return landed_at(handle, root=_box_of(role))


def landed_snapshot(handle: str) -> dict[str, str]:
    """**跑之前**的落盘时间快照：每本收件箱 → 那份字节的 `staged_at`（不在则空串）。

    ⚠️ 必须在 `runtime.run()` **之前**取。判据 A′ 要的是「本轮落的」，而「本轮」
        这件事只能靠**前后对比**证明：样本是确定性合成的，字节每轮都一样，
        光看「在不在」分不出是这一轮落的还是上一轮留下的。
    """
    return {role: _landed_at_for(role, handle) for role in HOPS}


def landed_inboxes(handle: str, before: dict[str, str]) -> list[tuple[str, bool, str]]:
    """逐跳查：那份字节**本轮**有没有落到**每一本**收件箱里。

    ⚠️ 用 `landed_at()`（**只看盘**）而不是 `resolve()`（带**归属**校验）：
        要证的是「**送到了**」，不是「我这个进程**取得到**」。
        下游两跳是常驻服务，各自 bind 了自己的归属 ⇒ 探针进程去 `resolve()`
        **必然**被拒（2026-10-06 实测三本全拒，而字节三跳都在盘上）——
        那时这条判据在「下游是常驻服务」这个形态下**恒红**，与链通不通无关。

    ⚠️ 三态都要说清理由：判据只报「❌」而不说**为什么**，人就会退回去猜，
        而猜错方向的代价是一整轮真机（约 6 分钟）。
    """
    out: list[tuple[str, bool, str]] = []
    for role in HOPS:
        now_at = _landed_at_for(role, handle)
        was = before.get(role, "")
        if not now_at:
            out.append((role, False, "这份字节不在（或元数据对不上）"))
        elif now_at <= was:
            out.append((role, False, f"字节在，但落盘时间没变（{now_at}）—— 是**上一轮**留下的"))
        else:
            out.append((role, True, f"本轮落盘 {now_at}（上一轮是 {was or '无'}）"))
    return out


def dump_parts(title: str, parts: list[object]) -> None:
    """逐条打印消息的**部件构成**（照 `run/e2e_leader_image.py` 的口径）。

    ⚠️ wit 里**没有** `ToolCallPart` / `ToolResultPart`：工具回执是 `role="tool"`
       消息里的 `TextPart`。按部件类型判"模型调没调工具"在 wit 上恒假 ——
       已经因此制造过一次假阴性（"工具没被调用"），所以这里**以 role 为准**。
    """
    print(f"\n{BAR}\n  {title}\n{BAR}")
    for index, message in enumerate(parts, 1):
        role = getattr(message, "role", "?")
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            content = [content]
        bits: list[str] = []
        for part in content:
            if part is None:
                continue
            kind = type(part).__name__
            text = getattr(part, "text", None)
            if kind in ("ImagePart", "AudioPart", "VideoPart"):
                payload = getattr(part, "data", None) or b""
                bits.append(
                    f"{kind}(mime={getattr(part, 'mime_type', '?')}, "
                    f"{len(payload)} 字节)"
                )
            elif kind == "ReasoningPart":
                bits.append(f"Reasoning({len(text or '')} 字)")
            elif text:
                label = "工具返回" if role == "tool" else "Text"
                bits.append(f"{label}({len(text)} 字)")
            else:
                bits.append(kind)
        print(f"  [{index:02d}] {role:<10} {' + '.join(bits)}")


def output_text(parts: list[object]) -> str:
    """把 **output_parts** 里的 TextPart 拼起来。

    ⚠️ `output_parts` 是**部件列表**，不是消息列表 —— 它没有 `.content`（`messages` 才有）。
       按其中一种的结构去遍历另一种，只会得到空串**而且不报错**。
    ⚠️ 且它**同时含 ReasoningPart**（也有 `.text`）：不按类型过滤就会把模型的
       **内部思考**当成最终答复打出来 —— 读者会把草稿当结论。
    """
    return "\n".join(
        text
        for part in parts
        if type(part).__name__ == "TextPart"
        for text in [getattr(part, "text", None)]
        if isinstance(text, str) and text
    )


async def run_probe(
    runtime: object,
    parts: list[object],
    task: str,
    config: RunConfig,
    *,
    role: str = "leader",
    wit_dir: str | None = None,
):
    """跑这一轮 —— **必须经发起轮台账**，返回 `(run 结果, 台账记录)`。

    ⚠️ 与 `run/e2e_leader_image.py` 的 `run_probe()` **同签名**：`check_local` 3c 节
       用同一个驱动去驱动三个探针，签名不一致就得各写一份 —— 而"各写一份"
       正是那条判据要防的漂移。单拎成可 import 的函数是为了让自检塞一个**假 runtime**
       进来**行为断言**"这一轮真的记了台账"：判据不能退化成 grep 源码
       ——"grep 到了"并不等于"运行时真会走那条路"。
    """
    return await run_ledger.run_ledgered(
        lambda: runtime.run(parts, config=config),
        text=task,
        label=run_ledger.PROBE_LABEL,
        role=role,
        wit_dir=wit_dir,
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", action="store_true", help="不打长文本，只看结构")
    parser.add_argument(
        "--max-steps", type=int, default=0,
        help="覆盖步数预算（0 = 用 research_run_config() 的正式值）。"
             "⚠️ 传一个**比正式值小**的数就是本脚本的**负对照**："
             "用来证明判据 A′/B/C 里该失败的那几条**真的会失败**，而不是「永远绿」。",
    )
    args = parser.parse_args()

    data, mime, how = make_clip()
    marks = markers()
    # 判据 A′ 的比对目标：**由本模块现算**，不写死。
    # 用 `inbox.handle_for()` 而不是自己拼字符串 —— 句柄格式只有它一个定义处。
    handle = handle_for(data)
    task = task_text()

    print("=" * 70)
    print("  真机端到端 · 主控带音频（真实模型 / 真实附件 / 真实 ACPs 委派链）")
    print("=" * 70)
    print(f"  音频来源   ：{how}")
    print(f"  字节       ：{len(data)} 字节   MIME {mime}")
    print(f"  句柄       ：{handle}")
    print(f"  样本独有数字：{' / '.join(marks)}（判据 B / C 就找它们）")
    print(
        f"  内联上限   ：{MAX_INLINE_FILE_BYTES} 字节 = 0.5 MB"
        + ("  ⚠️ 超限，委派会发不出去！" if len(data) > MAX_INLINE_FILE_BYTES else "")
    )
    config_hint = (
        research_run_config()
        if args.max_steps <= 0
        else RunConfig(max_steps=args.max_steps)
    )
    print(
        f"  步数预算   ：{config_hint.max_steps} 步"
        f"（服务模式钉死 {RunConfig().max_steps} 步）"
        + ("  ⚠️ 已被 --max-steps 覆盖" if args.max_steps > 0 else "")
    )

    role = "leader"
    agent = build_agent(role, build_model(), self_aic=_active_aic(role))
    config = build_config(role=role)
    runtime = WitRuntime(agent, workspace=f".wit/{role}", acps=config)

    parts: list[object] = [TextPart(text=task), AudioPart(mime_type=mime, data=data)]

    print(f"\n{BAR}\n  交给主控的输入（进程内 run，不启动监听）\n{BAR}")
    print(f"  TASK 前 90 字：{task[:90]}…")
    print(f"  部件数      ：{len(parts)}（1 个 TextPart + 1 个 AudioPart）")

    before_verdict = newest_output("verdict")
    before_evidence = newest_output("evidence")
    # ⚠️ 判据 A′ 的**基线**：跑之前每本收件箱里那份字节的落盘时间。
    #    必须在跑之前取 —— 样本字节每轮都一样，没有基线就分不出「本轮落的」
    #    和「上一轮留下的」。（纪律：比对类判据必须先单独断言「基线存在」，
    #    否则「没得比」与「不一样」在输出里长得一模一样。）
    before_hops = landed_snapshot(handle)
    if not REMOTE_LEAF_SSH:
        print("  [A′·注] 未设 E2E_AUDIO_LEAF_REMOTE —— 第三跳（evidence-audio，"
              "已在第二台机器）将查本地旧目录，跨机部署后**必报已知假红**；"
              "设该变量可启用远端核对（两本地跳不受影响）")
    print(
        f"  [基线] 跑之前每本收件箱里那份字节的落盘时间："
        f"{ {r: (v or '无') for r, v in before_hops.items()} }"
    )

    print(f"\n{BAR}\n  正在跑……（预计 3~8 分钟；主控每 45 s 才会从 wait 里醒来一次）\n{BAR}")
    try:
        result, ledger_run = await run_probe(runtime, parts, task, config_hint, role=role)
    except Exception as exc:  # noqa: BLE001 - 全真跑要把失败原样打出来给人看
        print(f"  ❌ run 抛异常：{type(exc).__name__}: {exc}")
        return 1

    print(
        f"  [台账] run_id = {ledger_run.run_id}  label = {ledger_run.label}"
        f"  pid = {ledger_run.pid}（.wit/{role}/runs.jsonl，只增不改）"
    )
    print(f"\n  status = {result.status}   迭代 = {result.iterations} 轮")
    # ⚠️ 这一行**只如实报告、不作判据**：A′/B 转绿有两种可能 —— 模型自觉派了，
    #    或者闸门逼出来的。两者产物一样，但含义不同：后者说明提示词仍然没被
    #    模型当回事，链是靠**结构性闸门**才走的。判据只认产物，这一行留给人看。
    gate_blocks = (
        getattr(getattr(result, "state", None), "metadata", None) or {}
    ).get(BLOCKS_KEY, 0)
    print(
        f"  [闸门] 本轮拦了 {gate_blocks} 次终稿"
        + (
            " ⇒ 委派是**闸门逼出来的**，不是模型自觉派的"
            if gate_blocks
            else " ⇒ 模型自己就派了"
        )
    )

    dump_parts("消息部件构成", result.messages)

    print(f"\n{BAR}\n  工具调用轨迹\n{BAR}")
    for obs in result.observations or []:
        name = obs.get("tool") or obs.get("name") or "?"
        print(
            f"  · {name:<28} status={obs.get('status', '?'):<8} "
            f"{obs.get('duration_ms', '?')} ms"
        )
    # ⚠️ **失败原因必须打出来**。2026-10-06 那一轮的教训：工具轨迹里 9 次
    #    `acps_delegate` 全是 `status=error`，而 brief 模式只打了状态、没打原因
    #    ⇒ 只知道"派了 9 次都失败"，**不知道为什么**，只能靠猜（而猜了三次都错）。
    #    判据报红时**说不出为什么**的判据，等于把人推回猜的路上。
    errors = [o for o in (result.observations or []) if o.get("status") == "error"]
    if errors:
        print(f"\n{BAR}\n  失败调用的原因（{len(errors)} 条）\n{BAR}")
        for obs in errors:
            name = obs.get("tool") or obs.get("name") or "?"
            payload = obs.get("error") or obs.get("result") or obs
            try:
                text = json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001
                text = str(payload)
            print(f"  · {name}：{text[:600]}")

    print(f"\n{BAR}\n  主控最终文本\n{BAR}")
    body = output_text(result.output_parts)
    print(body if body else "  （本次没有文字输出）")

    # ── 判据 A′：附件**逐跳**落盘 ────────────────────────────────────────
    hops = landed_inboxes(handle, before_hops)
    crit_a = all(ok for _, ok, _ in hops)

    # ── 判据 B：音频技术线索交回了鉴证层 ──────────────────────────────────
    after_evidence = newest_output("evidence")
    evidence_fresh = (
        after_evidence is not None and after_evidence != before_evidence
    )
    evidence_data: dict = {}
    evidence_note = ""
    if evidence_fresh and after_evidence is not None:
        try:
            evidence_data = json.loads(after_evidence.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  （读 evidence 失败：{type(exc).__name__}: {exc}）")
        # 把整份报告拍平成一段文本：`findings[].detail` 才是检测器写的那句话，
        # 主控/鉴证层只是转述者。
        evidence_note = "\n".join(
            f"{f.get('check')} {f.get('verdict')} {f.get('detail')}"
            for f in (evidence_data.get("findings") or [])
        )
    ev_hits = marker_hits(evidence_note, marks)
    crit_b = evidence_fresh and any(ok for _, ok in ev_hits)

    # ── 判据 C：结论**随终稿交付**（被采纳）──────────────────────────────
    after_verdict = newest_output("verdict")
    verdict_fresh = after_verdict is not None and after_verdict != before_verdict
    verdict_data: dict = {}
    note = ""
    if verdict_fresh and after_verdict is not None:
        try:
            verdict_data = json.loads(after_verdict.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  （读 verdict 失败：{type(exc).__name__}: {exc}）")
        note = str(verdict_data.get("consistency_note") or "")
    vd_hits = marker_hits(note, marks)
    crit_c = verdict_fresh and any(ok for _, ok in vd_hits)

    # ── 判据 T：这一轮**有没有被 token 上限截断**（2026-10-06 真机夹出）─────
    #    背景：这一轮曾经"跑通了降级（422 = 0）却仍然零产物" ——
    #    `status=completed`、迭代 1 轮、只有 `Reasoning(31044 字)`、
    #    **零工具调用、零正文**。原因是响应被 `finish_reason=length` 截断：
    #    思考把 `DEFAULT_MAX_TOKENS=8192` 吃光 ⇒ `content` 没有额度。
    #    ⚠️ **空转的一轮与跑完的一轮长得一模一样**，所以这里必须有一条判据盯着。
    #    ⚠️ **三态**：`0` 干净 / `n>0` 截断过 n 次 / `-1` **无法判定**。
    #       只有 `0` 算通过 —— `-1` 不是"没截断"，**跳过 ≠ 通过**。
    truncated = truncated_of(result)
    crit_t = truncated == 0

    print(f"\n{BAR}\n  判据\n{BAR}")
    print(
        f"  [{'✅' if crit_a else '❌'}] A′｜附件逐跳落盘（且是本轮落的）："
        f"{sum(1 for _, ok, _ in hops if ok)}/{len(hops)} 本收件箱（文件名即 sha256）"
    )
    for hop_role, ok, why in hops:
        print(
            f"        {'✓' if ok else '✗'} .wit/{hop_role}/evidence-inbox/"
            f"{handle.split(':')[-1][:16]}…  {why}"
        )
    print(
        f"  [{'✅' if crit_b else '❌'}] B｜音频技术线索交回鉴证层："
        + (
            f"本轮新增 {after_evidence.name}，样本数字命中 "
            f"{[m for m, ok in ev_hits if ok]}"
            if evidence_fresh
            else "本轮**没有新的 evidence-*.json**（鉴证层没交出报告）"
        )
    )
    print(
        f"  [{'✅' if crit_c else '❌'}] C｜结论随终稿交付："
        + (
            f"本轮新增 {after_verdict.name}，样本数字命中 "
            f"{[m for m, ok in vd_hits if ok]}"
            "（数字是本批样本独有参数，猜不出来）"
            if verdict_fresh
            else "本轮**没有新终稿落盘** ⇒ 不得借用历史那份判通过"
        )
    )
    print(
        "\n  ⚠️ B 与 C 是**两件事**：B 只说明**鉴证层把报告交出来了**（送出去了）；"
        "\n     C 才说明**主控把它用进了交付物**（被采纳）。B ✅ / C ❌ 完全可能 ——"
        "\n     那时文件都在、看起来正常，但音频线索其实**没进终稿**。"
    )
    print(
        f"  [{'✅' if crit_t else '❌'}] T｜这一轮**没有被 token 上限截断**："
        + (
            "留痕显示 0 次 `finish_reason=length`"
            if truncated == 0
            else f"**被截断 {truncated} 次** ⇒ 模型没有额度把话说完（零产物的经典成因）"
            if truncated > 0
            else "**无法判定**（拿不到 `result.state.metadata`）⇒ 跳过 ≠ 通过"
        )
    )

    print(f"\n{BAR}\n  落盘产物\n{BAR}")
    if after_evidence is None:
        print("  evidence：⚠️ output/ 下没有任何 evidence-*.json —— 鉴证层这一轮没交报告")
    else:
        print(
            f"  evidence：{after_evidence}"
            f" {'（本轮新增）' if evidence_fresh else '（⚠️ 与跑之前同一份，本轮没落盘）'}"
        )
        if evidence_fresh:
            print(
                f"    suspicion={evidence_data.get('suspicion')}  "
                f"modality={evidence_data.get('modality')}  "
                f"findings={len(evidence_data.get('findings') or [])}  "
                f"not_run={len(evidence_data.get('not_run') or [])}"
            )
    if after_verdict is None:
        print("  verdict ：⚠️ output/ 下没有任何 verdict-*.json —— 这一轮没交出结构化结论")
    else:
        print(
            f"  verdict ：{after_verdict}"
            f" {'（本轮新增）' if verdict_fresh else '（⚠️ 与跑之前同一份，本轮没落盘）'}"
        )
        if verdict_fresh:
            print(
                f"    band={verdict_data.get('band')}  "
                f"truth_score={verdict_data.get('truth_score')}  "
                f"harm_index={verdict_data.get('harm_index')}  "
                f"is_incitement={verdict_data.get('is_incitement')}"
            )
            if not args.brief:
                print(f"\n  consistency_note（{len(note)} 字）：\n{note}")
        else:
            print(
                "  （本轮没落盘，故不打印上一轮的内容 —— "
                "打了就会有人把它当成这一轮的结果）"
            )

    return 0 if (crit_a and crit_b and crit_c and crit_t) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
