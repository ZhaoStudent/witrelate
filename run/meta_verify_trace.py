"""元验证：结论溯源表（`tools/trace.py`）本轮补上的断言**不是空转的**。

做法沿用前几个元验证：把每处关键实现**退回旧形态**，要求
`tools.verify_trace`（离线断言）**立刻报出对应的那条失败**。

================================ 要钉住的二十一处 =================================
（第一轮 5 处：`_preview` 假值 / verdict 清单 / 定局会话 / gaps 理由 / HTML class；
  第二轮 8 处：请求原文 / 重复跳 / 自指边 / 子串命中 / 命中外壳 / 坏库告警 /
               owner 未知 / 窗口重叠；
  第三轮 7 处：强度图例 / 命中片段 / HTML 混 markdown / 表格竖线 / 时间戳比较 /
               pin 夹取 / **缺 sidecar 告警的星号**。
  后两轮全由 2026-10-03 的 bug hunt 抓出。）
  T1. `_preview()` 退回 `str(text or "")`
      假值（`False` / `0`）被抹成空串 ⇒ `is_incitement=False`（这是**最敏感的
      那个结论**）与 `harm_index=0` 在表里**显示成空白**，读起来像"这一项没有值"。
      这是本项目明令禁止的那类"静默丢数字"。
      ⚠️ 旧形态**必须是 `str(text or "")`**，不能写成 `(text or "")` ——
         `verdict` 里有 `harm_index: -25` 这类**真值非字符串**，`(-25 or "").split()`
         会抛 `AttributeError`。那是**崩溃**，不是"静默抹白"，探针会拿崩溃
         冒充"断言逮到"。第一次写这条探针就是这么错的（实测 exit=1、0 条 [失败]）。

  T2. `_verdict_files()` 退回不过滤 sidecar
      `verdict-<TS>.meta.json` 也以 `.json` 结尾 ⇒ `verdict-*.json` 会把它一起捞进来，
      "取最新一份"取到 sidecar，再去找 `…meta.meta.json` 自然找不到 ⇒ **整局定不出来**。
      ⚠️ 这条的形态特别像本项目那个老毛病：**不报错，只是空**。

  T3. `_resolve_run()` 退回"在**全部角色**的会话里取**最近**的一条"
      ⇒ 认领到 `fact-judge` 的**内层**会话，整条链凭空少一层，而输出**照样像模像样**。
      （这是本项目最怕的「静默给出错误归属」。）

  T4. `gaps` 的 `reason` 退回**空白**
      gaps 这一行还在、字段名也在，唯独"为什么找不到来源"没了 ⇒ 一行没有理由的
      "找不到来源"读起来像噪声，下次就没人看它了。

  T5. HTML 里结论字段行退回**不带 class**
      硬溯源行与无来源行长得一模一样 ⇒ 4 屏页面上"哪些字段有下游原文撑腰"
      这件事**在视觉上消失**（CSS 规则写了但永远不触发）。

  T6. `_text_payload()` 退回**递归收集所有字符串**
      文本项形态是 `{"type":"text","text":"正文"}` ⇒ 递归把 `"type"` 的**值** `"text"`
      也收进来，"请求原文"前面凭空多一行 `text`。看着只是多一行，实质是**假证据**。

  T7. `_build_chain()` 里 `fresh` 退回 `list(cands)`（`seen_sessions` 又成死守卫）
      主控在同一局里对同一下游委派两趟时，同一个内层会话被**每个父各展开一遍** ⇒
      同一条任务被造成两条跳、跨智能体调用次数跟着虚增，而输出看起来完全正常。

  T8. `_build_chain()` 退回不排掉**父跳自己的会话**
      父跳就在内层会话里、时间窗必然"落在自己之内" ⇒ 造出 `task_Jp → …INNER`
      这条毫无含义的**自指边**。

  T9. `_field_span()` 退回 `blob.find(name)`（纯子串）
      `band` 命中 `abandon` ⇒ **假的 `[硬]`**；而 `[硬]` 正是本表全部说服力的来源。

  T10. `_content_blob()` 退回不剔信封
      产物外壳里就有 `name` 键 ⇒ verdict 里叫 `name` / `id` 的字段会"命中"包装纸。

  T11. `_load_all()` 退回 `except Exception: …`（静静吞掉）
      某角色的库坏掉 ⇒ 该角色任务全部消失、链路凭空少一层，而唯一的警告把原因
      指成"**配不到对端的入站行**" —— **方向是错的**，人会被引去查配对。

  T12. `pick_live_session()` 退回 `if not owner or s.owner_aic == owner`
      owner 未知时把 owner 过滤**整个跳过** ⇒ 认领到别的角色的会话，
      返回的原因串却写着"owner 匹配"（**证据串在说谎**）。

  T13. `pick_live_session()` 退回 `max(created_at)`
      两局窗口重叠时"取最新"只是赌注，赌错就把上一局的 session 写进这一局。

  T14～T19 是**渲染层**的六处，全是「说得比做得多」那一类：docstring 声称
  "已经写明 / 已经转义 / 已经夹住"，页面却什么也没做，而页面照样长得正常。
      T14. HTML 里 `硬` 没有强度图例 ⇒ 读者会把它读成"平台级认证"。
      T15. 硬字段不摆命中片段 ⇒ `硬` 是一句无法复核的断言。
      T16. HTML 里写 `**…**` ⇒ 浏览器**原样显示**星号，交付页看着没做完。
      T17. Markdown 单元格不转义 `|` ⇒ 值里一个竖线就把表格拆成两行。
      T18. `min()`/`max()` 直接比 `sent_at` **字符串** ⇒ 字典序当时间序。
           平台眼下统一 `+00:00` 所以恰好对得上，那只是巧合。
      T19. 越界的 `harm_index` ⇒ pin 被甩到色条外，静默暗示一个不存在的档位。
      T20. 告警 / gaps 原因 / 推断边判据里写 `**…**` ⇒ 它们经 `_esc()` **原样**
           进 HTML，交付页上直接显示星号。⚠️ 范围**只圈我们自己写的文案**：
           命中片段是下游产物原文，里面本来就可能带 `**`，那是**证据**，不许动。
      T21. **真机里真的泄漏过的那一条**：缺 sidecar 时的认领告警。
           ⚠️ T20 只退了一处字符串，而实测漏星号的是**另一条告警**；当时用例 N
           踩了五条告警分支却漏了这条 ⇒ 断言对真正回归的分支空转。见 `_SIDECAR_MD_*`。

  T14～T19、T21 都是「说得比做得多」那一类：
  docstring 声称"已经写明 / 已经转义 / 已经夹住"，页面却什么也没做，而页面照样长得正常。

⚠️ 关于 T3，必须写清楚**为什么旧形态要一次退两处**（不然探针是空转的）：
   `tools/aip_sessions.sessions_covering()` 返回前会 `sort(key=created_at)`，
   而 `_resolve_run` 里又写了 `min(cands, key=created_at)` —— 这两处**互为冗余**，
   退掉任何一处都还剩另一处把结果掰回"最外层"，探针**不会响**。
   所以旧形态必须同时退掉"角色限制"与"取最外层"，才等价于当初那个真实缺陷
   （"在全部会话里取最近一局"）。
   ⚠️ 这也是本条探针的**唯一性前提**：`sessions = read_sessions(owner_db)` 与
   `chosen = min(...)` 两行在文件里各只出现一次（步骤 0 会验）。

⚠️ 四条纪律（沿用 `meta_verify_evidence_leader.py`，本项目反复踩过）：
  1. 退回后的文件**必须仍然语法正确** —— 否则证明的只是"文件坏了会报错"。
  2. 匹配词必须锚在**只出现在失败行**的片段上，否则会把"通过行"误判成"报失败"。
  3. **文本一律 `str` 匹配、字节一律 `read_bytes()` / `write_bytes()`**：
     读字节 → 显式 utf-8 解码 → 文本替换 → 显式编码 → 写字节。
     （`write_text()` 在 Windows 上会把 `\n` 翻成 `\r\n`，曾把源文件整篇变成 CRLF。）
  4. **它跑的时候不要动这些源文件。** 本脚本启动时把目标文件读进内存当快照，
     结束（或收到 SIGTERM / SIGINT / SIGHUP）时按**快照**回写 —— 期间任何外部
     修改都会被**静默覆盖**，而输出仍全显示"已复原 ✅"。
     ⚠️ 所以**每次跑完，必须重新 grep 一遍**，"途中看到还在"不算数。
     ⚠️ 跑完还要再跑 `python -m run.check_meta_residue` 与
        `python -m tools.tree_diff diff` —— 前者的锚点表**已登记本模块**。
  5. 🔴 **信号钩子实测不可靠 ⇒ 必须有"落盘备份 + 启动自愈"。**
     2026-10-03 实测：本脚本被工具超时 SIGTERM 掐断后，`tools/trace.py` **停在 T3 的
     旧形态上**（`chosen = max(...)`），信号钩子一声不吭地没生效；而那份源文件
     看起来完全正常、照样能跑 —— 这正是本项目最怕的形态。
     所以现在每处退回**之前**先把原始字节写到 `<file>.metaorig`，复原后删掉；
     下次启动若发现 `.metaorig` 还在，就**先用它自愈并大声报告**。
     这样即使被 SIGKILL（连钩子都跑不了）也不会留残留。

用法（在 witrelate 目录下）：
    ./.venv/Scripts/python.exe -m run.meta_verify_trace
    ⚠️ 本地跑一遍要 9 分钟左右（沙箱里每次 rmdir / executescript 都要 ~0.1s），
       建议后台跑；服务器上是秒级。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACE = ROOT / "tools" / "trace.py"
AIP_SESSIONS = ROOT / "tools" / "aip_sessions.py"

#: 单实例锁。**故意放在系统临时目录、不放进仓库**：它是个运行期产物，
#: 放进 `run/` 会让 `tools/tree_diff.py` 每次 diff 都多出一条"内容总在变"的文件。
#: 作用域正好是"同一台机器"—— 并发只会在同一台机器上互相踩源文件。
LOCK_PATH = Path(tempfile.gettempdir()) / "acps_meta_verify_trace.lock"

TARGETS = {"verify_trace": "tools.verify_trace"}


# ── 锚窗口一律**现场派生**，不手抄 ──────────────────────────────────────────
#
# 手抄多行代码块进 `old` 字段有三个坑：缩进、全角标点、以及将来改了源码而锚点
# 没人更新（那时探针会"退回"到一个已经不存在的位置，静默空转）。现场派生的窗口
# 只要**首尾标记**还在就自动跟上；标记没了就在 import 时当场炸，响比空转好。


def _window(text: str, start_marker: str, end_marker: str) -> str:
    """取 `[start_marker, end_marker]` 闭区间（含首尾）。"""
    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    return text[start:end]


_TRACE_TEXT = TRACE.read_bytes().decode("utf-8")

# ── T3 的窗口：从集合会话那句，一直到"取最外层"那句（两处缺陷都在这段里）──
_RESOLVE_NEW = _window(
    _TRACE_TEXT,
    "    sessions = read_sessions(owner_db)\n",
    "    chosen = min(cands, key=lambda c: (c.created_at, c.session_id))\n",
)
_RESOLVE_OLD = _RESOLVE_NEW.replace(
    # ① 角色限制 → 退成"把全部角色的会话混在一起"
    "    sessions = read_sessions(owner_db)\n",
    "    sessions = [s for r in sorted(by_role)\n"
    "                for s in read_sessions(role_db_path(wit_dir, r))]\n",
).replace(
    # ② 取最外层 → 退成"取最近的一局"（连那句解释"为什么要取最外层"的注释一起拿走）
    "    # 最外层 = 全局那一局。嵌套时内层窗口一定被外层包住，所以 started 最早的那个是外层。\n"
    "    chosen = min(cands, key=lambda c: (c.created_at, c.session_id))\n",
    "    chosen = max(cands, key=lambda c: (c.created_at or stamp, c.session_id))\n",
)
assert _RESOLVE_OLD != _RESOLVE_NEW, "T3 旧形态派生失败：两处替换都没发生"
assert _RESOLVE_OLD.count("    sessions = [s for r in sorted(by_role)\n") == 1, \
    "T3 旧形态里「混全部角色」那句不止一处"
assert "chosen = min(" not in _RESOLVE_OLD, "T3 旧形态里仍留着'取最外层'"

# ── T5 的窗口：结论字段行（含 class 的那两行）──
_ROW_NEW = _window(
    _TRACE_TEXT,
    '            cls = "hard" if f.evidence == "hard" else "gap"\n',
    '              f"<td><code>{_esc(f.field)}</code></td>"\n',
)
_ROW_OLD = _ROW_NEW.replace(
    '            cls = "hard" if f.evidence == "hard" else "gap"\n', ""
).replace(
    "f\"<tr class='{cls}'>\"", '"<tr>"'
)
assert _ROW_OLD != _ROW_NEW, "T5 旧形态派生失败：替换没发生"
assert "cls = " not in _ROW_OLD, "T5 旧形态里仍留着 cls 赋值"
assert "class='{cls}'" not in _ROW_OLD, "T5 旧形态里仍留着 class 插值"


# ══════════════════ 第二轮补的八处（bug hunt 2026-10-03 抓出来的）══════════════════
#
# 口径与上面一致：`new` 一律**现场派生**（源码一改就跟着走，标记没了当场炸）；
# `old` 只能是手写的"曾经的样子"，但它**不许是 `new` 的子串** ——
# 否则步骤 0 的 `n_old == 0` 那条前置会失败（那个检查正是用来证明
# "旧形态确实已经不在文件里"的）。
# ⚠️ 每一处的 `old` 还必须是**非空**且**语法正确**的：空串会让
#    `text.count("")` 返回 `len(text)+1`；语法错误的"倒退"证明不了任何事。

_SESS_TEXT = AIP_SESSIONS.read_bytes().decode("utf-8")

# ── B1：`_text_payload` 退回"递归收集所有字符串" ──
_TEXT_PAYLOAD_NEW = _window(
    _TRACE_TEXT,
    "def _text_payload(items: Any) -> str:\n",
    '    return "\\n".join(parts)\n',
)
_TEXT_PAYLOAD_OLD = (
    "def _text_payload(items: Any) -> str:\n"
    '    """旧形态：递归收集 JSON 里所有字符串（错）。"""\n'
    "    out = []\n"
    "\n"
    "    def walk(x: Any) -> None:\n"
    "        if isinstance(x, str):\n"
    "            if x.strip():\n"
    "                out.append(x)\n"
    "        elif isinstance(x, dict):\n"
    "            for v in x.values():\n"
    "                walk(v)\n"
    "        elif isinstance(x, list):\n"
    "            for v in x:\n"
    "                walk(v)\n"
    "\n"
    "    walk(items)\n"
    '    return "\\n".join(out)\n'
)
assert _TEXT_PAYLOAD_OLD != _TEXT_PAYLOAD_NEW, "B1 旧形态派生失败"
assert _TEXT_PAYLOAD_OLD not in _TRACE_TEXT, "B1 旧形态居然还在文件里"

# ── B3：`_field_span` 退回纯子串匹配（丢掉词边界）──
_FIELD_SPAN_NEW = _window(
    _TRACE_TEXT,
    "    m = re.search(\n",
    "    return (m.start(), m.end()) if m else None\n",
)
_FIELD_SPAN_OLD = (
    "    idx = blob.find(name)\n"
    "    return (idx, idx + len(name)) if idx >= 0 else None\n"
)
assert _FIELD_SPAN_OLD != _FIELD_SPAN_NEW, "B3 旧形态派生失败"
assert _FIELD_SPAN_OLD not in _TRACE_TEXT, "B3 旧形态居然还在文件里"

# ── B5：`_load_all` 退回"读不出来就静静吞掉" ──
_LOAD_ALL_NEW = _window(
    _TRACE_TEXT,
    "        try:\n            by_role[role] = _read_tasks(db)\n",
    '            continue\n        files.append(db.replace(os.sep, "/"))\n',
)
_LOAD_ALL_OLD = (
    "        try:\n"
    "            by_role[role] = _read_tasks(db)\n"
    "        except Exception:  # noqa: BLE001\n"
    "            by_role[role] = []\n"
    "            continue\n"
    '        files.append(db.replace(os.sep, "/"))\n'
)
assert _LOAD_ALL_OLD != _LOAD_ALL_NEW, "B5 旧形态派生失败"
assert _LOAD_ALL_OLD not in _TRACE_TEXT, "B5 旧形态居然还在文件里"

# ── B6a：`pick_live_session` 退回"owner 未知就跳过 owner 过滤" ──
_B6A_NEW = _window(
    _SESS_TEXT,
    "    if not owner:\n",
    "    cands = [s for s in sessions if s.owner_aic == owner]\n",
)
_B6A_OLD = "    cands = [s for s in sessions if not owner or s.owner_aic == owner]\n"
assert _B6A_OLD != _B6A_NEW, "B6a 旧形态派生失败"
assert _B6A_OLD not in _SESS_TEXT, "B6a 旧形态居然还在文件里"

# ── B6b：多个新鲜候选退回"静默取 max(created_at)" ──
_B6B_NEW = _window(
    _SESS_TEXT,
    "    fresh.sort(key=lambda p: (p[0].created_at or datetime.min, p[0].session_id))\n",
    "    chosen, age = fresh[-1]\n",
)
_B6B_OLD = (
    "    fresh.sort(key=lambda p: (p[0].created_at or datetime.min, p[0].session_id))\n"
    "    chosen, age = fresh[-1]\n"
)
assert _B6B_OLD != _B6B_NEW, "B6b 旧形态派生失败"
assert "for (a, _), (b, _) in zip(" not in _B6B_OLD, "B6b 旧形态里仍留着重叠检查"
assert _B6B_OLD not in _SESS_TEXT, "B6b 旧形态居然还在文件里"

# ── T4 的锚点：**改为现场派生** ─────────────────────────────────────────────
# ⚠️ 这条原本是**手抄**的多行代码块，实测已经烂掉过一次（2026-10-03）：
#    `_attribute_fields` 里的 reason 文案被改过（加了"按词边界匹配，不含产物外壳"），
#    手抄的锚点没人更新 ⇒ 步骤 0 报 `新形态×0 旧形态×0` ⇒ 探针**静默失效**。
#    这正是本文件开头那条纪律的教材：**锚窗口一律现场派生，不手抄**。
_GAPS_NEW = _window(
    _TRACE_TEXT,
    "        gaps.append({\n",
    "        })\n",
)
_GAPS_OLD = '        gaps.append({"field": name, "reason": ""})\n'
assert _GAPS_OLD != _GAPS_NEW, "T4 旧形态派生失败"
assert _GAPS_OLD not in _TRACE_TEXT, "T4 旧形态居然还在文件里"

# ── 第三轮：渲染层的五处（bug hunt 2026-10-03 第二轮抓出来的）──────────────
#
# 这一轮全部是**「说得比做得多」**那一类：代码/docstring 声称"已经写明/已经转义/
# 已经夹住"，渲染出来的页面却什么也没做 —— 页面照样长得正常，所以没人会发现。

# ── T14：`硬` 的强度图例（HTML）──
_LEGEND_NEW = _window(
    _TRACE_TEXT,
    '        A("<div class=\'legend\'>强度口径 —— "\n',
    '          f"　<b>{_EVID[\'inferred\']}</b>：跨层归属，协议里没有硬键。</div>")\n',
)
_LEGEND_OLD = "        pass  # 旧形态：不解释「硬」是什么\n"
assert _LEGEND_OLD != _LEGEND_NEW, "T14 旧形态派生失败"
assert _LEGEND_OLD not in _TRACE_TEXT, "T14 旧形态居然还在文件里"
assert "不是平台级认证" in _LEGEND_NEW, "T14 新形态里没有那句否认（那这条探针守的不是重点）"

# ── T15：硬字段的**命中片段**（HTML）──
_EXCERPT_NEW = _window(
    _TRACE_TEXT,
    '                if src.get("excerpt"):\n',
    "                              f\"命中片段：{_esc(src['excerpt'])}</span>\")\n",
)
_EXCERPT_OLD = "                pass  # 旧形态：不摆命中片段，只说结论\n"
assert _EXCERPT_OLD != _EXCERPT_NEW, "T15 旧形态派生失败"
assert _EXCERPT_OLD not in _TRACE_TEXT, "T15 旧形态居然还在文件里"

# ── T16：HTML 里混进 markdown 加粗（`**` 会被浏览器原样显示）──
_MDMARK_NEW = '              "在整条链上找不到下游产物来源 —— 如实列出，不留空"\n'
_MDMARK_OLD = '              "在整条链上找不到下游产物来源 —— **如实列出，不留空**"\n'
assert _MDMARK_OLD != _MDMARK_NEW, "T16 旧形态派生失败"
assert _MDMARK_OLD not in _TRACE_TEXT, "T16 旧形态居然还在文件里"
assert _MDMARK_NEW not in _MDMARK_OLD, "T16 两个形态互相包含，替换不成立"

# ── T17：Markdown 表格单元格的竖线转义 ──
# ⚠️ 锚点里带反斜杠，最容易写错 —— 好在步骤 0 的 `n_new == 1` 会当场把写错的锚点
#    顶回来（"锚点过期 ⇒ 探针静默失效"就是这么防的）。
_MDCELL_NEW = '    return _preview(x, limit).replace("|", "\\\\|")\n'
_MDCELL_OLD = "    return _preview(x, limit)\n"
assert _MDCELL_OLD != _MDCELL_NEW, "T17 旧形态派生失败"
assert _MDCELL_OLD not in _TRACE_TEXT, "T17 旧形态居然还在文件里"

# ── T18：取最早/最晚时刻必须按时间比，不许按字典序比 ──
_MAXTS_NEW = _window(
    _TRACE_TEXT,
    "    sent = [t for t in (_utc(_parse_ts(h.sent_at)) for h in hops) if t is not None]\n",
    '    finished = _iso(max(fin)) if fin else ""\n',
)
_MAXTS_OLD = (
    '    started = min((h.sent_at for h in hops if h.sent_at), default="")\n'
    '    finished = max((h.last_event_at for h in hops if h.last_event_at), default="")\n'
)
assert _MAXTS_OLD != _MAXTS_NEW, "T18 旧形态派生失败"
assert _MAXTS_OLD not in _TRACE_TEXT, "T18 旧形态居然还在文件里"
assert "_utc(" not in _MAXTS_OLD, "T18 旧形态里居然还留着时区归一，那它就不是旧形态"
assert "_parse_ts(h.sent_at)" not in _MAXTS_OLD, "T18 旧形态里居然还解析了时间戳"

# ── T19：越界值的 pin 必须夹住 ──
_CLAMP_NEW = "        pos = max(0.0, min(100.0, raw_pos))\n"
_CLAMP_OLD = "        pos = raw_pos\n"
assert _CLAMP_OLD != _CLAMP_NEW, "T19 旧形态派生失败"
assert _CLAMP_OLD not in _TRACE_TEXT, "T19 旧形态居然还在文件里"

# ── T20：**我们自己的文案**里也不许有 markdown 标记 ──
# ⚠️ 这条与 T16 是**两件事**：T16 管页面模板里的两行字，T20 管**告警 / gaps 原因 /
#    推断边的判据** —— 它们是运行时拼出来的字符串，同样会被 `_esc()` 原样放进 HTML。
#    只修模板不修这些，页面上照样会冒出星号（实测：旗舰样本单页里出现过）。
# ⚠️ 范围**只圈我们自己写的**：命中片段是下游产物原文，里面本来就可能带 `**`，
#    那是**证据**，不能动 —— 一刀切"整页不许有 `**`"会逼人去改证据。
_CHK_MD_NEW = '                          else "（本局没有任何下游产物返回：各跳均失败或超时）")),\n'
_CHK_MD_OLD = '                          else "（本局**没有任何**下游产物返回：各跳均失败或超时）")),\n'
assert _CHK_MD_OLD != _CHK_MD_NEW, "T20 旧形态派生失败"
assert _CHK_MD_OLD not in _TRACE_TEXT, "T20 旧形态居然还在文件里"

# ── T21：真机样本里**真的泄漏过**的那一条告警文案 ──
# ⚠️ 这不是 T20 的重复，是**同一个病灶的另一条分支**：
#    T20 退的是 gaps 那处字符串；而**真机旗舰样本单页里当真冒出来的星号**，来自
#    「缺少 sidecar ⇒ 按 verdict 时刻认领一局」这条 WARNING —— 实测修前重新生成的
#    样本 HTML 里，`**本表把 verdict 归到哪一局靠推断**` **原样进了交付页**。
#    当时用例 N 的 ③b 已经把那五条告警分支逐个踩过，**偏偏漏了这一条** ⇒ 那条
#    "我们自己的文案里没有 markdown 标记"的断言，对**真正回归过的那条分支**
#    恰好是空转的。（这就是本项目那条"契约只挡住最危险的一半"。）
#    T21 连同用例 N 新增的 n7 分支一起堵上：退回 `**` ⇒ case N 必须在
#    「缺 sidecar 降级认领」那条上报失败。
_SIDECAR_MD_NEW = '        + f"；容差 {used_tol:.0f}s。本表把 verdict 归到哪一局靠推断，"\n'
_SIDECAR_MD_OLD = '        + f"；容差 {used_tol:.0f}s。**本表把 verdict 归到哪一局靠推断**，"\n'
assert _SIDECAR_MD_OLD != _SIDECAR_MD_NEW, "T21 旧形态派生失败"
assert _SIDECAR_MD_OLD not in _TRACE_TEXT, "T21 旧形态居然还在文件里"
assert _SIDECAR_MD_NEW in _TRACE_TEXT, "T21 新形态不在文件里（锚点过期了）"


CASES = [
    {
        "name": "假值不许被抹成空串（_preview 退回 `str(text or \"\")`）",
        "why": "False / 0 是假值 ⇒ is_incitement=False 与 harm_index=0 显示成空白，"
               "读起来像'这一项没有值'。最敏感的结论静默丢数字。",
        "file": TRACE,
        "new": '    s = " ".join(("" if text is None else str(text)).split())\n',
        "old": '    s = " ".join(str(text or "").split())\n',
        "needle": "_preview(False) 必须是 'False' 而不是空串",
        "runner": "verify_trace",
    },
    {
        "name": "verdict 清单不许把 sidecar 一起捞进来（_verdict_files 退回不过滤）",
        "why": "`verdict-<TS>.meta.json` 也以 .json 结尾 ⇒ 取'最新一份'取到 sidecar，"
               "再去 `…meta.meta.json` 找不到 ⇒ 整局定不出来。不报错，只是空。",
        "file": TRACE,
        "new": '    return sorted(\n'
               '        p for p in glob.glob(os.path.join(root, "verdict-*.json"))\n'
               '        if not p.endswith(".meta.json")\n'
               '    )\n',
        "old": '    return sorted(\n'
               '        p for p in glob.glob(os.path.join(root, "verdict-*.json"))\n'
               '    )\n',
        "needle": "verdict 清单只含 verdict 本体（排掉了 *.meta.json）",
        "runner": "verify_trace",
    },
    {
        "name": "定局不许认错会话（退回'全部角色 + 取最近一局'）",
        "why": "内层会话窗口也罩住 verdict 时刻 ⇒ 认领到 fact-judge 的内层 session，"
               "整条链凭空少一层，而输出照样像模像样。",
        "file": TRACE,
        "new": _RESOLVE_NEW,
        "old": _RESOLVE_OLD,
        "needle": "🔴 认领到的是**外层/主控**会话",
        "runner": "verify_trace",
    },
    {
        "name": "gaps 不许只有字段名、没有原因（reason 退回空白）",
        "why": "一行没有理由的'找不到来源'读起来像噪声 —— 下次就没人看它了，"
               "'如实列出'这条纪律随之失效。",
        "file": TRACE,
        "new": _GAPS_NEW,
        "old": _GAPS_OLD,
        "needle": "gaps 每项都带原因",
        "runner": "verify_trace",
    },
    {
        "name": "硬溯源行必须在页面上看得出来（结论字段行退回不带 class）",
        "why": "CSS 规则写了但 class 不插值 ⇒ 硬溯源行与无来源行长得一模一样，"
               "第 3 屏上'哪些字段有下游原文撑腰'这件事在视觉上消失。",
        "file": TRACE,
        "new": _ROW_NEW,
        "old": _ROW_OLD,
        "needle": "硬溯源字段渲染成 class='hard'",
        "runner": "verify_trace",
    },

    # ── 第二轮（bug hunt 2026-10-03）──────────────────────────────────────────
    {
        "name": "🔴 请求原文只取正文（_text_payload 退回递归收集所有字符串）",
        "why": "文本项形态是 {'type':'text','text':'正文'}，递归会把 'type' 的**值** 'text' "
               "也收进来 ⇒ 请求原文前面凭空多一行 `text`。看着只是多一行，"
               "实质是**假证据**：那一行不是下游说的。",
        "file": TRACE,
        "new": _TEXT_PAYLOAD_NEW,
        "old": _TEXT_PAYLOAD_OLD,
        "needle": "请求原文与原始正文逐字相同",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 一跳不许造两遍（seen_sessions 退回真正的死守卫）",
        "why": "主控在同一局里对同一下游委派两趟时，同一个内层会话被每个父各展开一遍："
               "同一条任务被造成两条跳、连'跨智能体调用次数'也跟着虚增，"
               "而输出看起来完全正常。",
        "file": TRACE,
        "new": "            fresh = [t for t in cands "
               "if t.session_id not in seen_sessions]\n",
        "old": "            fresh = list(cands)\n",
        "needle": "跳数 = 3",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 不许'自己所属会话是自己子会话'的自指边"
                "（退回不排掉父跳自己的会话）",
        "why": "父跳本身就在内层会话里，它的时间窗必然'落在自己之内' ⇒ "
               "同一会话里的那行被记成'该会话的子会话'，造出一条毫无含义的边。",
        "file": TRACE,
        "new": "                and t.session_id != parent_row.session_id\n",
        "old": "                and True  # 旧形态：不排除父跳自己的会话\n",
        "needle": "自指边",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 字段名不许子串命中（_field_span 退回 blob.find）",
        "why": "`band` 命中 `abandon`、`is_incitement` 命中 `不是is_incitement性` ⇒ "
               "给出**假的硬溯源**，而 `[硬]` 正是本表全部说服力的来源。",
        "file": TRACE,
        "new": _FIELD_SPAN_NEW,
        "old": _FIELD_SPAN_OLD,
        "needle": "`band` 不许命中 `abandon`（子串）",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 字段名不许命中产物**外壳**（_content_blob 退回不剔信封）",
        "why": "产物外壳里就有 `name` 键。verdict 里若恰好有个叫 `name`（或 `id`）的字段，"
               "整份产品 JSON 一搜就'命中'了包装纸 ⇒ 看着有出处，出处却是信封。",
        "file": TRACE,
        "new": '    return json.dumps(inner, ensure_ascii=False) if inner else ""\n',
        "old": "    return json.dumps(prod, ensure_ascii=False)\n",
        "needle": "`name` 不许命中产物外壳",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 某角色库读不出来必须告警点名（_load_all 退回静静吞掉）",
        "why": "某个角色的库坏掉后任务**全部消失** ⇒ 链路凭空少一层而输出照样像模像样；"
               "更糟的是唯一那条警告说'配不到对端的入站行' —— **把原因指到了错误的方向**，"
               "让人去查配对，而不是去查那个读不出来的库。",
        "file": TRACE,
        "new": _LOAD_ALL_NEW,
        "old": _LOAD_ALL_OLD,
        "needle": "告警点名了那个角色",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 sidecar 不许把 owner 过滤整个跳过"
                "（pick_live_session 退回 `not owner or …`）",
        "why": "owner 未知时旧写法把 owner 过滤**整个跳过** ⇒ 能认领到**别的角色**的会话，"
               "而返回的原因串还写着'owner 匹配' —— **证据串本身在说谎**，"
               "且侧车会被溯源表当**权威**、连 WARNING 都不打。",
        "file": AIP_SESSIONS,
        "new": _B6A_NEW,
        "old": _B6A_OLD,
        "needle": "owner 未知 ⇒ 拒绝认领（不再静默跳过 owner 过滤）",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 sidecar 不许在窗口重叠时静默取最新"
                "（pick_live_session 退回 max(created_at)）",
        "why": "顺序跑的局里'取最新'通常没错，但**两局时间上重叠**时它就是在赌；"
               "赌错就是把上一局的 session 写到这一局的 verdict 上。",
        "file": AIP_SESSIONS,
        "new": _B6B_NEW,
        "old": _B6B_OLD,
        "needle": "两局窗口重叠 ⇒ 拒绝认领（宁缺勿错）",
        "runner": "verify_trace",
    },

    # ── 第三轮（渲染层，"说得比做得多"）─────────────────────────────────────
    {
        "name": "🔴 「硬」必须有强度图例（退回不解释）",
        "why": "`硬` 不解释，读者会读成'平台级认证' —— 而它只表示"
               "'该字段名出现在下游返回的产物正文里'。`_field_span` 的 docstring "
               "早就要求渲染层写明这一点，**说了却没做**。",
        "file": TRACE,
        "new": _LEGEND_NEW,
        "old": _LEGEND_OLD,
        "needle": "页面明文写明了『硬』不是平台级认证",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 硬字段必须摆出**命中片段**（退回只给结论）",
        "why": "不给片段，「硬」就是一句无法复核的断言 —— 而这正是本表全部说服力的来源。",
        "file": TRACE,
        "new": _EXCERPT_NEW,
        "old": _EXCERPT_OLD,
        "needle": "的命中片段",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 HTML 里不许混入 markdown 加粗（退回 `**…**`）",
        "why": "`**` 在浏览器里**原样显示**：交付页上出现 `**如实列出，不留空**`，"
               "看着就是没做完。",
        "file": TRACE,
        "new": _MDMARK_NEW,
        "old": _MDMARK_OLD,
        "needle": "HTML 明文里没有 markdown 加粗标记",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 Markdown 单元格竖线必须转义（退回不转义）",
        "why": "值里一个 `|` 就把表格拆成两行 —— 页面看着只是'多了一行怪东西'，"
               "读者却会把后半截当成新的一行数据。",
        "file": TRACE,
        "new": _MDCELL_NEW,
        "old": _MDCELL_OLD,
        "needle": "含竖线的字段值仍是",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 取最早/最晚时刻不许按字典序（退回 min/max 字符串）",
        "why": "平台眼下统一 `+00:00`，字典序**恰好**等于时间序 —— 那只是巧合。"
               "换一种偏移写法（`+08:00` / `Z`）就会静静取错端点，"
               "总耗时与泳道横轴跟着错，而页面照样长得正常。",
        "file": TRACE,
        "new": _MAXTS_NEW,
        "old": _MAXTS_OLD,
        "needle": "最早时刻按『绝对时刻』取",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 越界的 pin 必须夹住（退回不夹）",
        "why": "不夹的话 pin 被甩到色条外面，页面看着只是'有点怪'，"
               "其实在暗示一个标尺上不存在的档位。",
        "file": TRACE,
        "new": _CLAMP_NEW,
        "old": _CLAMP_OLD,
        "needle": "pin 被夹在 100% 以内",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 我们自己拼出来的文案里也不许有 markdown 标记"
                "（退回 gaps 原因里的 `**`）",
        "why": "告警 / gaps 原因 / 推断边的判据都是运行时拼出来的字符串，"
               "`render_html` 会把它们 `_esc()` 后**原样**放进页面 ⇒ 交付页上"
               "直接显示星号。只修页面模板、不修这些，星号照样出现"
               "（实测旗舰样本单页里出现过）。",
        "file": TRACE,
        "new": _CHK_MD_NEW,
        "old": _CHK_MD_OLD,
        "needle": "无下游产物：我们自己的文案里没有 markdown 标记",
        "runner": "verify_trace",
    },
    {
        "name": "🔴 真机里真的泄漏过的那条告警：缺 sidecar 认领分支"
                "也不许带 markdown 标记",
        "why": "修前重新生成的真机样本单页里，`**本表把 verdict 归到哪一局靠推断**`"
               "原样进了交付页。用例 N 当时踩了五条告警分支、**唯独漏这一条**，"
               "于是那条断言对真正回归过的分支是空转的。",
        "file": TRACE,
        "new": _SIDECAR_MD_NEW,
        "old": _SIDECAR_MD_OLD,
        "needle": "缺 sidecar 降级认领",
        "runner": "verify_trace",
    },
]


def syntax_ok(path: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)], capture_output=True
    ).returncode == 0


def run_target(which: str) -> tuple[int, str]:
    env = dict(os.environ)
    # ⚠️ 子进程默认块缓冲：断言打印到管道时若进程**中途崩掉**（比如 `assert rep is not None`
    #    抛栈），最后那几行可能还没落盘。定局类缺陷恰恰会让 verify_trace 在用例 A 就抛栈，
    #    那时"没听见响"与"确实没响"长得一模一样。强制不缓冲，让响一定听得见。
    env["PYTHONUNBUFFERED"] = "1"
    r = subprocess.run(
        [sys.executable, "-m", TARGETS[which]],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


# ── 单实例锁 ────────────────────────────────────────────────────────────────
#
# 🔴 2026-10-03 实测事故：**四个 `run.meta_verify_trace` 同时在跑**（两次 launch
#    各留下一个仍在运行的进程，加启动自愈互相配合），互相 revert / 自愈同一个
#    `tools/trace.py`。后果不是"报错"，而是：
#      · `heal_from_backup()` 会把**另一个实例正在用的 revert** 修回去、并**删掉
#        对方的备份** —— 于是对方在"文件其实已是新形态"的前提下继续跑探针；
#      · 双方都可能打印"全部逮到 ✅"，而源文件停在某个中间形态。
#    **一个能静默并发、并给出可信外观的元验证，比它要验的那些缺陷更危险** ——
#    因为它是其它所有验证的认证者。所以：拿不到锁就大声拒绝启动。
#
# 为什么是"锁文件 + PID 存活判断"而不是 `fcntl.flock`：
#   Windows 上没有 `fcntl`，而本项目**本地（Windows）与服务器（Linux）都要跑**。
#   `os.open(..., O_CREAT | O_EXCL)` 是两侧都有的**原子**创建语义，够用。


def _pid_alive(pid: int) -> bool:
    """PID 是否还活着。⚠️ 不能用 `os.kill(pid, 0)` 了事：Windows 上它**不支持**
    sig=0，会直接把这个进程**杀掉**（或抛错），两种都不是我们要的语义。"""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - 平台分支
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # 存在但不是我们有权发的 —— 仍然算活着
        return True
    except OSError:
        return False
    return True


def _read_lock(path: Path) -> dict[str, object]:
    try:
        obj = json.loads(path.read_bytes().decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 - 锁文件内容不认识就当空
        return {}


def _acquire_lock(path: Path) -> tuple[bool, str]:
    """原子加锁。返回 `(是否拿到, 说明)`；说明里带**为什么没拿到**。"""
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
        ensure_ascii=False,
    )
    for attempt in (1, 2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = _read_lock(path)
            raw = info.get("pid")
            try:
                pid = int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pid = 0
            if pid == os.getpid():
                return False, f"本进程（pid={pid}）已持有锁 {path}"
            if pid and _pid_alive(pid):
                return False, (
                    f"另一个 `run.meta_verify_trace` 正在跑"
                    f"（pid={pid}@{info.get('host', '?')}，起于 {info.get('started', '?')}）。"
                    "两个元验证并发会互相 revert / 自愈同一批源文件 ⇒ **双方的结论都不可信**，"
                    "所以这里直接拒绝启动。等它跑完再来（或确认它已死："
                    f"进程不存在时本脚本会自动回收 {path.name}）。"
                )
            if attempt == 1:
                print(
                    f"[锁] ⚠️ 发现陈旧锁（pid={pid or '?'} 已不存在），回收后重试",
                    flush=True,
                )
                with contextlib.suppress(OSError):
                    os.unlink(str(path))
                continue
            return False, f"锁文件 {path} 存在但回收不了（权限？）⇒ 拒绝启动"
        except OSError as exc:  # 目录不可写等
            return False, f"建不了锁文件 {path}（{type(exc).__name__}: {exc}）⇒ 拒绝启动"
        else:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
            return True, f"已加锁（pid={os.getpid()}，{path}）"
    return False, f"加锁未成功（{path}）"


def _release_lock(path: Path) -> None:
    """释放锁。**只释放自己的**：别的实例的锁动了会让"拒绝并发"整条失效。"""
    info = _read_lock(path)
    try:
        holder = int(info.get("pid"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        holder = 0
    if holder and holder != os.getpid():
        return
    with contextlib.suppress(OSError):
        os.unlink(str(path))


def lock_selftest(path: Path) -> list[str]:
    """证明这把锁**真的挡得住**（不是个摆设）。跑在主锁之外的另一条路径上。

    两条正向判据：
      ① 已持有它时**再拿必须失败** —— 如果 `_acquire_lock` 被写成永远成功，
         这条会当场逮到（这正是"锁是不是空转"的正面证据）；
      ② **死 PID 的陈旧锁必须能被回收** —— 否则上次被 SIGKILL 之后，
         元验证会永远拒绝启动，而拒绝的理由看起来还挺合理。
    """
    problems: list[str] = []
    probe = path.with_name(path.name + ".self")
    with contextlib.suppress(OSError):
        os.unlink(str(probe))

    ok, why = _acquire_lock(probe)
    if not ok:
        problems.append(f"① 自检连自己都锁不上：{why}")
    else:
        ok2, why2 = _acquire_lock(probe)
        if ok2:
            problems.append(
                "① 锁已被持有时**再拿却成功了** —— 这把锁挡不住并发，"
                "「拒绝并发」是句空话"
            )
            _release_lock(probe)
        else:
            print(f"    ① ✅ 已持有时再拿被拒：{why2.splitlines()[0][:80]}", flush=True)
        _release_lock(probe)

    dead = 999_999_999  # 两侧 PID 上限都远小于它
    probe.write_bytes(
        json.dumps(
            {"pid": dead, "host": socket.gethostname(), "started": "2000-01-01T00:00:00+0000"},
            ensure_ascii=False,
        ).encode("utf-8")
    )
    if _pid_alive(dead):
        problems.append(f"② 自检前提不成立：PID {dead} 居然被判成活着")
    else:
        ok3, why3 = _acquire_lock(probe)
        if not ok3:
            problems.append(f"② 死 PID 的陈旧锁没被回收：{why3}")
        else:
            print("    ② ✅ 死 PID 的陈旧锁被回收（上次被 SIGKILL 不会把元验证钉死）",
                  flush=True)
            _release_lock(probe)
    with contextlib.suppress(OSError):
        os.unlink(str(probe))
    return problems


def _startup_order_problems(src: str | None = None) -> list[str]:
    """关系断言：**加锁必须排在自愈与快照之前**。

    ⚠️ 位置不对，锁就等于没装（本项目那条"「装了」≠「会响」"）：
      · `heal_from_backup()` 会**删掉**对方的 `.metaorig` 备份、并把对方正在用的
        revert 修回去 —— 发生在加锁之前的话，两个实例照样互相踩。
      · `originals = {...}` 会把"别人 revert 到一半的内容"当原始内容存下来，
        然后心安理得地"复原"到一个**错的**状态。
    ⚠️ 只断言**先后**，不断言行号（行号会漂）。三处标记都必须**唯一**。
    ⚠️🔴 **判据不许匹配到它自己的实现。** 所有标记一律**用相邻字面量拼出来**
        （`"_acquire_lock(" "LOCK_PATH)"`，Python 编译期接成完整串，而**源码文本里
        不含**完整标记）—— 本函数自己的 `marks` 字典于是不会污染计数。
        第一版直接写字面量、又去按 `def main(` 定扫描起点，结果两处都栽了同一个跟头：
        字典里的字面量被数了进去，而 `def main(` 的**第一个**匹配点竟然是本函数里
        那行 `anchor = "def main() -> int:"`（它比真正的 `def main` 更靠前）。
        两次的报错都是"三处全 2 处" —— 一个**恒假**的判据，比没有判据更坏。
    ⚠️ `src` 可注入 —— 否则这条断言自己没法被反证（拿一段打乱的源码喂进来，
        它必须响），而"没法被反证的断言"本身就是本模块要消灭的东西。
    """
    text = Path(__file__).read_bytes().decode("utf-8") if src is None else src
    marks = {
        "加锁": "_acquire_lock(" "LOCK_PATH)",
        "自愈": "healed = heal_from_backup(" "paths)",
        "快照": "originals = {p: p.read_bytes()" " for p in paths}",
    }
    problems: list[str] = []
    pos: dict[str, int] = {}
    for label, mark in marks.items():
        n = text.count(mark)
        if n != 1:
            problems.append(f"定位不到唯一的「{label}」标记（{n} 处）：`{mark}`")
            continue
        pos[label] = text.index(mark)
    if len(pos) == 3 and not (pos["加锁"] < pos["自愈"] < pos["快照"]):
        problems.append(
            "启动次序不对：必须是「加锁 → 自愈 → 快照」，实测 "
            f"加锁@{pos['加锁']}、自愈@{pos['自愈']}、快照@{pos['快照']}"
        )
    return problems


#: 落盘备份的后缀。**故意不用 `.py`** —— 否则 `tools/file_fingerprint.py` 的
#: `*.py` 与 `tree_diff` 的遍历会把它当成一份"新增源文件"。
BACKUP_SUFFIX = ".metaorig"


def backup_of(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def heal_from_backup(paths: "set[Path]") -> list[str]:
    """启动自愈：上次被**硬掐断**（SIGKILL / 进程树被直接杀）时源文件会停在旧形态。

    ⚠️ 为什么不能只靠信号钩子：2026-10-03 实测被工具超时 SIGTERM 掐断后，
    `tools/trace.py` 停在 T3 的旧形态（`chosen = max(...)`）而钩子**没生效**，
    且没有任何提示。文件看起来完全正常、照样能跑 —— 所以必须有一条
    **不依赖信号**的兜底路径。
    """
    healed: list[str] = []
    for p in paths:
        bk = backup_of(p)
        if bk.is_file():
            p.write_bytes(bk.read_bytes())
            bk.unlink()
            healed.append(f"{p.name}（从 {bk.name} 自愈）")
    return healed


def install_restore_guard(originals: dict[Path, bytes]) -> None:
    """收到 SIGTERM / SIGINT / SIGHUP 时也要先把文件还原再退出。

    ⚠️ 没有这一段的话，被工具超时 SIGTERM 掐断时 `finally` 根本不执行
    （Python 的 SIGTERM 默认直接终止，不做栈展开）⇒ 文件就留在"退回旧形态"
    的状态里，而它看起来完全正常、照样能跑。**"看起来能跑"正是它危险的地方。**
    ⚠️ SIGHUP 不是凑数：通过 ssh 在服务器上跑时，连接一断就发 SIGHUP。
    ⚠️ **但钩子本身不可靠**（实测漏过一次，见 `heal_from_backup`）⇒ 钩子是
       第一道，`.metaorig` 备份才是兜底，两道都要有。
    """
    def handler(signum: int, _frame: object) -> None:
        for path, original in originals.items():
            try:
                if path.read_bytes() != original:
                    path.write_bytes(original)
                    print(f"[信号 {signum}] 已把 {path.name} 恢复为原始内容", flush=True)
                backup_of(path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        # 锁也要还回去 —— 否则被 SIGTERM 掐断后残留的锁会让下一次启动
        # 要等到"PID 存活判断"才发现是陈旧的（虽然也能回收，但多一道噪音）。
        try:
            _release_lock(LOCK_PATH)
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
    #    而文件其实已经留在旧形态。
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

    # 🔴 单实例：**必须排在自愈与快照之前**。理由见 `_acquire_lock` 上方的事故记录 ——
    #    并发的元验证会互相把对方的 revert 自愈回去，双方都能打印"全部逮到 ✅"。
    ok, why = _acquire_lock(LOCK_PATH)
    print(f"[锁] {'✅' if ok else '❌'} {why}", flush=True)
    if not ok:
        return 1
    try:
        problems = lock_selftest(LOCK_PATH)
        if problems:
            for p in problems:
                print(f"[锁] ❌ {p}", flush=True)
            print("[锁] ❌ 这把锁证明不了自己挡得住并发 ⇒ 本次结论一律不可信，拒绝跑",
                  flush=True)
            return 1
        print("[锁] ✅ 自检通过：已持有时再拿被拒、陈旧锁可回收", flush=True)
        order = _startup_order_problems()
        if order:
            for p in order:
                print(f"[锁] ❌ {p}", flush=True)
            print("[锁] ❌ 启动次序不成立 ⇒ 并发保护是摆设，拒绝跑", flush=True)
            return 1
        print("[锁] ✅ 次序正确：加锁 → 自愈 → 快照", flush=True)
        return _run_locked()
    finally:
        _release_lock(LOCK_PATH)


def _run_locked() -> int:
    paths = {c["file"] for c in CASES}  # type: ignore[misc]
    # ⚠️ 自愈必须在**读快照之前**：否则会把"上次被掐断留下的旧形态"当成原始内容
    #    快照下来，然后心安理得地"复原"到一个**错的**状态。
    healed = heal_from_backup(paths)
    if healed:
        print("[步骤0] ⚠️ 检测到上次被掐断的残留，已自愈：" + "、".join(healed))
    originals = {p: p.read_bytes() for p in paths}
    install_restore_guard(originals)

    texts: dict[Path, str] = {}
    for p in paths:
        text = originals[p].decode("utf-8")
        if "\r" in text:
            print(f"[步骤0] ❌ {p.name} 不是纯 LF —— 先规范化再跑元验证")
            return 1
        texts[p] = text

    # [0] 前提：每一处都必须是"新形态"，且**唯一**
    for c in CASES:
        text = texts[c["file"]]  # type: ignore[index]
        n_new = text.count(c["new"])  # type: ignore[arg-type]
        n_old = text.count(c["old"])  # type: ignore[arg-type]
        if n_new != 1 or n_old != 0:
            print(f"[步骤0] ❌ 前置不成立：{c['name']}（新形态×{n_new} 旧形态×{n_old}）")
            return 1

    # [0b] 前提：目标当前必须是绿的 —— 否则"退回后报失败"什么都证明不了，
    #      因为旧形态与现状分不开。（这一步同时是"锚点仍然有效"的前置。）
    code, out = run_target("verify_trace")
    green = code == 0 and "[失败]" not in out
    print(f"[步骤0] {'✅' if green else '❌'} {TARGETS['verify_trace']} 当前为绿 = {green}")
    if not green:
        print(out[-2000:])
        return 1

    befores = {p: digest(p) for p in paths}
    print(f"[步骤0] ✅ {len(CASES)} 处关键实现均为新形态且唯一；"
          f"指纹 {[f'{p.name}={d[:8]}' for p, d in befores.items()]}")

    try:
        for i, c in enumerate(CASES, 1):
            path, name = c["file"], str(c["name"])  # type: ignore[index]
            original = texts[path]
            new, old, needle, runner = (
                str(c["new"]), str(c["old"]), str(c["needle"]), str(c["runner"]),
            )

            # [1] 退回旧形态（先落盘备份：信号钩子不可靠，备份才是兜底）
            broken = original.replace(new, old)
            if broken.count(old) != 1 or new in broken:
                print(f"[{i}.1] ❌ {name}：替换未按预期发生")
                return 1
            backup_of(path).write_bytes(original.encode("utf-8"))
            path.write_bytes(broken.encode("utf-8"))
            if not syntax_ok(path):
                print(f"[{i}.1] ❌ {name}：退回后语法错误 —— 本次元验证证明不了任何事")
                return 1
            print(f"[{i}.1] ✅ {name}：已退回旧形态（{path.name}），且仍语法正确")

            # [2] 断言必须报失败
            #
            # ⚠️ 判据是"**某条 [失败] 行上**出现了这个匹配词"，不是"输出里出现过这个词"。
            #    两者差别很大：退回旧形态常见的两种错法是
            #      (a) 只是崩了（`assert rep is not None` 抛栈 / AttributeError），
            #      (b) 匹配词恰好出现在别处（头部说明、通过行）。
            #    只判 `needle in out` 会把这两种都算成"探针有效" —— 而它们恰恰
            #    证明不了"那条断言不是空转的"。
            _, out = run_target(runner)
            hit_lines = [l for l in out.splitlines() if "[失败]" in l and needle in l]
            hit = bool(hit_lines)
            print(f"[{i}.2] {'✅' if hit else '❌'} {name}："
                  f"{TARGETS[runner]} 在 [失败] 行上报出该匹配词 = {hit}")
            for line in hit_lines:
                print(f"          {line.strip()}")
            if not hit:
                print(f"[{i}.2] ⚠️ {name} 的断言没响 —— 它是空转的（或匹配词写错了）")
                if "[失败]" not in out:
                    print(f"          ⚠️ 注意：本次运行**一条 [失败] 行都没有**，"
                          f"exit={run_target(runner)[0]} —— 多半是'崩溃'而非'逮到'")
                    for line in out.splitlines()[-8:]:
                        print(f"          | {line}")
                return 1

            # [3] 复原
            path.write_bytes(original.encode("utf-8"))
            backup_of(path).unlink(missing_ok=True)
            if not syntax_ok(path):
                print(f"[{i}.3] ❌ {name}：复原后语法错误")
                return 1
            print(f"[{i}.3] ✅ {name}：已复原")

        # [4] 全部复原后，指纹必须一致，且目标整体转绿
        stale = [p for p in originals if digest(p) != befores[p]]
        if stale:
            print(f"[步骤4] ❌ 复原后指纹不一致：{[p.name for p in stale]}")
            return 1
        code, out = run_target("verify_trace")
        green = code == 0 and "[失败]" not in out
        print(f"[步骤4] {'✅' if green else '❌'} 复原后 {TARGETS['verify_trace']}"
              f" 转绿 = {green}（exit={code}）")
        if not green:
            print(out[-2000:])
            return 1

        print()
        print(f"元验证结论：{len(CASES)} 处旧形态**全部**被断言逮到；"
              "复原后指纹一致、目标转绿。")
        print("⚠️ 仍需另跑 `python -m run.check_meta_residue` 与 "
              "`python -m tools.tree_diff diff` —— 元验证自己的输出证明不了'没有残留'。")
        return 0
    finally:
        for path, original in originals.items():
            if path.read_bytes() != original:
                path.write_bytes(original)
                print(f"[清理] 已把 {path.name} 恢复为原始内容")
            # ⚠️ 备份**必须**在这里删干净：留着它会让下一次启动"自愈"到一个**过期的**
            #    状态（把今天修好的代码换回昨天的），而那同样是静默的。
            bk = backup_of(path)
            if bk.is_file():
                bk.unlink()
                print(f"[清理] 已删掉备份 {bk.name}")


if __name__ == "__main__":
    sys.exit(main())
