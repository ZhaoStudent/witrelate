import asyncio
import os

from wit import OpenAIChatProvider, WitAgent, WitRuntime


async def main() -> None:
    provider = OpenAIChatProvider(
        model=os.environ["OPENAI_MODEL"],
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )
    runtime = WitRuntime(
        WitAgent(
            id="streaming-assistant",
            instructions="请用中文简洁回答。",
            model=provider,
        )
    )
    try:
        async for event in runtime.astream_events("介绍一下事件流。"):
            print(event.event, event.name)
    finally:
        await provider.aclose()


asyncio.run(main())
