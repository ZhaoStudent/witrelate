"""元验证跑完后的**残留核对**：逐个锚点确认已回到"新形态"、旧形态不存在、并且是纯 LF。

为什么单独写一个脚本：元验证的输出只能证明它在**跑的时候**把文件复原了，
而它启动时会把源文件读进内存当快照、结束时按快照回写 —— 期间任何外部修改
都会被静默覆盖。所以核对必须发生在**它结束之后**，且要重新读盘。

⚠️ 2026-10-02 复查发现：本脚本原先**只读 `meta_verify_evidence_leader` 的 CASES**
   —— 而真正跑得最久、最容易被超时掐断的那个是 `meta_verify_evidence`（21 个
   用例、约 10 分钟）。也就是说"最容易留下残留的那一个"恰恰**没有**残留核对。
   现在所有元验证模块的 CASES 一起核（同一文件 + 同一锚点被多处引用时去重）。

⚠️ 另一条：本脚本核对的是**锚点定位**（新形态在不在、旧形态有没有回来、换行符形态），
   **不核对"有没有别的意外改动"** —— 那件事只有全树 md5 diff 能做
   （`python -m tools.tree_diff`）。两者互补，别拿一个当另一个用。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from run import meta_verify_audio as MVA  # noqa: E402
from run import meta_verify_audio_mcp as MVAM  # noqa: E402
from run import meta_verify_calibration as MVC  # noqa: E402
from run import meta_verify_case_round as MVCR  # noqa: E402
from run import meta_verify_chain_payload as MVCP  # noqa: E402
from run import meta_verify_debate_ruling as MVDR  # noqa: E402
from run import meta_verify_evidence as MVE  # noqa: E402
from run import meta_verify_evidence_leader as MVEL  # noqa: E402
from run import meta_verify_fact_store as MVFS  # noqa: E402
from run import meta_verify_fast_lane as MVFL  # noqa: E402
from run import meta_verify_fetch_facts as MVFF  # noqa: E402
from run import meta_verify_human_debate as MVHD  # noqa: E402
from run import meta_verify_law_advisor as MVLA  # noqa: E402
from run import meta_verify_lead_intake as MVLI  # noqa: E402
from run import meta_verify_lead_recon as MVLR  # noqa: E402
from run import meta_verify_leader_name as MVLM  # noqa: E402
from run import meta_verify_negative_controls as MVNC  # noqa: E402
from run import meta_verify_run_ledger as MVRL  # noqa: E402
from run import meta_verify_ruling_store as MVRS  # noqa: E402
from run import meta_verify_trace as MVT  # noqa: E402
from run import meta_verify_video as MVV  # noqa: E402

#: (来源, CASES)。加新的元验证模块时**必须**在这里登记一行 ——
#: 否则它的残留没人核，而"没人核"在外表上与"核过了、没残留"完全一样。
SOURCES = (
    ("meta_verify_audio", MVA.CASES),
    ("meta_verify_audio_mcp", MVAM.CASES),
    # ⚠️ 2026-10-05 补登记：`meta_verify_case_round`（#27 的 13 条口径）最初写成
    #    "函数式退回 + 4 元组 CASES"，与本表要求的 `{file, new, old}` 不兼容 ⇒
    #    它的 13 个锚点**从来没有被本脚本看过**，而 129 处"全部通过"看起来一切正常。
    #    ⇒ 新模块**必须先对齐 `CASES` 的字段形态**，再在这里登记一行。
    # ⚠️ 判定校准台账（2026-10-07 新增）：仪表盘最危险的失败方式不是报错，
    #    是**安静地给出误导性的数字** —— 值域缺口不报 ⇒ 一致率低全背在模型
    #    头上；弃权与判错合并 ⇒ 一致率变成说不清的数字。不登记，它的残留
    #    没人核，而「没人核」与「核过了」长得一模一样。
    ("meta_verify_calibration", MVC.CASES),
    ("meta_verify_case_round", MVCR.CASES),
    # ⚠️ 跨进程委托链（2026-10-13 新增，#123 收尾）：三锚对应手册 §12.18
    #    「定点复现三条」的**持久化**——此前那三条是一次性人肉验证，改坏了
    #    这三处中任何一处都不会有东西变红，只能等真机事故来发现。
    #    A 锚正是 #123 的洞本身（入站不写 metadata ⇒ 跨进程环挡不住）。
    ("meta_verify_chain_payload", MVCP.CASES),
    # ⚠️ 2026-10-07 补登记：辩论裁定（阶段 2）与人机辩论两个模块当日写成时
    #    **都忘了在这里登记** —— 8 个锚点的残留从没人核，而"没人核"与
    #    "核过了没残留"在外表上完全一样（正是本文件开头警告的那条老账）。
    ("meta_verify_debate_ruling", MVDR.CASES),
    ("meta_verify_evidence", MVE.CASES),
    ("meta_verify_evidence_leader", MVEL.CASES),
    # ⚠️ 事实库（2026-10-07 新增）：检索判据**连着废了三版**都还能跑通、
    #    都返回非空、check_local 都会绿 ⇒ 不登记进这张表，**它退回旧形态后
    #    留下的痕迹就没人核**，而"没人核"与"核过了没问题"长得一模一样。
    ("meta_verify_fact_store", MVFS.CASES),
    # ⚠️ 快车道分级（2026-10-12 新增，⑦-10）：资格规则/落盘打标这类判据的
    #    失败方式同样是**安静放行**（全开资格照样出裁定、不打标照样落盘）。
    ("meta_verify_fast_lane", MVFL.CASES),
    # ⚠️ 离线抓取器（2026-10-07 新增）：它是本项目**唯一的出网面**。
    #    安全判据最容易变成摆设 —— 功能照样跑得好好的，没有任何东西会变红
    #    （白名单里塞进 localhost，抓列表页照样返回 78 条）。
    ("meta_verify_fetch_facts", MVFF.CASES),
    ("meta_verify_human_debate", MVHD.CASES),
    # ⚠️ 法条库与法律顾问（2026-10-07 新增，#law-advisor）：防幻觉的**双闸门**
    #    （检索侧同义扩展 / 提交侧逐条硬校验）最危险的失败方式不是报错，是
    #    **安静地放行** —— 阈值空转照样返回 5 条、校验只记不拦照样 status=ok。
    #    不登记，退回形态留下的残留就没人核。
    ("meta_verify_law_advisor", MVLA.CASES),
    # ⚠️ 线索受理官（2026-10-11 新增，⑦-9）：2026-10-12 复查发现本表
    #    **漏登记了它**——七条退回用例跑完后的残留此前一直没人核，而
    #    「没人核」与「核过了没残留」长得一模一样（正是本表存在的原因）。
    ("meta_verify_lead_intake", MVLI.CASES),
    # ⚠️ 线索复研判（2026-10-13 新增，⑦-11）：三处锚点都是当天首跑
    #    check_local 时**逐个暴露的真缺陷**（旧 epoch 复活 / RulingVerdict
    #    缺导入 / 只定义没登记）。B 锚尤其要紧：缺导入这种病藏在
    #    「装了但没跑过」的位置，除了退回形态没有任何东西会红。
    ("meta_verify_lead_recon", MVLR.CASES),
    # ⚠️ 系统性负控集（2026-10-13 新增，⑦-12，#102 拍板）：A/C 两锚同在
    #    official_confirms 函数体上、old 不同——按本表去重口径各核一次，
    #    防的旧形态不同（无窗口对齐 / 对齐恒否），正是想要的。
    ("meta_verify_negative_controls", MVNC.CASES),
    ("meta_verify_leader_name", MVLM.CASES),
    ("meta_verify_run_ledger", MVRL.CASES),
    # ⚠️ 裁定经验库（2026-10-07 新增）：它是「裁判从既往裁定里增强判断力」的落点，
    #    而这个模块**最容易安静地一直错下去** —— 拿裁定自己当基准 ⇒ 一致率恒为
    #    100%；`graded==0` 返回 0.0 ⇒ 报表写着"准确率 0%"而其实一条可比数据都没有。
    ("meta_verify_ruling_store", MVRS.CASES),
    ("meta_verify_trace", MVT.CASES),
    # ⚠️ 视频这条链的元验证是**一份覆盖两层**（检测层 + MCP 层）——
    #    见 `meta_verify_video.py` 开头那段"为什么可以合成一份"。
    ("meta_verify_video", MVV.CASES),
)

bad: list[str] = []
#: 去重键 = (文件, 新形态, **旧形态**)。
#: ⚠️ 原先只按 (文件, 新形态) 去重 —— 那是**判据缺口**：同一处实现被多条用例
#:    引用、而各自的 `old` 不同时，只有**第一条**的 `old` 会被核对，剩下的
#:    等于"没核对"却照样出现在清单里。2026-10-04 干跑审计实测：
#:    `server.py` 的 `tools/call` 那一行同时被 `meta_verify_audio_mcp` 与
#:    `meta_verify_video` 的 A3 引用，两条 `old` 不同 ⇒ A3 的旧形态从来没被本脚本看过。
#:    加上 `old` 之后，同一个物理锚点会因为"要防的旧形态不同"而各查一次 ——
#:    这正是想要的行为（**没人核**与**核过了**从外表看是一样的，这是本项目的老账）。
seen: set[tuple[str, str, str]] = set()
checked_files: set[Path] = set()

for source, cases in SOURCES:
    for c in cases:
        path: Path = c["file"]  # type: ignore[assignment]
        key = (str(path), str(c["new"]), str(c["old"]))
        if key in seen:
            continue
        seen.add(key)
        checked_files.add(path)
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        crlf = raw.count(b"\r\n")
        n_new = text.count(str(c["new"]))
        n_old = text.count(str(c["old"]))
        ok = n_new == 1 and n_old == 0 and crlf == 0
        print(
            f"  [{'通过' if ok else '失败'}] {path.name:20s} "
            f"新形态×{n_new} 旧形态×{n_old} CRLF={crlf}  [{source}] {c['name']}"
        )
        if not ok:
            bad.append(f"[{source}] {c['name']}")

print()
print(f"共核对 {len(seen)} 处锚点、覆盖 {len(checked_files)} 个文件：")
for p in sorted(checked_files):
    print(f"  · {p.relative_to(ROOT)}")
print()

if bad:
    print(f"有 {len(bad)} 处残留：")
    for item in bad:
        print(f"  · {item}")
    raise SystemExit(1)
print(f"全部通过（{len(seen)} 处锚点均已复原，且均为纯 LF）")
