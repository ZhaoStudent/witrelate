"""元验证：本轮（`evidence-leader`，2026-10-02）新增的断言**不是空转的**。

做法与前几个元验证一致：把每处关键实现**退回旧形态**，要求对应断言立刻报失败。

⚠️ 与别的元验证有一处不同：本轮的断言**分散在三个目标里**，所以每条用例都带
   `runner` 字段，指明该退回到哪里去听响：

     · `verify`          → `tools.verify_evidence_leader`（行为 / 契约断言）
     · `check_local`     → `legal_acps.check_local`（结构 / 关系断言）
     · `verify_evidence` → `tools.verify_evidence`（附件通道 / 真链路顺序断言）

   为什么要连 `check_local` 一起元验证：**它此前一条元验证都没有**。
   2c 那一组"主控只认一个入口""预期下游数 = 1""会委派 ⇔ 接管委派"全是
   纯文本 / 纯属性断言 —— 这种断言最典型的失效方式不是报错，而是**探针锚点写错**
   （锚了一个永远不出现的字符串），从此永远"通过"。下面 B1–B4 就是把锚点
   逐个钉住：把文本真的改回旧形态，它必须响。

   为什么要连 `verify_evidence` 一起：C1 那条断言守的是**Hook 链的顺序**，
   而它在 2026-10-02 之前只被"直接调 `wrap_tool_call`"的方式测过 ——
   顺序恰恰是被那种测法绕掉的那一环。

十一处"旧形态"分别对应：

  A1. `SuspicionLevel` 里塞回一个自造档「疑似」
      取值域一旦能容纳模型爱写的词（"疑似""大概率""中等偏上"），汇总层与检测层
      就会各漂各的，而两侧都是自由文本、没有 schema 可校。
      （这一条同时证明：取值域的确只有那四个合法值。）

  A2. `EvidenceRuling._undecidable_needs_reasons` 关掉
      退回"「无法判定」不需要理由" ⇒ **把「没检查」说成「没问题」** 这条路重新打开。
      这是 `evidence-image` 缺陷 1 的代码化，也是本项目最忌讳的一类缺陷。

  A3. `Finding.caveat` 变成可缺省
      退回"线索可以只说异常、不说异常不等于篡改" ⇒ 一条 0.6 强度的噪声残差
      读起来就成了"这图有问题"。

  A4. `write_fallback_verdict` 的"无载荷即不产文件"闸门关掉
      退回"照样拼一份" ⇒ 证据链会得到一份 `harm_index=0 / truth_score=50` 的空壳，
      看起来像"中性结论"，会被当成"链路成立"的证据，污染 `output/` 这个唯一判据。

  A5. `EVIDENCE_LEADER_INSTRUCTIONS` 里那条越层边界声明去掉
      ⚠️ **这一条是文本断言，证据强度有限**：它只能证明"这句话还在提示词里"，
        证明不了模型会照做（后者靠 `DebateChainGuardHooks` 的行为断言）。
        把它列进来的价值是**钉住锚点本身** —— 防止哪天文案改了、
        断言还在断言一个不存在的字符串却继续"通过"。

  A6. `EvidenceRuling._level_needs_effective_findings` 关掉（2026-10-02 复查新增）
      退回之后，**一项都没检查**却给出 `suspicion="低"` 的报告又能通过契约了 ——
      这是 A2 的**镜像**，也是更隐蔽的那一半：A2 管的是"说无法判定却不给理由"，
      A6 管的是"连一句'我没查'都没有，直接给了个让人安心的档位"。
      ⚠️ 判据是"至少一条 `verdict != 无法判定` 的线索"，不是"findings 非空" ——
        整项弃权的检查项**也会**进 findings，所以只判非空的写法**挡不住**
        "全部弃权却报低"。这一条同时钉住了后面那句"与检测层 `summarize()` 同源"。

  B1. 主控提示词重新写上下游专家的注册名
      `leader` 一旦知道「图像鉴伪 Partner」这个名字，就会绕过证据鉴证 Leader
      直接派发 —— 方案 A 全部收益（只认一个入口 + 编排不占主控预算）当场归零，
      而且**不报任何错**。

  B2. `evidence-leader` 的 `expected_partners` 从 1 改成 2
      `_decide` 的判据是 `max(expected, 本轮实际成功派发数)` ⇒ 写成 2 之后
      只有 1 位下游时**永远不触发收尾**，只能等预算耗尽。这类"数字写大了"的
      缺陷运行时只表现为"慢"。

  B3. `EvidenceInboxHooks()` 传成 `delegate_tool=""`
      会委派却不接管委派 ⇒ 模型工具层只认 uri，图片字节**永远到不了下游**：
      下游回一句"没有图可看"，链路看起来跑通了，而证据从未离开本进程。

  B4. `MAX_CHAIN_DEPTH` 从 3 降到 2
      「主控(1) → 证据鉴证 Leader(2) → 图像鉴伪(3)」会被熔断拒绝。
      这条断言的意思不是"现在能跑"，而是"**再往下挂一级就会撞墙**"。

  C1. `EvidenceInboxHooks.priority` 从 100 改成 10
      ⚠️ 10 这个值不是随便挑的 —— 它正是**原来那句注释的字面意思**
         （"取一个比委派 Hook 小的值"，而 `AipDelegationHooks.priority == 50`）。
      取小了之后，接管会在到达 `DebateChainGuardHooks`（也是 50）之前就返回，
      把**身份白名单 / 链深 / 环检测**整体短路掉：discovery 捞到的外部团队 Agent
      可以直接拿到材料字节，断言全绿、链路照跑、边界没了。
      断言落在 `tools/verify_evidence` 第 9 节 ⑥（真链路上的行为断言 +
      priority 关系断言），所以这里要能听见它的响。

⚠️ 四条纪律（沿用 `meta_verify_evidence.py`，本项目反复踩过）：
  1. 退回后的文件**必须仍然语法正确** —— 否则证明的只是"文件坏了会报错"。
  2. 匹配词必须锚在**只出现在失败行**的片段上（这里统一锚在
     `**没有**被契约拒绝` / `[失败]` 的文案上），否则会把"通过行"误判成"报失败"。
  3. **文本一律 `str` 匹配、字节一律 `read_bytes()` / `write_bytes()`**：
     读字节 → 显式 utf-8 解码 → 文本替换 → 显式编码 → 写字节。
     （`write_text()` 在 Windows 上会把 `\n` 翻成 `\r\n`，曾把源文件整篇变成 CRLF。）
  4. **它跑的时候不要动这些源文件。** 本脚本启动时把目标文件读进内存当快照，
     结束（或收到 SIGTERM / SIGINT / SIGHUP）时按**快照**回写 —— 期间任何外部
     修改都会被**静默覆盖**，而输出仍全显示"已复原 ✅"。
     ⚠️ 所以**每次跑完，必须重新 grep 一遍并发改动**，"途中看到还在"不算数。

用法（在 witrelate 目录下）：
    ./.venv/Scripts/python.exe -m run.meta_verify_evidence_leader
"""

from __future__ import annotations

import contextlib
import hashlib
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "legal_acps" / "agents.py"
CONTRACTS = ROOT / "legal_acps" / "contracts.py"
EV_CONTRACTS = ROOT / "legal_acps" / "evidence" / "contracts.py"
EV_HOOKS = ROOT / "legal_acps" / "evidence_hooks.py"


def _evidence_leader_inbox_window() -> str:
    """evidence-leader 的收件 Hook 窗口（**现场派生**，不手抄）。

    ⚠️ 2026-10-03 复查：加视频模态后，主控与 evidence-leader 的收件 Hook 行
    **一字不差**（都是 `kinds=("image", "audio", "video")`）—— 单锚那一行，
    残留核对报「新形态×2」；而就算步骤 0 放行，`replace(new, old)` 只换
    **第一处**，退的也是**主控**、不是本角色 —— 比报红更危险：
    元验证会「通过」，但它证明的是另一行代码的事。
    所以窗口从本角色独有的 `conclusion_tool="evidence_issue_report"` 起，
    连续取到收件 Hook 行为止。中间的注释行不得不一并纳入：两害相权，
    **唯一性优先于「不锚注释」** —— 注释变了会在步骤 0 当场响，
    响比退错人好。
    """
    text = AGENTS.read_bytes().decode("utf-8")
    start_marker = '                conclusion_tool="evidence_issue_report",\n'
    hook_line = '            EvidenceInboxHooks(kinds=("image", "audio", "video")),\n'
    start = text.index(start_marker)
    end = text.index(hook_line, start) + len(hook_line)
    return text[start:end]


_EL_INBOX_WINDOW = _evidence_leader_inbox_window()
_EL_INBOX_OLD = _EL_INBOX_WINDOW.replace(
    'EvidenceInboxHooks(kinds=("image", "audio", "video")),',
    'EvidenceInboxHooks(kinds=("image", "audio"), delegate_tool=""),'
)
assert _EL_INBOX_OLD != _EL_INBOX_WINDOW, "旧形态派生失败：替换没发生"
assert _EL_INBOX_OLD.count("EvidenceInboxHooks") == 1, "旧形态里 Hook 行不止一处"
TERMINATION = ROOT / "legal_acps" / "termination.py"

TARGETS = {
    "verify": "tools.verify_evidence_leader",
    "check_local": "legal_acps.check_local",
    "verify_evidence": "tools.verify_evidence",
}

# 修复前的那段边界声明（= 本轮真正补上的内容）。
# 退回形态刻意写成"只留一句不越层、拿掉 AIP §3.2 那半句"：
# 既语义等价于"没有这条声明"，又不含探针要找的 `不是你的`。
BOUNDARY_NOW = (
    "- 你**不越层**：你只跟自己的 Partner（鉴伪专家）打交道。同一条链路上别家的下游\n"
    "  —— 比如上游主控的「网络裁判」及其辩手 —— **不是你的** Partner：不要去打探它们，\n"
    "  更不要直接派发。反过来，也不要让上游越过你直接去找鉴伪专家。\n"
    "  AIP §3.2 要求 Partner 之间不直接交互 —— 跨层调用既不合规，也会让这份报告\n"
    "  失去独立性（一份由\"委托人自己找人鉴\"的报告，本身就说明不了什么）。\n"
)
BOUNDARY_OLD = "- 你**不越层**：只跟自己的 Partner（鉴伪专家）打交道。\n"

CASES = [
    {
        "name": "综合线索等级的取值域（塞回自造档「疑似」）",
        "file": EV_CONTRACTS,
        "new": 'SuspicionLevel = Literal["低", "中", "高", "无法判定"]',
        "old": 'SuspicionLevel = Literal["低", "中", "高", "无法判定", "疑似"]',
        "needle": "suspicion='疑似' **没有**被契约拒绝",
        "runner": "verify",
    },
    {
        "name": "「无法判定」必须有理由（关掉 ⇒ 把'没检查'说成'没问题'）",
        "file": CONTRACTS,
        "new": '        if self.suspicion == "无法判定" and not self.not_run:\n',
        "old": "        if False:\n",
        "needle": "「无法判定」但 not_run 为空（= 把'没检查'说成'没问题'） **没有**被契约拒绝",
        "runner": "verify",
    },
    {
        # ⚠️ A2 的**镜像**，也是更隐蔽的那一半：连一句"我没查"都没有，
        #    直接给出一个让人安心的档位。判据必须是"至少一条 `verdict != 无法判定`
        #    的线索" —— 只判"findings 非空"挡不住"全部弃权却报低"。
        "name": "非「无法判定」必须至少有一条有效线索（关掉 ⇒ 一项没查也能报「低」）",
        "file": CONTRACTS,
        "new": '        if not any(f.verdict != "无法判定" for f in self.findings):\n',
        "old": "        if False:\n",
        "needle": " findings 为空（= 一项都没检查，却给了个让人安心的档位） **没有**被契约拒绝",
        "runner": "verify",
    },
    {
        "name": "每条线索必须自带 caveat（退回可缺省 ⇒ 只说异常、不说异常不等于篡改）",
        "file": EV_CONTRACTS,
        "new": '    caveat: str = Field(description="**不能证明什么** —— 这一项为什么不足以单独定论")\n',
        "old": '    caveat: str = Field(default="", description="**不能证明什么** —— 这一项为什么不足以单独定论")\n',
        "needle": "缺 caveat 的线索（只说异常、不说异常不等于篡改） **没有**被契约拒绝",
        "runner": "verify",
    },
    {
        "name": "兜底不产空壳（关掉闸门 ⇒ 证据链得到一份无依据的「中性」结论）",
        "file": TERMINATION,
        "new": (
            "    payloads = collect_partner_payloads(messages)\n"
            "    if payloads.empty:\n"
            "        return None\n"
        ),
        "old": (
            "    payloads = collect_partner_payloads(messages)\n"
            "    if False:\n"
            "        return None\n"
        ),
        "needle": "兜底为证据链产出了文件",
        "runner": "verify",
    },
    {
        "name": "提示词里的越层边界声明（去掉 ⇒ 找不到这句就说明锚点在空转）",
        "file": AGENTS,
        "new": BOUNDARY_NOW,
        "old": BOUNDARY_OLD,
        "needle": "提示词没有声明越层调用的边界",
        "runner": "verify",
    },
    {
        "name": "主控不得知道下游专家的注册名（写回去 ⇒ 方案 A 被架空）",
        "file": AGENTS,
        "new": "  ⚠️ 你**不知道**、也不需要知道下游专家注册在平台上的名字 —— 这正是这一层存在的意义。\n",
        "old": "  ⚠️ 下游专家注册在平台上的名字是「图像鉴伪 Partner」。\n",
        "needle": "主控 instructions 里出现了下游专家的注册名",
        "runner": "check_local",
    },
    {
        "name": "预期下游数必须是 1（写大 ⇒ 收尾永不触发，只能等预算耗尽）",
        "file": AGENTS,
        "new": '                partner_label="鉴伪专家",\n                expected_partners=1,\n',
        "old": '                partner_label="鉴伪专家",\n                expected_partners=2,\n',
        "needle": "证据鉴证 Leader 预期下游数应为 1",
        "runner": "check_local",
    },
    {
        # ⚠️ 2026-10-02 复查：锚点原先指向 `EvidenceInboxHooks(),` —— 那是
        #    **法律咨询主控**（`build_leader`，没有 `acps_delegate`）的写法；
        #    而本用例要钉的是 `evidence-leader`（有 `acps_delegate`、必须接管）。
        #    本轮为"两种模态"改造后它成了 `EvidenceInboxHooks(kinds=("image","audio")),`，
        #    锚点正好随之暴露：**锚错了对象**与"锚点过期"在这里长得一模一样。
        # ⚠️ 2026-10-03 复查：加视频模态后主控与本角色的 Hook 行**一字不差**，
        #    单行锚点失效（新形态×2）。窗口改为现场派生（见
        #    `_evidence_leader_inbox_window` 的 docstring），不再手抄。
        "name": "会委派 ⇔ 收件箱接管委派（传空串 ⇒ 字节永远到不了下游）",
        "file": AGENTS,
        "new": _EL_INBOX_WINDOW,
        "old": _EL_INBOX_OLD,
        "needle": "委派能力与收件箱接管不匹配",
        "runner": "check_local",
    },
    {
        "name": "链深上限（降到 2 ⇒ 证据链会被熔断拒绝）",
        "file": CONTRACTS,
        "new": "MAX_CHAIN_DEPTH = 3            # 委托链深度上限（补 wit 未实现的 AAC 约束）",
        "old": "MAX_CHAIN_DEPTH = 2            # 委托链深度上限（补 wit 未实现的 AAC 约束）",
        "needle": "链深上限 2 < 3",
        "runner": "check_local",
    },
    {
        # ⚠️ `10` 正是原来那句注释的字面意思（"取一个比委派 Hook 小的值"，
        #    而 `AipDelegationHooks.priority == 50`）。这是一个**有语法、能跑、
        #    且看起来像"按注释办事"**的值 —— 危险就在这儿。
        "name": "接管 Hook 的 priority 取到闸门之下（⇒ 边界闸门被整体短路）",
        "file": EV_HOOKS,
        "new": "    priority = 100\n",
        "old": "    priority = 10\n",
        "needle": "[未达预期] 接管 Hook 必须在边界闸门",
        "runner": "verify_evidence",
    },
]


def syntax_ok(path: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)], capture_output=True
    ).returncode == 0


def run_target(which: str) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", TARGETS[which]],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def install_restore_guard(originals: dict[Path, bytes]) -> None:
    """收到 SIGTERM / SIGINT / SIGHUP 时也要先把文件还原再退出。

    ⚠️ 没有这一段的话，被工具超时 SIGTERM 掐断时 `finally` 根本不执行
    （Python 的 SIGTERM 默认直接终止，不做栈展开）⇒ 文件就留在"退回旧形态"
    的状态里，而它看起来完全正常、照样能跑。**"看起来能跑"正是它危险的地方。**
    ⚠️ SIGHUP 不是凑数：通过 ssh 在服务器上跑时，连接一断就发 SIGHUP。
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
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # 非主线程 / 平台不支持该信号
            pass


def main() -> int:
    # ⚠️ 行缓冲：被掐断时也必须看得见"跑到第几条了"。
    #    实测（2026-10-02）：默认块缓冲下，`python -m run.xxx | tail` 被强杀时
    #    **一个字都没落盘** —— 于是"中途被杀"与"根本没跑"在日志里长得一模一样，
    #    而文件其实已经留在旧形态。这一条与下面的还原守卫是互补的：
    #    守卫负责把文件改回来，行缓冲负责让"它没跑完"这件事**看得见**。
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
            print(f"[步骤0] ❌ 前置不成立：{c['name']} 不是预期的'新形态'")
            return 1

    # [0b] 前提：两个目标当前都得是绿的 —— 否则"退回后报失败"什么都证明不了，
    #      因为旧形态与现状分不开。（这一步同时是"锚点有效"的前置。）
    for which in ("verify", "check_local", "verify_evidence"):
        code, out = run_target(which)
        green = code == 0 and "[失败]" not in out
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
            _, out = run_target(runner)
            hit = needle in out
            print(f"[{i}.2] {'✅' if hit else '❌'} {name}："
                  f"{TARGETS[runner]} 报出该失败 = {hit}")
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

        # [4] 全部复原后，指纹必须一致，且两个目标整体转绿
        stale = [p for p in originals if digest(p) != befores[p]]
        if stale:
            print(f"[步骤4] ❌ 复原后指纹不一致：{[p.name for p in stale]}")
            return 1
        all_green = True
        for which in ("verify", "check_local", "verify_evidence"):
            code, out = run_target(which)
            green = code == 0 and "[失败]" not in out
            all_green = all_green and green
            print(f"[步骤4] {'✅' if green else '❌'} 复原后 {TARGETS[which]} 转绿 = {green}")
            if not green:
                print(out[-2000:])
        return 0 if all_green else 1
    finally:
        for path, original in originals.items():
            if path.read_bytes() != original:
                path.write_bytes(original)
                print(f"[清理] 已把 {path.name} 恢复为原始内容")


if __name__ == "__main__":
    sys.exit(main())
