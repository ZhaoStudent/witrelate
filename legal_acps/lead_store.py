"""线索受理队列 —— 「群众来信」形态的线索入口（2026-10-11 新增 lead-intake）。

设计依据：``docs/aicomp/线索侦察角色-可行性分析与设计.md``（10-04）第 6 节
「三步走」的**第 1 步**（B2 队列式）：

    线索由外部递入（用户把自己**有权看到**的内容转交进来），
    受理官规范化后落进队列；每日研判链路按需取用。
    —— 零出网、零授权、零 SSRF 面（主动发现是第 3 步，须单独过合规论证）。

该文档 §4.3 定的硬门槛（「先离线回测达标，才做线索入口」）现已满足：
校准台账 2026-10-11 一致率 21/22、弃权 5%、负控在——**按文档的次序，
这一步现在有资格做了**。

════════════════════════════════════════════════════════════════════
单一来源（本项目硬规矩：同一判定写两处必然漂移）
════════════════════════════════════════════════════════════════════

· ``FALSE_PAT`` / ``PUNISH_PAT`` 的**定义在本模块**；``tools/derive_gt_cases.py``
  从这里 import（它不能被 runtime import，但 runtime 模块可以被 tools import）。
  两处共用同一把「不许把答案喂给裁判」的尺子：derive 筛校准候选、
  lead-submit 拦自带定性的线索。
· 类别枚举从 ``case_library.CASES`` **现场派生**，不手抄清单
  （加类别时这里不报错、只是自然认得——手抄清单则必然漏）。
· 归一化判重复用 ``ruling_store._norm_event``（折叠同一把尺子）。

════════════════════════════════════════════════════════════════════
台账纪律（与 runs.jsonl / case_rounds.jsonl 同款）
════════════════════════════════════════════════════════════════════

· ``.wit/leads/queue.jsonl``    —— 受理记录，append-only，**只存事实**；
· ``.wit/leads/consumed.jsonl`` —— 消费记录（哪条线索被哪一轮取用），append-only；
· 「待处理」**读时现场算**（queue − consumed），不写第二份状态文件
  —— 存结论的副本必然与事实漂移（发起轮台账同一条教训）。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .contracts import LeadSubmission
from .ruling_store import _norm_event

#: 官方定性（「不实」）模式。**定义处**（单一来源）——derive_gt_cases 从这里取。
#: ⚠️ 裸「属实」会误匹配「**不**属实」——不实系表达必须整词列出（负对照须覆盖）。
FALSE_PAT = re.compile(
    r"不属实|情况不实|信息不实|.{0,6}系谣言|属谣言|"
    r"为不实信息|不实信息|虚假信息|系编造|编造.{0,8}谣言|"
    r"为谣言|是谣言|严重不实"
)

#: 处罚类字样。claim 里不该有——处罚结果是核查链路的**下游产物**，
#: 出现在线索里说明报送人已经把结论喂进来了（基准污染同型）。
PUNISH_PAT = re.compile(r"拘留|刑拘|行政处罚|刑事强制|罚款|判刑|立案侦查")

#: claim 最短长度（与 derive 工具的 title 提取前置线同值——同一条纪律）。
MIN_CLAIM_LEN = 8


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def leads_dir(wit_dir: str | Path | None = None) -> Path:
    """线索队列目录。默认 ``<repo>/.wit/leads``（跨角色共享——由受理官写、
    每日研判链路读，不属于任何单一角色的工作区）。"""
    if wit_dir is not None:
        return Path(wit_dir) / "leads"
    return repo_root() / ".wit" / "leads"


def queue_path(wit_dir=None) -> Path:
    return leads_dir(wit_dir) / "queue.jsonl"


def consumed_path(wit_dir=None) -> Path:
    return leads_dir(wit_dir) / "consumed.jsonl"


def categories() -> list[str]:
    """合法类别清单：**现场派生自案例库** + 「其他」兜底。

    ⚠️ 不许手抄（同一事实散在多处 ⇒ 加类别必漏）。
    """
    from .case_library import CASES  # 局部 import：运行时取

    return sorted({c.category for c in CASES} | {"其他"})


def lead_id_of(claim: str) -> str:
    """线索 id = 归一化 claim 的短哈希（同一传闻重复报送 ⇒ 同 id）。"""
    return "lead-" + hashlib.sha1(_norm_event(claim).encode("utf-8")).hexdigest()[:10]


def validate_lead(lead: LeadSubmission) -> list[str]:
    """纯函数校验器（可注入做负对照）。返回拒绝理由列表；空 = 通过。

    三道闸门（与 derive_gt_cases 的 input 纪律同一把尺子）：
      ① claim 非空且 ≥ MIN_CLAIM_LEN；
      ② claim **不含**官方定性词——线索是「待核查」的输入，自带定性
         = 把答案喂给下游裁判；
      ③ claim **不含**处罚结果——处罚是核查链路的下游产物；
      ④ category ∈ 案例库类别枚举 ∪ {其他}（单一来源派生）。
    """
    reasons: list[str] = []
    claim = (lead.claim or "").strip()
    if len(claim) < MIN_CLAIM_LEN:
        reasons.append(
            f"claim 太短（{len(claim)} < {MIN_CLAIM_LEN}）：传闻原文请完整转述"
        )
    if FALSE_PAT.search(claim):
        reasons.append(
            "claim 里含官方定性词（如「谣言/不实/辟谣」系表述）："
            "定性是核查链路的结论，线索输入不得自带——请只转述传闻本身"
        )
    if PUNISH_PAT.search(claim):
        reasons.append(
            "claim 里含处罚结果（拘留/罚款/判刑等）：处罚是核查下游的产物，"
            "线索里不该有——请删去后重新提交"
        )
    if lead.category not in categories():
        reasons.append(
            f"category {lead.category!r} 不在合法清单里：{categories()}"
        )
    return reasons


def _append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            out.append(json.loads(ln))
    return out


def intake(lead: LeadSubmission, wit_dir=None) -> dict:
    """受理一条线索：校验 → 判重 → 落盘。**工具层入口**。

    返回永远是结构化的（失败也带 ``refusal_reason`` + ``how_to_fix``）：
      · ``status="ok"``          —— 受理成功（重复报送也算成功，附 ``duplicate_of``）；
      · ``status="rejected"``    —— 闸门拦下（带理由与修法）。
    终止 Hook 只认 ``ok``（重复受理也是交付，不是失败）。
    """
    out: dict = {
        "status": "rejected",
        "refusal_reason": "",
        "how_to_fix": "",
        "categories": categories(),
    }
    reasons = validate_lead(lead)
    if reasons:
        out["refusal_reason"] = "；".join(reasons)
        out["how_to_fix"] = "按 refusal_reason 逐条修正后重新调用 lead_submit_lead"
        return out

    lid = lead_id_of(lead.claim)
    existing = {r.get("lead_id") for r in _read(queue_path(wit_dir))}
    record = {
        "lead_id": lid,
        "claim": lead.claim.strip(),
        "reporter_type": lead.reporter_type,
        "platform": lead.platform,
        "virality": lead.virality,
        "observed_at": lead.observed_at,
        "category": lead.category,
        "sensitive": lead.sensitive,
        "note": lead.note,
    }
    out.update(record)
    if lid in existing:
        # 同一传闻重复报送：不重复入队（队列只存事实一次），
        # 但受理本身是成功的——报送人需要回执，不是报错。
        out["status"] = "ok"
        out["duplicate_of"] = lid
        out["received_at"] = ""
        return out

    _append(queue_path(wit_dir), record)
    out["status"] = "ok"
    out["duplicate_of"] = ""
    out["queue_len"] = len(_read(queue_path(wit_dir)))
    return out


def pending(wit_dir=None) -> list[dict]:
    """待处理线索 = queue − consumed（**读时现场算**，不存第二份状态）。"""
    consumed = {r.get("lead_id") for r in _read(consumed_path(wit_dir))}
    return [r for r in _read(queue_path(wit_dir)) if r.get("lead_id") not in consumed]


def consume(lead_id: str, *, consumer: str, run_id: str = "", wit_dir=None) -> bool:
    """记一次消费（append-only 事实）。谁取用、哪一轮，**不存结论**。"""
    known = {r.get("lead_id") for r in _read(queue_path(wit_dir))}
    if lead_id not in known:
        return False
    _append(consumed_path(wit_dir), {
        "lead_id": lead_id, "consumer": consumer, "run_id": run_id,
    })
    return True
