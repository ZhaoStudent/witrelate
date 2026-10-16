"""模型提供方 —— 三个 Agent 共用一套 OpenAI 兼容配置。

只要服务方兼容 OpenAI 的 /chat/completions 就能直接用，
包括 DeepSeek、通义千问、Kimi、智谱、本地 vLLM 等。

环境变量：
    OPENAI_MODEL     必填，本项目实测可用：deepseek-v4-pro / deepseek-flash
    OPENAI_API_KEY   必填
    OPENAI_BASE_URL  选填，第三方服务必填，结尾不要带 /chat/completions
    OPENAI_TIMEOUT   选填，默认 180 秒

关于 DeepSeek-V4 系列（2026-09 实测）：
    它们默认是**推理模型**，会把思考过程放在 reasoning_content 里，
    正式回答放在 content 里。若 max_tokens 给小了，token 会被思考过程吃光，
    content 会是空字符串 —— 表现为「模型没输出」。所以这里强制给足预算。
"""

from __future__ import annotations

import os

from wit import OpenAIChatProvider

from .audio_modality import deny_audio_provider
from .video_modality import model_provider_for

# 推理模型的思考过程也占 max_tokens，给少了会拿到空的 content
DEFAULT_MAX_TOKENS = 8192


def build_model(*, temperature: float = 0.0, max_tokens: int | None = None):
    """构造模型适配器。temperature 默认 0，保证同一输入结论可复现。

    ⚠️ 返回值**不一定是 `OpenAIChatProvider` 本身**：它先过一遍
    `video_modality.model_provider_for()` —— 那一步会**现场探测**
    "provider 认不认视频"，并据此决定要不要套上协议适配层（PPT 4-10）。

    本部署里这一层**不会**被套上（探测结果是"不认"，且没有任何网关后端），
    视频靠 `ContentTransform` 降级（PPT 4-11）—— 两条路的取舍写在
    `video_modality` 的模块 docstring 里。

    ⚠️ 之后再过一遍 `audio_modality.deny_audio_provider()` —— **这一层不是可选的**。
    框架只在 provider **自报「不支持」**某模态时才应用 `ContentTransform`；
    而本 provider 按 **OpenAI 报文形状**对 `audio` 自报"支持"
    （实测 `supports_content(AudioPart) -> (True, None)`）⇒ 不声明的话，
    我们注册的 `audio_content_transform()` **永远不会被调用**，`AudioPart`
    一路走到厂商侧 ⇒ HTTP 422 ⇒ 整个 run 挂（2026-10-06 真机实测两次）。
    视频侧不需要这一步，纯粹因为该 provider 恰好对 `video` 自报不支持。
    """
    model = os.environ.get("OPENAI_MODEL")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not model or not api_key:
        raise SystemExit(
            "缺少环境变量。请在 .env 或 shell 里设置：\n"
            '  OPENAI_MODEL="deepseek-v4-pro"\n'
            '  OPENAI_API_KEY="sk-..."\n'
            '  OPENAI_BASE_URL="https://api.deepseek.com"   # DeepSeek 官方地址\n'
        )

    base = OpenAIChatProvider(
        model=model,
        api_key=api_key,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        timeout=float(os.environ.get("OPENAI_TIMEOUT", "180")),
        # 下面这些会作为「每次请求都带上」的参数交给 SDK
        temperature=temperature,
        max_tokens=max_tokens or DEFAULT_MAX_TOKENS,
    )
    # `backend_reads_video` **刻意留默认 False**，且刻意**不做成环境变量**：
    # 环境变量会被 systemd / shell rc / 父进程一路继承，纪律守不住；
    # 而它一旦被误开，后果是模型收到它读不了的 `VideoPart` ⇒ 整个 run 失败。
    # 将来真接了支持视频的网关，就在这里**显式**改成 True（同形先例：
    # `evidence/fetch.py` 的 `enable_local_file`）。
    #
    # 顺序：先过视频那条（可能套上网关适配层），再声明音频的**有效能力**。
    # 后者包在最外层，`supports_content` 只对 `AudioPart` 改口，其余原样委托。
    return deny_audio_provider(model_provider_for(base))
