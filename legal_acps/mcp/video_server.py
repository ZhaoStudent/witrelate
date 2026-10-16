"""视频鉴伪 MCP server 的**入口** —— 协议层复用 `mcp/server.py` 的同一份实现。

启动方式（**必须模块方式**，理由见项目 README 的"一律模块方式跑"）：

    python -m legal_acps.mcp.video_server

════════════════════════════════════════════════════════════════════════
本文件**没有一行协议代码**，这是刻意的
════════════════════════════════════════════════════════════════════════

报文分帧、生命周期、错误码、通知语义、stdout 纪律都在 `server.py` 里，
与模态无关。视频入口要做的只有一件事：把那三个工具包成一个 `ToolFamily`
交给协议层。所以本文件短到可以一眼看完 —— 而"能不能一眼看完"正是判据：

    ⚠️ 一旦本文件开始出现 `is_notification` / `read_message` 这类东西，
       就说明有人把协议抄了第二份。`run/meta_verify_video.py` 里有一条用例
       把 `server.handle_message` 的 `family` 参数化**退回旧形态**
       （写死成音频族），要求视频侧立刻报失败 ——
       也就是说"共用同一份协议"这件事是被钉住的，不是靠读者相信。

════════════════════════════════════════════════════════════════════════
为什么是**两个 server** 而不是一个 server 挂六个工具
════════════════════════════════════════════════════════════════════════

合并会让音频侧那批已经过审的断言必须改动（"`tools/list` 吐出的就是音频
`TOOLS` 本尊"、"返回 3 个工具"、`SERVER_NAME` 含 `audio`），而**改判据让它通过**
是本项目最忌讳的那类修法。拆开则：

  · 音频侧断言**一字不动**（默认族就是音频）；
  · 两个入口各自有独立、可分别声明的服务标识与开场白 ——
    这在 MCP 里本来就是对用户可见的东西（客户端按 server 名区分工具来源）；
  · 代价只有一条，且要**如实说**：想同时用音频与视频，MCP 配置里要写**两项**。

代价写在这里，也写在 `docs/aicomp/视频鉴伪使用说明.md` 里；
不要把"两个入口"说成"一个服务六个工具"。
"""

from __future__ import annotations

from . import server as _server
from . import video_forensics

#: 视频族。**构造期不碰 `legal_acps.video`** ——
#: `video_forensics.TOOLS` 是纯数据（模块导入期就建好），
#: 而真正的检测核心要在工具被调用时才导入（理由见那边模块开头的按需导入纪律）。
#: ⚠️ 传的是**模块本身**（不是 `call_tool` 的引用）—— 理由见 `ToolFamily` 的
#: docstring：绑成引用会让 monkeypatch 静默失效。
FAMILY = _server.ToolFamily(
    name=video_forensics.SERVER_NAME,
    version=video_forensics.SERVER_VERSION,
    instructions=video_forensics.INSTRUCTIONS,
    module=video_forensics,
    entry="legal_acps.mcp.video_server",
    usage=video_forensics.USAGE,
)

__all__ = ["FAMILY", "main"]


def main(argv: list[str] | None = None) -> int:
    return _server.main(argv, family=FAMILY)


if __name__ == "__main__":  # pragma: no cover - 由 `python -m` 触发
    raise SystemExit(main())
