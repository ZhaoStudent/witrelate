"""人机辩论驱动器（功能③ · 2026-10-07）。

用户（人）与正方 agent（debate-pro）逐轮对抗：
  · 用户每轮提交一段自由论证（交互输入，或 --script 文件逐行喂入）；
  · 驱动器把「事件 + 立场分配 + 前 n-1 轮全文 + 用户本轮论点」拼成输入，
    本地直跑正方 —— 它是叶子 agent，唯一工具 debate.submit_argument 纯本地
    交付，全程无出站委派；
  · 从 observation 里抽结构化论点（round / stance / confidence / key_point /
    rebuts 五字段），**先落台账再展示**，然后进入下一轮。

运行形态（2026-10-07 步骤 0 实证，run/_probe_debate_standalone.py）：
  · 不传 acps= 连编译都过不了（acps_skill=True 的工具强制要求 AcpsConfig）⇒
    必须传 acps=build_config(role="debate-pro")；
  · 但**不登录**（不调 up_until_ready / login / enroll）run() 照样完成，
    acps_status()=blocked 也不影响出论点 ⇒ 不登录 ⇒ 不碰 Registry 会话 ⇒
    与常驻服务**不会**互相顶掉登录（人机辩论随时可跑，不用错峰）；
  · 每轮一个**全新** runtime + 独立 workspace：无框架会话，历史全靠显式回放，
    跨轮状态不会泄漏，重试也不会踩上一次的残局。

终止三条件（先到先停，outcome 记进台账 close 行）：
  1. concede    —— 正方认输（给用户的诚实信号）；
  2. user_exit  —— 用户退出（交互输入 q/退出/EOF，或 --script 读完）；
  3. max_rounds —— 轮数上限（默认 6：最小完整辩论 2 轮 ×3 冗余 + 正方在
     长回放下的质量衰减边界；--max-rounds 可调，代价 = 耗时与 token 线性涨、
     回放越长正方越容易注意力稀释/复读）。
  另有 error（重试后仍交不出结构化论点）与 interrupted（Ctrl+C）—— 都留痕。

与裁定经验库的隔离（刻意设计，不是遗漏）：
  · 台账落 output/debates/<sid>.jsonl；RulingStore.load() 只 glob
    output/ruling-*.json（**非递归**）⇒ 辩词进不了经验库 —— 正方立场是
    **分配的**、未经裁定的对抗内容，当先例 = 污染判据
    （check_local ⑦-6 在 debates/ 里放同名毒饵盯着这层隔离）。
  · 唯一计入路径 = 阶段 2 把辩词交裁判出正式裁定书（ruling-*.json）。

用法（仓库根目录）：

    ./.venv/Scripts/python.exe -m legal_acps.human_debate                    # 交互
    ./.venv/Scripts/python.exe -m legal_acps.human_debate \\
        --event "网传……" --script my_args.txt --max-rounds 4                 # 脚本喂用户论点
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]

#: 会话台账目录（与 ruling-*.json 刻意分开，见模块 docstring 的隔离段）。
LEDGER_DIR = ROOT / "output" / "debates"
#: 正方 runtime 的 workspace 根：每轮（含每次重试）一个独立子目录。
WORKSPACE_ROOT = ROOT / ".wit" / "human-debate"

PRO_ROLE = "debate-pro"
TOOL_ID = "debate.submit_argument"
DEFAULT_MAX_ROUNDS = 6
DEFAULT_RETRIES = 1
DEFAULT_MAX_STEPS = 8
DEFAULT_PRO_STANCE = "该传闻属实"

#: 用户在交互模式下的退出词（大小写不敏感）。
EXIT_WORDS = {"q", "quit", "exit", "退出"}

#: user_source 的形态：给轮次，返回用户本轮论点；None = 用户退出。
UserSource = Callable[[int], "str | None"]
#: 每轮展示回调（CLI 传入；check_local 离线断言时不传）。
OnRound = Callable[[int, dict], None]


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ══════════════════════════════════════════════════════════════════════
# 会话台账：output/debates/<sid>.jsonl，append-only 单一事实源
# ══════════════════════════════════════════════════════════════════════


class DebateLedger:
    """一场人机辩论的台账：每行一条 JSON，**只追加、永不改写**。

    只存事实（用户原文、正方五字段、run 状态），不存中间结论 ——
    outcome 只在 close 一行里出现一次，且是驱动器**当场**判的。
    每次追加都独立 open/flush/close：进程中途被杀，已写的行不会丢。
    """

    def __init__(self, sid: str, directory: Path | None = None):
        self.sid = sid
        self.directory = Path(directory) if directory else LEDGER_DIR
        self.path = self.directory / f"{sid}.jsonl"

    def append(self, record: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": _now_iso(), **record}, ensure_ascii=False)
        # newline="\n"：Windows 上也保持纯 LF（同步纪律：read_bytes 数 CR LF）。
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")

    def open_session(self, *, event: str, pro_stance: str, max_rounds: int,
                     mode: str, retries: int) -> None:
        self.append({"type": "session_open", "sid": self.sid, "event": event,
                     "pro_stance": pro_stance, "max_rounds": max_rounds,
                     "mode": mode, "retries": retries})

    def round_record(self, round_no: int, user_argument: str, pro: dict,
                     run_status: object, iterations: object) -> None:
        """一轮成功记录：用户原文 + 正方五字段 + run 事实。

        `pro_round_match` 是事实（正方自报轮次 == 驱动器轮次），不是结论 ——
        模型报错轮次时驱动器逻辑仍用自己的计数器，台账里两边都留着。
        """
        self.append({"type": "round", "round": round_no,
                     "user_argument": user_argument, "pro": pro,
                     "pro_round_match": pro.get("round") == round_no,
                     "run_status": run_status, "iterations": iterations})

    def round_failed(self, round_no: int, user_argument: str, reason: str,
                     run_status: object, attempt: int) -> None:
        self.append({"type": "round_failed", "round": round_no,
                     "user_argument": user_argument, "reason": reason,
                     "run_status": run_status, "attempt": attempt})

    def close(self, *, outcome: str, rounds: int) -> None:
        self.append({"type": "session_close", "outcome": outcome,
                     "rounds": rounds})

    def evidence_note(self, records: list[dict]) -> None:
        """开辩前的证据核验记录（方案③）。**只存事实**（与 open/round 同纪律）：
        每个文件一条——名字、路径、状态、模态、等级、指纹、拒收原因；
        findings 逐条明细不进台账（brief 会随每轮输入全文回放，台账存
        索引级事实即可追源）。"""
        self.append({"type": "evidence", "files": [
            {k: rec.get(k) for k in
             ("name", "path", "status", "modality", "media_type",
              "suspicion", "sha256", "reason")}
            for rec in records
        ]})


# ══════════════════════════════════════════════════════════════════════
# 方案③（2026-10-18）：用户证据 · 本地核验 · 注入辩论（体验层）
# ══════════════════════════════════════════════════════════════════════
#
# 反诈体验的高光场景：用户拿一张"现场实拍图"当王牌证据甩出来，系统当场
# 做本地核验（确定性代码，不出网、不跑模型），把技术线索**如实**注入辩论
# 上下文——正方（分配了相反立场的辩手）据此驳斥。用户由此亲身经历
# 「我以为是铁证的图，其实是拼接的」——这正是"体验被欺骗的感觉"的本体。
#
# 设计边界（与主链同一条铁律，一处不松）：
#   · **只给线索，不定性**：注入的是 suspicion 等级 + 逐条 caveat，
#     绝不出现「证据是假的 / 主张不实」——那是裁判的活，不是核验的活。
#   · 核验**中立**：结论对用户不利（检出拼接）也照实注入——它不是
#     用户的助手，是辩论的公证人。
#   · **旁路纪律**：核验失败/拒收（文件读不了、认不出容器、超大小上限）
#     ⇒ 台账如实记 refused + 原因，brief 里如实写「未核验」，**绝不静默**，
#     也**不挡辩论**——没有证据的辩论本来就是合法形态。
#   · 与主链②（fact_store 落库）**刻意不合并**：体验层的核验不进事实库
#     （它是实验/演示通道，进库会污染生产检索面）；判据锚各自独立。
#   · 🔴 重依赖隔离：三个模态的 forensics 模块都是 numpy(+PIL) 重模块，
#     而 check_local 要能在缺它们的机器上导入本模块 ⇒ **所有 forensics
#     import 都必须在函数体内**（与 3c 的探针泄漏判据同一条纪律）。

#: 证据文件的大小上限。体验层面向"随手甩一张图/一段录音"，不是送检通道；
#: 上限挡的是「把整个电影扔进来把进程内存吃爆」。
EVIDENCE_MAX_BYTES = 32 * 1024 * 1024

#: 注入段标题与使用规则——**固定文案**（判据锚：渲染判据认这些句子）。
EVIDENCE_BRIEF_TITLE = (
    "【已核验证据技术线索（系统在开辩前对用户提交的媒体文件做的本地核验；"
    "以下是事实，不是任何一方的立场）】"
)
EVIDENCE_BRIEF_RULES = (
    "使用规则：① 技术线索只评价**证据载体本身**，不能单独证明或推翻"
    "事件主张的真伪；② 引用任何一条线索时必须连同它的 caveat 一起；"
    "③ 「未执行 / 无法判定」是「没查」，不是「查过没事」。"
)


def verify_evidence_file(path: Path) -> dict:
    """读一个证据文件 → 本地核验（确定性代码）。返回**只含事实**的记录。

    返回形态（status 三值）：
      · ``verified`` —— 有 modality / media_type / suspicion / rationale /
        sha256 / findings（逐条 check/verdict/detail/caveat）；
      · ``refused``  —— 读不了 / 太小 / 超上限 / 三模态都认不出容器，
        ``reason`` 是人话原因（调用方必须原样转述，不许静默吞）；
      · ``error``    —— forensics 抛了意外异常（reason 带异常名）。

    🔴 模态分诊的单一来源：**容器判定由各模态自己的 ``analyze()`` 做**
    （image → video → audio 依次尝试，认不出会抛 ValueError 自动降级），
    本函数不复制魔术字节表（复制了必漂移——检测层加新格式、这边还在拒）。
    唯一的自主逻辑是 MP4 家族按 ``ftyp`` 品牌预分流（``M4A`` 前缀 = 纯音频，
    其余 = 视频）：三个 sniffer 对 ``ftyp`` 的声明互相冲突（audio 侧全收、
    video 侧也全收），不预分流的话 MP4 视频会被 audio 侧截胡。
    """
    rec: dict = {"path": str(path), "name": Path(path).name}
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        rec.update(status="refused",
                   reason=f"读不了文件：{type(exc).__name__}: {exc}")
        return rec
    if len(data) < 12:
        rec.update(status="refused",
                   reason=f"文件只有 {len(data)} 字节，小到装不下任何媒体容器")
        return rec
    if len(data) > EVIDENCE_MAX_BYTES:
        rec.update(status="refused",
                   reason=f"{len(data)} 字节超过体验层上限 "
                   f"{EVIDENCE_MAX_BYTES}（正式送检请走线下司法鉴定通道）")
        return rec

    # MP4 家族预分流（见 docstring）；其余按 image → video → audio 降级。
    if data[4:8] == b"ftyp" and data[8:12].startswith(b"M4A"):
        order = ("audio",)
    else:
        order = ("image", "video", "audio")

    def _analyzer(modality: str):
        # 🔴 函数体内 import（重依赖隔离，见模块头方案③段）。
        if modality == "image":
            from legal_acps.evidence.forensics import analyze
        elif modality == "video":
            from legal_acps.video.forensics import analyze
        else:
            from legal_acps.audio.forensics import analyze
        return analyze

    refused_reasons: list[str] = []
    for modality in order:
        try:
            report = _analyzer(modality)(data, name=rec["name"])
        except ValueError as exc:
            # 该模态的容器判定认不出 ⇒ 换下一个（单一来源在 analyze 里）
            refused_reasons.append(f"{modality}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 —— 意外异常如实记，不挡辩论
            rec.update(status="error",
                       reason=f"{modality} 核验抛异常：{type(exc).__name__}: {exc}")
            return rec
        dump = report.model_dump(mode="json")
        rec.update(
            status="verified",
            modality=modality,
            media_type=dump.get("media_type") or "",
            byte_size=dump.get("byte_size") or len(data),
            sha256=dump.get("sha256") or "",
            suspicion=dump.get("suspicion") or "无法判定",
            rationale=dump.get("suspicion_rationale") or "",
            findings=[
                {"check": f.get("check"), "verdict": f.get("verdict"),
                 "strength": f.get("strength"), "detail": f.get("detail"),
                 "caveat": f.get("caveat")}
                for f in (dump.get("findings") or [])
            ],
        )
        return rec
    rec.update(status="refused",
               reason="三个模态的容器判定都认不出（"
                      + "；".join(refused_reasons) + "）")
    return rec


def render_evidence_brief(records: list[dict]) -> str:
    """把核验记录渲染成注入正方输入的「已核验证据技术线索」段。

    **纯函数（判据锚）**：输入只有记录 dict，输出只有字符串——
    check_local ⑦-6(7) 用假记录断言渲染形态，不碰真媒体文件。
    空记录 ⇒ 空串（= 辩论输入零变化，老路径完全不动）。
    """
    if not records:
        return ""
    lines = [EVIDENCE_BRIEF_TITLE]
    for i, r in enumerate(records, 1):
        if r.get("status") != "verified":
            lines.append(
                f"{i}. 文件 {r.get('name')}：**未核验**"
                f"（{r.get('reason') or '原因未记录'}）。"
                "它只能作为**未经检验的主张**参与辩论，不得当作已验证事实。"
            )
            continue
        sha = str(r.get("sha256") or "")
        lines.append(
            f"{i}. 文件 {r.get('name')}（{r.get('media_type')}，"
            f"{r.get('byte_size')} 字节，sha256={sha[:16]}…）"
        )
        lines.append(f"   综合线索等级：{r.get('suspicion')} —— {r.get('rationale')}")
        flagged = [f for f in (r.get("findings") or [])
                   if f.get("verdict") != "未提示异常"]
        for f in flagged:
            head = str(f.get("detail") or f.get("check") or "")
            lines.append(f"   · {head}（caveat：{f.get('caveat')}）")
        clean = len(r.get("findings") or []) - len(flagged)
        if clean > 0:
            lines.append(f"   其余 {clean} 项未提示异常"
                         "（阴性只说明未见信号，不能证明载体干净）。")
    lines.append(EVIDENCE_BRIEF_RULES)
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# 三个纯函数：判据的锚（check_local ⑦-6 / run/meta_verify_human_debate.py）
# ══════════════════════════════════════════════════════════════════════


def build_round_input(event: str, pro_stance: str, history: list[dict],
                      user_argument: str, round_no: int,
                      evidence_brief: str = "") -> str:
    """拼正方第 round_no 轮的输入：事件 + 立场 + **全部**历史回放 + 本轮论点。

    无框架会话 ⇒ 记忆全靠显式回放：history 里的每一轮（用户论点 + 正方
    key_point + 信心）都必须逐字进输入 —— 漏一轮，正方就当那轮没发生过。

    ``evidence_brief``（方案③，默认空串 = 输入与旧形态**逐字节一致**，
    老路径零变化）：``render_evidence_brief`` 的产出，插在立场之后、
    历史回放之前 —— 每轮都带（证据线索是**全程事实**，不是某一轮的论点，
    后加入场反而会被正方当成"临场掏出来的新东西"重新质疑一遍）。
    """
    parts = [f"【待辩论事件】\n{event.strip()}",
             f"你被分配的立场：**{pro_stance}**。"]
    if evidence_brief:
        parts.append(evidence_brief)
    if history:
        replay = []
        for h in history:
            replay.append(f"—— 第 {h['round']} 轮 ——")
            replay.append(f"【对方（用户）论点】{h['user']}")
            replay.append(f"【你方（正方）论点】{h['pro']}（信心 {h['confidence']:.2f}）")
        parts.append("【辩论历史（你的立场不变，逐轮回放）】\n" + "\n".join(replay))
    parts.append(f"【对方本轮（第 {round_no} 轮）论点】\n{user_argument.strip()}")
    tail = (f"请提交第 {round_no} 轮论证：调用 debate_submit_argument，"
            f"round={round_no}")
    if round_no == 1:
        tail += "（第 1 轮是立论，rebuts 留空）"
    else:
        tail += "，并针对对方论点作实质反驳（rebuts 填对方轮次编号）"
    parts.append(tail + "。")
    return "\n\n".join(parts)


def extract_pro_argument(observations) -> dict | None:
    """从 run().observations 抽正方结构化论点；抽不到 ⇒ None（= 轮失败）。

    口径（2026-10-07 真机实测的 observation 形态）：
      {toolCallId, tool, capabilityId, modelToolName, status,
       result{round, stance, confidence, key_point, rebuts}, error, …}
    · 只认 tool == debate.submit_argument **且** status == "success" 的调用
      （失败调用的 result 残渣不许被当成论点）；
    · 多条命中取**最后**一条（重试语义：最后一次成功才是交回的论点）；
    · 一条都没有 ⇒ None —— 模型只写散文 = 轮失败，由调用方记账并重试。
    """
    found: dict | None = None
    for o in observations or []:
        if o.get("tool") == TOOL_ID and o.get("status") == "success":
            r = o.get("result")
            if isinstance(r, dict):
                found = r
    return found


def round_decision(pro: dict, round_no: int, max_rounds: int) -> str | None:
    """这一轮之后辩论要不要停：'concede' | 'max_rounds' | None（继续）。"""
    if str(pro.get("stance") or "") == "concede":
        return "concede"
    if round_no >= max_rounds:
        return "max_rounds"
    return None


# ══════════════════════════════════════════════════════════════════════
# 正方调用与主循环
# ══════════════════════════════════════════════════════════════════════


async def _invoke_pro(text: str, *, model, sid: str, round_no: int,
                      attempt: int, max_steps: int):
    """本地直跑正方一轮：全新 runtime + 独立 workspace，**不登录 Registry**。

    运行约束的依据见模块 docstring（步骤 0 实证）。
    重度 import 放函数内：让本模块的纯函数部分不依赖 .env / wit 也能被
    check_local 离线导入。
    """
    from wit import RunConfig, WitRuntime

    from legal_acps.acps_config import build_config
    from legal_acps.agents import build_agent

    agent = build_agent(PRO_ROLE, model, self_aic=None)
    workspace = WORKSPACE_ROOT / str(sid) / f"r{round_no}a{attempt}"
    runtime = WitRuntime(agent, workspace=str(workspace),
                         acps=build_config(role=PRO_ROLE))
    return await runtime.run(text, config=RunConfig(max_steps=max_steps))


async def run_debate(event: str, *, pro_stance: str, max_rounds: int,
                     user_source: UserSource, ledger: DebateLedger,
                     model=None, invoke=None, retries: int = DEFAULT_RETRIES,
                     max_steps: int = DEFAULT_MAX_STEPS, sid: str = "",
                     mode: str = "interactive",
                     on_round: OnRound | None = None,
                     evidence_records: list[dict] | None = None,
                     on_input=None) -> str:
    """跑一场人机辩论，返回 outcome（close 行已同步写入台账）。

    可注入的三处（check_local ⑦-6 靠它们做**离线行为断言**，不碰真模型）：
      · invoke(text, round_no, attempt) -> run 对象 —— 默认 _invoke_pro；
      · user_source(round_no) -> str | None —— CLI 的交互/脚本两种来源；
      · on_input(round_no, text) —— 每轮**实际发给正方**的全文回调
        （方案③真机探针用它断言「注入被采纳」：brief 真的进了输入）。
    每轮顺序：用户论点 → 拼输入 → 正方（失败记账+重试）→ **先落台账** →
    展示 → 终止判定。中断（Ctrl+C）也走 finally 关台账。

    ``evidence_records``（方案③，默认 None = 行为零变化）：
    ``verify_evidence_file`` 的产出列表。开辩时渲染一次 brief、
    台账记一行 evidence（事实），此后**每轮**输入都带同一段 brief。
    核验失败/拒收不挡辩论（旁路纪律，见 verify_evidence_file）。
    """
    ledger.open_session(event=event, pro_stance=pro_stance,
                        max_rounds=max_rounds, mode=mode, retries=retries)
    if evidence_records:
        ledger.evidence_note(evidence_records)
    brief = render_evidence_brief(evidence_records or [])
    history: list[dict] = []
    outcome = "user_exit"
    rounds_done = 0
    try:
        round_no = 1
        while round_no <= max_rounds:
            user_arg = user_source(round_no)
            if user_arg is None or not str(user_arg).strip():
                outcome = "user_exit"
                break
            user_arg = str(user_arg).strip()
            text = build_round_input(event, pro_stance, history,
                                     user_arg, round_no,
                                     evidence_brief=brief)
            if on_input is not None:
                try:
                    on_input(round_no, text)
                except Exception:  # noqa: BLE001 —— 观测回调不许挡辩论
                    pass

            pro: dict | None = None
            reason = ""
            run_status: object = None
            run = None
            for attempt in range(1, retries + 2):
                try:
                    if invoke is not None:
                        run = await invoke(text, round_no, attempt)
                    else:
                        run = await _invoke_pro(text, model=model, sid=sid,
                                                round_no=round_no,
                                                attempt=attempt,
                                                max_steps=max_steps)
                    run_status = getattr(run, "status", None)
                    pro = extract_pro_argument(
                        getattr(run, "observations", None))
                    if pro is not None:
                        break
                    reason = f"正方未交出结构化论点（没有成功的 {TOOL_ID} 调用）"
                except Exception as exc:  # noqa: BLE001 —— 异常也要留痕再决定
                    run_status = f"exception:{type(exc).__name__}"
                    reason = f"{type(exc).__name__}: {exc}"
                ledger.round_failed(round_no, user_arg, reason, run_status,
                                    attempt)

            if pro is None:
                outcome = "error"
                break

            ledger.round_record(round_no, user_arg, pro, run_status,
                                getattr(run, "iterations", None))
            rounds_done = round_no
            if on_round is not None:
                on_round(round_no, pro)
            history.append({
                "round": round_no,
                "user": user_arg,
                "pro": str(pro.get("key_point") or ""),
                "confidence": float(pro.get("confidence") or 0.0),
            })
            decision = round_decision(pro, round_no, max_rounds)
            if decision is not None:
                outcome = decision
                break
            round_no += 1
        else:
            outcome = "max_rounds"
    except KeyboardInterrupt:
        outcome = "interrupted"
    finally:
        ledger.close(outcome=outcome, rounds=rounds_done)
    return outcome


# ══════════════════════════════════════════════════════════════════════
# 用户论点的两种来源
# ══════════════════════════════════════════════════════════════════════


def interactive_user_source() -> UserSource:
    """交互模式：每轮提示用户输入论点；q/退出/EOF = 退出辩论。"""

    def next_argument(round_no: int) -> str | None:
        print(f"\n轮到你（第 {round_no} 轮）：输入你的论点"
              f"（{ '/'.join(sorted(EXIT_WORDS)) } 结束辩论）")
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                return None
            if not line:
                continue  # 空行不给过，但也不当退出
            if line.lower() in EXIT_WORDS:
                return None
            return line

    return next_argument


def script_user_source(path: Path) -> UserSource:
    """脚本模式：文件逐行喂用户论点（# 开头为注释）；读完 = 用户退出。

    用途：真机对跑一轮完整辩论（交互 stdin 在无人值守会话里不可用）。
    """
    lines = [ln.strip() for ln in
             path.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]

    def next_argument(round_no: int) -> str | None:
        if round_no - 1 >= len(lines):
            return None
        return lines[round_no - 1]

    return next_argument


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════


def _read_multiline(prompt: str) -> str:
    print(prompt + "（可多行，单独一行 . 结束）")
    buf: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == ".":
            break
        buf.append(line)
    return "\n".join(buf).strip()


def _read_evidence_paths() -> list[Path]:
    """交互模式下问一次：有没有媒体证据要提交（方案③）。

    逗号/空格分隔多个路径；直接回车 = 没有证据（合法形态，辩论照跑）。
    路径读不读得了、认不认得出容器，由 ``verify_evidence_file`` 在
    开辩前逐个核验并如实报告——这里不做任何预判。
    """
    try:
        raw = input("媒体证据文件路径，多个用逗号分隔（图/音/视频；"
                    "系统会在开辩前做本地核验，技术线索同步给正方；"
                    "没有就回车跳过）：").strip()
    except EOFError:
        return []
    out: list[Path] = []
    for tok in raw.replace("，", ",").replace(" ", ",").split(","):
        tok = tok.strip()
        if tok:
            out.append(Path(tok))
    return out


def _print_evidence(records: list[dict]) -> None:
    """把核验结果如实打给用户看（含拒收原因——绝不静默）。"""
    if not records:
        return
    print("\n── 证据核验（本地 · 确定性代码，只给线索不定性）────────")
    for i, r in enumerate(records, 1):
        if r.get("status") == "verified":
            print(f"  {i}. {r['name']}：{r['media_type']}，"
                  f"综合线索等级 {r['suspicion']}（{r['sha256'][:16]}…）")
        else:
            print(f"  {i}. {r['name']}：**未核验** —— {r.get('reason')}")
    print("  （线索等级会连同逐条 caveat 注入辩论，正反双方都看得到）\n")


def _print_round(round_no: int, pro: dict) -> None:
    stance = str(pro.get("stance") or "?")
    label = "认输（concede）" if stance == "concede" else "坚持（hold）"
    print(f"\n── 第 {round_no} 轮 · 正方论点 ──────────────────────")
    print(f"立场：{label}　信心：{pro.get('confidence')}　"
          f"反驳轮次：{pro.get('rebuts') or []}")
    print(str(pro.get("key_point") or ""))


def main() -> int:
    from dotenv_lite import load_env

    load_env(ROOT / ".env")

    ap = argparse.ArgumentParser(
        description="人机辩论：你 vs 正方 agent（debate-pro，本地直跑不登录）")
    ap.add_argument("--event", help="待辩论事件描述（不给则交互输入）")
    ap.add_argument("--pro-stance", default=DEFAULT_PRO_STANCE,
                    help=f"分配给正方的立场（默认：{DEFAULT_PRO_STANCE}）")
    ap.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                    help=f"轮数上限（默认 {DEFAULT_MAX_ROUNDS}）")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                    help=f"每轮失败重试次数（默认 {DEFAULT_RETRIES}）")
    ap.add_argument("--script", type=Path,
                    help="用户论点脚本（每行一轮，# 注释；读完=退出）")
    ap.add_argument("--evidence", type=Path, action="append", default=[],
                    metavar="FILE",
                    help="媒体证据文件（可重复；开辩前本地核验，"
                         "技术线索注入辩论。交互模式不传则现场询问）")
    ap.add_argument("--sid", help="会话 id（默认现场生成时间戳）")
    args = ap.parse_args()

    event = (args.event or "").strip() or _read_multiline("请描述待辩论的事件：")
    if not event:
        print("没有事件描述，无法开题。")
        return 1

    sid = args.sid or _now_stamp()
    ledger = DebateLedger(sid)
    if args.script:
        user_source = script_user_source(args.script)
        mode = "script"
    else:
        user_source = interactive_user_source()
        mode = "interactive"

    # 方案③：证据核验在**开辩前**做一次（证据是全程事实，不是某一轮的论点）。
    evidence_paths = list(args.evidence)
    if not args.script and not evidence_paths:
        evidence_paths = _read_evidence_paths()
    evidence_records = ([verify_evidence_file(p) for p in evidence_paths]
                        if evidence_paths else [])
    _print_evidence(evidence_records)

    from legal_acps.llm import build_model
    model = build_model()

    print(f"会话 {sid}：你将与正方辩论 {args.max_rounds} 轮（上限）；"
          f"正方立场 = {args.pro_stance}；台账 = {ledger.path}")
    outcome = asyncio.run(run_debate(
        event, pro_stance=args.pro_stance, max_rounds=args.max_rounds,
        user_source=user_source, ledger=ledger, model=model, sid=sid,
        mode=mode, retries=args.retries, on_round=_print_round,
        evidence_records=evidence_records or None))

    print(f"\n辩论结束：outcome = {outcome}；台账 = {ledger.path}")
    if outcome == "error":
        return 1
    if outcome == "interrupted":
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
