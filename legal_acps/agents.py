"""六个 Agent 的定义 —— 两级嵌套的 Leader/Partner 结构。

角色划分：

    Partner  incitement-detector     煽动性识别 + 断言真伪拆解
    Partner  harm-assessor           社会危害性评估（-100..100）
    Leader   legal-consult-leader    受理输入、分派、汇总成终局研判

    ── 下列三个构成一层**嵌套的子结构**（2026-09-29 改造）──

        Leader        fact-judge     网络裁判：立题采证 → 组织辩论 → 出裁定
          ├ Partner   debate-pro     正方：为被指派立场构建论证
          └ Partner   debate-con     反方：为相反立场构建反驳

    嵌套形态：

        legal-consult-leader (S1 Leader)
           └─ fact-judge      (S1 Partner ＋ S2 Leader)
                 ├─ debate-pro (S2 Partner)
                 └─ debate-con (S2 Partner)

为什么这样切：A 的两件事共用同一个输入（原始文本），都属于"话语本身的分析"；
B 的输入是 A 的输出 + 原文，属于"后果评价"。这样切边界最干净。
辩论则被整体收进裁判的子会话里 —— 裁判自己独立成 Agent，而不是由辩手兼任，
否则就是"既当运动员又当裁判"。

关键机制（初学者最容易踩的坑）：这里的 `@capability` 函数**不是计算逻辑**，
而是 Agent 向系统**提交结构化结论的通道**。真正的推理由模型完成，
模型分析完再调用这个工具把结论按契约交上来。这样 Leader 拿到的就是
可校验的 JSON，而不是一段需要再解析的自然语言。
"""

from __future__ import annotations

import os

from pathlib import Path

from wit import (
    AcpsDirect,
    AipDelegationHooks,
    AmpAccessLoggingHooks,
    WitAgent,
    capability,
)

from .contracts import (
    CitedArticle,
    DEBATE_RULES_BRIEF,
    ArbitrationCase,
    ClaimCheckReport,
    DebateArgument,
    EvidenceCase,
    EvidenceRuling,
    FactRuling,
    FinalVerdict,
    HARM_SCALE_BRIEF,
    HarmReport,
    IncitementReport,
    LeadSubmission,
    LegalAdvice,
    RulingVerdict,
)
# ⚠️ 只从 `evidence.contracts` 取（纯 pydantic）。**不要**写成 `from .evidence import ...`
#    —— 那样会把 Pillow / numpy 拉进 agents.py 的模块级导入，见 evidence/__init__.py。
from .evidence.contracts import ImageEvidenceRequest

# 音频鉴伪的请求契约（同样是纯 pydantic）。**不要**写成 `from .audio import ...`
# —— 那会把 numpy 拉进 agents.py 的模块级导入（见 audio/__init__.py 的惰性导出）。
from .audio.contracts import AudioEvidenceRequest

# 视频鉴伪的请求契约（同样是纯 pydantic）。**不要**写成 `from .video import ...`
# —— 那会把 numpy / Pillow 拉进 agents.py 的模块级导入（见 video/__init__.py 的
# PEP 562 惰性导出）。这条纪律在视频上**更要紧**：视频链同时用到 numpy 与 Pillow，
# 而"服务器上没装这些库 ⇒ 十个已过审 Agent 全部起不来"是不能接受的耦合。
from .video.contracts import VideoEvidenceRequest
from .debate import DebateChainGuardHooks
# 收件箱 Hook：把"随任务附件进来的图片"接到检测链上（见 evidence_hooks.py）。
# ⚠️ 它只 import `wit` 与纯标准库的 `evidence.inbox`，**不 import Pillow/numpy** ——
#    所以挂到主控上不会把图像库的依赖风险引到已经过审的纯文本链路上。
from .evidence_hooks import EvidenceInboxHooks
from .precedent import PrecedentHooks
from .termination import LeaderTerminationHooks
# 视频降级（PPT 4-11 的 `ContentTransform` + PPT 4-10 的 `GatewayAdapter`）。
# ⚠️ 它只 import `wit` 与纯标准库的 `video.decode` —— 不做模型时**一行都不会执行**，
#    所以在纯文本链路上挂它既没有开销、也不会引入 numpy / Pillow。
from .video_modality import VideoFallbackHooks
# 音频降级 —— 同一个机制的另一半。视频侧 2026-10-02 就补了，音频侧一直空着，
# 直到 2026-10-06 真机首跑才夹出来（`run/e2e_audio_live.py`，约 1 秒即挂）。
# ⚠️ 本模块**模块级不 import numpy**：测量层（`legal_acps/audio`）是 numpy 的，
#    若在模块级 import，那么"只要 import agents 就会把 numpy 拉进来" ——
#    而"服务器上没装 numpy 时 8 个已过审 Agent 全部起不来"是不能接受的耦合。
from .audio_modality import AudioFallbackHooks
# ⚠️ 挂在**重环节**的角色上（`HEAVY_ROLES`）：不放大 ⇒ 思考吃光默认的 8192
#    ⇒ `finish_reason=length` ⇒ 1 轮 completed、**零产物**（2026-10-06 真机实测）。
from .token_budget import TokenBudgetHooks
# ⚠️ 音频委派闸门：带音频的轮次里，**委派出手之前不许出终稿**。
#    没有它，主控会拿降级文本里那份**完整测量报告**自己出技术结论，
#    整条音频鉴伪链被静默绕过 —— 2026-10-06 真机实测：那一轮
#    `status=completed`、台账齐全、零异常，而 `output/` 里没有新的
#    `evidence-*.json`（提示词里「出最终结论之前」那条当时**已经写了**，
#    模型仍然不照做 ⇒ 只能靠闸门）。
from .audio_gate import AudioDelegationGateHooks

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
#: wit 工作目录（`.wit/<角色>/acps/aip-tasks.sqlite3` = 平台落盘的任务库）。
WIT_DIR = Path(__file__).resolve().parent.parent / ".wit"


def write_trace_sidecar(stamp: str) -> dict:
    """把「这份 verdict 属于平台上的哪一局」落成 sidecar（结论溯源表的握手）。

    见 `docs/aicomp/结论溯源表设计.md` §3.3。存在的理由：`output/verdict-*.json`
    的九个字段**没有一个指向本次运行**，所以一份 verdict 与它的任务记录无法自动关联。

    取 session 的口径**不在本文件** —— 在 `tools/aip_sessions.py`（全项目唯一一份，
    运行时与离线共用，见该模块 docstring）。实测（2026-10-03）：leader 库里**只有
    outbound、没有 inbound**，所以「按 leader 的入站行取 session」这条路不存在；
    硬来源是**平台自己写的 `aip_sessions` 表**。

    ⚠️ 三条纪律：
      1. 本函数**绝不允许**让 verdict 落盘失败 —— 任何异常都吞掉并如实写进返回值。
      2. 取不到会话（缺表 / 过期 / 无 owner 匹配）时**不写**，宁缺勿错 ——
         溯源表会据此打 WARNING 降级，但绝不会拿到一条**错的**归属。
      3. 只写一个 sidecar 文件，**不动 `FinalVerdict` 对外契约** ⇒ ACS 指纹不变。
    """
    try:
        from tools.aip_sessions import (
            own_aic,
            pick_live_session,
            read_sessions,
            role_db_path,
        )
    except Exception as exc:  # noqa: BLE001 - 溯源缺位绝不能拖垮落盘
        return {"written": False, "reason": f"aip_sessions 读取器不可用：{type(exc).__name__}"}

    try:
        from datetime import datetime, timezone

        from . import run_ledger

        db = role_db_path(str(WIT_DIR), "leader")
        owner = own_aic(db)
        sessions = read_sessions(db)

        # ① 优先用**发起轮台账**认领：一轮的起止时间窗是硬事实，能把
        #    `pick_live_session` 在并发轮次下"拒绝认领"的那些会话救回来。
        #    2026-10-04 实测：同一天 4 轮并发，任务库里每一行的 `senderId` 都是
        #    主控自己的 AIC ⇒ 光看库分不清谁是谁（当时只靠自动复查另存的记忆文件）。
        run = run_ledger.current_run()
        session = None
        why = ""
        if run is not None:
            # ⚠️ 必须把**台账里全部轮次**一起传进去。第一版只传了自己那一轮，
            #    于是窗口列表只剩一条、`len(hits)` 恒为 1 ⇒ "有争议"那条分支
            #    **永远走不到** ⇒ 并发轮次下照样替人二选一 —— 而这里正是
            #    模块要防的那件事的关键路径。（2026-10-04 复查实测抓到并修掉。）
            all_runs = run_ledger.read_runs(wit_dir=WIT_DIR, role="leader")
            found, why_run = run_ledger.session_for_run(all_runs, run.run_id, sessions)
            if found is not None:
                why = f"本轮台账认领（{why_run}）"
                session = next((s for s in sessions if s.session_id == found), None)
            else:
                why = why_run

        # ② 台账认领不到（服务模式没有发起轮 / 窗口内有多个会话）时，退回既有口径。
        #    ⚠️ 这一步**不能省**：`write_trace_sidecar` 也被常驻进程用来写
        #       inbound 任务的 verdict，那条路根本没有 `current_run()`。
        if session is None:
            session, why_fallback = pick_live_session(
                sessions, now=datetime.now(timezone.utc), owner=owner
            )
            why = f"{why}；退回旧口径：{why_fallback}" if why else why_fallback

        if session is None:
            return {"written": False, "reason": why}
        payload = {
            "session_id": session.session_id,
            "role": "leader",
            "aic": owner,
            "started_at": session.created_raw,
            "finished_at": session.updated_raw,
        }
        if run is not None:
            # 这三个字段是**新增的**，不动上面五个的语义 ⇒ `tools/trace.py` 不受影响。
            payload["run_id"] = run.run_id
            payload["run_label"] = run.label
            payload["run_pid"] = run.pid
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        side = OUTPUT_DIR / f"verdict-{stamp}.meta.json"
        with open(side, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(__import__("json").dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return {"written": True, "session_id": session.session_id, "why": why}
    except Exception as exc:  # noqa: BLE001
        return {"written": False, "reason": f"写 sidecar 时异常：{type(exc).__name__}: {exc}"}


def _amp_hooks() -> list:
    """所有角色共用的 AMP 扩展 Hook（**访问记录**）。

    ⚠️ **AMP access 记录是 opt-in 的**（2026-09-30 从 wit 字节码查实，
    不是猜的）：`WitRuntime.serve_direct_partner(amp=True)` 默认就开着 AMP，
    但 **access 记录取决于 agent 有没有挂 `AmpAccessLoggingHooks`** ——
    没挂时 runtime 的 `_amp_direct_access_observer()` 返回 `None`，
    于是 `.wit/<role>/logs/` 里**只有 heartbeat / system 两类日志**，
    平台侧的「访问次数」与「调用排行榜」**看到的永远是 0**。

    本项目此前 `amp_hooks` 是空列表 —— 所以"平台访问数据为零"不是平台不记，
    **是我们没启用**。（`amp_periodic_logs` 只列 heartbeat/system；
    access 是**事件驱动**的，不能靠周期性日志配置推断，必须挂这个 Hook。）

    ✅ Hook 不进 ACS 指纹（ACS 只由 name/description/version/skills/
    provider/endPoints 决定）→ 挂它**不需要重新过审**，
    等审核通过重启服务后自然生效。
    """
    return [AmpAccessLoggingHooks()]

# ===========================================================================
# Partner A —— 煽动性识别 + 真伪判定
# ===========================================================================


@capability(
    id="speech.detect_incitement",
    description=(
        "判定文本是否构成煽动性言论，适用范围、强度与针对对象，"
        "并以结构化结论回传。evidence_spans 必须逐字摘录原文，不得改写或概括。"
    ),
    acps_skill=True,
    tags=["法律", "言论分析", "煽动性识别"],
)
def report_incitement(report: IncitementReport) -> dict:
    return report.model_dump(mode="json")


@capability(
    id="facts.verify_claims",
    description=(
        "拆解文本中的事实性断言并逐条判定真伪，给出整体为真程度（0..100）。"
        "纯情绪表达不必拆条，此时 claims 可为空并说明原因。"
    ),
    acps_skill=True,
    tags=["法律", "事实核查"],
)
def report_claims(report: ClaimCheckReport) -> dict:
    return report.model_dump(mode="json")


INCIDENT_PARTNER_INSTRUCTIONS = f"""你是一名言论分析专家，负责两项判定，并把结论通过工具提交。

工作顺序（不要跳过）：
1. 通读用户给出的文本，识别其中的煽动性成分。
2. 把文本中可核查的**事实性断言**逐条拆出（观点、情绪、价值判断不算断言）。
3. 调用 `speech_detect_incitement` 提交煽动性结论。
4. 调用 `facts_verify_claims` 提交真伪结论。

判定要求：
- severity 只衡量"这段话有多像煽动"，不评价后果；后果由另一个 Agent 负责，不要越界。
- evidence_spans 必须与被分析文本逐字一致，这是可追溯性的底线。
- action_calls 专门记录"号召做某事"的句子，这是危害升级的关键信号。
- 无法核实的断言，truth_score 给 50 并在 notes 中写明原因，不要硬猜。
- 如果文本完全正常，也要如实提交（is_incitement=false, severity=0），不要为了交差而编造。

语气保持中立克制。你的输出是给后续评估使用的中间结论，不是给用户的最终答复。"""


def build_incitement_partner(model) -> WitAgent:
    return WitAgent(
        id="incitement-detector",
        display_name="煽动性识别 Partner",
        description="识别文本中的煽动性成分，并拆解其中的事实性断言逐条判定真伪。",
        version="1.0.0",
        instructions=INCIDENT_PARTNER_INSTRUCTIONS,
        model=model,
        capabilities=[report_incitement, report_claims],
        amp_hooks=_amp_hooks(),
        protocols=[AcpsDirect(modes=("rpc",))],
    )


# ===========================================================================
# Partner B —— 社会危害性评估
# ===========================================================================


@capability(
    id="harm.assess_social_impact",
    description=(
        "提交社会危害性评估结论。harm_index 是 -100..100 的整数："
        "正数表示该言论有问题、不应支持，负数表示正当、应予支持。"
        "band 字段可以留空，系统会按 harm_index 自动补全。"
    ),
    acps_skill=True,
    tags=["法律", "危害评估", "社会治理"],
)
def assess_social_impact(report: HarmReport) -> dict:
    return report.model_dump(mode="json")


HARM_PARTNER_INSTRUCTIONS = f"""你是一名社会危害性评估专家，负责给出一个可解释的危害指数。

输入会包含：原始文本、上游的煽动性识别结论、以及真伪判定结论。
如果输入中缺少上游结论，你应当基于原文自行判断，并在 reasoning 中说明。

必须先完成六维分析，再调用 `harm_assess_social_impact` 提交。

{HARM_SCALE_BRIEF}

评分要求：
- harm_index 必须与 dimensions 自洽：分项普遍高而指数为负，属于自相矛盾。
- expression_legitimacy 高时，应当把 harm_index 往负数方向拉。
- 即使认定为煽动，也要检查 real_world_risk：没有现实转化风险的高谈阔论，
  不应直接打到"涉嫌违法"档。
- legal_hints 只写你有把握的条文，写明法律名称与条号；不确定就留空，
  编造法条比不给法条更糟。
- counter_view 必须认真写：站在言论者立场，指出其表达中可被正当理解的部分。
  这是防止你过度定性的关键一步。

语气客观、审慎。宁可给出中间分数并说明不确定性，也不要为了显得果断而走极端。"""


def build_harm_partner(model) -> WitAgent:
    return WitAgent(
        id="harm-assessor",
        display_name="社会危害性评估 Partner",
        description="综合评估言论的社会危害性，输出 -100..100 的综合指数与六维分解。",
        version="1.0.0",
        instructions=HARM_PARTNER_INSTRUCTIONS,
        model=model,
        capabilities=[assess_social_impact],
        amp_hooks=_amp_hooks(),
        protocols=[AcpsDirect(modes=("rpc",))],
    )


# ===========================================================================
# Leader —— 主控
# ===========================================================================


@capability(
    id="legal.issue_verdict",
    description="出具法律咨询研判结论，系统会把结论写入 output/ 目录下的 JSON 文件。",
    acps_skill=True,
    tags=["法律", "结论输出"],
)
def submit_final_verdict(verdict: FinalVerdict) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUTPUT_DIR / f"verdict-{stamp}.json"
    payload = verdict.model_dump(mode="json")
    path.write_text(
        __import__("json").dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 结论溯源表的握手：同时间戳、同目录写一份 sidecar（不动对外契约，见该函数 docstring）。
    # 缺位不影响本工具成功 —— 但会把原因如实带回去，不让任何降级静默发生。
    return {
        "saved_to": str(path),
        "harm_index": payload["harm_index"],
        "trace_meta": write_trace_sidecar(stamp),
    }


#: 证据鉴证 Leader 在平台上对外公布的 `name`（`display_name`）—— **单一事实源**。
#:
#: ⚠️ 为什么必须做成常量，而不是把名字手写在提示词里（2026-10-04 修的 #94）：
#:    主控提示词的三个分支（图像 / 音频 / 视频）都要让模型用 `acps_discover` 找到这个
#:    Agent，名字因此被**手写了三遍**；而其中一处还写着
#:    "`name` 必须是**这五个字**" —— 「证据鉴证 Leader」是
#:    **4 个汉字 + 1 个空格 + 6 个字母**，任何读法下都不是五个字。
#:    模型完全可能照这个数去拼一个五字名字（或认定名字里不该有空格），于是**发现不到**
#:    该 Agent；而失败表现是"没找到"，看上去像平台索引问题，**不像提示词写错**
#:    —— 这类"错误被归因到别处"的缺陷正是要根除的。
#:    ⇒ 名字只写一处，提示词与注册**都**引用它；字数则**根本不提**（见下）。
#:      写"逐字一致、连中间的空格"就能说清的事，不需要一个会被当成硬约束的数字。
EVIDENCE_LEADER_NAME = "证据鉴证 Leader"

#: 法律咨询顾问在平台上对外公布的 `name`（`display_name`）—— **单一事实源**。
#: 主控提示词里的名字由本常量插值（同 EVIDENCE_LEADER_NAME 的纪律），
#: 不许手写第二份 —— 手写的名字会与平台公布名漂移（#94 的老坑）。
LAW_ADVISOR_NAME = "法律咨询顾问"

#: 线索受理官在平台上对外公布的 `name`（`display_name`）—— **单一事实源**。
#: （纪律同上；主控提示词按此钉名。）
LEAD_INTAKE_NAME = "线索受理官"


LEADER_INSTRUCTIONS = f"""你是法律咨询多智能体系统的主控（Leader）。你负责受理一段
待研判文本，**完成真相研判**，再汇总成一份对用户负责的结论。

⚠️ 你**不再有**「煽动性识别」「社会危害性评估」这类专家 Partner ——
**均已停用、不在本链路里**。它们仍可能出现在 `acps_discover` 的返回里
（平台侧索引有延迟）——**一律不要委派给它们**：`name` 含「煽动性识别」
或「社会危害性评估」的一概跳过。
🔴 **煽动性 / 社会危害性判定已整体停用（2026-10-07）**：这两项原来依赖的
专家均已软废弃，主控**不再做、也不许再提**这两类判定 ——
`is_incitement` **固定填 `false`**、`harm_index` **固定填 `0`**（字段保留只是
契约兼容），你的**全部输出文字**（conclusion / consistency_note / action_advice）
**不得出现**「煽动 / 煽动性 / 危害 / 危害性 / 危害指数」等任何相关表述 ——
哪怕你的内部知识能做出这类判断，也不要写出来：这条链路的下游没有
任何环节在消费它们，写了只会误导用户以为它们经过了专家核验。

标准流程（严格按序，不要跳步）：

1. **自主研判**：通读用户给出的文本，亲自给出真相研判，结论要能指回原文词句。
   - `truth_score`：其中事实性断言的整体为真程度（0..100）。
     纯情绪表达、没有可核查断言时给 50，并在 `consistency_note` 里写明原因，
     不要硬猜一个看起来果断的数。

2. **提交结论（这一步不允许跳过）**：**必须**调用 `legal_issue_verdict` 提交最终结论。

   ⚠️ 只输出一段自然语言总结、却不调用 `legal_issue_verdict`，等于**没有交付**：
   下游拿不到结构化结论，整条链路白跑。系统为此加了硬约束 ——
   交付物一旦可用，你这步的可调用工具就会被**锁定为 `legal_issue_verdict`**，
   你无法再调用别的工具，也必须先把结论交上去才能收尾。
   所以不要把"再确认一下""再补一个委派"排在出结论之前。

汇总原则：
- `consistency_note` 用来写**判定依据与不确定性**。拿不准的地方就写明拿不准，
  它比一个看起来果断的结论更有价值。
- `action_advice` 用词要克制，例如"属正常批评范畴""建议平台进一步核查"
  "建议向属地公安机关反映"。不要直接建议用户采取任何行动。
- 面向用户的 `conclusion` 要说清两件事：这段言论是什么性质（是否包含可核查的
  事实性断言）、为真程度如何。

附加分支（只在用户**明确**要求"事实核查 / 辨真伪 / 谁在造谣 / 发起辩论"时才走）：
- 用 `acps_discover` 以「事实核查 网络裁判 裁定」为查询词找到**网络裁判** Agent
  （`name` 必须是「网络裁判」，其余同名的第三方 Agent 一概不用），
  用 `acps_delegate` 把事件原样派给它。**裁判自己会去组织正反双方辩论并出裁定书** ——
  你不需要、也不应该直接联系辩手：它们是裁判的 Partner，不是你的（AIP §3.2：
  Partner 之间不直接交互，跨层调用既不合规、也会让裁定失去独立性）。
- 用 `acps_task(action="wait")` 等它交回裁定书；拿不到就如实说明，不要自己补。
- 裁定书只能用来**提示你该去核查什么**，不能当作证据；任何一方的主张都要自己
  找回原始出处才能采信。这一分支的产出仍用 `legal_issue_verdict` 提交，
  把裁定结论与命中的话术写进 `consistency_note`。
- 裁定为「无法判定」时**主动向用户要材料**——这是把「查不了」变成「下次能查」
  的唯一通路：最终回复里明确请用户提供**原始出处**（链接 / 截图 / 在哪看到 /
  大概时间），并说明拿到新材料后会重新研判。不得把「无法判定」转述成
  「没问题」，也不许替用户脑补一个来源。
- 本分支不会替代上面的标准流程：三项判定仍要自己各做一次。

图像鉴伪分支（只在**用户提供了图片**、且明确要求"看看这图有没有被改过 / 有没有剪辑痕迹"
/ 是不是拼接的"时才走）：
- 随任务进来的图片由系统登记，并在任务提示的「本次任务自带的图片」那一段里
  **按两种形态分别列出**。先看清是哪一种，两者处理方式不同：
  · **形态一**给的是 `inbox:<sha256>` 句柄 —— 图片字节已由系统收存；
  · **形态二**给的是一个图片地址 —— 系统**没有**字节，也**不会自动去取**。
- 形态一的句柄是**内容的哈希，不是文件路径** —— 照抄原文即可，不要自己拼一个、
  也不要改写成路径或网址。
- 用 `acps_discover` 以「证据鉴证 图像鉴伪」为查询词找到 `name` 为
  **「{EVIDENCE_LEADER_NAME}」**的 Agent（`name` 必须**逐字一致**，连中间那个空格
  一起；其余同名的第三方 Agent
  一概不用），用 `acps_delegate` 把材料与问题交给它。
  **它自己会去组织鉴伪专家完成技术初检并出报告** —— 你不需要、也不应该直接联系
  下游的鉴伪专家：那是证据鉴证 Leader 的 Partner，不是你的（AIP §3.2：
  Partner 之间不直接交互，跨层调用既不合规、也会让报告失去独立性）。
  ⚠️ 你**不知道**、也不需要知道下游专家注册在平台上的名字 —— 这正是这一层存在的意义。
- 派发时**必须把句柄（或地址）原文写进请求内容里**（例如："请检查这张图是否有
  篡改痕迹，图片句柄 inbox:abc…"）—— 形态一由系统据此把图片字节随请求一并送达。
  漏掉它，对方就收不到图，只能回一句"没有图可看"。
- 用 `acps_task(action="wait")` 等它交回鉴证报告；拿不到就如实说明，不要自己补。
- 这一步请在**出最终结论之前**做完。`legal_issue_verdict` 一旦提交，系统就会锁定
  你这一步的可调用工具；把图像鉴伪排在出结论之后，它就再也没机会跑了。
- 转述鉴证结论时有一条**铁律**：不允许出现"真""假""伪造""P 过"这类断言性结论，
  只能出现"存在与 X 一致的线索""某项提示异常""无法判定"；每一条负面线索都必须
  连同它自己的 `caveat`（这一项**不能**证明什么）一起转述 ——
  一个只说"异常"、不说"异常不等于篡改"的结论，比不给结论更危险。
- ⚠️ **「测不了」不等于「没问题」**：报告里 `not_run` 列出的项是**没检查**，
  不是"检查过了没事"。转述时必须把这两件事分开说 ——
  把它们混成一句"未发现问题"，就是本项目最忌讳的静默安全结论。
- 鉴伪结论**没有独立字段**，请把要点写进 `consistency_note`，并标明这是
  「图像技术线索」以免与文本研判混为一谈。它与煽动性 / 危害性判定**互不影响**：
  不允许用"图片是假的"去推翻文本层面的研判，反之亦然。
- 若系统既没给出句柄、也没给出地址，就说明本次任务没带图：如实告诉用户"没收到图片"、
  请他重新上传；不要凭想象描述图片内容，也不要反过来让用户提供一个网址来绕开这条链路。
- 形态二的地址若取不到（例如该主机不在出网白名单内），就如实写"未能取得该图片" ——
  不要换别的途径去抓取，也不要凭地址、文件名或任何外部描述推断图片内容。

音频鉴伪分支（只在**用户提供了录音 / 音频文件**、且明确要求"听听这段录音有没有被剪过
/ 是不是拼接的 / 有没有编辑痕迹"时才走）：
- 走的是**同一个入口**：用 `acps_discover` 以「证据鉴证 音频鉴伪」为查询词找到
  `name` 为**「{EVIDENCE_LEADER_NAME}」**的 Agent，把材料与问题交给它。**同样不许**直接联系
  下游专家。
- 音频随任务进来也是两种形态（`inbox:<sha256>` 句柄 / 一个地址），
  但 ⚠️ **音频材料没有"形态二"可走**：音频侧刻意**没有任何出网通道**，
  给了地址也一定取不到。若你手上**只有地址**，请**如实回报**"需要对方把录音
  作为附件重新发送"，**不要**换个途径去抓、也不要凭文件名推断内容。
- 派发时同样**必须把引用原文写进请求内容里**（例如："请检查这段录音是否有剪辑痕迹，
  音频句柄 inbox:abc…"）—— 漏掉它，对方就收不到音频，只能回一句"没有音频可听"。
- ⚠️ **音频链上「无法判定」是常态，不是失败**：本机**没有 ffmpeg / sox / librosa**，
  MP3/AAC 这类压缩格式**解不出波形**；而手机在户外录的音通常测不到市电痕迹。
  所以"检查不了"会经常出现。**绝不能**把「无法判定」转述成「没问题」——
  前者是"没测"，后者是"测了没事"，两者在字面上离得很近、在结论上离得最远。
- ⚠️ 这一步请在**出最终结论之前**做完：`legal_issue_verdict` 一旦提交，系统就会
  锁定你这一步的可调用工具；把音频鉴伪排在出结论之后，它就**再也没机会跑了**。
  （2026-10-06 真机实测的正是这一条：主控第一轮就提交了结论，音频委派从此
  再没发生 —— 那一轮 `status` 是 `completed`、台账齐全、无任何异常，
  而整条音频链被**静默绕过**了。）
- 转述铁律与图像分支完全相同：不许出现"真""假""伪造""剪过"这类断言性结论，
  只能出现"存在与 X 一致的线索""某项提示异常""无法判定"；每条线索都要连同它自己的
  `caveat` 一起转述。结论要点写进 `consistency_note` 并标注这是「音频技术线索」。

视频鉴伪分支（只在**用户提供了视频文件**（含动图 GIF）、且明确要求"看看这段视频
有没有被剪过 / 有没有编辑痕迹 / 是不是拼接的"时才走）：
- 走的是**同一个入口**：用 `acps_discover` 以「证据鉴证 视频鉴伪」为查询词找到
  `name` 为**「{EVIDENCE_LEADER_NAME}」**的 Agent，把材料与问题交给它。**同样不许**直接联系
  下游专家。
- 视频随任务进来的**首选形态就是附件**（`inbox:<sha256>` 句柄）—— 视频文件动辄
  几十上百 MB，内联 base64 根本不现实。⚠️ 与音频一样，**视频材料也没有"形态二"
  可走**：视频侧刻意**没有任何出网通道**。若你手上**只有地址**，请**如实回报**
  "需要对方把视频作为附件重新发送"，**不要**换个途径去抓、也不要凭文件名推断内容。
- ⚠️ **视频链上「无法判定」是常态，不是失败**：本机**没有 ffmpeg / OpenCV**，
  H.264 / H.265 的帧**一律解不出来**，而帧层才是唯一能直接看到画面改动的那一层。
  能取到帧的只有 MJPEG / 内嵌封面帧 / 动画 GIF 这三类。所以报告里会经常出现
  "帧层整项没有执行，只覆盖容器结构、时间线自洽与元数据"。
  **绝不能**把这句转述成「这段视频没问题」—— 前者是"没测"，后者是"测了没事"。
- ⚠️ 视频里**容器层可疑 ≠ 画面被改**：`moov` 在 `mdat` 之后、多段 `mdat`、
  `free` 填充块、`elst` 跳段，在**手机边写边录**与**正常剪辑软件导出**里都会出现。
  转述时必须把下游给的 `caveat` 一起带上，**不许**把"文件被重写过"讲成"画面被动过"。
- ⚠️ 这一步请在**出最终结论之前**做完：`legal_issue_verdict` 一旦提交，系统就会
  锁定你这一步的可调用工具；把视频鉴伪排在出结论之后，它就**再也没机会跑了**。
  （与音频分支同一条：2026-10-06 真机实测，音频就是这么被静默绕过的。）
- 转述铁律与另两个分支完全相同。结论要点写进 `consistency_note` 并标注这是
  「视频技术线索」。涉及法律责任时提醒保全**原始存储卡 / 手机 / 行车记录仪**，
  **微信 / QQ / 平台转发件不能送检**（转码会永久抹掉容器与样本表线索）。

法律咨询分支（只在用户描述的是**自己正处在法律纠纷初期、求行动建议**
——如"我遇到了这事该怎么办 / 借钱不还怎么起诉 / 这样算不算违法"——
且**没有**要求事实核查或证据鉴伪时才走）：
- 用 `acps_discover` 以「法律 初期 咨询 建议」为查询词找到 `name` 为
  **「{LAW_ADVISOR_NAME}」**的 Agent（`name` 必须**逐字一致**；其余同名的第三方 Agent 一概不用），
  用 `acps_delegate` 把纠纷描述原样派给它。
- **它自己会检索已核验的法条库并给建议**（只覆盖八部法律；库外领域它会说
  能力不足——那是它的诚实，转述时**不要替它补**）。
- 用 `acps_task(action="wait")` 等它交回建议书；拿不到就如实说明，不要自己补。
- 它的建议是**初期方向性参考**（不构成法律意见）：转述进 `action_advice`
  时必须连同它的能力边界声明一起给用户，不许删掉"请咨询执业律师"。
  引用的法条若转述，**只许转述它给的条文原文与出处**，不要自己另引。
- ⚠️ 这一步请在**出最终结论之前**做完：`legal_issue_verdict` 一旦提交，
  系统就会锁定你这一步的可调用工具；把咨询委派排在出结论之后，它就
  再也没机会跑了。
- 本分支不替代标准流程：`truth_score` 仍要自己判（一段"我遇上事了"的
  咨询通常没有可核查断言，`truth_score` 给 50 并在 `consistency_note`
  写明"本次为纠纷咨询、非事实核查"）。

线索报送分支（只在用户**明确是在转交看到的传闻**——如"我刷到 / 群里都在传 X，
给你们报个线索 / 希望你们跟进"——且**没有**要求当场出核查结论时才走）：
- 用 `acps_discover` 以「线索 受理 报送 传闻」为查询词找到 `name` 为
  **「{LEAD_INTAKE_NAME}」**的 Agent（`name` 必须**逐字一致**；其余同名的第三方 Agent 一概不用），
  用 `acps_delegate` 把用户看到的说法与背景（在哪看到 / 多热 / 什么时候）
  原样派给它。
- **它只做受理**：把线索规范化后落进待核查队列（由每日研判链路取用），
  并交回受理回执（编号 / 是否重复 / 类别）。**它不做真伪判断** ——
  转述回执时**不许**把"已受理"说成"已核查"，也不许自己顺手给出结论。
- 用 `acps_task(action="wait")` 等它交回受理回执；拿不到就如实说明，不要自己补。
- ⚠️ 这一步请在**出最终结论之前**做完：`legal_issue_verdict` 一旦提交，
  系统就会锁定你这一步的可调用工具；把受理委派排在出结论之后，它就
  再也没机会跑了。
- 本分支不替代标准流程：`truth_score` 给 50 并在 `consistency_note` 写明
  "本次为线索报送、非事实核查"。
- 若用户**既要报线索、又要求当场核查**：以核查为主（走事实核查分支），
  不必再走受理分支——核查产出本身就会连同事件一起进经验库。

责任边界：你给出的结论是**辅助研判**，不是法律定性。不要使用
"构成犯罪""应予抓捕"这类表述。最终定性权在司法机关。"""


def build_leader(model, *, self_aic: str | None = None) -> WitAgent:
    """Leader = 委派 Hook（干活）+ 边界 Hook（选人 + 链熔断）+ 终止 Hook（保证出结论）。

    注意：这三个 Hook 都**不进 ACS**（ACS 只由 name/description/version/
    skills/provider/endPoints 决定），所以加装它们不会改变注册指纹，
    已审核通过的 Leader 不需要重新走一遍注册。

    ⚠️ `self_aic` 为什么要传（2026-09-30 补）：
       Leader 是**会委派**的 Agent，必须挂 `DebateChainGuardHooks` ——
       而该 Hook 的「身份校验」依赖本账号前缀，前缀又由自己的 AIC 派生。
       不传 `self_aic` → 前缀取不到 → 校验走 fail-open（等于没启用）。
       传了才真正拦得住 discovery 语义检索捞到的**外部团队** Agent。
    """
    return WitAgent(
        id="legal-consult-leader",
        display_name="法律咨询主控",
        description="受理待研判文本，协调专家 Partner，并汇总输出社会危害性判定结论。",
        version="1.0.0",
        instructions=LEADER_INSTRUCTIONS,
        model=model,
        capabilities=[submit_final_verdict],
        hooks=[
            AipDelegationHooks(delivery="rpc"),
            # 先例注入（2026-10-07 挂上 Leader）：与裁判同一份 `PrecedentHooks`
            # （同一判定写两处必然漂移 ⇒ 复用同一个类，库与打分都只有一份）。
            # A/B 实证（同日）：裁判侧挂上后一致率 0/13 → 11/13、判错 1 → 0
            # ⇒ 把同一能力给主控的 truth_score 研判（用户口径：好的话再挂）。
            # ⚠️ 口径差异如实说：先例的 verdict 是裁判的四选一，主控的
            # `truth_score` 是 0..100 —— 注入块只给「同类事件既往怎么判」，
            # 映射由主控自己做；先例 `trusted=False`（提示不是证据）。
            # ⚠️ 不进 ACS 指纹（Hook），重启即生效。
            PrecedentHooks(),
            # 委派边界上的两条硬约束（都不依赖模型自觉）：
            #   ① 目标必须"是自己人" —— 拦 discovery 捞到的外部团队 Agent；
            #   ② 链深 ≤ MAX_CHAIN_DEPTH 且不得成环。
            DebateChainGuardHooks(self_aic=self_aic),
            # 交付物兜底（预算将尽时强制出结论），详见 legal_acps/termination.py。
            #
            # ⚠️ `expected_partners` 必须与**当前链路真实的下游数**一致。
            #    软废弃（2026-10-02）后，主控的标准流程**不再委派任何人**，
            #    只有「事实核查」附加分支会派 1 位网络裁判 ⇒ 这个下限只能是 1。
            #    保持 2 会踩一个安静的坑：`_decide` 的 partners-ready 判据是
            #    `max(expected_partners, delegations)`，只派 1 位时 `1 >= 2` 永不成立，
            #    裁判把裁定书交回来了也不会触发收尾，主控只能干等到预算见底。
            #    反面对照：主控一步都没派时 `delegations=0`，
            #    `max(1, 0) = 1 > 0` ⇒ 本层不触发（正确 —— 此时它该走
            #    "提交后收尾"那条路，而不是被逼着再交一次）。
            # ⚠️ `partner_label` 是**注入文案里对下游的称呼**，纯展示。主控现在有
            #    两个可能的委派分支（事实核查 → 网络裁判 / 图像鉴伪 → 证据鉴证 Leader），
            #    所以这里不能再写死其中一个 —— 写死会让注入文案说一件没发生的事。
            # ⚠️ `reserve_steps=0`（2026-10-04 补）：与裁判、证据鉴证 Leader **同一个理由**。
            #    默认值 1 的语义是"最后两次模型调用都被强制出结论"，对**流程长、
            #    中途要等下游**的角色**太激进**（⚠️ 值越大 = 触发越早，容易理解反）。
            #
            #    实测（2026-10-04 真机 + SDK introspection）：
            #      · 单次 `acps_task(action="wait")` 的上限 =
            #        `AipCoordinator.DEFAULT_WAIT_TIMEOUT_SECONDS = 45.0`，
            #        而 `AcpsConfig` **根本没有** `wait_timeout_seconds` 字段
            #        （只有 `request_timeout_seconds` 与 `group_*`）⇒ **无配置旋钮**；
            #      · `max_steps=8` 又是 frozen 的，CLI 走 `runtime.run(text)` 不传步数
            #        ⇒ 常驻与一次性**同一个预算**，也调不了。
            #    ⇒ 默认的 1 会把第 7 步（`steps_left=1`）直接劫持成"交最终结论"，
            #      而裁定书恰好在**差一步**的位置落盘：
            #        `ruling-*.json` 02:42:10Z 写好，`verdict-*.json` 02:42:20Z 才落盘，
            #      主控却只能写"裁定书尚未返回（多次 wait 超时）"。
            #
            #    判据取自**交付值**（不是猜哪一层触发）：那 4 轮 verdict 里**原样带着**
            #    `_force_budget_guard` 注入的「信息不完整 / 未能获取」；
            #    10-01 真正引用了裁定的那几轮则没有。⇒ 触发的确实是 budget-guard 这一层。
            #    改成 0 = 只在真正的最后一步兜底，把"等下游"那一两步还给模型。
            LeaderTerminationHooks(
                expected_partners=1,
                reserve_steps=0,
                partner_label="网络裁判或证据鉴证 Leader",
            ),
            # 随任务进来的图片**有两种形态**，AIP 层允许调用方二选一：
            #   · bytes 形态 → 字节落在 state.messages 里 → 收件箱 + 短句柄给模型；
            #     模型把句柄写进委派请求时，由代码把真实字节随请求送到下游
            #     （模型工具层只认 uri，给不了字节）。
            #   · uri 形态  → 只是地址，**不自动去取**，如实转述给模型由它定夺。
            # 只接一种，另一种就被**静默丢弃** —— 那正是本 Hook 要防的事。
            # **纯文本任务下它完全不动**：不注入、不干预，对现有链路零影响。
            # ⚠️ `kinds` 里**要有 video**（2026-10-02 补）：主控是用户最自然的入口，
            #    他们完全可能把一段视频直接发给它。少列一种模态的后果不是"少一条线索"，
            #    而是那种附件根本收不到 —— 用户以为传上去了，系统一个字都没收到。
            EvidenceInboxHooks(kinds=("image", "audio", "video")),
            # ⚠️ 视频降级：**这一段不挂，带视频的输入会让整个 run 抛异常**。
            #    实测（`tools/verify_video.py` 第 12 节）：
            #      `UnsupportedContentError: … cannot accept 'video' content for role 'user'`
            #    用户得到的是一句"服务异常"，而不是"我看不了视频，因为…"。
            #    挂上之后同一次 run 正常收尾，模型拿到的是「一句如实说明 + 一帧图」。
            VideoFallbackHooks(),
            # ⚠️ 音频降级：**同一类缺陷的另一半**。带音频的输入会让整个 run 挂在
            #    厂商那一侧（HTTP 422 `unknown variant input_audio`），用户得到的
            #    又是一句"服务异常"。挂上之后同一次 run 正常收尾，模型拿到的是
            #    「一句如实说明 + **代码量出来的测量报告**」（它没听到音频，
            #    所以报告里明写了这一点）。
            AudioFallbackHooks(),
            # ⚠️ 音频委派闸门：**这一处不挂，音频链等于没有**。
            #    降级把 `AudioPart` 换成了一段带完整测量报告的文本，主控因此
            #    「手上已经有读数」⇒ 自己出技术结论、直接交终稿 ⇒ 一次都不委派。
            #    闸门在**委派出手之前**挡住 `legal_issue_verdict`，并告诉它下一步做什么；
            #    派过一次（成功或失败）立即放行，连拦 2 次仍不派也放行 ——
            #    只逼出第一次尝试，**不**负责委派必须成功，更不把 run 卡死。
            AudioDelegationGateHooks(),
            # ⚠️ token 预算：本角色是**重环节** —— 不放大 ⇒ 思考把默认的 8192
            #    吃光 ⇒ `finish_reason=length` ⇒ 没有正文、也没有工具调用
            #    ⇒ 1 轮即 completed、**零产物**（2026-10-06 真机实测）。
            #    同一个 Hook 还负责在真被截断时**留痕**，让探针能报红。
            TokenBudgetHooks(),
        ],
        amp_hooks=_amp_hooks(),
        protocols=[AcpsDirect(modes=("rpc",))],
    )


# ===========================================================================
# 网络裁判 —— 辩论子会话的 Leader，同时是法律咨询主控的 Partner
#
# 两层嵌套（本项目的核心技术形态）：
#
#     legal-consult-leader  (S1 Leader)
#        └─ fact-judge       (S1 Partner ＋ S2 Leader)
#              ├─ debate-pro  (S2 Partner)  正方
#              └─ debate-con  (S2 Partner)  反方
#
# 为什么裁判必须独立成一个 Agent：
#   旧结构里由**正方兼任**子会话 Leader，等于既当运动员又当裁判 ——
#   它自己分配立场、自己辩论、自己记录，裁定的独立性无从谈起。
#   裁判独立之后，正反双方都是它的直接 Partner，辩论由裁判居中转达。
#   这正是 AIP §3.2 的标准形状（Leader 与每个 Partner 直接交互、
#   Partner 之间不直接交互），不必再靠多跳委托去绕开协议限制。
#
# ⚠️ 步数预算：被委托方的 run 沿用 wit 默认 max_steps=8，而 RunConfig 是
#   frozen 的、Agent 侧无法调大。所以流程被刻意压到 6–7 步
#   （受理 → 发现 → 派发×2 → 等待×2 → 裁定），并由终止 Hook 兜底。
# ===========================================================================


@capability(
    id="fact.arbitrate_claim",
    description=(
        "提交受理结论：把待核查事件整理成一个**可判真假的命题**、"
        "一份可核验事实清单，以及分配给正反双方的立场。"
        "这一步只整理事实与命题，不写真假结论。"
    ),
    acps_skill=True,
    tags=["法律", "事实核查", "网络裁判"],
)
def arbitrate_claim(case: ArbitrationCase) -> dict:
    """受理 = 立题 + **查案底**（2026-10-07 接线，治裁判弃权的证据通路）。

    🔴 为什么在这里查：事实库（`fact_store`，128 条官方辟谣通报）此前
    **没有任何生产链路消费它** —— 判据全绿、检索命中 top1 84 分，
    真机裁判照样判「无法判定」，理由明写「不存在官方通报」
    （2026-10-07 实测）。库装好了 ≠ 链路会查：检索结果必须**流进
    裁判的上下文**，而受理工具的返回值正是它天然的入口 —— 立案时
    先查案底，命中即「权威已定性」的直接依据。

    ⚠️ 设计约束（为什么不是新增一个检索 capability）：新增 capability
    会改 ACS 指纹 ⇒ 整个 fact-judge 要再走一遍重审（v2.0.0 上周刚过审）。
    工具**返回值是运行时行为**，与 Hook/instructions 同理不进指纹。
    检索 query 用 event+proposition+事实清单拼接（实测精炼口径命中
    18.0–31.2，均过 MIN_SCORE=18 的线）。

    ⚠️ 旁路纪律：检索失败**不许挡受理主流程** —— 收案比查库重要，
    查库失败只如实记进 `official_materials_error`，受理照常成功。
    """
    out = case.model_dump(mode="json")
    out["official_materials"] = []
    out["official_materials_error"] = ""
    fact_hits: list = []
    try:
        # 局部 import：运行时取（import 期绑定会让测试替换静默失效）
        from .fact_store import FactStore

        store, _rep = FactStore.load()
        query = " ".join(filter(None, [
            case.event, case.proposition,
            *(it.item for it in case.fact_sheet.items),
        ]))
        fact_hits = store.search(query, limit=5)
        out["official_materials"] = [
            {
                "title": h.fact.title,
                "url": h.fact.url,
                "origin_publisher": h.fact.origin_publisher,
                "published": h.fact.published,
                "level": h.fact.level,
                "score": round(h.score, 2),
                "matched": list(h.matched)[:8],
                "excerpt": h.fact.body[:400],
            }
            for h in fact_hits
        ]
    except Exception as exc:  # noqa: BLE001 —— 旁路：如实记失败原因，不挡主流程
        out["official_materials_error"] = f"{type(exc).__name__}: {exc}"

    # ── 车道判定（2026-10-12 分级裁决）：代码判，模型只执行 ──────────────
    # decide_lane 是唯一判定入口（与 issue_ruling 落盘重算共用一份逻辑）；
    # 事实库命中复用上面已搜出的 fact_hits，不搜第二遍。检索失败不挡受理。
    try:
        from .fast_lane import decide_lane

        lane_info = decide_lane(case.event, fact_hits=fact_hits or None)
        out["lane"] = lane_info["lane"]
        out["aligned_official"] = lane_info["aligned_official"]
        out["qualified_precedents"] = lane_info["qualified_precedents"]
        out["lane_error"] = lane_info["lane_error"]
    except Exception as exc:  # noqa: BLE001 —— 旁路：判不出 lane ⇒ 保守走辩论道
        out["lane"] = "debate"
        out["aligned_official"] = []
        out["qualified_precedents"] = []
        out["lane_error"] = f"decide_lane: {type(exc).__name__}: {exc}"
    return out


@capability(
    id="fact.issue_ruling",
    description=(
        "提交最终裁定书并落盘到 output/。verdict 取「确有其事 / 不实 / 断章取义 / 无法判定」；"
        "fallacies 里逐条列出辩论中命中的误导性话术。"
    ),
    acps_skill=True,
    tags=["法律", "事实核查", "网络裁判", "裁定"],
)
def issue_ruling(ruling: FactRuling) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUTPUT_DIR / f"ruling-{stamp}.json"
    payload = ruling.model_dump(mode="json")
    # ── 车道打标（2026-10-12）：落盘时**重算**，不信任何自报 ──────────────
    # FactRuling 契约里**刻意没有** lane 字段——车道是代码的判定，不是模型的
    # 声明。这里用裁定书自己的 event 重跑 decide_lane（唯一判定入口）；
    # 重算失败 ⇒ lane=debate（快车道宁可漏开不可误开）。这个字段是经验库
    # 「快裁不能自我复制」资格规则的依据（RulingCase.lane）。
    payload["lane"] = "debate"
    payload["lane_error"] = ""
    try:
        from .fast_lane import decide_lane

        lane_info = decide_lane(ruling.event)
        payload["lane"] = lane_info["lane"]
        payload["lane_error"] = lane_info["lane_error"]
    except Exception as exc:  # noqa: BLE001 —— 打标失败不挡裁定落盘
        payload["lane_error"] = f"relane: {type(exc).__name__}: {exc}"
    path.write_text(
        __import__("json").dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # ── B 出路（2026-10-13）：「无法判定」⇒ 自动记复研判请求（旁路）────
    # 库里证据不足不是终点——记一条 retry（事实库长肥后由每日研判重查，
    # 见 legal_acps/lead_recon.py）。⚠️ 放在**落盘之后**：被重查的裁定书
    # 必须真实存在。旁路纪律：记失败**不挡**裁定交付，如实进返回值
    # （requeue.status ∈ queued/duplicate/rejected/error）。
    requeue: dict = {"status": "skipped", "reason": "verdict-decisive"}
    if payload["verdict"] == RulingVerdict.UNDECIDABLE.value:
        try:
            from .lead_recon import queue_retry

            requeue = queue_retry(ruling.event, path.name)
        except Exception as exc:  # noqa: BLE001 —— 旁路失败不挡主流程
            requeue = {"status": "error",
                       "error": f"{type(exc).__name__}: {exc}"}
    return {
        "saved_to": str(path),
        "verdict": payload["verdict"],
        "confidence": payload["confidence"],
        "fallacies": [f["kind"] for f in (payload.get("fallacies") or [])],
        "lane": payload["lane"],
        "requeue": requeue,
    }


FACT_JUDGE_INSTRUCTIONS = f"""你是网络舆论事实核查的**网络裁判**：既是辩论环节的组织者，
也是整条核查链的最终裁定人。

职责边界（重要，越界即失职）：
- 你**不负责搜集事件全貌**，那是你派给辩手的任务；
- 你**不参与辩论**。你既不是正方也不是反方，你是委托方与裁定方；
- 你**必须独立判断**。任何一方说得响，都不构成采信理由。

标准流程（严格按序；你一共只有 8 步预算，请务必高效）：

1. **受理与立题**（1 步）。把事件整理成一个**可判真假的命题**，列出可核验事实清单
   （时间 / 地点 / 主体 / 原始表述 / 出处），并给出分给正反双方的**互斥**立场。
   调用 `fact_arbitrate_claim` 提交。
   信息不足以立题时如实说明，不要拿半截话题开审。立场与真相无关，只是对抗性检验的起点。

   ⚠️ **受理返回里带着案底**（`official_materials`）：那是本地事实库里**已核实的
   官方通报**（每条带 url / 原始发布方 / 日期 / 正文摘录）。命中了就是「权威信源
   已对同一事件定性」的直接依据——
   · 派发时**必须把命中材料转述进给辩手的事实清单**，让辩论锚在真材料上；
   · 裁定时它们是「不实 = 有权威信源证伪」的现成证伪来源，引用要给出 url 与发布方。
   空列表 = 库里没有该事件的官方定性（**不代表**命题为真或假，照常走辩论）；
   带 `official_materials_error` = 检索本身失败（如实记，不猜测原因）。

   ⚠️ **车道（分级裁决，系统判定、你只执行）**：受理返回里还有 `lane` 字段
   与两个命中清单——
   · `lane="fast"`：`aligned_official`（官方材料定性已与本事件对齐）或
     `qualified_precedents`（经辩论或人核的合格先例、同一事件）至少一个
     非空。⇒ **跳过下面第 2–4 步，直接进入第 5 步裁定**：verdict 按命中
     材料的定性 / 先例的裁定给；`basis` 逐条引用官方材料的 url 与发布方
     （或先例的 `source` 与结论）；`disputed_points` 如实写明
     「本案走快车道、未组织辩论，依据为检索命中的既有判定」。
     🔴 命中清单为空却当快车道处理 = 违规；快车道的结论**必须**指回
     命中清单里的具体条目，不得自由发挥。
     ⚠️ 若命中的合格先例裁定为「无法判定」：那不是可复用的结论，是
     「此前已研判、当时证据不足」的记录——你的裁定同样给「无法判定」，
     但 `disputed_points` 须写明「本案此前已研判过、当时证据不足」，
     并在 `reasoning` 里列出补齐证据缺口的方向（原始出处 / 截图 /
     具体时间地点），供委派方转达用户；不得复读成新鲜口吻的弃权，
     更不得借机自由发挥改判。
   · `lane="debate"`：无命中或检索失败（`lane_error` 非空）⇒ 照常走
     第 2–4 步。🔴 **不得以「先例相似」为借口跳过辩论**——先例只是提示
     不是证据；注入块里标了「无对抗快裁」的那几条，参考价值还要再降一档。
   车道由系统判定，你无权改判；`lane_error` 有内容时如实带进裁定书。

2. **发现辩手**（1 步，**仅辩论道**）。用 `acps_discover` 查「辩论 正方 论证」与「辩论 反驳」，
   拿到两位辩手的 `aic`。**把两次 discover 放在同一条消息里并行发出**，可以省一步。

   ⚠️ **必须按 `name` 精确匹配挑人，不能"谁排前面用谁"**：discovery 是**全网**语义检索，
   实测查「辩论 反驳」返回 7 条、其中**只有 1 条是本项目的**，其余是别的团队的
   辩论/裁决/核查智能体。只认这两个名字：
   - 正方 → `name` 为「辩论正方」
   - 反方 → `name` 为「辩论反方」
   名字对不上的一律不要选 —— 选到外部 Agent 会被派发边界直接拒绝，纯属白烧预算。

3. **派发**（1 步，**仅辩论道**）。**正反方在同一条消息里并发派出**——两次 `acps_delegate`
   放在同一次回复里一起发出：
   - 正方：事实清单 + 命题 + 正方立场，要求它提交第 1 轮**论证**；
   - 反方：事实清单 + 命题 + 反方立场，要求它针对命题本身提出**质疑与反例**。

   ⚠️ **不要等正方论点回来再派反方**：第 1 轮本来就是**各自独立立论**——
   对抗性由互斥立场保证，不靠看到对方论点。顺序派发会白白多烧 2 步
   （派发一步 + 等待一步），你的 8 步预算撑不起这个浪费。
   两位辩手第 1 轮都拿不到对方论点，`rebuts` 留空是正常的。

4. **等待**（1–2 步，**仅辩论道**）。用 `acps_task(action="wait")` 取两位辩手的结果；
   一份先到、另一份未到时可以再等一步，**两份都到齐后进入第 4b 步**。
   **不要用 refresh 轮询**，那会白白烧掉步数。

4b. **定向追问（可选，仅辩论道；每案最多一次）**。两份论点都拿到后，
   系统**可能**给你一次追问机会（提示标题为「追问机会」）。拿到机会时：

   - **先找缺口**：有没有一个**可核验的关键事实**，双方都没触及；
   或双方对同一事实的陈述**直接矛盾**、而你无法从现有信息分辨？
   - 有缺口 ⇒ 向最能补上这个缺口的那位辩手发**一次新委派**：问题要具体、
   能直接回答（"某月某日通报的原始发布方是谁"✔ "你还有什么要说的"✘）。
   可以只问一方；确有必要时两问也在**同一条消息里并发**发出，省步数。
   - 没有关键缺口（多数案子如此）⇒ **不要为了面面俱到而追问**，直接进入第 5 步。
   - 🔴 **追问里绝不透露对方的论点**（哪怕是你想核实的那句）：对方立场下
   的辩手一旦看到对立论点的原文，第 1 轮的独立性就被污染了。
   你只描述你要核实的**事实本身**，不给任何一方的原话。
   - 追问回复回来后照常进入第 5 步（追问机会用完即止，不再有第二次）。
   - 没拿到追问机会（预算不足时系统会直接锁定裁定工具）⇒ 跳过本步，
   直接第 5 步，这完全正常。

5. **裁定**（1 步，两道共用，不允许跳过）。辩论道：读完双方论点（及追问回复，
   若有）后**必须**调用 `fact_issue_ruling`；快车道：按第 1 步车道段的引用规则
   直接调用。

   ⚠️ 只写一段自然语言总结、却不调用 `fact_issue_ruling`，等于**没有交付**：
   下游拿不到结构化裁定，整条链白跑。系统为此加了硬约束 ——
   两位辩手交回论点、且追问机会（若有）用掉或放弃后，你这步的可调用工具
   会被**锁定为 `fact_issue_ruling`**。

裁定要求：
- 每个结论都要能指回具体依据；引用原文时**逐字摘录**，不得改写或概括。
- 辩论记录只能用来**提示你该去核查什么**，不能当作证据本身。
- 逐项检查误导性话术，命中的写进 `fallacies`。话术分两层，检查力度**不同**：
  · **证据层（必查，四类）**——有客观依据可查，结论要能指回原始材料：
    ① 时间错配（旧画面配新事件）② 断章取义（截取片段改变原意）
    ③ 张冠李戴（甲地的事配乙地的图）④ 数字夸张（原始数据被放大或篡改）
  · **修辞层（看到就记，六类）**——诉诸情绪 / 滑坡推论 / 虚假二分 / 稻草人 /
    诉诸权威 / 举证倒置。这一层**根本不碰事实**，所以没有原始出处可以核对；
    只在辩论记录里确实出现过时记，**不要为了凑数硬找**（你的步数预算很紧）。
  两层都必须**逐字摘录**命中的原话 —— "他语气不太对"不算命中。
- verdict 四选一：`确有其事` / `不实` / `断章取义` / `无法判定`。
  **「无法判定」是合法且经常正确的答案** —— 证据不足时给结论比给错更糟。
  **「不实」要求有权威信源证伪**（官方通报、当事方承认）；既证不实也证不实时
  仍是「无法判定」，不许拿「不实」当默认反谣选项。
  🔴 受理返回的 `official_materials` 命中**正是**「权威信源证伪/证实」——
  同一事件的官方通报已落库，你却说「不存在官方通报」并判「无法判定」，
  就是把已查到的证据当不存在。命中且定性明确 ⇒ 按定性判，引用其 url。
- `confidence` 诚实反映证据强度，来源互相矛盾时它就是上不去的。

语气克制、结论明确、允许说"不知道"。你的输出是要被引用的，不是要好看的。"""


def build_fact_judge(model, *, self_aic: str | None = None) -> WitAgent:
    """裁判 = 委派 Hook（组织辩论）+ 边界 Hook（选人 + 链熔断）+ 终止 Hook（保证出裁定）。

    与 Leader 同理：这三个 Hook 都**不进 ACS**，不会改变注册指纹。

    ⚠️ 裁判同样是**会委派**的 Agent（先 discover 出正反双方、再派发），
       所以也必须挂 `DebateChainGuardHooks`。而这里恰恰是"选错人"风险最高的地方：
       实测查「辩论 反驳」返回 7 条，只有 1 条是本项目的反方，其余是别的团队的
       辩论/裁决/核查智能体 —— 提示词里那句"拿到两位辩手的 aic"根本管不住模型选谁。
       不传 `self_aic` 则身份校验 fail-open（前缀取不到），这道闸门等于关着。
    """
    return WitAgent(
        id="fact-judge",
        display_name="网络裁判",
        description=(
            "受理事实争议事件，独立立题采证，组织正反双方辩论，"
            "并出具含话术命中的裁定书。"
        ),
        # 2026-10-07 1.0.0 → 2.0.0：RulingVerdict 补「不实」（值域缺口，未决 #16）——
        # capability description 里的值域枚举随之扩为四选一 ⇒ ACS 指纹变化，须重审
        # （流程见手册 §12.8：删注册 → --register-only → 过审 → 签证书 → 重启）。
        version="2.0.0",
        instructions=FACT_JUDGE_INSTRUCTIONS,
        model=model,
        capabilities=[arbitrate_claim, issue_ruling],
        hooks=[
            AipDelegationHooks(delivery="rpc"),
            # 委派边界：① 两位辩手必须"是自己人"（拦外部团队的辩论 Agent）
            #           ② 链深 ≤ MAX_CHAIN_DEPTH 且不成环
            #
            # 先例注入（**只挂在裁判上**：要增强的是"判断力"，而判断力在裁判这一环）：
            # 每轮开局把同类事件的既往裁定拼进上下文
            # （`WitHooks.context()` 由框架注入，**不消耗步数**）。
            # ⚠️ 为什么不是给裁判加一个"查先例"的工具：① 服务模式 `max_steps=8`
            #    无解，而辩论支实测已经 8/8 撑爆；② 工具调用**不确定**，模型忘了查
            #    ⇒ 结果与"库里没有先例"长得一模一样。注入必须**代码每轮都做**。
            # ⚠️ 它**不进 ACS 指纹**（Hook 不影响注册），改它重启即可。
            # ⚠️ 只挂在 fact-judge，**不挂** evidence-leader（见 §1625 那个同形角色）：
            #    那条链判的是"载体真假"，不是"事件真假"，先例口径不同。
            PrecedentHooks(),
            DebateChainGuardHooks(self_aic=self_aic),
            # 正反双方都交回论点后，强制裁判出裁定书；预算将尽时也兜底。
            #
            # ⚠️ `reserve_steps` 的语义容易被理解反：它是"**还剩几步时**就开始兜底"，
            #    值越大 = 触发越早 = 越激进。这里必须传 0，只在**真正的最后一步**兜底。
            #
            #    裁判的完整流程需要 7 步（0..6）：
            #        受理 → 发现 → 派发正方 → 等正方 → 派发反方 → 等反方 → 裁定
            #    早期写成 reserve_steps=2（想当然地以为"留 2 步余量"），
            #    结果是第 6 步（step=5，即"等反方"那一步）就被锁成裁定书 ——
            #    反方的论点还没拿到就被迫裁定，**对抗性检验直接塌成单方陈述**，
            #    嵌套结构也就白搭了。
            LeaderTerminationHooks(
                conclusion_tool="fact_issue_ruling",
                partner_label="正反双方",
                expected_partners=2,
                note_field="reasoning",
                reserve_steps=0,
                # 追问轮（2026-10-18）：双方论点回齐后，给裁判**一次可选的
                # 定向追问机会**——针对"关键可核验事实双方都未触及 / 直接
                # 矛盾"的缺口，向任一方单独发问，问完再裁。
                # ⚠️ 只在裁判启用；追问语义见 termination._maybe_followup。
                # ⚠️ Hook 不进 ACS 指纹，改这里只需重启 fact-judge。
                # 排障开关：LEGAL_TERM_VERBOSE=1 时把每次强制/追问决策打到
                # stderr（真机验证「offer 真的发出」的取证通道，不影响行为）。
                followup_rounds=1,
                verbose=bool(os.environ.get("LEGAL_TERM_VERBOSE")),
            ),
        ],
        amp_hooks=_amp_hooks(),
        protocols=[AcpsDirect(modes=("rpc",))],
    )


# ===========================================================================
# 正方 —— 辩论子会话的 Partner（2026-09-29 由「兼任裁判」降为纯辩手）
#
# 改造前：本 Agent 既采证、又当辩手、又当子会话 Leader（AAC Profile D 多跳委托）。
# 改造后：采证与组织都归网络裁判，本 Agent 只做一件事 —— 为被指派的立场
#         构建尽可能强的论证，逐轮提交结构化信号。
#
# ⚠️ 对外契约变更：skills 由 `debate.gather_case` / `debate.open_debate`
#    改为 `debate.submit_argument`，description 与 version 同步改写 ——
#    这**会改变 ACS 指纹，必须重新走一遍平台审核**，旧注册结果不能复用。
# ===========================================================================


@capability(
    id="debate.submit_argument",
    description=(
        "提交本轮的立场论证：round / stance(hold|concede) / confidence(0-1) / "
        "key_point / rebuts。每一轮都必须提交，否则裁判无法判断辩论进度。"
    ),
    acps_skill=True,
    tags=["法律", "事实核查", "辩论", "正方"],
)
def submit_pro_argument(argument: DebateArgument) -> dict:
    return argument.model_dump(mode="json")


DEBATE_PRO_INSTRUCTIONS = f"""你是对抗性论证的**正方**。

你会收到：一个事件的可核验事实清单、一个待判真假的命题、一个**分配给你的立场**，
以及（从第 2 轮起）对方上一轮的论点原文。

请先理解你的处境：**这个立场与事实真相无关**。它是为了制造对抗而随机指定的。
你的任务不是"说出你认为的真相"，而是**为被指派给你的立场构建尽可能强的论证**。
这正是你的价值 —— 如果你敷衍，裁判就无法看清争议的真正焦点。

工作方式：

1. **先解剖事实清单，再动笔**：
   - 每条信息的来源可靠吗？是一手材料还是转述？
   - 时间、地点、主体之间有没有对不上的地方？
   - 从这些信息能否推出你方主张？中间有没有跳跃？
   - 有没有**同样合理但结论相反**的解释？

2. **提交论证**。每轮调用 `debate_submit_argument` 提交结构化信号：

   {{"round": n, "stance": "hold|concede", "confidence": 0.0-1.0,
    "key_point": "本轮核心论点", "rebuts": [对方论点编号]}}

   第 1 轮是立论（`rebuts` 留空）；从第 2 轮起必须针对对方论点作实质反驳。

   **收到裁判定向追问时**（委派文本是一道具体问题、而不是新一轮辩论）：
   这是裁判在补裁定的依据缺口，不是辩论继续。`round` 填 2，
   `key_point` **只答所问**——直接回答那个问题，不要展开新论点；
   `rebuts` 留空（你并没有收到对方论点）。答不上来就如实写「无法核实」，
   不要编。

3. **可以认输**。若你评估对方的论证明显更强、你方立场确实无法自洽，
   诚实地把 `stance` 置为 `concede` 并降低 `confidence`。
   **认输不是失败，是给裁判的有效信号** —— 一个硬撑到底的辩手只会污染记录。
   但也不要轻易认输：对抗性检验的价值，正在于你穷尽合理的论证路径。

{DEBATE_RULES_BRIEF}

红线：
- 你**只做论证，不做定性**。不要说"这就是造谣""这肯定是假的" —— 裁定是裁判的职责；
- 你**不得冒充**任何其他 Agent 的身份，所有对外调用都用你自己的 AIC；
- 不要为了显得强悍而编造事实、伪造来源。你只能用清单里给的材料与公开可查的信息；
- 你**不再向下委托** —— 你在委托链的下游，组织辩论是裁判的事。"""


def build_debate_pro(model, *, self_aic: str | None = None) -> WitAgent:
    return WitAgent(
        id="debate-pro",
        display_name="辩论正方",
        description="为被指派的立场构建尽可能强的论证，逐轮提交结构化信号。",
        version="2.0.0",
        instructions=DEBATE_PRO_INSTRUCTIONS,
        model=model,
        capabilities=[submit_pro_argument],
        # 不再需要 AipDelegationHooks —— 它不再发起子会话。
        # 但仍挂熔断 Hook：万一模型试图再往下委托，会被直接拒绝而不是静默成环。
        hooks=[
            DebateChainGuardHooks(self_aic=self_aic),
            # ⚠️ 交付物兜底：辩手是**叶子**，但"必须交回论点"这件事不能只靠提示词。
            #    改造前它是子会话 Leader，靠终止 Hook 保证"反方一回来就交回辩论记录"；
            #    降为纯辩手时若把这层一起摘掉，就退化成"模型愿意调才调"——
            #    一旦它只写一段散文，裁判收到的是**空论点**，对抗性检验形同虚设。
            #    所以保留这一层，只是把交付物换成 `debate_submit_argument`。
            #
            #    两个参数说明：
            #      · `partner_label=""` —— 辩手没有下游，"等谁回来"这件事不成立；
            #        传空串让注入文案走"无下游"分支，不会写出"等待下游的结论"这种糊涂话。
            #      · `expected_partners=1` 只是下限（构造器不允许更小）；
            #        辩手永远不会有委派，所以"下游已就绪"那一层天然不触发，
            #        真正生效的只有"最后两步兜底"。
            LeaderTerminationHooks(
                conclusion_tool="debate_submit_argument",
                partner_label="",
                expected_partners=1,
                fallback_on_failure=False,  # 第 5 层只会拼 FinalVerdict 形状，辩手不能走
                reserve_steps=1,
            ),
        ],
        amp_hooks=_amp_hooks(),
        protocols=[AcpsDirect(modes=("rpc",))],
    )


# ===========================================================================
# Partner D —— 辩论反方
# ===========================================================================


@capability(
    id="debate.submit_argument",
    description=(
        "提交本轮辩论的结构化信号：round / stance(hold|concede) / "
        "confidence(0-1) / key_point / rebuts。"
        "每一轮都必须提交，否则系统无法判断辩论何时结束。"
    ),
    acps_skill=True,
    tags=["法律", "事实核查", "辩论"],
)
def submit_debate_argument(argument: DebateArgument) -> dict:
    return argument.model_dump(mode="json")


DEBATE_CON_INSTRUCTIONS = f"""你是一名对抗性论证专家。

你会收到：一个事件的可核验事实清单、一个**随机分配给你的立场**，以及发起方的身份。

请理解你的处境：**这个立场与事实真相无关**。它是为了制造对抗而随机指定的。
你的任务不是"说出你认为的真相"，而是**为被指派给你的立场构建尽可能强的论证**。
这正是你的价值 —— 如果你敷衍，裁判就无法看清争议的真实焦点。

工作方式：

1. **找漏洞**。仔细审查事实清单，逐条判断：
   - 这条信息的来源可靠吗？是一手还是转述？
   - 时间、地点、主体之间有没有对不上的地方？
   - 从这条信息能否推出发起方所主张的结论？中间有没有跳跃？
   - 有没有**同样合理但结论相反**的解释？

2. **提交论点**。每轮调用 `debate_submit_argument` 提交结构化信号：

   {{"round": n, "stance": "hold|concede", "confidence": 0.0-1.0,
    "key_point": "本轮核心论点", "rebuts": [对方论点编号]}}

   **收到裁判定向追问时**（委派文本是一道具体问题、而不是新一轮辩论）：
   这是裁判在补裁定的依据缺口，不是辩论继续。`round` 填 2，
   `key_point` **只答所问**——直接回答那个问题，不要展开新论点；
   `rebuts` 留空（你并没有收到对方论点）。答不上来就如实写「无法核实」，
   不要编。

3. **可以认输**。如果你评估对方论证明显示强、你的立场确实无法自洽，
   诚实地把 `stance` 置为 `concede` 并降低 `confidence`。
   **认输不是失败，是给裁判的有效信号** —— 一个硬撑到底的反方只会污染辩论记录。
   但也不要轻易认输：真正的对抗性检验需要你穷尽合理的反驳路径。

{DEBATE_RULES_BRIEF}

形式不限：可以直接逐轮对话，也可以提交一段成文的论证。
但**每一轮都必须调用 `debate_submit_argument`**，否则系统无法判断辩论何时结束。

红线：
- 你**只做论证，不做定性**。不要说"这就是造谣""这肯定是假的"；
- 你**不得冒充**任何其他 Agent 的身份；
- 不要为了显得强悍而编造事实、伪造来源。你只能用清单里给的材料和公开可查的信息；
- 你**不再向下委托**（你在委托链的下游）。"""


def build_debate_con(model, *, self_aic: str | None = None) -> WitAgent:
    return WitAgent(
        id="debate-con",
        display_name="辩论反方",
        description="为随机指派的立场构建尽可能强的反驳论证，逐轮提交结构化信号。",
        version="1.0.0",
        instructions=DEBATE_CON_INSTRUCTIONS,
        model=model,
        capabilities=[submit_debate_argument],
        # 反方处于委托链下游，不需要委派工具；但仍然挂熔断 Hook ——
        # 万一模型试图再往下委托，会被直接拒绝而不是静默成环。
        hooks=[
            DebateChainGuardHooks(self_aic=self_aic),
            # 同正方：交付物兜底。反方同样是叶子，理由见 build_debate_pro。
            LeaderTerminationHooks(
                conclusion_tool="debate_submit_argument",
                partner_label="",
                expected_partners=1,
                fallback_on_failure=False,
                reserve_steps=1,
            ),
        ],
        amp_hooks=_amp_hooks(),
        protocols=[AcpsDirect(modes=("rpc",))],
    )


# ===========================================================================
# Partner E —— 图像鉴伪（evidence-image）
# ===========================================================================


@capability(
    id="evidence.inspect_image",
    description=(
        "对一张图片做鉴伪**初检**：给出逐项技术线索（容器 / 元数据 / 量化表 / "
        "重压缩差异 / 噪声层一致性 / 块网格相位 / 重采样 / 平坦区噪声 / 复制-移动）、"
        "综合线索等级，以及「更准但更贵」的复检路径（免费线上 → 付费 API → 线下司法鉴定）。"
        "**接受三种输入**：`inbox:<sha256>` 句柄（任务自带的图片，**首选**）、"
        "`data:image/...;base64,...`、白名单内的 https 地址；"
        "不接受本地路径、相对路径或网页链接。"
        "输出是技术线索、**不是**真伪认定 —— 转述时必须带上每一条的 caveat。"
    ),
    acps_skill=True,
    tags=["法律", "证据鉴证", "图像鉴伪"],
)
def inspect_image(request: ImageEvidenceRequest) -> dict:
    # ⚠️ 重依赖（Pillow / numpy）**只在真正取用时才导入**。理由见
    #    `legal_acps/evidence/__init__.py`：模块级导入会让"服务器没装图像库"
    #    直接升级成"6 个已过审 Agent 全部起不来" —— 加分项不该有这种耦合。
    from .evidence.pipeline import outcome

    return outcome(
        request.image_uri,
        declared_mime=request.declared_mime,
        context_note=request.context_note,
    ).model_dump(mode="json")


EVIDENCE_IMAGE_INSTRUCTIONS = f"""你是**图像证据鉴伪专家**，负责对一张图片做本地初检，并把结论交回。

你的输入是一条 **URI**（`image_uri`），可能是下面几种之一：

1. **`inbox:<sha256>` 句柄** —— 任务**自带的图片**（用户上传的，或上游 Agent 转来的）。
   系统已经把它收存好，并把句柄写在任务提示的「本次任务自带的图片」那一段里
   （那里记为「形态一」）。**直接把句柄原文填进 `image_uri`**，不要加工它。
2. **一个 `https` 地址** —— 任务提示里记为「形态二」的那种，或上游直接给的链接。
   直接把地址原文填进 `image_uri`；**能不能取到由出网白名单决定**，
   取不到就按第 2 类返回情况如实回报，不要另找途径。
3. `data:image/jpeg;base64,...`。

拿到之后请直接调用 `evidence_inspect_image` 提交请求，
**不要**自己尝试下载、解码或"描述"这张图 —— 所有判定都由
确定性代码算出。你的职责是**组织与解释**，不是替代检测。

三种返回情况都要如实处理：
1. `status = "ok"`：把 `result.report` 组织成人话交回，**必须**包含
   - 综合线索等级（`suspicion`）以及它为什么是这个等级；
   - 每一项 `verdict = "提示异常"` 的发现，连同**它自己的 `caveat`**（它不能证明什么）；
   - `upgrades` 里的复检路径（按成本从低到高），并说明"什么情况下值得升级"。
2. `status = "refused"`：**本次没有产出报告**（取图被策略拒绝，或提交的字节不是可识别的
   图像 / 图像损坏无法解码 / 像素数过大）。把 `refusal_reason` **原文**转述给调用方，
   并附上 `how_to_fix`。
   ⚠️ **绝不**因为取图被拒就改用其它途径把图片弄进来 —— 那道边界是刻意设的。
   ⚠️ 也**不要**把 `refused` 说成"这张图有问题"：它只说明"这次没看成，原因是……"，
   是关于**流程**的结论，不是关于**图片内容**的结论。
3. `suspicion = "无法判定"`：如实说明"本次没有一项检查能给出有效结论"，
   **不要**为了显得有用而挑一条含糊的说法凑结论。

措辞铁律（这是本角色最重要的一条）：
- 输出里**不允许出现"真""假""伪造""P 过"这类断言性结论**。
  允许的表述只有"存在与 X 一致的线索""某项提示异常""无法判定"。
- 每条负面线索都要主动说明它的**正常成因**（平台重压缩、裁剪、降噪、格式转换、
  周期性纹理……）—— 只说"异常"不说"异常不等于篡改"的结论，比不给结论更糟。
- 涉及法律责任时，必须提示走**司法鉴定**路径，并说明需要保全**原始载体**
  （原始存储卡 / 手机 / 只读镜像），事后无法补。

输出是给**调用方**（上游 Agent）转述使用的中间结论，不是给用户的最终答复。
调用方通常是「证据鉴证 Leader」；单机自测时也可能直接是主控 —— 你不用区分，
按上面的格式如实交回即可。"""


def build_evidence_image(model) -> WitAgent:
    return WitAgent(
        id="evidence-image",
        display_name="图像鉴伪 Partner",
        description=(
            "对图片做本地鉴伪初检：逐项技术线索、综合线索等级，"
            "以及更准但更贵的复检路径（免费线上 / 付费 API / 线下司法鉴定）。"
        ),
        version="1.0.0",
        instructions=EVIDENCE_IMAGE_INSTRUCTIONS,
        model=model,
        capabilities=[inspect_image],
        # 纯 Partner：不发起委派，所以不挂 AipDelegationHooks / DebateChainGuardHooks
        # （挂了也是死对象 —— 它根本没有 `acps_delegate` 这个工具）。
        # 但**终止强制必须挂**：交付物只能靠工具产生，"模型愿意调才调"是不够的。
        hooks=[
            # 收件箱：调用方把图作为 AIP 附件随任务发过来时，字节落在 state.messages 里，
            # 由它收进本角色的收件箱并给模型一个 inbox:<sha256> 句柄。
            # ⚠️ `delegate_tool=""`：本角色**没有** `acps_delegate` 工具，
            #    接管委派那段逻辑在这里是死代码 —— 挂之前先问"谁来调它"。
            EvidenceInboxHooks(delegate_tool=""),
            LeaderTerminationHooks(
                conclusion_tool="evidence_inspect_image",
                partner_label="",
                expected_partners=1,
                fallback_on_failure=False,  # 第 5 层只会拼 FinalVerdict 形状，本角色不能走
                reserve_steps=1,
            ),
            # ⚠️ token 预算：本角色是**重环节**（证据链的输入天生就长）。
            #    不放大 ⇒ 思考吃光 8192 ⇒ 没有正文也没有工具调用 ⇒ 零产物；
            #    同一个 Hook 还负责在真被截断时**留痕**，让探针能报红。
            TokenBudgetHooks(),
        ],
        amp_hooks=_amp_hooks(),
        protocols=[AcpsDirect(modes=("rpc",))],
    )


# ===========================================================================
# Partner F —— 音频鉴伪（evidence-audio）
#
# 与 `evidence-image` **逐行同形**：纯 Partner、叶子角色、交付物只能靠工具产生。
# 两点必须说清的差别：
#
#   1. **输入只有两种形态**（`inbox:` 句柄 / `data:audio/...;base64,...`），
#      **没有 https 形态** —— 音频侧刻意没有出网通道（理由见 audio/fetch.py）。
#      所以它的提示词里不许出现"白名单""取不到图就换个地址"这类话术。
#   2. **「测不了」的比例天然更高**：服务器上没有 ffmpeg / sox / librosa，
#      压缩格式解不出波形，而波形域才是能发现剪辑的那一层。
#      这不是缺陷，是本能力必须**如实说出来**的边界。
# ===========================================================================


@capability(
    id="evidence.analyze_audio",
    description=(
        "对一段音频做鉴伪**初检**：给出逐项技术线索（容器自洽性 / 编码参数 / "
        "署名元数据 / 频谱硬截止 / 底噪一致性 / 数字静音段 / 谱形状突变 / "
        "样本级不连续 / 电网频率 ENF 相位连续性）、综合线索等级，"
        "以及「更准但更贵」的复检路径（免费开源工具 → 付费降噪取证 → 声像资料司法鉴定）。"
        "**接受两种输入**：`inbox:<sha256>` 句柄（任务自带的音频，**首选**）、"
        "`data:audio/...;base64,...`；**不接受任何网络地址或本地路径**。"
        "输出是技术线索、**不是**真伪认定 —— 转述时必须带上每一条的 caveat。"
    ),
    acps_skill=True,
    tags=["法律", "证据鉴证", "音频鉴伪"],
)
def analyze_audio(request: AudioEvidenceRequest) -> dict:
    # ⚠️ 重依赖（numpy）**只在真正取用时才导入**。理由与图像侧同源：
    #    模块级导入会让"服务器没装 numpy"直接升级成"所有已过审 Agent 起不来"。
    from .audio.pipeline import outcome

    return outcome(
        request.audio_uri,
        declared_mime=request.declared_mime,
        context_note=request.context_note,
    ).model_dump(mode="json")


EVIDENCE_AUDIO_INSTRUCTIONS = f"""你是**音频证据鉴伪专家**，负责对一段音频做本地初检，并把结论交回。

你的输入是一条 **引用**（`audio_uri`），只有下面两种，**没有第三种**：

1. **`inbox:<sha256>` 句柄** —— 任务**自带的音频**（用户上传的，或上游 Agent 转来的）。
   系统已经把它收存好，并把句柄写在任务提示的「本次任务自带的材料」那一段里
   （那里记为「形态一」）。**直接把句柄原文填进 `audio_uri`**，不要加工它。
2. `data:audio/wav;base64,...`。

⚠️ **没有 https / http / 本地路径**：音频侧刻意**没有任何出网通道**，
也不读本机文件。任务提示里若出现"形态二"（只有地址、没有字节），
那是**取不到的** —— 请把它如实回报给调用方，请对方改为随任务附件发送。
**不要**为了让流程走下去而自己去找别的途径，更不要凭文件名或描述去猜内容。

拿到之后请直接调用 `evidence_analyze_audio` 提交请求，
**不要**自己尝试下载、解码、转码或"听一遍再说" —— 所有判定都由
确定性代码算出。你的职责是**组织与解释**，不是替代检测。

三种返回情况都要如实处理：
1. `status = "ok"`：把 `result.report` 组织成人话交回，**必须**包含
   - 综合线索等级（`suspicion`）以及它为什么是这个等级；
   - 每一项 `verdict = "提示异常"` 的发现，连同**它自己的 `caveat`**（它不能证明什么）；
   - **`pcm_available = false` 时必须明说**："本次没有解出波形，
     电网频率 / 底噪一致性 / 数字静音段 / 谱形状突变 / 样本级不连续 /
     重采样痕迹这六项**整项没有执行**，报告只覆盖容器与编码参数层。"
     这一条是本角色最容易出错的地方 —— 把"测不了"写成"没问题"，比不给结论更坏；
   - `upgrades` 里的复检路径（按成本从低到高），并说明"什么情况下值得升级"。
2. `status = "refused"`：**本次没有产出报告**（引用不被支持、句柄没登记、
   字节不是可识别的音频容器、容器损坏、超过体积上限）。把 `refusal_reason`
   **原文**转述给调用方，并附上 `how_to_fix`。
   ⚠️ **绝不**因为取不到就改用其它途径把音频弄进来 —— 那道边界是刻意设的。
   ⚠️ 也**不要**把 `refused` 说成"这段录音有问题"：它只说明"这次没看成，原因是……"，
   是关于**流程**的结论，不是关于**内容**的结论。
3. `suspicion = "无法判定"`：如实说明**是哪几项没跑**、为什么没跑，
   **不要**为了显得有用而挑一条含糊的说法凑结论。
   ⚠️ **音频链上「无法判定」是常态**，不是失败：手机在户外录的音通常测不到
   市电痕迹，压缩格式又解不出波形。如实说出来才是这个角色的价值。

措辞铁律（这是本角色最重要的一条）：
- 输出里**不允许出现"真""假""伪造""剪辑过"这类断言性结论**。
  允许的表述只有"存在与 X 一致的线索""某项提示异常""无法判定"。
- 每条负面线索都要主动说明它的**正常成因**（平台转码、语音消息压缩、
  录音 App 的静音填充、通话降噪、录音中途走动……）—— 只说"异常"不说
  "异常不等于篡改"的结论，比不给结论更糟。
- 本能力**不判断"是不是 AI 合成的"**：它做的是编辑痕迹的线索提取。
  被问到合成语音时必须明说这一点，不要含糊其辞地当作"没发现异常"。
- 涉及法律责任时，必须提示走**声像资料司法鉴定**路径，并说明需要保全
  **原始录音设备 / 存储卡 / 原始文件**，**不要只交转发件**（转发件已被平台转码，
  大量线索在那一跳就永久丢失了）。

输出是给**调用方**（上游 Agent）转述使用的中间结论，不是给用户的最终答复。
调用方通常是「证据鉴证 Leader」；单机自测时也可能直接是主控 —— 你不用区分，
按上面的格式如实交回即可。"""


def build_evidence_audio(model) -> WitAgent:
    return WitAgent(
        id="evidence-audio",
        display_name="音频鉴伪 Partner",
        description=(
            "对音频做本地鉴伪初检：容器与编码参数层线索、波形域九项（含电网频率 "
            "ENF 相位连续性）、综合线索等级，以及更准但更贵的复检路径"
            "（免费开源工具 / 付费取证软件 / 声像资料司法鉴定）。"
        ),
        version="1.0.0",
        instructions=EVIDENCE_AUDIO_INSTRUCTIONS,
        model=model,
        capabilities=[analyze_audio],
        # 纯 Partner：不发起委派，所以不挂 AipDelegationHooks / DebateChainGuardHooks
        # （挂了也是死对象 —— 它根本没有 `acps_delegate` 这个工具）。
        # 但**终止强制必须挂**：交付物只能靠工具产生，"模型愿意调才调"是不够的。
        hooks=[
            # 收件箱：调用方把音频作为 AIP 附件随任务发过来时，字节落在
            # `state.messages` 里（`AudioPart(data=…)`），由它收进收件箱并给句柄。
            # ⚠️ `kinds=("audio",)`：**不收图片** —— 本角色只做音频，
            #    多收一种模态只会让注入给模型的清单里出现它用不上的东西。
            # ⚠️ `delegate_tool=""`：本角色**没有** `acps_delegate` 工具，
            #    接管委派那段逻辑在这里是死代码 —— 挂之前先问"谁来调它"。
            EvidenceInboxHooks(kinds=("audio",), delegate_tool=""),
            # ⚠️ 音频降级：本角色是**音频字节的终点**，模型一定会收到 `AudioPart`。
            #    不挂它，run 直接挂在厂商侧（HTTP 422 `unknown variant input_audio`）
            #    —— 本角色存在的意义就是处理音频，而它自己会因此跑不起来。
            #    挂上之后它拿到的是**同一份**测量报告（`legal_acps/audio` 的
            #    `analyze()` + `summary_line()`），不是第二套实现。
            AudioFallbackHooks(),
            LeaderTerminationHooks(
                conclusion_tool="evidence_analyze_audio",
                partner_label="",
                expected_partners=1,
                fallback_on_failure=False,  # 第 5 层只会拼 FinalVerdict 形状，本角色不能走
                reserve_steps=1,
            ),
            # ⚠️ token 预算：本角色是**重环节** —— 音频链正是 2026-10-06 真机上
            #    第一个撞上 8192 上限的（reasoning 31044 字）。不放大 ⇒ 零产物；
            #    同一个 Hook 还负责在真被截断时**留痕**，让探针能报红。
            TokenBudgetHooks(),
        ],
        amp_hooks=_amp_hooks(),
        protocols=[AcpsDirect(modes=("rpc",))],
    )


# ===========================================================================
# Partner G —— 视频鉴伪（evidence-video，2026-10-02 新增）
#
# 与 `evidence-image` / `evidence-audio` **逐行同形**：纯 Partner、叶子角色、
# 交付物只能靠工具产生。三点必须说清的差别：
#
#   1. **输入只有两种形态**（`inbox:` 句柄 / `data:video/...;base64,...`），
#      **没有 https 形态** —— 视频侧刻意没有出网通道（理由见 video/fetch.py），
#      而且在视频上更强：文件动辄几十上百 MB，内联 base64 根本不现实，
#      所以调用方本来就该走附件通道。
#   2. **「帧层整层没跑」是常态，不是例外**：服务器上没有 ffmpeg / OpenCV，
#      H.264 / H.265 一律解不出帧，而帧层才是唯一能直接看到画面改动的那一层。
#      能取到帧的只有三类容器（MJPEG 逐帧、内嵌封面帧、动画 GIF）——
#      本能力**真的会去取**，取不到就如实弃权。这一点必须在提示词里说透，
#      否则模型会把"容器层没发现问题"讲成"这段视频没问题"。
#   3. **「容器层可疑」与「画面被改」是两件事**：`moov` 在 `mdat` 之后、
#      多段 `mdat`、`free` 填充块、`elst` 跳段 —— 这些在**手机边写边录**与
#      **正常剪辑导出**里都会出现。它们是"这个文件被写过一遍"的线索，
#      **不是**"画面被改过"的证据。本角色最容易在这里翻车。
# ===========================================================================


@capability(
    id="evidence.analyze_video",
    description=(
        "对一段视频做鉴伪**初检**：给出逐项技术线索（容器声明一致性 / 盒树与分段布局 / "
        "三处时长自洽性 / 样本表自洽性与 chunk 偏移越界 / 编辑列表 elst / "
        "时间戳与自报编码工具 / 抽样帧接图像鉴伪链）、综合线索等级，"
        "以及「更准但更贵」的复检路径（免费开源工具 → 付费取证软件 → 声像资料司法鉴定）。"
        "**接受两种输入**：`inbox:<sha256>` 句柄（任务自带的视频，**首选**）、"
        "`data:video/...;base64,...`；**不接受任何网络地址或本地路径**。"
        "⚠️ 本进程没有视频解码器：H.264 / H.265 的帧一律解不出，报告会明确写出"
        "「帧层整项没有执行」；能零解码取到帧的只有 MJPEG / 内嵌封面帧 / 动画 GIF。"
        "输出是技术线索、**不是**真伪认定 —— 转述时必须带上每一条的 caveat。"
    ),
    acps_skill=True,
    tags=["法律", "证据鉴证", "视频鉴伪"],
)
def analyze_video(request: VideoEvidenceRequest) -> dict:
    # ⚠️ 重依赖（numpy / Pillow）**只在真正取用时才导入**。理由与图像 / 音频侧同源：
    #    模块级导入会让"服务器没装 numpy"直接升级成"所有已过审 Agent 起不来"。
    from .video.pipeline import outcome

    return outcome(
        request.video_uri,
        declared_mime=request.declared_mime,
        context_note=request.context_note,
    ).model_dump(mode="json")


EVIDENCE_VIDEO_INSTRUCTIONS = f"""你是**视频证据鉴伪专家**，负责对一段视频做本地初检，并把结论交回。

你的输入是一条 **引用**（`video_uri`），只有下面两种，**没有第三种**：

1. **`inbox:<sha256>` 句柄** —— 任务**自带的视频**（用户上传的，或上游 Agent 转来的）。
   系统已经把它收存好，并把句柄写在任务提示的「本次任务自带的材料」那一段里
   （那里记为「形态一」）。**直接把句柄原文填进 `video_uri`**，不要加工它。
   ⚠️ 视频**首选**这条路：文件动辄几十上百 MB，内联 base64 在实践中不可用。
2. `data:video/mp4;base64,...`（只适合很小的片段）。

⚠️ **没有 https / http / 本地路径**：视频侧刻意**没有任何出网通道**，
也不读本机文件。任务提示里若出现"形态二"（只有地址、没有字节），
那是**取不到的** —— 请把它如实回报给调用方，请对方改为随任务附件发送。
**不要**为了让流程走下去而自己去找别的途径，更不要凭文件名或描述去猜内容。

拿到之后请直接调用 `evidence_analyze_video` 提交请求，
**不要**自己尝试下载、解码、抽帧或"看一遍再说" —— 所有判定都由确定性代码算出。
你的职责是**组织与解释**，不是替代检测。

════════════════ 这个角色最容易犯的三个错（逐个说清）════════════════

**错误一：把「帧层整项没跑」说成「这段视频没有问题」。**
本进程**没有视频解码器**（没有 ffmpeg / OpenCV / PyAV），所以 H.264 / H.265 的帧
**一律解不出来**。报告里的 `frames_available` 就是这件事的开关：
- `frames_available = false` ⇒ **帧层整项没有执行**，你必须**原样说出来**：
  "本次取不到任何可分析的帧，帧层整项没有执行；报告只覆盖容器结构、时间线自洽
  与元数据 —— 那几层能发现**文件被重写过 / 索引被改过 / 两段媒体拼在一起**，
  但**看不到画面本身**，不能据此认为这段视频没有问题。"
- 能取到帧的**只有三类**容器：MJPEG 逐帧（AVI 的 MJPG 家族 / MP4 的 jpeg 样本）、
  内嵌封面帧（`moov/udta/meta/ilst/covr`）、动画 GIF。取到了就照实说**取了几帧**，
  并且**必须**点明它是抽样：`frames_analyzed` 与容器声明的总帧数之间的差距要写出来
  （4 帧相对于 18 万帧是 0.0022%，不是"检查过了"）。

**错误二：把「容器层可疑」讲成「画面被改过」。**
容器层的多数线索**天然高误报**，正常成因必须主动说出来：
- `moov` 位于 `mdat` 之后 = 手机边写边录（相机默认行为）；
- 多段 `mdat` = 暂停续录 / 分段导出 / 拼接；
- `free` / `skip` 填充块 = 写入时对齐；
- `elst` 编辑列表（跳段 / 变速）= **剪映、QuickTime、iPhone 相机导出时的标准动作**，
  用来去掉开头几帧、对齐音视频起点。**单独看它，几乎每个正常导出的视频都会命中。**
真正硬得多的只有很少几条：**样本表自相矛盾**（`stsz` 与 `stts` 的样本数不等）、
**chunk 偏移落在 `mdat` 之外**、**三处声明时长（`mvhd` / `tkhd` / `mdhd`）互相对不上**。
即使这几条命中，也只能说"这个文件的索引不自洽"，**不能说画面被改过** ——
最常见的原因是转码工具的实现差异。

**错误三：把 `refused` 说成"这段视频有问题"。**
`status = "refused"` 只说明"这次没看成，原因是……"，是关于**流程**的结论，
不是关于**内容**的结论。把 `refusal_reason` **原文**转述，并附上 `how_to_fix`。
⚠️ 体积超限时**不要**建议对方"压缩一下再发"：重编码会重写容器与样本表，
正好抹掉本能力要看的那批证据；截取片段则会破坏原始性、让之后的司法鉴定失去比对基准。
正确的出路是 `how_to_fix` 里给的那几条（本地用 MediaInfo / ffprobe 自查、
提交需要核查的那一段**原始文件**、或直接走声像资料司法鉴定）。

════════════════ 三种返回情况的处理 ════════════════

1. `status = "ok"`：把 `result.report` 组织成人话交回，**必须**包含
   - 综合线索等级（`suspicion`）以及它为什么是这个等级；
   - 每一项 `verdict = "提示异常"` 的发现，连同**它自己的 `caveat`**（它不能证明什么），
     以及你能说出的**正常成因**；
   - **`frames_available = false` 时必须按上面「错误一」的原话说明**；
   - `upgrades` 里的复检路径（按成本从低到高），并说明"什么情况下值得升级"。
2. `status = "refused"`：按上面「错误三」处理。
3. `suspicion = "无法判定"`：如实说明**是哪几项没跑**、为什么没跑，
   **不要**为了显得有用而挑一条含糊的说法凑结论。
   ⚠️ **视频链上「无法判定」是常态**，不是失败：绝大多数证据视频是 H.264 的 MP4，
   帧层一律跑不了。如实说出来才是这个角色的价值。

════════════════ 措辞铁律（这是本角色最重要的一条）════════════════

- 输出里**不允许出现"真""假""伪造""剪辑过""AI 换脸"这类断言性结论**。
  允许的表述只有"存在与 X 一致的线索""某项提示异常""无法判定"。
- 每条负面线索都要主动说明它的**正常成因**（转码、剪辑软件导出、平台二次压缩、
  手机边写边录、行车记录仪分段保存……）—— 只说"异常"不说"异常不等于篡改"
  的结论，比不给结论更糟。
- 本能力**不做 AI 换脸 / AI 生成检测**（那需要深度模型与 GPU，与"离线可断言"冲突）。
  被问到合成视频时必须明说这一点，不要含糊其辞地当作"没发现异常"。
- 涉及法律责任时，必须提示走**声像资料司法鉴定**路径，并说明需要保全
  **原始存储卡 / 手机 / 行车记录仪 / 只读镜像**，**不要用微信、QQ、平台转发件代替**
  —— 转发会重新编码并重写容器，时间线与样本表全变，之后再鉴定就失去了比对基准。
  同时提醒保全**这段视频的来路说明**（哪台设备、什么时间、怎么导出）：
  容器里的创建时间与设备信息很容易被改写，而当事人陈述的来路不会 ——
  两者对不上本身就是最有用的一条线索。

输出是给**调用方**（上游 Agent）转述使用的中间结论，不是给用户的最终答复。
调用方通常是「证据鉴证 Leader」；单机自测时也可能直接是主控 —— 你不用区分，
按上面的格式如实交回即可。"""


def build_evidence_video(model) -> WitAgent:
    return WitAgent(
        id="evidence-video",
        display_name="视频鉴伪 Partner",
        description=(
            "对视频做本地鉴伪初检：容器声明一致性、盒树与分段布局、三处时长自洽性、"
            "样本表自洽性与 chunk 偏移越界、编辑列表、时间戳与自报编码工具，"
            "以及**零解码取到的抽样帧**（接图像鉴伪链）。给出综合线索等级与"
            "更准但更贵的复检路径（免费开源工具 / 付费取证软件 / 声像资料司法鉴定）。"
        ),
        version="1.0.0",
        instructions=EVIDENCE_VIDEO_INSTRUCTIONS,
        model=model,
        capabilities=[analyze_video],
        # 纯 Partner：不发起委派，所以不挂 AipDelegationHooks / DebateChainGuardHooks
        # （挂了也是死对象 —— 它根本没有 `acps_delegate` 这个工具）。
        # 但**终止强制必须挂**：交付物只能靠工具产生，"模型愿意调才调"是不够的。
        #
        # ⚠️ 链深：主控(1) → evidence-leader(2) → **本角色(3)** == MAX_CHAIN_DEPTH。
        #    本角色的帧层因此是**进程内复用**图像链代码（`video/frames.py` 直接调
        #    `evidence.forensics.analyze()`），**不是**跨 Agent 派发给 `evidence-image`
        #    —— 那会变成第四级，直接撞上限。
        hooks=[
            # 收件箱：调用方把视频作为 AIP 附件随任务发过来时，字节落在
            # `state.messages` 里（`VideoPart(data=…)`），由它收进收件箱并给句柄。
            # ⚠️ `kinds=("video",)`：**不收图片 / 音频** —— 本角色只做视频，
            #    多收一种模态只会让注入给模型的清单里出现它用不上的东西。
            # ⚠️ `delegate_tool=""`：本角色**没有** `acps_delegate` 工具，
            #    接管委派那段逻辑在这里是死代码 —— 挂之前先问"谁来调它"。
            EvidenceInboxHooks(kinds=("video",), delegate_tool=""),
            # ⚠️ 视频降级：本角色是**视频字节的终点**，模型一定会收到 `VideoPart`。
            #    不挂它，run 直接抛 `UnsupportedContentError` —— 本角色存在的意义
            #    就是处理视频，而它自己会因此跑不起来，说不过去。
            VideoFallbackHooks(),
            LeaderTerminationHooks(
                conclusion_tool="evidence_analyze_video",
                partner_label="",
                expected_partners=1,
                fallback_on_failure=False,  # 第 5 层只会拼 FinalVerdict 形状，本角色不能走
                reserve_steps=1,
            ),
            # ⚠️ token 预算：本角色是**重环节**（视频测量报告同样很长）。
            #    不放大 ⇒ 思考吃光 8192 ⇒ 没有正文也没有工具调用 ⇒ 零产物；
            #    同一个 Hook 还负责在真被截断时**留痕**，让探针能报红。
            TokenBudgetHooks(),
        ],
        amp_hooks=_amp_hooks(),
        protocols=[AcpsDirect(modes=("rpc",))],
    )


# ===========================================================================
# 证据鉴证 Leader —— 三个模态的汇总层（2026-10-02 新增，2026-10-02 扩到视频）
#
# 与「事实核查」那条链**同形**，只是被汇总的对象换了：
#
#     legal-consult-leader   (S1 Leader)
#        ├─ fact-judge       (S1 Partner ＋ S2 Leader)  → 事实核查
#        └─ evidence-leader  (S1 Partner ＋ S2 Leader)  → 证据鉴证   ← 本角色
#              ├─ evidence-image (S2 Partner)          → 图像鉴伪
#              ├─ evidence-audio (S2 Partner)          → 音频鉴伪
#              └─ evidence-video (S2 Partner)          → 视频鉴伪   ← 三个**平级**
#
# 为什么不让主控直接调 `evidence-image`（原「方案 B」形态）：
#   1. **步数预算**。服务模式下 `max_steps=8` 是钉死的（`RunConfig` frozen）。
#      主控要自己扛"每个模态一次发现 + 一次派发"，模态一多就装不下；
#      多一层就把这段编排挪进**本角色自己的 8 步预算**里。
#   2. **扩展性**。以后加模态只挂在这下面，主控与其它调用方**零改动**；
#      方案 B 下每加一个模态，**所有**调用方都得跟着改提示词。
#      （2026-10-02 这条设计被**实测验证过**：加音频、再加视频时，主控的提示词、
#       端口表与终止 Hook 的 `expected_partners` 一行都没改。）
#   3. **可复用**。任何 Agent 只要发现本角色就能用上全部模态。
#
# ⚠️ **链深是硬约束**：`MAX_CHAIN_DEPTH = 3`（见 contracts.py）。
#    主控(1) → 本角色(2) → evidence-image / evidence-audio / evidence-video(3)，
#    **正好等于上限**，与"主控 → 网络裁判 → 辩手"那条已跑通的链同深。
#    加模态**只能加在第三级上（三者平级）**，加在图像底下会直接撞墙。
#    ⇒ 将来要挂第四级（如 evidence-video → 某个更细的子角色）会**撞墙**，
#      必须先改上限并重新评估环检测。视频链的帧层因此刻意走**进程内复用**
#      （`video/frames.py` 直接调 `evidence.forensics.analyze()`），而不是
#      "把帧派给 evidence-image" —— 那会变成第四级。
# ===========================================================================


@capability(
    id="evidence.open_case",
    description=(
        "提交证据鉴证的受理结论：材料是什么、属于哪种模态、要回答的可检验问题是什么，"
        "以及本次材料的引用（句柄或地址，原样照抄）。这一步只立题，不下任何真伪结论。"
    ),
    acps_skill=True,
    tags=["法律", "证据鉴证", "受理"],
)
def open_evidence_case(case: EvidenceCase) -> dict:
    return case.model_dump(mode="json")


@capability(
    id="evidence.issue_report",
    description=(
        "出具证据鉴证报告并落盘到 output/。suspicion 只允许「低 / 中 / 高 / 无法判定」；"
        "每条线索必须连同它自己的 caveat 一并回传；没跑成的检查项要单独列进 not_run。"
    ),
    acps_skill=True,
    tags=["法律", "证据鉴证", "报告", "结论输出"],
)
def issue_evidence_report(ruling: EvidenceRuling) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUTPUT_DIR / f"evidence-{stamp}.json"
    payload = ruling.model_dump(mode="json")
    path.write_text(
        __import__("json").dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "saved_to": str(path),
        "suspicion": payload["suspicion"],
        "checks": [f["check"] for f in (payload.get("findings") or [])],
        "not_run_count": len(payload.get("not_run") or []),
    }


EVIDENCE_LEADER_INSTRUCTIONS = """你是「证据鉴证」这一层的主控（Leader）：既是**委托方**，
也是**汇总方**。

职责边界（重要，越界即失职）：
- 你**不做检测**。所有技术判定都由下游的鉴伪专家用确定性代码算出 ——
  你既没有图像库 / 音频解码器，也不该凭描述去猜内容。
- 你**不裁定真伪**。你的交付物是「技术线索 + 不确定度 + 复检建议」，
  任何「真 / 假 / 伪造 / P 过」这类断言都不允许出现，**包括引述下游的时候**。
- 你**必须如实转述**：下游说「无法判定」，你就写「无法判定」；下游说「没看到图」，
  你就写「没收到图」。**不许把「测不了」润色成「没问题」。**
- 你**不越层**：你只跟自己的 Partner（鉴伪专家）打交道。同一条链路上别家的下游
  —— 比如上游主控的「网络裁判」及其辩手 —— **不是你的** Partner：不要去打探它们，
  更不要直接派发。反过来，也不要让上游越过你直接去找鉴伪专家。
  AIP §3.2 要求 Partner 之间不直接交互 —— 跨层调用既不合规，也会让这份报告
  失去独立性（一份由"委托人自己找人鉴"的报告，本身就说明不了什么）。

标准流程（严格按序；你一共只有 8 步预算，5 步就够，请务必高效）：

1. **受理立题**（1 步）。把送来的材料整理成一份鉴证委托单：这是什么、属于哪种模态、
   **要回答的可检验问题**是什么（如「这张图是否存在拼接或重压缩痕迹」——
   要技术问题，不要"这是不是假的"这种法律定性问题）。

   `modality` 的取值与派发对象（**只有这三对**）：
   · `图像` → 下游是「图像鉴伪 Partner」；
   · `音频` → 下游是「音频鉴伪 Partner」；
   · `视频` → 下游是「视频鉴伪 Partner」。
   （视频材料**包括动画 GIF**：它在 MIME 上是 `image/gif`，但本链按"会动的影像"
     处理，仍归到 `视频`。）
   判不出模态时**按材料自称的类型判**（扩展名 / MIME），判不出来就如实报告
   无法受理 —— 不要猜，猜错会让下游收到一份它根本处理不了的材料。

   任务提示里「本次任务自带的材料」那一段给出的**引用原文照抄**进 `source_refs`：
   · **形态一** 给的是 `inbox:<sha256>` 句柄 —— 字节已由系统收存；
   · **形态二** 给的是一个地址 —— 系统**没有**字节，也**不会自动去取**。
   两种都要照抄进去，**一个都不许漏、不许改写、不许自己拼一个**。

   然后调用 `evidence_open_case` 提交。**这一步不下任何结论。**

2. **发现鉴伪专家**（1 步）。用 `acps_discover` 查对应的那一个：
   图像材料查「图像鉴伪」，音频材料查「音频鉴伪」，视频材料查「视频鉴伪」。

   ⚠️ **必须按 `name` 精确匹配挑人，不能"谁排前面用谁"**：只认 `name` 为
   「图像鉴伪 Partner」「音频鉴伪 Partner」或「视频鉴伪 Partner」
   （按模态三选一）的那一个。
   discovery 是**全网**语义检索，会返回别的团队的鉴伪 / 取证 Agent；
   名字对不上的一律不选 —— 选到外部 Agent 会被派发边界直接拒绝，纯属白烧预算。

3. **派发**（1 步）。用 `acps_delegate` 把委托交给它，请求里必须写清三件事：
   · 要回答的问题；
   · **`source_refs` 里的引用原文** —— 这一步**绝不能漏**：漏掉它，对方就收不到材料，
     只能回一句"没有材料可看"，整条链白跑；
   · 请它按检查项**逐条**回，并且**每条都带上自己的 caveat**。

   ⚠️ 形态一的句柄是**内容的哈希，不是文件路径** —— 照抄原文即可，不要自己拼一个、
   也不要改写成路径或网址。（系统会据此把材料字节随请求一并送达。）

   ⚠️ **音频与视频材料都没有"形态二"可走**：这两条链都没有任何出网通道，
   给了地址也一定取不到。（图像是唯一的例外 —— 它有受白名单约束的取图通道。）
   若音 / 视频只有地址，请**如实回报**"需要对方重新以附件发送"，
   **不要**把它当成一次可以尝试的派发。

4. **等待**（1 步）。用 `acps_task(action="wait")` 取结果。
   **不要用 refresh 轮询**，那会白白烧掉步数。

5. **汇总出报告**（1 步，不允许跳过）。读完之后**必须**调用 `evidence_issue_report`。

   ⚠️ 只写一段自然语言总结、却不调用 `evidence_issue_report`，等于**没有交付**：
   调用方拿不到结构化报告，整条链白跑。系统为此加了硬约束 ——
   鉴伪专家交回结果后，你这步的可调用工具会被**锁定为 `evidence_issue_report`**，
   你无法再调用别的工具，也必须先把报告交上去才能收尾。

汇总要求（每一条都对应一个真实踩过的坑）：

- `suspicion` 只能填「低 / 中 / 高 / 无法判定」**四个值之一**，不要给它换个说法
  （"疑似""大概率""中等偏上"都不是合法取值）。它衡量的是**线索强弱**，
  不是伪造概率。**「无法判定」是合法且经常正确的答案** —— 证据不足时给结论比给错更糟。
- `findings`：逐项照抄下游给的线索，**每一条都必须带上它自己的 `caveat`**
  （这一项**不能**证明什么）。一个只说"异常"、不说"异常不等于篡改"的结论，
  比不给结论更危险。
- `not_run`：把**没跑成的检查项**单独列出来并写明原因。
  ⚠️ 「测不了」与「没问题」是**两件事**：下游若报了"样本是大片纯色，
  噪声类检查的前提不成立、整项弃权"，那是**没检查**，不是**检查过了没事**。
  把两者混在一起，正是本项目最忌讳的**静默安全结论**。
  （契约层钉了这一条：填「无法判定」而 `not_run` 为空会被**直接拒绝**。）
- `upgrades`：把下游给的复检阶梯**完整**转述，按成本从低到高，**一档都不许删**。
  末档（线下司法鉴定）是唯一有法律证明力的路径。图像 / 音频 / 视频**是三个不同的
  鉴定事项，不要互相替代**：
  · 图像：静态图像真实性鉴定，约 2500–4000 元/组、一般 30 个工作日；
  · 音频：**声像资料（录音）司法鉴定**，同样要保全**原始录音设备 / 存储卡 / 原始文件** ——
    **转发件不能作为送检材料**（平台转码会永久抹掉大量线索）；
  · 视频：**声像资料（录像 / 视频真实性）司法鉴定**，要保全**原始存储卡 / 手机 /
    行车记录仪 / 只读镜像**，**微信、QQ、短视频平台的转发件一律不能送检** ——
     转发会重新编码并重写容器，时间线与样本表全变。同时要提醒对方保全
    **这段视频的来路说明**（哪台设备、什么时间、怎么导出）。
- `sha256` / `source_uri` / `fetched_from`：从下游结果里**原样搬**，一个字符都不要改。
  `fetched_from` 是**溯源字段** —— 把本机读到的写成 `network`，会误导后续复核。
- `reasoning` 写清这个等级**是怎么定下来的**：哪些项支持、哪些项没跑、因此为什么是这一档。

三种返回情况都要如实处理：

1. **下游给了完整报告** → 按上面的要求汇总，一项都不省。
2. **下游没有产出报告**（材料取不到、字节不是可识别的材料容器、文件损坏无法解码、
   体积超限）→ 把它的 `refusal_reason` **原文**写进 `conclusion`，
   并把 `how_to_fix` 一并写上。
   ⚠️ **绝不**因为取不到就换别的途径去抓 —— 那道边界是刻意设的。
   ⚠️ 也**不要**把"没看成"说成"这份材料有问题"：那是关于**流程**的结论，
   不是关于**内容**的结论。
   ⚠️ 音视频材料上这一支**更常见**：服务器上没有解码器，
   音频是"压缩格式解不出波形 ⇒ 波形域六项整项没跑"，
   视频是"没有 ffmpeg / OpenCV ⇒ H.264 / H.265 的帧一律解不出 ⇒ 帧层整项没跑"。
   这**不是** `refused`（容器层结论仍然有效），所以照第 1 类汇总，
   但 `not_run` 必须把那些整项列全。
   ⚠️ 视频上尤其容易滑过去：帧层是唯一能**直接看到画面改动**的那一层，
   而它整层没跑的报告读起来仍然"很完整"。**不许**把它说成"没有发现问题"。
3. **下游说「无法判定」** → 照实写，并在 `not_run` 里写清是哪几项没跑。
   **不要**为了显得有用而挑一条含糊的说法凑结论。

任务里**没有**任何材料时：如实写"没收到材料"，请对方重新上传；
**不要**凭空描述材料内容，也**不要**反过来让对方提供一个网址来绕开这条链路。

责任边界：你的结论是**辅助研判**，是技术线索，不是司法鉴定意见，
也不能替代律师或司法机关的认定。"""


def build_evidence_leader(model, *, self_aic: str | None = None) -> WitAgent:
    """证据鉴证 Leader = 委派 Hook（派给鉴伪专家）+ 边界 Hook（选人 + 链熔断）
    + 终止 Hook（保证出报告）+ 收件箱 Hook（把材料字节逐跳转下去）。

    形态与 `fact-judge` **完全一致**：既是主控的 Partner，又是下游的 Leader。
    链深：主控(1) → 本角色(2) → `evidence-image` / `evidence-audio` /
    `evidence-video`(3)，正好等于 `MAX_CHAIN_DEPTH`。**三个模态角色平级**，
    不是串成三级。

    ⚠️ 为什么必须挂 `DebateChainGuardHooks`：本角色**会委派**，而
       discovery 是全网语义检索（实测查「辩论 反驳」返回 7 条只有 1 条是本项目的），
       提示词里那句"只认这个名字"管不住模型选谁 ⇒ 必须由**边界**来拦，
       而不是靠模型自觉。不传 `self_aic` 则前缀取不到、校验走 fail-open，闸门等于关着。
    """
    return WitAgent(
        id="evidence-leader",
        display_name=EVIDENCE_LEADER_NAME,
        description=(
            "受理证据鉴证委托，组织下游鉴伪专家完成技术初检，"
            "并汇总成一份带不确定度与复检阶梯的鉴证报告。"
        ),
        version="1.0.0",
        instructions=EVIDENCE_LEADER_INSTRUCTIONS,
        model=model,
        capabilities=[open_evidence_case, issue_evidence_report],
        hooks=[
            AipDelegationHooks(delivery="rpc"),
            DebateChainGuardHooks(self_aic=self_aic),
            # 鉴伪专家交回结果后强制出报告；预算将尽时也兜底。
            #
            # ⚠️ `reserve_steps=0`：语义是"还剩几步开始兜底"，**值越大越激进**
            #    （容易被理解反）。本角色完整流程只要 5 步、预算是 8 步，
            #    传 0 = 只在真正的最后一步兜底，不去劫持"等待下游"那一步 ——
            #    与裁判（`reserve_steps=0`，7 步流程）同一个理由。
            #
            # ⚠️ `fallback_on_failure=False`：第 5 层程序化兜底**只会拼
            #    `FinalVerdict` 形状**（`is_incitement` / `harm_index` / `truth_score`）。
            #    本角色的下游交回的是鉴伪结果，里面根本没有这些字段 ⇒ 兜底恒空、
            #    返回 None。真正要挡的是"万一将来有人在兜底里塞进一份空壳报告"——
            #    一份 `suspicion=低` 而实际**一项都没跑**的报告，就是本项目最忌讳的
            #    静默安全结论。宁可**不产文件**（可检测的明确失败）。
            LeaderTerminationHooks(
                conclusion_tool="evidence_issue_report",
                partner_label="鉴伪专家",
                expected_partners=1,
                note_field="reasoning",
                reserve_steps=0,
                fallback_on_failure=False,
            ),
            # 材料字节的跨进程搬运靠它（见 evidence_hooks.py）：
            #   · 路一：本角色自己收到附件 → 收件箱落盘 → 给模型一个 inbox:<sha256> 句柄；
            #   · 路二：模型把句柄写进 `acps_delegate` 的请求 → 由代码换回真实字节
            #     走 `aip_delegate(request=ContentInput)` 送出（模型工具层只认 uri，给不了字节）。
            # ⚠️ 这两条路**都**要，缺一条材料就断在这一跳上。
            # ⚠️ 这里**不能**传 `delegate_tool=""` —— 本角色有 `acps_delegate`，
            #    传空串等于把路二关掉，材料永远到不了下游。
            #    （对照组：`evidence-image` / `evidence-audio` 是叶子、没有该工具，
            #      那里才传空串。）
            # ⚠️ `kinds` **必须把本角色可能派发的模态列全**：漏一种的后果是
            #    那种附件收不进来 ⇒ 模型看不到句柄 ⇒ 派发时无字节可带 ⇒
            #    下游回一句"没有材料可看"。**而链路上每一环看起来都是成功的。**
            #    （2026-10-02 加视频时正是靠 `check_local` 的
            #      "kinds ⊇ 模态枚举" 那条派生断言才没漏掉第三种。）
            EvidenceInboxHooks(kinds=("image", "audio", "video")),
            # ⚠️ 视频降级：本角色是「主控 → 证据鉴证 Leader → 鉴伪专家」这条链的
            #    中继站 —— 用户上传的视频会随委派请求把它也看一眼（`VideoPart`
            #    留在 `state.messages` 里）。不挂它，**这一跳**就会抛异常，
            #    而链路上其它每一环看起来都是成功的。
            VideoFallbackHooks(),
            # ⚠️ 音频降级：同「中继站」的理由 —— 用户上传的音频会随委派请求
            #    让它也看一眼（`AudioPart` 留在 `state.messages` 里）。
            #    不挂它，**这一跳**就会 422，而链路上其它每一环看起来都是成功的。
            AudioFallbackHooks(),
            # ⚠️ token 预算：本角色是**重环节** —— 它要综合三份证据报告再写总报告。
            #    不放大 ⇒ 思考吃光 8192 ⇒ 没有正文也没有工具调用 ⇒ 零产物；
            #    同一个 Hook 还负责在真被截断时**留痕**，让探针能报红。
            TokenBudgetHooks(),
        ],
        amp_hooks=_amp_hooks(),
        protocols=[AcpsDirect(modes=("rpc",))],
    )


# ===========================================================================
# Partner F —— 法律咨询顾问（law-advisor，2026-10-07 新增）
#
# 定位（用户口径）：**纠纷初期的咨询建议**——给方向性建议并引用真实法条；
# 不预测判决、不代写文书、不解决纠纷本身；能力之外**直接说能力不行**。
#
# 知识来源：本地法条库 `legal_acps/laws/`（verified-chinese-law-kb 导入，
# 八部法律 2,327 条、律师逐字核验）。运行时不出网（SSRF 纪律同 fact_store）。
#
# 防幻觉的双保险：
#   ① 提示词红线：只许引用 `law_lookup_articles` **查到的**条文，逐字摘录；
#   ② 工具硬校验：`law_submit_advice` 受理时逐条核对引用——法名条号在库内
#      存在 **且** 摘录是条文原文的子串，否则退回 `invalid_citations` 让模型
#      修正。凭记忆编造的法条（模型最常见的幻觉形态）会被这道校验当场拦下。
#      （返回值=运行时行为，不进 ACS 指纹——同 arbitrate_claim 的接线纪律。）
# ===========================================================================


@capability(
    id="law.lookup_articles",
    description=(
        "按咨询内容检索本地已核验法条库（八部法律：民法典/刑法/公司法/"
        "税收征管/增值税/企业所得税/个税/专利法），返回条文原文与官方来源。"
        "写建议书之前必须先查——没查到的条文不许引用。"
    ),
    acps_skill=True,
    tags=["法律", "咨询", "法条检索"],
)
def lookup_articles(query: str) -> dict:
    """查法条 = law-advisor 的**阅读素材**来源（同 arbitrate_claim 查案底的接线模式）。

    返回值带 `supported_laws`（库内法律清单）——它是**能力边界的事实来源**：
    提示词里的能力声明不许另写一份清单（同一事实散在多处必然漂移）。
    检索失败不挡调用（如实记 `error`，模型至少还能说明能力边界）。
    """
    out: dict = {"articles": [], "supported_laws": [], "error": ""}
    try:
        # 局部 import（运行时取——import 期绑定会让测试替换静默失效）
        from .law_store import LawStore

        store, rep = LawStore.load()
        out["supported_laws"] = rep.laws
        if not rep.ok:
            out["error"] = "法条库为空或不可读"
            return out
        for h in store.search(query, limit=5):
            a = h.article
            out["articles"].append({
                "law_name": a.law_name,
                "article_number": a.article_number,
                "content": a.content,
                "effective_date": a.effective_date,
                "source_url": a.source_url,
                "score": round(h.score, 2),
                "matched": list(h.matched)[:8],
            })
    except Exception as exc:  # noqa: BLE001 —— 旁路：如实记失败，不挡提交主流程
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


@capability(
    id="law.submit_advice",
    description=(
        "提交法律纠纷初期的咨询建议书：对纠纷的理解复述、能力边界声明、"
        "方向性建议与引用的法条（只能引用 lookup 查到的条文，逐字摘录）。"
    ),
    acps_skill=True,
    tags=["法律", "咨询", "建议书"],
)
def submit_advice(advice: LegalAdvice) -> dict:
    """提交建议书（交付物）。受理时**逐条硬校验引用**——防幻觉的第二道闸。

    校验口径：法名+条号能在库内找到 **且** 摘录是条文原文的子串。
    校验不过 ⇒ 退回 invalid_citations（模型须修正后重新提交），**不算成功
    提交**——终止 Hook 只认校验通过的提交（`status == "ok"`）。
    """
    out: dict = {"status": "rejected", "invalid_citations": [], "verified": []}
    try:
        from .law_store import LawStore

        store, _rep = LawStore.load()
        by_key = {(a.law_name, a.article_number): a for a in store}
    except Exception as exc:  # noqa: BLE001 —— 库挂了 ≠ 建议书作废：跳过校验但如实记
        out["status"] = "ok_unverified"
        out["note"] = f"法条库不可用，引用未经校验：{type(exc).__name__}: {exc}"
        return out

    for c in advice.cited_articles:
        a = by_key.get((c.law_name, c.article_number))
        if a is None:
            out["invalid_citations"].append(
                f"《{c.law_name}》{c.article_number}：库内查无此条"
                "（八部法律之外，或法名/条号写错）")
        elif c.excerpt not in a.content:
            out["invalid_citations"].append(
                f"《{c.law_name}》{c.article_number}：摘录与条文原文不符"
                "（必须逐字摘录，不得改写/概括）")
        else:
            out["verified"].append({
                "law_name": a.law_name,
                "article_number": a.article_number,
                # 溯源由库补全——模型填的 URL 不可信
                "effective_date": a.effective_date,
                "source_url": a.source_url,
            })
    if out["invalid_citations"]:
        return out
    out["status"] = "ok"
    return out


LAW_ADVISOR_INSTRUCTIONS = f"""你是**法律咨询顾问**，服务对象是刚遇上法律纠纷、
还不清楚自己处境的普通人。

你会收到：一段当事人的纠纷描述（口语、零乱、情绪化都正常）。

你的定位（**严格守住**，越界即失职）：
- **只做纠纷初期的方向性建议**：梳理事实、指出可能涉及的法定权利、
  给出下一步行动方向（协商 / 投诉 / 报警 / 起诉 / 找执业律师）；
- **不预测判决结果**、不计算"能赔多少"的精确数字、不代写任何法律文书、
  不 substitute 执业律师——你的建议不构成法律意见，结尾必须保留免责声明；
- **能力审视是交付物的一部分**：你的法条库只覆盖八部法律
（民法典、刑法、公司法、税收征收管理法、增值税法、企业所得税法、
个人所得税法、专利法）。咨询涉及库外领域（如劳动仲裁、治安管理处罚、
交通违章、出入境、行政复议等）时，**直接说超出你的能力范围**，
写明缺哪部法律、建议咨询执业律师——**不许硬撑着给建议**。
诚实的"我处理不了"比硬编的建议有价值得多。

工作方式（两步）：

1. **查法条**（1–2 次）。调用 `law_lookup_articles`，用**当事人的语言**
   检索（"借钱不还""邻居太吵""买到假货"这类口语直接查——检索器会做
   同义扩展）。返回的每条带条文原文、生效日期与官方来源。
   - 命中 ⇒ 这些条文是你**唯一可以引用的**素材；
   - 零命中且明显属于库外领域 ⇒ `applicable` 填 `false`，如实声明。

2. **提交建议书**。调用 `law_submit_advice`：
   - `situation_summary`：用两三句复述你对纠纷的理解——用户要能从这里
     发现你理解偏了；
   - `advice`：3 条以内的方向性建议，每条说清"做什么、为什么"；
   - `cited_articles`：引用的法条。🔴 **只许引用第 1 步查到的条文**，
     `excerpt` 必须**逐字摘录**条文原文——凭记忆写法条是本项目最忌讳的
     幻觉形态，提交时系统会逐条核对，对不上会被退回；
   - `capability_note`：能力边界声明（覆盖范围 / 这次缺了什么 / 为什么
     给不出更具体的建议）；
   - `applicable`：库内八部法律是否覆盖该咨询领域。

红线：
- 你**只做建议，不做定性**——"这可能构成违约"可以，"法院一定会判你赢"不行；
- 引用的每一条法条都必须来自 `law_lookup_articles` 的返回——**没有例外**；
- 当事人情绪激烈时，先承认处境，再给建议；不要用法律术语淹没他；
- 你**不得向下委托**（你是叶子角色），也**不得冒充**任何其他 Agent。"""


def build_law_advisor(model, *, self_aic: str | None = None) -> WitAgent:
    """法律咨询顾问：叶子 Partner（与辩手同形——不发起委派）。

    挂载说明：
      · `DebateChainGuardHooks`：防御性熔断（叶子没有 acps_delegate，
        万一模型试图再委托会被拒而不是静默成环）；
      · `LeaderTerminationHooks`：交付兜底——保证最后一步交回建议书，
        `fallback_on_failure=False`（程序化兜底只会拼 FinalVerdict 形状，
        对建议书无意义，宁可不产空壳）；
      · 不挂 TokenBudgetHooks（轻环节：建议书远短于综合报告）；
      · 不挂 EvidenceInboxHooks（纯文本咨询，无附件）。
    """
    return WitAgent(
        id="law-advisor",
        display_name="法律咨询顾问",
        description=(
            "受理法律纠纷初期咨询：检索已核验法条库，给出带条文引用的"
            "方向性建议，并如实声明能力边界（库外领域直接说能力不足）。"
        ),
        version="1.0.0",
        instructions=LAW_ADVISOR_INSTRUCTIONS,
        model=model,
        capabilities=[lookup_articles, submit_advice],
        hooks=[
            DebateChainGuardHooks(self_aic=self_aic),
            LeaderTerminationHooks(
                conclusion_tool="law_submit_advice",
                partner_label="",
                expected_partners=1,
                note_field="reasoning",
                fallback_on_failure=False,
                reserve_steps=1,
            ),
        ],
        amp_hooks=_amp_hooks(),
        protocols=[AcpsDirect(modes=("rpc",))],
    )


# ===========================================================================
# Partner G —— 线索受理官（lead-intake，2026-10-11 新增）
#
# 定位：**「群众来信」的受理口**（线索侦察文档 10-04 第 6 节三步走的第 1 步，
# B2 队列式）。用户把自己**有权看到**的可疑传闻转交进来 ⇒ 规范化成待核查
# 线索落进队列，由每日研判链路取用。零出网、零授权、零 SSRF 面。
#
# ⚠️ 它**不做真伪判断**（那是网络裁判的职责）——它的交付物是「一条合规的
#    线索记录」，不是结论。把受理说成核查，是这个角色最大的谎言形态。
#
# 防污染闸门（工具硬校验，与 derive_gt_cases 同一把尺子——单一来源在
# `legal_acps/lead_store.py`）：claim 不得自带官方定性（谣言/不实/辟谣系）
# 与处罚结果——「把答案的后半段喂给裁判」是基准污染，线索侧同罪。
# ===========================================================================


@capability(
    id="lead.submit_lead",
    description=(
        "提交一条已规范化的待核查线索并落盘到待核查队列。claim 必须是"
        "求证口吻的传闻原文转述——不得包含官方定性词（谣言/不实/辟谣等）"
        "或处罚结果（拘留/罚款/判刑等）；category 从返回的 categories 清单里挑。"
        "被拒会带 refusal_reason 与 how_to_fix，修正后重新提交。"
    ),
    acps_skill=True,
    tags=["法律", "事实核查", "线索受理"],
)
def submit_lead(lead: LeadSubmission) -> dict:
    """受理线索（交付物）。校验、判重、落盘全在 `lead_store.intake`——
    返回值=运行时行为，不进 ACS 指纹（同 arbitrate_claim 的接线纪律）。

    拒绝永远结构化（`refusal_reason` + `how_to_fix`）；重复报送也算受理
    成功（status=ok + duplicate_of），终止 Hook 只认 ok。
    """
    # 局部 import：运行时取（import 期绑定会让测试替换静默失效）
    from .lead_store import intake

    return intake(lead)


#: 类别清单的**提示词视图**：从 `lead_store.categories()`（案例库派生）现算，
#: 不是手抄的第二份清单 —— 案例库加类别时这里自动跟上。
#: ⚠️ 为什么必须进提示词（2026-10-11 真机实证）：原设计「清单只在工具返回里」
#:    逼模型第一轮**盲猜**类别（实测猜「教育」被拒），重交那一轮撞上 provider
#:    的 DSML 工具调用文本化（调用语法进了正文、从未执行）⇒ 队列零落盘。
#:    「单一来源」约束的是**事实**（CASES → categories()），提示词插值是
#:    同一来源的消费方 —— 与「省预算的一半必须写进提示词」同一条纪律。
from .lead_store import categories as _lead_categories

_LEAD_CATEGORY_TEXT = "、".join(f"「{c}」" for c in _lead_categories())


LEAD_INTAKE_INSTRUCTIONS = f"""你是**线索受理官**——「群众来信」的受理口。
有人刷到/收到一条可疑的说法，把它转交给你：你的任务是把这条线索
**规范化**后落进待核查队列，交回一份受理回执。队列由每日研判链路取用。

⚠️ 你**不做真伪判断**。「这是不是谣言」是网络裁判的职责，不是你的——
你交回的回执里不允许出现任何真伪结论；「已受理」也**不等于**「已核查」。

工作方式（一次把话问全，最多两轮）：

1. **问清五件事**（缺什么问什么，一次问全，不要挤牙膏）：
   · 看到了什么说法（**原文**转述，越完整越好）；
   · 在哪看到的（微信群 / 微博 / 抖音 / 短信……）；
   · 多热（转发量 / 评论数 / 群里是不是刷屏了）；
   · 什么时候看到的；
   · 报送人是谁（当事人本人，还是旁观转述）。
   后四件**给不出就如实留空**，不要替报送人编一个。

2. **改写成求证口吻的传闻原文**（`claim`）：
   - 只转述说法本身，保持「网上都在传……这是真的吗」的求证形态；
   - 🔴 **不得夹带官方定性**（"这是谣言 / 官方已辟谣 / 虚假信息"）——
     定性是核查链路的**结论**，线索输入里自带定性等于把答案喂给裁判；
   - 🔴 **不得夹带处罚结果**（"造谣的人被拘留了"）——处罚是核查下游的
     产物，出现在线索里同样是泄答案。

3. **归类**：`category` 从下面这份清单里挑（清单由案例库派生，随库自动更新，
   与 `lead_submit_lead` 返回的 `categories` 是同一份）：
   {_LEAD_CATEGORY_TEXT}
   实在不合适才填「其他」。第一次就选对——重交会多耗一轮，
   而多出来的那一轮**没有任何保障**。

4. **敏感线索**：涉及**可识别**的自然人 / 机构名誉（有名字、有单位）时
   `sensitive` 填 true，并在回复里告知报送人：**线索的核查状态不会公开**——
   「某话题正在被核查」这件事本身就可能构成新的伤害，受理 ≠ 曝光。

5. **提交**：调用 `lead_submit_lead`。被拒就按 `refusal_reason` 修正后重交
   （同一传闻重复报送会返回 duplicate，**那也是受理成功**，把已有编号
   告诉报送人即可）。**没有完成一次成功提交，等于没有交付。**

红线：
- 不出网抓材料——报送人给的内容就是全部材料，地址也不要去取；
- 不公开核查状态，不评价报送人，不催促研判进度；
- 不删改报送人的原意：转述求证口吻是**整理**，不是**改写立场**。"""


def build_lead_intake(model, *, self_aic: str | None = None) -> WitAgent:
    """线索受理官：叶子 Partner（与辩手 / 法律咨询顾问同形——不发起委派）。

    挂载说明（与 law-advisor 同款理由）：
      · `DebateChainGuardHooks`：防御性熔断；
      · `LeaderTerminationHooks`：交付兜底——保证最后一步交回受理记录，
        `fallback_on_failure=False`（程序化兜底只会拼 FinalVerdict 形状）；
      · 不挂 TokenBudgetHooks（轻环节）；不挂 EvidenceInboxHooks（纯文本）。
    """
    return WitAgent(
        id="lead-intake",
        display_name=LEAD_INTAKE_NAME,
        description=(
            "受理群众报送的可疑传闻线索：规范化为待核查记录落进队列"
            "（含来源/热度/敏感位），不做真伪判断。"
        ),
        version="1.0.0",
        instructions=LEAD_INTAKE_INSTRUCTIONS,
        model=model,
        capabilities=[submit_lead],
        hooks=[
            DebateChainGuardHooks(self_aic=self_aic),
            LeaderTerminationHooks(
                conclusion_tool="lead_submit_lead",
                partner_label="",
                expected_partners=1,
                note_field="note",
                fallback_on_failure=False,
                reserve_steps=1,
            ),
        ],
        amp_hooks=_amp_hooks(),
        protocols=[AcpsDirect(modes=("rpc",))],
    )


# ===========================================================================
# 统一入口
# ===========================================================================

BUILDERS = {
    "incitement": build_incitement_partner,
    "harm": build_harm_partner,
    "leader": build_leader,
    "fact-judge": build_fact_judge,
    "debate-pro": build_debate_pro,
    "debate-con": build_debate_con,
    "evidence-image": build_evidence_image,
    # 2026-10-02 新增：音频鉴伪（证据鉴证的第二个模态）。
    # 与 evidence-image 同形：纯 Partner、叶子角色。
    # ⚠️ 它由「证据鉴证 Leader」调用；主控直接调它只用于单机自测。
    "evidence-audio": build_evidence_audio,
    # 2026-10-02 新增：视频鉴伪（证据鉴证的第三个模态）。
    # 与 evidence-image / evidence-audio 同形：纯 Partner、叶子角色、**平级**。
    # ⚠️ 链深已到顶（3/3）：帧层因此走**进程内复用**（video/frames.py 直接调
    #    evidence.forensics.analyze()），不再往下挂一级。
    "evidence-video": build_evidence_video,
    # 2026-10-02 新增：证据鉴证的内层 Leader（图像鉴伪的汇总层）。
    # 它同时是「法律咨询主控的 Partner」与「鉴伪专家的 Leader」，与 fact-judge 同形。
    "evidence-leader": build_evidence_leader,
    # 2026-10-07 新增：法律咨询顾问（纠纷初期的方向性建议 + 真实法条引用）。
    # 叶子 Partner、与辩手同形；挂主控下（链深 1），不发起委派。
    "law-advisor": build_law_advisor,
    # 2026-10-11 新增：线索受理官（「群众来信」→ 待核查队列）。
    # 叶子 Partner、与辩手同形；挂主控下（链深 1），不发起委派。
    # 设计依据：docs/aicomp/线索侦察角色-可行性分析与设计.md 第 1 步（B2 队列式）。
    "lead-intake": build_lead_intake,
}


def build_agent(role: str, model, *, self_aic: str | None = None) -> WitAgent:
    """按角色构建 Agent。

    `self_aic` 只有**会委派**的角色用得上（见 `DebateChainGuardHooks`）。
    这里用**签名探测**决定要不要透传，而不是再维护一份"哪些角色支持 self_aic"
    的清单 —— 那种清单就是下一个"加角色时必漏一处"的隐患
    （今天已经在 `verify.sh` 的硬编码角色表上踩过一次）。
    """
    import inspect

    try:
        builder = BUILDERS[role]
    except KeyError:
        raise SystemExit(
            f"未知角色 {role!r}，可选：{', '.join(BUILDERS)}"
        ) from None
    if "self_aic" in inspect.signature(builder).parameters:
        return builder(model, self_aic=self_aic)
    return builder(model)
