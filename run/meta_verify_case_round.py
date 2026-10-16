"""元验证：把「真实案例定期研判」的每条口径各退回旧形态，断言 `check_local` 3d 必须报失败。

⚠️ 为什么必须做这一步
════════════════════
「断言通过了」只能证明**当前实现**符合预期，证明不了**断言本身有牙齿**。
本项目吃过这个亏：heredoc 把 `\\n` 吃掉转义 ⇒ 退回形态实际是语法错误 ⇒
元验证退化成"文件坏掉会报失败"，测的根本不是要测的东西。所以这里：

  1. 退回**一律字节级**（`read_bytes` / `write_bytes`）—— `Path.write_text` 在
     Windows 上会把 `\\n` 翻成 `\\r\\n`，一次往返就把纯 LF 的文件弄脏，
     而内容看起来完全一样。
  2. 退回后的 **.py 必须仍然语法正确**（单元文件不适用），否则元验证无效。
  3. 每条性质都要**真的**造出它要测的那个前提（例：「不剔 sidecar」必须让一份
     `.meta.json` 真的出现在产物集合里）。
  4. 每条变换前先断言锚点**唯一** —— 不唯一的替换会静默改到别处。
  5. 「判据**报红**但没命中期望措辞」与「判据**根本没响**」必须**分开报**：
     前者多半是这里的 needle 写错（判据其实有牙），后者才是空转的判据。
     混成一个"失败"就会把两者误诊成同一个东西 —— 本模块第一版就犯了两次：
     ④ 的 needle 只取了「label 反解」，而那条断言真正响的分支里写的是
     「末尾多一个空格的 label 被当成正常 id」；同时子进程没把返回值翻成退出码，
     于是兜底分支还是**死代码**。（⇒ 见 `classify()` 与 `--selftest`。）

⚠️ 本模块必须**登记进 `run/check_meta_residue.py` 的 `SOURCES`**
════════════════════════════════════════════════════════════
那个脚本靠**写死的登记表**逐个核残留，而「没人核」与「核过了、没残留」在外表上
**完全一样**。所以：
  · `CASES` 的字段名与形态**照抄**既有模块：`{name, why, file, new, old, needle}`，
    且 `new`/`old` 一律是 **str**（`check_meta_residue` 用的是
    `text.count(str(c["new"]))`；写成 bytes 会算成 `"b'...'"` 而**恒不命中**）。
  · `needle` 允许是 str 或 tuple（本模块的 ④ 有两个分支、两条措辞不同）；
    `check_meta_residue` 不读这个字段，所以多形态无妨。
  · **退回形态由 `new`/`old` 一数据两端派生**，不再另写 `rev_*()` 函数：
    否则"锚点写在哪儿"就有了第二份声明，而**两处必然漂移**。
    代价是某些退回形态要迁就"字面替换"这个骨架（见 ⑬ 的注释）。

被测口径（均为 2026-10-05 给 #27 新增的）
────────────────────────────────────────
案例库 · 轮换（`legal_acps/case_library.py`）
  ① 轮换**真的在换**：不是永远取第一个（"永远取第一个"能通过所有形态断言）
  ② 圈数 = `min(每个案子的轮次数)`，不是 0
  ③ 轮次数 = **不同 `run_id` 数**（台账里一轮是开/收两行）
  ④ `label` 反解不宽松：末尾多一个空格**不许**被悄悄修好
执行器 · 四项体检与两个闸门（`legal_acps/case_round.py`）
  ⑤ 台账体检：`label` 写错要判失败
  ⑥ 产物体检：`verdict-*.meta.json`（sidecar）不许冒充裁定书
  ⑦ 凭据判据：裸 `401` 不许命中（那是耗时数字）
  ⑧ 偏移闸门：**量不出来 ⇒ 放行**（拦下会让功能在开发机上永远不跑）
  ⑨ 忙闸门：只有「进程还活着」才算忙（有开无收但进程已死 = 被打断的轮次）
  ⑩ 期望端口不含软废弃角色（含进去 ⇒ 这项体检永远红）
  ⑪ 偏移闸门门槛必须大于最大错开延迟
接线（`deploy/systemd/` 与 `scripts/`）
  ⑫ 单元 `TimeoutStartSec` 必须大于 `case_round.ROUND_TIMEOUT_S`
  ⑬ 单元里写了 `[Install]` 必须被抓到
    —— 这一条同时是"判据不许退化成 grep 源码"的**正对照**：第一版用子串匹配，
       单元自己的注释里写着「刻意不写 `[Install]`」，于是判据被注释骗了。
       现在按行首锚定，注释不会命中，而真写了一行 `[Install]` 必须命中。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "legal_acps" / "case_library.py"
RND = ROOT / "legal_acps" / "case_round.py"
SVC = ROOT / "deploy" / "systemd" / "acps-case-round.service"

#: 在**子进程**里跑 `check_local.check_case()` 并把它的 stdout 原样带回。
#: 用子进程而不是进程内调用：退回的是**源文件**，必须让 Python 重新 import 一次；
#: 进程内 `importlib.reload` 在多模块互相引用时很脆（而且本模块还被
#: `check_local` 在 import 期就抓着一堆类/函数引用）。
#: 只跑 3d 这一节而不是整个 `check_local`：够用、且快得多（整节 ≈1.6s × 15 次）。
#:
#: ⚠️ **必须把 `check_case()` 的返回值翻成退出码**。第一版直接以 `CL.check_case()`
#:    收尾，看上去没问题 —— 但 `check_case()` 只把失败打成 `[失败] …` 后
#:    `return False`，**自己不设退出码**，于是子进程**永远 exit 0**，
#:    兜底分支一次都不会执行：死代码。（实测：手工退回 label 反解后判据确实打了
#:    `[失败] 末尾多一个空格…`，而子进程 exit=0。）
#:    这就是「装了 ≠ 会响」落在**元验证工具自己**身上的样子。
#: 退出码语义：0 = 3d 全绿；1 = 3d **报红**；3 = 判据**崩了**（异常）。
PROGRAM = (
    "import contextlib, io, sys, traceback\n"
    "from legal_acps import check_local as CL\n"
    "buf = io.StringIO()\n"
    "try:\n"
    "    with contextlib.redirect_stdout(buf):\n"
    "        rc = CL.check_case()\n"
    "except BaseException:\n"
    "    sys.stdout.write(buf.getvalue())\n"
    "    traceback.print_exc()\n"
    "    raise SystemExit(3)\n"
    "sys.stdout.write(buf.getvalue())\n"
    "raise SystemExit(0 if rc else 1)\n"
)


def _b(s: str) -> bytes:
    return s.encode("utf-8")


def run_case_section() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", PROGRAM],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def syntax_ok(path: Path) -> bool:
    return (
        subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            cwd=str(ROOT),
            capture_output=True,
        ).returncode
        == 0
    )


def digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _needles(case: dict) -> tuple[str, ...]:
    n = case["needle"]
    return (n,) if isinstance(n, str) else tuple(n)  # type: ignore[arg-type]


def matched_failures(res: subprocess.CompletedProcess, needles: tuple[str, ...]) -> list[str]:
    """只认 `[失败]` 开头的行 —— 通过行的中文文案里本身就可能含这些词。"""
    return [
        ln.strip()
        for ln in res.stdout.splitlines()
        if "[失败]" in ln and any(n in ln for n in needles)
    ]


def classify(
    res: subprocess.CompletedProcess, needles: tuple[str, ...]
) -> tuple[str, list[str]]:
    """把「退回一次之后的子进程输出」归成四种结论之一。

    抽成独立函数是为了让它**可单测**（见 `--selftest`）：退出码与措辞的四种组合
    里，有三种都是"不能算通过"，而它们的原因完全不同 —— 混在一起就会把
    「needle 写错（判据其实有牙）」误诊成「判据没牙」，反过来也一样。
    """
    hit = matched_failures(res, needles)
    if hit:
        return "pass", hit
    if res.returncode == 3:
        tail = (
            res.stderr.strip().splitlines()[-1] if res.stderr.strip() else "（无 traceback）"
        )
        return "crash", [f"判据本身崩了：{tail}"]
    if res.returncode != 0:
        return "offtarget", [
            f"判据报红，但没命中期望措辞 {list(needles)} ⇒ needle 写错？"
            "（响的不是你要证的那件事）"
        ]
    return "silent", ["退回旧形态后判据**全绿** —— 空转的判据"]


#: `classify()` 的单测：四种组合各来一条。**必须**覆盖 `offtarget` 与 `crash`
#: —— 它们正是本次修掉的那两条（第一版是死代码 / 被误诊）。
SELF_TESTS: list[tuple[str, int, str, str, tuple[str, ...], str]] = [
    # (说明, exit, stdout, stderr, needles, 期望 kind)
    ("报红且命中措辞 ⇒ 通过", 1, "  [失败] 圈数算错：应为 1（实得 0）\n", "",
     ("圈数算错",), "pass"),
    ("报红但措辞没命中 ⇒ offtarget（不能算通过）", 1,
     "  [失败] 末尾多一个空格的 label 被当成正常 id\n", "", ("label 反解",), "offtarget"),
    ("判据异常退出 3 ⇒ crash（不能算通过）", 3, "",
     "Traceback (most recent call last):\nValueError: min() arg is an empty sequence\n",
     ("圈数算错",), "crash"),
    ("退回后全绿 ⇒ silent（空转的判据）", 0, "  [通过] 圈数口径\n", "",
     ("圈数算错",), "silent"),
]


def selftest() -> int:
    print("classify() 单测（四种组合必须各自分得开）")
    bad = 0
    got: set[str] = set()
    for note, rc, out, err, needles, want in SELF_TESTS:
        res = subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)
        kind, detail = classify(res, needles)
        got.add(kind)
        if kind != want:
            bad += 1
            print(f"  [失败] {note}：期望 {want}，实得 {kind}")
        else:
            print(f"  [通过] {note}  ->  {kind}：{detail[0][:70]}")
    # 光靠"每条都对"还不够：还得证明四种结论**真的各不相同**
    # （若 classify 退化成常量，上面每条都会错，但万一期望也一起退化了就看不出来）。
    want_kinds = {t[5] for t in SELF_TESTS}
    if got != want_kinds or len(want_kinds) != 4:
        bad += 1
        print(f"  [失败] 四种结论没有分得开：只出现了 {sorted(got)}")
    return 1 if bad else 0


#: ⚠️ 语序：`new` 是**现形态**，`old` 是**退回形态**（与 `check_meta_residue` 一致）。
#: 硬约束（`main()` 步骤 0 逐条实测，不满足就当场退出）：
#:   · `text.count(new) == 1`  —— 锚点必须唯一，否则会静默改到别处；
#:   · `old not in text`       —— 旧形态本来就不该在；
#:   · `new not in old`        —— 否则替换完 `new` 仍在（藏在 `old` 里），
#:                              "替换未按预期发生"这条检查会永远误报。
CASES: list[dict] = [
    {
        "name": "① 轮换退化成「永远取第一个」",
        "why": "「永远取第一个」**能通过所有形态断言**（类型对、有 case、不是 None），"
               "现象是其它 12 个案子永远轮不到，而且**不报错**。",
        "file": LIB,
        # 退回形态只动一半条件：`chosen` 一旦非空就再也不更新 ⇒ 恒为 cases[0]。
        "new": "        if chosen is None or counts[case.id] < counts[chosen.id]:\n",
        "old": "        if chosen is None:\n",
        "needle": "轮换没在换",
    },
    {
        "name": "② 圈数永远报 0",
        "why": "答辩材料上「跑满几圈」会永远显示 0；而轮换照常工作、不报任何错。",
        "file": LIB,
        "new": "    lap = min(counts.values()) if counts else 0\n",
        "old": "    lap = 0\n",
        "needle": "圈数算错",
    },
    {
        "name": "③ 按行数数轮次（不按 run_id 折叠）",
        "why": "台账里一轮是开/收**两行** ⇒ 每一轮都翻倍。它仍然语法正确、仍然能跑完，"
               "只是每个案子都多算一倍 —— 静默的那种错，会让轮换按一个偏大的计数挑案子。",
        "file": LIB,
        "new": (
            "    by_run: dict[str, list[RunRecord]] = {}\n"
            "    order: list[str] = []\n"
            "    for run in runs:\n"
            "        if run.run_id in by_run:\n"
            "            deduped_rows += 1\n"
            "        else:\n"
            "            by_run[run.run_id] = []\n"
            "            order.append(run.run_id)\n"
            "        by_run[run.run_id].append(run)\n"
        ),
        # 退回形态 = 不再归组：`order` 里每一**行**占一格 ⇒ 逐行计数（= 翻倍）。
        "old": (
            "    by_run: dict[str, list[RunRecord]] = {}\n"
            "    order: list[str] = []\n"
            "    for run in runs:\n"
            "        by_run[run.run_id] = [run]\n"
            "        order.append(run.run_id)\n"
        ),
        "needle": "未折叠的台账被判成两轮",
    },
    {
        "name": "④ label 反解宽松化（吞掉末尾空格）",
        "why": "手工改过的台账（多打一个空格）会**静默**少算/错算一轮。"
               "⚠️ 这条断言在判据里**有两条分支**、两种措辞："
               "正/负对照不过时写「label 反解」，末尾空格被悄悄修好时写"
               "「末尾多一个空格的 label 被当成正常 id」。两条都列进 needle。",
        "file": LIB,
        # ⚠️ 锚点**必须带上后一行**：只锚那一行的话，`new` 会变成 `old` 的子串
        #    （`…:]` 是 `…:].strip()` 的前缀），替换完 `new` 仍在。
        "new": (
            "    case_id = label[len(CASE_ID_PREFIX) :]\n"
            '    if not case_id or ":" in case_id:\n'
        ),
        "old": (
            "    case_id = label[len(CASE_ID_PREFIX) :].strip()\n"
            '    if not case_id or ":" in case_id:\n'
        ),
        "needle": ("label 反解", "末尾多一个空格"),
    },
    {
        "name": "⑤ 台账体检不看 label",
        "why": "只要求非空 ⇒ 一轮用**别的标签**写进台账也算「本轮成功」，而轮换从此"
               "永远认不出它（静默少算一轮）。",
        "file": RND,
        "new": "    if run.label != want:\n",
        "old": "    if not run.label:\n",
        "needle": "台账体检分不开",
    },
    {
        "name": "⑥ 产物体检不剔 sidecar",
        "why": "一份几百字节的 `verdict-x.meta.json` 就能冒充裁定书 ⇒ "
               "「有产物」这条唯一的硬判据被旁路证据满足。",
        "file": RND,
        "new": "    fresh_verdict = sorted(_drop_sidecars(after.verdict)"
               " - _drop_sidecars(before.verdict))\n",
        "old": "    fresh_verdict = sorted(after.verdict - before.verdict)\n",
        "needle": "产物体检分不开",
    },
    {
        "name": "⑦ 凭据判据用裸 401",
        "why": "正常日志里的耗时数字 `duration_ms: 401` 会被报成凭据事故 —— "
               "这正是「永远在响的警报 = 没有警报」的来源。",
        "file": RND,
        "new": '    (r"(?:HTTP|status|code)\\D{0,8}401", "带上下文的 401'
               '（裸 401 会误报耗时数字）"),\n',
        "old": '    (r"401", "带上下文的 401（裸 401 会误报耗时数字）"),\n',
        "needle": "凭据判据分不开",
    },
    {
        "name": "⑧ 偏移闸门「量不出来」时拦下",
        "why": "改的是**方向**（True→False），不是措辞 —— 「量不出来」与「刚起来」"
               "必须分开对待：开发机上根本量不出来，拦下等于功能静默永不运行。",
        "file": RND,
        # ⚠️ 锚点**截到 `f"` 为止**，不带后面那句中文：措辞将来改了，
        #    这条前置检查不该跟着坏（要证的是"方向"，不是"那句话怎么写"）。
        "new": '    if age is None or who is None:\n        return Gate("offset", True, f"',
        "old": '    if age is None or who is None:\n        return Gate("offset", False, f"',
        "needle": "偏移闸门分不开",
    },
    {
        "name": "⑨ 忙闸门不判进程存活",
        "why": "一条**已被 kill** 的轮次会永久挡住后续所有轮次（有开无收 ≠ 在跑）。",
        "file": RND,
        "new": "    live = [r for r in runs if r.unfinished"
               " and pid_alive(r.pid) is not False]\n",
        "old": "    live = [r for r in runs if r.unfinished]\n",
        "needle": "忙闸门分不开",
    },
    {
        "name": "⑩ 期望端口含软废弃",
        "why": "软废弃角色（8443/8444）本地根本不部署 ⇒ 含进去这项体检**永远红**，"
               "而「永远在响」就等于没有警报。",
        "file": RND,
        "new": "    return sorted(PORTS[r] for r in roles if r in PORTS)\n",
        "old": "    return sorted(PORTS.values())\n",
        "needle": "期望端口集合不对",
    },
    {
        "name": "⑪ 偏移闸门门槛小于错开延迟",
        "why": "门槛必须大于最大错开延迟（108s），否则「等常驻服务稳定」变成一句空话"
               "—— 它可能在最后一个服务还没登录完 Registry 的时候就放行。",
        "file": RND,
        "new": "OFFSET_MIN_S = 180\n",
        "old": "OFFSET_MIN_S = 30\n",
        "needle": "偏移闸门门槛",
    },
    {
        "name": "⑫ 单元启动超时小于子进程上限",
        "why": "systemd 会先动手把一整轮研判切断，而日志只会写「服务启动超时」"
               "—— 一个完全不指向真因的措辞。",
        "file": SVC,
        "new": "TimeoutStartSec=1200\n",
        "old": "TimeoutStartSec=60\n",
        "needle": "TimeoutStartSec",
    },
    {
        "name": "⑬ 单元补上 [Install] 段（正对照）",
        "why": "写了 `[Install]` 就可能被 `systemctl enable` 成「开机跑一轮」，"
               "而开机正是常驻服务刚起来、最不该登录 Registry 的时刻。"
               "⚠️ 这一条同时是「判据不许退化成 grep 源码」的**正对照**：第一版判据写的是"
               '`"[Install]" in body`，而单元自己的注释里就写着「刻意不写 `[Install]`」'
               "—— 于是**没问题的时候判据报失败**（被自己的注释骗了）。"
               "现在按行首锚定：注释不命中，而这里真补上的 `[Install]` 必须命中。",
        "file": SVC,
        # ⚠️ 退回形态用"字面替换"实现（本套骨架的契约是 `new` → `old`）：
        #    拿掉最后那行注释、换成真的 `[Install]` 段。删一行注释对这条判据毫无影响
        #    —— 要证的是"单元里出现真 `[Install]` 必须被抓到"。
        #    不能写成"在文件末尾追加"：那样 `new` 会变成 `old` 的前缀，骨架的
        #    "替换未按预期发生"检查会永远误报（见 CASES 上方的硬约束）。
        "new": '#    顶掉别人会话"风险最高的时刻。少一个可以被误用的入口，就少一次事故。\n',
        "old": "[Install]\nWantedBy=multi-user.target\n",
        "needle": "单元写了 [Install]",
    },
    {
        "name": "⑭ 材料分母写死成 13（而不是现场派生 `len(CASES)`）",
        "why": "写死的处数必然与库分家。⚠️ 这条判据**必须改一次库长度才分得开**："
               "今天 `len(CASES)` 恰好就是 13，所以写死的与派生的**印出来一模一样** —— "
               "不换长度的话它是空转的。加一条案例之后，材料会印出 `14 / 13` 这种一眼错的"
               "数字，而**没有任何东西会报错**。",
        "file": RND,
        "new": '    out.append(f"| 覆盖到的案例 | {len(cases)} / {len(case_library.CASES)} |")\n',
        "old": '    out.append(f"| 覆盖到的案例 | {len(cases)} / 13 |")\n',
        "needle": "分母没有现场派生",
    },
    {
        "name": "⑮ 空体检结果被 `all([])` 当成「四项体检全过」",
        "why": "`all([])` 是 `True` ⇒ 一条 `outcome=ran` 但**没有任何体检结果**的记录会被"
               "报成「四项体检全过」，而**同一份材料**的逐轮明细里那一格写的是 `—`"
               "（那里注明 `—` = 没跑）。两处自相矛盾，且方向是**假通过** —— "
               "唯一硬判据被一句空断言顶替掉了。",
        "file": RND,
        "new": (
            '    checks = row.get("checks") or []\n'
            '    return bool(checks) and all(bool(c.get("ok")) for c in checks)\n'
        ),
        # 退回形态 = 去掉那个 `bool(checks) and`。
        "old": (
            '    checks = row.get("checks") or []\n'
            '    return all(c.get("ok") for c in checks)\n'
        ),
        "needle": "空体检结果被算成了",
    },
    {
        "name": "⑯ 「连续跑通 N 轮」用**累计**通过数冒充连续数",
        "why": "输入「过 / 失败 / 过」会印出「连续跑通 **2** 轮」，而那两个通过的轮次"
               "中间隔着一个失败的 —— 材料是给评审看的，「连续 N 天跑通」这句话"
               "必须经得起逐行核对。**累计数与连续数是两个数**，混起来就是在材料上写假话。",
        "file": RND,
        "new": '            f"> ✅ **最近连续跑通 {streak} 轮**"\n',
        "old": '            f"> ✅ **最近连续跑通 {len(passed)} 轮**"\n',
        "needle": "与真实的末端连续段不符",
    },
    {
        "name": "⑰ 「当场结论」与「事后材料」各写一份通过判定",
        "why": "两处各写一遍必然漂移，而这条判定**直接决定材料上写「跑通了几轮」**。"
               "退回成「当场那份单独算」之后，`CaseRoundResult.ok` 与 `round_passed` "
               "对「空体检结果」这一种输入给出**相反**的结论。",
        "file": RND,
        "new": "        return round_passed(self.to_json())\n",
        "old": '        return self.outcome == "ran" and all(c.ok for c in self.checks)\n',
        "needle": "不是同一个通过判定",
    },
    {
        "name": "⑱ 「覆盖到的案例」把**没跑**的轮次也算成覆盖",
        "why": "被闸门拦下的轮次也记着 `case_id`，但它**没往发起轮台账写任何东西** ⇒ "
               "轮换算这个案子 0 轮、下次还会再挑它一次 ⇒ 这个案子**根本没被覆盖**。"
               "算进去就是在给评审的材料里虚报覆盖面，而且与紧邻的「真跑了」"
               "「被闸门拦下（没跑，≠ 通过）」两行**自相矛盾**"
               "（实测曾出现「真跑了 0」与「覆盖到的案例 1 / 13」并存）。",
        "file": RND,
        "new": (
            "    cases = sorted(\n"
            '        {str(r.get("case_id")) for r in rows\n'
            '         if r.get("outcome") == "ran" and r.get("case_id")}\n'
            "    )\n"
        ),
        "old": '    cases = sorted({str(r.get("case_id")) for r in rows if r.get("case_id")})\n',
        "needle": "把**没跑**的轮次也算进去了",
    },
]


def main() -> int:
    originals = {c["file"]: c["file"].read_bytes() for c in CASES}
    before = {p: digest(p) for p in originals}
    was = {p: b for p, b in originals.items()}

    print("=" * 78)
    print(f"元验证：真实案例定期研判（#27）{len(CASES)} 条口径，各退回旧形态后"
          "断言 check_local 3d **必须**报失败")
    print("=" * 78)

    # 前置检查：报告器（`classify`）本身若不可信，下面 13 条"通过"就没有意义。
    # 便宜、快、且不需要改动任何源文件 —— 放在长跑之前。
    if selftest() != 0:
        print()
        print("  [失败] classify() 单测不过 ⇒ 报告器本身不可信，先修它再谈元验证")
        return 1
    print()

    # [0] 前提：每一处都必须是"新形态"，旧形态一处都不在，且 `new` 不是 `old`
    #     的子串（否则替换完 `new` 仍藏在 `old` 里）。
    for c in CASES:
        text = originals[c["file"]].decode("utf-8")
        n_new = text.count(c["new"])
        n_old = text.count(c["old"])
        if n_new != 1 or n_old != 0 or c["new"] in c["old"]:
            print(f"  [失败] 前置不成立：{c['name']} —— 新形态×{n_new}（应为 1）、"
                  f"旧形态×{n_old}（应为 0）、new⊂old={c['new'] in c['old']}（应为 False）")
            return 1
    print(f"  前置：{len(CASES)} 处锚点均为新形态、且都唯一（str 形态，"
          "与 check_meta_residue 的核对口径一致）")
    print()

    # 基线：当前形态必须是绿的，否则下面的"退回后报红"毫无意义
    # （如果它本来就是红的，那每条都会"通过" —— 元验证退化成空转）。
    base = run_case_section()
    base_green = [ln for ln in base.stdout.splitlines() if "[通过] 案例研判" in ln]
    if base.returncode != 0 or not base_green:
        print("  [失败] 基线就不绿 —— 元验证没有意义（先修好再跑）")
        print(f"         exit={base.returncode}")
        for ln in base.stdout.splitlines()[-12:]:
            print(f"         {ln}")
        return 1
    print(f"  基线：check_local 3d 全绿（exit={base.returncode}）")
    print()

    #: (用例名, 结论, 明细)。结论只有四种，且**必须**分开报 ——
    #: 「判据报红但响的不是那句话」与「判据根本没响」是两件事，混成一个"失败"
    #: 就会把 needle 写错（判据其实有牙）误诊成"判据没牙"。
    report: list[tuple[str, str, list[str]]] = []
    try:
        for c in CASES:
            path: Path = c["file"]
            name = str(c["name"])
            original = was[path]
            # [1] 退回旧形态；`new`/`old` 是 str，读写一律走 bytes 保住纯 LF。
            text = original.decode("utf-8")
            broken = text.replace(c["new"], c["old"])
            if broken.count(c["old"]) != 1 or c["new"] in broken:
                print(f"  [失败] {name}：替换未按预期发生，本次元验证无效")
                return 1
            path.write_bytes(_b(broken))
            if path.suffix == ".py" and not syntax_ok(path):
                print(f"  [失败] {name}：退回后文件语法不正确 —— 元验证无效！")
                return 1

            # [2] 断言必须报失败（且是**那条**断言）
            res = run_case_section()
            kind, detail = classify(res, _needles(c))
            report.append((name, kind, detail))

            # [3] 立刻复原（失败路径也走这里 —— 见末尾的 finally 兜底）
            path.write_bytes(original)
    finally:
        # 纪律：无论从哪条路径退出（含异常与提前 return），都必须把原文写回。
        # ⚠️ 本项目踩过这个坑：工具超时 SIGTERM 会把源文件**留在退回形态**。
        for p, blob in was.items():
            if p.read_bytes() != blob:
                p.write_bytes(blob)
                print(f"  [finally] 已强制复原 {p.name}")

    print()
    ok = True
    why = {
        "crash": "判据本身崩了（不是「有牙」）",
        "offtarget": "报红了，但措辞没命中（needle 写错？）",
        "silent": "退回旧形态后断言**没有响**（空转的判据！）",
    }
    for name, kind, detail in report:
        if kind == "pass":
            print(f"  [通过] {name}  ->  退回后报失败 {len(detail)} 项")
            print(f"         {detail[0][:150]}")
        else:
            ok = False
            print(f"  [失败] {name}  ->  {why[kind]}")
            for d in detail[:2]:
                print(f"         {d[:150]}")

    print()
    stray = [p.name for p, h in before.items() if digest(p) != h]
    if stray:
        ok = False
        print(f"  [失败] 复原后 md5 与原文不一致：{stray}")
    else:
        print(f"  [通过] 复原后 {len(before)} 个文件的 md5 与跑前逐字节一致")

    res = run_case_section()
    restored = res.returncode == 0 and bool(
        [ln for ln in res.stdout.splitlines() if "[通过] 案例研判" in ln]
    )
    print(f"  复原后 check_local 3d exit = {res.returncode}  "
          f"（{'全绿' if restored else '仍有失败 —— 复原失败！'}）")
    print("=" * 78)
    return 0 if (ok and restored) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raise SystemExit(main())
