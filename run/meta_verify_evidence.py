"""元验证：`tools/verify_evidence.py` 的断言**不是空转的**。

做法：把每处关键实现**退回旧形态**，要求断言立刻报失败。二十二处（第 21 条为
2026-10-02 复查新增，第 22 条为 2026-10-04 新增）的"旧形态"就是这一项真实踩过的二十一个坑 ——
每一处都曾让全绿毫无意义：

  1. `CM_FEATURE_STEP` 0.5 → 2.0
     块特征量化退回过粗 ⇒ 干净图上出现几百个碰撞桶 ⇒ 负对照被误报成克隆。
     （这一条同时证明"负对照断言"真的在干活。）
  2. `analysis_crop_box` 的 8 像素对齐
     退回不对齐 ⇒ 我们自己的裁剪挪走了块网格相位 ⇒ 大图被报"偏移"。
  3. `check_flat_noise` 的 JPEG 闸门
     退回无条件执行 ⇒ 每张正常 JPEG 都被报"噪声极低"。
  4. `run/samples.py::_smooth_noise` 的归一化
     退回 0..255 量纲 ⇒ 样本变成一张**白纸**，负对照"全绿"（因为什么都没测到）。
     这一条证明的是**正样本断言**在干活：白纸上 P2/P4 不会触发。
  5. `fetch.check_policy` 的白名单空集语义（第 7 节新增断言）
     退回"空白名单 = 不过滤"（`if allow and host not in allow`）—— 这是最经典的那种
     写法错误：把白名单当成"额外的过滤器"，于是**没配置就等于放行一切**。
     取图入口是本项目唯一一处主动向外部地址发起请求的环节（风险在出站 SSRF），
     这一条退回的代价是 SSRF。
  6. `evidence/__init__.py` 的惰性导出（第 8 节新增断言）
     退回模块级 `from .forensics import ...` ⇒ Pillow / numpy 被拉进主链路 ⇒
     服务器上没装图像库时，**6 个已过审 Agent 一起起不来**。
     这一条证明"缺库隔离"断言在干活。
     （退回方式是"把 `if TYPE_CHECKING:` 闸门改成恒真"，**一行**即可 ——
       2026-10-02 之前是整段复制代码块当快照，加一个导出就过期一次。）
  7. `verdict.summarize_findings` 的「前提不满足 ⇒ 无法判定」规则
     （⚠️ 2026-10-02：判据原在 `evidence/forensics.summarize`，已抽成图像 / 音频
       两条链共用的纯函数；锚点随之移到 `legal_acps/verdict.py`。
       教训：**判据搬家时锚点不会自己跟着走**，而失效的锚点只会显示"没命中"。）
     退回"只数提示异常项"的旧口径 ⇒ 一张 92% 纯色的截图被报成「**低**」，
     而「低」读起来就是"没问题"。真相是 `ela` / `noise_residual` / `copy_move`
     **三项整项没跑**，剩下能出结论的只有容器 / 量化表这类**封装层**检查。
     这一条证明的是**最危险的那条性质**在干活：不许把「测不了」说成「没问题」。
  8. `pipeline.outcome` 的 `except (ValueError, OSError)`（2026-10-01 复查补上的一处真实漏网）
     退回成"什么都不捕" ⇒ 畸形字节 / 截断图像会把异常**直接漏给调用方** ——
     而这一层存在的唯一理由就是"不要让调用方收到一句不透明的异常"。
     它漏了四种输入，一次都没被抓到，因为原断言只测了"策略拒绝"那条路径。
     **一条只测成功路径的断言，看起来和"整个边界都验过了"一模一样。**
  9. `pipeline.summary_line` 的「前提不满足」分支（与第 7 条同一个坑的**第二个出口**）
     退回旧口径 ⇒ 纯色图的摘要变成「无法判定。4 项可执行检查均未提示异常。」
     —— 报告内部改对了，而**照抄给主控的那一句**漏在外面。
     教训：改判据要顺着所有出口走一遍，尤其是"会被下游原样转述"的那一个。
 10. `forensics.normalize_mime` 的别名表
     退回不归一化 ⇒ `image/jpg`（浏览器与不少客户端的常规写法）被报「容器不一致」，
     每一张这样的正常图都会中招 —— 又是一个纯粹的假阳性。
 11. `forensics.pixel_budget_exceeded` 的像素上限闸门
     退回恒 False ⇒ 闸门形同不存在：超大图会先被 `load()` 解压（几百 MB 内存），
     然后以「Truncated File Read」这种**与真实原因无关**的失败收场。
     它护的是"判它有问题"这一步自己别先把内存吃满。

（第 12–17 条是 2026-10-01 补的"入站附件通道"那一批：

 12. `inbox.parse_handle` 的 64 位哈希格式校验
     退回不校验 ⇒ `inbox:../secret` 这种句柄会被当成**相对路径**解析，
     读到收件箱**以外**的文件。整个模块"引用内容而不是引用位置"的依据就此作废。
     ⚠️ 这一条特意选了**唯一真能穿透**的用例：其它伪造句柄（穿越到 /etc/passwd、
     编一个未登记的哈希）在两种写法下都会被拒，只能证明"没读到"，
     证明不了"为什么没读到" —— 那种断言是空转的。
 13. `fetch.resolve_local_file_flag` 的"服务模式一律不开启"
     退回"照请求执行" ⇒ 常驻进程也能开本地读取，等于把本机文件系统开放给
     每一个能调用本角色的对方（`.ssh/acps-hk-key.pem` 也在射程内）。
     这条规则刻意写在**纯函数**里而不是 `run_agent` 的 if 里，正是为了能被它钉住。
 14. `EvidenceInboxHooks.context` 的"没有附件就一个字都不注入"
     退回无条件注入 ⇒ 每条纯文本任务都被塞一段"本次自带 0 张图片"，
     对已过审的主链路是实打实的行为改动 —— 而它看起来只是啰嗦了一点。
 15. `plan_delegation` 的"必须命中本次收件箱"
     退回不看命中 ⇒ 模型随便写个哈希就能让几 MB 的图发给任意下游，
     包括与图像无关的煽动性识别 / 危害评估。
 16. `inbox.stage` 的字节上限
     退回不设限 ⇒ 超限附件照收。与取图入口同一条规矩：**超限拒绝、不做截断**。
 17. `strip_handles` 的抹除
     退回不抹 ⇒ 内部那串 64 位哈希被原样发给下游。下游该看到的是"附图一张，请鉴伪"，
     而不是我们的内部机构。
     （⚠️ 退回形态是**一行**（`return raw` → `return str(text)`）：早先是把整段实现
       抄成字符串当快照，改实现就得同步，漏同步会把旧实现静默写回。）

 18–20 是同日"**两种形态都要接**"那一批（AIP 允许调用方在 bytes / uri 之间二选一）：

 18. `EvidenceInboxHooks.context` 里对 `_references()` 的取用
     退回"只认内联字节" ⇒ **uri 形态的入站附件无声消失**。"没收到图"与
     "收到了但没人理"在结论里长得一模一样 —— 这是最难被注意到的那类省略：
     它不报错，只是少说了一件事。
 19. `inbox.image_references` 的"有内联字节就跳过"
     退回不跳过 ⇒ 同一张图同时出现在两份清单里，模型以为收到两张，
     而其中一张并没有自己的字节。
 20. `EvidenceInboxHooks._delegate_with_bytes` 的附图循环
     退回"接管了却不附字节" ⇒ 最坏的一种：模型以为图已经送出、下游回一句
     "没有图可看"，链路看起来跑通了，**而证据从未离开本进程**。
     ⚠️ 2026-10-02：循环体已从"只认图片"改成**按 MIME 分流**
     （`profile_for(kind)` → `getattr(content_module, prof.part_class)`），
     锚点随之重写；退回形态是"遍历空序列"，与"把附字节那几行删掉"语义一致。

 21. `inbox.HANDLE_IN_TEXT_RE` 的 `re.IGNORECASE`（2026-10-02 复查新增）
     退回大小写敏感的前缀 ⇒ 模型把句柄写成 `INBOX:…` / `Inbox:…` 时
     `find_handles` 认不出 ⇒ `plan_delegation` 落空 ⇒ 走原生委派、只送文本 ⇒
     **字节静默丢失、下游回"没有图可看"，整条链白跑且不报任何错**。
     它的前身更糟：识别侧用大小写敏感的正则、抹除侧用**朴素 replace**，
     两套"什么算一个句柄"各自漂 —— 于是 `inbox:A1B2…` 会被认出来却不被抹掉，
     **内部哈希泄漏给下游**。修法是两侧共用同一个正则（本条的退回只去掉
     `re.IGNORECASE`，两侧仍一致，但一致地**认不出**大写的 ⇒ 证明
     "一致性断言"防不住"一起变窄"，正面判据必须单独存在）。

 22. `inbox.resolve` 的**归属闸门**（2026-10-04 新增，#95 第 3 项）
     退回不判归属 ⇒ B 轮次能取走 A 轮次落下的字节当证据。句柄是**内容哈希**，
     所以"这份字节在"与"这份字节归本次任务"是两件事 —— 后者从模块诞生起就
     **没人管**（`resolve()` 只在文件不存在时才拒绝）。留下的不是崩溃，而是一次
     **溯源错误**：报告上一切正常，只有归属是错的，而归属恰恰是事后唯一能追的东西。
     ⚠️ 这一条同时证明「正反两面都断」是必要的：只断"跨轮次被拒"会被
     "**永远拒绝**"的实现骗过，所以第 9 节另有"同归属必须取得回来"与
     "没有归属的世界（离线核对、本脚本自己）必须照常通过"两条并列判据。
     退回形态用 `if False:` —— 与第 3 条同一个写法（本模块的既有惯例）。

⚠️ 本脚本**不覆盖** `tools/verify_evidence.py` 第 10 节的两条断言（两条委派链都走
同一个 `coordinator.delegate`、字节经 AIP 线格式往返无损）。理由：它们测的是
**wit 框架的事实**，不是我们的实现 —— 没有"退回旧形态"可言（框架是字节码分发，
改不动）。它们的证据强度是"对框架的实测"，不是"我们的决定被钉住"，两者别混为一谈。

⚠️ 四条纪律（本项目反复踩过）：
  1. 退回后的文件**必须仍然语法正确** —— 否则证明的只是"文件坏了会报错"。
     （所以退回尽量改成**一行**：需要"整块去缩进"的那种替换，只换首行就会留下
       `IndentationError`，而它看起来只是"元验证没过"。第 6 条已按这条纪律重写。）
  2. 匹配词锚定 `[未达预期]` 前缀 —— 通过行的文案里本身就含"干净 PNG"这些字，
     裸词匹配会把"已通过"误判成"已报失败"。
  3. **文本一律 `str` 匹配、字节一律 `read_bytes()` / `write_bytes()`**。
     早先图省事把中文写成 `\\xNN` 字节转义，几乎必然抄错一位；但也不能退回
     `read_text()` / `write_text()` —— Windows 上 `write_text()` 会把 `\\n` 翻成
     `\\r\\n`（曾把源文件整篇变成 CRLF 并同步上服务器）。
     所以：**读字节 → 显式 utf-8 解码 → 文本替换 → 显式编码 → 写字节**，
     并在这里断言目标文件本来就是纯 LF。
  4. 退回**不能只改一行就完事**：要看"改完之后这段代码还成不成立"。
  5. **它跑的时候不要动这些源文件。** 本脚本在启动时把目标文件的原始内容读进内存，
     结束（或收到信号）时按**快照**回写 —— 期间任何外部修改都会被**静默覆盖掉**，
     没有任何提示。本轮就真踩到了：在后台跑元验证的同时改了一行 `forensics.py`，
     几分钟后发现改动没了，而所有输出都显示"已复原 ✅"。
     所以它既是"还原守卫"，也是"覆盖守卫" —— **要改代码就等它跑完。**

用法（在 witrelate 目录下）：
    ./.venv/Scripts/python.exe -m run.meta_verify_evidence
"""

from __future__ import annotations

import contextlib
import hashlib
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORENSICS = ROOT / "legal_acps" / "evidence" / "forensics.py"
#: ⚠️ 2026-10-02 复查：`summarize` 的判据已从 `evidence/forensics.py` **抽到**
#: `legal_acps/verdict.py`（图像链与音频链共用，避免两条链对同一种失败给出不同档位）。
#: 锚点必须跟着走 —— 留在旧文件里会变成"锚在一个已经不存在的位置"，而
#: `check_meta_residue` 只会说"这处没命中"，看起来像残留、其实是被重构搬走了。
VERDICT = ROOT / "legal_acps" / "verdict.py"
SAMPLES = ROOT / "run" / "samples.py"
FETCH = ROOT / "legal_acps" / "evidence" / "fetch.py"
EVIDENCE_INIT = ROOT / "legal_acps" / "evidence" / "__init__.py"
PIPELINE = ROOT / "legal_acps" / "evidence" / "pipeline.py"
INBOX = ROOT / "legal_acps" / "evidence" / "inbox.py"
HOOKS = ROOT / "legal_acps" / "evidence_hooks.py"

CASES = [
    {
        "name": "块特征量化步长（过粗 ⇒ 干净图被误报克隆）",
        "file": FORENSICS,
        "new": "CM_FEATURE_STEP = 0.5       #",
        "old": "CM_FEATURE_STEP = 2.0       #",
        "needle": "[未达预期] N1 干净 PNG",
    },
    {
        "name": "裁剪框 8 像素对齐（不对齐 ⇒ 自作自受的「网格偏移」）",
        "file": FORENSICS,
        "new": ("    x0 = min(max((w - side) // 2 // 8 * 8, 0), w - side)\n"
                "    y0 = min(max((h - side) // 2 // 8 * 8, 0), h - side)\n"),
        "old": ("    x0 = min(max((w - side) // 2, 0), w - side)\n"
                "    y0 = min(max((h - side) // 2, 0), h - side)\n"),
        "needle": "[未达预期] N4 大图",
    },
    {
        "name": "平坦区噪声的 JPEG 闸门（去掉 ⇒ 每张正常 JPEG 都报异常）",
        "file": FORENSICS,
        "new": '    if media_type == "image/jpeg" and (jpeg_quality is None or jpeg_quality < 95):\n',
        "old": "    if False:\n",
        "needle": "[未达预期] N5 大片纯色",
    },
    {
        "name": "样本构造的归一化（退化成白纸 ⇒ 正样本断言空转）",
        "file": SAMPLES,
        "new": ("    field -= float(field.mean())\n"
                "    return field / max(float(field.std()), 1e-6)\n"),
        "old": "    return field\n",
        "needle": "[未达预期] P2 复制-移动",
    },
    {
        # ⑤ 取图入口 fail-closed（第 7 节新增断言）
        "name": "取图入口 fail-closed（退回「空白名单 = 不过滤」⇒ 未配置即可出网）",
        "file": FETCH,
        "new": (
            "    allow = allowed_hosts()\n"
            "    if not allow:\n"
            "        raise FetchRefused(\n"
            '            "未配置白名单，禁止出网取图（这是默认行为，不是故障）。"\n'
            '            "请设置 EVIDENCE_IMAGE_ALLOWED_HOSTS=<逗号分隔的主机名> 后再试。"\n'
            "        )\n"
            "    if host not in allow:\n"
        ),
        "old": (
            "    allow = allowed_hosts()\n"
            "    if allow and host not in allow:\n"
        ),
        "needle": "[未达预期] 未配置白名单时",
    },
    {
        # ⑥ 重依赖惰性导入（第 8 节新增断言）
        #    退回方式：把 `if TYPE_CHECKING:` 的**闸门本身**改成恒真 ——
        #    这就等价于"模块级一次性把子模块导出都导进来"，numpy / Pillow 被拉进主链路。
        #
        #    ⚠️ 这里原来是**整段复制** `if TYPE_CHECKING: ...` 的代码块当快照，
        #       代价是"每加一个导出就过期一次"：2026-10-02 加 `SuspicionLevel` 之后
        #       前置检查直接报「不是预期的'新形态'」，而文案指向完全无关的方向。
        #       改成一行结构退回后，导出的增删都不会再碰它。
        #       （旧写法还有个坑：整块替换必须连 4 空格缩进一起去掉，
        #         只换首行会留下 `IndentationError` —— 于是证明的只是"文件坏了会报错"。）
        "name": "重依赖惰性导入（退回模块级导入 ⇒ 缺 Pillow 时主链路一起起不来）",
        "file": EVIDENCE_INIT,
        "new": "if TYPE_CHECKING:  # pragma: no cover - 仅供静态检查，运行时不执行\n",
        "old": "if True:  # 旧形态：模块级一次性导入，把 numpy / Pillow 拉进主链路\n",
        "needle": "[未达预期] 缺 Pillow/numpy",
    },
    {
        # ⑦ 「前提不满足 ⇒ 无法判定」这条规则（第 4 节新增断言）
        #    退回"只数异常项"的旧口径（等价于把整段 gated 分支关掉）。
        #    ⚠️ 锚点在 `verdict.py` 而不是 `forensics.py`：判据已于 2026-10-02
        #       抽成两条链共用的纯函数（见该模块 docstring）。锚点位置跟着实现走。
        "name": "前提不满足时必须判「无法判定」（退回只数异常项 ⇒ 纯色图被裹成安全的「低」）",
        "file": VERDICT,
        "new": "    if not triggered:\n        gated = [\n",
        "old": "    if False:\n        gated = [\n",
        "needle": "[未达预期] 大片纯色图",
    },
    {
        # ⑧ 工具边界的异常捕获（2026-10-01 复查补上的一处**真实漏网**）
        #    退回成 `except ()`（= 什么都不捕，与最初的"只捕 FetchRefused"行为等价），
        #    要求"四种畸形输入不得漏异常"这条断言立刻响。
        #    为什么不直接把 `except (ValueError, OSError)` 整行删掉：那就成了"删除"而不是
        #    "替换"，而本元验证的前置检查要求旧形态**当前不存在**、且替换后语法仍正确。
        #    `except () as exc:` 正好两条件都满足，且语义与"不捕"完全一致。
        "name": "工具边界的异常捕获（退回「什么都不捕」⇒ 畸形输入把异常漏给调用方）",
        "file": PIPELINE,
        "new": "    except (ValueError, OSError) as exc:\n",
        "old": "    except () as exc:\n",
        "needle": "[未达预期] 四种畸形输入全部返回结构化结果",
    },
    {
        # ⑨「给主控的那句话」与报告同源（本轮修复的**第二个出口**）
        #    退回"不区分前提是否满足"的旧口径 ⇒ 纯色图的摘要会变成
        #    「综合线索等级：无法判定。4 项可执行检查均未提示异常。」
        #    —— 前半句测不了、后半句没问题，而这句话正是被原样转述给用户的那一句。
        "name": "给主控的一句话与报告同源（退回旧口径 ⇒ 摘要变成「测不了 + 没问题」）",
        "file": PIPELINE,
        "new": "    elif gated:\n",
        "old": "    elif False:\n",
        "needle": "[未达预期] 前提不满足时，「给主控的一句话」不得出现「均未提示异常」",
    },
    {
        # ⑩ MIME 别名归一化（本轮修复的一处假阳性）
        #    退回"不做归一化" ⇒ `image/jpg`（浏览器与不少客户端的常规写法）
        #    会被报成「容器不一致」，每一张这样的正常图都中招。
        "name": "MIME 别名归一化（退回不归一 ⇒ image/jpg 被误报成容器不符）",
        "file": FORENSICS,
        "new": "    return MIME_ALIASES.get(base, base)\n",
        "old": "    return base\n",
        "needle": "[未达预期] MIME 常见别名",
    },
    {
        # ⑪ 像素数闸门（本轮新增，护的是"判它有问题"这一步自己别先把内存吃满）
        #    退回成恒 False ⇒ 闸门形同不存在，超大图会先被解压，然后以
        #    「Truncated File Read」这种**与真实原因无关**的失败收场。
        "name": "像素数上限闸门（退回恒 False ⇒ 超大字头图不再报「像素数过大」）",
        "file": FORENSICS,
        "new": "    return w * h > MAX_IMAGE_PIXELS\n",
        "old": "    return w * h > 10 ** 18  # 旧形态：闸门形同不存在\n",
        "needle": "[未达预期] ……超大图报的是「像素数过大」",
    },
    {
        # ⑫ 收件箱句柄的**格式**校验（本轮新增）
        #    退回"不校验格式" ⇒ `inbox:../secret` 这类句柄会被当成**相对路径**解析，
        #    读到收件箱**以外**的文件。整个模块"引用内容而不是引用位置"的依据就此作废，
        #    而它的收益是"服务进程里一个既有的本地文件读取原语"。
        "name": "收件箱句柄必须是 64 位哈希（退回不校验 ⇒ 相对路径句柄能读出箱外的文件）",
        "file": INBOX,
        "new": "    if not _SHA256_RE.match(digest):\n",
        "old": "    if False:\n",
        # ⚠️ **2026-10-04 换过 needle**：原先匹配的是端到端那条
        #    「伪造 / 穿越 / 未登记的句柄一律拒绝」。加了**归属闸门**之后，
        #    `inbox:../secret` 会被"没有归属记录"先拦住 ⇒ 症状消失 ⇒
        #    退回格式校验**整节全绿、一条 `[未达预期]` 都不出**（已实测：
        #    `parse_handle` 照样返回 `'../secret'`，`is_handle` 照样认它）。
        #    ⇒ 改用**入口校验点直断**那条当 needle；这条教训值得单记一句：
        #      **后加的第二道防线会把前面判据的症状掩盖掉 —— 判据不是变红，是变空。**
        "needle": "[未达预期] `is_handle` 只认 64 位十六进制",
    },
    {
        # ⑬ 「服务模式不读本地文件」（本轮把环境变量改成进程内开关时立的规矩）
        #    退回"照请求执行" ⇒ 常驻进程也能开本地读取，等于把本机文件系统
        #    开放给每一个能调用本角色的对方（`.ssh/acps-hk-key.pem` 也在射程内）。
        #    注意这条规则刻意写在一个**纯函数**里而不是 run_agent 的 if 里 ——
        #    正是为了能被这一条钉住。
        "name": "服务模式不读本地文件（退回「照请求执行」⇒ 常驻进程也开放本地读取）",
        "file": FETCH,
        "new": "    if serving:\n        return False\n",
        "old": "    if serving:\n        return True\n",
        "needle": "[未达预期] 服务模式下即使调用方要求",
    },
    {
        # ⑭ 纯文本任务的零注入（本轮新增的能力，必须证明它"没附件时真的不动"）
        #    退回无条件注入 ⇒ 每一条纯文本任务都被塞进一段"本次自带 0 张图片"，
        #    对已经过审的主链路是实打实的行为改动 —— 而它看起来只是啰嗦了一点。
        "name": "纯文本任务不注入收件箱提示（退回无条件注入 ⇒ 已过审的纯文本链路被改）",
        "file": HOOKS,
        "new": (
            "        if not items and not refs:\n"
            "            # **纯文本任务在这里退场** —— 一个字都不注入。\n"
            "            return []\n"
        ),
        "old": "        if False:\n            return []\n",
        "needle": "[未达预期] 纯文本任务",
    },
    {
        # ⑮ 委派只在**请求里出现句柄**时才附带字节（本轮新增）
        #    退回"请求里只要有一串句柄样子就把全部条目带上" ⇒
        #    模型随便写个哈希就能让几 MB 的图发给任意下游 —— 包括与图像无关的
        #    煽动性识别 / 危害评估。传不传必须由"这个句柄真的在本次收件箱里"决定。
        "name": "委派附字节必须命中本次收件箱（退回不看命中 ⇒ 编个哈希也能把图发出去）",
        "file": HOOKS,
        "new": "    hit = [by_handle[h] for h in handles if h in by_handle]\n",
        "old": "    hit = list(by_handle.values()) if handles else []\n",
        "needle": "[未达预期] 句柄不在本次收件箱里",
    },
    {
        # ⑯ 收件箱的字节上限（本轮新增）
        #    退回不设限 ⇒ 一条远超上限的附件照收不误。与取图入口同一条规矩：
        #    **超限拒绝、不做截断** —— 截断过的图会让所有像素域统计失真。
        "name": "收件箱字节上限（退回不设限 ⇒ 超限附件照收）",
        "file": INBOX,
        "new": "    if len(data) > MAX_STAGE_BYTES:\n",
        "old": "    if False:\n",
        "needle": "[未达预期] 超过上限的附件被拒",
    },
    {
        # ⑰ 委派时抹掉句柄（本轮新增）
        #    退回不抹 ⇒ 内部那串 64 位哈希被原样发给下游：下游看到的应该是
        #    "附图一张，请鉴伪"，而不是我们的内部机构；顺带也避免句柄被再次转发。
        #
        # ⚠️ 退回形态写成**一行**（`return raw` → `return str(text)`）而不是把整段
        #    实现抄成字符串：抄整段的话，实现一改这份快照就过期，而元验证会把
        #    **过期的旧实现静默写回**（上一轮刚踩过）。一行退回只依赖"最后返回什么"。
        "name": "委派文本里抹掉句柄（退回不抹 ⇒ 内部哈希被原样发给下游）",
        "file": HOOKS,
        "new": "    return inbox.HANDLE_IN_TEXT_RE.sub(_sub, raw)\n",
        "old": "    return str(text)\n",
        "needle": "[未达预期] ……文字部分保留",
    },
    {
        # ⑲ 句柄的**大小写形态**两侧必须一致（2026-10-02 实测缺陷）
        #    实测：`find_handles` 原本用大小写敏感的正则、`strip_handles` 用朴素
        #    replace ⇒ 模型把前缀写成大写时**接管静默落空**、字节丢失且不报错。
        #    修法是两侧共用 `inbox.HANDLE_IN_TEXT_RE`。
        #    这里退回成"去掉 re.IGNORECASE"（一行，语法仍正确）：
        #    两侧**仍然一致**，但一致地**认不出大写形态** ⇒ 正面判据必须响。
        #    （顺带证明"关系断言"单独用不够：一致性防不住"一起变窄"。）
        "name": "句柄大小写形态（退回大小写敏感前缀 ⇒ 大写句柄认不出、字节静默丢失）",
        "file": INBOX,
        "new": 'HANDLE_IN_TEXT_RE = re.compile(r"inbox:[0-9a-fA-F]{64}", re.IGNORECASE)\n',
        "old": 'HANDLE_IN_TEXT_RE = re.compile(r"inbox:[0-9a-fA-F]{64}")\n',
        "needle": "[未达预期] 句柄的大小写形态",
    },
    {
        # ⑱ 入站附件的**第二种形态**（uri）不许被静默丢弃（本轮新增）
        #    AIP 允许调用方在 bytes / uri 之间二选一，所以"只认 data" = 另一种形态的
        #    附件**无声消失**。"没收到图"与"收到了但没人理"在结论里长得一模一样 ——
        #    这条退回的正是那种"不报错、只是少说一件事"的省略。
        "name": "uri 形态入站附件不被静默丢弃（退回只认内联字节 ⇒ 地址形态无声消失）",
        "file": HOOKS,
        "new": "        refs = self._references(ctx)\n",
        "old": "        refs = []  # 旧形态：只认内联字节\n",
        "needle": "[未达预期] 入站附件的 uri 形态",
    },
    {
        # ⑲ 两种形态互斥（本轮新增）
        #    退回"不做跳过" ⇒ 带内联字节的附件**同时**出现在两份清单里：
        #    模型会以为收到两张图，而其中一张并没有自己的字节。
        "name": "内联字节与地址两种形态互斥（退回不跳过 ⇒ 同一张图被报两次）",
        "file": INBOX,
        "new": (
            "        if isinstance(data, (bytes, bytearray)) and data:\n"
            "            continue  # 有内联字节的归 image_attachments()，不在这里重复报\n"
        ),
        "old": "        if False:\n            continue\n",
        "needle": "[未达预期] 两种形态互斥",
    },
    {
        # ⑳ 接管委派时必须**真的把字节附上**（本轮新增）
        #    退回"接管了却不附字节" ⇒ 最坏的一种：模型以为图已经送出、下游回一句
        #    "没有图可看"，链路看起来跑通了，**而证据从未离开本进程**。
        #    ⚠️ 2026-10-02 复查：实现已从"只认图片"改成**按 MIME 分流**
        #       （`kind = kinds.get(...)` → `profile_for(kind)` → `getattr(content_module,
        #       prof.part_class)`），锚点随之重写。退回形态取"遍历空序列" ——
        #       与"把附字节那几行删掉"**语义完全一致**，但仍是合法语句
        #       （元验证要求旧形态当前不存在、且替换后语法仍正确；
        #        "删除"不能表达成"替换"）。
        #    ⚠️ 2026-10-03 复查（**过期锚点，当场抓到**）：加视频模态时第三行又改成了
        #       `kinds.get(item.handle, self._fallback_kind())`，而这里没跟着改
        #       ⇒ 残留核对报「新形态×0 旧形态×0」：锚点两边**都不存在**。
        #       这是"锚点过期 ⇒ 探针静默失效"的教科书形态 ——
        #       `meta_verify_evidence` 跑到这一步会以"前置不成立"退出，
        #       而**它不响的样子与"跑过了、没问题"一模一样**。
        "name": "接管委派必须真的附上字节（退回只送文本 ⇒ 证据留在本进程）",
        "file": HOOKS,
        "new": (
            "        parts: list[Any] = [TextPart(text=body)]\n"
            "        for item in items:\n"
            "            kind = kinds.get(item.handle, self._fallback_kind())\n"
        ),
        "old": (
            "        parts: list[Any] = [TextPart(text=body)]\n"
            "        for item in ():  # 旧形态：接管了却不附字节\n"
            "            kind = kinds.get(item.handle, self._fallback_kind())\n"
        ),
        "needle": "[未达预期] 请求里带句柄",
    },
    {
        "name": "句柄的归属不许跨轮次推断（退回 ⇒ B 轮取走 A 轮的字节）",
        "why": "句柄是**内容哈希** ⇒「这份字节在」与「这份字节归本次任务」是两件事。"
               "`resolve()` 原先只在**文件不存在**时才拒绝，于是 B 轮可以拿 A 轮落下的"
               "字节当证据 —— 这不是崩溃，是一次**溯源错误**：报告上一切正常，"
               "只有归属是错的，而归属恰恰是事后唯一能追的东西。",
        "file": INBOX,
        # 退回形态 = 干脆不判归属（第 3 条用的也是这个写法：`if False:`）。
        # ⚠️ 这条判据**自带正反两面**（同归属要取得回来、没有归属的世界要照常通过），
        #    否则一个"永远拒绝"的实现也能让"跨轮次被拒"通过。
        "new": (
            "    owners = _owners_of(meta)\n"
            "    owner = current_owner()\n"
            "    if owner not in owners:\n"
            "        raise InboxRefused(_owner_refusal(digest, owners, owner))\n"
        ),
        "old": (
            "    owners = _owners_of(meta)\n"
            "    owner = current_owner()\n"
            "    if False:  # 退回旧形态：不判归属（只在文件不存在时才拒绝）\n"
            "        raise InboxRefused(_owner_refusal(digest, owners, owner))\n"
        ),
        "needle": "[未达预期] 跨轮次：B 轮",
    },
]


def syntax_ok(path: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)], capture_output=True
    ).returncode == 0


def run_verify() -> str:
    r = subprocess.run(
        [sys.executable, "-m", "tools.verify_evidence"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return (r.stdout or "") + (r.stderr or "")


def digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def install_restore_guard(originals: dict[Path, bytes]) -> None:
    """收到 SIGTERM / SIGINT / **SIGHUP** 时也要先把文件还原再退出。

    ⚠️ 为什么必须有这一段（实测踩到）：元验证跑到一半被工具超时 SIGTERM 掐断时，
    **`finally` 根本不会执行**（Python 的 SIGTERM 默认直接终止，不做栈展开），
    于是 `forensics.py` 就留在"退回旧形态"的状态里 —— 而它看起来完全正常、
    照样能跑，只有下一次跑断言时才会以两条莫名其妙的失败暴露出来。
    "看起来能跑"正是它危险的地方。

    ⚠️ SIGHUP 不是凑数：**在服务器上通过 ssh 跑这个脚本时，连接一断就发 SIGHUP**，
    和 SIGTERM 一样不走 `finally`。远程跑元验证恰恰是最需要还原保证的场合 ——
    留在服务器上的破文件没有任何人会发现。
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

    # ⚠️ 不能用 `signal.SIGHUP` 字面量：**Windows 上没有这个属性**，
    #    而且它是在 `try` 之外求值的（构造 for 的元组时），`except AttributeError`
    #    根本拦不住 —— 结果是本地直接崩、服务器却正常。
    #    （这个平台差异真的发生了一次：两侧文件 md5 完全相同，服务器 6 用例全绿，
    #      本地 AttributeError。**"跑过了"必须按平台分别说**。）
    #    所以按名字取、取不到就跳过。
    for sig in [s for s in (getattr(signal, n, None)
                            for n in ("SIGTERM", "SIGINT", "SIGHUP"))
                if s is not None]:
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # 非主线程 / 平台不支持该信号
            pass


def main() -> int:
    # ⚠️ 行缓冲：被掐断时也必须看得见"跑到第几条了"。
    #    2026-10-02 实测事故：本脚本在 Windows 上被工具强杀时，块缓冲导致
    #    **一个字都没落盘**，`fetch.py` 却已经被退回旧形态（fail-open 的白名单判定）
    #    静静留在盘上 —— 而"中途被杀"与"根本没跑"在日志里长得一模一样。
    #    ⚠️ 更要紧的一条平台事实：**Windows 上外部强杀（TerminateProcess）
    #       根本挂不上 `signal` 处理器**，所以下面的还原守卫在那种情况下救不了你。
    #       ⇒ 跑元验证的**唯一**可靠保障是：跑前记全树指纹、跑完 diff 一遍
    #       （`bash /tmp/mkman.sh` 那套），以及给足超时、别用会被掐断的管道。
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

    paths = {c["file"] for c in CASES}  # type: ignore[misc]
    originals = {p: p.read_bytes() for p in paths}
    install_restore_guard(originals)

    # 读字节 → 显式 utf-8 解码。目标必须本来就是纯 LF：否则我们"退回 → 复原"这一趟
    # 有可能顺手改掉换行符，而元验证的成功/失败会被这个副作用污染。
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
    befores = {p: digest(p) for p in paths}
    print(f"[步骤0] ✅ {len(CASES)} 处关键实现均为新形态；"
          f"指纹 {[f'{p.name}={d[:8]}' for p, d in befores.items()]}")

    try:
        for i, c in enumerate(CASES, 1):
            path, name = c["file"], str(c["name"])  # type: ignore[index]
            original = texts[path]
            new, old, needle = str(c["new"]), str(c["old"]), str(c["needle"])

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
            out = run_verify()
            hit = needle in out
            print(f"[{i}.2] {'✅' if hit else '❌'} {name}：verify_evidence 报出该失败 = {hit}")
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

        # [4] 全部复原后，指纹必须一致，且整体转绿
        bad = [p for p in originals if digest(p) != befores[p]]
        if bad:
            print(f"[步骤4] ❌ 复原后指纹不一致：{[p.name for p in bad]}")
            return 1
        out2 = run_verify()
        green = ("[未达预期]" not in out2) and ("全部用例符合预期" in out2)
        print(f"[步骤4] {'✅' if green else '❌'} 复原后整体转绿 = {green}")
        for line in out2.splitlines():
            if "全部用例符合预期" in line:
                print(f"          {line.strip()}")
        return 0 if green else 1
    finally:
        for path, original in originals.items():
            if path.read_bytes() != original:
                path.write_bytes(original)
                print(f"[清理] 已把 {path.name} 恢复为原始内容")


if __name__ == "__main__":
    sys.exit(main())
