"""把「随任务附件进来的材料」接到检测链上 —— 补的是模型够不着的那一段。

（2026-10-02 起按 `kind` 泛化到音频：链路、纪律、边界都没变，
只是"图片"这个词在若干处换成了模态画像里的措辞与参数名。
**默认 `kinds=("image",)` 保持不变** —— 图片链的既有行为一字未动。）

════════════════════════════════════════════════════════════════════════
为什么需要一个 Hook，而不是在工具里多写一句
════════════════════════════════════════════════════════════════════════

`evidence.inspect_image` 的入参只有一条 `image_uri`（这是 AIP 的约束，不是设计
选择：模型工具层的内容 schema 只暴露 uri，见 `evidence/contracts.py`）。
但**用户上传的图片根本不是以 URI 下发的**：它是 `FileDataItem.bytes`，
wit 把它转成 `ImagePart(data=字节)` 放在消息里。

于是出现一个缺口：

    · 字节**在**（`state.messages` 里躺着，代码伸手就能拿）；
    · 但模型够不着（它拿不到 base64，也不该经手证据字节）；
    · 工具只认 URI ⇒ **谁都递不进去**。

本 Hook 就是把这件事做掉：在字节还在手上的时候收进收件箱（`evidence/inbox.py`），
然后把一个 **64 位内容哈希**交给模型。模型要做的只剩"照抄一个短 token"。

════════════════════════════════════════════════════════════════════════
入站附件有两种形态，**都要接**
════════════════════════════════════════════════════════════════════════

AIP 层允许调用方二选一（`wit.acps.aip.data_item_to_content_part` 实测）：

    FileDataItem.bytes  →  ImagePart(data=…)  →  收件箱：落盘、发句柄
    FileDataItem.uri    →  ImagePart(uri=…)   →  **只转述地址**，取不取由上层定

⚠️ "平台会发哪一种"是个**问错了的问题** —— 这是调用方的选择，不是平台的固定
行为。所以只支持一种，另一种就被**静默丢掉**，而"没收到图"与"收到了但没当回事"
在结论里长得一模一样。对可变输入的正确做法是**两种都接、各自说明**。

════════════════════════════════════════════════════════════════════════
两条路，各走各的
════════════════════════════════════════════════════════════════════════

**路一（本角色自己收到附件）** —— `evidence-image` 被别的 Agent 直接调用，
对方把图连同任务发过来。这里 `on_run_start` 就能看到 `ImagePart`：

    ImagePart(data=…)  →  收件箱  →  system 注入句柄  →  模型调 inspect(inbox:…)
    ImagePart(uri=…)   →  system 转述地址  →  模型自行决定是否填进 inspect(…)

**路二（主控收到用户的附件，要转发给下游）** —— 主控够不着 Partner 的内容通道，
模型调 `acps_delegate` 时只能给字符串。所以让模型**照抄句柄**，由本 Hook 在
`wrap_tool_call` 里把句柄换回真实字节、改走 `aip_delegate(request=ContentInput)`
把字节送过去。这样跨进程的字节传递**由代码完成**，模型全程只传递一个 hash。

⚠️ 路二刻意做成"句柄驱动"而不是"自动附带"：一旦改成"有图就塞给所有下游"，
煽动性识别、危害评估这些与图像无关的 Partner 也会收到一份几 MB 的字节 ——
既浪费通道，也让"谁看过这张图"变得说不清。**传不传，由请求内容里的句柄决定。**

════════════════════════════════════════════════════════════════════════
纪律
════════════════════════════════════════════════════════════════════════

  · **纯文本任务下必须完全不动**：没有附件 ⇒ `context()` 不注入、`wrap_tool_call`
    不干预。这保证这个 Hook 对已经过审的纯文本链路是零影响。
  · **扫描是幂等的**：`inbox.stage()` 按内容哈希命名，扫一百遍也只有一份。
  · **hook 里出错不许拖垮整轮 run**：任何异常都只记录、放行，
    与 `termination.py` 的既有做法一致 —— 检测功能是加分项，不该让主链路陪葬。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from wit.kernel.context import ContextBlock
from wit.kernel.hooks import HookContext, RunStartEvent, WitHooks

from .evidence import inbox

#: 收件箱根 token / 条目的 private_state 键前缀
_STATE_PREFIX = "legal.evidence-inbox"

#: 默认只收图片。**保持这个默认值不变**：`evidence-image` 与既有的
#: 通过验收的行为都建立在它上面；加模态靠显式传 `kinds=`，不靠改默认。
DEFAULT_KINDS: tuple[str, ...] = ("image",)


@dataclass(frozen=True)
class KindProfile:
    """一种附件模态在**措辞**与**上线格式**上的差异。

    ⚠️ 抽成表的理由与 `verdict.summarize_findings` 同源：模态一多，
    "每加一种就在每一段文案里加一个 if"必然漏 —— 漏掉的那处不会是
    崩溃，而是**注入给模型的话只说对了一半**（例如音频被写成"图片"、
    或句柄被填进 `image_uri`），模型照着做就是一次静默失败的委派。
    所以把差异集中到一处，缺哪个字段当场看得到。

    `part_class` 用**类名字符串**而不是类对象：`wit.kernel.content` 里
    音频 / 视频这两个类在旧版本 wit 上不存在，模块级 `import` 会让
    整个 Hook 起不来 —— 而那会把图像链一起拖下去。取名字、在用时解析。
    """

    noun: str            # 单数（"一张图片"里的"图片"）
    counter: str         # 量词短语（"2 张图片"里的"张图片"）
    uri_arg: str         # 该模态工具的参数名
    part_class: str      # wit 里对应的 ContentPart 类名
    default_mime: str    # 拿不到 MIME 时的兜底


KIND_PROFILES: dict[str, KindProfile] = {
    "image": KindProfile(
        noun="图片", counter="张图片", uri_arg="image_uri",
        part_class="ImagePart", default_mime="image/jpeg",
    ),
    "audio": KindProfile(
        noun="音频", counter="段音频", uri_arg="audio_uri",
        part_class="AudioPart", default_mime="audio/wav",
    ),
    # 2026-10-02 新增：视频。`uri_arg="video_uri"` 必须与
    # `video.contracts.VideoEvidenceRequest.video_uri` **逐字相同** ——
    # 对不上就会让模型把句柄填进一个不存在的参数名，而那种失败在日志里
    # 只表现为"模型没调用工具"（与 `audio` 那一项同一个坑）。
    "video": KindProfile(
        noun="视频", counter="段视频", uri_arg="video_uri",
        part_class="VideoPart", default_mime="video/mp4",
    ),
}


#: 「句柄查不到模态」时的取值。**刻意是一个不在 `KIND_PROFILES` 里的空串** ——
#: 这样 `profile_for()` 会走它自己那份中性兜底（"材料" + `FilePart`），
#: 而不是猜测一个具体模态。
#:
#: ⚠️ 2026-10-03 复查：两个注入点原先写的是 `kinds.get(handle, self.kinds[0])` ——
#:    对 `kinds=("image", "audio", "video")` 的角色（主控、证据鉴证 Leader），
#:    一旦某个句柄没登记上模态，就会被说成"图片"、并按 `image_uri` 去调工具。
#:    那句注释（"收进来的必然属于其中之一"）论证的是"属于其中之一"，
#:    而代码做的是"取第一顺位" —— 对单模态角色这两件事等价，对多模态角色不等价。
#:    猜错的代价不是崩溃，是**注入给模型的指令指向了错的参数名**，
#:    日志里只表现为"模型没调用工具"。宁可说"材料"（不够具体），也不说错。
UNKNOWN_KIND = ""


def profile_for(kind: str) -> KindProfile:
    """取模态画像；没登记过的 kind 用一份**中性**兜底并如实说明。

    兜底刻意不猜（不说成"图片"也不说成"音频"）：猜错会让注入给模型的
    指令指向一个不存在的工具参数，而那种失败在日志里只表现为
    "模型没调用工具"。中性措辞至少是**说得对但不够具体**。
    """
    return KIND_PROFILES.get(
        kind,
        KindProfile(
            noun="材料", counter="份材料", uri_arg="material_uri",
            part_class="FilePart", default_mime="application/octet-stream",
        ),
    )


def plan_delegation(
    arguments: Mapping[str, Any],
    *,
    items: Sequence[inbox.InboxItem],
    arg_partner: str = "partner_aic",
    arg_request: str = "request",
) -> dict[str, Any] | None:
    """**纯函数**：判断这次委派要不要由代码补上附件字节。

    返回 `{"partner_aic": …, "text": …, "items": [...]}`；
    不需要干预时返回 `None`（没提到句柄 / 参数不合法 / 句柄不在本次收件箱里）。

    ⚠️ 独立成纯函数是为了**可断言**：这条规则决定"证据字节会不会被送出去"，
    藏在 `wrap_tool_call` 的 if 里就只能靠人工 review 保证。
    """
    if not isinstance(arguments, Mapping):
        return None

    partner = arguments.get(arg_partner)
    if not isinstance(partner, str) or not partner.strip():
        return None

    text = _flatten_request_text(arguments.get(arg_request))
    if not text:
        return None

    handles = inbox.find_handles(text)
    if not handles:
        return None

    by_handle = {item.handle: item for item in items}
    # 只认**本次收件箱里真的存在**的句柄。模型若编了一个哈希，这里就落空 ——
    # 于是它拿不到任何字节，而不是"拿到别的什么"。
    hit = [by_handle[h] for h in handles if h in by_handle]
    if not hit:
        return None

    return {"partner_aic": partner.strip(), "text": text, "items": hit,
            "data_items": structured_data_items(arguments.get(arg_request))}


def structured_data_items(value: Any) -> list[dict[str, Any]]:
    """从 `acps_delegate` 的 `request` 里挑出**结构化数据项**（`{"type": "data", …}`）。

    ⚠️ 为什么接管时必须把它们一起带走（#123）：本 Hook 接管委派时会用收件箱里的
       字节**重写**整个 `request`（`_delegate_with_bytes`）。若不保留这些项，
       外层 `DebateChainGuardHooks` 刚并进去的**委托链**就会被吃掉 ⇒
       下游读不到链 ⇒ 跨进程链深**恰恰在有附件的那几条链上**静默失效
       （而"链传过去了"的判据跑在没有附件那条路上，全绿 ——
        与"没人核"长得一模一样）。
       ⚠️ 通用教训：**两个 Hook 都碰同一个字段时，改一处必须问另一处**：
          "被你重写掉的那个值，还有谁在用？"
    """
    if isinstance(value, str) or not isinstance(value, Sequence):
        return []
    out: list[dict[str, Any]] = []
    for part in value:
        if not isinstance(part, Mapping) or part.get("type") != "data":
            continue
        data = part.get("data")
        if isinstance(data, Mapping):
            out.append(
                {"data": dict(data), "metadata": dict(part.get("metadata") or {})}
            )
    return out


def _flatten_request_text(value: Any) -> str:
    """把 `acps_delegate` 的 `request` 参数压成纯文本。

    它可能是字符串，也可能是 `[{type: "text", text: …}, …]`（模型工具层允许的形状）。
    只取文本部分：附件部分在本端口径里**必然**是 URI 引用，不会被当成字节来源。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        chunks: list[str] = []
        for part in value:
            if isinstance(part, Mapping) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks)
    return ""


#: 抹掉句柄后填进去的人话。
#:
#: ⚠️ 刻意用**中性**的「材料」而不是「图片」：本 Hook 现在服务多种模态，
#: 而一句"（本次任务附带的图片）"送到音频链上，下游会以为附件丢了。
#: 一句说得不准的话比不说更坏 —— 收方会照着它去解释"为什么没看到音频"。
_HANDLE_PLACEHOLDER = "（本次任务附带的材料）"


def strip_handles(text: str, handles: Iterable[str]) -> str:
    """把句柄从给下游看的文本里抹掉。

    下游不需要知道我们的内部机构：它收到的应该是"附图一张，请鉴伪"，
    而不是一串 64 位哈希。抹掉也顺带避免了句柄在下游被再次转发出去。

    ⚠️ **必须与 `inbox.find_handles()` 共用同一个正则**（`inbox.HANDLE_IN_TEXT_RE`），
    否则两侧的"什么算一个句柄"会各自漂。2026-10-02 实测过一次真实缺陷：旧实现是
    **朴素 replace**，只试「整串小写」和「整串大写」两种形态 ⇒ 与识别侧对不上：

        inbox:a1b2…  识别 ✓  抹掉 ✓
        inbox:A1B2…  识别 ✓  抹掉 ✗  ⇒ 内部哈希泄漏给下游
        INBOX:A1B2…  识别 ✗  抹掉 ✓  ⇒ **接管落空、字节静默丢失**（不报任何错）
        Inbox:a1b2…  识别 ✗  抹掉 ✗  ⇒ 同上

    现在改为"用同一个正则扫出**所有**句柄形态的 token，大小写归一后与 `handles`
    比对"—— 于是"**能被识别的必然能被抹掉**"由构造保证，不再依赖两处持续对齐。

    `handles` 的语义保持不变：只有**本次真的要发出去的那几个**才抹（其余同形态
    token 不是我们的句柄，抹了反而会让下游的文本与原话对不上）。
    """
    raw = str(text)
    # 归一化交给 `inbox.parse_handle()`（唯一定义处）；形状不合法的一律忽略 ——
    # ⚠️ 这里**不能抛**：本函数的调用点在 `wrap_tool_call` 的 try 里，
    #    一抛就会被"接管失败，已放行原调用"兜走 ⇒ 变成"字节没送出去、哈希照旧泄漏"，
    #    正是本函数要防的那种结果。而反正形状不合法就不可能出现在下面扫出的 token 里。
    known: set[str] = set()
    for handle in handles:
        try:
            known.add(inbox.parse_handle(handle))
        except inbox.InboxRefused:
            continue
    if not known:
        return raw

    def _sub(match: re.Match[str]) -> str:
        # 正则已保证形状合法 ⇒ `parse_handle` 只做归一，不会抛。
        return _HANDLE_PLACEHOLDER if inbox.parse_handle(match.group(0)) in known else match.group(0)

    return inbox.HANDLE_IN_TEXT_RE.sub(_sub, raw)


class EvidenceInboxHooks(WitHooks):
    """收件箱：把入站附件收下来、发句柄，并在委派时由代码把字节送出去。

    参数
    ----
    kinds
        要收的附件种类（默认 `("image",)`）。音频 / 视频接入时在这里加。
    delegate_tool
        需要接管的委派工具名（默认 `acps_delegate`）。
        ⚠️ 传 `""` 可以关掉"路二" —— 给那些**没有**这个工具的角色用。
        挂载前先问一句"谁来调它"：挂在一个根本不委派的角色上，
        那段逻辑就是死代码（见 `MEMORY.md` 里 DebateChainGuardHooks 的教训）。
    verbose
        把每次收件 / 注入打到 stderr。
    """

    #: wit 的语义是"priority **越大越靠内层**"（900 = 最内层，50 = 最外层）。
    #: 这个值要同时满足两件事，而**只有第一件需要争**：
    #:
    #:   ① 必须在 `DebateChainGuardHooks`（`priority = 50`，边界闸门：目标是不是
    #:      自己人 / 链深 ≤ 上限 / 不得成环）**之内** —— 闸门先校验，本 Hook 再接管。
    #:      ⚠️ 取一个**小于 50** 的值就会把闸门整体短路：接管直接返回，
    #:      `handler` 那一侧（闸门在里）**一次都不会被执行**。后果是 discovery
    #:      捞到的外部团队 Agent 可以直接拿到材料字节，而且**不报任何错**。
    #:      2026-10-02 实测：链路里放一个 `priority = 50` 的探针 Hook，
    #:      本 Hook 取 100 时探针记到 `enter/exit`，取 10 时探针**一次都没被调用**。
    #:      断言见 `tools/verify_evidence.py` 第 9 节 ⑥（含真链路上的行为断言）。
    #:   ② 必然在 wit 注册的那个工具 handler **之外**（所有 Hook 都包在 handler 外层，
    #:      这是 Hook 的定义）⇒"接管这次调用"本身不需要靠优先级去争取。
    #:      （`AipDelegationHooks` 虽然也是 50，但它只实现 `on_run_start` / `context`，
    #:       没有 `wrap_tool_call` ⇒ 与它谁先谁后都不影响结果。）
    #:
    #: 留 100 而不是 51：与 50 拉开距离，免得以后有人把闸门调成 60 时
    #: 顺序被悄悄翻过去、而所有断言（当时）都还是绿的。
    priority = 100
    id = "legal.evidence-inbox"

    def __init__(
        self,
        *,
        kinds: Sequence[str] = DEFAULT_KINDS,
        delegate_tool: str = "acps_delegate",
        verbose: bool = False,
    ) -> None:
        self.kinds = tuple(kinds) or DEFAULT_KINDS
        self.delegate_tool = str(delegate_tool or "")
        self.verbose = verbose
        #: 供离线验证 / 排障使用：记录每一次接管
        self.trace: list[dict[str, Any]] = []

    # -- 日志 ---------------------------------------------------------------

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[evidence-inbox] {message}", file=sys.stderr, flush=True)

    def _record(self, event: Mapping[str, Any]) -> None:
        if len(self.trace) < 64:
            self.trace.append(dict(event))

    # -- 收件 ---------------------------------------------------------------

    def _state_key(self, name: str) -> str:
        return f"{_STATE_PREFIX}.{name}"

    def _refresh(self, ctx: HookContext) -> list[inbox.InboxItem]:
        """扫一遍消息，把还没收过的内联附件收进收件箱。**幂等。**

        刻意每次 `context()` / `wrap_tool_call()` 前都扫：附件不只在 `on_run_start`
        那一刻存在（下游交回的任务也可能带回图片），而扫描成本只是遍历一遍消息列表。
        """
        state = getattr(ctx, "private_state", None)
        if not isinstance(state, dict):
            return []

        items: list[inbox.InboxItem] = list(state.get(self._state_key("items")) or [])
        refused: list[str] = list(state.get(self._state_key("refused")) or [])
        #: 句柄 → kind。**必须在这里记下来，不能靠事后从 MIME 反推**：
        #: `kind` 是"调用方把它放在哪一种 ContentPart 里"，而 MIME 是它**自称**的类型
        #: （两者可以不一致：一个 audio/wav 声明被塞进 FilePart 也不是不可能）。
        #: 委派时按哪个类构造 Part 决定了对端能不能识别，所以要用**我们收件时看到的
        #: 那个 kind**，不是反推出来的猜测。
        kinds: dict[str, str] = dict(state.get(self._state_key("kinds")) or {})

        root = state.get(self._state_key("root"))
        if root is None:
            return items

        parts: list[Any] = []
        for message in getattr(ctx.state, "messages", None) or ():
            parts.extend(getattr(message, "content", None) or ())

        # ⚠️ 入站附件有**两种形态**，必须都接：`data` 形态落收件箱换句柄，
        #    `uri` 形态只是地址（本 Hook 不替调用方决定要不要去取）。
        #    只认其中一种 = 另一种被**静默丢弃**，而"没收到图"与"收到了但没用"
        #    在结论里长得一模一样 —— 那正是本项目最忌讳的那种缺陷。
        refs: dict[tuple[str, str], dict[str, str]] = {}

        known = {item.handle for item in items}
        for kind in self.kinds:
            for mime, data, name in inbox.attachments(parts, kind=kind):
                # 先算句柄再决定要不要写盘：重扫一遍消息不该伴随一遍写盘。
                if inbox.handle_for(data) in known:
                    continue
                try:
                    item = inbox.stage(data, root=root, mime_type=mime, name=name)
                except inbox.InboxRefused as exc:
                    if str(exc) not in refused:
                        refused.append(str(exc))
                        self._log(f"附件被拒：{exc}")
                    continue
                items.append(item)
                known.add(item.handle)
                kinds[item.handle] = kind
                self._log(f"收到附件 {item.handle} ({kind}, {item.size} 字节)")
                self._record(
                    {"event": "staged", "handle": item.handle, "kind": kind, "size": item.size}
                )

            for mime, uri, name in inbox.references(parts, kind=kind):
                # 纯派生值，每次重扫都重算 ⇒ 天然不会重复累积。
                refs.setdefault(
                    (kind, mime, uri),
                    {"kind": kind, "mime_type": mime, "uri": uri, "name": name or ""},
                )

        state[self._state_key("items")] = items
        state[self._state_key("refused")] = refused
        state[self._state_key("kinds")] = kinds
        state[self._state_key("refs")] = list(refs.values())
        return items

    def _kinds_of(self, ctx: HookContext) -> dict[str, str]:
        """取回"哪个句柄属于哪种模态"（由 `_refresh()` 在收件时记下）。"""
        state = getattr(ctx, "private_state", None)
        if not isinstance(state, dict):
            return {}
        return dict(state.get(self._state_key("kinds")) or {})

    def _fallback_kind(self) -> str:
        """句柄没登记模态时的兜底取值。**能不能"猜"取决于本角色有几种模态。**

        ⚠️ 允许猜的唯一情形是**单模态角色**（`kinds` 只有一项）：那时
        "收进来的必然属于其中之一"成立，猜与不猜是同一件事；而退回中性口径
        反而有害 —— `FilePart` 会让下游按模态分派找不到它（下面那条
        "不静默降级成 FilePart"的注释就是为此写的）。

        多模态角色（主控、证据鉴证 Leader：`("image", "audio", "video")`）
        **绝不能猜第一顺位** —— 把视频说成"图片"、把句柄写进 `image_uri`，
        在日志里只表现为"模型没调用工具"。那种角色走中性口径（`UNKNOWN_KIND`）。

        （2026-10-03 复查：原先两处一律 `self.kinds[0]`；改成一律中性之后
         `verify_evidence` 立刻报出 4 条失败 —— 那 4 条是图像链（单模态）的
         真实通路。**"一律中性"同样是猜，只是猜向另一边**；分界在"能不能猜"，
         不在猜哪一边。）
        """
        return self.kinds[0] if len(self.kinds) == 1 else UNKNOWN_KIND

    def _references(self, ctx: HookContext) -> list[dict[str, str]]:
        """取回上一次 `_refresh()` 记下的 **uri 形态** 附件（只有地址、没有字节）。"""
        state = getattr(ctx, "private_state", None)
        if not isinstance(state, dict):
            return []
        return [dict(r) for r in (state.get(self._state_key("refs")) or [])]

    async def on_run_start(self, event: RunStartEvent) -> None:
        ctx = event.context
        state = getattr(ctx, "private_state", None)
        if not isinstance(state, dict):
            return
        try:
            root = inbox.inbox_root(ctx.workspace)
            # ⚠️ 绑定到**当前上下文**而不是模块全局：一个进程内可能先后跑多个 run，
            #    而收件箱属于"这次任务带来的那份字节"，不该跨任务泄漏。
            state[self._state_key("root_token")] = inbox.bind_root(root)
            # ⚠️ **归属**也要一起绑（2026-10-04 补）：收件箱的字节按内容哈希命名，
            #    所以"这份字节存在"与"这份字节属于本次任务"是两件事。没有归属时
            #    `resolve()` 只能靠文件在不在来判 —— 于是上一轮落下的字节
            #    会被这一轮取去当证据，而报告上看不出任何异常。
            #    归属 id 优先取**发起轮**的 `run_id`（能直接对上台账），
            #    服务模式（没有发起轮）则现造一个"本次 run"唯一的 id。
            #    与 root_token 同款：不显式解绑，因为下一次 `on_run_start`
            #    会换一个新的归属，而**每个 run 的归属本来就不同**。
            state[self._state_key("owner_token")] = inbox.bind_owner()
            state[self._state_key("root")] = root
            items = self._refresh(ctx)
            if items:
                self._log(f"本次任务自带 {len(items)} 张图，收件箱 {root}")
        except Exception as exc:  # noqa: BLE001 - 加分项不许拖垮主链路
            self._log(f"on_run_start() 收件失败，已跳过：{exc!r}")

    # -- 告诉模型：图在这，句柄是这个 ---------------------------------------

    def context(self, ctx: HookContext) -> list[ContextBlock]:
        try:
            items = self._refresh(ctx)
        except Exception as exc:  # noqa: BLE001
            self._log(f"context() 收件失败，已跳过：{exc!r}")
            return []
        refs = self._references(ctx)
        if not items and not refs:
            # **纯文本任务在这里退场** —— 一个字都不注入。
            return []

        kinds = self._kinds_of(ctx)
        # ⚠️ 句柄 → 模态（收件时记的）。查不到时**不许**猜成 `self.kinds[0]`
        #    —— 见 `UNKNOWN_KIND` 的注释：对多模态角色那就是"把视频说成图片"。
        multi = len({kinds.get(i.handle, self._fallback_kind()) for i in items}
                    | {str(r.get("kind") or self._fallback_kind()) for r in refs}) > 1

        def label(kind: str) -> str:
            """多模态时给每行加一个模态前缀，免得模型把音频句柄填进 image_uri。"""
            return f"[{profile_for(kind).noun}] " if multi else ""

        def count_phrase(counter: int, kind: str) -> str:
            return f"{counter} {profile_for(kind).counter}" if counter else ""

        grouped_items: dict[str, list[inbox.InboxItem]] = {}
        for item in items:
            grouped_items.setdefault(kinds.get(item.handle, self._fallback_kind()), []).append(item)
        grouped_refs: dict[str, list[dict[str, str]]] = {}
        for ref in refs:
            grouped_refs.setdefault(str(ref.get("kind") or UNKNOWN_KIND), []).append(ref)

        summary = "、".join(
            count_phrase(len(grouped_items.get(k, [])) + len(grouped_refs.get(k, [])), k)
            for k in sorted(set(grouped_items) | set(grouped_refs))
        )
        lines: list[str] = [
            f"本次任务**自带 {summary}**，按下面两种形态各说各的：",
            "",
        ]
        if items:
            lines.append("【形态一 · 字节已收存（可用句柄直接检查）】")
            for kind in sorted(grouped_items):
                for item in grouped_items[kind]:
                    lines.append(
                        f"  · {label(kind)}句柄 {item.handle}"
                        f"（{item.mime_type or '类型未声明'}，{item.size} 字节"
                        f"{'，' + item.name if item.name else ''}）"
                    )
            lines.append("")
        if refs:
            lines.append("【形态二 · 只有地址，**没有字节**（系统不会自动去取）】")
            for kind in sorted(grouped_refs):
                for ref in grouped_refs[kind]:
                    lines.append(
                        f"  · {label(kind)}{ref['uri']}（{ref['mime_type'] or '类型未声明'}"
                        f"{'，' + ref['name'] if ref.get('name') else ''}）"
                    )
            lines.append("")

        # ⚠️ 每个模态**分开说**该填哪个参数：把音频句柄填进 `image_uri` 的后果
        #    不是报错，而是那次派发**什么也没送到**（下游拿不到字节、只收到一段文本）。
        for kind in sorted(grouped_items):
            name = profile_for(kind).noun
            lines.append(
                f"· 形态一的{name}：**把句柄原文填进 `{profile_for(kind).uri_arg}`** 即可。"
            )
        if items:
            lines.append(
                "⚠️ 句柄是**内容的哈希，不是文件路径**：不要自己拼、不要改写、不要当成路径去取文件。"
            )
            lines.append(
                "⚠️ 要把形态一的材料转给下游 Agent，请把**句柄**原文写进委派请求 —— "
                "系统会把字节随请求一并送达。"
            )
        for kind in sorted(grouped_refs):
            prof = profile_for(kind)
            if kind == "image":
                # ⚠️ 这一段的**措辞一字不能改**：`verify_evidence.py` 按原文断言
                #    "取不到时如实写「未能取得」、不许凭地址推断内容"。
                #    泛化时把它改成"该材料"曾让断言当场变红 —— 而那条断言挡的正是
                #    "模型凭一个 URL 猜图片内容"这种最危险的编造，不能因为改措辞
                #    而被削弱。模态专属的话就留在模态分支里。
                advice = (
                    "能否取到取决于出网白名单 —— 被拒绝时，请把「未能取得该图片」"
                    "如实写进结论，**不要凭地址、文件名或任何外部描述推断图片内容**。"
                )
            else:
                advice = (
                    "⚠️ **这一侧没有任何出网通道**：这个地址一定取不到。"
                    "请把它如实转告调用方，并请对方改为**随任务附件发送** —— "
                    "不要凭地址、文件名或任何外部描述推断它的内容。"
                )
            lines.append(
                f"· 形态二的{prof.noun}：把**地址原文**填进 `{prof.uri_arg}`。" + advice
            )
        return [
            ContextBlock.text(
                title="本次任务自带的材料（收件箱 / 仅有地址）",
                value="\n".join(lines),
                metadata={
                    "source": self.id,
                    "count": len(items) + len(refs),
                    "staged": len(items),
                    "references": len(refs),
                    "kinds": sorted({kinds.get(i.handle, self._fallback_kind()) for i in items}
                                    | {str(r.get("kind") or self._fallback_kind()) for r in refs}),
                },
            )
        ]

    # -- 路二：委派时由代码把字节送出去 -------------------------------------

    async def wrap_tool_call(self, ctx: HookContext, tool_call: Any, handler: Any) -> dict:
        name = getattr(tool_call, "name", "")
        if not self.delegate_tool or name != self.delegate_tool:
            return await handler(tool_call)
        try:
            items = self._refresh(ctx)
            if not items:
                return await handler(tool_call)
            plan = plan_delegation(dict(getattr(tool_call, "arguments", None) or {}), items=items)
            if plan is None:
                return await handler(tool_call)
            return await self._delegate_with_bytes(ctx, plan)
        except Exception as exc:  # noqa: BLE001 - 接管失败就放行，让原委派照常走
            self._log(f"委派接管失败，已放行原调用：{exc!r}")
            return await handler(tool_call)

    async def _delegate_with_bytes(self, ctx: HookContext, plan: Mapping[str, Any]) -> dict:
        """把句柄换回真实字节，走 `aip_delegate(request=ContentInput)` 发出去。"""
        from wit.kernel import content as content_module
        from wit.kernel.content import DataPart, TextPart

        items: Sequence[inbox.InboxItem] = plan["items"]
        handles = [item.handle for item in items]
        kinds = self._kinds_of(ctx)
        # 下游不该看见我们的内部机构，也不需要那串哈希 —— 给它人话。
        body = strip_handles(str(plan["text"]), handles)
        tally: dict[str, int] = {}
        for item in items:
            kind = kinds.get(item.handle, self._fallback_kind())
            tally[kind] = tally.get(kind, 0) + 1
        attached = "、".join(
            f"{n} {profile_for(k).counter}" for k, n in sorted(tally.items())
        )
        body += f"\n\n（随本请求附带 {attached}，请用系统提供给你们的句柄或附件通道处理。）"

        parts: list[Any] = [TextPart(text=body)]
        for item in items:
            kind = kinds.get(item.handle, self._fallback_kind())
            prof = profile_for(kind)
            part_class = getattr(content_module, prof.part_class, None)
            if part_class is None:
                # ⚠️ 不静默降级成 FilePart：那会让下游收到一个它按模态分派找不到的
                #    东西（"文件附件"而不是"音频"），而调用方这边看起来一切正常。
                #    宁可这一次委派干脆失败 —— 失败至少是响的。
                raise RuntimeError(
                    f"当前 wit 版本没有 {prof.part_class}，无法把 {kind} 附件交给下游"
                    f"（句柄 {item.handle}）。请升级 wit，或改用不经过附件通道的方式提交。"
                )
            data, mime = inbox.load(item.handle)
            parts.append(
                part_class(mime_type=mime or item.mime_type or prof.default_mime,
                           data=data, name=item.name)
            )

        # ⚠️ 接管会**重写**整个 request ⇒ 必须把外层 Hook 并进去的结构化数据
        #    原样带走（委托链就在里面，见 `structured_data_items` 的说明）。
        #    少这一步，跨进程链深会**只在有附件的链上**失效，而判据全绿。
        for entry in plan.get("data_items") or []:
            parts.append(
                DataPart(data=dict(entry["data"]), metadata=dict(entry.get("metadata") or {}))
            )

        self._log(f"委派附字节：partner={plan['partner_aic']} 附件={handles} kinds={tally}")
        self._record(
            {"event": "delegated_with_bytes", "partner": plan["partner_aic"],
             "handles": handles, "kinds": tally}
        )
        # ⚠️ 这里走代码层的 `aip_delegate`（`request` 可以是含 `ImagePart(data=…)`
        #    的 `ContentInput`），而不是模型工具层那条只认 uri 的路。
        #
        #    返回结构**与原生 `acps_delegate` handler 一致** —— 这不是推断，是调用链：
        #      · handler 内部：`A.delegate(state, partner_aic, request, delivery, notify_on_states)`
        #        其中 `A = ctx.runtime._ensure_aip_coordinator()`
        #      · `aip_delegate` 内部：`coordinator.delegate(state, partner_aic, request,
        #        str(A2.delivery), A2.notify_on_states)`，其中 A2 = runtime 上固定的那个
        #        `AipDelegationHooks` —— 而 `acps_delegate` 这个工具本就是**它**在
        #        `on_run_start` 里加的，所以这里 `self.delivery` 与 `A2.delivery` 是同一个值。
        #    ⇒ 同一个 coordinator、同一个方法、同一组参数语义 ⇒ 同一个返回值。
        #    两条路唯一的区别是**入参能力**：工具层受 TOOL_CONTENT_INPUT_SCHEMA 约束
        #    只能给 uri，代码层可以给字节。这正是本分支存在的理由。
        return await ctx.runtime.aip_delegate(
            ctx.state, partner_aic=str(plan["partner_aic"]), request=parts
        )


__all__ = [
    "DEFAULT_KINDS",
    "KIND_PROFILES",
    "EvidenceInboxHooks",
    "KindProfile",
    "plan_delegation",
    "profile_for",
    "structured_data_items",
    "strip_handles",
]
