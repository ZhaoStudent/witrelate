"""元验证：`tools/verify_audio_mcp.py` 的断言**不是空转的**。

做法与前几个元验证一致：把每处关键实现**退回旧形态**，要求对应断言立刻报失败。
判据只有一条：**退回后仍然语法正确**（否则证明不了任何事）。

════════════════════════════════════════════════════════════════════════
为什么 MCP 这一层也需要元验证
════════════════════════════════════════════════════════════════════════

这一层的缺陷与检测层完全不同：它**不改变报告，它改变"能不能用"**。

最典型的是 C1：`notifications/initialized` 是客户端**每次**都会发的第一条通知。
如果把它当成请求回一条 `id: null` 的应答，多数客户端会报 "unexpected response"，
现象是"这个服务器用不了" —— 而服务端日志一片正常。这类缺陷**只能靠协议语义断言抓**，
读代码看不出来（`"id" not in message` 与 `message.get("id") is None` 只差几个字符，
而且后者在 SQL 式的直觉里更"自然"）。

**处数不写在这里**（下面 `CASES` 里逐条列着；写死的数字迟早与 `len(CASES)` 分家，
而跑步时打印的是**现场派生**的那个）。逐条对应：

  C1. 通知的判据从「`id` 这个键在不在」退回「`id` 是不是 `None`」
      ⇒ `{"id": null, "method": "ping"}` 被当成通知、**一条应答都没有**。
      规范允许 null id，那种报文是**请求**；而真正该沉默的通知恰好会因此走另一条路，
      于是"服务器用不了"与"服务器很安静"在日志里长得一模一样。

  C2. 版本协商退回「一律回我们最新版」
      ⇒ 客户端按**更高版本**的语义解析我们的应答，而我们其实没实现那些语义。
      这类错在客户端换版之后才会显形，而且表现为"某些字段没了"。

  C3. 工具级失败退回走 JSON-RPC 错误码
      ⇒ 调用方（模型）收到的是客户端渲染的一句通用「tool failed」。
      而这一层存在的**唯一理由**就是"让模型读到为什么不行、可以怎么改"。

  C4. `_audio_module()` 的导出面少一个符号（静默变 `None`）
      ⇒ 不在导入期出声，要到调用那一刻才炸；而调用它的地方是
      `except Exception` 兜底的边界层 ⇒ 只剩一句"内部错误"，没有指向真正的原因。
      （这一处的真实前身更隐蔽：`enf_track` 只存在于子模块里，
        `legal_acps.audio.enf_track` 取不到 —— 一个"依赖导入顺序"的导出。）

  C5. `_HANDLERS` 少一项（`TOOLS` 里还有它）
      ⇒ `tools/list` 会把它列出去，`tools/call` 调它却失败。
      "列表里有、调不到"是最容易被当成"客户端的问题"的一种缺陷。

  C6. 往 stdout 写一行日志
      ⇒ MCP 的 stdio 传输里 **stdout 是协议专用**。多一个字节不是"多一行日志"，
      而是客户端解析失败、整个 server 变砖 —— 而现场看不出原因。

  C7. 带 `id` 的 `notifications/*` 被当成通知吞掉
      ⇒ 判据「`id` 这个键在不在」**对两个方向都成立**，而原先那一支是无条件返回
      `None` 的 ⇒ 那种报文一条应答都拿不到，客户端一直等。它与 C1 是同一个判据的
      两面：C1 是"该回应的被当成通知"，C7 是"该沉默的判据被用在请求上"。

  C8. 通知里出错也回一条 `id: null` 的错误
      ⇒ 客户端把它当成"一条应答不上的请求"。**成功与失败都不该有应答，才叫通知**；
      只把成功那一半管住的话，故障时机的表现恰好是"服务器用不了"。

  C9. `base_hz` 不校验 `enum`
      ⇒ schema 里的 `enum` 不是所有客户端都会执行，而收下 123 会照它算一条
      **没有科学含义**的谐波梳（123/246/369/492 Hz），再如实报"未检出" ——
      调用方读到的结论是"没有市电痕迹"。这是"收下了却做了另一件事"。

  C10. 没解出波形时一概归因成「没有解码器」
      ⇒ "没解出波形"至少三种原因（根本不是音频 / WAV 头坏了 / 真的压缩格式），
      三种的补救动作完全不同。归错因会让调用方白折腾一遍转格式，
      而真正要改的是他送进来的东西 —— 溯源字段不许含糊。
"""

from __future__ import annotations

import contextlib
import hashlib
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SERVER = ROOT / "legal_acps" / "mcp" / "server.py"
AUDIO_FORENSICS = ROOT / "legal_acps" / "mcp" / "audio_forensics.py"
AUDIO_PIPELINE = ROOT / "legal_acps" / "audio" / "pipeline.py"

TARGETS = {
    "verify_audio_mcp": "tools.verify_audio_mcp",
}

#: 目标脚本里"这一条没通过"的标记。
#: ⚠️ 必须按目标分别写：`verify_audio_mcp` 用的是 `[未达预期]`，
#: 而 `check_local` 用的是 `[失败]` —— 统一判一个词会让步骤 0 / 步骤 4 的
#: 绿判定失效，于是"退回后报失败"这件事**根本无从证明**（前一个元验证踩过）。
FAIL_MARKERS = {
    "verify_audio_mcp": ("[未达预期]", "[失败]"),
}

CASES = [
    {
        "name": "通知的判据退回「id 是不是 None」（⇒ id=null 的请求被吞掉）",
        "file": SERVER,
        "new": '    is_notification = "id" not in message\n',
        "old": '    is_notification = message.get("id") is None  # 退回旧形态\n',
        # ⚠️ 这一句里含 ASCII 双引号，所以整条用**单引号**字符串写。
        "needle": '`{"id": null}` 是**请求**、必须回应',
    },
    {
        "name": "版本协商退回「一律回最新版」（⇒ 客户端按更高版本的语义解析）",
        "file": SERVER,
        "new": ("    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:\n"
                "        return requested\n"),
        "old": ("    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:\n"
                "        return DEFAULT_PROTOCOL_VERSION  # 退回旧形态：一律回最新版\n"),
        "needle": "端到端：`initialize` 协商回客户端那一版 2024-11-05",
    },
    {
        "name": "工具级失败退回走协议错误码（⇒ 模型只看到一句通用的 tool failed）",
        "file": SERVER,
        # ⚠️ 2026-10-04 更新锚点：协议层改成按「工具族」分派后，这一行从
        #    `audio_forensics.call_tool(...)` 变成了 `family.module.call_tool(...)`
        #    （并拆成两行）。**语义没变**（工具级失败仍然走 `result.isError`），
        #    变的是它的**文本形状** —— 而锚点断的恰恰是形状。
        #    这件事是 `run/check_meta_residue` 抓出来的：音频判据全绿、残留核对红了。
        #    教训：**改一处被多方引用的代码，必须重跑残留核对**，
        #    否则"判据全绿"会掩盖"锚点已失效"（后者会让元验证从此空转）。
        "new": ('            reply = _result(msg_id, family.module.call_tool(\n'
                '                params["name"], params.get("arguments")))\n'),
        "old": ('            reply = _error(msg_id, INTERNAL_ERROR, "工具执行失败。")'
                '  # 退回旧形态：工具失败走协议错误码\n'),
        "needle": "未知**工具名**走 `result.isError=true`",
    },
    {
        "name": "导出面少一个符号、静默变 None（⇒ 到调用那一刻才炸，只剩一句「内部错误」）",
        "file": AUDIO_FORENSICS,
        "new": "        enf_track=_forensics.enf_track,\n",
        "old": "        enf_track=None,  # 退回旧形态：导出面少一个符号时静默变 None\n",
        "needle": "`_audio_module()` 显式给出**列明依赖面**的命名空间",
    },
    {
        "name": "处理器表少一项（⇒ 列表里有、调不到）",
        "file": AUDIO_FORENSICS,
        "new": '    "audio_enf_track": _handle_enf_track,\n',
        "old": '    # "audio_enf_track": _handle_enf_track,  # 退回旧形态：列表里有、调不到\n',
        "needle": "`TOOLS` 与处理器表**一一对应**",
    },
    {
        "name": "往 stdout 写一行日志（⇒ 客户端解析失败、整个 server 变砖）",
        "file": SERVER,
        # ⚠️ 退回形态刻意写成 `raise SystemExit(serve())` 而不是
        #    "先 print 再 return serve()"：元验证要求**替换后旧形态里不含新形态**
        #    （`new in broken` 即判失败）。把原行留在 `old` 里会让这条前置检查挂掉，
        #    而它报出来的话是"替换未按预期发生" —— 看起来像锚点写错了，
        #    实际是"旧形态包含了新形态"。两种语义完全等价（退出码都来自 serve()）。
        # ⚠️ 2026-10-04 更新锚点：`main()` 现在要把 `family` 透传给 `serve()`，
        #    所以原行 `return serve()` 变成了 `return serve(family=family)`。
        #    退回形态同步跟着改（不然它连语法都过不去，元验证会在 [i.1] 就拦住）。
        #    同一条教训见上面那个用例。
        "new": "    return serve(family=family)\n",
        "old": ('    print("starting", flush=True)  # 退回旧形态：往 stdout 写日志\n'
                "    raise SystemExit(serve(family=family))\n"),
        "needle": "**stdout 只有协议消息**",
    },
    {
        # `{"id": null}` 是请求、带 id 的 notifications/* 也是请求 —— 判据对两个
        # 方向都成立。原先 `notifications/` 那一支无条件返回 None，于是带 id 的那种
        # 报文**一条应答都拿不到**，而它自己文档里写的判据恰好相反。
        "name": "带 `id` 的 `notifications/*` 被当成通知吞掉（⇒ 客户端一直等一条应答）",
        "file": SERVER,
        "new": '    if method.startswith("notifications/") and is_notification:\n',
        "old": '    if method.startswith("notifications/"):  # 退回旧形态：带 id 也不回应\n',
        "needle": "带 `id` 的 `notifications/*` 是请求",
    },
    {
        # 通知出错时回一条 `id: null` 的错误 —— 正是本文件开头那条规则要防的事
        # （客户端把它当成"应答不上的请求"）。成功与失败都不该有应答，才叫通知。
        "name": "通知里出错也回一条 `id: null` 的错误（⇒ 客户端报 unexpected response）",
        "file": SERVER,
        "new": '            if isinstance(message, dict) and "id" not in message:\n',
        "old": '            if False:  # 退回旧形态：通知出错也回一条 id:null 的错误\n',
        "needle": "通知（无 `id`）里工具实现崩了",
    },
    {
        # schema 里写着 `enum: [50, 60]`，但 JSON Schema 的 enum 不是所有客户端都会执行。
        # 收下 123 的后果是照它算一条**没有科学含义**的谐波梳，再如实报"未检出"——
        # 调用方读到的结论却是"没有市电痕迹"。
        "name": "`base_hz` 不校验 enum（⇒ 照一个不存在的电网频率找一通，然后报「未检出」）",
        "file": AUDIO_FORENSICS,
        "new": "        if float(raw_base) not in _ENF_BASES:\n",
        "old": "        if False:  # 退回旧形态：不校验 enum，来者不拒\n",
        "needle": "`base_hz` 只能是 50 / 60",
    },
    {
        # `enf_track(wave=None)` 只有一句归因（"压缩 / 带封装的格式、没有解码器"），
        # 而"没解出波形"至少有三种原因（根本不是音频 / WAV 头坏了 / 真的压缩格式）。
        # 归错因会让调用方去转格式，而真正要改的是他送进来的东西。
        "name": "没解出波形时一概归因成「没有解码器」（⇒ 调用方照着错误的归因改）",
        "file": AUDIO_FORENSICS,
        "new": "    if decoded.waveform is None:\n",
        "old": "    if False:  # 退回旧形态：沿用那句「压缩 / 带封装的格式、没有解码器」\n",
        "needle": "WAV 头坏了（没有 `fmt` 块）",
    },
    {
        # 🔴 2026-10-05 的 B2：**那句话被手抄了两份**（检测层拼一次、MCP 抄一次）。
        #    原形态与"只有一份"在**文本上逐字相同** —— 所以"正文里有这句话"、
        #    "两条路一致"这类判据**全绿**。只有**哨兵**（把检测层那句话临时换掉、
        #    要求 MCP 出口跟着变）能把它抓出来：手抄的实现不会跟着变。
        #    ⚠️ 退回形态必须是**一字不差**的那一句 —— 抄得不一样的话，
        #    被"两条路文本一致"那条断言就抓到了，证明不了哨兵的价值。
        "name": "MCP 里**手抄**一份原因（且抄得一字不差 ⇒ 只有哨兵判据抓得到）",
        "file": AUDIO_FORENSICS,
        "new": "        reason = core.non_audio_clause(str(decoded.media_type))\n",
        "old": ('        reason = f"不是可识别的音频容器（按魔术字节判定为 '
               '{str(decoded.media_type)}）"  # 退回旧形态：手抄一份\n'),
        "needle": "行为化**的单一来源证明①",
    },
    {
        # 🔴 同一处的**第二个**手抄：`pipeline` 那条**路由关键词**。
        #    它坏了**不报错** —— 后果是「你给的不是音频」被答成兜底那句
        #    「请提交完整未截断的原始音频字节」，调用方照着错误的归因去改。
        #    与"那句话"不同源，所以**必须**另开一条用例（一处实现两种缺陷 ⇒ 两条）。
        "name": "路由关键词退回手写字面量（⇒ 改句子会静默改丢路由，而正文看起来正常）",
        "file": AUDIO_PIPELINE,
        "new": "    return _forensics.NON_AUDIO_CLAUSE_PREFIX\n",
        "old": "    return \"不是可识别的音频容器\"  # 退回旧形态：手写字面量\n",
        "needle": "行为化**的单一来源证明②",
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


def is_green(which: str, code: int, out: str) -> bool:
    return code == 0 and not any(m in out for m in FAIL_MARKERS[which])


def digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def install_restore_guard(originals: dict[Path, bytes]) -> None:
    """收到 SIGTERM / SIGINT / SIGHUP 时也要先把文件还原再退出。

    ⚠️ Windows 上被强杀时挂不上 `signal`（进程直接没了，不做栈展开）⇒
    文件会**静静留在旧形态**，而它看起来完全正常、照样能跑。
    所以真正可靠的判据不是这个守卫，而是**跑完之后的残留核对**：
    `python -m tools.tree_diff diff` 与 `python -m run.check_meta_residue`。
    本函数只是第一道防线。
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
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handler)


def main() -> int:
    # ⚠️ 行缓冲：被掐断时也必须看得见"跑到第几条了"。
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

    paths = {c["file"] for c in CASES}  # type: ignore[misc]
    originals = {p: p.read_bytes() for p in paths}
    install_restore_guard(originals)

    # 读字节 → 显式 utf-8 解码。目标必须本来就是纯 LF：否则"退回 → 复原"这一趟
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
            print(f"[步骤0] ❌ 前置不成立：{c['name']} 不是预期的'新形态'"
                  f"（new×{text.count(c['new'])}、old 出现={c['old'] in text}）")
            return 1

    # [0b] 前提：目标当前必须是绿的 —— 否则"退回后报失败"什么都证明不了。
    for which in TARGETS:
        code, out = run_target(which)
        green = is_green(which, code, out)
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
            code, out = run_target("verify_audio_mcp")
            hit = needle in out
            print(f"[{i}.2] {'✅' if hit else '❌'} {name}："
                  f"tools.verify_audio_mcp 报出该失败 = {hit}（退出码 {code}）")
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

        # [4] 全部复原后，指纹必须一致，且目标整体转绿
        stale = [p for p in originals if digest(p) != befores[p]]
        if stale:
            print(f"[步骤4] ❌ 复原后指纹不一致：{[p.name for p in stale]}")
            return 1
        all_green = True
        for which in TARGETS:
            code, out = run_target(which)
            green = is_green(which, code, out)
            all_green = all_green and green
            print(f"[步骤4] {'✅' if green else '❌'} 复原后 {TARGETS[which]} 转绿 = {green}")
            if not green:
                print(out[-2000:])
        print()
        print("⚠️ 以上只证明「在跑的时候还原过」。**真正的判据是跑完之后**：")
        print("   ① `python -m tools.tree_diff save` 在跑之前记全树指纹")
        print("   ② `python -m tools.tree_diff diff` 在跑完之后比对（**不 diff 等于没跑过**）")
        print("   ③ `python -m run.check_meta_residue` 核对每处锚点都在新形态上")
        return 0 if all_green else 1
    finally:
        for path, original in originals.items():
            if path.read_bytes() != original:
                path.write_bytes(original)
                print(f"[清理] 已把 {path.name} 恢复为原始内容")


if __name__ == "__main__":
    sys.exit(main())
