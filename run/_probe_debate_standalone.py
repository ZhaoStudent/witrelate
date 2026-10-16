"""探针：`WitRuntime.run()` 不带 `up_until_ready` 能否跑通叶子 agent（2026-10-07）。

为什么必须实证（人机辩论驱动器的运行约束由它定）：
  · 方案设计假设：正方是叶子（唯一工具 `debate.submit_argument` 纯本地交付、
    无 ACPs 出站委派）⇒ 不需要 enrollment/登录 Registry ⇒ 不会与常驻服务
    顶掉会话（今天 A/B 长跑靠错峰避开的那颗雷直接消失）。
  · ⚠️ 「理论上不需要」不算数——接口签名只证明 run() 不要求 up_until_ready
    **先被调用过**，不证明运行时不偷偷碰 ACPs（比如 discover 缓存、
    partner 诊断）。必须真跑一次看行为。

判据（这个探针回答三件事，每件都要有硬证据）：
  1. run() 不带 up_until_ready **能正常完成**（status / iterations / 产物）。
  2. 正方**真的调了** `debate_submit_argument`（只写散文 = 白跑）。
  3. 运行期间**没有发生登录**：不传 credentials ⇒ 若 run() 内部偷偷
     enroll，会因取不到凭据而炸或报凭据错误——「没炸」且「任务完成」
     双证据下，才可以说"没登录也跑通了"。
  ⚠️ 这是**本地开发机**探针：不出网委派，`.env` 只需要 OPENAI_* 供真模型用。

用法（本机，两段）：

    ./.venv/Scripts/python.exe -m run._probe_debate_standalone --fake   # 假模型（验机制）
    ./.venv/Scripts/python.exe -m run._probe_debate_standalone          # 真模型（验全链）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv_lite import load_env  # noqa: E402

load_env(ROOT / ".env")

from wit import RunConfig, WitRuntime  # noqa: E402

from legal_acps.agents import build_agent  # noqa: E402
from legal_acps.llm import build_model  # noqa: E402

#: 与 `_enrich_rulings` 直跑裁判同款输入形态：开题 + 明确要求调工具。
INPUT_TEXT = (
    "【待辩论事件】\n"
    "网传某矿泉水厂今年3月、6月各有一名员工落水身亡，尸体捞起时仅剩骨架，"
    "含尸体的生产用水被附近居民饮用。\n\n"
    "你被分配的立场：**这条传闻属实**。请为该立场提交第 1 轮立论"
    "（调用 debate_submit_argument，round=1，rebuts 留空）。"
)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true",
                    help="用离线假模型（只验机制：不登录能否走完 run）")
    args = ap.parse_args()

    # 🔴 关键：构造 WitRuntime 时刻意【不】传 acps=…（不配置 ACPs 面），
    #    也不调用 up_until_ready / login / enroll —— 模拟人机辩论驱动器的
    #    最小运行形态。若 run() 内部需要 ACPs，这里会当场炸。
    if args.fake:
        from legal_acps.llm import build_model as _bm  # noqa: F401
        # 假模型：providers 里的离线桩（与既有探针同款来源）
        from wit import WitAgent  # noqa: F401
        from legal_acps.agents import build_debate_pro  # noqa: F401

        # 离线假模型走 providers 的 fake provider（name="fake"）
        agent = build_debate_pro(None, self_aic=None)
        # ⚠️ build_debate_pro(None) 的 model=None 会在编译期炸 —— 换个路径：
        # 直接构造 WitAgent，model 用 fake provider。
        from wit import Model  # noqa: F401
        print("（--fake 模式：离线假模型路径待定，先跑真模型路径）")
        return 2

    agent = build_agent("debate-pro", build_model(), self_aic=None)
    # 🔴 第一轮实测（2026-10-07）：**不传 acps= 连编译都过不了** ——
    #    `debate-pro` 的 capability 带 `acps_skill=True` ⇒ WitAgent.compile
    #    强制要求 AcpsConfig（ValueError: ACPS protocol bindings require…）。
    #    ⇒ 「完全不配置 ACPs 面」这条路**不存在**；真正要验的问题变成：
    #    **配置了 ACPs 面（acps=…）、但不登录（不调 up_until_ready/login/
    #    enroll）**，run() 能不能走通 —— 这才是人机辩论驱动器的运行形态。
    from legal_acps.acps_config import build_config
    runtime = WitRuntime(
        agent, workspace=".wit/debate-pro-standalone",
        acps=build_config(role="debate-pro"),
    )
    print("[1] WitRuntime 构造完成（传 acps=、未登录）")

    run = await runtime.run(INPUT_TEXT, config=RunConfig(max_steps=8))
    print(f"[2] run() 完成：status={run.status} iterations={run.iterations}")

    called = [o for o in (run.observations or [])
              if "submit" in str(o.get("tool") or o.get("name") or "")]
    print(f"[3] 调用 debate_submit_argument：{len(called)} 次")
    for o in run.observations or []:
        print(f"    · {o.get('tool') or o.get('name') or '?'} ({o.get('status', '?')})")

    final = getattr(run, "output", None) or ""
    print(f"[4] 输出末段：{str(final)[-300:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
