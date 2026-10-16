"""本机 MCP 入口的**接线判据**：配置写对了没有 + **真的起得来吗**。

为什么要有这个文件
------------------
`docs/aicomp/{音频,视频}鉴伪使用说明.md` 的 §2 一直写着「在客户端的 MCP 配置里加一项」，
而**实际配置里一个都没有**（2026-10-05 实测：`~/.workbuddy/mcp.json` 不存在）。
文档承诺 ≠ 接了线 —— 这是本项目那条「装了 ≠ 会响」的**配置版**：
配置文件的语法错误、路径写错、少了 `cwd`，全都**不会立刻报错**，
一直到某次真的调用失败为止。

所以这里判两件事，**缺一不可**：

  ① **配置与文档逐字一致**（文档是「同一判定写两处」的第三处，没有锚点盯着它）；
  ② **那条命令行真的能起**（真跑一次 stdio 握手，`initialize` + `tools/list`）。

第 ② 条是唯一有说服力的判据：只校验 JSON 结构与路径存在，
挡不住「模块名写错」「少了 cwd」「裸 python 起不来」这些最常踩的坑。

退出码：0 全过 / 1 判据失败 / 3 环境不成立（配置文件不存在等）。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 客户端配置的**唯一**位置。可用第一个命令行参数覆盖（探针用）。
DEFAULT_CONFIG = Path.home() / ".workbuddy" / "mcp.json"

#: 两处文档里各写了一段示例配置。**现场从文档里抄**，不在这里再手写一遍 ——
#: 手写 = 第三份事实源。
DOC_SNIPPETS = (
    ROOT / "docs" / "aicomp" / "音频鉴伪使用说明.md",
    ROOT / "docs" / "aicomp" / "视频鉴伪使用说明.md",
)

#: 期望的两个键，以及启动后 `tools/list` 至少要吐出的工具名。
EXPECTED = {
    "legal-acps-audio-forensics": ("legal_acps.mcp.server", "audio"),
    "legal-acps-video-forensics": ("legal_acps.mcp.video_server", "video"),
}

_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.S)

_problems: list[str] = []

#: 负对照期间置 True：**只收集、不打印**。
#: ⚠️ 否则一次"通过"的运行里会夹着两行 `[失败]` —— 读的人会以为整轮挂了，
#:    这正是本项目那条「报告器必须把结论分开报」要防的事。
_quiet = False


def fail(msg: str) -> None:
    _problems.append(msg)
    if not _quiet:
        print(f"[失败] {msg}")


def _doc_entries() -> dict[str, dict]:
    """从两处使用说明里现场抽出示例配置的条目（逐段解析，不抄字面）。"""
    found: dict[str, dict] = {}
    for doc in DOC_SNIPPETS:
        if not doc.is_file():
            fail(f"找不到使用说明：{doc}")
            continue
        text = doc.read_text(encoding="utf-8")
        blocks = _JSON_BLOCK.findall(text)
        if not blocks:
            fail(f"{doc.name} 里一段 ```json 示例都没有 —— 文档改了写法而这里没跟着改")
            continue
        for raw in blocks:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                fail(f"{doc.name} 里的示例配置**不是合法 JSON**：{exc}")
                continue
            for name, entry in (parsed.get("mcpServers") or {}).items():
                found[name] = entry
    return found


def check_shape(cfg: dict, doc_entries: dict[str, dict]) -> None:
    servers = cfg.get("mcpServers")
    if not isinstance(servers, dict):
        fail("配置里没有 `mcpServers` 这个 dict（客户端就读不到任何 server）")
        return

    for name, (module, word) in EXPECTED.items():
        if name not in servers:
            fail(f"缺少条目 `{name}` —— 两条入口**刻意不合并**，缺一条就等于少了一整个模态")
            continue
        entry = servers[name]

        # ① 与文档逐字一致（只比文档里出现过的键，允许配置多写别的键）
        if name in doc_entries:
            for key, want in doc_entries[name].items():
                got = entry.get(key)
                if got != want:
                    fail(f"`{name}.{key}` 与使用说明 §2 不一致：配置={got!r} 文档={want!r}")
        else:
            fail(f"使用说明 §2 里没有 `{name}` 这一段 —— 文档与配置各说各话")

        # ② command 必须是**绝对路径**且指向真实文件（文档三条「必须」的第 1 条）
        cmd = entry.get("command", "")
        if not Path(cmd).is_absolute():
            fail(f"`{name}.command` 不是绝对路径：{cmd!r}（裸 `python` 会在 import pydantic 那步崩）")
        elif not Path(cmd).is_file():
            fail(f"`{name}.command` 指向的文件不存在：{cmd}")

        # ③ 必须是**模块方式**启动，不能是 `python 某个.py`
        args = entry.get("args") or []
        if args[:2] != ["-m", module]:
            fail(f"`{name}.args` 不是模块方式启动：{args!r}（应为 ['-m', {module!r}]）")
        if any(str(a).endswith(".py") for a in args):
            fail(f"`{name}.args` 里出现了 `.py` 文件路径：{args!r} ⇒ 会 ModuleNotFoundError")

        # ④ cwd 必须是**项目根**：判据是"那里真的有个 legal_acps 包"，不是字符串相等
        cwd = entry.get("cwd")
        if not cwd:
            fail(f"`{name}` 缺 `cwd` —— `legal_acps` 是相对包名，别的目录下 `-m` 找不到它")
        elif not (Path(cwd) / "legal_acps").is_dir():
            fail(f"`{name}.cwd` 下没有 `legal_acps` 包目录：{cwd}")


def handshake(name: str, entry: dict) -> None:
    """真起一次 server，走完 initialize + tools/list。**这是唯一有说服力的判据。**"""
    proc = subprocess.Popen(  # noqa: S603 - 命令行来自我们自己的配置
        [entry["command"], *entry.get("args", [])],
        cwd=entry.get("cwd"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def rpc(obj: dict) -> dict:
        assert proc.stdin and proc.stdout
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server 还没应答就把 stdout 关了")
        return json.loads(line)

    try:
        init = rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "verify_mcp_config", "version": "1"},
                },
            }
        )
        got_name = ((init.get("result") or {}).get("serverInfo") or {}).get("name")
        assert proc.stdin
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t.get("name") for t in ((tools.get("result") or {}).get("tools") or [])]
    finally:
        if proc.stdin:
            proc.stdin.close()  # 读到 EOF 会正常退出
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = -1
        err = (proc.stderr.read() if proc.stderr else "") or ""

    if got_name != name:
        fail(f"`{name}` 起来后自报的名字是 {got_name!r} —— 两个入口串了（等于把音频入口当视频用）")
    if not names:
        fail(f"`{name}` 的 tools/list 是空的（stderr 尾部：{err.strip()[-200:]!r}）")
    if code != 0:
        fail(f"`{name}` 关掉 stdin 后退出码是 {code}（应为 0 —— 读到 EOF 是正常结束）")

    label = "音频" if "audio" in name else "视频"
    print(f"  ✅ {name:32s} 名字={got_name!r} {len(names)} 个工具：{'、'.join(names)}")


def negative_control(cfg: dict, doc_entries: dict[str, dict]) -> None:
    """负对照：把 command 换成裸 `python`，**结构判据必须报失败**。

    ⚠️ 没有这一条，上面那些"通过"可能只是因为判据对什么都点头。
    ⚠️ 这里**只跑结构判据、不起子进程** —— 第一版让它再走一遍 `main()`，
    结果那条假配置里的 `command` 是裸 `python`，脚本真的去 spawn 它然后卡住
    （整轮被 SIGTERM 打断，连表头都被 stdout 缓冲吞掉了）。探测负对照
    **不应该引入新的副作用**。
    """
    import copy

    bad = copy.deepcopy(cfg)
    bad["mcpServers"]["legal-acps-audio-forensics"]["command"] = "python"
    global _problems, _quiet
    keep, _problems, was_quiet = _problems, [], _quiet
    try:
        _quiet = True
        check_shape(bad, doc_entries)
        hit = list(_problems)
    finally:
        _problems, _quiet = keep, was_quiet
    if not hit:
        fail("负对照没响：把 command 换成裸 `python` 之后结构判据仍然全过 ⇒ 这条判据对什么都点头")
    else:
        print(f"  ✅ 负对照：裸 `python` 被判失败（{len(hit)} 条）—— 判据分得开，不只是会响")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cfg_path = Path(argv[0]) if argv else DEFAULT_CONFIG

    print("=" * 68)
    print("本机 MCP 入口接线判据")
    print("=" * 68)
    print(f"配置文件：{cfg_path}")
    if not cfg_path.is_file():
        print(f"[跳过] 配置文件不存在 —— 这本身就是要修的事（文档 §2 承诺了它）：{cfg_path}")
        return 3

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    doc_entries = _doc_entries()
    print(f"文档里抽出的条目：{sorted(doc_entries)}")
    print("-" * 68)

    before = len(_problems)
    check_shape(cfg, doc_entries)
    print(f"  ① 结构 / 路径 / 模块方式 / cwd：{'通过' if len(_problems) == before else '见上'}")
    print("  ② 真跑一次 stdio 握手：")
    for name in EXPECTED:
        if name in (cfg.get("mcpServers") or {}):
            try:
                handshake(name, cfg["mcpServers"][name])
            except Exception as exc:  # noqa: BLE001 - 探针失败也要报出来，不是崩
                fail(f"`{name}` 握手失败：{type(exc).__name__}: {exc}")

    print("  ③ 负对照：")
    negative_control(cfg, doc_entries)

    print("-" * 68)
    if _problems:
        print(f"结论：**未通过**（{len(_problems)} 条）")
        for p in _problems:
            print(f"  · {p}")
        return 1
    print("结论：**通过** —— 两条入口与文档一致，且都真的起得来。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
