"""视频降级 —— 把 provider 读不了的 `VideoPart` 换成它读得懂的形态。

════════════════════════════════════════════════════════════════════════
两张 PPT 给的是一对**互补**机制，本项目两条都用，但分工不许混
════════════════════════════════════════════════════════════════════════

    · `ContentTransform`（PPT 4-11 内容降级）—— **主力**，也是本文件的主体。
      provider 不支持 video 时，在**发包前**把 `VideoPart` 换成
      「一句如实说明 + 首帧图」。它属于业务层，所以「这次降级是有损的」这句话
      **必须由它自己写出来**。
    · `GatewayAdapter`（PPT 4-10 协议适配）—— **配套**，只在
      「网关后端**实际**支持视频、而官方 SDK 的 wire 契约表达不了」时才启用。
      本机没有这样的网关 ⇒ **不启用**；该不该启用由
      `model_provider_for()` 现场决定，见下面那条「不许用环境变量」的说明。

    ⚠️ 本机框架的 API 与 PPT 上写的**不完全一样**（PPT 只示意）：
       PPT 用 `ImagePart.from_bytes(frame, mime_type=...)`；本机这一版
       `ImagePart` **没有** `from_bytes` 类方法，得写
       `ImagePart(mime_type=..., data=...)`。
       按 PPT 抄会直接 `AttributeError` —— 而它出现在**模型请求路径**上，
       表现为「整个 run 挂了」，不是「少一条线索」。

════════════════════════════════════════════════════════════════════════
四条不许越的线
════════════════════════════════════════════════════════════════════════

1. **降级 ≠ 无害。** 首帧只是第 0 帧：真正被剪掉的那几秒大概率不在上面。
   所以产物**必须**拆成两个 Part（说明 + 图），不能只发一张图 ——
   单发一张图时，模型看到的就是一张普普通通的图片，它**没有任何理由**
   怀疑自己漏看了什么。这是本项目最忌讳的静默安全结论的变体。

2. **抽不到帧就如实说。** 不许返回空、更不许编一张图。
   ⚠️ 尤其不许返回 `None`：`TransformHandler` 里 `None` 的语义是「不转换」，
   下游会照原样收到 `VideoPart` ⇒ provider 抛 `UnsupportedContentError` ⇒
   **整个 run 失败**。所以取不到时返回的是一段**文本**，把原因与后果都写清。

3. **绝不为「给模型看」而重编码**（与 `video/frames.py` 同一条纪律）。
   取到什么字节就发什么，不缩放、不降质量 —— JPEG 的量化表与块网格
   正是图像鉴伪要看的两项，重编码等于往里面塞我们自己的编码器痕迹。

4. **`added_input_modalities` 是一句承诺，不是一句愿望。**
   它向 wit 声明「本适配器扩展了支持的模态」⇒ wit 就会把 `VideoPart`
   原样发出去。挂在一个后端根本不认视频的网关前面，等于让模型收到它读不了
   的东西。所以本模块**不自己决定要不要挂**，而是现场探测 + 显式传参。

════════════════════════════════════════════════════════════════════════
为什么「后端支持视频」这件事**不许用环境变量**控制
════════════════════════════════════════════════════════════════════════

本项目在 `fetch.fetch` 的本地读取开关上踩过同形的坑：环境变量会被 systemd /
shell rc / 父进程一路继承过去，**纪律守不住**；改成「只能由入口代码显式调用」
才是结构保证。同一条理由用在这里 —— 见 `model_provider_for`。
"""

from __future__ import annotations

from typing import Any, Sequence

from .verdict import stable_reason
from wit import AdaptedModelProvider, ContentTransform, GatewayAdapter, WitHooks
from wit.kernel.content import ContentPart, ImagePart, TextPart, VideoPart

from .video.decode import ExtractedFrame, decode_video, sniff_media_type

#: 降级说明的**固定前缀**。写成常量是为了让断言能钉住「说明必须跟着走」这件事：
#: 只要产物里有图，就必须同时有一个以它开头的 Part。
DEGRADE_NOTICE_PREFIX = "【视频已降级】"

#: 降级器愿意在**模型请求路径**上过一遍的字节上限。
#: ⚠️ 这条路径进来的字节**不经过** `video/fetch.py` 的取件上限 ——
#: 它直接来自入站附件层（`state.messages` 里的 `VideoPart`），
#: 所以这个判断是**真会被走到**的，不是摆设。超限时如实拒绝，
#: 而不是静默截断：截断过的字节解出来的「事实」与真的长得一模一样。
MAX_DEGRADE_BYTES = 64 * 1024 * 1024

#: 探测用的最小视频字节。**刻意非空**：空字节在不同 provider 上可能走不同的
#: 判定分支（「没有内容」 vs 「不支持这种内容」），而我们要探的是后者。
#: 探针必须与被探测对象的**形态**一致，否则探出来的结论不适用。
_PROBE_BYTES = b"\x00\x00\x00\x18ftypmp42"

#: `VideoFallbackHooks` 的优先级。`content_transforms` 不是工具边界 Hook，
#: 它不参与「谁包住谁」的竞争（wit 只是把各 Hook 声明的 Transform 收集成一个列表），
#: 所以这个值**取多少都不影响结果**。仍然显式写出来并发誓它无关，是因为
#: 本项目的另两个 Hook（`EvidenceInboxHooks` 100 / `DebateChainGuardHooks` 50）
#: 的优先级是**语义性**的 —— 不写清楚，下一个维护者会以为这里也是。
VIDEO_HOOK_PRIORITY = 100

#: 三条链里**可能收到入站视频**的角色。挂在别处就是死代码
#: （纪律：先问「谁来调它」，再问「调了管不管用」）。
VIDEO_CAPABLE_ROLES = ("leader", "evidence-leader", "evidence-video")


# ---------------------------------------------------------------------------
# 抽预览帧：零解码、不重编码
# ---------------------------------------------------------------------------

def pick_preview_frame(frames: Sequence[ExtractedFrame]) -> ExtractedFrame | None:
    """在已取到的帧里挑一帧当预览。

    偏好顺序：**真正的第 0 帧**（样本序号 ≥ 0 里最小的那个）→ 内嵌封面帧
    （序号 -1）。封面帧是海报图、不是画面，所以只在前者缺席时才用它 ——
    而它到底是哪一种会由 `preview_source_note()` 如实写进说明里。
    """
    real = [f for f in frames if f.index >= 0]
    if real:
        return min(real, key=lambda f: f.index)
    return frames[0] if frames else None


def preview_source_note(frame: ExtractedFrame) -> str:
    """这一帧**是什么**（第 0 帧 / 封面图 / 动图帧）—— 不许含糊。"""
    if frame.source == "cover_art" or frame.index < 0:
        return (
            "⚠️ 发过去的这一张是容器里内嵌的**封面图（封面帧）**，"
            "它是作品的海报，**通常不是视频的第 0 帧画面**。"
        )
    if frame.source == "animated_gif":
        return "发过去的是这段动图的第 1 帧。"
    return f"发过去的是视频的第 1 帧（样本序号 {frame.index}）。"


def preview_bytes(video_bytes: bytes) -> tuple[ExtractedFrame | None, str]:
    """原始字节 → `(预览帧 或 None, 人话原因)`。原因在两个分支上**都非空**。

    ⚠️ `decode_video` 承诺不抛异常，这里**仍然兜住**：本函数跑在**模型请求
    路径**上 —— 在别处，一个异常顶多让某条线索缺席；在这里，它让整个 run 失败。
    代价不对称，所以防御不对称。（这也是「返回类型只多一层」换来的东西。）
    """
    try:
        decoded = decode_video(video_bytes)
    except Exception as exc:  # pragma: no cover - decode 从不抛，纯防御
        return None, (
            f"容器解析时发生了本不该发生的异常（{stable_reason(exc)}）—— "
            "本次没有可用的预览帧。"
        )
    if not decoded.frames:
        return None, decoded.frames_reason or "本次一帧都没取到，原因未记录。"
    frame = pick_preview_frame(decoded.frames)
    if frame is None:  # pragma: no cover - frames 非空时 pick 必不返回 None
        return None, "取到了帧但挑不出可用的预览帧。"
    return frame, (
        f"本次从容器里零解码取到 {len(decoded.frames)} 帧"
        f"（来源 `{decoded.frame_source}`），取其中一帧作预览。"
    )


# ---------------------------------------------------------------------------
# 降级产物（纯函数，可离线断言）
# ---------------------------------------------------------------------------

def degraded_parts(part: Any, context: Any) -> list[ContentPart]:
    """`VideoPart` → `[说明, 图]` 或 `[说明]`。**永远不返回空列表。**

    `context` 是 wit 的 `ContentTransformContext`（可空传 `None`，只用它的
    `provider` / `reason` 两个字段，都做了兜底）。之所以把 provider 自报的
    `reason` 原样抄进说明：**我们猜的原因**与**provider 实际给的原因**
    是两件事，后者才是调用方能拿去改配置的那条信息。
    """
    provider = str(getattr(context, "provider", "") or "（未知 provider）")
    reason = str(getattr(context, "reason", "") or "").strip()
    why = f"provider 自报的原因：{reason}" if reason else "provider 未给出原因"

    data = getattr(part, "data", None)
    uri = getattr(part, "uri", None)
    mime = str(getattr(part, "mime_type", "") or "（未声明 MIME）")

    if not data:
        # ⚠️ 只有地址的形态**不会被自动抓取**：本项目的取件通道刻意不出网。
        #    这一段要说得让人能据此行动，而不是只说「取不到」。
        return [
            TextPart(
                text=(
                    f"{DEGRADE_NOTICE_PREFIX}模型服务（{provider}）读不了视频，"
                    f"而这次拿到的视频**只有地址、没有字节**（uri={uri!r}，{why}）。"
                    "系统**不会自动去取这个地址** —— 本项目的取件通道刻意不出网。"
                    "所以本次模型**看不到任何画面**，也看不到首帧。"
                    "如需分析，请把视频**作为附件重新发送**（拿到 inbox: 句柄即可）。"
                )
            )
        ]

    if len(data) > MAX_DEGRADE_BYTES:  # 只为挡住「整段视频塞进内存」的极端输入
        # 不做静默截断：截断过的字节解出来的「事实」看上去和真的一模一样。
        return [
            TextPart(
                text=(
                    f"{DEGRADE_NOTICE_PREFIX}这段字节有 {len(data)} 字节，"
                    f"超过了降级器在**模型请求路径**上的上限 "
                    f"{MAX_DEGRADE_BYTES} 字节，因此**没有抽帧**"
                    "（截断后再抽帧会得出看起来同样可信的错结论）。"
                    f"（{provider}，{why}）"
                )
            )
        ]

    raw = bytes(data)
    frame, note = preview_bytes(raw)
    if frame is not None:
        picture = ImagePart(mime_type=frame.mime_type, data=frame.data)
        how = preview_source_note(frame) + " 字节未经任何重编码。"
    elif sniff_media_type(raw) == "image/gif":
        # ⚠️ GIF **本身就是图片格式** ⇒ 连光栅化都不必做，原样发过去。
        #    这一支不引入 Pillow、也不重编码：走的是"取到什么字节发什么字节"，
        #    正是模块纪律第 3 条想要的那种降级。
        #    （对比：`video/frames.py` 的逐帧光栅化要 Pillow，且是为**像素域分析**
        #      服务的 —— 那是检测链路的事，不是"给模型看一眼"的事。）
        picture = ImagePart(mime_type="image/gif", data=raw)
        how = (
            "发过去的是这份动图的**原文件**（GIF 本身就是图片格式，未经任何重编码）。"
            "⚠️ 模型通常只看到它的**第 1 帧** —— 逐帧的像素域分析请走视频鉴伪链路。"
        )
    else:
        return [
            TextPart(
                text=(
                    f"{DEGRADE_NOTICE_PREFIX}模型服务（{provider}）读不了视频（{why}），"
                    "**本次也没能抽到任何一帧**作为替代画面，所以模型这次"
                    "看不到任何图像内容。原因：" + note +
                    " ⇒ 这不是「视频没问题」，而是**画面层整项没有进入模型视野**。"
                )
            )
        ]

    notice = (
        f"{DEGRADE_NOTICE_PREFIX}模型服务（{provider}）读不了视频（{why}），"
        f"所以这次只把**一帧**降级成图片发过去。"
        f"原视频是 {mime}、{len(raw)} 字节。" + how +
        " ⚠️ 一帧 ≠ 全片：被剪掉的那几秒大概率不在这张图上，"
        "而容器结构 / 样本表 / 时间线这些线索本来也不在画面上 —— "
        "要它们请走**视频鉴伪**链路（那是代码算的，不是模型看的）。"
    )
    # 说明在前、图在后：顺序本身是判据的一部分（见模块纪律第 1 条）。
    return [TextPart(text=notice), picture]


def video_to_media(part: Any, context: Any) -> list[ContentPart]:
    """`ContentTransform` 的 handler：`VideoPart` → `[说明, 首帧图]`。

    只做一层防御包装：**任何**异常都折成一段可读文本。
    理由同 `preview_bytes` —— 这里的失败代价是「整个 run 挂掉」，不是「少一条线索」。
    """
    try:
        return degraded_parts(part, context)
    except Exception as exc:  # pragma: no cover - 纯防御
        return [
            TextPart(
                text=(
                    f"{DEGRADE_NOTICE_PREFIX}降级处理器自己出错了"
                    f"（{stable_reason(exc)}）—— 本次模型看不到任何视频内容。"
                )
            )
        ]


# ---------------------------------------------------------------------------
# ContentTransform（PPT 4-11）
# ---------------------------------------------------------------------------

def video_content_transform() -> ContentTransform:
    """注册用的一条 Transform。抽成函数是为了让断言能直接拿到对象。"""
    return ContentTransform(source_type="video", handler=video_to_media)


class VideoFallbackHooks(WitHooks):
    """把视频降级器注册给 wit。挂在**可能收到入站视频**的角色上。

    挂载点见 `VIDEO_CAPABLE_ROLES`。不挂的后果不是「少一条线索」，而是
    `VideoPart` 一路走到 provider ⇒ `UnsupportedContentError` ⇒ **整个 run 失败**：
    用户传了一段视频，得到的是一句「服务异常」，而不是「我看不了视频，因为…」。
    """

    id = "legal.video.fallback"
    priority = VIDEO_HOOK_PRIORITY

    def content_transforms(self, ctx: Any) -> list[ContentTransform]:
        return [video_content_transform()]


# ---------------------------------------------------------------------------
# 能力探测 + GatewayAdapter（PPT 4-10）
# ---------------------------------------------------------------------------

def probe_video_support(provider: Any, *, role: str = "user") -> tuple[bool, str | None]:
    """问 provider「你读得了视频吗」。返回 `(读得了吗, provider 自报的原因)`。

    ⚠️ 判据只认 provider **自己**的回答（`supports_content`），不认我们的猜测、
    也不认配置里的声明 —— 猜测和声明都不会因为猜错而报错。
    探测失败时返回 `(False, 「<异常>」)`：**探测不出来就当不支持**，
    因为「当作支持」的失败模式是 run 直接挂。
    """
    probe = VideoPart(mime_type="video/mp4", data=_PROBE_BYTES)
    fn = getattr(provider, "supports_content", None)
    if fn is None:
        return False, "该 provider 没有 `supports_content`，无法探测 ⇒ 按不支持处理"
    try:
        result = fn(probe, role=role)
    except Exception as exc:  # pragma: no cover - 防御
        return False, f"探测时抛异常（{stable_reason(exc)}）⇒ 按不支持处理"
    if isinstance(result, tuple) and len(result) == 2:
        return bool(result[0]), (str(result[1]) if result[1] else None)
    # ⚠️ 返回形状不符时**不当成 True**（协议见 PPT 4-8：`-> tuple[bool, str | None]`）。
    #    这里必须 fail-closed：形状都答不对的 provider，把它当「支持」的失败模式是
    #    VideoPart 一路发出去 ⇒ run 直接挂；当「不支持」的失败模式只是多降级一次。
    #    代价不对称，所以方向不对称。（2026-10-03 复查夹出：原写法 `bool(result)`
    #    会把裸 `True` 放行 —— 注释说的是一回事，代码做的是另一回事。）
    return False, (
        f"provider 返回了非 (bool, reason) 形状（实得 {type(result).__name__}），"
        "无法按契约解读 ⇒ 按不支持处理"
    )


class VideoGatewayAdapter(GatewayAdapter):
    """PPT 4-10 那条路：网关后端**实际**支持视频、官方 SDK 表达不了时的翻译层。

    ⚠️ 本部署**不启用它** —— 本机没有网关。写出来不是为了好看，而是为了把
    「这件事该在哪一层做、由谁决定」钉在代码里：`added_input_modalities` 是一句
    **承诺**（挂上就等于告诉 wit「可以把 VideoPart 发出去了」），所以它只能由
    `model_provider_for()` 在**探测 + 显式传参**两件事都成立时才挂。
    """

    name = "legal.video.gateway-adapter"
    added_input_modalities = frozenset({"video"})

    def adapt_request(self, request: Any, context: Any) -> Any:
        # 报文层只做翻译、不写业务规则（PPT 的开发者提示语）：本机没有网关后端，
        # 这里**原样返回**。真正把视频降级成图片的是 `ContentTransform` —— 分工不同。
        return request

    def adapt_response(self, response: Any, context: Any) -> Any:
        return response


def model_provider_for(base: Any, *, backend_reads_video: bool = False) -> Any:
    """按**现场探测的结果**决定要不要给 base 套一层协议适配。

    返回的是下面三者之一，且**每个分支对应一个可验证的事实**：

        provider 自己就认视频        → 原样返回 `base`（什么都不用做）
        不认 + 调用方确认后端认视频  → `AdaptedModelProvider(base, [adapter])`
        不认 + 后端也不认            → 原样返回 `base`，由 `ContentTransform` 降级

    ⚠️ `backend_reads_video` **刻意做成参数而不是环境变量**：环境变量会被
    systemd / shell rc / 父进程一路继承过去，纪律守不住；而它一旦被误开，
    后果是模型收到它读不了的 `VideoPart` ⇒ **整个 run 失败**。同形的先例见
    `evidence/fetch.py` 的 `enable_local_file`（从环境变量改成进程内显式调用）。
    """
    if base is None:
        # 编译期检查会传 `model=None`（假模型）。探测无从谈起 ⇒ 原样返回，
        # **不许**在这里造一个 provider 出来：那会让「离线能跑」变成一句谎话。
        return base
    ok, _reason = probe_video_support(base)
    if ok or not backend_reads_video:
        return base
    return AdaptedModelProvider(base, adapters=[VideoGatewayAdapter()])


__all__ = [
    "DEGRADE_NOTICE_PREFIX",
    "MAX_DEGRADE_BYTES",
    "VIDEO_CAPABLE_ROLES",
    "VIDEO_HOOK_PRIORITY",
    "VideoFallbackHooks",
    "VideoGatewayAdapter",
    "degraded_parts",
    "model_provider_for",
    "pick_preview_frame",
    "preview_bytes",
    "preview_source_note",
    "probe_video_support",
    "video_content_transform",
    "video_to_media",
]
