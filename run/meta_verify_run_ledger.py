"""元验证：**发起轮台账**的两条判据必须真的会响（不是空转）。

被测的两条判据（都在 `legal_acps/check_local.py` 的 3c 节）：

  A. `runtime.run` 执行期间台账必须**已绑定**（`run_ledger.current_run()` 非空）
     —— `agents.write_trace_sidecar` 正是靠它才知道自己属于哪一轮；
        绑定一旦没生效，整条溯源链就断了，而且**它不会报错**（只会静默降级），
        所以只有"假 runtime 真跑一遍"才可能发现。
  B. 两轮的时间窗**都覆盖**同一条会话时，必须判"有争议"、**不许二选一**
     —— 静默挑一个，就是把一次猜测伪装成一条溯源。
  C. `session_for_run`（sidecar 实际走的那条路）必须**看得见别的轮**：
     第一版它内部只传自己那一轮 ⇒ 窗口列表只剩一条 ⇒「有争议」永远走不到
     ⇒ **模块要防的那件事，在它自己的关键路径上没防住**。
     ⇒ 判据的输入被裁剪到只剩自己，等于把判据关掉。
  D. **两个真机探针也必须进台账**（2026-10-04 #97 新增）：它们直驱 `runtime.run`、
     不经过 `run_research_ledgered` ⇒ 过去跑完一轮台账里什么都没有（实测仍是 2 条），
     侧车也拿不到 run 身份。判据调的是**探针自己的** `run_probe()`，不是自检另写一段
     —— 退化成"grep 源码里有没有 run_ledgered"是不行的（"grep 到了"≠"运行时真会走"）。
     （当天只有图像 / 视频两个探针；第三个是 2026-10-05 #122 补的音频探针，见 S。）

以下四条是 **2026-10-04 对抗式复查**抓出来的：它们都是"已修好、但**没有判据**"，
退回旧形态后自检**整节 exit 0、一声没响**（每条都用实验证实过，不是推断）：

  E. **包装必须交回「收尾后」那条记录**：第一版返回 `open_run` 那条（`finished_at`
     为空）⇒ 调用方拿它问 `unfinished` 得 True，把跑完的轮读成「还在跑」。
     ⚠️ 只有断在**返回给调用方的那个对象**上才抓得到：退回形态下盘上的收尾记录
     照样是对的（`close_run` 照写），读回断言与 run_id 断言**全会通过**。
  F. **台账必须「先落盘、再跑」**：这是台账的**存在理由**（被 kill 的轮次也要留痕）。
     改成"先攒着、收尾时一起落盘"**不会报任何错**，只会让被 kill 的轮次一个字都不剩。
     判据只能取在"跑的那一刻"（假 runtime 在 `run()` 里现场读盘）。
  G. **读回必须按绝对时刻排**：同一秒内 `…11.4Z` 的 `.`(0x2E) 小于 `…11Z` 的 `Z`(0x5A)
     ⇒ **晚发生的那一轮排到前面**。不报错，但顺序会被认领与审计直接读。
  H. **人肉入口的「未收尾」按 `finished_at` 判**：按 `status` 判会把"已收尾、只是结果
     对象没带 status"的轮显示成「未收尾」—— **一条错的显示比 `?` 更糟**：
     它会让人去找一个根本不存在的僵尸进程。
  I. **探针的台账必须落在「它自己」的角色目录**：探针角色不固定（`e2e_video_live`
     跑的是叶子角色 `evidence-video`）⇒ 落错角色**不会报错**，只会把这一轮记到
     别人的文件里。⚠️ 与用例 D 是**两条判据**：D 证"探针进了台账"，I 证"进的是它自己那本"。

2026-10-04 晚 #101 / #95 又修了三处，同样"修好了但没判据"，补 J–P：

  J. **开记录写不进去时只降级、绝不改主流程**（#101 ①）：`open_run` 曾经在任何
     `try` 之外调 `_append` ⇒ 磁盘满 / 只读挂载时 `runner` **一次都不被调用** ——
     一轮本来能做成的研判，因为一本**旁路账**而彻底不做。
     判据断在**行为**上：`runner` 到底被调用了几次。
  K. **收记录写不进去时调用方仍须拿到 `result`**（#101 ②）：收尾一抛 ⇒ 产物其实
     已经正确落盘，却被报成"这一轮失败了"—— 一条**错信号**。
  L. **记账失败不许把原异常顶掉**（#101 ③）：异常路径里再抛，会把"这一轮为什么
     失败"替换成记账的失败，排查的人去追一个与失败无关的原因。
     ⚠️ **K 与 L 共用同一处退回形态**（`close_run` 的落盘降级）：这不是偷懒 ——
     "原异常被顶掉"本来就是"收尾会抛"的直接后果；拆成两处退回反而会让人以为
     它们是两个互不相干的修复。两条判据**各自断言一个不同的可观测量**（交回的值 /
     抛出的异常），所以仍然必须分开写。
  M. **`open_run_locked` 必须先真的抢到锁**：互斥的全部实现就是那一句
     `acquire_run_lock`。它一旦被拿掉，**没有任何别的东西会报错** ——
     两轮照样各自开、各自写台账，只是"同时只许一轮"这件事静默消失了。
  N. **`pid_alive` 必须认 Windows 的 `WinError 87`**（2026-10-04 实测：本平台上
     "进程不在"报的就是它，**不是** POSIX 的 `ESRCH`；`errno=22`）。
     只写 POSIX 那一支 ⇒ Windows 侧一路落到 `None` ⇒ **永远判"可能活着"**
     ⇒ 陈旧锁再也接不了管，而且不报任何错。
     ⚠️ **N 是平台相关的**：退回形态（只认 POSIX `ESRCH`）**在 Linux 上本来就是对的**
     ⇒ 那条判据在 Linux 上**不响才是正确行为**。所以 N 带
     `"platforms": ("win32",)`，在别的平台上被**响亮地跳过**。
     ⚠️ 这条是 **2026-10-04 在服务器 Linux 上跑本模块时实测暴露的**：本地（win32）
     当时 18 个用例全绿，服务器上它却报「断言没响 ⇒ 它是空转的」——**结论错得离谱**，
     判据没毛病，是它在一个不适用它的平台上被要求作响。
     （用例后来长到 23 条，见下面 S–W；**处数不许写死**，这里说的是当天的事实。）
     ⇒ **跨平台复跑的价值就在这里**：它逼出一条"只在某平台才成立的判据"，
       而这种事本地永远发现不了。（跳过 ≠ 通过；静默 pass 与"判据空转"**长得一模一样**。）
  O. **回收不许凭空造记录**：陈旧锁的持有人在台账里根本不存在时（台账被截断、
     或锁是别的目录留下的），`_reclaim_interrupted` 只许返回 `None`，
     不许"现造一条再收尾" —— 那会让台账里出现一个**从来没发生过的轮次**。
  P. **`release_run_lock` 只许放自己那把**：替别人解锁会把两条轮次的边界擦掉。
     ⚠️ 与 (o) 段的两条一样，必须**带正面判据**（锁主人自己必须解得掉），
     否则"永远返回 False 的实现"也能让"不许替别人解"通过。

2026-10-04 深夜再补一条（Q）。它不是"修好了但没判据"，而是**从头到尾就没有判据**：

  Q. **归属 id 必须能对上发起轮**（#95 第 2 项）：`_mint_owner()` 跑在发起轮里时铸
     `run:<run_id>`，没有发起轮（服务模式、离线脚本）才铸 `ctx:<uuid>`。
     退成「恒铸 `ctx:`」**不会报任何错**，只会让落下的字节在元数据里留一个
     **查不到出处**的 id —— 溯源链上少一环，而报告上一片正常。
     ⚠️ 「归属 = 发起轮的 run_id」被**三处** docstring 同时承诺（`inbox` 模块头、
     `_mint_owner`、`evidence_hooks.on_run_start`）⇒ 正是漂移的温床，必须有判据。
     ⚠️ 本用例的锚点在 **`legal_acps/evidence/inbox.py`** —— 其余用例都在 `run_ledger.py`。
     这不是笔误：它测的是**两模块的交界**（台账的 `run_id` 怎样变成收件箱的归属），
     而交界处的约定最容易两边都以为"对方保证了"。

同一次复查还补了 R —— 它是"判据在、但**没挂在正确的层面**"：

  R. **两个包装的入参形态不同，传错必须在入口就被拒**：
     `run_research_ledgered(runtime, …)` 收**带 `.run` 的对象**，
     `run_ledger.run_ledgered(runner, …)` 收**零参工厂**。传错的后果不是一句清楚的
     报错，而是深处那句 `AttributeError: 'function' object has no attribute 'run'`
     —— 它会被「记账失败必须不抛」那条判据当成**记账的锅**报出来，
     把人指向一个完全无关的模块（判据响了，响的理由是错的）。
     ⚠️ 判据断的是**拒绝的形态**（类名 + 可执行的出路），不是"会不会抛"：
     `AttributeError` 也抛，只断"抛了"等于没写。
     ⚠️ 本用例顺带证明一件**只能靠判据发现**的事：`.wit` 被 `tools/tree_diff`
     明确排除（"运行期数据，每次跑都会变"）⇒ "一次元验证往**权威台账**里写了垃圾"
     在**任何「批前批后全树比对」里都看不见**。退回形态下 (l0) 会真的走进
     `run_ledgered`；不给它临时 `wit_dir` 的话它就用仓库默认根 —— 本地是凭空造出
     `.wit/leader/runs.jsonl`，**服务器上那是权威溯源文件**（实测：2 行）。
     判据那边为此自带 `tempfile.TemporaryDirectory`（实测：0 行）。

2026-10-05 补三条（S–U）。它们全都来自同一天的两个任务：**#122**（音频侧没有
真机 e2e 脚本）与 **#103**（抢不到轮次锁时打一屏 traceback）：

  S. **音频探针也必须进台账**（#122）：四条证据链里**音频那一条此前没有真机 e2e
     脚本** —— 图像 / 视频各有各的探针，音频只能靠「代码看起来一样」。
     ⚠️ 与用例 D 是**两条判据**：D 证的是图像探针**那一个文件**，S 证的是**新加的
     那个文件也真被 3c 驱动了**。光把文件写出来、没被任何判据驱动，与
     「压根没有这个文件」在外表上一模一样。
  T. **3c 的探针名单不许写死**（#122 的根因）：3c 原先只登记了两个模块名 ——
     **手写名单必然漏项**：音频探针加进来了、而「没人记得补那一行」时，它的台账行为
     **没有任何判据盯着**，而「没人核」与「核过了没问题」长得一模一样。
     现在 `run/e2e_*.py` **按目录现场派生**，逐个必须在 `_PROBES` 表里。
     ⚠️ 这条守卫只保证「新探针不会悄悄溜过去」，**保证不了「该有的探针根本没有文件」**
     —— 后者只能靠人，代码里已明说（别让人误以为它管得到）。
  U. **CLI 收口只吞 `RunLocked`**（#103）：抢不到锁是**预期结果**，不该渲染成
     一屏 traceback；而 `main()` 那条路要先 `up_until_ready` 抢平台会话才轮得到锁，
     开发机上**根本走不到那一步** ⇒ 没有判据就等于没修（所以收口被抽成
     `run_agent.run_cli(factory)` 供离线驱动）。
     ⚠️ **退回形态是 `except RuntimeError`，不是"把 try 删掉"**：`RunLocked` 本身就是
     `RuntimeError` 的子类 ⇒ 方向①（被拒时的形态）**照样通过**，只有方向③
     （别的运行错误必须原样抛出去）会响。这是刻意的 —— 它测的正是「为什么不能
     放宽到父类」，而不是「有没有 try」。**闸门分得开，才算闸门。**

2026-10-06 补两条（V–W）。它们来自用户拍板的那条路（**先补真机探针、再补降级器**）：

  V. **音频侧必须挂降级 Hook**：provider 的 `input_modalities` 里**有** `audio`
     —— 它按 **OpenAI 的报文形状**声明能力，**不看 `base_url` 后面那个网关**；
     而本部署只认 `text` / `image_url` / `file` ⇒ `AudioPart` 上路即
     HTTP 422 `unknown variant 'input_audio'` ⇒ **整个 run 抛异常**。
     视频侧早有 `video_to_media` 这类降级器，音频侧一直空着 ——
     **四条链里只有被单独盯着的那条才有人替它兜底。**
     ⚠️ 退回形态**只摘 `evidence-audio` 那一处**（不是三处一起摘）：
     三处一起摘只能证明「有判据」，证明不了「判据分得清是哪一个角色漏挂」。

  W. **降级器「挂上了」不等于「会响」：有效能力声明不许被摘掉**（V 在当天傍晚
     被真机推翻之后补的）：框架的 `ContentTransform` **只在 provider 自报
     「不支持」该模态时才被应用** —— V 只证明了「Hook 挂在三个角色上」，
     而挂上 ≠ 会响：本部署的 provider 对 `audio` 自报支持
     （`supports_content(AudioPart) == (True, None)`）⇒ 框架**跳过**我们注册的
     变换器 ⇒ `AudioPart` 一路走到网关 ⇒ 真机**照样** HTTP 422。
     ⇒ `llm.build_model()` 必须把 provider 包一层 `deny_audio_provider(...)`，
     让它对 `audio` 给出**有效能力 = 不支持**（外加一句写得出由来的 `reason`）。
     ⚠️ 这条是「装了 ≠ 会响」的又一次现场重演：V 全绿的半天里，功能**是坏的**，
     而且**离线全绿**（离线假模型压根不走真 provider 的能力协商）。
     ⚠️ 退回形态用**两行**（一行注释 + 原来那句 `return`）：`old` 不许是 `new` 的
     **前缀子串**（本模块 [步骤0] 的判据是 `old not in raw`），也不许是空串
     （`bytes.count(b"")` 从来不是 1）—— 第一版就栽在这两条上。

2026-10-06 深夜再补一条（X）。它不是"没修"，是**修好了却没人盯着**：

  X. **重环节的 token 预算不许被摘掉**（W 落地后真机复跑又夹出来的）：W 之后
     真机复跑 ⇒ 422 没了（降级**真的发生**），但**整条链零产物** ——
     `status=completed`、迭代 1 轮、只有 `Reasoning(31044 字)`、
     **零工具调用、零正文**。根因实证：`finish_reason=length`、
     `completion_tokens = 8192`（**正好等于** `llm.DEFAULT_MAX_TOKENS`）、
     `reasoning_tokens = 7256` ⇒ 思考把预算吃光。
     ⚠️ **空转的一轮与跑完的一轮长得一模一样**：状态是 `completed`、
     台账两行齐全、无任何异常 —— 唯一区别是 `output/` 里没有新产物。
     ⇒ 重环节挂 `TokenBudgetHooks(max_tokens=32768)`，且**被截断时必须留痕**
     （三态：`0` / `n` / `-1`；`-1` = 无法判定，**不许**并成"没截断"）。
     ⚠️ 退回形态**只摘 `evidence-audio` 那一处**：五处一起摘只能证明"有判据"，
     证明不了"判据分得清是哪一个角色漏挂"。

做法：把每一处**退回旧形态**，要求 `legal_acps.check_local` 立刻报出对应的失败。

⚠️ **锚点要跟着实现搬家**（本模块已经因此搬过三次）：
   · 用例 A 原本锚在 `run_agent.py` 的那行 `bind_run`，#97 把它抽进了
     `run_ledger.run_ledgered()`（一次性研判与探针共用）—— 锚点同步搬家。
   · 用例 F 原本锚在 `open_run` 里那行 `_append(...)`；#101 把"落盘 + 失败降级"
     统一收进 `_persist()`（开记录与收记录共用一处），那一行没了。
   · 用例 E 原本锚在 `return result, close_run(...)`；#95 抢锁后在收尾与放锁之间
     插了顺序要求，那一句被拆成 `closed = ...` + `return result, closed`。
   · 用例 H 原本锚在三元表达式上；#95 第 4 项把它扩成三分支 `if/elif/else`。
   不搬的话前置检查会立刻报「不是预期的'新形态'」，而文案指向完全无关的方向。

⚠️⚠️ **比"搬家"更深的一条：锚点不仅要"在新形态里存在"，还必须落在
   「这条判据真正走的那条路径」上**（2026-10-04 一天内踩了两次）：
   · 用例 F：锚点落在 `open_run`，而判据取的是 `_FakeRuntime.on_disk`，
     那条路走 `run_research_ledgered → run_ledgered →` **`open_run_locked`**。
   · 用例 J：同样毛病，同样修法。
   #95 的抢锁把"开一轮"分成了**两个名字相近的入口**（`open_run` 给自检/离线用、
   `open_run_locked` 给真轮次用）。锚在错的入口上时，退回形态**改了也没人走** ——
   而"前置检查通过、所有断言没响"这个组合，与"判据本来就是空转的"**长得一模一样**。
   ⇒ **每加一个语义相近的兄弟入口，所有锚点都要重新问一次"判据到底走哪个入口"。**

⚠️ 用例 F 另有一条关于**退回形态**的教训：第一版改锚点时我写成"把 `_persist`
   换成裸 `_append`"，那**根本没退回它要测的性质**（裸写照样是先落盘，只是失败会抛），
   于是这条判据变成空转 —— 判据自己在、看起来也在跑，但它测的东西已经没了。
   **要退的是顺序，所以退回形态必须是"根本不落盘"。**

⚠️ CASES 的 `new` / `old` 一律用 **str**（不是 bytes）：`run/check_meta_residue.py`
   是按 `str(c["new"])` + `text.count(...)` 核残留的，用 bytes 会让它算成 `"b'...'"`
   而**恒不命中** —— 那正是"没人核"与"核过了没残留"长得一模一样的另一种形态。
   本模块内部读写仍一律 `read_bytes()` / `write_bytes()`（纪律 3）。

⚠️ 四条纪律（前三条是本项目踩过的坑，第四条是本模块新增的）：
  1. 退回后的文件**必须仍然语法正确** —— 否则证明的只是"文件坏了会报错"。
  2. 匹配要锚定 `[失败]` 前缀 —— 通过行的中文文案里本身就含"台账"这些字，
     用裸词匹配会把"已通过"误判成"已报失败"。
  3. 整篇读写一律 `read_bytes()` / `write_bytes()` —— Windows 上 `write_text()`
     会把 `\n` 翻成 `\r\n`，曾把源文件整篇变成 CRLF 并同步上服务器。
  4. **必须 `try/finally` 兜底复原**。既有模块在"断言没响"那条路径上直接
     `return 1`、**没有写回原文**，于是元验证失败会**把旧形态留在工作区里**
     —— 而失败路径恰恰最可能被中断。本模块不重复这个错。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 被测校验器（3c 节就在它里面）。
VERIFIER = [sys.executable, "-m", "legal_acps.check_local"]

CASES = [
    {
        "name": "台账绑定：模型执行期间必须绑着",
        "why": "`write_trace_sidecar` 靠 `current_run()` 认领本轮；绑定没生效时"
               "溯源链静默断裂（不报错、只降级）⇒ 只有真跑一遍才发现得了。",
        # ⚠️ 锚点 **2026-10-04 从 `run_agent.py` 搬到 `run_ledger.py`**：
        #    #97 把开/绑/收抽成了 `run_ledger.run_ledgered()` 的唯一实现
        #    （一次性研判与两个真机探针共用），那一行随之搬家。
        #    **锚点必须跟着实现走** —— 不搬的话前置检查立刻报「不是预期的'新形态'」，
        #    而文案指向完全无关的方向（这条坑本项目踩过：整块复制当快照，
        #    每加一个导出就过期一次）。
        "file": ROOT / "legal_acps" / "run_ledger.py",
        # 退回形态 = "绑了个空"：token 仍是合法的，`unbind_run` 不会炸，
        # 所以这条元验证证明的是**判据本身**，而不是"退回后代码崩了"。
        "new": "    token = bind_run(ledger_run)\n",
        "old": "    token = bind_run(None)  # 退回旧形态\n",
        "needle": "[失败] `runtime.run` 执行期间",
    },
    {
        "name": "窗口重叠时不许替人二选一",
        "why": "两条时间窗都覆盖同一条会话时静默取一个，就是把猜测伪装成溯源；"
               "正确行为是拒绝认领并如实报「有争议」。",
        "file": ROOT / "legal_acps" / "run_ledger.py",
        "new": (
            "        if len(hits) == 1:\n"
            "            claimed[session_id] = hits[0]\n"
            "        else:\n"
            "            contested[session_id] = hits\n"
        ),
        # 退回形态 = 干脆全部认领（= 静默二选一），`contested` 恒为空。
        # ⚠️ 开头的 `\n` 是**刻意的**：新形态里那行有 12 个空格，而 12 个空格
        #    **包含** 8 个空格 ⇒ 不锚行首的话，旧形态在新形态内部就"已经存在"，
        #    前置检查会当场判"前置不成立"（元验证自杀）。这一步实测踩过。
        "old": "\n        claimed[session_id] = hits[0]\n",
        "needle": "[失败] 两轮窗口重叠时仍然替人二选一了",
    },
    {
        "name": "单轮视角必须看得见别的轮",
        "why": "`session_for_run` 第一版内部只传自己那一轮 ⇒ 窗口列表只剩一条 ⇒"
               "「有争议」那条分支永远走不到 ⇒ 并发下照样替人二选一，"
               "而 `write_trace_sidecar` 走的正是它 —— 判据的输入被裁到只剩自己，"
               "等于把判据关掉。",
        "file": ROOT / "legal_acps" / "run_ledger.py",
        "new": "    claimed, contested = attribute_sessions(runs, sessions, slop_s=slop_s)\n",
        # 退回形态 = **只把自己挑出来**交给判据（= 原缺陷）。
        # 用列表推导而不是 `[run]`：与原来那一行等义，且不随变量改名漂移。
        "old": (
            "    claimed, contested = attribute_sessions(\n"
            "        [r for r in runs if r.run_id == run_id], sessions, slop_s=slop_s\n"
            "    )\n"
        ),
        "needle": "[失败] `session_for_run` 带上重叠的另一轮后仍认领了",
    },
    {
        "name": "真机探针也必须进台账",
        "why": "两个探针（`run/e2e_leader_image`、`run/e2e_video_live`）直驱 "
               "`runtime.run`，过去不经过 `run_research_ledgered` ⇒ 跑完一轮台账里"
               "什么都没有，侧车也拿不到 run 身份 —— 台账保留了它本来要消灭的那个"
               "盲区，只是位置挪到了探针这一侧。",
        "file": ROOT / "run" / "e2e_leader_image.py",
        "new": (
            "    return await run_ledger.run_ledgered(\n"
            "        lambda: runtime.run(parts, config=config),\n"
            "        text=task,\n"
            "        label=run_ledger.PROBE_LABEL,\n"
            "        role=role,\n"
            "        wit_dir=wit_dir,\n"
            "    )\n"
        ),
        # 退回形态 = #97 之前那行（直驱 runtime.run，不回台账记录）。
        # ⚠️ `old` **不是** `new` 的子串：它多出 `    return await ` 这一段
        #    （new 那一行是 `return await run_ledger.run_ledgered(`）。
        #    这条前置关系必须成立，否则元验证一上来就判"前置不成立"而自杀 ——
        #    上一次就是这么踩的（8 空格是 12 空格的子串）。
        "old": "    return await runtime.run(parts, config=config)\n",
        "needle": "[失败] run/e2e_leader_image.py 的 run_probe() 返回的是",
    },
    {
        "name": "包装必须交回**收尾后**那条记录",
        "why": "第一版返回的是 `open_run` 那条（`finished_at` 为空）⇒ 调用方拿它问 "
               "`unfinished` 得 True，把**已经跑完**的轮读成「还在跑」。"
               "⚠️ 这条**只有断在「返回给调用方的那个对象」上才抓得到**："
               "退回形态下盘上那条收尾记录照样是对的（`close_run` 照写）、"
               "读回断言与 run_id 断言全都通过 —— 2026-10-04 实测：退回后整节 "
               "exit 0、一声没响，判据是空的。",
        "file": ROOT / "legal_acps" / "run_ledger.py",
        # ⚠️ 锚点 **2026-10-04 第二次搬家**（#95 第 2 项）：抢锁之后，收尾与放锁之间
        #    多了一段"先写收尾记录、再放锁"的顺序要求，原来那条 `return result,
        #    close_run(...)` 被拆成了 `closed = close_run(...)` + `return result, closed`。
        #    ⇒ 锚点搬到**返回语句本身**：它才是这条判据要断的那个可观测量。
        #    附带好处：退回形态只改一行，放锁那一步照常执行，不会把锁留在盘上
        #    （第一版把退回形态写成"提前 return"，会顺手跳过放锁，把后续用例搅乱）。
        "new": "    return result, closed\n",
        # 退回形态 = 照样收尾、照样放锁，只是把开轮那条交回去。
        # ⚠️ 不能写成"把 `close_run` 整个删掉"：那是**另一个**缺陷（收尾记录
        #    根本没落盘），自检会以 `status=None` 的理由响 —— 响了，但响的理由不对，
        #    证明不了"返回错了那条"。第一版探针就是这么写的，被这份元验证当场纠正。
        "old": "    return result, ledger_run  # 退回旧形态：收尾了，但返回开轮那条\n",
        "needle": "[失败] 包装返回的是",
    },
    {
        "name": "台账必须「先落盘、再跑」",
        "why": "台账的**存在理由**：进程中途被 kill 也要留下「这一轮开始过」的痕迹"
               "（10-04 那轮 14:56:53 被外部终止，事后只能靠文件时间戳猜）。"
               "若改成「先攒着、收尾时一起落盘」，则被 kill 的轮次**一个字都不剩**，"
               "而这**不会报任何错**。判据只能取在「跑的那一刻」（假 runtime 现场读盘）。",
        "file": ROOT / "legal_acps" / "run_ledger.py",
        # ⚠️ 锚点 **2026-10-04 搬了两次，第二次才是对的**：
        #    ① #101 把"落盘 + 写不下去怎么办"统一收进 `_persist()`，`open_run` 里那行
        #       裸 `_append` 随之消失 ⇒ 锚点先改到"`open_run` 走 `_persist`"。
        #    ② **但 `open_run` 根本不是这条判据走的路**：判据取的是
        #       `_FakeRuntime.on_disk`，而那条路是
        #       `run_research_ledgered → run_ledgered → `**`open_run_locked`**。
        #       #95 的抢锁把"开一轮"分成了**两个入口**（`open_run` 给自检/离线用、
        #       `open_run_locked` 给真轮次用），锚点落在另一个入口上 ⇒
        #       退回形态**改了也没人走**，判据一声不响（实测：`[6.2] = False`）。
        #    ⇒ 教训一句话：**"锚点在新形态里存在"不等于"锚点在这条判据走的路径上"**。
        #      加一个语义相近的兄弟入口（这里就是 `open_run_locked`）之后，
        #      每一处锚点都要重新问一次"判据到底走哪个入口"。
        # ⚠️ 退回形态必须**真的退回它要测的性质**：第一版我写成"把 `_persist`
        #    换成裸 `_append`"，那是**错的** —— 裸写照样先落盘，只是失败会抛，
        #    于是这条判据变成空转（在、也跑了，但测的东西已经没了）。
        #    要退的是**顺序**，所以退回形态必须是"根本不落盘"。
        "new": (
            "    reclaimed = acquire_run_lock(record, wit_dir=wit_dir, role=role)\n"
            "    return _persist(record, wit_dir=wit_dir), reclaimed\n"
        ),
        # 退回形态 = 照样抢锁，但**开记录不落盘**；等 `close_run` 时才第一次写下去。
        # ⚠️ 与用例 M 锚在同样的两行上，但退的是**不同的东西**：
        #    M 退"抢锁"，这条退"落盘"。两者互不覆盖。
        "old": (
            "    reclaimed = acquire_run_lock(record, wit_dir=wit_dir, role=role)\n"
            "    return record, reclaimed  # 退回旧形态：先攒着，跑到收尾才落盘\n"
        ),
        "needle": "[失败] 跑的时候台账里还没有这一轮的",
    },
    {
        "name": "读回必须按绝对时刻排（不按字符串）",
        "why": "同一秒内两种 ISO 形态会**排反**：`…07:22:11.4Z` 的 `.`(0x2E) 小于 "
               "`…07:22:11Z` 的 `Z`(0x5A) ⇒ **晚发生的那一轮排到前面**。"
               "顺序决定「谁是后一轮」，而认领与审计都读它；且它**不会报错**。",
        "file": ROOT / "legal_acps" / "run_ledger.py",
        "new": "    return sorted((folded[r] for r in order), key=_order_key)\n",
        "old": (
            "    return sorted((folded[r] for r in order), key=lambda r: r.started_at)\n"
        ),
        "needle": "[失败] `read_runs` 没按**绝对时刻**排",
    },
    {
        "name": "人肉入口的「未收尾」按 finished_at 判",
        "why": "通用包装允许结果对象不带 status（那时如实记 `None`），而那种轮次"
               "**已经收尾**；按 `status` 判会把它显示成「未收尾」——"
               "**一条错的显示比 `?` 更糟**：它会让人去找一个根本不存在的僵尸进程。"
               "⚠️ #95 第 4 项之后这一行还多承担了一件事：未收尾的轮次里，"
               "**进程已不在**的要另标成「中断」（`pid_alive` 这个可观测事实），"
               "所以判据分两支取，退回形态只需错在第一支上。",
        "file": ROOT / "legal_acps" / "run_ledger.py",
        # ⚠️ 锚点 **2026-10-04 搬家**：#95 第 4 项把它从一个三元表达式
        #    （`state = "(未收尾)" if run.unfinished else (run.status or "?")`）
        #    扩成了三分支 if/elif/else。锚点随之改成**第一支**。
        "new": (
            "        if not run.unfinished:\n"
            "            state = run.status or \"?\"\n"
        ),
        # 退回形态 = 按 `status` 空不空判（第一版就是这么写的）。
        "old": (
            "        if not run.status:\n"
            "            state = \"(未收尾)\"  # 退回旧形态：按 status 空不空判\n"
        ),
        "needle": "[失败] **已收尾**、只是结果对象没带 status 的轮被显示成",
    },
    {
        "name": "探针的台账必须落在**它自己的角色**目录",
        "why": "探针角色不固定：`e2e_video_live` 默认跑叶子角色 `evidence-video` ⇒ "
               "台账必须落到 `.wit/<role>/runs.jsonl`。**落错角色不会报错**，"
               "只会把这一轮记到别人的文件里（于是「这一轮是谁发起的」又被记错了）。"
               "⚠️ 这条与用例 D 是**两条不同的判据**：D 证「探针进了台账」，"
               "本用例证「进的是**它自己**那本」。",
        "file": ROOT / "run" / "e2e_video_live.py",
        # 退回形态 = 角色写死成 `leader`（图像探针那个角色的默认值）。
        "new": "        label=run_ledger.PROBE_LABEL,\n        role=role,\n",
        "old": '        label=run_ledger.PROBE_LABEL,\n        role="leader",\n',
        # ⚠️ 匹配词 **2026-10-06 搬过一次**：3c 那段断言改成**表驱动**（加探针只加一行）
        #    之后，文案从「视频探针的轮次没记进 …」变成统一的「探针轮次没按角色落盘 …」。
        #    本用例当时因此报「断言没响 ⇒ 它是空转的」—— **判据其实响得好好的**，
        #    响的是我的匹配词。这正是本项目那条：
        #    **「判据报红但措辞没命中」不等于「判据没牙」**，要退回旧形态实测再下结论
        #    （实测见 `run/` 的定点复现：退回后这句话**正反两面各响一次**）。
        "needle": "[失败] 探针轮次没按角色落盘",
    },
    {
        "name": "音频探针也必须进台账（#122：它此前一个探针都没有）",
        "why": "四条证据链里**音频那一条此前没有真机 e2e 脚本**（#122）——"
               "图像 / 视频各有各的探针，音频只能靠「代码看起来一样」。"
               "⚠️ 这条与用例 D 是**两条不同的判据**，不能互相顶替：D 证的是图像探针"
               "那**一个文件**，本用例证的是**新加的那个文件也真被 3c 驱动了**。"
               "光把文件写出来、没被任何判据驱动，与「压根没有这个文件」长得一模一样。",
        "file": ROOT / "run" / "e2e_audio_live.py",
        # 新形态与图像探针**逐字相同**（这正是"同签名"要的效果：3c 一个驱动带三个探针）。
        # ⚠️ 但 `check_meta_residue` 是**按 `c["file"]` 分别计数的**，所以两处各出现一次
        #    不会互相干扰（`n_new == 1` 是对**那个文件**说的）。
        "new": (
            "    return await run_ledger.run_ledgered(\n"
            "        lambda: runtime.run(parts, config=config),\n"
            "        text=task,\n"
            "        label=run_ledger.PROBE_LABEL,\n"
            "        role=role,\n"
            "        wit_dir=wit_dir,\n"
            "    )\n"
        ),
        # 退回形态 = #97 之前那行（直驱 `runtime.run`，不回台账记录）。
        # ⚠️ `old` **不是** `new` 的子串：它多出 `    return await ` 这一段
        #    （new 的上一行是 `    return await run_ledger.run_ledgered(`）。
        #    这条前置关系必须成立，否则元验证一上来就判"前置不成立"而自杀。
        "old": "    return await runtime.run(parts, config=config)\n",
        "needle": "[失败] run/e2e_audio_live.py 的 run_probe() 返回的是",
    },
    {
        "name": "3c 的探针名单不许写死（#122：新探针会静默溜过去）",
        "why": "3c 原先只登记了两个模块名。**手写名单必然漏项** —— 音频探针加进来了、"
               "而「没人记得补那一行」时，它的台账行为**没有任何判据盯着**，"
               "而「没人核」与「核过了没问题」长得一模一样（这正是 #122 的形状）。"
               "现在 `run/e2e_*.py` **按目录现场派生**，逐个必须在 `_PROBES` 表里。"
               "⚠️ 这条判据只保证「新探针不会悄悄溜过去」，保证不了"
               "「该有的探针根本没有文件」—— 后者只能靠人，代码里已明说。",
        "file": ROOT / "legal_acps" / "check_local.py",
        "new": '            ("run/e2e_audio_live.py", probe_aud, "leader", True),\n',
        # 退回形态 = 把音频那一行从表里摘掉（**= #122 之前的真实状态**：
        # 文件在工作区里，但 3c 的输入集合里没有它）。
        # ⚠️ 用注释行而不是空串：`old` 为空串时 `"" in text` 恒真，前置检查会当场
        #    判"旧形态已经在了"而自杀；注释行还顺带写明了退回到哪一天的状态。
        "old": "            # 退回旧形态：音频探针没登记（= #122 之前的真实状态）\n",
        "needle": "[失败] 有真机探针没登记进",
    },
    {
        "name": "CLI 收口：只吞 `RunLocked`，不许把兄弟一起吞掉（#103）",
        "why": "抢不到轮次锁是**预期结果**（不是崩溃），不该以一屏 traceback 的形态出现 ——"
               "而 `main()` 那条路要先 `up_until_ready` 抢平台会话才轮得到锁，"
               "开发机上**根本走不到**那一步 ⇒ 没有判据就等于没修。"
               "⚠️ 退回形态取 `except RuntimeError`（而不是「把 try 整个删掉」）："
               "`RunLocked` 本身就是 `RuntimeError` 的子类 ⇒ 写成 `RuntimeError` 时"
               "**方向①照样通过**、只有「别的运行错误必须原样抛出去」那一条会响。"
               "这正是「闸门要分得开」：一对只差异常类型的输入必须要求**不同**结论。",
        "file": ROOT / "legal_acps" / "run_agent.py",
        "new": "    except run_ledger.RunLocked as exc:\n",
        # 退回形态 = 把捕获放宽到父类（= 把真正的运行故障也变成一句安静的退出码）。
        # ⚠️ `old` 不是 `new` 的子串（多了 `RuntimeError` 与那段注释）。
        "old": (
            "    except RuntimeError as exc:"
            "  # 退回旧形态：把 RunLocked 的兄弟一起吞了\n"
        ),
        "needle": "[失败] 别的**运行错误**没有被原样抛出去",
    },
    {
        "name": "音频侧必须挂降级 Hook（2026-10-06：它此前**一个都没有**）",
        "why": "provider 的 `input_modalities` 里**有** `audio` —— 因为它按 **OpenAI 的"
               "报文形状**声明能力，**不看 `base_url` 后面那个网关实际实现了什么**。"
               "而本部署是 `deepseek-v4-pro @ api.deepseek.com`，只认 "
               "`text` / `image_url` / `file` ⇒ `AudioPart` 一路走到 provider ⇒ "
               "HTTP 422 `unknown variant 'input_audio'` ⇒ **整个 run 抛异常**"
               "（不是「少一条线索」）。2026-10-06 真机实测：`run/e2e_audio_live.py` "
               "约 **1 秒**就挂在这儿。视频侧早有降级器（`video_modality.py`），"
               "音频侧一直空着 —— **四条链里只有被单独盯着的那条才有人替它兜底**，"
               "这条用例就是把「音频也有降级器」钉住。"
               "⚠️ 退回形态**只摘掉 `evidence-audio` 那一处**（不是三处一起摘）："
               "漏一个角色就该报出来，判据要能指出**是哪一个** —— "
               "「三处一起摘只能证明有判据，证明不了判据分得清」。",
        "file": ROOT / "legal_acps" / "agents.py",
        # ⚠️ `new` **只圈注释 + Hook 那几行，不许把上面那行收件箱 Hook 带进来**：
        #    本模块 [步骤0] 的判据是 `raw.count(new) == 1` **且** `old not in raw`
        #    —— `old` 是"退回后的**替身文本**"，必须是原文件里**不存在**的串。
        #    2026-10-06 实测（差点踩）：把收件箱行一起圈进来后，`old` 正好成了
        #    `new` 的前缀子串 ⇒ `old in raw` 恒真 ⇒ **步骤 0 直接判红**；
        #    而且"连收件箱行一起摘掉"还会顺带点亮 `[失败] 嵌套结构`
        #    —— 探针是响了，但响的不是你要证的那件事。
        #    真正的修复前状态 = 收件箱行**在**、只是没有降级 Hook；所以 `old`
        #    就是这一行注释（语法上仍是 hooks 列表里的合法元素）。
        "new": (
            "            # ⚠️ 音频降级：本角色是**音频字节的终点**，模型一定会收到 `AudioPart`。\n"
            "            #    不挂它，run 直接挂在厂商侧（HTTP 422 `unknown variant input_audio`）\n"
            "            #    —— 本角色存在的意义就是处理音频，而它自己会因此跑不起来。\n"
            "            #    挂上之后它拿到的是**同一份**测量报告（`legal_acps/audio` 的\n"
            "            #    `analyze()` + `summary_line()`），不是第二套实现。\n"
            "            AudioFallbackHooks(),\n"
        ),
        "old": (
            "            # 退回旧形态：evidence-audio 没挂音频降级 Hook"
            "（= 2026-10-06 之前的真实状态）\n"
        ),
        "needle": "[失败] 这些角色会收到音频字节却没挂音频降级 Hook",
    },
    {
        "name": "音频降级的「有效能力声明」不许被摘掉（2026-10-06 当天被真机夹出）",
        "why": "框架的 `ContentTransform` **只在 provider 自报「不支持」该模态时才被应用**"
               "（用 `wit.providers.base.prepare_messages_for_provider` 实测）。而本 provider"
               "按 OpenAI 报文形状对 `audio` 自报**支持** ⇒ 不显式声明有效能力，"
               "`audio_content_transform()` **永远不会被调用**，`AudioPart` 一路走到厂商侧"
               "⇒ HTTP 422 ⇒ 整个 run 挂。⚠️ 2026-10-06 真机实测**两次**，第二次是在"
               "「Hook 已挂、全部判据皆绿」之后 —— 因为覆盖判据只查**挂没挂**，"
               "查不出**响不响**；这条用例钉住的正是「它到底会不会被调用」。"
               "⚠️ 退回形态用**两行**（注释 + 原来那句 `return`）：`old` 不许是 `new` 的"
               "前缀子串（本模块 [步骤0] 的判据是 `old not in raw`），也不许是空串。",
        "file": ROOT / "legal_acps" / "llm.py",
        "new": "    return deny_audio_provider(model_provider_for(base))\n",
        "old": (
            "    # 退回旧形态：不声明音频的有效能力"
            "（= 2026-10-06 上午的真实状态）\n"
            "    return model_provider_for(base)\n"
        ),
        "needle": "[失败] 音频降级：provider 仍然**自报支持** audio",
    },
    {
        "name": "重环节的 token 预算不许被摘掉（2026-10-06 深夜真机夹出）",
        "why": "`llm.DEFAULT_MAX_TOKENS = 8192` 是**全局写死**的，而本部署的 "
               "deepseek-v4-pro 是推理模型 —— 思考也吃这份预算。真机实测那一跳："
               "`finish_reason=length`、`completion_tokens=8192`（正好打满）、"
               "`reasoning_tokens=7256` ⇒ 没有正文、没有工具调用 ⇒ 1 轮即 "
               "`completed`、**零产物**。⚠️ 空转的一轮与跑完的一轮**长得一模一样**，"
               "所以重环节必须挂 `TokenBudgetHooks`，且被截断时必须**留痕**。"
               "⚠️ 退回形态**只摘 `evidence-audio` 那一处**：五处一起摘只能证明"
               "「有判据」，证明不了「判据分得清是哪一个角色漏挂」。",
        "file": ROOT / "legal_acps" / "agents.py",
        "new": (
            "            # ⚠️ token 预算：本角色是**重环节** —— 音频链正是 2026-10-06 真机上\n"
            "            #    第一个撞上 8192 上限的（reasoning 31044 字）。不放大 ⇒ 零产物；\n"
            "            #    同一个 Hook 还负责在真被截断时**留痕**，让探针能报红。\n"
            "            TokenBudgetHooks(),\n"
        ),
        "old": (
            "            # 退回旧形态：evidence-audio 没挂 token 预算 Hook\n"
            "            # （= 2026-10-06 深夜之前的真实状态：吃全局写死的 8192）\n"
        ),
        "needle": "[失败] token 预算：重环节没挂",
    },
    {
        "name": "鉴伪分支必须要求「出结论之前派发」（2026-10-06 深夜真机夹出）",
        "why": "主控提示词里三个鉴伪分支（图像 / 音频 / 视频）此前**只有图像**写着"
               "「这一步请在**出最终结论之前**做完」——`legal_issue_verdict` 一旦提交，"
               "系统就锁定这一步的可调用工具。音频分支缺这句 ⇒ 真机上主控**第一轮**"
               "就提交了结论 ⇒ 音频委派再也没机会发生 ⇒ 整条证据链被**静默绕过**，"
               "而那一轮 `status=completed`、台账齐全、零异常。"
               "⚠️ 判据落在**每个分支各自的段落**（现场派生），不是"
               "「全文里有没有这个词」—— 全文有一句 ≠ 每个分支都有。",
        "file": ROOT / "legal_acps" / "agents.py",
        "new": (
            "- ⚠️ 这一步请在**出最终结论之前**做完：`legal_issue_verdict` 一旦提交，系统就会\n"
            "  锁定你这一步的可调用工具；把音频鉴伪排在出结论之后，它就**再也没机会跑了**。\n"
            "  （2026-10-06 真机实测的正是这一条：主控第一轮就提交了结论，音频委派从此\n"
            "  再没发生 —— 那一轮 `status` 是 `completed`、台账齐全、无任何异常，\n"
            "  而整条音频链被**静默绕过**了。）\n"
        ),
        "old": (
            "（退回旧形态：音频分支没有「出结论之前派发」这条 "
            "—— 2026-10-06 真机被绕过的原状）\n"
        ),
        "needle": "[失败] 委派顺序",
    },
    {
        "name": "音频委派闸门不许被摘掉（2026-10-06：提示词写了，模型仍不照做）",
        "why": "上一条用例守的是「提示词里写清了顺序」，这一条守的是**模型不照做时还有东西拦着它**。"
               "2026-10-06 第二次真机复跑：铁律已在提示词里（用例 Y 全绿），主控仍然**一次都没委派** —— "
               "真因是 `audio_modality` 的降级把一份**完整的技术测量报告**塞进了主控上下文"
               "（终稿里「底噪一致性 / 电网频率相位连续性没测成」这两项是报告里的 `not_run` 项，"
               "主控不可能猜到 ⇒ 它确实读了那份报告）⇒ 手上已有读数 ⇒ 自己出技术结论、直接交终稿，"
               "整条链被静默绕过。**提示词改了 ≠ 模型照做**第二次被坐实 ⇒ 补一道**闸门**。"
               "⚠️ 退回形态只摘主控那一处挂载：判据必须指名 `['leader']`，"
               "而不是笼统地说「闸门没了」—— 后者会让人看不出是哪一个角色漏挂。",
        "file": ROOT / "legal_acps" / "agents.py",
        "new": "            AudioDelegationGateHooks(),\n",
        "old": (
            "            # 退回旧形态：主控没挂音频委派闸门\n"
            "            # （= 2026-10-06 第二次真机复跑的原状：提示词有铁律，委派链仍被静默绕过）\n"
        ),
        "needle": "[失败] 音频闸门：['leader'] 没挂这个 Hook",
    },
    {
        "name": "开记录写不进去时只降级、绝不改主流程",
        "why": "`open_run` 曾经在任何 `try` 之外调 `_append` ⇒ 磁盘满 / 只读挂载时 "
               "`runner` **一次都不被调用**：一轮本来能做成的研判，因为一本**旁路账**"
               "而彻底不做。判据落在**行为**上（runner 被调用了几次），"
               "读代码是读不出来的。",
        "file": ROOT / "legal_acps" / "run_ledger.py",
        # ⚠️ 锚点落在 **`open_run_locked`**，不是 `open_run`：这条判据的
        #    `_CountingRuntime` 是经 `run_research_ledgered → run_ledgered` 跑起来的，
        #    而那条路走的是 `open_run_locked`。第一版锚在 `open_run` 上 ⇒
        #    **改了也没人走**，判据一声不响（实测：`[10.2] needle 命中 = False`）。
        #    ⇒ 「锚点在新形态里存在」**不等于**「锚点在这条判据走的路径上」。
        # 退回形态 = 开记录**裸写**（`_append` 会抛），绕过 `_persist` 的降级。
        # ⚠️ 与用例 F 的退回形态**不同、且必须不同**：F 退的是**顺序**
        #    （根本不落盘），这里退的是**失败处理**（裸写、写不进去就抛）。
        #    两者锚在同一行上，测的却是两件事（这正是两条判据都留着的原因）。
        "new": (
            "    reclaimed = acquire_run_lock(record, wit_dir=wit_dir, role=role)\n"
            "    return _persist(record, wit_dir=wit_dir), reclaimed\n"
        ),
        "old": (
            "    reclaimed = acquire_run_lock(record, wit_dir=wit_dir, role=role)\n"
            "    _append(ledger_path(wit_dir, record.role), record)"
            "  # 退回旧形态：开记录裸写，写不下去就抛\n"
            "    return record, reclaimed\n"
        ),
        "needle": "[失败] 记账写不进去时整轮直接抛了异常",
    },
    {
        "name": "收记录写不进去时调用方仍须拿到结果",
        "why": "收尾一抛 ⇒ 产物其实**已经正确落盘**，却被报成「这一轮失败了」——"
               "一条**错信号**：人会去重跑一轮本来成功的研判。",
        "file": ROOT / "legal_acps" / "run_ledger.py",
        "new": (
            "    finished = replace(\n"
            "        run,\n"
            "        finished_at=_iso((now or datetime.now(timezone.utc))"
            ".astimezone(timezone.utc)),\n"
            "        status=status,\n"
            "        iterations=iterations,\n"
            "    )\n"
            "    return _persist(finished, wit_dir=wit_dir)\n"
        ),
        # 退回形态 = 收记录**裸写**（写不下去就抛），调用方因此拿不到 result。
        # ⚠️ 与下一条（原异常被顶掉）**共用这一处退回形态**，理由见模块头 K/L。
        "old": (
            "    finished = replace(\n"
            "        run,\n"
            "        finished_at=_iso((now or datetime.now(timezone.utc))"
            ".astimezone(timezone.utc)),\n"
            "        status=status,\n"
            "        iterations=iterations,\n"
            "    )\n"
            "    _append(ledger_path(wit_dir, finished.role), finished)"
            "  # 退回旧形态：收记录裸写，写不下去就抛\n"
            "    return finished\n"
        ),
        "needle": "[失败] 收尾记账失败时调用方**拿不到这一轮的结果**",
    },
    {
        "name": "记账失败不许把原异常顶掉",
        "why": "异常路径里再抛，会把「这一轮为什么失败」**替换**成记账的失败："
               "排查的人会去追一个与失败无关的原因（实测拿到的会是 `OSError`，"
               "而真正的原因是 runner 自己抛的 `RuntimeError`）。",
        "file": ROOT / "legal_acps" / "run_ledger.py",
        # ⚠️ **与上一条共用同一处退回形态**（`close_run` 的落盘降级）：
        #    "原异常被顶掉"本来就是"收尾会抛"的直接后果。拆成两处退回，会让人
        #    以为它们是两个互不相干的修复。但两条判据断的是**不同的可观测量**
        #    （上一条：交回的值；这一条：抛出的异常），所以必须各写一条。
        "new": (
            "    finished = replace(\n"
            "        run,\n"
            "        finished_at=_iso((now or datetime.now(timezone.utc))"
            ".astimezone(timezone.utc)),\n"
            "        status=status,\n"
            "        iterations=iterations,\n"
            "    )\n"
            "    return _persist(finished, wit_dir=wit_dir)\n"
        ),
        "old": (
            "    finished = replace(\n"
            "        run,\n"
            "        finished_at=_iso((now or datetime.now(timezone.utc))"
            ".astimezone(timezone.utc)),\n"
            "        status=status,\n"
            "        iterations=iterations,\n"
            "    )\n"
            "    _append(ledger_path(wit_dir, finished.role), finished)"
            "  # 退回旧形态：收记录裸写，写不下去就抛\n"
            "    return finished\n"
        ),
        "needle": "[失败] 记账失败把**原异常顶掉了**",
    },
    {
        "name": "互斥地开一轮必须先真的抢到锁",
        "why": "互斥的**全部实现**就是 `open_run_locked` 里那一句 `acquire_run_lock`。"
               "把它拿掉之后**没有任何别的东西会报错**：两轮照样各开各的、各写台账，"
               "只是「同一角色同时只许一轮」静默消失了 —— 而两轮共用同一个收件箱与"
               "产物目录，会把归属搅成说不清的账。",
        "file": ROOT / "legal_acps" / "run_ledger.py",
        "new": (
            "    reclaimed = acquire_run_lock(record, wit_dir=wit_dir, role=role)\n"
            "    return _persist(record, wit_dir=wit_dir), reclaimed\n"
        ),
        # 退回形态 = 跳过抢锁，直接落盘开工（`reclaimed` 恒为 None）。
        "old": (
            "    return _persist(record, wit_dir=wit_dir), None"
            "  # 退回旧形态：不抢锁\n"
        ),
        "needle": "[失败] 同一角色已经有一轮在跑时，**第二轮照样开始了**",
    },
    {
        "name": "pid_alive 必须认 Windows 的 WinError 87",
        "why": "本平台实测（2026-10-04，win32 / Py3.14.6，3/3 次）：进程不在时 "
               "`os.kill(pid, 0)` 抛的是 `OSError(winerror=87, errno=22)`，"
               "**不是** POSIX 的 `ESRCH`。只写 POSIX 那一支 ⇒ Windows 侧一路落到 "
               "`None` ⇒ 永远判「可能活着」⇒ 陈旧锁再也接不了管，"
               "**而且不报任何错**（锁会一直堵着）。",
        # ⚠️ **平台门控**（2026-10-04 在服务器 Linux 上跑本模块时实测暴露）：
        #    退回形态「只认 POSIX `ESRCH`」**在 Linux 上本来就是对的** ——
        #    那边 reaped 进程抛的就是 `ProcessLookupError` ⇒ 判据不响是**正确行为**。
        #    不门控的话，服务器侧会把它报成「断言没响 ⇒ 它是空转的」，
        #    而那个结论**错得离谱**：判据没问题，是这条判据在被跑的那个平台上不适用。
        #    ⇒ 平台相关的用例必须显式声明、并被**响亮地跳过**（跳过 ≠ 通过）。
        "platforms": ("win32",),
        "file": ROOT / "legal_acps" / "run_ledger.py",
        "new": (
            "        if getattr(exc, \"winerror\", None) == 87:\n"
            "            return False\n"
            "        return None\n"
        ),
        # 退回形态 = 只认 POSIX 那一支，`OSError` 一律返回 None。
        "old": "        return None  # 退回旧形态：只认 POSIX 的 ESRCH\n",
        "needle": "[失败] 一个**确实已经退出**的进程",
    },
    {
        "name": "回收不许凭空造记录",
        "why": "陈旧锁的持有人在台账里根本不存在时（台账被截断、或锁是别的目录留下的），"
               "`_reclaim_interrupted` 只许返回 `None`。它一旦「现造一条再收尾」，"
               "台账里就会出现一个**从来没发生过的轮次** —— 而那本台账的全部价值，"
               "就在于「盘上每一条都对应一次真实发生过的发起」。",
        "file": ROOT / "legal_acps" / "run_ledger.py",
        "new": (
            "    if run is None or not run.unfinished:\n"
            "        return None\n"
            "    return close_run(run, status=ABANDONED_STATUS, wit_dir=wit_dir)\n"
        ),
        # 退回形态 = 台账里没有就现造一条（`open_run`），再给它补收尾。
        "old": (
            "    if run is None:\n"
            "        run = open_run(  # 退回旧形态：台账里没有就现造一条\n"
            "            run_id=holder.run_id, label=holder.label, role=role,"
            " wit_dir=wit_dir\n"
            "        )\n"
            "    return close_run(run, status=ABANDONED_STATUS, wit_dir=wit_dir)\n"
        ),
        "needle": "[失败] 接管陈旧锁时**凭空造出了一条台账记录**",
    },
    {
        "name": "释放锁只许放自己那把",
        "why": "替**别人**解锁会把两条轮次的边界擦掉：前一轮还没结束，后一轮就能进来。"
               "⚠️ 这条必须**带正面判据**（锁主人自己解得掉），"
               "否则一个「永远返回 False」的实现也能让「不许替别人解」通过。",
        "file": ROOT / "legal_acps" / "run_ledger.py",
        "new": (
            "    holder = _read_lock(path)\n"
            "    if holder is None or holder.run_id != run.run_id:\n"
            "        return False\n"
        ),
        # 退回形态 = 只判「有没有锁」，不判「是不是自己的」。
        "old": (
            "    holder = _read_lock(path)\n"
            "    if holder is None:\n"
            "        return False  # 退回旧形态：只判有没有锁，不判是不是自己的\n"
        ),
        "needle": "[失败] 一个**不是锁主人**的轮次把锁释放了",
    },
    {
        "name": "归属 id 必须能对上发起轮（run:<run_id>）",
        "why": "`_mint_owner()` 跑在发起轮里时铸 `run:<run_id>`，没有发起轮"
               "（服务模式、离线脚本）才铸 `ctx:<uuid>`。退成「恒铸 `ctx:`」"
               "**不会报任何错**，只会让落下的字节在元数据里留一个查不到出处的 id"
               "—— 溯源链上少一环，而报告上一片正常。"
               "⚠️ 锚点在 `evidence/inbox.py`：测的是**两模块的交界**"
               "（台账的 `run_id` 怎样变成收件箱的归属）。",
        "file": ROOT / "legal_acps" / "evidence" / "inbox.py",
        "new": (
            "    if run is not None:\n"
            '        return f"{OWNER_RUN_PREFIX}{run.run_id}"\n'
            '    return f"{OWNER_CTX_PREFIX}{uuid.uuid4().hex[:12]}"\n'
        ),
        # 退回形态 = 恒铸 `ctx:`（= 这条判据之前的世界：没有出处，但也不报错）。
        # ⚠️ 末尾那截注释**不是装饰**：`new` 的最后一行与它逐字相同，不挂注释的话
        #    `old` 就是 `new` 的子串 ⇒ 前置检查会判「旧形态已经在新形态里面」而当场
        #    自杀（本项目踩过：8 个空格是 12 个空格的子串）。注释也让下面 [1.1] 那句
        #    "替换未按预期发生"万一出现时，人能一眼看出是哪一处在退。
        "old": (
            '    return f"{OWNER_CTX_PREFIX}{uuid.uuid4().hex[:12]}"'
            "  # 退回旧形态：恒铸 ctx:\n"
        ),
        "needle": "[失败] 发起轮里铸出的归属是",
    },
    {
        "name": "两个包装的入参形态不同：传错必须在入口被拒",
        "why": "`run_research_ledgered(runtime, …)` 收**带 `.run` 的对象**，"
               "`run_ledger.run_ledgered(runner, …)` 收**零参工厂**。传错了不会"
               "得到一句清楚的报错，而是深处那句 `AttributeError: 'function' "
               "object has no attribute 'run'` —— 它会被「记账失败必须不抛」那条"
               "判据当成**记账的锅**报出来：判据响了，但响的理由是错的。"
               "⚠️ 判据断的是**拒绝的形态**（类名 + 可执行的出路），不是"
               "「会不会抛」—— `AttributeError` 也抛，只断抛了等于没写。",
        "file": ROOT / "legal_acps" / "run_agent.py",
        "new": (
            "    if not callable(getattr(runtime, \"run\", None)):\n"
            "        raise TypeError(\n"
            "            \"run_research_ledgered() 要的是**带 .run 的运行时对象**\"\n"
            "            f\"（如 WitRuntime / 自检的 _FakeRuntime），收到的是 \"\n"
            "            f\"{type(runtime).__name__}；\"\n"
            "            \"若手上是一个零参工厂（回调），请直接调 \"\n"
            "            \"run_ledger.run_ledgered(runner, text=…, label=…, role=…)。\"\n"
            "        )\n"
        ),
        # 退回形态 = 整块删掉、只留一句 `pass`（= 这条守卫之前的世界）。
        # ⚠️ 替换之后这一轮会**真的**走进 `run_ledgered` ⇒ (l0) 那个探针必须自带
        #    临时 `wit_dir`：否则这一次元验证会往**仓库默认台账**里写两条垃圾记录
        #    （实测 2 行），而 `.wit` 被 `tools/tree_diff` 排除、全树比对看不见。
        "old": "    pass  # 退回旧形态：入口守卫整块删掉\n",
        "needle": "[失败] 入参形态不对时抛的是",
    },
]


def _b(value: object) -> bytes:
    return str(value).encode("utf-8")


def syntax_ok(path: Path) -> bool:
    r = subprocess.run([sys.executable, "-m", "py_compile", str(path)], capture_output=True)
    return r.returncode == 0


def run_verifier() -> str:
    r = subprocess.run(
        VERIFIER,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (r.stdout or "") + (r.stderr or "")


def digests() -> dict[str, str]:
    return {str(c["file"]): hashlib.md5(c["file"].read_bytes()).hexdigest() for c in CASES}


#: 当前平台。用例可用 `"platforms": ("win32",)` 声明"只在某些平台上才有意义"。
#: ⚠️ 平台门控不是可选项：**2026-10-04 在服务器 Linux 上跑本模块时实测暴露** ——
#:    用例 N 退掉 `winerror == 87` 那一支后，Linux 上 POSIX 的 `ESRCH` 分支照样工作
#:    ⇒ 判据不响 ⇒ 元验证报「断言没响、它是空转的」，而那个结论**错得离谱**。
#:    平台相关的用例必须被**响亮地跳过**：跳过 ≠ 通过。静默 pass 与"判据空转"
#:    在外表上完全一样，所以跳过要单独计数、单独打印，并在结尾点名。
PLATFORM = sys.platform


def _applies(case: dict) -> bool:
    plats = case.get("platforms")
    return not plats or PLATFORM in tuple(plats)


def main() -> int:
    originals = {str(c["file"]): c["file"].read_bytes() for c in CASES}

    # [0] 前提：每一处都必须是"新形态"，且旧形态一处都不在
    #     ⚠️ 这一条**不**做平台门控：锚点的存在性与平台无关，多核一遍没有代价，
    #        而"跳过的用例连锚点都没核"是另一条静默的洞。
    for c in CASES:
        raw = originals[str(c["file"])]
        if raw.count(_b(c["new"])) != 1 or _b(c["old"]) in raw:
            print(f"[步骤0] ❌ 前置不成立：{c['name']} 不是预期的'新形态'")
            return 1
    before = digests()
    applicable = [c for c in CASES if _applies(c)]
    print(
        f"[步骤0] ✅ {len(CASES)} 处均为新形态（str 形态，与 check_meta_residue 的核对口径一致）"
    )
    print(
        f"[步骤0] 本平台 {PLATFORM}：应跑 {len(applicable)} 个用例、"
        f"跳过 {len(CASES) - len(applicable)} 个（平台相关，见下）"
    )

    skipped: list[str] = []
    try:
        for i, c in enumerate(CASES, 1):
            path: Path = c["file"]
            name = str(c["name"])
            needle = str(c["needle"])
            original = originals[str(path)]

            if not _applies(c):
                plats = "/".join(str(p) for p in c["platforms"])
                reason = f"{name}（只在 {plats} 上适用，当前是 {PLATFORM}）"
                skipped.append(reason)
                print(f"[{i}] ⏭ **跳过**：{reason}")
                print(f"          ⇒ 退回形态在本平台**本来就是对的**，判据不响是"
                      "正确行为，不是空转。**跳过不等于通过** —— "
                      "换到对应平台必须重跑本模块。")
                continue

            # [1] 退回旧形态
            broken = original.replace(_b(c["new"]), _b(c["old"]))
            if broken.count(_b(c["old"])) != 1 or _b(c["new"]) in broken:
                print(f"[{i}.1] ❌ {name}：替换未按预期发生")
                return 1
            path.write_bytes(broken)
            if not syntax_ok(path):
                print(f"[{i}.1] ❌ {name}：退回后文件语法错误 —— 本次元验证证明不了任何事")
                return 1
            print(f"[{i}.1] ✅ {name}：已退回旧形态（{path.name}），且文件仍语法正确")

            # [2] 断言必须报失败
            out = run_verifier()
            hit = needle in out
            print(f"[{i}.2] {'✅' if hit else '❌'} {name}：check_local 报出该失败 = {hit}")
            for line in out.splitlines():
                if needle in line:
                    print(f"          {line.strip()}")
            if not hit:
                print(f"[{i}.2] ⚠️ {name} 的断言没响 —— 它是空转的（或匹配词写错了）")
                return 1

            # [3] 复原
            path.write_bytes(original)
            if not syntax_ok(path):
                print(f"[{i}.3] ❌ {name}：复原后语法错误")
                return 1
            print(f"[{i}.3] ✅ {name}：已复原")

        # [4] 全部复原后：md5 必须逐文件一致，且整体转绿
        if digests() != before:
            print("[步骤4] ❌ 复原后 md5 与原文不一致")
            return 1
        out2 = run_verifier()
        green = ("[失败]" not in out2) and ("本地层面全部就绪" in out2)
        print(f"[步骤4] {'✅' if green else '❌'} 复原后整体转绿 = {green}")
        if skipped:
            # 末尾再点一次名：一次 9 分钟的运行里，"跳过"很容易被读成"通过"。
            print(
                f"[步骤4] ⏭ 本次**跳过了 {len(skipped)} 个平台相关用例**（本平台 {PLATFORM}；"
                f"**它们不算通过**，换到对应平台必须重跑）："
            )
            for s in skipped:
                print(f"          · {s}")
        else:
            print("[步骤4] ✅ 无跳过：本平台适用的用例全部实跑（没有'跳过'被读成'通过'）")
        return 0 if green else 1
    finally:
        # 纪律 4：无论从哪条路径退出（包括 return 1 与异常），都必须把原文写回。
        for c in CASES:
            path: Path = c["file"]
            if path.read_bytes() != originals[str(path)]:
                path.write_bytes(originals[str(path)])
                print(f"[finally] 已强制复原 {path.name}")


if __name__ == "__main__":
    raise SystemExit(main())
