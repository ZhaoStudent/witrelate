"""真实案例定期研判的执行器 —— 一轮 = 真的跑一次完整链路（#27，2026-10-05）。

它解决的问题
------------
`case_library` 只回答"该跑哪个案子"；本模块负责**真的把它跑起来，并当场体检**。
访问量是时间的函数，停一天就永久少一天 —— 所以这件事要**定时、自动、无人值守**地做。
而无人值守的前提是：**出了问题有人看得见**。所以一轮跑完不是"打印个 OK 就完"，
而是四项体检逐项落台账。

为什么是一个**独立进程**，而不是在常驻服务里加个定时器
--------------------------------------------------
常驻服务（`acps-*.service`）与"一次性研判"是**两条路**：

  · 常驻服务收的是平台派进来的 inbound 任务，绑着端口、靠 Registry 会话活着；
  · 一次性研判（`run_agent leader -i …`）只跑一轮就退出，**不绑任何端口**。

在常驻服务里加定时器，等于让一个正在对外服务的进程去跑一段长任务：它会在跑的时候
占用收件箱与产物目录（`run_ledger` 的轮次锁拦的就是这件事），而且一旦崩掉，
**平台那一侧看到的"这个 Agent 在线"就成了假的** —— 而"声明在线却连不上"比"没起服务"
更糟（本项目把这条钉成了硬规矩）。所以本模块**另起一个进程**，跑完就退。

`cwd` 必须是仓库根 —— 这是硬要求，不是便利
------------------------------------------
`run_agent.run_leader` 里，wit 的 `workspace=".wit/leader"` 是 **CWD 相对**的，
而台账的默认根是**仓库相对**的（`run_ledger._default_wit_dir()`）。两者**只在
CWD == 仓库根**时重合，而 `agents.write_trace_sidecar()` 读的是仓库相对那份会话库。
⇒ 子进程必须以仓库根为 CWD 启动，否则会写出**两份互不相干的 `.wit`**：
任务库一份、台账一份，产物在第三处，谁都不报错。

两个闸门（跑之前）
------------------
1. **忙闸门**：同角色的轮次锁/台账显示还有一轮在跑 ⇒ **不启动**。
   ⚠️ 它不是锁的替代品。真正的互斥仍然只在 `run_ledger.acquire_run_lock()` 那一处；
      这里读的是**台账**（"有没有一轮有开无收且进程还在"），回答的是另一个问题。
      两者不一致时按"忙"处理（安全方向），但**不能说**这一步就等于拿到了互斥。
2. **偏移闸门**：常驻服务刚起来（最年轻的那个进程存活不足 `OFFSET_MIN_S` 秒）⇒ 不跑。
   出处：`scripts/serve.sh` 与每个单元文件都写着 ——「多个进程同时用同一账号登录
   Registry，后登录的会把先前会话顶掉」。一轮案例研判在自己开头要登录 Registry
   （主控要 discover 下游），**正好会撞上常驻服务的启动窗口**。
   ⚠️ `OFFSET_MIN_S` 不是量出来的精确上界，而是"明显已经过了启动期"的粗判
      （当前最大错开延迟 108 s + 一次实测约 13 s 的登录 + 建 agent 的时间）。
      它只决定"今天跑不跑"，不参与任何正确性判定 —— 所以宁可取大一点。

四项体检（跑之后）
------------------
| # | 体检 | 判据 | 为什么要它 |
|---|------|------|-----------|
| 1 | 台账 | 新增**恰好**一条本轮的收尾记录，且 `status` 不是 `raised:*` | 「进程退出码 0」说明不了这一轮被记过账 |
| 2 | 产物 | `output/` 新增 `verdict-*.json` | **唯一**能说明"结构化结论真的交出来了"的判据 |
| 3 | 端口 | 8 个常驻端口仍在监听 | 本轮不许把常驻服务弄掉线（会话被顶、端口被抢都会在这里现形） |
| 4 | 凭据 | `logs/*.log` 新增的字节里没有凭据嫌疑行 | 上面那条的**原因**：端口没了是现象，凭据被顶才是病 |

⚠️ 体检 2 里 **`ruling-*` 只是如实报告，不是硬判据**：一轮里主控可能走"事实核查"
   分支（产出 ruling），也可能走"证据鉴证"分支（产出 evidence-*）。
   把 `ruling` 写成硬判据，会在合法的证据分支上**误报失败** —— 而一个会误报的
   看板比没有看板更糟（本项目已有同类教训：永久假警报会让真失败淹没在里面）。

退出码与"跳过 ≠ 通过"
--------------------
    0  一轮跑完，四项体检全过
    1  跑完了，但体检有未通过项
    2  **没跑**（被闸门拦下：忙 / 偏移不足）—— 专项记录里 `outcome="skipped"`
    3  环境性问题（案例库自检不过、找不到 `ss`、台账读不出来）

"没跑"必须是**独立的退出码**，不许并进 0：跳过不等于通过，而"今天没跑"恰恰是
最需要被人看见的一件事（访问量那天就永久少了一天）。
systemd 单元用 `SuccessExitStatus=0 2` 把 2 排除在"失败"之外，避免单元长期变红；
真相在 `case_rounds.jsonl` 的 `outcome` 字段与屏幕上，不在单元状态里。

专项记录：`.wit/<role>/case_rounds.jsonl`
----------------------------------------
append-only、只增不改、一轮一条 —— 与 `runs.jsonl` 同一个取向。它记的是**体检结论**
（`runs.jsonl` 只记轮次本身），外加当时的闸门读数，这样"那天为什么没跑"有据可查。

    ⚠️ 写入纪律与 `run_ledger._append` 一致（`O_APPEND` + **一次 write 写完一整行**），
       但**刻意没有复用**那个函数：它是 `RunRecord` 专用的私有函数（字段集完全不同）。
       要复用就得把它改成"接受任意 payload"，那会动到一条被三方引用、且有 18 用例
       元验证（约 9 分钟）盯着的热路径 —— 为了省 4 行代码而承担那个代价不划算。
       这里把这个取舍写下来，是为了让下一个人知道**它是想过的**，不是漏了。

    ⚠️ 它是**旁路记录**：写不下去只告警，不改本轮结论（同 `run_ledger` 的口径）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import case_library
from .acps_config import DEPLOY_ROLES, PORTS
from .agents import OUTPUT_DIR
from .case_library import CaseDef, RotationPlan, case_label, plan_next
from .run_ledger import DEFAULT_ROLE, RunRecord, pid_alive, read_runs

__all__ = [
    "CRED_PATTERNS",
    "EXIT_CHECKS_FAILED",
    "EXIT_ENV",
    "EXIT_OK",
    "EXIT_SKIPPED",
    "OFFSET_MIN_S",
    "ROUND_TIMEOUT_S",
    "ROUNDS_LEDGER_NAME",
    "SERVICE_NAME",
    "TIMER_NAME",
    "CaseRoundResult",
    "Check",
    "check_artifacts",
    "check_credentials",
    "check_ledger",
    "check_ports",
    "expected_ports",
    "log_paths",
    "output_dir",
    "repo_root",
    "rounds_ledger_path",
    "run_case_round",
    "youngest_resident_age_s",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 一轮案例研判的子进程超时（秒）。实测一轮约 179~247 s；而 `max_steps=14`
#: 意味着主控最多等 10 次下游、每次上限 45 s（`DEFAULT_WAIT_TIMEOUT_SECONDS`
#: 在 SDK 里写死，无旋钮可调）⇒ **结构性上界约 450 s 的等待**，再叠加每步的模型
#: 调用延迟。900 s 覆盖这个上界并留出余量。
#:
#: ⚠️ 超时**不是"失败"**：超时后仍然照常跑四项体检（那一轮可能已经交出了产物，
#:    只是进程没退出）。判据永远是产物，不是进程的退出码。
#:    systemd 单元的 `TimeoutStartSec` 必须**大于**这个值，否则 systemd 先动手。
ROUND_TIMEOUT_S = 900

#: 偏移闸门：常驻服务里**最年轻**的那个进程存活不足这么多秒就跳过本轮。
#: 见模块头 §两个闸门。宁可取大一点（代价只是"今天晚一点跑"）。
OFFSET_MIN_S = 180

#: 专项记录文件名（放在 `.wit/<role>/` 下，与 `runs.jsonl` 同目录）。
ROUNDS_LEDGER_NAME = "case_rounds.jsonl"

#: systemd 单元名。**这三个地方必须一致**：本常量、`deploy/systemd/` 下的实际文件名、
#: 以及 `scripts/systemd-install.sh` 里的那两个字面量（shell 没法 import 本模块）。
#: 一致性由 `check_local` 3d 节当裁判：它按本常量去拼路径找文件，再回过来断言
#: shell 脚本里写的就是这一串 —— 任何一处改名而没同步，都会当场报失败。
#: ⚠️ 刻意**不叫** `acps-<角色>`：`check_local` 的"脚本里的角色清单"扫描用的是
#:    `^[a-z][a-z0-9-]*$` 这个形态，而 `case-round` 首字母之后带连字符仍会被视为
#:    "像角色名"；真正让它**不会被当成角色单元**的是"它不在 `PORTS` 里"——所以
#:    `acps-case-round` 这个名字是安全的，不必为了躲开扫描而改得面目全非。
SERVICE_NAME = "acps-case-round.service"
TIMER_NAME = "acps-case-round.timer"

#: 日志目录名（仓库相对）。**判据来自单元文件里的 `StandardOutput=append:`**：
#: `check_local` 会断言每个已部署单元的落盘路径末尾两段正好是 `logs/<role>.log`，
#: 所以这里的 `logs` 不是"我以为它在那儿"，而是有断言盯着的。
LOGS_REL = "logs"

#: `output/` 里"结构化结论"的文件名前缀。两个都是**产物**（不是日志）：
#:   · `verdict-*` —— 主控交付的裁定书（`legal.issue_verdict`）；
#:   · `ruling-*`  —— 网络裁判交付的裁定（`fact.issue_ruling`）。
#: ⚠️ `verdict-*` 还有 `.meta.json` 孪生文件（sidecar，同样以 `verdict-` 开头），
#:    判据必须把它们排除 —— 否则一份几百字节的 sidecar 就能冒充一份裁定书
#:    （`run/e2e_leader_image.py` 已经踩过这个坑）。
VERDICT_PREFIX = "verdict-"
RULING_PREFIX = "ruling-"
EVIDENCE_PREFIX = "evidence-"
SIDECAR_SUFFIX = ".meta.json"

#: 凭据嫌疑行的判据。**每一条都要能说清它为什么可疑** —— 一条说不出理由的
#: 模式会变成一个永远在响的警报，那比没有警报更糟。
#:
#: ⚠️ 刻意**不用裸的 `401`**：日志里有 `duration_ms: 401` 这类正常数字，
#:    裸匹配会把它当成凭据问题。要求它挨着 `HTTP`/`status`/`code` 才认。
CRED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"Unauthorized", "服务端明确回了未授权"),
    (r"Could not validate", "wit 的凭据校验失败原话"),
    (r"acps_identity_not_active", "平台侧身份不处于 active（审核被撤/过期）"),
    (r"RegistryClientError", "Registry 客户端报错（登录/会话层）"),
    (r"顶掉", "本项目对「会话被后登录者顶掉」的原始措辞"),
    (r"\bblocked\b", "监听器处于 blocked —— 通常是身份状态出了问题"),
    (r"(?:HTTP|status|code)\D{0,8}401", "带上下文的 401（裸 401 会误报耗时数字）"),
)
_CRED_RE = re.compile("|".join(f"(?:{p})" for p, _ in CRED_PATTERNS), re.IGNORECASE)

#: 退出码。见模块头 §退出码与「跳过 ≠ 通过」。
EXIT_OK = 0
EXIT_CHECKS_FAILED = 1
EXIT_SKIPPED = 2
EXIT_ENV = 3


# ---------------------------------------------------------------------------
# 路径与派生
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    """仓库根。与 `agents.OUTPUT_DIR` / `run_ledger._default_wit_dir()` 同根。"""
    return Path(__file__).resolve().parent.parent


def output_dir() -> Path:
    """产物目录。**派生自 `agents.OUTPUT_DIR`**，不另写一份字面量。"""
    return Path(OUTPUT_DIR)


def log_paths(roles: Iterable[str] = DEPLOY_ROLES) -> dict[str, Path]:
    """角色 → 它的落盘日志路径（`<仓库>/logs/<role>.log`）。"""
    root = repo_root() / LOGS_REL
    return {role: root / f"{role}.log" for role in roles}


def rounds_ledger_path(
    wit_dir: str | os.PathLike[str] | None = None, role: str = DEFAULT_ROLE
) -> Path:
    """`.wit/<role>/case_rounds.jsonl`。**只算路径，不创建目录**。"""
    root = Path(wit_dir) if wit_dir is not None else repo_root() / ".wit"
    return root / role / ROUNDS_LEDGER_NAME


def expected_ports(roles: Iterable[str] = DEPLOY_ROLES) -> list[int]:
    """**应当**在监听的端口。派生自 `acps_config.PORTS`（唯一事实源）。

    ⚠️ 只取 `DEPLOY_ROLES`（已排除软废弃的 `incitement` / `harm`）：那两个刻意
       不部署、端口本来就不监听 —— 把它们算进期望值会让这项体检**永远失败**。
    """
    return sorted(PORTS[r] for r in roles if r in PORTS)


# ---------------------------------------------------------------------------
# 闸门
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    """一个前置闸门的结果。`ok=False` ⇒ 本轮**不跑**。"""

    name: str
    ok: bool
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def busy_gate(runs: Sequence[RunRecord]) -> Gate:
    """台账里还有一轮在跑吗。

    ⚠️ 判据是「有开无收」**且** `pid_alive(pid) is not False`（活着**或判不了**）。
       `pid_alive` 返回 `None` 表示"判断不了"，此时按"可能还在跑"处理 ——
       与 `run_ledger` 拒绝抢锁时的取向**逐条一致**（拒绝 > 猜）。
    """
    live = [r for r in runs if r.unfinished and pid_alive(r.pid) is not False]
    if not live:
        return Gate("busy", True, f"台账里没有在跑的轮次（共 {len(runs)} 轮）")
    shown = "、".join(
        f"{r.run_id}（pid={r.pid}，label={r.label or '?'}，起于 {r.started_at}）" for r in live[:3]
    )
    return Gate(
        "busy",
        False,
        f"还有 {len(live)} 轮没收敛，不能与它抢同一个收件箱与产物目录：{shown}",
    )


def _proc_age_s(pid: int) -> int | None:
    """进程已存活秒数。**量不出来就返回 `None`**（不是 0）。"""
    exe = shutil.which("ps")
    if exe is None or pid <= 0:
        return None
    try:
        proc = subprocess.run(
            [exe, "-o", "etimes=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    if not out.isdigit():
        return None
    return int(out)


def _unit_main_pid(role: str) -> tuple[int | None, str]:
    """常驻单元的 MainPID。返回 `(pid, 原因)`；`pid is None` ⇒ **判不出来**。

    ⚠️ 返回的**原因串刻意不含角色名**：调用方要按原因**归并**（八个单元往往因为
       同一个原因一起判不出来），串里带上角色名就永远归并不了 —— 于是那份说明会
       把同一句话重复八遍，长到没人看。角色名由调用方自己拼。
    """
    exe = shutil.which("systemctl")
    if exe is None:
        return None, "本机没有 systemctl（常驻服务不是 systemd 管的）"
    unit = f"acps-{role}.service"
    try:
        proc = subprocess.run(
            [exe, "show", "-p", "MainPID", "--value", unit],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"systemctl 调用失败（{type(exc).__name__}）"
    out = (proc.stdout or "").strip()
    if not out.isdigit():
        return None, "单元的 MainPID 读不出（单元不存在、没在跑，或 systemctl 报错）"
    pid = int(out)
    return (pid if pid > 0 else None), "MainPID=0（单元没在跑）"


def youngest_resident_age_s(
    roles: Iterable[str] = DEPLOY_ROLES,
) -> tuple[int | None, str | None, dict[str, int | None], str]:
    """常驻服务里**最年轻**那个进程的存活秒数。

    返回 `(最年轻秒数, 最年轻的角色, 每个角色的秒数, 说明)`。
    `最年轻秒数 is None` ⇒ **量不出来**（取值 0 会被误当成"刚刚才起来"，方向刚好相反）。

    ⚠️ "最年轻的是谁"由**本函数**一同给出，调用方不许自己再 `min()` 一遍：
       第一版就是这么写的，结果"量到了秒数但没有对应角色"这种**不一致的输入**
       会让 `offset_gate` 里的 `min()` 对一个空序列抛出 `ValueError`
       —— 一个体检用的闸门，在输入不完整时崩掉，比它给出一个笨答案糟得多。
       （离线用例当场抓到：`check_local` 3d 用**打桩**的返回值驱动闸门，
        打的桩恰好就是这种不一致输入；生产路径上它确实不会出现，但
        "生产上不会出现"不是理由 —— 判据本来就该在别人喂它怪东西时也不崩。）
    """
    ages: dict[str, int | None] = {}
    #: 原因 → 哪些角色是这个原因（**归并**后再拼说明，否则同一句话重复八遍）。
    by_reason: dict[str, list[str]] = {}
    measurable = 0
    for role in roles:
        pid, why = _unit_main_pid(role)
        if pid is None:
            ages[role] = None
            by_reason.setdefault(why, []).append(role)
            continue
        age = _proc_age_s(pid)
        ages[role] = age
        if age is None:
            by_reason.setdefault("进程存活秒数读不出", []).append(role)
        else:
            measurable += 1
    known = [(r, a) for r, a in ages.items() if a is not None]
    if not known:
        return None, None, ages, _join_reasons(by_reason) or "一个常驻单元的进程年龄都量不出来"
    who, age = min(known, key=lambda kv: kv[1])
    note = f"{measurable}/{len(ages)} 个常驻单元量到了进程年龄"
    if by_reason:
        note += "；" + _join_reasons(by_reason)
    return age, who, ages, note


def _join_reasons(by_reason: Mapping[str, list[str]]) -> str:
    """把「原因 → 角色」归并成一句人话。"""
    return "；".join(
        f"{'、'.join(rs)}：{why}" for why, rs in sorted(by_reason.items())
    )


def offset_gate(*, roles: Iterable[str] = DEPLOY_ROLES, min_s: int = OFFSET_MIN_S) -> Gate:
    """常驻服务是不是刚起来（是 ⇒ 别在这时候登录 Registry）。

    统计口径取**最年轻**那个进程，不取平均也不取最老：错开启动的意义就是
    "最后起来的那个也要 settle"，取平均会把一个刚重启的服务藏在一堆老进程中间。

    ⚠️ **量不出来时不拦**（`ok=True`）—— 但要**原样报出为什么量不出来**。
       方向是这样选的：量不出来的常见原因是"这台机器根本没有 systemd 在管那些
       常驻服务"（开发机上就是这样），此时**没有可被顶掉的会话**，拦下只会让功能
       静默地永远不跑。真正需要拦的场景（服务器上刚重启完）恰好是**能量出来**的。
       ⇒ 这是"如实报告 + 不假装有保证"，不是静默降级。
    """
    age, who, _ages, note = youngest_resident_age_s(roles)
    if age is None or who is None:
        return Gate("offset", True, f"无法判定常驻服务年龄 ⇒ 不拦（{note}）")
    if age < min_s:
        return Gate(
            "offset",
            False,
            f"最年轻的常驻服务 acps-{who} 只跑了 {age}s（< {min_s}s）"
            f"⇒ 现在登录 Registry 会撞上启动窗口，把别人的会话顶掉。{note}",
        )
    return Gate("offset", True, f"最年轻的常驻服务 acps-{who} 已跑 {age}s（≥ {min_s}s）。{note}")


# ---------------------------------------------------------------------------
# 四项体检
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """一项体检的结论。`detail` 必须能**指名道姓**（谁、多少、在哪）。"""

    name: str
    ok: bool
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def check_ledger(
    before: Sequence[RunRecord], after: Sequence[RunRecord], case: CaseDef
) -> Check:
    """体检 1：本轮**真的被记了账**，而且**已收尾、不是异常退出**。

    判据三件事，缺一不可：
      · 新增**恰好一条** `run_id`（多于一条说明有别人也在跑 —— 那也是本轮的问题）；
      · 它的 `label` 就是 `case:<id>`（写错标签会让轮换永远认不出这一轮）；
      · 它有收尾记录，且 `status` 不是 `raised:*`。
    """
    seen = {r.run_id for r in before}
    fresh = [r for r in after if r.run_id not in seen]
    want = case_label(case.id)
    if not fresh:
        return Check(
            "ledger",
            False,
            "台账里**没有**新增轮次：这一轮连「开」记录都没落盘"
            "（`run_agent` 没起来、或台账根与 CWD 不是同一个地方）",
        )
    if len(fresh) > 1:
        ids = "、".join(r.run_id for r in fresh)
        return Check("ledger", False, f"台账里新增了 {len(fresh)} 轮（不是 1 轮）：{ids}")
    run = fresh[0]
    if run.label != want:
        return Check("ledger", False, f"新增轮次的 label 是 {run.label!r}，期望 {want!r}")
    if run.unfinished:
        return Check(
            "ledger",
            False,
            f"本轮 run_id={run.run_id} **没有收尾记录**（有开无收）"
            " —— 子进程可能被 kill 或被超时打断",
        )
    if (run.status or "").startswith("raised:"):
        return Check("ledger", False, f"本轮 run_id={run.run_id} 以异常收尾：status={run.status}")
    return Check(
        "ledger",
        True,
        f"run_id={run.run_id}，label={run.label}，status={run.status or '?'}，"
        f"迭代 {run.iterations if run.iterations is not None else '?'} 轮",
    )


def _drop_sidecars(names: Iterable[str]) -> frozenset[str]:
    """剔掉 sidecar 文件名。**判据「sidecar 不算产物」只有这一个定义。**

    ⚠️ 它在**两层**上都被调用（`_artifacts` 造快照时、`check_artifacts` 比对时），
       这不是"同一判定写两处"，而是**同一个函数**被两处引用：定义只有一份。
       为什么比对时还要再过滤一次 —— 因为 `ArtifactSnapshot` 是个普通 dataclass，
       谁都可以手工造一个出来。手造的快照里塞进一个 `verdict-x.meta.json`，
       滤网只在造快照那一步的话就**漏过去了**，而漏过去的方向是**假通过**
       （一份几百字节的 sidecar 顶替一份裁定书）。探针第一次跑就抓到了这条。
    """
    return frozenset(n for n in names if not n.endswith(SIDECAR_SUFFIX))


def _artifacts(directory: Path, prefix: str) -> frozenset[str]:
    """目录下以 `prefix` 开头的产物**名**（已剔 sidecar）。"""
    if not directory.is_dir():
        return frozenset()
    return _drop_sidecars(
        p.name for p in directory.glob(f"{prefix}*") if p.is_file()
    )


@dataclass(frozen=True)
class ArtifactSnapshot:
    """`output/` 三个前缀的**同时**快照。三类一起取、一起比。

    ⚠️ 为什么三类必须**同一个快照对象**而不是三个各自 before/after 的集合：
       第一版只给 `verdict-*` 取了 before，另外两类**只取了 after 就当成"新增"**，
       于是打印出来的是"另新增 ruling N 份"而 N 其实是**总量** —— 一条错的显示。
       （判据本身没坏，坏的是给人看的那句话；而"错的显示"比 `?` 更糟。）
    """

    verdict: frozenset[str]
    ruling: frozenset[str]
    evidence: frozenset[str]


def snapshot_artifacts(directory: Path | None = None) -> ArtifactSnapshot:
    """取一次现场快照（**只读**）。"""
    target = directory if directory is not None else output_dir()
    return ArtifactSnapshot(
        verdict=_artifacts(target, VERDICT_PREFIX),
        ruling=_artifacts(target, RULING_PREFIX),
        evidence=_artifacts(target, EVIDENCE_PREFIX),
    )


def check_artifacts(before: ArtifactSnapshot, after: ArtifactSnapshot) -> Check:
    """体检 2：`output/` 里**新增了主控的结构化结论**。

    ⚠️ 只有 `verdict-*` 是硬判据（主控的交付物）。`ruling-*`（网络裁判）与
       `evidence-*`（证据鉴证）**如实附带报告数量**：一轮走哪个分支由主控自己决定，
       把 `ruling` 写成硬判据会在合法的证据分支上误报失败。见模块头。
    """
    fresh_verdict = sorted(_drop_sidecars(after.verdict) - _drop_sidecars(before.verdict))
    fresh_ruling = sorted(_drop_sidecars(after.ruling) - _drop_sidecars(before.ruling))
    fresh_evidence = sorted(_drop_sidecars(after.evidence) - _drop_sidecars(before.evidence))
    extra = (
        f"；另新增 ruling {len(fresh_ruling)} 份、evidence {len(fresh_evidence)} 份"
        "（这两个只是**如实报告**，不是判据）"
    )
    if not fresh_verdict:
        return Check(
            "artifacts",
            False,
            "`output/` 下**没有**新增 `verdict-*.json` ⇒ 这一轮没有交出结构化结论"
            "（模型只说了段自然语言就收尾，是实测踩过的一种收尾方式）" + extra,
        )
    return Check("artifacts", True, f"新增 verdict：{'、'.join(fresh_verdict)}" + extra)


def _listen_ports(entries: Sequence[str]) -> set[int]:
    """从 `ss -ltn` 的输出里取监听端口（纯函数，便于离线用例）。"""
    found: set[int] = set()
    for line in entries:
        if not line.lstrip().upper().startswith("LISTEN"):
            continue
        found.update(int(m) for m in re.findall(r":(\d+)\s", line))
    return found


def check_ports(
    *, roles: Iterable[str] = DEPLOY_ROLES, ss_exe: str | None = None
) -> Check:
    """体检 3：常驻端口**仍在监听**。

    ⚠️ 找不到 `ss` ⇒ **判失败**（不是"跳过"）：量不出来就该如实说量不出来，
       把它算成通过等于把体检关掉。服务器上的 `iproute2` 是基础包，实际不会缺。
    """
    want = expected_ports(roles)
    exe = ss_exe or shutil.which("ss")
    if exe is None:
        return Check("ports", False, f"找不到 `ss`，无法判定端口状态（期望 {want}）")
    try:
        proc = subprocess.run(
            [exe, "-ltn"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("ports", False, f"跑 `ss -ltn` 失败：{type(exc).__name__}: {exc}")
    have = _listen_ports((proc.stdout or "").splitlines())
    missing = [p for p in want if p not in have]
    if missing:
        return Check(
            "ports",
            False,
            f"这些常驻端口**没有**在监听：{missing}（期望 {want}，实际 {sorted(have)}）"
            " —— 本轮可能把常驻服务弄掉了线（会话被顶 / 端口被抢）",
        )
    return Check("ports", True, f"{len(want)} 个常驻端口都在监听：{want}")


def snapshot_logs(paths: dict[str, Path]) -> dict[str, int]:
    """记录每个日志的当前字节数。取不到就记 `-1`（**不是 0**）。

    ⚠️ `0` 是一个合法的基线（空文件），而"文件不存在"与"文件是空的"后面要走
       完全不同的两条路。用同一个 0 表示两者，会让"日志被删了"看起来像
       "日志一直空着" —— 而那正是"没得比"与"不一样"长得一模一样的那个坑。
    """
    sizes: dict[str, int] = {}
    for role, path in paths.items():
        try:
            sizes[role] = path.stat().st_size
        except OSError:
            sizes[role] = -1
    return sizes


def check_credentials(
    before: Mapping[str, int], *, paths: Mapping[str, Path] | None = None
) -> Check:
    """体检 4：日志里**新增的字节**中，有没有凭据嫌疑行。

    ⚠️ 只读**增量**（从基线偏移往后读），不是整篇重扫：整篇重扫会把"很久以前那次
       启动时报过的 `blocked`"当成"这一轮搞坏的"，于是体检**永远红**。
    ⚠️ 增量比对**必须先确认基线还在**：
         · 文件变小 = 被 logrotate 轮转（配置是 `daily` + `copytruncate`，所以是
           **原地截断**，表现正是"变小"）；
         · 记录为 `-1` = 取基线时它还不存在。
       这两种情况下"读不到新内容"与"没有新内容"长得**一模一样** ⇒ 如实报
       「无法判定（不是「干净」）」。本项目纪律：比对类判据必须先单独断言基线存在。
    ⚠️ 反过来也要防：如果一个角色的日志**从来就不存在**（路径写错了），
       那这份体检其实**什么都没查**。所以"一条可比基线都没有"是**判失败**，
       不能因为"没查出问题"就说通过 —— 那正是「把判据的输入裁剪到只剩自己」。

    📌 轮转这条只要定时器的时刻**远离** logrotate 的时刻就不会常红（见单元文件里
       选 09:1x 的理由）；常红的话那不是判据坏了，是排班撞上了。
    """
    targets = paths if paths is not None else log_paths()
    hits: list[str] = []
    rotated: list[str] = []
    uncomparable: list[str] = []
    absent: list[str] = []
    compared = 0
    scanned_bytes = 0

    for role, path in targets.items():
        base = before.get(role)
        if base is None:
            # 这份体检根本没被问到它（快照里没这一项）—— 那是调用方的问题，如实报。
            uncomparable.append(f"{role}（不在基线快照里）")
            continue
        exists = path.exists()
        if base < 0:
            if not exists:
                absent.append(role)
            else:
                uncomparable.append(f"{role}（基线时不存在，现在有了）")
            continue
        if not exists:
            uncomparable.append(f"{role}（基线 {base}B，现在文件没了）")
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            uncomparable.append(f"{role}（现在读不到：{exc}）")
            continue
        if size < base:
            # 把**最可能的成因**直接写进原因串：只说「变小了」的话，读的人还得自己
            # 知道"本项目的 logrotate 是 copytruncate"才能推出来 —— 那是把排查成本
            # 转嫁给下一个人。
            rotated.append(f"{role}（{base}B → {size}B，变小 ⇒ 多半是 logrotate 原地截断）")
            continue
        compared += 1
        if size == base:
            continue
        try:
            with open(path, "rb") as fh:
                fh.seek(base)
                chunk = fh.read(size - base)
        except OSError as exc:
            uncomparable.append(f"{role}（读取失败：{exc}）")
            continue
        scanned_bytes += len(chunk)
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            m = _CRED_RE.search(line)
            if m:
                hits.append(f"{role}: 「{m.group(0)}」 ← {line.strip()[:160]}")

    if hits:
        return Check(
            "credentials",
            False,
            f"新增日志里有 {len(hits)} 行凭据嫌疑内容：\n       " + "\n       ".join(hits[:8]),
        )
    # ⚠️ 顺序：**先报"判不了"，再报"没基线"**。反过来的话，一个"唯一那条日志恰好
    #    被轮转"的场景会走成"一条可比基线都没有"，把真正的起因（轮转）从原因串里
    #    丢掉 —— 那正是「一条错的显示比 `?` 更糟」。（探针第一次跑就抓到了。）
    if rotated or uncomparable:
        return Check(
            "credentials",
            False,
            f"无法判定（**不是**「干净」）：{'、'.join(rotated + uncomparable)}"
            " —— 基线没了就没得比，此时说什么都不算数",
        )
    if compared == 0:
        return Check(
            "credentials",
            False,
            "**一条可比基线都没有** ⇒ 这份体检什么都没查"
            + (f"（{len(absent)} 个日志文件不存在：{'、'.join(absent[:6])}）" if absent else "")
            + " —— 先确认日志路径与 `logs/` 目录是否对得上",
        )
    note = f"；另有 {len(absent)} 个日志文件不存在（{'、'.join(absent[:4])}）" if absent else ""
    return Check(
        "credentials",
        True,
        f"{compared} 份日志可比，本轮新增 {scanned_bytes} 字节，里面没有凭据嫌疑行{note}",
    )


# ---------------------------------------------------------------------------
# 一轮
# ---------------------------------------------------------------------------


@dataclass
class CaseRoundResult:
    """一轮的完整记录。**它就是要落进 `case_rounds.jsonl` 的东西**。"""

    at: str
    outcome: str
    case: CaseDef | None
    label: str
    gates: tuple[Gate, ...]
    checks: tuple[Check, ...]
    exit_code: int
    run_id: str | None = None
    status: str | None = None
    iterations: int | None = None
    duration_s: float | None = None
    timed_out: bool = False
    skip_reason: str | None = None
    lap_before: int = 0
    rounds_before: int = 0
    #: 子进程的输出尾部（人被叫去排查时最想先看的那一段）。
    tail: str = ""
    #: 专项记录**自己**写失败的原因（不是这一轮失败的原因）。空 = 记进去了。
    ledger_error: str | None = None

    @property
    def ok(self) -> bool:
        # ⚠️ 与「事后读台账渲染答辩材料」用的是**同一个**判定（`round_passed`）：
        #    一个在跑完那一刻、一个在读 `case_rounds.jsonl` 时。两处各写一遍就是
        #    「同一判定写两处」，而这条判定**直接决定材料上写"跑通了几轮"** ——
        #    漂移的后果是材料与当场的结论对不上，且没有任何东西会报错。
        return round_passed(self.to_json())

    def to_json(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "outcome": self.outcome,
            "case_id": self.case.id if self.case else None,
            "category": self.case.category if self.case else None,
            "title": self.case.title if self.case else None,
            "label": self.label,
            "lap_before": self.lap_before,
            "rounds_before": self.rounds_before,
            "run_id": self.run_id,
            "status": self.status,
            "iterations": self.iterations,
            "duration_s": self.duration_s,
            "timed_out": self.timed_out,
            "skip_reason": self.skip_reason,
            "gates": [g.to_json() for g in self.gates],
            "checks": [c.to_json() for c in self.checks],
            "exit_code": self.exit_code,
            "ledger_error": self.ledger_error,
        }


def _iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """追加一行 JSONL。**一次 write 写完一整行**（并发下不互相拼行）。

    ⚠️ 这是 `run_ledger._append` 的**同纪律副本**，取舍见模块头最后一段。
       要动这里的写法，请同时看一眼 `run_ledger._append` —— 两边守的是同一条纪律
       （`O_APPEND` + 单次 `write`），而且那条纪律的注释里记着一个**引错常量的教训**
       （有人曾拿 `PIPE_BUF` 当普通文件原子性的依据，那是错的）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)


def _persist(result: CaseRoundResult, path: Path) -> CaseRoundResult:
    """落专项记录。**写不下去也不抛**（旁路记录不许改主流程的结论）。"""
    try:
        _append_jsonl(path, result.to_json())
    except Exception as exc:  # noqa: BLE001 - 宽是有意的，见模块头
        print(
            f"[case-round] ⚠️ 专项记录写失败：{path}\n"
            f"             {type(exc).__name__}: {exc}\n"
            "             它是**旁路记录** ⇒ 本轮结论照常；但这次体检在溯源上会缺一份凭证。",
            file=sys.stderr,
        )
        result.ledger_error = f"{type(exc).__name__}: {exc}"
    return result


def _spawn_round(
    case: CaseDef, *, label: str, timeout_s: int, cwd: Path
) -> tuple[int | None, str, bool, float]:
    """起一个子进程跑一轮。返回 `(returncode, 输出尾部, 是否超时, 耗时秒)`。

    ⚠️ 用 `sys.executable` 而不是裸的 `python`：本模块本身是被
       `./.venv/bin/python -m legal_acps.case_round` 起来的，`sys.executable`
       就是那个 venv 解释器 —— 而**托管 python 没装 wit**，用错解释器会得到
       一堆 ImportError（本项目记过一条：「托管 python 没 pydantic ⇒ 造出假失败」）。
    ⚠️ `cwd` 必须是仓库根（见模块头）：否则会写出两份互不相干的 `.wit`。
    """
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "legal_acps.run_agent",
        "leader",
        "-i",
        case.text,
        "--label",
        label,
    ]
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        code: int | None = proc.returncode
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        code = None
        out = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + (
            (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        )
    except OSError as exc:
        code = None
        out = f"[case-round] 子进程起不来：{type(exc).__name__}: {exc}"
    return code, out, timed_out, time.monotonic() - started


TAIL_LINES = 60


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    kept = text.splitlines()[-lines:]
    return "\n".join(kept)


def run_case_round(
    *,
    wit_dir: str | os.PathLike[str] | None = None,
    role: str = DEFAULT_ROLE,
    case_id: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    timeout_s: int = ROUND_TIMEOUT_S,
    cases: Sequence[CaseDef] = case_library.CASES,
) -> CaseRoundResult:
    """跑一轮（或如实报告"没跑"）。**本函数不改任何全局状态**。

    步骤：案例库自检 → 选案 → 两个闸门 → 起子进程 → 四项体检 → 落专项记录。
    """
    at = _iso()
    problems = case_library.self_check(cases)
    if problems:
        return CaseRoundResult(
            at=at,
            outcome="environment",
            case=None,
            label="",
            gates=(),
            checks=(),
            exit_code=EXIT_ENV,
            skip_reason="案例库自检未通过：" + "；".join(problems),
        )

    before_runs = read_runs(wit_dir=wit_dir, role=role)
    plan: RotationPlan = plan_next(before_runs, cases=cases)
    case = case_library.get_case(case_id, cases=cases) if case_id else plan.case
    if case is None and case_id:
        return CaseRoundResult(
            at=at,
            outcome="environment",
            case=None,
            label="",
            gates=(),
            checks=(),
            exit_code=EXIT_ENV,
            skip_reason=(
                f"指定了 `--case {case_id}`，但案例库里没有这一条"
                f"（现有 {len(cases)} 条；见 `python -m legal_acps.case_library --library`）"
            ),
        )
    if case is None:
        # 库为空 —— `plan_next` 刻意不兜一个默认案子出来。
        return CaseRoundResult(
            at=at,
            outcome="environment",
            case=None,
            label="",
            gates=(),
            checks=(),
            exit_code=EXIT_ENV,
            skip_reason="案例库为空 ⇒ 没有可跑的案子（**不猜一个出来**）",
        )
    label = case_label(case.id)

    # ⚠️ 两个闸门**总是都测**，哪怕 `--force` 也不跳过测量：被绕过的闸门必须留下
    #    「它本来会拦下」这个读数 —— 否则事后只看到"跑了"，看不出当初为什么该拦。
    gates = [busy_gate(before_runs), offset_gate()]
    if force:
        gates = [
            Gate(g.name, True, g.detail if g.ok else f"[--force 已绕过] {g.detail}")
            for g in gates
        ]
    blocked = [g for g in gates if not g.ok]

    base = dict(
        at=at,
        case=case,
        label=label,
        gates=tuple(gates),
        lap_before=plan.lap,
        rounds_before=plan.total_rounds,
    )

    if dry_run:
        return CaseRoundResult(
            outcome="dry-run", checks=(), exit_code=EXIT_OK, skip_reason="--dry-run：没有真跑", **base
        )
    if blocked:
        reason = "；".join(f"[{g.name}] {g.detail}" for g in blocked)
        return CaseRoundResult(
            outcome="skipped", checks=(), exit_code=EXIT_SKIPPED, skip_reason=reason, **base
        )

    # 基线：产物快照 + 日志字节数。**在起子进程之前取**。
    art_before = snapshot_artifacts()
    logs = log_paths()
    logs_before = snapshot_logs(logs)

    code, output, timed_out, duration = _spawn_round(
        case, label=label, timeout_s=timeout_s, cwd=repo_root()
    )

    after_runs = read_runs(wit_dir=wit_dir, role=role)
    checks = (
        check_ledger(before_runs, after_runs, case),
        check_artifacts(art_before, snapshot_artifacts()),
        check_ports(),
        check_credentials(logs_before, paths=logs),
    )

    fresh = [r for r in after_runs if r.run_id not in {r.run_id for r in before_runs}]
    run = fresh[0] if len(fresh) == 1 else None
    result = CaseRoundResult(
        outcome="ran",
        checks=checks,
        exit_code=EXIT_OK if all(c.ok for c in checks) else EXIT_CHECKS_FAILED,
        run_id=run.run_id if run else None,
        status=run.status if run else None,
        iterations=run.iterations if run else None,
        duration_s=round(duration, 1),
        timed_out=timed_out,
        tail=_tail(output),
        **base,
    )
    if timed_out:
        result.skip_reason = None
        print(
            f"[case-round] ⚠️ 子进程 {timeout_s}s 超时被终止（退出码 {code}）——"
            "但体检照跑：判据是**产物**，不是进程的退出码。",
            file=sys.stderr,
        )
    return result


# ---------------------------------------------------------------------------
# 渲染与入口
# ---------------------------------------------------------------------------


def render_result(result: CaseRoundResult) -> str:
    """把一轮的结论摊开。**所有事实都来自 `result` 本身**，不重新推导。"""
    lines: list[str] = []
    head = {
        "ran": "已跑",
        "skipped": "**跳过**（没跑）",
        "dry-run": "预演（没有真跑）",
        "environment": "**环境性问题**（没跑）",
    }.get(result.outcome, result.outcome)
    lines.append(f"[case-round] {result.at}  {head}")
    if result.case is not None:
        lines.append(f"  案例：{result.case.id}（{result.case.category}）{result.case.title}")
        lines.append(f"  标签：{result.label}   本轮之前：{result.rounds_before} 轮 / 跑满 {result.lap_before} 圈")
    for gate in result.gates:
        lines.append(f"  闸门 {gate.name:<8}{'通过' if gate.ok else '拦下'}  {gate.detail}")
    if result.skip_reason:
        lines.append(f"  原因：{result.skip_reason}")
    if result.outcome == "ran":
        lines.append(
            f"  子进程：耗时 {result.duration_s}s"
            + ("（**超时被终止**）" if result.timed_out else "")
            + f"；台账 run_id={result.run_id or '（没有）'}，status={result.status or '?'}"
            + f"，迭代 {result.iterations if result.iterations is not None else '?'} 轮"
        )
    for check in result.checks:
        lines.append(f"  [{'通过' if check.ok else '失败'}] {check.name}：{check.detail}")
    if result.outcome == "ran":
        lines.append(f"  总判定：{'通过' if result.ok else '**未通过**'}（退出码 {result.exit_code}）")
    if result.ledger_error:
        lines.append(f"  ⚠️ 专项记录写失败：{result.ledger_error}")
    return "\n".join(lines)


def read_rounds(
    wit_dir: str | os.PathLike[str] | None = None, role: str = DEFAULT_ROLE
) -> tuple[list[dict[str, Any]], int]:
    """读专项记录 `case_rounds.jsonl`。返回 `(记录列表, 坏行数)`。

    ⚠️ 坏行**单独计数、不静默丢**：把读不出来的行当成"没有这一轮"，与"这一轮确实
       没发生"在汇总里**长得一模一样** —— 那是本项目反复踩的那种坑。
    """
    path = rounds_ledger_path(wit_dir, role)
    if not path.is_file():
        return [], 0
    rows: list[dict[str, Any]] = []
    bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            bad += 1
    return rows, bad


def round_passed(row: Mapping[str, Any]) -> bool:
    """台账里的一行算不算「**这一轮真的跑通了**」（跑过 + 四项体检全过）。

    ⚠️ **`checks` 为空 ⇒ 不算通过。** 顺手的写法是
       `all(c.get("ok") for c in checks)`，而 **`all([])` 是 `True`** —— 于是一条
       `outcome=ran` 但**没有任何体检结果**的记录会被报成"四项体检全过"，而
       **同一份材料**的逐轮明细里那一格写的却是 `—`（那里注明 `—` = 没跑）。
       两处自相矛盾，且矛盾的方向是**假通过**（唯一硬判据被一句空断言顶替掉了）。
       实测：`case/{ran, checks: []}` 曾得到「四项体检全过 **1**」。
    ⚠️ 这条判定**只有这一处实现**：`CaseRoundResult.ok` 也走它（见那里的注释）。
       要改这里的口径，先想清楚"当场结论"与"事后材料"会不会分家。
    """
    if row.get("outcome") != "ran":
        return False
    checks = row.get("checks") or []
    return bool(checks) and all(bool(c.get("ok")) for c in checks)


def render_ledger_md(
    wit_dir: str | os.PathLike[str] | None = None, role: str = DEFAULT_ROLE
) -> str:
    """把台账渲染成一份可交的 Markdown（答辩材料「连续 N 天跑通」的凭据）。

    ⚠️ **从台账生成，不是手工维护的副本**。手工维护一份"台账文档"就是**第二份事实源**
       （`case_rounds.jsonl` 是一份、文档是另一份），**必然漂移** —— 而材料一旦漂移，
       评审照着文档核对就会对不上。用法：

           ./.venv/bin/python -m legal_acps.case_round --ledger-md > docs/aicomp/案例研判台账.md

    ⚠️ 汇总里的每个数字都是**现场算**的（`len(rows)` / 逐个计数），不写死。
       **"不写死"是有判据的**，别只当口号：`check_local` §3d 会临时把案例库改长、
       再看分母跟不跟着变 —— 因为今天的 `len(CASES)` 恰好就是那个曾被写死的数，
       不换长度的话，"写死的"与"派生的"印出来一模一样（判据是空转的）。
    """
    rows, bad = read_rounds(wit_dir, role)
    out: list[str] = []
    out.append("# 真实案例研判台账（自动生成）")
    out.append("")
    out.append("> **本文件由台账自动生成，不要手工编辑**（手工维护的副本必然与台账漂移）。")
    out.append(">")
    out.append("> 生成命令：`./.venv/bin/python -m legal_acps.case_round --ledger-md`")
    out.append(f"> 数据源：`{rounds_ledger_path(wit_dir, role)}`（角色 `{role}`）")
    out.append(f"> 生成时间：{_iso()}")
    out.append("")
    out.append("## 口径（先看这一节，再看数字）")
    out.append("")
    out.append("- **一轮的通过判据只有一个：`output/` 里新增了 `verdict-*.json`。**")
    out.append("  `status=completed` **不算**；`ruling-*` / `evidence-*` 会被报出来但不当判据")
    out.append("  （法律证据链那一支产出的就是它们）。")
    out.append("- **`outcome=skipped` = 今天没跑**（被闸门拦下，不是失败，但**也不等于通过**）。")
    out.append("- 每轮四个体检项：台账 / 产物 / 端口 / 凭据。")
    out.append("- 「**四项体检全过**」要求体检结果**确实存在**（`checks` 为空的行不算通过）；")
    out.append("  「**连续跑通 N 轮**」只数**末尾**连续通过的那一段，与**累计**通过数是两个数。")
    out.append("")

    if bad:
        out.append(f"> ⚠️ **有 {bad} 行读不出来**（坏行已单独计数，没有静默丢掉）。")
        out.append("")

    if not rows:
        out.append("## 台账为空")
        out.append("")
        out.append("还没有任何一轮记录。装定时器后等第一次触发，或先手工补跑一轮：")
        out.append("")
        out.append("```bash")
        out.append("bash scripts/case_round.sh")
        out.append("```")
        return "\n".join(out) + "\n"

    ran = [r for r in rows if r.get("outcome") == "ran"]
    # ⚠️ 通过判定收在 `round_passed` 一处：`all([])` 是 `True`，顺手写会让
    #    「ran 但没有 checks」的一行变成"四项体检全过"（见该函数的说明）。
    passed = [r for r in rows if round_passed(r)]
    # ⚠️ **「连续」必须真的是连续的**：取从**最后一行往前**连续通过的那一段。
    #    原先写的是 `len(passed)`（= **累计**通过数）却叫"连续跑通" —— 输入
    #    「过 / 失败 / 过」会印出"连续跑通 2 轮"，那两个通过的轮次中间隔着一个失败的。
    #    材料是给评审看的，"连续 N 天跑通"这句话必须经得起逐行核对。
    streak = 0
    for _r in reversed(rows):
        if round_passed(_r):
            streak += 1
        else:
            break
    skipped = [r for r in rows if r.get("outcome") == "skipped"]
    env = [r for r in rows if r.get("outcome") == "environment"]
    # ⚠️ 「覆盖到」= **真的跑过**。被闸门拦下（`skipped`）的那些轮次虽然也记着
    #    `case_id`，但它们**没往发起轮台账写任何东西** ⇒ 轮换算这个案子 0 轮
    #    ⇒ 下次还会再挑它一次。把"没跑"算进"覆盖到"，是在材料里虚报覆盖面，
    #    而且与紧邻的那两行（「真跑了」「被闸门拦下（没跑，≠ 通过）」）**自相矛盾**。
    #    实测：一条 `outcome=skipped` 的记录曾让「真跑了 0」与「覆盖到的案例 1 / 13」并存。
    cases = sorted(
        {str(r.get("case_id")) for r in rows
         if r.get("outcome") == "ran" and r.get("case_id")}
    )
    stale = [r for r in rows if r.get("ledger_error")]

    out.append("## 汇总")
    out.append("")
    out.append("| 项 | 值 |")
    out.append("|---|---|")
    out.append(f"| 记录轮次 | **{len(rows)}** |")
    out.append(f"| 真跑了 | {len(ran)} |")
    out.append(f"| 其中**四项体检全过** | **{len(passed)}** |")
    out.append(f"| 被闸门拦下（没跑，≠ 通过） | {len(skipped)} |")
    out.append(f"| 环境性问题（没跑） | {len(env)} |")
    # ⚠️ 分母**现场派生**（`len(case_library.CASES)`），不许写死。
    #    原先写的是字面量 `13` —— 今天恰好等于 `len(CASES)`，所以两边看起来一样；
    #    加一条案例（或删一条）之后，材料会印出 `14 / 13` 这种一眼错的东西，
    #    而**没有任何东西会报错**（本项目的「处数不许写死」就是这条）。
    out.append(f"| 覆盖到的案例 | {len(cases)} / {len(case_library.CASES)} |")
    out.append(f"| 最近一轮 | {rows[-1].get('at', '?')} |")
    if stale:
        out.append(
            f"| ⚠️ 专项记录自身写失败 | {len(stale)} 轮（结论仍以体检为准，但溯源缺一份凭证） |"
        )
    out.append("")
    if streak:
        out.append(
            f"> ✅ **最近连续跑通 {streak} 轮**"
            f"（末尾连续 {streak} 轮都在 `output/` 产出了新的 `verdict-*`）。"
        )
        out.append("")
    elif passed:
        # ⚠️ 有通过、但**末尾不连续** ⇒ 不许说"连续跑通 N 轮"。
        #    累计数（`len(passed)`）与连续数是两个数，把它们混起来就是在材料上写假话 ——
        #    而评审正是照着这句话去核的。
        out.append(
            f"> ⚠️ 累计有 **{len(passed)}** 轮四项体检全过，但**末尾不连续**"
            f"（末尾连续通过只有 {streak} 轮；更早的 {len(rows) - streak} 轮里"
            "有没通过的或没跑的）⇒ 只报累计，**不称「连续跑通」**。"
        )
        out.append("")

    out.append("## 逐轮明细")
    out.append("")
    out.append("| # | 时间(UTC) | 案例 | 类别 | 结论 | 出口码 | 耗时(s) | 台账 run_id | 体检 | 备注 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        checks = r.get("checks") or []
        marks = "".join("✅" if c.get("ok") else "❌" for c in checks) or "—"
        note = ""
        if r.get("outcome") == "skipped":
            note = (r.get("skip_reason") or "").replace("|", "\\|")[:70]
        elif r.get("timed_out"):
            note = "**超时被终止**（判据仍是产物）"
        if r.get("ledger_error"):
            note = (note + " " if note else "") + "专项记录写失败"
        out.append(
            "| {i} | {at} | `{cid}` | {cat} | {oc} | {ec} | {dur} | `{rid}` | {marks} | {note} |".format(
                i=i,
                at=str(r.get("at", "?")).replace("T", " ")[:19],
                cid=r.get("case_id") or "—",
                cat=r.get("category") or "—",
                oc=r.get("outcome") or "?",
                ec=r.get("exit_code", "?"),
                dur=r.get("duration_s", "—"),
                rid=(r.get("run_id") or "—"),
                marks=marks,
                note=note,
            )
        )
    out.append("")
    out.append("> 体检四列的顺序固定为 **台账 / 产物 / 端口 / 凭据**。")
    out.append("> `—` 表示这一轮**没跑**（没有体检结果），不要读成「全部通过」。")
    out.append("")
    out.append("## 各案例轮次分布")
    out.append("")
    counts: dict[str, int] = {}
    for r in rows:
        cid = r.get("case_id")
        if cid:
            counts[str(cid)] = counts.get(str(cid), 0) + 1
    out.append("| 案例 | 轮次 |")
    out.append("|---|---|")
    for cid in sorted(counts):
        out.append(f"| `{cid}` | {counts[cid]} |")
    out.append("")
    out.append(
        "> 轮换**没有游标文件**：下一个跑哪个是每次从台账**现场算**的（取轮次数最少的，"
        "并列时取库里靠前的）。所以这张表就是轮换的输入。"
    )
    return "\n".join(out) + "\n"


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑一轮真实案例研判，并做四项体检")
    parser.add_argument("--wit-dir", default=None, help="默认 <仓库根>/.wit")
    parser.add_argument("--role", default=DEFAULT_ROLE)
    parser.add_argument("--case", default=None, help="指定案例 id（默认按轮换挑）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="绕过两个闸门（忙 / 偏移）；仍然只跑一轮，不会绕过四项体检",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只选案、只跑闸门，不起子进程、不落记录"
    )
    parser.add_argument("--timeout", type=int, default=ROUND_TIMEOUT_S)
    parser.add_argument(
        "--ledger-md",
        action="store_true",
        help="**不跑**，只把专项记录渲染成 Markdown（打 stdout，落盘请自行重定向）",
    )
    args = parser.parse_args(argv)

    if args.ledger_md:
        # ⚠️ 放在**最前面**：这是"读台账出材料"，与"跑一轮"是两件事。
        #    材料必须**从台账生成** —— 手工维护的副本就是第二份事实源，必然漂移。
        print(render_ledger_md(args.wit_dir, args.role), end="")
        return 0

    # ⚠️ 这里打印的是**轮换计划**，不是整份案例库：计划里已经有"每个案子跑了几轮"，
    #    而那才是每次跑之前要确认的东西。整份库（含出处）走
    #    `python -m legal_acps.case_library --library`，别把它灌进每天的日志。
    print(
        case_library.render_plan(
            case_library.plan_next_from_ledger(wit_dir=args.wit_dir, role=args.role)
        )
    )
    print()
    result = run_case_round(
        wit_dir=args.wit_dir,
        role=args.role,
        case_id=args.case,
        force=args.force,
        dry_run=args.dry_run,
        timeout_s=args.timeout,
    )
    if result.outcome == "ran":
        persisted = _persist(result, rounds_ledger_path(args.wit_dir, args.role))
        result = persisted
    elif result.outcome in ("skipped", "environment"):
        _persist(result, rounds_ledger_path(args.wit_dir, args.role))
    print()
    print(render_result(result))
    if result.tail:
        print("\n--- 子进程输出（尾部）---")
        print(result.tail)
    print(f"\n  专项记录：{rounds_ledger_path(args.wit_dir, args.role)}")
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - 人肉入口
    raise SystemExit(_main())
