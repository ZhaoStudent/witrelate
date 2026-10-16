"""**真机端到端 · 主控带图** —— 把「证据链会不会响」与「14 步预算够不够」一起问出来。

为什么必须有它（两条待办合并；二者都只能被"带附件的真机一轮"回答）：

  #91  **证据链从未在真机上被触发过**。历轮全是纯文本，而主控提示词规定
       图像鉴伪分支要在「用户提供了图片」**且**「明确问有没有被改过 / 是不是拼接的」
       两个条件**同时**成立时才走 ⇒ 纯文本轮次里这条分支**结构上不可能亮**。
  #93  **14 步预算的额外容量从未被用上**。内层三级嵌套实测只要 ~170 s，
       主控 4 次 `acps_task(wait)` 就等到裁定书 ⇒ 8 步够用、第 9 步从未发生。
       图像这条分支会在内层三级嵌套**之外再加一跳**（证据鉴证 Leader → 图像鉴伪），
       把链拉长 ⇒ 才有机会观察到主控跨过 8 步。

它与 `run_agent leader --input` 的**唯一**区别是输入带 `ImagePart`：

    CLI 只有 `--input`（纯字符串）—— 附件**结构上进不去**；
    而 `runtime.run(parts)` 收部件列表，且**不启动监听**（进程内跑）。

所以不必新写一个"外部调用方 Agent"（`run/e2e_video_live.py` 走的就是这条路）。
⚠️ 但它仍然**必须用主控自己的身份**（出站委派要 mTLS + AIC）⇒ 跑之前**必须停掉**
   常驻的 `acps-leader.service`：两个进程共用 `.wit/leader`（同一份 AIP 任务库）会互相抢。
   ⚠️ 要 `systemctl stop`，**不是** `serve.sh stop` —— 单元是 `Restart=always`，
   kill 完 systemd 会立刻把服务拉起来，正好与本次运行抢工作区。

步数预算**取同一份单一事实源**（`run_agent.research_run_config()`），这里**不写第二个 14**：
写死就会漂移，而漂移的那一半恰好是没人盯着的那一半。

════════════════════════════════════════════════════════════════════════
判据怎么取（本章是本脚本唯一值得细读的部分）
════════════════════════════════════════════════════════════════════════

本脚本**只算两条判据**，两条都满足"从交付出来的值上能唯一判定"：

  A｜附件有没有进到主控
     取注入文本里有没有出现 `inbox:<sha256>`，而那个 sha256 就是**我本次发出去的
     字节的哈希**（由 `inbox.handle_for()` 现算，不写死）。
     它唯一的成立方式是：入站 Hook 真的拿到了那串字节并落了盘。
     ⚠️ 这条判据**不可能**被提示词或模型的措辞污染 —— 它比对的是密码学哈希。

  C｜#93 的额外预算有没有被用上
     `result.iterations > RunConfig().max_steps`（服务模式钉死的那个值，现读不写死）。
     超过它 = 这一轮**只可能**因为 CLI 路径那份更大的预算才跑得完。

而「证据链到底响没响」**故意不在本脚本里判**：
     「消息里出现了『证据鉴证 Leader』」这种字符串判据会**命中自身反面**（模型完全可以
      写一句"我未能联系上证据鉴证 Leader"），本项目已经因为这类判据误判过一次。
     权威判据在**任务库**里（`.wit/evidence-leader/acps/aip-tasks.sqlite3`）——
     有就是有、没有就是没有。那一步由脚本之外的审计做，见 docs 与
     `.workbuddy/memory/` 中 2026-10-04 的记录。

用法（在 witrelate 目录下）：
    ./.venv/bin/python -m run.e2e_leader_image
    ./.venv/bin/python -m run.e2e_leader_image --brief     # 不打全文，只看结构
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dotenv_lite import load_env

load_env()

from wit import ImagePart, RunConfig, TextPart, WitRuntime  # noqa: E402
from wit.acps.aip import MAX_INLINE_FILE_BYTES  # noqa: E402

from legal_acps import run_ledger  # noqa: E402
from legal_acps.acps_config import build_config  # noqa: E402
from legal_acps.agents import build_agent  # noqa: E402
from legal_acps.evidence.inbox import (  # noqa: E402
    InboxRefused,
    handle_for,
    inbox_root,
    resolve,
)
from legal_acps.llm import build_model  # noqa: E402
from legal_acps.run_agent import _active_aic, research_run_config  # noqa: E402
from legal_acps.token_budget import truncated_of  # noqa: E402

# ⚠️ `from run import samples as IMG` **刻意不放在模块级**：`run/samples.py` 会拉
#    numpy + Pillow，而本模块要能被 `legal_acps.check_local` **import 进来**
#    （3c 节用它驱动 `run_probe()`，从而行为断言"探针这一轮真的记了台账"）。
#    自检为了这一条把 numpy/Pillow 引进自己的进程是不划算的 —— 与
#    `legal_acps/evidence/__init__.py` 用 `if TYPE_CHECKING:` 挡住重依赖同一个理由。
#    ⚠️ 挪回模块级不会报错，只会让自检在缺 Pillow 的机器上**起不来** ⇒ 由 3c 断言盯着。

BAR = "─" * 70

#: 提问文案必须**同时**满足提示词里的两个触发条件：
#:   ① 用户提供了图片（由 `ImagePart` 满足）
#:   ② 明确要求"看看有没有被改过 / 是不是拼接的"
#: 少任何一条，图像鉴伪分支都不会走 —— 那这一轮就白跑了。
#: ⚠️ 第二段是给**标准流程**（三项判定）用的，提示词明确要求本分支不得替代它们。
def task_text(count: int) -> str:
    head = (
        "我收到一张别人转来的现场照片，对方说这是某商场促销活动的现场图。"
        if count == 1
        else f"我收到 {count} 张别人转来的现场照片，对方说都是某商场促销活动的现场图。"
    )
    per = "" if count == 1 else "请**逐张**分别给出结论，不要合并成一段。"
    return (
        f"{head}"
        "请帮我看看**这些图有没有被改过**、**是不是拼接的**，"
        "有没有复制粘贴或修图的痕迹。" + per + "\n"
        "另外请按你的标准流程，对随附这段文字做一次完整研判："
        "「网传某商场在促销活动中出现踩踏，现场照片是上周拍的，"
        "商场为了压消息把视频都删了。」"
    )


#: 单张样本的边长。**刻意取小**：AIP 的内联附件上限是
#: `wit.acps.aip.MAX_INLINE_FILE_BYTES = 0.5 MB`，而 720×540 的样本一张就 147 KB ⇒
#: 四张必然超限。多图这一路必须先过尺寸这道闸，否则连委派都发不出去。
SMALL_W, SMALL_H = 320, 240


def make_images(count: int) -> list[tuple[bytes, int]]:
    """造 `count` 张**互不相同**的复制-移动样本 → `[(字节, 水平位移), …]`。

    ⚠️ 必须**真的不同**：句柄是字节的 sha256，两张一样的图会给出同一个句柄，
       判据 A 就退化成"只证明了一张图进去了"。这里让**位移逐张不同**来制造差异。
    ⚠️ 位移必须取 **8 的整数倍**：图像检测实现以 8 像素为块步长做特征匹配，
       非整数倍的位移源块与目标块落不到同一格点上（这是实现已知限制，不是调参）。
    ⚠️ 返回位移是**刻意的**：判据 B 就靠它 —— 位移值是**我这批样本独有的参数**，
       模型不可能凭空写对，所以它在终稿里出现 ⇒ 报告确实回来了。见判据 B 的注释。
    """
    from run import samples as IMG  # 惰性：见文件顶部关于 numpy/Pillow 的说明

    if count == 1:
        # 单图沿用 720×540 的大样本（与既有 `probe_evidence` 同一批几何参数）
        return [(IMG.jpeg(IMG.copy_move_photo(), quality=95), 312)]

    box = (60, 124, 40, 104)          # y0,y1,x0,x1：块 64×64
    dxs = [96, 160, 208, 80][:count]  # 全为 8 的整数倍，且 104+208=312 ≤ 320
    out: list[tuple[bytes, int]] = []
    for dx in dxs:
        arr = IMG.photo("structured", w=SMALL_W, h=SMALL_H).copy()
        arr[box[0]:box[1], box[2]:box[3]] = arr[box[0]:box[1], box[2] + dx:box[3] + dx]
        out.append((IMG.jpeg(arr, quality=80), dx))
    return out


def dump_parts(title: str, parts: list[object]) -> None:
    """逐条打印消息的**部件构成**（照 `run/e2e_video_live.py` 的口径）。"""
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
                # ⚠️ wit 里**没有** ToolCallPart / ToolResultPart：工具回执是
                #    `role="tool"` 消息里的 TextPart。早先按部件类型判"模型调没调工具"
                #    在 wit 上恒假，制造过一次假阴性。这里以 role 为准。
                label = "工具返回" if role == "tool" else "Text"
                bits.append(f"{label}({len(text)} 字)")
            else:
                bits.append(kind)
        print(f"  [{index:02d}] {role:<10} {' + '.join(bits)}")


def cited_displacements(note: str, dxs: list[int]) -> list[tuple[int, bool]]:
    """终稿里有没有把**每张图自己的位移**写出来 —— 判据 B′ 的核心。

    为什么只认位移、不认"图像技术线索""copy_move"这类词：后者模型自己就能编，
    是**会命中自身反面**的那一类判据（本项目为这种判据误判过一次）。
    而 Δ=(96,0) 这种具体数字**只可能来自检测结果** —— 样本参数是本函数调用方
    现造的、没有任何地方写进提示词。

    ⚠️ 两种逗号都收（半角 `(96,0)` / 全角 `（96，0）`）：模型偶尔会写全角。
       这里只是"同一件事的两种排版"，放宽不会引入假阳性 —— 数字本身不可猜。
    """
    return [(dx, (f"({dx},0)" in note) or (f"（{dx}，0）" in note)) for dx in dxs]


def evidence_in_verdict(
    note: str, dxs: list[int], *, fresh: bool
) -> tuple[bool, list[tuple[int, bool]]]:
    """判据 B′：**本轮新落盘**的终稿里，逐张写了自己那条位移。

    ⚠️ `fresh` 这个条件是**负对照实测逼出来的**，不是设计时想到的：
       把预算压到 4 步那一轮，主控在第 4 轮被砍断、**没有**新 verdict 落盘
       （`status = budget_exhausted`、也没有文字输出），而 `newest_verdict()`
       老老实实返回了**上一轮**那份终稿 —— 于是 B′ 照着上一轮的产物判成 ✅。
       **判据借用历史产物 = 假阳性**，比没有判据更糟（它会替一次真实的失败背书）。
    """
    cited = cited_displacements(note, dxs)
    return (fresh and all(ok for _, ok in cited)), cited


def output_text(parts: list[object]) -> str:
    """把 **output_parts** 里的 TextPart 拼起来。

    ⚠️ `output_parts` 是**部件列表**，不是消息列表 —— 它没有 `.content`，
       而 `messages` 才有。按其中一种的结构去遍历另一种，只会得到空串、
       **而且不报错**。这是同一份代码里"两层结构长得像、其实是两回事"的典型陷阱。
    ⚠️ 且 `output_parts` 里**同时含 ReasoningPart**（它也有 `.text`）：
       不按类型过滤，就会把模型的**内部思考**当成最终答复打出来 ——
       读者会把草稿当结论。所以这里只认 `TextPart`。
    """
    return "\n".join(
        text
        for part in parts
        if type(part).__name__ == "TextPart"
        for text in [getattr(part, "text", None)]
        if isinstance(text, str) and text
    )


def newest_verdict() -> Path | None:
    """最新一份 `output/verdict-*.json`（**排除 `.meta.json` sidecar**）。

    ⚠️ 早先的取法是 `ls -t verdict-*.json | head -1`，它会先撞上几百字节的
    `.meta.json`（名字同样以 `verdict-` 开头）⇒ 判据 gsub 全 0、险些把
    "引用成功"误判成"没引用"。这里显式排除。
    """
    files = [
        p
        for p in Path("output").glob("verdict-*.json")
        if not p.name.endswith(".meta.json")
    ]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


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

    ⚠️ 为什么单拎成一个可 import 的函数（与 `run_agent.research_run_config()` 同一个
       理由）：只有做成函数，`legal_acps.check_local` 才能塞一个**假 runtime** 进来，
       **行为断言**"探针这一轮真的记了台账"。原实现是直接
       `await runtime.run(parts, config=…)`，跑完一轮后台账里**什么都没有**
       （2026-10-04 实测：跑完仍是 2 条），侧车也拿不到 run 身份 ——
       台账保留了它本来要消灭的那个盲区，只是位置从"人工轮 vs 自动复查轮"
       挪到了"人工/自动 vs 探针"。

    ⚠️ 判据**不能**退化成"grep 源码里有没有 `run_ledgered`"：本项目已经写死过这条
       ——"grep 到了"并不等于"运行时真会走那条路"。所以这里必须是个函数。
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
        "--count", type=int, default=1,
        help="附带几张图。>1 时用小尺寸样本（见 SMALL_W/SMALL_H 的注释）",
    )
    parser.add_argument(
        "--max-steps", type=int, default=0,
        help="覆盖步数预算（0 = 用 research_run_config() 的正式值）。"
             "⚠️ 传一个**比正式值小**的数就是本脚本的**负对照**："
             "用来证明判据 A′/B′ 会失败，而不是「永远绿」。",
    )
    args = parser.parse_args()

    samples = make_images(max(1, args.count))
    jpegs = [b for b, _ in samples]
    dxs = [dx for _, dx in samples]
    mime = "image/jpeg"
    # 判据 A' 的比对目标：**由本模块现算**，不写死。
    # 用 `inbox.handle_for()` 而不是自己拼字符串 —— 句柄格式只有它一个定义处。
    expected_handles = [handle_for(b) for b in jpegs]
    task = task_text(len(jpegs))

    print("=" * 70)
    print("  真机端到端 · 主控带图（真实模型 / 真实附件 / 真实 ACPs 委派链）")
    print("=" * 70)
    print(f"  图片张数   ：{len(jpegs)}")
    for i, (b, h, dx) in enumerate(zip(jpegs, expected_handles, dxs), 1):
        print(f"    #{i} {len(b):>7} 字节  位移 Δ=({dx},0)  句柄 {h}")
    print(f"  声明 MIME  ：{mime}（复制-移动篡改样本，真编码器产出）")
    total = sum(len(b) for b in jpegs)
    print(
        f"  内联合计   ：{total} 字节"
        f"（AIP 上限 {MAX_INLINE_FILE_BYTES} 字节 = 0.5 MB）"
        + ("  ⚠️ 超限，委派会发不出去！" if total > MAX_INLINE_FILE_BYTES else "")
    )
    config_hint = (
        research_run_config()
        if args.max_steps <= 0
        else RunConfig(max_steps=args.max_steps)
    )
    print(
        f"  步数预算   ：{config_hint.max_steps} 步"
        f"（服务模式钉死 {RunConfig().max_steps} 步；判据 C 就比这两个数）"
        + ("  ⚠️ 已被 --max-steps 覆盖" if args.max_steps > 0 else "")
    )

    role = "leader"
    agent = build_agent(role, build_model(), self_aic=_active_aic(role))
    config = build_config(role=role)
    runtime = WitRuntime(agent, workspace=f".wit/{role}", acps=config)

    parts: list[object] = [TextPart(text=task)]
    for i, b in enumerate(jpegs, 1):
        parts.append(ImagePart(mime_type=mime, data=b, name=f"scene{i}.jpg"))

    print(f"\n{BAR}\n  交给主控的输入（进程内 run，不启动监听）\n{BAR}")
    print(f"  TASK 前 90 字：{task[:90]}…")
    print(f"  部件数      ：{len(parts)}（1 个 TextPart + {len(jpegs)} 个 ImagePart）")

    before = newest_verdict()

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

    dump_parts("消息部件构成", result.messages)

    print(f"\n{BAR}\n  工具调用轨迹\n{BAR}")
    for obs in result.observations or []:
        name = obs.get("tool") or obs.get("name") or "?"
        print(
            f"  · {name:<28} status={obs.get('status', '?'):<8} "
            f"{obs.get('duration_ms', '?')} ms"
        )

    print(f"\n{BAR}\n  主控最终文本\n{BAR}")
    body = output_text(result.output_parts)
    print(body if body else "  （本次没有文字输出）")

    # ── 判据 ────────────────────────────────────────────────────────────
    #
    # ⚠️ 判据 A **曾经写错过一次**，记在这里以免再犯：
    #    初版是"注入文本里有没有出现 `inbox:<sha256>`"（在 `result.messages` 里找字符串）。
    #    单图那轮它返回 ✅；四图那轮它返回 ❌ —— 而**四张图其实全部处理了**
    #    （报告里逐张给出了位移 Δ=(96,0)/(160,0)/(208,0)/(80,0)，正是我这批样本的参数）。
    #    原因：句柄走的是 `context()` 注入的**系统块**，并且出站时会被
    #    `strip_handles` 抹掉 —— 于是"能不能在 messages 里找到句柄"取决于
    #    模型有没有在正文里复述它（单图那轮复述了，四图那轮改用文件名 scene1..4）。
    #    ⇒ **判据跟着模型的措辞走 = 不是判据**。已改为下面 A′（查磁盘）。
    #
    # A′｜附件落盘：句柄对应的字节文件**在收件箱里存在**。
    #     这是入站 Hook 干活的**直接后果**，与模型说什么无关。
    role_workspace = f".wit/{role}"
    box = inbox_root(Path(role_workspace))
    landed: list[bool] = []
    for h in expected_handles:
        try:
            resolve(h, root=box)
        except InboxRefused:
            landed.append(False)
        else:
            landed.append(True)
    crit_a = all(landed)

    # B′｜鉴伪结论**随终稿交付**：终稿的 `consistency_note` 里出现每张图**自己的位移值**。
    #     位移是**我这批样本独有的参数**（模型不可能凭空写对），所以它出现 ⇒
    #     报告确实回到了主控、并被写进了交付物。
    #     ⚠️ 只认"位移数字"这种不可猜的标记；不认"图像技术线索"之类措辞 ——
    #        那种词模型自己也能编，属于**会命中自身反面**的判据。
    #     ⚠️ 而且必须是**本轮新落盘**的那一份，见 `evidence_in_verdict` 的注释（负对照逼出来的）。
    after = newest_verdict()
    fresh = after is not None and after != before
    note = ""
    verdict_data: dict = {}
    if fresh and after is not None:
        try:
            verdict_data = json.loads(after.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  （读 verdict 失败：{type(exc).__name__}: {exc}）")
        note = str(verdict_data.get("consistency_note") or "")
    crit_b, cited = evidence_in_verdict(note, dxs, fresh=fresh)

    # C｜#93：这一轮有没有真正用到"超出服务默认"的那些步。
    #     ⚠️ 它**不**是 #93 的验收判据（见文件头）：正常一轮内层链只有 135~150 s，
    #        主控拿到材料就收尾，天花板根本够不到。这里只做**观察**用。
    service_default = RunConfig().max_steps
    crit_c = result.iterations > service_default

    # T｜这一轮**有没有被 token 上限截断**（2026-10-06 真机夹出，与音频探针同一条）。
    #    ⚠️ 为什么三条链都要装：那天是**音频链**先撞上的（reasoning 31044 字 ⇒
    #    `finish_reason=length` ⇒ 零产物），但"输入是降级后的长测量报告"这件事
    #    四条证据链**同形** ⇒ 只给被盯着的那条装，等于等下一条链再撞一次。
    #    ⚠️ 三态：`0` 干净 / `n>0` 截断 n 次 / `-1` **无法判定**；只有 `0` 通过。
    truncated = truncated_of(result)
    crit_t = truncated == 0

    print(f"\n{BAR}\n  判据\n{BAR}")
    print(
        f"  [{'✅' if crit_a else '❌'}] A′｜附件落盘收件箱：{sum(landed)}/{len(landed)} 份字节在磁盘上"
        f"（{box}，文件名即 sha256）"
    )
    if after is None:
        print("  [❌] B′｜鉴伪结论随终稿交付：output/ 下**没有**任何 verdict 文件，无从判定")
    elif not fresh:
        print(
            "  [❌] B′｜鉴伪结论随终稿交付：本轮**没有新终稿落盘**"
            f"（最新那份 {after.name} 是跑之前就有的）⇒ 不得借用它判通过"
        )
    else:
        miss = [dx for dx, ok in cited if not ok]
        print(
            f"  [{'✅' if crit_b else '❌'}] B′｜鉴伪结论随终稿交付："
            f"{sum(1 for _, ok in cited if ok)}/{len(cited)} 个样本位移出现在本轮终稿的 consistency_note 里"
            + (f" —— 缺 {miss}" if miss else "（位移值是本批样本独有参数，猜不出来）")
        )
    print(
        f"  [{'✅' if crit_c else '·'}] C｜（仅观察，非验收）本轮到第 {result.iterations} 轮收尾，"
        f"服务默认上限 {service_default} 轮"
        + ("→ 跨过了服务默认上限" if crit_c else "→ 天花板没够到，属正常")
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
    if after is None:
        print("  ⚠️ output/ 下没有任何 verdict-*.json —— 这一轮**没有**交出结构化结论")
    else:
        print(f"  verdict：{after} {'（本轮新增）' if fresh else '（⚠️ 与跑之前同一份，本轮没落盘）'}")
        if fresh:
            print(f"  band={verdict_data.get('band')}  truth_score={verdict_data.get('truth_score')} "
                  f"harm_index={verdict_data.get('harm_index')} "
                  f"is_incitement={verdict_data.get('is_incitement')}")
            if not args.brief:
                print(f"\n  consistency_note（{len(note)} 字）：\n{note}")
        else:
            print("  （本轮没落盘，故不打印上一轮的内容 —— 打了就会有人把它当成这一轮的结果）")

    # ⚠️ A′ / B′ 只打印、不进退出码（它们各自有"本轮新落盘"的前置条件，
    #     historical 那份会让它们常年红）。**T 必须进退出码** —— 被截断是
    #    「空转」的唯一可观测信号，不进退出码就等于没有这条判据。
    return 0 if crit_t else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
