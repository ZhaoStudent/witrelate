"""探针：离线把 case_round 的闸门与四项体检逐个走一遍（只读，不联网）。

为什么要有它：`case_round` 的正确性**不能**靠"真机跑一轮看看"来确认 —— 真机一轮
900 秒、而且失败原因混在一起（网络/模型/凭据）。这里用**假子进程**把每一类失败
单独造出来，一行一行地看清楚"哪一项该红、红成什么样"。

⚠️ 它**替换**了三处模块全局（`output_dir` / `log_paths` / `check_ports`）与
   `_spawn_round`，全部指向临时目录 —— 不许碰到真仓库的 `output/` 与 `.wit/`。
   `run_case_round` 内部调的是**模块级名字**（不是在 import 期绑定的引用），
   所以替换有效；若哪天有人把它们改成参数默认值绑定，这个探针会当场失效。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from legal_acps import case_round as CR  # noqa: E402
from legal_acps import case_library as CL  # noqa: E402
from legal_acps.acps_config import DEPLOY_ROLES, PORTS  # noqa: E402
from legal_acps.run_ledger import open_run, close_run  # noqa: E402

# ⚠️ **在任何 Sandbox 建立之前**抓住"真"函数：`Sandbox._patch()` 会把
#    `CR.check_ports` 换成 stub，之后再从 `CR` 上取就只能取到 stub ——
#    探针第一次跑就是这么把自己的第 5 组用例测成了空转（用的是 stub，
#    于是"本机没有 ss 应判失败"那条永远看不到真实现）。
REAL_CHECK_PORTS = CR.check_ports

FAILED: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  [{'OK ' if ok else '!! '}] {label}: got={got!r} want={want!r}")
    if not ok:
        FAILED.append(label)


def _ss_listening_ports() -> set[int]:
    """**独立**读一遍本机在听的端口。

    ⚠️ 刻意不复用 `case_round._listen_ports`：那正是被测对象。用它的输出当期望，
    等于拿被测对象的结论当答案（本项目纪律：判据的期望不许取自被测对象自己的标记）。
    这里只是一次极简解析，够用即可。
    """
    import re
    import subprocess

    exe = shutil.which("ss")
    if not exe:
        return set()
    out = subprocess.run(
        [exe, "-ltn"], capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout
    ports: set[int] = set()
    for line in out.splitlines():
        if "LISTEN" not in line:
            continue
        m = re.search(r":(\d+)\s", line)
        if m:
            ports.add(int(m.group(1)))
    return ports


class Sandbox:
    """一个临时仓库。替换掉所有指向真仓库的路径。"""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="case-round-probe-"))
        self.output = self.root / "output"
        self.logs = self.root / "logs"
        self.wit = self.root / "wit"
        self.output.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self._patch()

    def _patch(self) -> None:
        CR.output_dir = lambda: self.output
        CR.log_paths = lambda roles=DEPLOY_ROLES: {
            r: self.logs / f"{r}.log" for r in roles
        }
        for role in DEPLOY_ROLES:
            (self.logs / f"{role}.log").write_bytes(b"started ok\n")
        CR.check_ports = lambda **kw: CR.Check("ports", True, "[stub] 端口都在")

    def fake_spawn(self, *, verdict=True, log_line=None, label=None, close=True,
                   status="completed", extra_label_only=False):
        """造一个假子进程：它会**真的**写台账、写产物、写日志，然后返回。"""
        wit, output, logs = self.wit, self.output, self.logs

        def spawn(case, *, label: str, timeout_s: int, cwd: Path):
            run = open_run(label=label, role="leader", text=case.text, wit_dir=wit)
            if close:
                close_run(run, status=status, iterations=8, wit_dir=wit)
            if verdict:
                (output / "verdict-20261005T000000Z.json").write_bytes(b"{}")
                (output / "verdict-20261005T000000Z.meta.json").write_bytes(b"{}")
            if extra_label_only:
                open_run(label="automation", role="leader", text="boom", wit_dir=wit)
            if log_line:
                with open(logs / "leader.log", "ab") as fh:
                    fh.write(log_line)
            return 0, "[fake] 子进程输出一行", False, 1.5

        if label:
            def wrapped(case, **kw):
                kw["label"] = label
                return spawn(case, **kw)
            CR._spawn_round = wrapped
        else:
            CR._spawn_round = spawn
        return spawn

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def run(**kw) -> CR.CaseRoundResult:
    return CR.run_case_round(wit_dir=SB.wit, role="leader", **kw)


print("=" * 72)
print("0. 闸门与体检的**纯函数**部分（不经 run_case_round）")
print("=" * 72)

SB = Sandbox()

# ---- 忙闸门 ----
print("\n0.1 忙闸门：只有「有开无收 且 进程活着」才算忙")
live = open_run(label="case:c-food-01", role="leader", text="x" * 80, wit_dir=SB.wit)
check("有开无收（本进程活着）⇒ 拦下", CR.busy_gate([live]).ok, False)
closed = close_run(live, status="completed", wit_dir=SB.wit)
check("已收尾 ⇒ 放行", CR.busy_gate([closed]).ok, True)
dead = open_run(label="case:c-food-01", role="leader", text="x" * 80, wit_dir=SB.wit)
import dataclasses  # noqa: E402

check("有开无收但进程已死 ⇒ 放行（那一轮是「被打断」，不是「在跑」）",
      CR.busy_gate([dataclasses.replace(dead, pid=999999999)]).ok, True)

# ---- 端口解析 ----
print("\n0.2 ss 输出解析（纯函数）")
ss_sample = [
    "State  Recv-Q Send-Q      Local Address:Port  Peer Address:PortProcess",
    "LISTEN 0      4096            0.0.0.0:8445       0.0.0.0:*",
    "LISTEN 0      4096               [::]:8450          [::]:*",
    "ESTAB  0      0            10.0.0.1:22        10.0.0.2:51234",
]
check("只认 LISTEN 行", CR._listen_ports(ss_sample), {8445, 8450})

# ---- 凭据增量 ----
print("\n0.3 凭据体检：只读增量、只认嫌疑行")
log = SB.logs / "leader.log"
base = CR.snapshot_logs({"leader": log})
check("基线取字节数", base["leader"], 11)
with open(log, "ab") as fh:
    fh.write(b"normal line\n")
check("正常新增 ⇒ 通过", CR.check_credentials(base, paths={"leader": log}).ok, True)
check("清点的是**增量**字节（不是整篇）",
      f"{len(b'normal line\n')} 字节" in CR.check_credentials(base, paths={"leader": log}).detail,
      True)
with open(log, "ab") as fh:
    fh.write(b"Unhandled: Unauthorized from registry\n")
c = CR.check_credentials(base, paths={"leader": log})
check("Unauthorized ⇒ 失败", c.ok, False)
check("原因串点名是哪一行", "Unauthorized" in c.detail, True)
check("裸 401（耗时数字）**不**误报",
      CR._CRED_RE.search("duration_ms: 401, ok"), None)
check("带上下文的 401 会命中",
      bool(CR._CRED_RE.search("HTTP 401 from registry")), True)
with open(log, "rb") as fh:
    data = fh.read()
with open(log, "wb") as fh:  # 模拟 logrotate copytruncate（原地截断 ⇒ 变小）
    fh.write(data[:5])
c = CR.check_credentials(base, paths={"leader": log})
check("日志变小（被轮转）⇒ **判不了**，不是「干净」", c.ok, False)
check("原因串明说「无法判定」并带上最可能的成因",
      "无法判定" in c.detail and "logrotate" in c.detail and "11B → 5B" in c.detail, True)
check("一条日志都不存在时明说「什么都没查」",
      "什么都没查" in CR.check_credentials(
          {"leader": -1, "debate-pro": -1},
          paths={"leader": SB.logs / "nope.log", "debate-pro": SB.logs / "nope2.log"},
      ).detail, True)

# ---- 体检 1：台账 ----
print("\n0.4 台账体检")
case = CL.CASES[0]

def _runs(records):
    return records

r_ok = close_run(open_run(label="case:c-food-01", role="leader", text="t", wit_dir=SB.wit),
                 status="completed", iterations=8, wit_dir=SB.wit)
check("新增且收尾 ⇒ 通过", CR.check_ledger([], [r_ok], case).ok, True)
check("没有新增 ⇒ 失败", CR.check_ledger([r_ok], [r_ok], case).ok, False)
check("标签写错 ⇒ 失败",
      CR.check_ledger([], [r_ok], CL.CASES[1]).ok, False)
r_open = open_run(label="case:c-food-01", role="leader", text="t", wit_dir=SB.wit)
check("有开无收 ⇒ 失败", CR.check_ledger([], [r_open], case).ok, False)
r_raise = close_run(open_run(label="case:c-food-01", role="leader", text="t", wit_dir=SB.wit),
                    status="raised:TimeoutError", wit_dir=SB.wit)
check("raised:* ⇒ 失败", CR.check_ledger([], [r_raise], case).ok, False)
check("新增两条 ⇒ 失败", CR.check_ledger([], [r_ok, r_raise], case).ok, False)

# ---- 体检 2：产物 ----
print("\n0.5 产物体检：硬判据只有 verdict，且要排掉 sidecar")
empty = CR.ArtifactSnapshot(frozenset(), frozenset(), frozenset())
only_sidecar = CR.ArtifactSnapshot(frozenset({"verdict-x.meta.json"}), frozenset(), frozenset())
check("没 verdict ⇒ 失败", CR.check_artifacts(empty, empty).ok, False)
check("只有 sidecar 也不算 ⇒ 失败", CR.check_artifacts(empty, only_sidecar).ok, False)
# 真往盘上写一对（verdict + 它的 sidecar），再看 `_artifacts` 滤出来什么。
(SB.output / "verdict-z.json").write_bytes(b"{}")
(SB.output / "verdict-z.meta.json").write_bytes(b"{}")
check("`_artifacts` 会在造快照那层就剔掉 sidecar",
      CR._artifacts(SB.output, "verdict-"), frozenset({"verdict-z.json"}))
with_v = CR.ArtifactSnapshot(frozenset({"verdict-x.json"}), frozenset(), frozenset())
check("有 verdict ⇒ 通过", CR.check_artifacts(empty, with_v).ok, True)
with_r = CR.ArtifactSnapshot(
    frozenset({"verdict-x.json"}), frozenset({"ruling-y.json"}), frozenset()
)
c = CR.check_artifacts(empty, with_r)
check("只有 ruling 新增时 verdict 也算新增 ⇒ 通过（ruling 不是判据）", c.ok, True)
check("新增数按**差值**算，不是总量", "ruling 1 份" in c.detail, True)
c = CR.check_artifacts(with_v, with_r)
check("verdict 没有新增 ⇒ 失败（哪怕 ruling 新增了）", c.ok, False)

print()
print("=" * 72)
print("1~5. 端到端：跑一轮（假子进程）")
print("=" * 72)

# ---- 1 全过 ----
# ⚠️ 换一个**干净**的沙箱：上面 0.1 留了一条「有开无收」的轮次，那是**故意**造出来
#    测忙闸门的；不换沙箱的话它会把这一组用例也拦成 skipped（探针第一次跑就是这样）。
print("\n1. 一切正常 ⇒ exit 0，四项体检全过")
SB = Sandbox(); SB.fake_spawn(verdict=True, log_line=b"normal work\n")
r = run()
check("outcome", r.outcome, "ran")
check("exit_code", r.exit_code, CR.EXIT_OK)
check("四项体检都过",
      sorted(c.name for c in r.checks if c.ok),
      ["artifacts", "credentials", "ledger", "ports"])
check("run_id 填上了", bool(r.run_id), True)
check("label 就是 case:<id>", r.label, "case:c-food-01")

# ---- 1b 落专项记录 ----
print("\n1b. 专项记录：一轮一条、只增不改")
target = CR.rounds_ledger_path(SB.wit, "leader")
CR._persist(r, target)
CR._persist(r, target)
lines = [json.loads(x) for x in target.read_text(encoding="utf-8").splitlines()]
check("追加了两行", len(lines), 2)
check("每行都是一条完整 JSON", all(v["outcome"] == "ran" for v in lines), True)
check("记了四项体检的结论", len(lines[0]["checks"]), 4)
check("两个闸门的读数都记下来了（事后可查「那天为什么没跑」）", len(lines[0]["gates"]), 2)
check("没有 CR 换行符污染", target.read_bytes().count(b"\r"), 0)

# ---- 2 没有产物 ----
print("\n2. 子进程跑了、但没交 verdict ⇒ exit 1 且点名 artifacts")
SB = Sandbox(); SB.fake_spawn(verdict=False, log_line=b"normal\n")
r = run()
check("outcome", r.outcome, "ran")
check("exit_code", r.exit_code, CR.EXIT_CHECKS_FAILED)
check("失败项", [c.name for c in r.checks if not c.ok], ["artifacts"])

# ---- 3 忙闸门 ----
print("\n3. 忙闸门 ⇒ skipped / exit 2，且**没有起子进程**")
SB = Sandbox()
open_run(label="case:c-food-01", role="leader", text="x" * 80, wit_dir=SB.wit)
spawned = {"n": 0}
orig = SB.fake_spawn()
def counting(case, **kw):
    spawned["n"] += 1
    return orig(case, **kw)
CR._spawn_round = counting
r = run()
check("outcome", r.outcome, "skipped")
check("exit_code", r.exit_code, CR.EXIT_SKIPPED)
check("真的没起子进程", spawned["n"], 0)
check("原因指名道姓", "没收敛" in (r.skip_reason or ""), True)
r2 = run(force=True)
check("--force 能绕过", r2.outcome, "ran")

# ---- 4 偏移闸门 ----
print("\n4. 偏移闸门：常驻服务刚起来 ⇒ skipped")
SB = Sandbox(); SB.fake_spawn()
CR.youngest_resident_age_s = lambda roles=DEPLOY_ROLES: (5, "leader", {r: 5 for r in roles}, "假的：都很年轻")
r = run()
check("outcome", r.outcome, "skipped")
check("exit_code", r.exit_code, CR.EXIT_SKIPPED)
check("原因里带上「撞上启动窗口」", "启动窗口" in (r.skip_reason or ""), True)
CR.youngest_resident_age_s = lambda roles=DEPLOY_ROLES: (600, "leader", {r: 600 for r in roles}, "假的：都老了")
r = run()
check("跑够 180s 就放行", r.outcome, "ran")
CR.youngest_resident_age_s = lambda roles=DEPLOY_ROLES: (None, None, {r: None for r in roles}, "假的：量不出来")
r = run()
check("量不出来 ⇒ **不拦**（但要报出为什么）", r.outcome, "ran")
check("报出了原因", "无法判定常驻服务年龄" in CR.offset_gate().detail, True)

# ---- 5 端口 ----
print("\n5. 端口体检：走**真**实现（不是 stub）")
SB = Sandbox(); SB.fake_spawn()
CR.check_ports = lambda **kw: REAL_CHECK_PORTS(roles=DEPLOY_ROLES, ss_exe=None)
r = run()
c = next(c for c in r.checks if c.name == "ports")
print(f"      本机实测：ok={c.ok} detail={c.detail[:130]}")
if shutil.which("ss") is None:
    check("本机没有 ss ⇒ 判失败（不是跳过）", c.ok, False)
    check("明说找不到 ss", "找不到" in c.detail, True)
else:
    # ⚠️ 这里**不能**硬写"应当失败"。2026-10-05 服务器复跑实测：同一条判据在两个平台上
    #    期望**相反** —— 开发机上 8 个常驻端口大概率没在听（应失败），
    #    而服务器上它们**真在听**（应通过）。硬写 False 会把**正确行为**报成不符合预期，
    #    正是本仓那条「判据可能是平台相关的 —— 不门控就会把正确行为报成空转」。
    # ⇒ 改成"只差一个变量"的一对用例，且期望值**现场从 ss 独立读出**（`_ss_listening_ports`），
    #    不取自 `check_ports` 自己的结论。
    listening = _ss_listening_ports()
    if not listening:
        print("      本机一个监听端口都没有 ⇒ 这一对用例无从构造（**跳过不算通过**）")
    else:
        up = sorted(listening)[0]
        dead = next(p for p in range(65000, 65536) if p not in listening)
        real = CR.PORTS
        try:
            CR.PORTS = {"__probe-up__": up}
            c_up = REAL_CHECK_PORTS(roles=("__probe-up__",), ss_exe=None)
            CR.PORTS = {"__probe-down__": dead}
            c_down = REAL_CHECK_PORTS(roles=("__probe-down__",), ss_exe=None)
        finally:
            CR.PORTS = real
        check(f"期望端口真的在听（{up}）⇒ 通过", c_up.ok, True)
        check(f"期望端口没人听（{dead}）⇒ 失败（与上一条只差一个端口）", c_down.ok, False)
        # ⚠️ 钉的是「**必须点名**」，不是某种措辞：本项目纪律 —— 判据不许写死某一种
        #    措辞（否则同一句人话换个写法，判据就红了）。所以查的是端口**号**在不在
        #    那句 detail 里，而不是查"在听"这三个字。
        check("通过时点出了那个端口号", str(up) in c_up.detail, True)
        check("失败时点出了缺的那个端口号", str(dead) in c_down.detail, True)
    # 平台无关的一条：真跑一次也必须点名。期望端口号从**配置**现场取
    # （不经过 `case_round`，免得拿被测对象的输出去当期望）。
    want0 = sorted(PORTS[r] for r in DEPLOY_ROLES if r in PORTS)[0]
    check(f"真实现也点出了端口号（{want0}）", str(want0) in c.detail, True)
CR.check_ports = REAL_CHECK_PORTS
check("`_listen_ports` 纯函数：只认 LISTEN、冒号后有数字",
      CR._listen_ports(["LISTEN 0 4096 0.0.0.0:1 0.0.0.0:*", "garbage"]), {1})

# ---- 6 凭据 ----
print("\n6. 凭据体检：日志里混进凭据嫌疑行 ⇒ 失败")
SB = Sandbox(); SB.fake_spawn(log_line=b"Could not validate credentials\n")
r = run()
c = next(c for c in r.checks if c.name == "credentials")
check("凭据项失败", c.ok, False)
check("点名原因", "Could not validate" in c.detail, True)

# ---- 7 台账异常 ----
print("\n7. 台账体检：子进程没写台账 ⇒ 失败")
SB = Sandbox()
def spawn_no_ledger(case, *, label: str, timeout_s: int, cwd: Path):
    return 0, "[fake] 什么都没写", False, 0.1
CR._spawn_round = spawn_no_ledger
r = run()
c = next(c for c in r.checks if c.name == "ledger")
check("台账项失败", c.ok, False)
check("明说「没有新增轮次」", "新增轮次" in c.detail and "没有" in c.detail, True)

print()
print("=" * 72)
if FAILED:
    print(f"❌ {len(FAILED)} 项不符合预期：")
    for f in FAILED:
        print(f"   - {f}")
    raise SystemExit(1)
print("✅ 全部符合预期")
