import asyncio
import os

from wit import OpenAIChatProvider, WitAgent, WitRuntime


async def main() -> None:
    provider = OpenAIChatProvider(
        model=os.environ["OPENAI_MODEL"],
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        temperature=0,
    )
    runtime = WitRuntime(
        WitAgent(
            id="assistant",
            instructions="请用中文简洁回答。",
            model=provider,
        )
    )
    try:
        result = await runtime.run("用一句话说明什么是 Agent runtime。")
        print(result.final_text)
    finally:
        await provider.aclose()


asyncio.run(main())
