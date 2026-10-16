"""**全真模拟**：真实模型 + 真实视频附件 + 真实工具链路，全量打印输入输出。

为什么单写一个脚本：离线断言（`tools/verify_video.py`）验的是"判据对不对"，
验不到"这一跳接起来是什么样子"。这里要做的是把**从字节进来到结论出去**
的每一环都打出来，给人逐段看 —— 尤其是模型实际收到的是什么
（是 `VideoPart`？还是降级后的"一句说明 + 一帧图"？）。

用法（在 witrelate 目录下，需真实模型凭据）：

    ./.venv/bin/python -m run.e2e_video_live            # 跑三个用例
    ./.venv/bin/python -m run.e2e_video_live --role evidence-leader   # 跑两跳

⚠️ 它**不启动服务**（`runtime.run` 是进程内跑），所以不需要注册 / 证书；
   委派那一跳（evidence-leader → evidence-video）走的是真实 ACPs 通道，
   要求 evidence-video 已在另一端跑起来且能被 discovery 检索到。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys

from dotenv_lite import load_env

load_env()

from wit import TextPart, VideoPart, WitRuntime  # noqa: E402

from legal_acps import run_ledger  # noqa: E402
from legal_acps.acps_config import build_config  # noqa: E402
from legal_acps.agents import build_agent  # noqa: E402
from legal_acps.llm import build_model  # noqa: E402
from legal_acps.run_agent import _active_aic  # noqa: E402
from legal_acps.token_budget import truncated_of  # noqa: E402

# ⚠️ `run.samples` / `run.video_samples` **刻意不放在模块级**：它们会拉 numpy + Pillow，
#    而本模块要能被 `legal_acps.check_local` import 进来 —— 3c 节用它的 `run_probe()`
#    行为断言"探针这一轮真的记了台账"。自检为了这一条把重依赖引进自己的进程不划算
#    （同 `legal_acps/evidence/__init__.py` 用 `if TYPE_CHECKING:` 挡重依赖的理由）。
#    ⚠️ 挪回模块级**不会报错**，只会让自检在缺 Pillow 的机器上起不来 ⇒ 由 3c 断言盯着。

BAR = "─" * 70


def section(title: str) -> None:
    print()
    print(BAR)
    print(f"  {title}")
    print(BAR)


def make_inputs() -> dict[str, tuple[bytes, str, str]]:
    """造三种**真**输入：返回 `{名字: (字节, MIME, 怎么造出来的)}`。"""
    from run import samples as IMG  # 惰性：见文件顶部关于 numpy/Pillow 的说明
    from run import video_samples as V  # noqa: F401

    # ① 真编码的动画 GIF（Pillow 真编码器产出，不是手拼的块结构）
    gif = V.rasterizable_gif(3, size=(160, 120))

    # ② MJPEG / MP4：容器是真的 MP4，帧是真的 JPEG（真编码器产出）
    jpegs = [
        IMG.jpeg(IMG.photo(name="structured", w=160, h=120), quality=88)
        for _ in range(3)
    ]
    mjpeg = V.motion_jpeg_mp4(jpegs)

    # ③ 合成 avc1 MP4：代表**日常最常见的 H.264 —— 本进程解不出帧**，
    #    也就是"帧层整层没跑"这个常态路径。
    h264 = V.clean_mp4()

    return {
        "真动画 GIF": (gif, "video/gif", "Pillow 真编码器产出，3 帧，160×120"),
        "真 MJPEG/MP4": (mjpeg, "video/mp4",
                         "真 MP4 容器 + 3 张真 JPEG（取自 run/samples.py）"),
        "合成 avc1（= 日常 H.264 形态）": (
            h264, "video/mp4", "合成容器，codec=avc1 —— 本进程解不出帧的那一类"),
    }


def dump_input(name: str, data: bytes, mime: str, how: str) -> None:
    section(f"输入 · {name}")
    print(f"  来源          ：{how}")
    print(f"  字节          ：{len(data)} 字节")
    print(f"  sha256        ：{hashlib.sha256(data).hexdigest()}")
    print(f"  声明 MIME     ：{mime}")
    print(f"  前 24 字节    ：{data[:24].hex(' ')}")


def dump_messages(messages: list[object]) -> None:
    """逐条打印消息的**部件构成**。

    ⚠️ wit（v2.1.0）里**没有** `ToolCallPart` / `ToolResultPart` 这两个类 ——
    工具回执是 `role="tool"` 消息里的 **`TextPart`**。早先按"有没有 ToolResultPart"
    判断"模型调没调工具"，在 wit 上是恒假的判断（死分支），已经在 `dump_tool_text`
    上造成过一次假阴性。这里改成**以 role 为准**并显式标注工具回执。
    """
    for index, message in enumerate(messages, 1):
        role = getattr(message, "role", "?")
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            content = [content]
        bits: list[str] = []
        for part in content:
            if part is None:
                continue
            kind = type(part).__name__
            text = getattr(part, "text", None)
            if kind == "TextPart" and text:
                label = "工具返回" if role == "tool" else "Text"
                bits.append(f"{label}({len(text)} 字)")
            elif kind in ("ImagePart", "AudioPart", "VideoPart"):
                payload = getattr(part, "data", None) or b""
                bits.append(
                    f"{kind}(mime={getattr(part, 'mime_type', '?')}, "
                    f"{len(payload)} 字节)"
                )
            elif kind == "ReasoningPart":
                bits.append(f"Reasoning({len(text or '')} 字)")
            elif kind in ("ToolCallPart", "ToolResultPart"):  # wit 无此二者，兼容他版
                bits.append(kind)
            else:
                bits.append(kind)
        print(f"  [{index:02d}] {role:<10} {' + '.join(bits)}")


def dump_tool_text(messages: list[object]) -> None:
    """把工具返回的**正文**完整打出来（那是读者真正要看的结论）。

    ⚠️ **本条修的是一个真缺陷（2026-10-03）**：原实现只认 `ToolResultPart`，
    而 wit 的工具回执是 `role="tool"` 消息里的 `TextPart` ⇒ 三次全真模拟里
    工具明明各返回了 5000+ 字，这里却打印「模型没调工具，这一跳等于没干活」。
    这是**诊断报假阴性**：会让复核方得出与事实相反的意见（"工具没被调用"）。
    现在以 `role == "tool"` 为准（同时兼容他版的 ToolResultPart）。
    """
    printed = 0
    for message in messages:
        role = getattr(message, "role", "")
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            content = [content]
        for part in content:
            kind = type(part).__name__
            if role != "tool" and kind != "ToolResultPart":
                continue
            raw = getattr(part, "content", None)
            if raw is None:  # 普通 TextPart：正文就在 .text
                raw = getattr(part, "text", None)
            if raw is None:
                continue
            printed += 1
            if isinstance(raw, list):
                for bit in raw:
                    text = getattr(bit, "text", None)
                    if text:
                        print(text)
            elif isinstance(raw, str):
                print(raw)
            else:
                text = getattr(raw, "text", None)
                print(text or str(raw))
    if not printed:
        print("  （本次确实没有工具返回 —— 模型没调工具，这一跳等于没干活）")


async def run_probe(
    runtime: object,
    parts: list[object],
    task: str,
    *,
    role: str,
    wit_dir: str | None = None,
):
    """跑这一跳 —— **必须经发起轮台账**，返回 `(run 结果, 台账记录)`。

    ⚠️ `role` 是**必填**而不是默认 "leader"：本脚本可以跑 `--role evidence-video`
       这类**叶子**角色（默认值就是它），而台账是按角色分文件的
       （`.wit/<role>/runs.jsonl`）。给个默认值就等于替调用方猜了一个归属 ——
       那正是台账要消灭的东西。**角色错了不会报错**，只会把这一轮记到别人的文件里。

    ⚠️ 单拎成可 import 的函数，是为了让 `legal_acps.check_local` 3c 塞一个假 runtime
       进来**行为断言**"探针这一跳真的记了台账"（原实现直接 `runtime.run(parts)`，
       跑完台账里什么都没有）。判据不能退化成 grep 源码 —— 本项目写死过这条：
       "grep 到了"并不等于"运行时真会走那条路"。
    """
    return await run_ledger.run_ledgered(
        lambda: runtime.run(parts),
        text=task,
        label=run_ledger.PROBE_LABEL,
        role=role,
        wit_dir=wit_dir,
    )


async def run_case(role: str, name: str, data: bytes, mime: str, how: str,
                   *, show_full: bool) -> int:
    """跑一个用例，返回 `0`（通过）/ `1`（失败）。

    ⚠️ 返回值**必须**被 `main()` 汇总进退出码：判据不进退出码 = 没有判据
    （本项目写死过这条）。这里唯一进退出码的是判据 **T**（见文件末尾）。
    """
    dump_input(name, data, mime, how)

    agent = build_agent(role, build_model(), self_aic=_active_aic(role))
    config = build_config(role=role)
    runtime = WitRuntime(agent, workspace=f".wit/{role}", acps=config)

    task = (
        "随本任务附带一段视频，请对它做一次视频鉴伪初检，"
        "并如实说明你看不到什么、结论能用到什么程度。"
    )
    parts = [TextPart(text=task), VideoPart(mime_type=mime, data=data)]

    section(f"输出 · {name}（角色 {role}，真实模型）")
    try:
        result, ledger_run = await run_probe(runtime, parts, task, role=role)
    except Exception as exc:  # noqa: BLE001 - 全真模拟要把失败也打出来给人看
        print(f"  ❌ run 抛异常：{type(exc).__name__}: {exc}")
        return 1

    print(
        f"  [台账] run_id = {ledger_run.run_id}  label = {ledger_run.label}"
        f"  pid = {ledger_run.pid}（.wit/{role}/runs.jsonl，只增不改）"
    )
    print(f"  status={result.status}  迭代={result.iterations}")
    dump_messages(result.messages)
    if show_full:
        section(f"工具返回全文 · {name}")
        dump_tool_text(result.messages)

    section(f"最终答复 · {name}")
    # ⚠️ `output_parts` 里**同时含 ReasoningPart**（它也有 `.text`）——
    #    不区分就会把模型的**内部思考**当成最终答复打出来（读者会把草稿当结论）。
    #    只看 `TextPart`；思考另起一段，标明它是思考、不是答复。
    answered = False
    for part in result.output_parts:
        if type(part).__name__ != "TextPart":
            continue
        text = getattr(part, "text", None)
        if text:
            answered = True
            print(text)
    if not answered:
        print("  （本次没有文字答复 —— 见上面的部件构成）")
    thoughts = [getattr(p, "text", "") for p in result.output_parts
                if type(p).__name__ == "ReasoningPart" and getattr(p, "text", None)]
    if thoughts:
        section(f"（附）模型思考原文 · {name} —— 不是结论，仅供排查")
        print("\n".join(thoughts))

    # ── 判据 T：这一轮**有没有被 token 上限截断** ──────────────────────────
    #    与音频 / 图像探针同一条（2026-10-06 真机夹出）：响应被
    #    `finish_reason=length` 截断 ⇒ 没有正文、没有工具调用 ⇒ 1 轮即
    #    `completed`、**零产物**，而它与"跑完的一轮"长得一模一样。
    #    ⚠️ 三态：`0` 干净 / `n>0` 截断 n 次 / `-1` **无法判定**；只有 `0` 通过。
    truncated = truncated_of(result)
    mark = "✅" if truncated == 0 else "❌"
    tail = (
        "留痕显示 0 次 `finish_reason=length`"
        if truncated == 0
        else f"**被截断 {truncated} 次** ⇒ 模型没有额度把话说完（零产物的经典成因）"
        if truncated > 0
        else "**无法判定**（拿不到 `result.state.metadata`）⇒ 跳过 ≠ 通过"
    )
    print(f"  [{mark}] T｜{name}｜这一轮没有被 token 上限截断：{tail}")
    return 0 if truncated == 0 else 1


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", default="evidence-video")
    parser.add_argument("--only", default="")
    parser.add_argument("--brief", action="store_true", help="不打工具返回全文")
    args = parser.parse_args()

    inputs = make_inputs()
    if args.only:
        inputs = {k: v for k, v in inputs.items() if args.only in k}
        if not inputs:
            print(f"没有匹配 --only={args.only!r} 的用例", file=sys.stderr)
            return 2

    print("=" * 70)
    print("  全真模拟 · 视频鉴伪（真实模型 / 真实字节 / 真实工具）")
    print(f"  角色：{args.role}   用例：{len(inputs)} 个")
    print("=" * 70)

    bad = 0
    for name, (data, mime, how) in inputs.items():
        bad += 1 if await run_case(args.role, name, data, mime, how,
                                   show_full=not args.brief) else 0
    if bad:
        print(f"\n  ❌ {bad}/{len(inputs)} 个用例判失败（判据 T：token 截断）")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
