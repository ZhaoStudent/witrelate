import asyncio
import os

from wit import OpenAIChatProvider, WitAgent, WitRuntime, capability


@capability(id="math.add", description="计算两个整数之和。")
def add(a: int, b: int) -> int:
    return a + b


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
        )
    )
    try:
        result = await runtime.run("计算 17 + 25。")
        print(result.final_text)
        print(result.observations)
    finally:
        await provider.aclose()


asyncio.run(main())
