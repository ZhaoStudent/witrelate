"""离线探针：`RunConfig(max_steps=N)` 到底能不能把步数预算调上去？

背景：项目注释说"服务模式 max_steps=8 是钉死的、Agent 侧无法调大"。
      但 `WitRuntime.run(input, config: RunConfig | None)` 这个签名暗示
      **一次性 CLI 路径**也许能传自己的 RunConfig。

判据：用"停不下来"的假模型（verify_termination 的 loopy 形态）跑，
      看 iterations 是卡在 8，还是跟着 max_steps 走。
      —— 只有**行为**能证明，读 stub 不算。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓库根，不是 run/

from wit import WitRuntime  # noqa: E402

try:  # 不同版本导出位置可能不同，两条都试
    from wit import RunConfig  # type: ignore
except ImportError:  # pragma: no cover
    from wit.kernel.runtime import RunConfig  # type: ignore

from legal_acps.acps_config import build_config  # noqa: E402
from tools.verify_termination import MisbehavingLeaderModel, build_leader  # noqa: E402


async def one(label: str, cfg):
    model = MisbehavingLeaderModel("loopy")           # 永远停不下来
    agent = build_leader(behaviour="loopy", with_guard=False, model=model)
    # ⚠️ workspace 用临时目录：别在仓库里造文件，否则会污染 tree_diff 基线
    ws = Path(tempfile.mkdtemp(prefix="probe-steps-")) / "ws"
    runtime = WitRuntime(agent, workspace=ws, acps=build_config(role="leader"))
    run = await runtime.run("随便一段文本，只为把步数烧完。", config=cfg)
    print(f"{label:24s} status={run.status:20s} iterations={run.iterations:3d} "
          f"model_calls={model.model_calls}")
    return run.iterations


async def main() -> int:
    print("=== 步数预算探针 ===")
    base = await one("config=None（默认）", None)
    big = await one("RunConfig(max_steps=14)", RunConfig(max_steps=14))

    print()
    if base == 8 and big == 14:
        print("✅ 结论：**CLI 路径可以调大 max_steps**（默认 8 → 14 生效）")
        return 0
    if base == 8 and big == 8:
        print("❌ 结论：RunConfig 被忽略 —— 这条路走不通，需换方案")
        return 1
    print(f"⚠️ 形态意外（base={base} big={big}），需人工判读")
    return 2


sys.exit(asyncio.run(main()))
