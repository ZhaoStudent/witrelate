from wit import WitAgent, capability


@capability(id="text.upper", description="将文本转换为大写。")
def upper(text: str) -> str:
    return text.upper()


agent = WitAgent(
    id="text-assistant",
    display_name="文本处理服务",
    version="1.0.0",
    description="按请求对短文本进行格式转换。",
    instructions="用户要求转换为大写时，请调用 text_upper。",
    capabilities=[upper],
    metadata={"team": "example"},
)

compiled = agent.compile(workspace=".wit/text-assistant")
print(compiled.to_inspection())