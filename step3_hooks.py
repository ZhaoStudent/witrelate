import asyncio
import os

from wit import (
    OpenAIChatProvider,
    RunEndEvent,
    RunStartEvent,
    WitAgent,
    WitHooks,
    WitRuntime,
    capability,
)


@capability(id="math.add", description="计算两个整数之和。")
def add(a: int, b: int) -> int:
    return a + b


class RunLogger(WitHooks):
    id = "example.run-logger"

    async def on_run_start(self, event: RunStartEvent) -> None:
        print("run started:", event.context.compiled.agent_id)

    async def on_run_end(self, event: RunEndEvent) -> None:
        print("run ended:", event.result.status)


async def main() -> None:
    provider = OpenAIChatProvider(
        model=os.environ["OPENAI_MODEL"],
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        temperature=0,
    )
    runtime = WitRuntime(
        WitAgent(
            id="calculator",
            instructions="遇到加法时，先调用 math_add，再用中文回答。",
            model=provider,
            capabilities=[add],
            hooks=[RunLogger()],
        )
    )
    try:
        result = await runtime.run("计算 17 + 25。")
        print(result.final_text)
    finally:
        await provider.aclose()


asyncio.run(main())
