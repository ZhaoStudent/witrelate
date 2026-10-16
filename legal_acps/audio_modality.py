"""音频降级 —— 把 provider 读不了的 `AudioPart` 换成它读得懂的形态。

════════════════════════════════════════════════════════════════════════
一句话：视频降级是「少了一点」，音频降级是「换了一种证据」
════════════════════════════════════════════════════════════════════════

视频降级之后，模型**仍然看见了**画面（一帧换全片：有损，但同质）。
音频降级完全不同：模型**一个采样都没听到**，它读的是**代码量出来的数字**。

⇒ 所以产物**必须**同时含两样东西，缺一不可：

   ① 测量报告 —— 来自 `legal_acps/audio`，**与音频鉴伪链路同一份代码**
      （`analyze()` + `summary_line()`），不是第二套实现；
   ② 一句如实的「你没听到音频，你读的是代码的测量」。

只发 ① 的后果：模型会把测量结果读成"我听过之后的印象"，
于是**它用自己的措辞替一次没跑过的检查作证** —— 那正是本项目最忌讳的
**静默给安全结论**。

════════════════════════════════════════════════════════════════════════
为什么会有这个模块（2026-10-06 真机实测，不是推演）
════════════════════════════════════════════════════════════════════════

`run/e2e_audio_live.py`（#122 补的真机探针）**第一次真跑，约 1 秒**就挂了：

    HTTP 422: messages[1]: unknown variant `input_audio`,
              expected one of `text`, `image_url`, `file`

三层定位（账不能算错）：

  * **不是本仓代码**：全仓搜 `input_audio` = 0 命中；它由框架的 provider
    字节码（`wit/providers/_bytecode/…/openai.pyc`）造出来。
  * **框架也没撒谎，是"按形状声明"**：实测 `input_modalities` 里**有** `audio`
    （OpenAI 的 Chat Completions 确实有 `input_audio`）—— 它**不看 `base_url`
    后面那个网关实际实现了什么**。
  * **真正不认的是网关**：本部署是 `deepseek-v4-pro @ api.deepseek.com`，
    只认 `text` / `image_url` / `file`。

⇒ **没有任何一层会提前拦下它**，失败发生在**厂商那一侧**，表现为
**整个 run 抛异常**，而不是「我看不了音频，因为…」。

视频侧**早就**补了同一道降级（见 `video_modality.py`），音频侧一直空着。
缺口长成那样的原因很清楚：**四条链里，只有被单独盯着的那条才有人替它兜底。**

════════════════════════════════════════════════════════════════════════
四条不许越的线
════════════════════════════════════════════════════════════════════════

1. **降级 ≠ 等价，而且这句话必须由产物自己写出来。**
   模型没有耳朵：报告里每一个数字都是**代码**算的。产物里不写这一句，
   下游读到的就是"模型听过之后觉得没问题"。

2. **测不出就如实说。** 不许返回空、更不许编一个结论。
   ⚠️ 尤其不许返回 `None`：`TransformHandler` 里 `None` 的语义是「不转换」，
   下游会照原样收到 `AudioPart` ⇒ provider 抛 `UnsupportedContentError`
   ⇒ **整个 run 失败**。所以取不到时返回的是一段**文本**，把原因与后果写清。

3. **绝不为「给模型看」而重编码。** 测量吃的是入站那份**原始字节**：
   不做转码、不降采样、不裁时长。重采样痕迹与编码截止频率**正是**音频鉴伪
   要看的两项，重编码等于往里面塞我们自己的编码器痕迹。

4. **本模块不用 `GatewayAdapter`。** `added_input_modalities` 是一句承诺：
   挂上就等于告诉 wit「可以把 `AudioPart` 发出去了」。本部署**没有任何网关**
   读得了音频，所以这里只有 `ContentTransform` 这一条路。

════════════════════════════════════════════════════════════════════════
⚠️ 本模块在**模块级不许 import numpy**（惰性导出，理由与 `audio/__init__.py` 逐字相同）
════════════════════════════════════════════════════════════════════════

`agents.py` 必须 import 本模块才能挂 `AudioFallbackHooks`，而
`legal_acps/audio` 的**测量层**是 numpy 的。若在此写下
`from .audio import analyze`，那么**只要 import agents 就会把 numpy 拉进来** ——
而"服务器上没装 numpy 时 8 个已过审 Agent 全部起不来"是不能接受的耦合
（加分项不该有这种风险）。⇒ 测量相关的 import **全部写在函数体里**。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from wit import ContentTransform, WitHooks
from wit.kernel.content import ContentPart, TextPart

from .verdict import stable_reason

#: 降级说明的**固定前缀**。写成常量是为了让断言能钉住「如实说明必须跟着走」：
#: 只要产物里带着测量报告，就必须有且只有一个以它开头的 Part。
DEGRADE_NOTICE_PREFIX = "【音频已降级】"

#: `AudioFallbackHooks` 的优先级。`content_transforms` 不是工具边界 Hook，
#: 它不参与「谁包住谁」的竞争（wit 只是把各 Hook 声明的 Transform 收成一个列表），
#: 所以这个值**取多少都不影响结果**。仍然显式写出来并发誓它无关，是因为
#: 另两个 Hook（`EvidenceInboxHooks` 100 / `DebateChainGuardHooks` 50）的优先级
#: 是**语义性**的 —— 不写清楚，下一个维护者会以为这里也是。
AUDIO_HOOK_PRIORITY = 100

#: 三条链里**可能收到入站音频**的角色。挂在别处就是死代码
#: （纪律：先问「谁来调它」，再问「调了管不管用」）。
#:
#: ⚠️ 这是**声明式副本**：单一事实源是"该角色的收件箱收不收 audio"。
#: `check_local` 会**现场派生**真名单并与本元组比对 —— 副本漂了就直接红，
#: 免得下一个维护者照错的名单去挂 Hook。
AUDIO_CAPABLE_ROLES = ("leader", "evidence-leader", "evidence-audio")


def _byte_cap() -> int:
    """降级路径肯过一遍的字节上限 —— **与取件上限同源**，不写第二个数。

    在这里写死一个 `16 * 1024 * 1024`，就又多了一处「同一判定写两处」，
    而两处必然漂移。所以现场向 `audio.fetch` 要。
    """
    from .audio import MAX_AUDIO_BYTES

    return int(MAX_AUDIO_BYTES)


def _no_ears_clause() -> str:
    """那句**必写**的如实说明。它是本模块存在的一半理由，别当装饰。"""
    return (
        "⚠️ **模型没有听到这段音频** —— 它读不了音频输入。"
        "下面是**代码**对这份原始字节的确定性测量结果。"
        "请把它们当作仪器读数来推理；"
        "**不要**用「我听到…」这类措辞转述 —— 那等于替一次没跑过的检查作证。"
    )


# ---------------------------------------------------------------------------
# 测量：只走 `legal_acps/audio`，**不写第二套**
# ---------------------------------------------------------------------------

def _measure(raw: bytes, mime: str | None) -> tuple[str, list[str]]:
    """原始字节 → `(一句话摘要, 逐行明细)`。

    摘要用 `pipeline.summary_line()` —— 它**与音频鉴伪链路同源**，
    也是整份报告里最容易被原样抄进对话、抄进笔录的那一行。
    """
    from .audio import analyze, summary_line

    report = analyze(raw, declared_mime=(mime or None), name=None)
    summary = summary_line(report)

    facts = [
        f"容器 {report.container}",
        f"{report.byte_size} 字节",
        f"media_type（按魔术字节）{report.media_type}",
    ]
    if report.sample_rate:
        facts.append(f"{report.sample_rate} Hz")
    if report.channels:
        facts.append(f"{report.channels} 声道")
    if report.bits_per_sample:
        facts.append(f"{report.bits_per_sample} bit")
    if report.duration_seconds is not None:
        facts.append(f"时长 {report.duration_seconds:.2f} s")
    facts.append(f"sha256 {report.sha256}")

    lines = ["", "测量事实：", "  " + "；".join(facts)]
    if not report.pcm_available:
        # ⚠️ 这一条**必须**出现：服务器上没有 ffmpeg / sox / librosa，
        #    MP3 / AAC / FLAC / OGG 一律解不出波形。不提就等于让报告读起来
        #    像"检查过了没问题"，而真相是**波形域一项都没跑**。
        lines.append(f"  ⚠️ 波形域整项未跑（pcm_available=false）：{report.pcm_note}")
    lines.append("逐项检测：")
    described = [
        f"  · [{f.verdict}] {f.check}：{f.detail}"
        for f in (report.findings or [])
    ]
    lines.extend(described or ["  （本报告没有逐项记录）"])
    rationale = str(getattr(report, "suspicion_rationale", "") or "").strip()
    lines.append(
        f"综合线索等级：{report.suspicion}" + (f"（{rationale}）" if rationale else "")
    )
    return summary, lines


def _report_block(raw: bytes, mime: str | None) -> str:
    """测量报告的正文。**任何失败都折成一段可读文本**（理由见模块纪律第 2 条）。"""
    try:
        summary, lines = _measure(raw, mime)
    except Exception as exc:  # noqa: BLE001 - 模型请求路径上，代价不对称
        return (
            "\n**本次连测量都没能完成**："
            f"{stable_reason(exc)}。"
            "⇒ 这份音频**没有进入本次判断**（不是「音频没问题」）。"
        )
    return "\n测量摘要：" + summary + "\n" + "\n".join(lines)


def _report_block_for_uri(uri: str, mime: str | None) -> str:
    """只拿到地址时：走**工具层同一个入口**（`pipeline.outcome`）。

    它**永远返回结构化结果**：取件被拒也带着可执行的补救建议，
    所以这里不需要自己认异常类型（认漏一条就会穿透出去）。
    """
    try:
        from .audio import outcome

        out = outcome(uri, declared_mime=(mime or None))
    except Exception as exc:  # noqa: BLE001 - 同上
        return f"\n**取件通道自己出错了**：{stable_reason(exc)} ⇒ 本次没有任何测量。"
    status = str(getattr(out, "status", "") or "")
    result = getattr(out, "result", None)
    if status == "ok" and result is not None:
        summary = str(getattr(result, "summary_for_leader", "") or "").strip()
        report = getattr(result, "report", None)
        tail = ""
        if report is not None:
            _, lines = _safe_lines(report)
            tail = "\n" + "\n".join(lines)
        return f"\n测量摘要：{summary}{tail}"
    return (
        "\n**取件就被拒了**，所以这份音频连测量都没能做。"
        f"原因：{getattr(out, 'refusal_reason', '（未记录）')}。"
        f"出路：{getattr(out, 'how_to_fix', '（未记录）')}。"
    )


def _safe_lines(report: Any) -> tuple[str, list[str]]:
    """已经有 report 对象时，只把它排版成明细行（不再重复测量）。"""
    facts = [f"容器 {getattr(report, 'container', '?')}"]
    size = getattr(report, "byte_size", None)
    if size is not None:
        facts.append(f"{size} 字节")
    lines = ["", "测量事实：", "  " + "；".join(facts)]
    for f in getattr(report, "findings", None) or []:
        lines.append(f"  · [{getattr(f, 'verdict', '')}] {getattr(f, 'check', '')}：{getattr(f, 'detail', '')}")
    lines.append(f"综合线索等级：{getattr(report, 'suspicion', '?')}")
    return "", lines


# ---------------------------------------------------------------------------
# 降级产物（纯函数，可离线断言）
# ---------------------------------------------------------------------------

def audio_to_report(part: Any, context: Any) -> list[ContentPart]:
    """`ContentTransform` 的 handler：`AudioPart` → `[说明 + 测量报告]`。

    只做一层防御包装：**任何**异常都折成一段可读文本。
    理由同视频侧 —— 这里的失败代价是「整个 run 挂掉」，不是「少一条线索」。
    """
    try:
        return _report_parts(part, context)
    except Exception as exc:  # pragma: no cover - 纯防御
        return [
            TextPart(
                text=(
                    f"{DEGRADE_NOTICE_PREFIX}降级处理器自己出错了"
                    f"（{stable_reason(exc)}）—— 本次模型看不到任何音频内容，"
                    "也没有任何测量。"
                )
            )
        ]


def _report_parts(part: Any, context: Any) -> list[ContentPart]:
    """真正的产物构造。**永远返回非空列表**（纪律第 2 条）。"""
    provider = str(getattr(context, "provider", "") or "（未知 provider）")
    reason = str(getattr(context, "reason", "") or "").strip()
    why = f"provider 自报的原因：{reason}" if reason else "provider 未给出原因"
    mime = str(getattr(part, "mime_type", "") or "") or None

    data = getattr(part, "data", None)
    uri = getattr(part, "uri", None)

    head = f"{DEGRADE_NOTICE_PREFIX}模型服务（{provider}）读不了音频（{why}）。"

    if data:
        raw = bytes(data)
        cap = _byte_cap()
        if len(raw) > cap:
            # 不做静默截断：截断过的字节测出来的"事实"与真的长得一模一样。
            return [
                TextPart(
                    text=(
                        head
                        + f"这段字节有 {len(raw)} 字节，超过了降级路径在"
                        f"**模型请求路径**上的上限 {cap} 字节，因此**没有做任何测量**"
                        "（截断后再测会得出看起来同样可信的错结论）。"
                        + _no_ears_clause()
                    )
                )
            ]
        return [TextPart(text=head + _no_ears_clause() + _report_block(raw, mime))]

    if uri:
        text = str(uri)
        if text.startswith(("inbox:", "data:")):
            # 这两形态是**本项目唯一认的**取件通道（刻意不出网）。
            return [
                TextPart(
                    text=head + _no_ears_clause() + _report_block_for_uri(text, mime)
                )
            ]
        return [
            TextPart(
                text=(
                    head
                    + f"这次拿到的音频**只有地址、没有字节**（uri={text!r}）。"
                    "系统**不会自动去取这个地址** —— 本项目的取件通道刻意不出网，"
                    "只认 `inbox:` 句柄与内联 `data:`。"
                    "⇒ 模型看不到任何音频，也**没有任何测量**。"
                    "如需分析，请把音频**作为附件重新发送**。"
                    + _no_ears_clause()
                )
            )
        ]

    return [
        TextPart(
            text=(
                head
                + "这次既没有字节、也没有可识别的地址 ⇒ 模型看不到任何音频，"
                "也没有任何测量。**这不是「音频没问题」。**"
            )
        )
    ]


# ---------------------------------------------------------------------------
# ContentTransform
# ---------------------------------------------------------------------------

def audio_content_transform() -> ContentTransform:
    """注册用的一条 Transform。抽成函数是为了让断言能直接拿到对象。"""
    return ContentTransform(source_type="audio", handler=audio_to_report)


class AudioFallbackHooks(WitHooks):
    """把音频降级器注册给 wit。挂在**可能收到入站音频**的角色上。

    挂载点见 `AUDIO_CAPABLE_ROLES`。不挂的后果不是「少一条线索」，而是
    `AudioPart` 一路走到 provider ⇒ 厂商侧 422 ⇒ **整个 run 失败**：
    用户传了一段录音，得到的是一句"服务异常"，而不是
    「我听不了音频，但我把代码测出来的数字给你」。
    """

    id = "legal.audio.fallback"
    priority = AUDIO_HOOK_PRIORITY

    def content_transforms(self, ctx: Any) -> list[ContentTransform]:
        return [audio_content_transform()]


# ---------------------------------------------------------------------------
# 有效能力声明：**没有这一层，上面那个 ContentTransform 永远不会被调用**
# ---------------------------------------------------------------------------

#: 本部署对 `audio` 的**有效能力**：读不了。依据是真机实测，不是猜测 ——
#: 2026-10-06 `run/e2e_audio_live.py` 首跑即 `HTTP 422 unknown variant 'input_audio'`，
#: 网关只认 `text` / `image_url` / `file`。
AUDIO_DENIED_REASON = (
    "本部署的模型网关读不了音频输入（2026-10-06 真机实测 HTTP 422 "
    "`unknown variant 'input_audio'`；该网关只认 text / image_url / file）"
)


class AudioDeniedProvider:
    """把「provider 按报文形状自报支持 `audio`、但本部署的网关不认」**声明出来**。

    ════════════════════════════════════════════════════════════════════
    为什么必须有这一层（2026-10-06 真机复跑夹出，判据当时是全绿的）
    ════════════════════════════════════════════════════════════════════

    `ContentTransform` **只在 provider 自报「不支持」该模态时才被应用**。
    用框架自己的 `prepare_messages_for_provider` 实测同一个 audio 变换：

        supports_content(AudioPart) -> (True, None)    ⇒ 原样放行，降级**不发生**
        supports_content(AudioPart) -> (False, …)      ⇒ 应用变换，降级**发生**
        没有 supports_content 方法                      ⇒ 原样放行

    而 `OpenAIChatProvider.capabilities` 是**按 OpenAI 报文形状**声明的
    （实测 `input_modalities` 含 `audio`）⇒ 它对音频答「支持」⇒
    我们注册的 `audio_content_transform()` **永远不会被调用**，
    `AudioPart` 一路走到厂商侧 ⇒ 422 ⇒ **整个 run 挂**。

    ⚠️ **这正是「与视频侧逐行同形」不够用的地方**：视频**能**降级，只是因为
    该 provider 恰好对 `video` 自报不支持（实测 `(False, 'Chat user messages
    have no native video content part')`）。**同形只能复制"注册方式"，
    复制不了 `supports_content` 给出的那个答案。**

    ════════════════════════════════════════════════════════════════════
    这**不是**让 provider 撒谎
    ════════════════════════════════════════════════════════════════════

    本部署**真实的有效能力**就是读不了音频 —— 由**实测 422** 证明。
    包装器只是把这件事**声明出来**，让框架知道「这里需要降级」。
    同一种思路的先例：`video_modality.model_provider_for()` 的
    「**探测 + 显式传参**」—— **能力不能靠猜，也不能靠默认**。

    ⚠️ 反过来也要记住：真接了读得了音频的网关时，应当把这一层**退掉**
    （并相应退掉降级器），而不是两套并存。
    """

    def __init__(self, base: Any, *, reason: str = AUDIO_DENIED_REASON) -> None:
        self._base = base
        self._reason = reason

    @property
    def capabilities(self) -> Any:
        """有效能力：把 `audio` 从 `input_modalities` 里去掉。

        只改 `input_modalities`（入站我们可以自己降级）；**不动**
        `output_modalities`，也不碰任何其它字段。
        """
        caps = getattr(self._base, "capabilities", None)
        if caps is None or not hasattr(caps, "input_modalities"):
            return caps
        return replace(
            caps,
            input_modalities=frozenset(
                m for m in caps.input_modalities if m != "audio"
            ),
        )

    def supports_content(self, part: Any, *, role: str) -> tuple[bool, str | None]:
        """对 `AudioPart` 答「不支持」—— 这才是**会触发降级**的那个答案。"""
        if getattr(part, "type", "") == "audio":
            return False, self._reason
        return self._base.supports_content(part, role=role)

    async def complete(self, request: Any) -> Any:
        return await self._base.complete(request)

    async def aclose(self) -> Any:
        return await self._base.aclose()

    def __getattr__(self, name: str) -> Any:
        # 其余一切（`model` / `settings` / `stream` / 将来 wit 新加的接口）原样委托。
        # ⚠️ 用 `object.__getattribute__` 取 `_base`：若 `__init__` 在半途抛异常，
        #    直接 `self._base` 会再次进 `__getattr__` ⇒ 无限递归。
        return getattr(object.__getattribute__(self, "_base"), name)


def deny_audio_provider(base: Any) -> Any:
    """给 provider 套上「有效能力」声明。

    `base is None`（编译期假模型）时**原样返回**：不许在这里造一个 provider 出来，
    否则"离线能跑"就成了一句谎话（同 `video_modality.model_provider_for` 的理由）。
    """
    if base is None:
        return base
    return AudioDeniedProvider(base)


__all__ = [
    "AUDIO_CAPABLE_ROLES",
    "AUDIO_DENIED_REASON",
    "AUDIO_HOOK_PRIORITY",
    "AudioDeniedProvider",
    "AudioFallbackHooks",
    "DEGRADE_NOTICE_PREFIX",
    "audio_content_transform",
    "audio_to_report",
    "deny_audio_provider",
]
