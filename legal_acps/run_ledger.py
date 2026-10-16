"""主控「发起轮」台账 —— 把一次调用与它的 session / 产物连起来。

存在理由（2026-10-04 实测，**不是推测**）
----------------------------------------
`.wit/leader/acps/aip-tasks.sqlite3` 里每一行 outbound 的 `senderId` 都是**主控自己**
的 AIC（`…X1J5R5.HKBBAM.018M`），行里**没有 pid、没有 run id、没有任何标签**。
于是同一天并发跑的四轮（人工真机轮 × 3 + 自动复查 × 1）在权威库里**长得一模一样**：
当时能分清谁是谁，只因为自动复查恰好另外维护了一份记忆文件 —— 那是运气，不是机制。
（另一条同族的坑：`tools/aip_sessions.pick_live_session` 只有"新鲜候选彼此不重叠"
才敢认领，所以并发轮次下它会**拒绝写 sidecar** —— 它没做错，是缺一份时间窗事实。）

本模块补的就是这一环：每次**一次性研判**（`run_agent leader --input …`）自己落两条台账
（开一条、收一条），记下 `run_id / label / pid / 起止时刻 / 输入指纹 / 结局`。
台账是 append-only JSONL、**只增不改**，所以进程被 kill 时那条「开」记录仍在 ——
今天 14:56:53 那轮正是被外部终止的，而它恰好是最需要被认出来的那一类。

归属怎么算：存事实，不存结论
----------------------------
台账里**不写**"这条 session 归我"，只写时间窗。`attribute_sessions()` 在**读**的时候
现场算，于是晚到的收尾记录能让早先的「有争议」**自行消解**，台账里不会留下过期结论。

一轮 R 认领 session S ⇔ ① `S.created_at` 落在 R 的窗口内；② **没有第二轮的窗口也覆盖它**。
窗口 = `[started_at - slop, finished_at + slop]`；未收尾的轮次右端取 `+∞`
（它可能还在跑，这时说"归谁"就是在猜）。

取向与 `tools/aip_sessions.py` **逐条一致**：宁可认领失败，也不给一条错的归属。
拒绝时要给出**能指名道姓**的原因串，调用方把它原样带出去。

互斥：同一角色同时只许一轮（2026-10-04 补，#95 第 2 项）
--------------------------------------------------------
归属能不能算清，前提是**同一角色同时只有一轮在跑**。上面那套读时认领只是
"两轮撞车之后还能如实说『说不清』"，它**不阻止撞车本身**；而两轮同时跑会真的
互相污染：同一个 `.wit/<role>/` 只有一个收件箱、一套产物目录。

做法是 `.wit/<role>/run.lock`：**`O_CREAT|O_EXCL` 是原子抢锁**（POSIX 与 Windows
都成立），锁里记着持有者的 `run_id / pid / label / started_at`。

  · 持有者**进程还活着** ⇒ 拒绝开始，抛 `RunLocked`（带 `refusal_reason`
    + `how_to_fix`，与全项目的拒绝口径一致）。**而且一个字节都不写台账** ——
    被拒绝的轮次根本没开始，写条"未收尾"只会变成台账里永远收不掉的幽灵。
  · 持有者**进程已不在** ⇒ 那一轮是**被打断**的（不是"还在跑"）。此时先把它在
    台账里收尾成 `abandoned:stale-lock`（这就是 #95 第 4 项要的**回收口径**），
    再接管锁。

⚠️ **已知残余风险（如实写下来，不假装没有）**：接管陈旧锁这一步不是原子的 ——
   两个进程可能同时看到一个死掉的持有者、各自 unlink 再各自 create，于是
   **短暂地都以为自己拿到了锁**。没有靠"再读一次核对自己是不是持有者"去补：
   那个补救只在"接管发生在核对之前"时有效，窗口依然存在，加了反而像有保证。
   真正兜住它的是**第二道**：`attribute_sessions()` 在两轮窗口重叠时**拒绝认领**
   （见上）⇒ 撞车的后果退化成"说不清归谁"，而不是"记错了归谁"。这正是本模块
   一以贯之的取向：宁可说不知道，也不给一条错的归属。

记账失败：只降级、绝不改主流程（2026-10-04 补，#101）
----------------------------------------------------
台账是**旁路记录**（本模块自己的说法）。所以任何**写入失败**——磁盘满、只读挂载、
权限变更——都不许改变主流程的结论：

  · **开记录写不进去** ⇒ 照样跑。原先 `open_run` 在任何 `try` 之外，于是
    "记账写不下去"会让 `runner` **一次都不被调用**：一轮本来能做成的研判
    因为一本旁路账而彻底不做。这与"台账是旁路"自相矛盾。
  · **收记录写不进去** ⇒ 照样把 `result` 交回调用方。原先收尾一抛，调用方就
    **永远拿不到**那个已经落盘的正确产物，还会看到"这一轮失败了"——一条**错信号**。
  · **失败必须看得见**：降级不是静默，失败会写进返回记录的 `ledger_error`
    （调用方拿得到）**并**打到 stderr（人看得见）。
    ⚠️ 但要说清它的边界：`ledger_error` 只出现在**返回值**上——写失败的那条记录
    本身就没落盘，所以**盘上永远读不到它**。盘上那一侧的信号是另一条：
    开记录写成功、收记录写失败 ⇒ 盘上留下一条**未收尾**的轮次，而它会被下一次
    抢锁时的**陈旧锁回收**（见上）收成 `abandoned:stale-lock`。
  · **异常路径不许顶掉原异常**：`except` 块里再抛，会把"这一轮为什么失败"
    换成记账的失败。收尾记账自己已经降级，外面再加一道保险，且**原样重抛**原异常。

⚠️ 读侧宽容、写侧致命，这个取向反转曾经**哪里都没写下来**（`read_runs` 刻意
   容忍坏行，`run_ledgered` 却会因为写不下去而不跑）。现在两侧都写明了。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

__all__ = [
    "ABANDONED_STATUS",
    "LEDGER_NAME",
    "LOCK_NAME",
    "PROBE_LABEL",
    "RunLocked",
    "SLOP_S",
    "RunRecord",
    "acquire_run_lock",
    "attribute_sessions",
    "bind_run",
    "close_run",
    "current_run",
    "ledger_path",
    "label_from_env",
    "lock_path",
    "open_run",
    "open_run_locked",
    "pid_alive",
    "read_runs",
    "release_run_lock",
    "run_ledgered",
    "session_for_run",
    "unbind_run",
]

#: 台账文件名（放在 `.wit/<role>/` 下，与平台落盘的任务库同一个工作区）。
LEDGER_NAME = "runs.jsonl"

#: 轮次互斥锁文件名。与台账同目录，因为两者描述的是同一件事的两个时刻。
LOCK_NAME = "run.lock"

#: 陈旧锁回收时给被打断的那一轮写下的状态。
#:
#: **不要把 `abandoned:` 前缀去掉换成 `failed`**：这两个是不同的**事实**。
#: `failed` 的意思是"跑过了、跑砸了"；这里的意思是"**没有收到收尾记录，而它的进程
#: 已经不在了**"——它也可能其实跑成了，只是收尾那一步没落盘（见上面 §记账失败）。
#: 用 `failed` 会把"没收到消息"说成"坏消息"，而这正是本项目最忌讳的那类错。
ABANDONED_STATUS = "abandoned:stale-lock"

#: 时间窗两侧的宽限（秒）。我方记录 `started_at` 与平台写 session 都在**同一台机器**
#: 上，因此只需吸收落盘/序列化的抖动，不需要吸收时钟漂移。
#: ⚠️ 调大它会**放宽**认领条件 ⇒ 并发轮次更容易互相覆盖 ⇒ 更容易变成"有争议"。
#: 这是**安全方向**的失败（拒绝认领 > 认错），所以宁可偏小而不要偏大。
SLOP_S = 5.0

#: 默认角色。台账按角色分文件，与 `.wit/<role>/` 的既有布局对齐。
DEFAULT_ROLE = "leader"

#: 真机探针（`run/e2e_leader_image` / `run/e2e_video_live`）固定的标签。
#:
#: ⚠️ 为什么探针的标签必须住在**本模块**、不能两个探针各写一份：
#:    `check_local` 3c 要断言"探针那一轮记的就是这个标签"，而它**不复制字符串**
#:    —— 断言里引用的就是这一个常量；两个探针也引用它。
#:    三处引同一个定义，"同一事实写两处必然漂移"就没有落脚点。
#:
#: 它把发起方分成三类：`unlabeled`（没自报）/ `automation`（定时复查，由自动化
#: 的提示词自己传）/ `e2e-probe`（人工真机探针）。缺了第三类，探针轮就会退回
#: `unlabeled`，与"没自报的人工轮"混在一起 —— 那正是台账要消灭的东西。
PROBE_LABEL = "e2e-probe"

#: 输入文本在台账里只留一个**指纹 + 头部摘要**：指纹用于机器比对，
#: 摘要用于人肉辨认"这是哪一轮"。全文不落台账（它已经在上游的任务库里了）。
TEXT_HEAD_CHARS = 60

#: 当前进程正在跑的那一轮。**故意用 ContextVar 而不是模块级变量**：
#: 同一个进程里可以并发跑多轮（asyncio），模块级变量会互相覆盖 ——
#: 那正是本模块要消灭的那类缺陷（把 A 轮的归属写到 B 轮上）。
_CURRENT: ContextVar["RunRecord | None"] = ContextVar("acps_current_run", default=None)


def _default_wit_dir() -> Path:
    """`<仓库根>/.wit`。与 `legal_acps/agents.py` 的 `WIT_DIR` 同一口径。"""
    return Path(__file__).resolve().parent.parent / ".wit"


def ledger_path(wit_dir: str | os.PathLike[str] | None = None, role: str = DEFAULT_ROLE) -> Path:
    """`.wit/<role>/runs.jsonl`。**只算路径，不创建目录**。"""
    root = Path(wit_dir) if wit_dir is not None else _default_wit_dir()
    return root / role / LEDGER_NAME


def lock_path(wit_dir: str | os.PathLike[str] | None = None, role: str = DEFAULT_ROLE) -> Path:
    """`.wit/<role>/run.lock`。**只算路径，不创建目录**。"""
    root = Path(wit_dir) if wit_dir is not None else _default_wit_dir()
    return root / role / LOCK_NAME


class RunLocked(RuntimeError):
    """同角色的另一轮正在跑 ⇒ **拒绝开始**（不排队、不等待、不覆盖）。

    ⚠️ 拒绝口径与全项目一致：必须同时给出**能指名道姓的原因**与**可执行的出路**。
       "另一个没跑完"这种话等于把排查成本转嫁给下一个人 —— 他连该去看哪个
       文件、该杀哪个 pid 都不知道。
    """

    def __init__(self, refusal_reason: str, how_to_fix: str) -> None:
        super().__init__(refusal_reason)
        self.refusal_reason = refusal_reason
        self.how_to_fix = how_to_fix


def pid_alive(pid: int) -> bool | None:
    """进程还活着吗。**判断不了时返回 `None`**（调用方按"可能活着"处理 = fail-closed）。

    为什么不能用"锁文件有多老"来判：那需要一个**猜出来的**超时值，而一轮真实研判
    的耗时随下游而定（实测 179s ~ 247s，且随时可能更长）。**猜错的方向是致命的**：
    猜短了会把还在跑的轮次判成死的、于是放第二轮进来 —— 那正是锁要防的事。
    进程是否存活是一个**可观测的事实**，比任何超时值都硬。

    ⚠️ 两个平台给出的信号**不是同一个 errno**（2026-10-04 实测）：
       POSIX 上"不存在"是 `ProcessLookupError`（ESRCH）；**Windows 上是
       `OSError(winerror=87)`**（参数错误），`ESRCH` 那条分支根本走不到。
       只写 POSIX 那一支的话，Windows 侧会一路落到 `None` ⇒ **永远判"可能活着"**
       ⇒ 锁一旦残留就再也接不了管，而且**不报任何错**。
    """
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # POSIX：进程在，只是不属于本用户 —— 那是"活着"的**证据**，不是"不知道"。
        return True
    except OSError as exc:
        if getattr(exc, "winerror", None) == 87:
            return False
        return None
    except Exception:  # noqa: BLE001 - 探测失败不许影响主流程
        return None
    return True


def label_from_env() -> str:
    """调用方自报的标签（`ACPS_RUN_LABEL`），没设就是 `unlabeled`。

    ⚠️ 为什么默认值叫 `unlabeled` 而不是 `manual`：本模块**不能**分辨调用方是谁，
    默认成 `manual` 就等于**替调用方猜了一个归属** —— 那正是要消灭的东西。
    分辨不了就如实说不知道；`pid` / `ppid` / `argv` 仍然能把两轮区分开。
    """
    value = (os.environ.get("ACPS_RUN_LABEL") or "").strip()
    return value or "unlabeled"


def _iso(value: datetime) -> str:
    """统一落成带 `Z` 的 UTC ISO 串（平台侧的会话时间也是 UTC）。"""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _align(ref: datetime, like: datetime) -> datetime:
    """把 `ref` 的 tz 形态对齐到 `like`。naive 与 aware 直接相减会抛 `TypeError`。

    与 `tools/aip_sessions._age` 同一手法 —— 那边也已经踩过这个坑。
    """
    if ref.tzinfo is None and like.tzinfo is not None:
        return ref.replace(tzinfo=like.tzinfo)
    if ref.tzinfo is not None and like.tzinfo is None:
        return ref.replace(tzinfo=None)
    return ref


@dataclass(frozen=True)
class RunRecord:
    """一轮主控调用。`finished_at is None` ⇒ **未收尾**（正常结束、或被 kill）。"""

    run_id: str
    role: str
    label: str
    pid: int
    ppid: int
    started_at: str
    text_sha256: str
    text_head: str
    argv: tuple[str, ...]
    finished_at: str | None = None
    status: str | None = None
    iterations: int | None = None
    #: **记账本身**失败的原因（不是这一轮失败的原因）。空 = 记进去了。
    #:
    #: ⚠️ 这个字段**几乎不会出现在盘上**：写失败的那条记录根本没落盘。它存在的
    #:    意义是交给**当前这次调用**——调用方读它就知道"产物的有效性没有被记账背书"。
    #:    盘上那一侧要看的是另一件事：开记录在位、收记录缺失 ⇒ 未收尾，由陈旧锁回收。
    ledger_error: str | None = None

    @property
    def unfinished(self) -> bool:
        return self.finished_at is None

    @property
    def started(self) -> datetime | None:
        return _parse(self.started_at)

    @property
    def finished(self) -> datetime | None:
        return _parse(self.finished_at)

    def window(self, *, slop_s: float = SLOP_S) -> tuple[datetime, datetime | None] | None:
        """`(下界, 上界)`；上界为 `None` 表示**尚未收尾 ⇒ 上界开放**。"""
        start = self.started
        if start is None:
            return None
        end = self.finished
        return (
            start - timedelta(seconds=slop_s),
            None if end is None else end + timedelta(seconds=slop_s),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "role": self.role,
            "label": self.label,
            "pid": self.pid,
            "ppid": self.ppid,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "iterations": self.iterations,
            "text_sha256": self.text_sha256,
            "text_head": self.text_head,
            "argv": list(self.argv),
            "ledger_error": self.ledger_error,
        }


def _from_json(raw: Any) -> RunRecord | None:
    if not isinstance(raw, dict):
        return None
    run_id = str(raw.get("run_id") or "")
    if not run_id:
        return None
    argv = raw.get("argv")
    return RunRecord(
        run_id=run_id,
        role=str(raw.get("role") or DEFAULT_ROLE),
        label=str(raw.get("label") or ""),
        pid=int(raw.get("pid") or 0),
        ppid=int(raw.get("ppid") or 0),
        started_at=str(raw.get("started_at") or ""),
        text_sha256=str(raw.get("text_sha256") or ""),
        text_head=str(raw.get("text_head") or ""),
        argv=tuple(str(x) for x in argv) if isinstance(argv, list) else (),
        finished_at=(str(raw["finished_at"]) if raw.get("finished_at") else None),
        status=(str(raw["status"]) if raw.get("status") else None),
        iterations=(int(raw["iterations"]) if isinstance(raw.get("iterations"), int) else None),
        ledger_error=(str(raw["ledger_error"]) if raw.get("ledger_error") else None),
    )


def _append(path: Path, record: RunRecord) -> None:
    """追加一条。**写不下去就抛** —— 降级由调用方决定（见 `_persist`）。

    并发安全性靠 **`O_APPEND` + 单次 `write`**：以 `"a"` 打开的文件在 POSIX 下，
    写入是"定位到文件尾 + 写"的原子动作，所以两个进程各写一行不会互相拼进去。

    ⚠️ 这里**不能**拿 `PIPE_BUF` 当依据（本函数第一版就是这么写的，是错的）：
       `PIPE_BUF` 管的是**管道 / FIFO** 的原子性，与普通文件毫无关系 ——
       一句**引错常量**的注释比没有注释更糟：它看上去像有依据。
       真正要守的纪律只有一条：**一次 `write` 写完一整行**（别循环写、别先 `seek`）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.to_json(), ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)


def _warn_ledger_unavailable(action: str, exc: BaseException, path: Path) -> None:
    """记账写不下去时**必须响**：降级可以，静默不行。

    ⚠️ 打到 stderr 而不是 stdout：人肉入口（`_main`）的正常输出就在 stdout 上，
    把告警混进去会让"台账内容"和"台账坏了"看起来是同一类东西。
    """
    print(
        f"[ledger] ⚠️ 记账失败（{action}）：{path}\n"
        f"         {type(exc).__name__}: {exc}\n"
        "         台账是**旁路记录** ⇒ 主流程照常进行；但这一轮在溯源上会缺一份凭证。",
        file=sys.stderr,
    )


def _persist(record: RunRecord, *, wit_dir: str | os.PathLike[str] | None) -> RunRecord:
    """把记录落盘。**写不下去也不抛**：改成"告警 + 在返回值上如实记一笔"。

    ⚠️ 台账是**旁路记录**（本模块自己的说法），所以它的失败**不许**改变主流程的
       结论。于是这里两条路都不能走：

         · 静默吞掉 ⇒ 没人知道溯源缺了一环 —— 正是本项目最忌讳的"静默降级"；
         · 照原样抛出 ⇒ 一本旁路账把一轮**本来能做成的研判**判成了失败。

       两条都不行的中间态只有一种：**保住主流程，并把失败记成一条可读的事实**。
       返回的 `ledger_error` 给机器（调用方自己判断要不要理会），stderr 给人。
    """
    try:
        _append(ledger_path(wit_dir, record.role), record)
    except Exception as exc:  # noqa: BLE001 - 宽是有意的，见 docstring
        _warn_ledger_unavailable("写台账", exc, ledger_path(wit_dir, record.role))
        return replace(record, ledger_error=f"{type(exc).__name__}: {exc}")
    return record


def _fingerprint(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_record(
    *,
    label: str | None,
    role: str,
    text: str,
    argv: Sequence[str] | None,
    now: datetime | None,
    run_id: str | None,
) -> RunRecord:
    """只造记录、**不落盘**。

    为什么要单独一步：抢锁要拿 `run_id / pid / label / started_at` 去写锁文件，
    而**先抢锁、后落台账**是刻意的顺序（被拒绝的轮次不该在台账里留幽灵）。
    若在这里另算一遍 pid/时刻，就会与 `open_run` 的那一份**迟早对不上** ——
    "同一事实写两处必然漂移"。
    """
    started = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return RunRecord(
        run_id=run_id or uuid.uuid4().hex[:12],
        role=role,
        label=label if label is not None else label_from_env(),
        pid=os.getpid(),
        ppid=os.getppid(),
        started_at=_iso(started),
        text_sha256=_fingerprint(text),
        text_head=text.strip().replace("\n", " ")[:TEXT_HEAD_CHARS],
        argv=tuple(str(a) for a in (argv if argv is not None else sys.argv)),
    )


def open_run(
    *,
    label: str | None = None,
    role: str = DEFAULT_ROLE,
    text: str = "",
    wit_dir: str | os.PathLike[str] | None = None,
    argv: Sequence[str] | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
) -> RunRecord:
    """开始一轮：**先落盘再返回**。

    ⚠️ 顺序是刻意的 —— 先落盘，进程随后被 kill 也留得下痕迹。
       今天 14:56:53 那轮就是被外部终止的：它在任务库里停在 `state=working`、
       `products` 为空，事后只能靠 `evidence-*` 文件的时间戳猜是谁 —— 猜出来的
       归属没有一条是硬键。有了这条「开」记录，那一轮从**发起**那一刻就有主。

    ⚠️ 写不下去**不抛**（见 `_persist`）：一本旁路账不该让一轮研判一点都不做。
       代价如实说清 —— 此时"先落盘"这条就**做不到**了（盘上什么都没有），
       返回的记录会带 `ledger_error`，而它**不会**出现在盘上。

    本函数**不碰运行锁**：锁是"轮次"这一层的概念，而那些直接调 `open_run` 的地方
    （自检、离线核对）本来就不是在跑一轮。要互斥地用一轮，走 `open_run_locked`。
    """
    record = _build_record(
        label=label, role=role, text=text, argv=argv, now=now, run_id=run_id
    )
    return _persist(record, wit_dir=wit_dir)


def close_run(
    run: RunRecord,
    *,
    status: str | None = None,
    iterations: int | None = None,
    wit_dir: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> RunRecord:
    """收尾一轮：再追加一条同 `run_id` 的记录（**不是**改写上面那条）。

    ⚠️ **本函数永不因写盘失败而抛**（见 `_persist`）。这一点是**契约**，不是巧合：
       `run_ledgered` 的异常分支里正是靠它才敢直接调它 —— 若它抛，
       就会把"这一轮为什么失败"**顶掉**成记账的失败（`raise` 换成了另一个异常）。
       ⇒ 真要改这条契约，必须同时去看那条分支。
    """
    finished = replace(
        run,
        finished_at=_iso((now or datetime.now(timezone.utc)).astimezone(timezone.utc)),
        status=status,
        iterations=iterations,
    )
    return _persist(finished, wit_dir=wit_dir)


def read_runs(
    path: str | os.PathLike[str] | None = None,
    *,
    wit_dir: str | os.PathLike[str] | None = None,
    role: str = DEFAULT_ROLE,
) -> list[RunRecord]:
    """读台账并**按 `run_id` 折叠**（后写的非空字段覆盖先写的），按开始时刻排序。

    坏行（截断 / 手改坏）**逐行跳过**：台账是旁路记录，不该因为它坏一行就让
    整个溯源链崩掉 —— 但跳过的行数会以 `None` 之外的形态如实体现（调用方看到
    "少了一轮"），而不是静默假装完整。
    """
    target = Path(path) if path is not None else ledger_path(wit_dir, role)
    if not target.is_file():
        return []
    folded: dict[str, RunRecord] = {}
    order: list[str] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = _from_json(json.loads(line))
        except (ValueError, TypeError):
            continue
        if record is None:
            continue
        previous = folded.get(record.run_id)
        if previous is None:
            order.append(record.run_id)
            folded[record.run_id] = record
            continue
        folded[record.run_id] = replace(
            previous,
            finished_at=record.finished_at or previous.finished_at,
            status=record.status or previous.status,
            iterations=(
                record.iterations if record.iterations is not None else previous.iterations
            ),
            label=record.label or previous.label,
        )
    # ⚠️ **不能按 `started_at` 字符串排**：同一秒内"带微秒"与"不带微秒"两种形态会排反
    #    —— `…07:22:11.4Z` 里的 `.`(0x2E) < `…07:22:11Z` 里的 `Z`(0x5A)，
    #    于是**晚发生的那一轮排到前面去**。按解析后的绝对时刻排。
    #    用 `timestamp()`（浮点）而不是直接比 datetime：手改过的行可能是 naive，
    #    与 aware 直接比较会抛 `TypeError` —— 台账坏一行不该让整轮溯源崩掉。
    def _order_key(record: RunRecord) -> tuple[float, str]:
        started = record.started
        if started is None:
            return (float("-inf"), record.run_id)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (started.timestamp(), record.run_id)

    return sorted((folded[r] for r in order), key=_order_key)


# --------------------------------------------------------------------------
# 轮次互斥：同一角色同时只许一轮（#95 第 2 项）
#
# 归属能不能算清，前提是**同时只有一轮在跑**。`attribute_sessions` 只在撞车之后
# 如实说"说不清"，它**不阻止撞车**；而两轮同时跑会真的互相污染 —— 同一个
# `.wit/<role>/` 只有一个收件箱、一套产物目录、一份 sidecar 命名空间。
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LockHolder:
    """锁文件的持有者。只放判「谁挡住了我、该怎么处理」所必需的四个字段。

    ⚠️ 刻意**不复用 `RunRecord`**：两者的字段集不同（锁不需要 argv/指纹，
       台账不需要"我挡住了谁"），复用会让"给台账加一个字段"顺带改变锁文件的形状。
    """

    run_id: str
    pid: int
    label: str
    started_at: str

    def describe(self) -> str:
        return (
            f"run_id={self.run_id}、label={self.label or '?'}、"
            f"pid={self.pid}、起于 {self.started_at or '?'}"
        )


def _read_lock(path: Path) -> LockHolder | None:
    """读锁文件。**读不出就返回 `None`**（= "读不出"，不是"没有锁"）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("run_id"):
        return None
    return LockHolder(
        run_id=str(raw["run_id"]),
        pid=int(raw.get("pid") or 0),
        label=str(raw.get("label") or ""),
        started_at=str(raw.get("started_at") or ""),
    )


def _reclaim_interrupted(
    holder: LockHolder,
    *,
    wit_dir: str | os.PathLike[str] | None,
    role: str,
) -> RunRecord | None:
    """把陈旧锁对应的那一轮在台账里收尾成 `ABANDONED_STATUS`。

    这就是 #95 第 4 项要的**回收口径**：库里/台账里那一堆"永远 `working`"的残留，
    靠"它自己的进程已经不在了"这一个**可观测事实**收敛掉，而不是靠猜一个超时值。

    ⚠️ 写进去的是 `abandoned:stale-lock` 而**不是** `failed`：前者说的是
       "没有收到收尾记录，而它的进程已经不在了"，它也可能其实跑成了、
       只是收尾那一步没落盘（见模块头 §记账失败）。把"没收到消息"写成"坏消息"，
       会让后来的人去追一个不存在的失败原因、或者反过来漏掉已落盘的产物。

    ⚠️ 台账里**没有**这一轮、或它本来就已经收尾 ⇒ 返回 `None`，**不编一条出来**。
       回收的职责是"补上缺失的收尾"，不是"制造一条收尾"。
    """
    run = next(
        (r for r in read_runs(wit_dir=wit_dir, role=role) if r.run_id == holder.run_id),
        None,
    )
    if run is None or not run.unfinished:
        return None
    return close_run(run, status=ABANDONED_STATUS, wit_dir=wit_dir)


def acquire_run_lock(
    run: RunRecord,
    *,
    wit_dir: str | os.PathLike[str] | None = None,
    role: str | None = None,
) -> RunRecord | None:
    """为 `run` 抢本角色这一轮的锁。返回**被回收的陈旧轮次**（没有则 `None`）。

    抢不到时抛 `RunLocked`。三种抢不到：持有者活着 / 持有者状态读不出 / 极端并发下
    连续被别人抢先；三种都给不同的原因与不同的出路（"责任分清"）。
    """
    target_role = role or run.role
    path = lock_path(wit_dir, target_role)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "run_id": run.run_id,
            "pid": run.pid,
            "label": run.label,
            "started_at": run.started_at,
            "role": target_role,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    reclaimed: RunRecord | None = None
    for _ in range(3):
        try:
            # ⚠️ `"x"` 模式 = `O_CREAT|O_EXCL`，**这就是原子抢锁本身**：
            #    "文件已存在则创建失败"由内核保证。反例是 `exists()` 再写 ——
            #    两个进程可以同时通过存在性检查，然后一起写。
            with open(path, "x", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
            return reclaimed
        except FileExistsError:
            holder = _read_lock(path)
            if holder is None:
                raise RunLocked(
                    f"同角色（{target_role}）的轮次锁 {path} 存在，但**读不出持有者**"
                    "（半截写、手工残留，或根本不是本模块写的）⇒ 无法判断持有者是死是活，"
                    "**按最坏情况拒绝开始**。",
                    f"先确认没有别的 `run_agent {target_role}` 在跑，再删除 {path} 后重试；"
                    "若那个路径本就被别的东西占着，请换一个 wit_dir。",
                ) from None
            alive = pid_alive(holder.pid)
            if alive is not False:
                why = "进程仍在" if alive else "无法判断该进程是否还在（按最坏情况处理）"
                raise RunLocked(
                    f"同角色（{target_role}）已有另一轮在跑：{holder.describe()}，{why}"
                    " ⇒ **拒绝开始** —— 两轮共用同一个收件箱与产物目录，会把"
                    "「这一轮是谁发起的、产物归谁」搅成一团说不清的账。",
                    f"等它结束后重试（`python -m legal_acps.run_ledger --role "
                    f"{target_role}` 能看到它的状态）；**不要**直接删 {path} —— "
                    "它一走，那一轮就会在台账里留下永远收不了尾的痕迹。",
                ) from None
            # 持有者进程已不在 ⇒ 那一轮**被打断**了（不是"还在跑"）：先回收，再接管。
            reclaimed = _reclaim_interrupted(holder, wit_dir=wit_dir, role=target_role)
            path.unlink(missing_ok=True)
            continue
    raise RunLocked(
        f"连续 3 次都没抢到 {path}：每一轮接管之后立刻被别的进程抢先"
        "（这是「陈旧锁接管不是原子操作」那个已知窗口被反复命中）。",
        "稍后重试；若持续如此，说明有多个 `run_agent` 在互相接管同一把锁，"
        "请先排查是否有并发调度在反复重启这个角色。",
    )


def release_run_lock(
    run: RunRecord,
    *,
    wit_dir: str | os.PathLike[str] | None = None,
    role: str | None = None,
) -> bool:
    """释放**自己**那把锁；`run_id` 对不上就一个字节都不动，返回 `False`。

    ⚠️ 只认自己：替**别人**解锁会把两条轮次的边界擦掉 —— 那正好是这把锁要守的东西。
    """
    path = lock_path(wit_dir, role or run.role)
    holder = _read_lock(path)
    if holder is None or holder.run_id != run.run_id:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _release_run_lock_quietly(
    run: RunRecord,
    *,
    wit_dir: str | os.PathLike[str] | None,
    role: str,
) -> None:
    """释放锁，失败只告警。

    ⚠️ 为什么要有这个包装：正常路径与异常路径**都要**释放，两处各写一遍迟早漂移
       （漂移的会是"没人盯的那一半"）。但失败**不能**抛 —— 它发生在 `finally`
       与 `except` 的附近，抛出去会顶掉真正要报的那个异常。
    """
    if not release_run_lock(run, wit_dir=wit_dir, role=role):
        print(
            f"[ledger] ⚠️ 本轮（{run.run_id}）的运行锁没能释放：{lock_path(wit_dir, role)}\n"
            "         若确认没有别的轮在跑，可手工删除该文件；"
            "否则下一轮会把它当成「被打断的轮次」回收（能自愈，但不干净）。",
            file=sys.stderr,
        )


def open_run_locked(
    *,
    label: str | None = None,
    role: str = DEFAULT_ROLE,
    text: str = "",
    wit_dir: str | os.PathLike[str] | None = None,
    argv: Sequence[str] | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
) -> tuple[RunRecord, RunRecord | None]:
    """互斥地开始一轮：**先抢锁、再落台账**。返回 `(本轮记录, 被回收的陈旧轮次)`。

    ⚠️ 顺序是刻意的：抢不到锁时**台账里一个字都不写**。被拒绝的轮次根本没开始，
       在台账里留一条"未收尾"只会变成永远收不掉的幽灵 —— 而那正是本台账
       存在的理由要消灭的东西（"分不清谁是谁"）。
    """
    record = _build_record(
        label=label, role=role, text=text, argv=argv, now=now, run_id=run_id
    )
    reclaimed = acquire_run_lock(record, wit_dir=wit_dir, role=role)
    return _persist(record, wit_dir=wit_dir), reclaimed


def attribute_sessions(
    runs: Sequence[RunRecord],
    sessions: Iterable[Any],
    *,
    slop_s: float = SLOP_S,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """把 session 认领给轮次。返回 `(唯一归属, 有争议的候选)`。

    ⚠️ **读时计算**，不是写时落库的结论：今天 14:53 那两轮在**收尾之前**看是
       "有争议"的，等两轮都收尾后同一份台账就能唯一指认 —— 结论会随事实长大，
       所以只存事实。
    """
    windows: list[tuple[str, datetime, datetime | None]] = []
    for run in runs:
        window = run.window(slop_s=slop_s)
        if window is not None:
            windows.append((run.run_id, window[0], window[1]))

    claimed: dict[str, str] = {}
    contested: dict[str, list[str]] = {}
    for session in sessions:
        session_id = str(getattr(session, "session_id", "") or "")
        created = getattr(session, "created_at", None)
        if not session_id or not isinstance(created, datetime):
            # `created_at` 解析不出来的会话**不认领**：没有时刻就没有窗口，
            # 硬塞一个归属就是猜。
            continue
        hits: list[str] = []
        for run_id, lo, hi in windows:
            moment = _align(created, lo)
            if moment >= lo and (hi is None or moment <= hi):
                hits.append(run_id)
        if not hits:
            continue
        if len(hits) == 1:
            claimed[session_id] = hits[0]
        else:
            contested[session_id] = hits
    return claimed, contested


def session_for_run(
    runs: Sequence[RunRecord],
    run_id: str,
    sessions: Sequence[Any],
    *,
    slop_s: float = SLOP_S,
) -> tuple[str | None, str]:
    """`run_id` 这一轮**唯一**的 session；不唯一时返回 `(None, 原因)`。

    ⚠️ **必须传入全部轮次**，不能只传自己那一轮 —— 这是本函数第一个版本踩的坑：
       它内部调的是 `attribute_sessions([run], …)`，窗口列表里只有一条 ⇒
       `len(hits)` 恒为 1 ⇒ **"有争议"那条分支永远走不到** ⇒ 并发轮次下照样替人二选一。
       而 sidecar 走的正是这个函数，也就是说**模块要防的那件事在它自己的关键路径上没防住**。
       （2026-10-04 复查实测抓到；已补 3c 断言 + 第 3 个元验证用例。）
       ⇒ 教训一句话：**把判据的输入裁剪到只剩自己，等于把判据关掉。**

    原因串**必须能指名道姓**（哪几条 session、为什么说不清），因为它会被原样写进
    sidecar 缺失时的告警 —— 一句含糊的"无法确定"等于把排查成本转嫁给下一个人。
    """
    target = next((r for r in runs if r.run_id == run_id), None)
    window_desc = (
        f"{target.started_at} → {target.finished_at or '（未收尾）'}"
        if target is not None
        else "（未知窗口）"
    )
    claimed, contested = attribute_sessions(runs, sessions, slop_s=slop_s)
    mine = sorted(sid for sid, rid in claimed.items() if rid == run_id)
    if len(mine) == 1:
        return mine[0], f"本轮窗口 {window_desc} 内唯一 session"
    if not mine:
        clashes = sorted(sid for sid, rids in contested.items() if run_id in rids)
        if clashes:
            shown = "、".join(f"…{s[-12:]}" for s in clashes[:4])
            return None, (
                f"本轮（run_id={run_id}）窗口内有 {len(clashes)} 条会话**同时被别的轮次覆盖**"
                f"（{shown}）⇒ 说不清归谁，拒绝认领，宁缺勿错"
            )
        return None, f"本轮（run_id={run_id}）的窗口内没有任何会话记录"
    shown = "、".join(f"…{s[-12:]}" for s in mine[:4])
    return None, (
        f"本轮（run_id={run_id}）窗口内有 {len(mine)} 个会话（{shown}）"
        "⇒ 无法判断裁定书属于哪一个，拒绝认领，宁缺勿错"
    )


# --------------------------------------------------------------------------
# 进程内绑定：让"落 verdict 的那段代码"知道自己在哪一轮里
# --------------------------------------------------------------------------


def bind_run(run: RunRecord) -> Token["RunRecord | None"]:
    """把 `run` 绑到当前上下文，返回可 `unbind_run` 的 token。"""
    return _CURRENT.set(run)


def unbind_run(token: Token["RunRecord | None"]) -> None:
    _CURRENT.reset(token)


def current_run() -> RunRecord | None:
    """当前上下文里的那一轮；不在任何一次 `open_run` 里时返回 `None`。

    ⚠️ `None` 是**合法且在服务模式下常见**的答案（常驻进程收的是平台派进来的
       inbound 任务，没有一个"发起轮"）。调用方必须能接受它，并**降级**而不是猜。
    """
    return _CURRENT.get()


# --------------------------------------------------------------------------
# 唯一的「开 → 绑 → 跑 → 解绑 → 收」实现
# --------------------------------------------------------------------------


def _result_fields(result: Any) -> tuple[str | None, int | None]:
    """从 run 结果里取 `(status, iterations)`；取不到就**如实记 `None`**。

    ⚠️ 用 `getattr` 而不是直接 `result.status`，理由不是"图省事"：
       本包装是**通用**的（一次性研判与两个探针共用），而"结果对象缺这两个字段"
       绝不该让台账**丢掉收尾记录** —— 丢掉了那一轮就变成「未收尾」，
       而"其实跑完了"被记成"还在跑"是一条**错的事实**，比缺个字段更糟。
       取不到就记 `None`；人肉入口会把它显示成 `?`（**不是**「未收尾」，见 `_main`）。
    """
    status = getattr(result, "status", None)
    iterations = getattr(result, "iterations", None)
    return (
        status if isinstance(status, str) else None,
        iterations if isinstance(iterations, int) else None,
    )


async def run_ledgered(
    runner: Callable[[], Awaitable[Any]],
    *,
    text: str,
    label: str | None = None,
    role: str = DEFAULT_ROLE,
    wit_dir: str | os.PathLike[str] | None = None,
) -> tuple[Any, RunRecord]:
    """把任意「跑一轮」的动作包进发起轮台账，返回 `(结果, 台账记录)`。

    ⚠️ 第二条是**收尾后**的记录（`finished_at` 非空）。第一版返回的是 `open_run`
       那条（`finished_at` 为 None）—— 那是个**接口歧义**：调用方拿到它再问
       `unfinished` 会得到 `True`，看起来像"还在跑"，而它其实早跑完了。
       （写这一条的自检脚本当场就掉进去了。）三个调用点都只读 `run_id/label/pid`，
       两者都有，所以改返回收尾记录是零风险的、且更不容易误用。

    **本函数是开/绑/收的唯一实现**，四处调用：`run_agent.run_research_ledgered`
    （一次性研判）与三个真机探针（`run/e2e_leader_image`、`run/e2e_video_live`、
    `run/e2e_audio_live`）。
    ⚠️ 探针清单**按目录现场派生**（`run/e2e_*.py`），不在任何地方手写 —— 手写名单
    必然漏项：音频那一条整场没有探针（#122），而"没人核"与"核过了没问题"长得一模一样。
    为什么合成一处：以前只有一次性研判那条路记台账，探针那条路**不记** ——
    (2026-10-04 实测)跑完一轮探针，台账仍然只有 2 条；于是台账保留了它本来要
    消灭的那个盲区，只是盲区从「人工轮 vs 自动复查轮」挪到了「人工/自动 vs 探针」。
    几条路各写一份开/绑/收，迟早漂移，而漂移的那一半恰好是没人盯着的那一半（探针）。

    ⚠️ 参数是 `runner` **工厂**（无参、返回 awaitable），不是现成的协程对象：
       协程对象一创建就开始排期，若在 `open_run` **之前**创建，"先落盘再跑"的
       时序就不成立了 —— 而那条时序正是本台账存在的理由（被 kill 的轮次也要留痕）。

    ⚠️ `runner` 抛出的异常**照原样向上抛**，但先往台账写一条
       `status="raised:<异常类名>"`。探针的调用点仍然用 `try/except` 把异常
       打印成人能看的形态并 `return 1` —— 外层行为不变。

    ⚠️ **记账失败不许顶掉原异常**（#101）：异常分支里就是一句裸的 `close_run(...)`，
       没有额外的 `try/except` 保护 —— 因为 `close_run` 的契约是**永不因写盘失败而抛**
       （见它的 docstring）。加一层"永不可能触发"的保护等于给自己一个假的安心
       （本项目纪律：「装了」≠「会响」）。真正守住这条的是那个契约本身，
       而它由 3c 节的判据盯着；要改契约就必须同时去看这一行。

    ⚠️ 一轮 = **一把锁**（#95 第 2 项）：抢不到锁会抛 `RunLocked`，此时台账里
       一个字都不写。锁在所有出口都会释放（含异常路径）。
    """
    ledger_run, reclaimed = open_run_locked(
        label=label, role=role, text=text, wit_dir=wit_dir
    )
    if reclaimed is not None:
        # 响亮地说出来：有一条"其实可能跑成过、只是没收到收尾"的轮次被收敛了，
        # 这是需要人知道的事 —— 静默回收等于把一次中断伪装成正常序列。
        print(
            f"[ledger] ⚠️ 上一轮（{reclaimed.run_id}）没有收尾记录、其进程已不在 ⇒ "
            f"台账里收成 {ABANDONED_STATUS}；本轮接管 {lock_path(wit_dir, role)}",
            file=sys.stderr,
        )
    token = bind_run(ledger_run)
    try:
        result = await runner()
    except BaseException as exc:  # noqa: BLE001 - 台账要记下"这一轮怎么没的"
        close_run(ledger_run, status=f"raised:{type(exc).__name__}", wit_dir=wit_dir)
        _release_run_lock_quietly(ledger_run, wit_dir=wit_dir, role=role)
        raise
    finally:
        unbind_run(token)
    status, iterations = _result_fields(result)
    closed = close_run(
        ledger_run, status=status, iterations=iterations, wit_dir=wit_dir
    )
    # ⚠️ 先写收尾记录、**再**放锁：反过来的话，下一个轮次会在收尾记录落盘之前
    #    进来，把这一轮当成"被打断的"回收掉（虽然后来会被真正的收尾记录折叠回来，
    #    但那是靠运气自愈，不是靠顺序正确）。
    _release_run_lock_quietly(ledger_run, wit_dir=wit_dir, role=role)
    return result, closed


# --------------------------------------------------------------------------
# 人肉入口：把台账摊开看
# --------------------------------------------------------------------------


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="查看主控发起轮台账（只读）")
    parser.add_argument("--wit-dir", default=None, help="默认 <仓库根>/.wit")
    parser.add_argument("--role", default=DEFAULT_ROLE)
    parser.add_argument(
        "--sessions-from",
        default=None,
        help="可选：从该角色的任务库读 aip_sessions 并现场算归属",
    )
    args = parser.parse_args(argv)

    runs = read_runs(wit_dir=args.wit_dir, role=args.role)
    if not runs:
        print(f"[runs] 台账为空或不存在：{ledger_path(args.wit_dir, args.role)}")
        return 0

    print(f"[runs] {len(runs)} 轮（{ledger_path(args.wit_dir, args.role)}）")
    print(f"{'run_id':<14}{'label':<14}{'pid':>8}  {'started_at':<22}{'status':<12}iters  text")
    for run in runs:
        # ⚠️ 「未收尾」的判据是 **`finished_at` 为空**，不是 `status` 为空。
        #    通用包装 `run_ledgered` 允许结果对象不带 status（那种情况如实记 None），
        #    而那种轮次**已经收尾了**；按 status 判会把它显示成「未收尾」——
        #    一条错的显示比 `?` 更糟：它会让人去找一个根本不存在的僵尸进程。
        #
        # ⚠️ 而「未收尾」本身还要再分两种，它们的**含义完全不同**（#95 第 4 项）：
        #      · 进程还在   ⇒ 它**可能还在跑** ⇒ 「未收尾」，该等就等；
        #      · 进程已不在 ⇒ 它**永远不会再收尾**了 ⇒「中断」，该看产物或补账。
        #    混成一个字，要么让人去等一个已经不存在的进程，要么把一个还在跑的
        #    轮次当成死轮去"回收"。判据取 `pid_alive()` 这个**可观测事实**，
        #    不取任何猜出来的超时值。
        if not run.unfinished:
            state = run.status or "?"
        elif pid_alive(run.pid) is False:
            state = "(中断)"
        else:
            state = "(未收尾)"
        print(
            f"{run.run_id:<14}{run.label:<14}{run.pid:>8}  "
            f"{run.started_at[:19]:<22}{state:<12}"
            f"{'-' if run.iterations is None else run.iterations:<6}  {run.text_head[:34]}"
        )

    if args.sessions_from:
        from tools.aip_sessions import read_sessions  # 延迟导入：只在需要时依赖 tools

        sessions = read_sessions(args.sessions_from)
        claimed, contested = attribute_sessions(runs, sessions)
        print(f"\n[sessions] 平台会话 {len(sessions)} 条")
        for session in sessions:
            sid = session.session_id
            if sid in claimed:
                print(f"  …{sid[-12:]}  ← run {claimed[sid]}")
            elif sid in contested:
                print(f"  …{sid[-12:]}  ← **有争议**：{' / '.join(contested[sid])}")
            else:
                print(f"  …{sid[-12:]}  ← 无轮次认领")
    return 0


if __name__ == "__main__":  # pragma: no cover - 人肉入口
    raise SystemExit(_main())
