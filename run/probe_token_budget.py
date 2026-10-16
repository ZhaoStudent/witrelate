"""探针：`MAX_TOKENS_KEY` 真的透传到了模型服务吗？

═══ 为什么需要它 ═══

`settings["max_tokens"] = N` 这一步**离线验不了**：假模型不看 token，
写不进去也不会报错。而它正是揭示那次真机故障的修法 ——
"参数塞进了 request" 与 "模型真的按它执行" 是两件事（本项目为这个区别吃过多次亏）。

判据要落在**可观测的行为差异**上：同一个请求，给 64 与给默认值，
模型输出长度必须显著不同 —— 只有真的透传到服务端才会这样。

    ./.venv/bin/python -m run.probe_token_budget

⚠️ 需要 `.env`（真模型），本地跑不了；在服务器上用 `PYTHONPATH=.` 跑。
"""

from __future__ import annotations

import asyncio

from wit import RunConfig, WitAgent, WitRuntime

from legal_acps.experience.tool_scope import MAX_TOKENS_KEY, ToolScopeHooks
from legal_acps.llm import DEFAULT_MAX_TOKENS, build_model

#: 一个"越写越长"的请求 —— 长回答才有截断与不截断的区别。
ASK = (
    "请详细解释「举证责任」在民事诉讼中的含义，并举例说明它如何影响当事人的策略。"
    "要求：至少写 600 字，分点展开。"
)


def build_probe_agent() -> WitAgent:
    """只要 Hook，不要工具 —— 这样模型只能输出文本，长度差异一眼可见。"""
    return WitAgent(
        id="probe-token-budget",
        instructions="你是一个乐于详细作答的助手。",
        model=None,  # 运行时注入
        capabilities=[],
        hooks=[ToolScopeHooks()],
    )


async def main() -> int:
    model = build_model()
    runtime = WitRuntime(
        WitAgent(
            id="probe-token-budget",
            instructions="你是一个乐于详细作答的助手。",
            model=model,
            capabilities=[],
            hooks=[ToolScopeHooks()],
        ),
        workspace=".wit-probe-token",
    )

    print(f"provider 默认 max_tokens = {DEFAULT_MAX_TOKENS}")

    ok = True
    results: dict[str, tuple[int, int]] = {}
    for label, metadata in (
        ("默认", {}),
        ("给 64", {MAX_TOKENS_KEY: 64}),
        ("给 16384", {MAX_TOKENS_KEY: 16384}),
    ):
        run = await runtime.run(ASK, RunConfig(max_steps=2, metadata=dict(metadata)))
        body = run.final_text or ""
        think = run.reasoning_text or ""
        results[label] = (len(body), len(think))
        print(
            f"  [{label:>7}] status={run.status} 迭代={run.iterations} "
            f"正文={len(body)} 字 / 思考={len(think)} 字 | 开头：{body[:40]!r}"
        )

    small = results["给 64"][0]
    default = results["默认"][0]
    large = results["给 16384"][0]
    print()
    print(f"判据：给 64 的正文长度（{small}）必须**显著小于**默认（{default}）")
    if small < default:
        print("✅ 通过：token 预算确实透传到了模型服务（截断真的发生了）")
    else:
        print(
            "❌ 不成立：给 64 与默认的输出一样长 —— "
            "`settings['max_tokens']` 很可能**没有**透传到 SDK，"
            "那么揭示的修法就是无效的（必须换别的通路）"
        )
        ok = False
    if large < default:
        print(f"⚠️ 注意：给 16384（{large}）反而比默认（{default}）短 —— 可能是随机波动，建议复跑一次")
    return 0 if ok else 1


if __name__ == "__main__":
    from dotenv_lite import load_env

    load_env()
    raise SystemExit(asyncio.run(main()))
