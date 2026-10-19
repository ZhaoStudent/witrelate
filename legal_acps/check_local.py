"""本地自检 —— 不联网、不注册、不需要凭据。

这是你拿到平台凭据之前唯一该做的事：把能离线验证的全部验证掉。
跑通这个脚本，说明 Agent 定义、ACS 契约、工具接线都是对的，
之后注册失败就只剩"平台侧"的原因，排查范围一下缩小一半。

用法（在 witrelate 目录下）：
    ./.venv/bin/python -m legal_acps.check_local
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import io
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

from wit import AcpsDirect, AcpsGroup, AipDelegationHooks, RunConfig, WitRuntime

from dotenv_lite import load_env

from .agents import (
    BUILDERS,
    EVIDENCE_LEADER_INSTRUCTIONS,
    EVIDENCE_LEADER_NAME,
    FACT_JUDGE_INSTRUCTIONS,
    LEAD_INTAKE_NAME,
    LEADER_INSTRUCTIONS,
    build_agent,
)
from .contracts import (
    DEBATE_MAX_ROUNDS,
    FALLACY_EVIDENCE_KINDS,
    FALLACY_RHETORIC_KINDS,
    HARM_BAND_TABLE,
    MAX_CHAIN_DEPTH,
    ArbitrationCase,
    EvidenceCase,
    EvidenceModality,
    EvidenceRuling,
    FactRuling,
    FallacyKind,
    band_of,
    fallacy_layer,
)
from .debate import (
    CHAIN_PAYLOAD_KEY,
    DebateChainError,
    DebateChainGuardHooks,
    account_prefix_of,
    inbound_chain,
    plan_next_hop,
    request_with_chain,
    should_terminate,
)
from .acps_config import DEPLOY_ROLES, PORTS, SOFT_RETIRED, build_config, describe
from .evidence.inbox import MAX_STAGE_BYTES
from .evidence_hooks import KIND_PROFILES
from .termination import LeaderTerminationHooks
from .video_modality import VIDEO_CAPABLE_ROLES
from .audio_modality import AUDIO_CAPABLE_ROLES

# 一个不联网的假模型，只为实现编译期接线检查
DUMMY_MODEL = None


#: 每个模态的「取用方上限」住在自己的 fetch 模块里。
#: ⚠️ 图像链最早落地，它的包名是 `evidence`（**不是** `image`）—— 这不是笔误；
#: 音频 / 视频各自的包名与 `kind` 同名。
#: 这张表只是**定位器**，不是事实本身：上限值每次都从模块里现读，并且下面会整表
#: 断言"每个模态都登记了模块"，所以将来加模态时**不会静默跳过**这条不变量。
_FETCH_MODULE_BY_KIND: dict[str, str] = {
    "image": "legal_acps.evidence.fetch",
    "audio": "legal_acps.audio.fetch",
    "video": "legal_acps.video.fetch",
}


class ModalityRow(NamedTuple):
    """一个模态**从枚举现场派生**出来的四处接线。"""

    modality: EvidenceModality
    role: str                    # 角色 id（约定：`evidence-<kind>`）
    agent: object | None         # 建不起来就是 None —— 不抛异常，交给断言去报
    partner_name: str            # Partner **自己注册的名字**（Leader 必须照这个名字找）
    inbox_kinds: tuple[str, ...]  # 本角色收件箱收哪些模态


def _modality_rows() -> list[ModalityRow]:
    """现场派生「模态 → 角色 id / 角色对象 / 下游名字 / 收件箱 kinds」。

    为什么**派生**而不是手抄清单：同一个事实在本项目里散成四处，手抄的每一份
    都是下一个会漂的副本 —— 而且漂的时候**每一处看起来都是对的**：

        `EvidenceModality`（模型看到的中文，也是提示词里分支标签的原料）
        `evidence-<kind>`（角色 id，见 `EvidenceModality.kind` 的 docstring）
        Partner 的 `display_name`（Leader 被交代的下游名字）
        `EvidenceInboxHooks(kinds=…)`（收件箱要收哪些模态）

    ⚠️ 角色建不起来（`BUILDERS` 里没有）时返回 `agent=None` 而不是抛 `KeyError`：
    看板的职责是**把结构问题报出来**，一次 traceback 只告诉人"脚本坏了"，
    不告诉人"少了哪个角色"，而且它会发生在后续检查之前，整份看板只留一行栈。
    """
    rows: list[ModalityRow] = []
    for m in EvidenceModality:
        role = f"evidence-{m.kind}"
        builder = BUILDERS.get(role)
        agent = builder(DUMMY_MODEL) if builder is not None else None
        hooks = {getattr(h, "id", ""): h for h in getattr(agent, "hooks", [])}
        inbox = hooks.get("legal.evidence-inbox")
        rows.append(
            ModalityRow(
                modality=m,
                role=role,
                agent=agent,
                partner_name=str(getattr(agent, "display_name", "") or ""),
                inbox_kinds=tuple(getattr(inbox, "kinds", ()) or ()),
            )
        )
    return rows


def _quote(name: str) -> str:
    """把名字包成「」，只为失败信息好读。"""
    return f"「{name}」"


def _hook_ids_of(agent: object | None) -> set[str]:
    return {getattr(h, "id", "") for h in getattr(agent, "hooks", [])}


def _inbox_kinds_of(agent: object | None) -> tuple[str, ...]:
    for h in getattr(agent, "hooks", []):
        if getattr(h, "id", "") == "legal.evidence-inbox":
            return tuple(getattr(h, "kinds", ()) or ())
    return ()


def _role_agents() -> dict[str, object]:
    """把 10 个角色各建一次（离线假模型）。看板要多处用，只建一遍。"""
    return {role: BUILDERS[role](DUMMY_MODEL) for role in BUILDERS}


def _mib(n: int) -> int:
    """字节 → MiB（向下取整），只为失败 / 通过信息好读 —— 不参与任何判据。"""
    return n // (1024 * 1024)


#: ⑦-5c 用的极小 WAV —— 判据只问「降级有没有发生」，不问这段音频的取证结论。
_AUDIO_PROBE_BYTES = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x40\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)


def _check_audio_degrade_actually_fires(ok: bool) -> bool:
    """真跑一遍**框架的降级入口**，断言 `AudioPart` 真的被换掉。

    ⚠️ 这一条防的是「Hook 挂了、判据全绿、功能却是坏的」——
    2026-10-06 两次真机 422 时，⑦-5b 一直报[通过]。
    只查挂载点的判据**结构上**发现不了"空转"。

    判据用**临时注入的假凭据**构造 provider：要问的是"接线有没有生效"，
    不是"本机有没有 key"（构造 provider 本身不发任何请求，全程离线）。
    """
    import os

    from wit import AudioPart, TextPart
    from wit.kernel.messages import Message
    from wit.providers.base import prepare_messages_for_provider

    from .audio_modality import (
        AUDIO_DENIED_REASON,
        DEGRADE_NOTICE_PREFIX,
        audio_content_transform,
    )
    from .llm import build_model
    from .verdict import stable_reason

    saved = {
        k: os.environ.get(k)
        for k in ("OPENAI_MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL")
    }
    try:
        os.environ["OPENAI_MODEL"] = "check-local-probe"
        os.environ["OPENAI_API_KEY"] = "sk-check-local-not-a-real-key"
        os.environ["OPENAI_BASE_URL"] = "https://invalid.example/v1"
        try:
            provider = build_model()
        except Exception as exc:  # noqa: BLE001
            print(
                f"  [失败] 音频降级：**构造 provider 就失败了**（{stable_reason(exc)}）"
                " ⇒ 这条判据没跑成，**不能算通过**"
            )
            return False

        probe = AudioPart(mime_type="audio/wav", data=_AUDIO_PROBE_BYTES)
        supported = provider.supports_content(probe, role="user")
        if tuple(supported) != (False, AUDIO_DENIED_REASON):
            print(
                "  [失败] 音频降级：provider 仍然**自报支持** audio"
                f"（supports_content 答 {supported!r}）⇒ 框架**不会**应用我们注册的"
                " `ContentTransform` ⇒ `AudioPart` 会一路走到厂商侧（真机 HTTP 422）"
            )
            return False

        msgs = [Message(role="user", content=[TextPart(text="听听这段录音"), probe])]
        out = asyncio.run(
            prepare_messages_for_provider(msgs, provider, [audio_content_transform()])
        )
        parts = [p for m in out for p in m.content]
        if any(isinstance(p, AudioPart) for p in parts):
            print(
                "  [失败] 音频降级：跑完框架的降级入口，`AudioPart` **还在**"
                " ⇒ 降级器是空转的（真机 422 的成因）"
            )
            return False
        text = "".join(p.text for p in parts if isinstance(p, TextPart))
        missing = [
            label
            for label, needle in (
                ("降级说明前缀", DEGRADE_NOTICE_PREFIX),
                ("「模型没有听到」那句", "模型没有听到这段音频"),
            )
            if needle not in text
        ]
        if missing:
            print(
                f"  [失败] 音频降级：`AudioPart` 换掉了，但产物里缺 {'、'.join(missing)}"
                " ⇒ 下游会把代码量出来的数字读成「模型的印象」"
            )
            return False
        print(
            "  [通过] 音频降级**真的会发生**：provider 声明有效能力为「不支持 audio」，"
            f"框架的降级入口把 `AudioPart` 换成了带 `{DEGRADE_NOTICE_PREFIX}` +"
            "「模型没有听到这段音频」的文本"
        )
        return ok
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _check_token_budget(ok: bool) -> bool:
    """⑦-5d **token 预算**：重环节放大 + 被截断时必须留痕（2026-10-06 真机夹出）。

    两条**行为**断言（不看源码里有没有那个词）+ 一条**现场派生**的挂载断言：

      · 留痕必须是**三态**的：`0` 没截断 / `n` 截断过 n 次 / `-1` **无法判定**。
        合并成两态的后果：拿不到 `state.metadata` 时会被读成"没截断" ——
        而那正是本项目最危险的失败形态（**静默给安全结论**）。
      · 放大必须**真写进 `request.settings`**（跑一次 `wrap_model_call` 看结果），
        不是"Hook 挂上了"。
      · 挂载集合按 `HEAVY_ROLES` **现场派生**：加一个重环节忘了挂 ⇒ 立刻报出来；
        反过来，不在名单里的角色被放大 ⇒ 也要报（那是无缘无故摊成本）。
    """
    from .token_budget import (
        HEAVY_MAX_TOKENS,
        HEAVY_ROLES,
        TokenBudgetHooks,
        note_truncation,
        truncated_of,
    )

    class _State:
        def __init__(self) -> None:
            self.metadata: dict = {}

    class _Resp:
        def __init__(self, reason: str | None) -> None:
            self.finish_reason = reason

    class _Req:
        def __init__(self) -> None:
            self.settings: dict = {}

    class _Ctx:
        def __init__(self, state: object) -> None:
            self.state = state

    class _Result:
        """模拟 run 结果：`truncated_of()` 收的是**结果**（它自己去取 `.state`）。

        ⚠️ 第一版这里直接把 `state` 传给了 `truncated_of()` ⇒ 读到 -1 ⇒ 判据报红。
        那是**测试写错了**，不是被测代码错 —— 但判据确实响了（这是好事）。
        """

        def __init__(self, state: object) -> None:
            self.state = state

    # ---- ① 留痕：三态必须分得开 ----
    st = _State()
    if not note_truncation(st, _Resp("length")):
        print(
            "  [失败] token 预算：`finish_reason=length` 时**没留痕** ⇒ 真机上"
            "「空转的一轮」会继续显示成 `completed`（静默给安全结论）"
        )
        return False
    if truncated_of(_Result(st)) != 1:
        print(f"  [失败] token 预算：留痕后读回 {truncated_of(_Result(st))}，应为 1")
        return False
    if note_truncation(st, _Resp("stop")):
        print("  [失败] token 预算：`finish_reason=stop` 也被当成截断 ⇒ 会误报")
        return False
    if truncated_of(object()) != -1:
        print(
            "  [失败] token 预算：拿不到 `state.metadata` 时不是 **-1**"
            " ⇒ 「无法判定」会被读成「没截断」，与真正的干净**长得一模一样**"
        )
        return False
    clean = _State()
    note_truncation(clean, _Resp("stop"))
    if truncated_of(_Result(clean)) != 0:
        print(f"  [失败] token 预算：没截断时读回 {truncated_of(_Result(clean))}，应为 0")
        return False

    # ---- ② 放大：真跑一次 wrap_model_call，看它写进 settings 的是什么 ----
    async def _drive(reason: str) -> tuple[dict, object]:
        req = _Req()
        state = _State()
        ctx = _Ctx(state)

        async def handler(_request: object) -> object:
            return _Resp(reason)

        await TokenBudgetHooks().wrap_model_call(ctx, req, handler)
        return req.settings, state

    settings, _ = asyncio.run(_drive("stop"))
    if settings.get("max_tokens") != HEAVY_MAX_TOKENS:
        print(
            f"  [失败] token 预算：跑完 `wrap_model_call`，请求里的 max_tokens 是"
            f" {settings.get('max_tokens')!r}、应为 {HEAVY_MAX_TOKENS}"
            " ⇒ 重环节仍然只吃默认的 8192 ⇒ 思考会把预算吃光"
        )
        return False
    _, truncated_state = asyncio.run(_drive("length"))
    if truncated_of(_Result(truncated_state)) != 1:
        print(
            "  [失败] token 预算：走完一遍 `wrap_model_call`，截断**没留痕**"
            " ⇒ 真机探针报不了红，空转的一轮照样显示成 `completed`"
        )
        return False

    # ---- ③ 挂载：按 HEAVY_ROLES 现场派生（不许登记表写死 = 静默漏项）----
    agents = _role_agents()
    missing = [
        r for r in HEAVY_ROLES
        if not any(isinstance(h, TokenBudgetHooks)
                   for h in getattr(agents.get(r), "hooks", []))
    ]
    wrong = [
        r for r in HEAVY_ROLES
        if r not in missing
        and not any(getattr(h, "max_tokens", 0) == HEAVY_MAX_TOKENS
                    for h in getattr(agents.get(r), "hooks", [])
                    if isinstance(h, TokenBudgetHooks))
    ]
    if missing or wrong:
        print(
            f"  [失败] token 预算：重环节没挂 / 挂错 —— 漏挂 {missing}、"
            f"预算不对 {wrong}（重环节 = {list(HEAVY_ROLES)}）"
        )
        return False
    # 反向：不在名单里的角色被放大 = 无缘无故摊成本
    over = sorted(
        r for r, a in agents.items()
        if r not in HEAVY_ROLES
        and any(isinstance(h, TokenBudgetHooks) for h in getattr(a, "hooks", []))
    )
    if over:
        print(
            f"  [失败] token 预算：{over} 不在重环节名单里却挂了放大"
            " ⇒ 成本无缘无故摊到轻环节头上"
        )
        return False
    print(
        f"  [通过] token 预算：{len(HEAVY_ROLES)} 个重环节都放大到 {HEAVY_MAX_TOKENS}"
        "，被截断时会留痕（三态：0 / n / -1），轻环节不放大"
    )
    return ok


def _forensics_branches() -> dict[str, str]:
    """从 `LEADER_INSTRUCTIONS` **现场派生**出各「鉴伪分支」段落。

    ⚠️ **不写死**分支名 / 数量：按标题行（`XX鉴伪分支（`）切分 ⇒
    加一个模态就多一段，而判据立刻会要求那一段也带上同一条铁律 ——
    「登记表写死 = 静默漏项」在这件事上已经付出过一次代价（见下）。
    """
    from .agents import LEADER_INSTRUCTIONS

    lines = LEADER_INSTRUCTIONS.splitlines()
    heads = [(i, ln) for i, ln in enumerate(lines) if "鉴伪分支（" in ln]
    out: dict[str, str] = {}
    for n, (i, ln) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        name = ln.split("鉴伪分支")[0].lstrip("- ").strip() or f"#{n}"
        out[name] = "\n".join(lines[i:end])
    return out


def _check_delegation_order(ok: bool) -> bool:
    """⑦-5e 每个鉴伪分支都必须带上「**出最终结论之前**做完」这条顺序铁律。

    2026-10-06 真机夹出的正是这一条：**图像分支有它、音频分支没有** ⇒
    主控第一轮就调了 `legal_issue_verdict`（`legal_issue_verdict` 一旦提交，
    系统就锁定这一步的可调用工具）⇒ 音频委派**再也没机会发生** ⇒
    整条证据链被静默绕过 —— 而那一轮 `status=completed`、台账齐全、无异常。

    ⚠️ 判据落在**每个分支各自的段落**上（现场派生），不是"全文里有没有这个词"：
    全文里有一句 ≠ 每个分支都有。
    """
    branches = _forensics_branches()
    if not branches:
        print(
            "  [失败] 委派顺序：主控提示词里**一个鉴伪分支都没派生出来**"
            " ⇒ 这条判据没跑成，**不能算通过**"
        )
        return False
    missing = sorted(
        name for name, body in branches.items() if "出最终结论之前" not in body
    )
    if missing:
        print(
            f"  [失败] 委派顺序：{missing} 分支缺「**出最终结论之前**做完」这条铁律"
            " ⇒ 主控可能先把结论交了、工具被锁，鉴伪委派就再也没机会发生"
            "（2026-10-06 音频链正是这么被静默绕过的，而 `status` 仍是 `completed`）"
        )
        return False
    print(
        f"  [通过] 委派顺序：{len(branches)} 个鉴伪分支"
        f"（{'、'.join(sorted(branches))}）都带了「出最终结论之前」这条铁律"
    )
    return ok


def _check_inbox_landed(ok: bool) -> bool:
    """⑦-5g 「送到没送到」与「取得到取不到」是**两个问题**，各有一个函数。

    2026-10-06 真机：端到端探针用 `inbox.resolve()` 逐跳查三本收件箱。字节**三跳都在
    盘上**，而探针拿到的是**三本全拒** —— 因为 `resolve()` 带**归属**校验
    （跨轮次 / 跨进程不许互相取用），而下游两跳是**常驻服务**，各自在
    `on_run_start` 里 bind 了自己的 `ctx:<uuid>`；探针那个进程根本不在它们的
    归属名单里（它自己的归属是「不属于任何一次 run」）。
    ⇒ 判据红的是「**我**取不到」，而它要证的是「**没送到**」—— 两句话长得一模一样
    ⇒ 这条判据在「下游是常驻服务」这个形态下**恒红**，与链通不通无关。

    ⇒ 于是有了 `inbox.landed_at()`：只看盘、不问归属。

    ⚠️ 两条必须**分得开**：同一份字节、同一本收件箱、同一个句柄，
    只差「用哪个函数问」⇒ 要求**相反**的结论。它们一旦漂成同一个东西，
    丢掉的那一半恰好是**归属闸门**（2026-10-04 为了溯源专门加的），
    而那样的事故在报告上**看不出任何异常**。
    """
    # ⚠️ `tempfile` 在本文件里是**按需局部导入**的（见 3c 节）：模块级没有它。
    import tempfile

    from .evidence import inbox as _ib

    bad16 = 0  # 本条自己的失败数：只为了让最后那行「通过」**如实**（不拿别人的结果背书）

    with tempfile.TemporaryDirectory() as tmp16:
        root16 = Path(tmp16)
        payload16 = b"\x89PNG\r\n\x1a\n" + b"selftest-landed" * 6
        handle16 = _ib.handle_for(payload16)
        digest16 = _ib.parse_handle(handle16)

        def _landed(owner: str) -> str:
            token = _ib.bind_owner(owner)
            try:
                return _ib.landed_at(handle16, root=root16)
            finally:
                _ib.unbind_owner(token)

        def _resolved(owner: str) -> str:
            token = _ib.bind_owner(owner)
            try:
                _ib.resolve(handle16, root=root16)
                return "ok"
            except _ib.InboxRefused:
                return "refused"
            finally:
                _ib.unbind_owner(token)

        # ⓪ 没落盘 ⇒ 空串（不是异常，也不是「现在」—— 报个时间就等于说「在」）
        if _landed("run:X") != "":
            print("  [失败] 落盘判据：收件箱里根本没有这份字节，`landed_at()` 却给了个时间"
                  " ⇒ 「不在」被写成「在」，端到端判据 A′ 从此恒绿"
                  "（而恒绿的那条恰好是唯一能证明链走通的那条）")
            ok, bad16 = False, bad16 + 1

        token16 = _ib.bind_owner("run:X")
        try:
            _ib.stage(payload16, root=root16, mime_type="image/png")
        finally:
            _ib.unbind_owner(token16)
        at16 = _landed("run:X")
        if not at16:
            print("  [失败] 落盘判据：字节刚落盘，`landed_at()` 却说不在 ⇒ 判据 A′ 恒红，"
                  "而在真机上它看起来正像「链断了」（2026-10-06 那轮就是这么报的）")
            ok, bad16 = False, bad16 + 1

        # ① **分得开**（本条的核心）：同一份字节、同一本收件箱、同一个句柄，
        #    只差「谁在问」⇒ 落盘判据**不**变、取用判据**变**。
        if at16 and _landed("run:Y") != at16:
            print("  [失败] 落盘判据：换一个归属去问，`landed_at()` 的答案变了"
                  " ⇒ 它其实在做归属校验，与 `resolve()` 漂成了同一个东西；"
                  "那样判据 A′ 会在「下游是常驻服务」的形态下恒红，而字节其实在盘上")
            ok, bad16 = False, bad16 + 1
        if at16 and _resolved("run:X") != "ok":
            print("  [失败] 落盘判据：**落盘者自己**反而 `resolve()` 不回来"
                  " ⇒ 这两条被改成了同一个方向，正常链路会被一起挡住")
            ok, bad16 = False, bad16 + 1
        if at16 and _resolved("run:Y") != "refused":
            print("  [失败] 落盘判据：别的归属竟然 `resolve()` 成功了 ⇒ 归属闸门"
                  "（跨轮次 / 跨进程不许互相取用）被这条新函数顺手废掉了："
                  "B 轮落的字节被 C 轮取去当证据，而报告上看不出任何异常")
            ok, bad16 = False, bad16 + 1

        # ② sidecar 里 `sha256` 对不上 ⇒ 必须算「不在」。
        #    少了这一半就退化成「文件名在就算」，而**文件名只是文件名**。
        _ib._meta_path(root16, digest16).write_text(
            json.dumps(
                {
                    "sha256": "0" * 64,
                    "size": len(payload16),
                    "mime_type": "image/png",
                    "name": None,
                    "staged_by": ["run:X"],
                    "staged_at": at16,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
            newline="\n",
        )
        if _landed("run:X") != "":
            print("  [失败] 落盘判据：sidecar 里的 `sha256` 与句柄对不上，`landed_at()` "
                  "却仍说在 ⇒ 退化成「文件名在就算」，把「这份字节确实是这一份」"
                  "的自证丢掉了（判据 A′ 比的是密码学哈希，靠的就是这一条）")
            ok, bad16 = False, bad16 + 1

        # ③ 重新收一次 ⇒ 落盘时间必须**变晚**。少了这一半，端到端那条
        #    「本轮落的」就无从判断：样本字节每轮都一样（确定性合成），
        #    光看「在不在」分不出是这一轮落的还是上一轮留下的。
        token16b = _ib.bind_owner("run:Z")
        try:
            _ib.stage(payload16, root=root16, mime_type="image/png")
        finally:
            _ib.unbind_owner(token16b)
        at16b = _landed("run:Z")
        if not (at16b and at16 and at16b > at16):
            print(f"  [失败] 落盘判据：重复收一次同样的字节，落盘时间没变晚"
                  f"（{at16} → {at16b}）⇒ 端到端判据 A′ 无法区分"
                  "「这一轮送到了」与「上一轮留下的还在」")
            ok, bad16 = False, bad16 + 1

    # ④ 端到端那条「**本轮**落的」也要有判据 —— 上面三条证的是 `landed_at()` 这个
    #    **工具**对，而探针要证的是「**这一轮**送到了」。两者之间差一个**前后对比**，
    #    而对比逻辑在探针里（`landed_snapshot` + `landed_inboxes`）。
    #    它方向写反（或干脆不做对比）时，`landed_at()` 三条全绿，判据 A′ 照样恒绿：
    #    本脚本的样本是**确定性合成**的，字节每轮都一样 ⇒ 昨天那一轮留下的文件
    #    会替今天这次失败**背书**（与判据 C 不许借用历史产物是同一个道理）。
    #    ⚠️ 所以这里驱动的是**探针自己的那两个函数**，不是重写一遍同样的比较。
    from run import e2e_audio_live as _probe

    saved_box_of = _probe._box_of
    try:
        with tempfile.TemporaryDirectory() as tmp17:
            boxes17 = {r: Path(tmp17) / r / "evidence-inbox" for r in _probe.HOPS}
            _probe._box_of = lambda role: boxes17[role]
            payload17 = b"selftest-hop-bytes" * 4
            handle17 = _ib.handle_for(payload17)

            # ⓪ 一条都没落 ⇒ 全 False（且每条都要**说清为什么**）
            snap17 = _probe.landed_snapshot(handle17)
            got17 = _probe.landed_inboxes(handle17, snap17)
            if any(o for _, o, _ in got17):
                print("  [失败] 落盘判据：一份字节都没送出去，逐跳却报了「落盘」"
                      " ⇒ 判据 A′ 恒绿，链断在哪一跳都看不出来")
                ok, bad16 = False, bad16 + 1
            if any(not w for _, _, w in got17):
                print("  [失败] 落盘判据：不通过的那几跳**没说原因** ⇒ 判据只报红不说话，"
                      "人只能退回去猜（而猜错方向的代价是一整轮真机，约 6 分钟）")
                ok, bad16 = False, bad16 + 1

            # ① 只有**第一跳**落了 ⇒ 必须是 (True, False, False)。
            #    少了这一半，「链断在第二跳 / 第三跳」会被判成走通 ——
            #    而「下游其实没收到、回一句『没有音频可听』」正是这条链最常见的坏法。
            first17 = _probe.HOPS[0]
            _ib.stage(payload17, root=boxes17[first17], mime_type="audio/wav")
            got17a = _probe.landed_inboxes(handle17, snap17)
            want17a = [(r, r == first17) for r in _probe.HOPS]
            if [(r, o) for r, o, _ in got17a] != want17a:
                print(f"  [失败] 落盘判据：只有 {first17} 落了盘，逐跳结论却是"
                      f" {[(r, o) for r, o, _ in got17a]}（应为 {want17a}）"
                      " ⇒ 链断在第二跳照样判成走通")
                ok, bad16 = False, bad16 + 1

            # ② 三跳都落（相对**落之前**的快照）⇒ 全 True。少了这一半，
            #    "恒红"的实现也能过（那就等于这条判据永远说链没通）。
            for r in _probe.HOPS[1:]:
                _ib.stage(payload17, root=boxes17[r], mime_type="audio/wav")
            if not all(o for _, o, _ in _probe.landed_inboxes(handle17, snap17)):
                print("  [失败] 落盘判据：三跳**都**落了盘（且都是本轮落的），"
                      "逐跳却仍报没落 ⇒ 判据 A′ 恒红，通着的链会被判成断了")
                ok, bad16 = False, bad16 + 1

            # ③ **落完之后**再取快照 ⇒ 全 False：字节在，但它是**上一轮**留下的。
            #    ⚠️ 这是「本轮落的」这条闸门的**核心**：少了它，昨天那份文件
            #       会替今天这次失败背书，而报告上一切正常。
            snap17b = _probe.landed_snapshot(handle17)
            got17b = _probe.landed_inboxes(handle17, snap17b)
            if any(o for _, o, _ in got17b):
                print("  [失败] 落盘判据：字节是**上一轮**留下的（落盘时间没变），"
                      "却判成了本轮送到 ⇒ 上一轮的文件替这一轮的失败背书，"
                      "而样本字节每轮都一样（确定性合成），光看「在不在」分不出来")
                ok, bad16 = False, bad16 + 1
    finally:
        _probe._box_of = saved_box_of

    if not bad16:
        print(
            "  [通过] 落盘判据：`landed_at()` 只看盘、`resolve()` 看归属，"
            "同一份字节给出相反结论（两者分得开）；重复收会刷新落盘时间；"
            "探针能区分「本轮落的」与「上一轮留下的」"
        )
    return ok


def _check_fact_store(ok: bool) -> bool:
    """⑦-5h 事实库：**闸门要分得开**，且溯源字段不许含糊。

    为什么必须有这一条（两条理由，都是"没人核 = 核过了"的形态）：

    1. **检索质量是三版 ad hoc 打分全废之后才定下来的**（BM25）。
       前三版（IDF top-K / 全量覆盖度 / max 归一化）**每一版都能跑通、
       都返回非空、看起来都正常**，但相关与不相关的分数区间**完全重叠**。
       ⇒ 只断言"能检索出东西"是**空转的判据**，必须断言**两组给不同结论**。
    2. **事实是要被写进法律结论的**。一条没有 `url` / 原始发布方 / 日期的
       事实进了结论，就是**假证据** —— 而"字段没解析出来"与"本来就没有"
       在产物里长得一模一样。

    闸门设计（只差一个变量 ⇒ 必须给不同结论）：
      · 与库内容相关的查询 ⇒ 必须返回**非空**
      · 完全不相关的查询   ⇒ 必须返回**空**（阈值真的在拦，不是摆设）
    """
    import tempfile

    from legal_acps import fact_store as fs

    print("⑦-5h 事实库（闸门分得开 + 溯源字段齐全）")

    store, rep = fs.FactStore.load()
    if not rep.ok:
        # ⚠️ 库不存在时**跳过 ≠ 通过**：没有库就没法证闸门，必须如实报出来。
        #    （`tools.fetch_facts` 是离线手动跑的，开发机上可能没跑过）
        print(f"  [跳过·不计通过] 事实库为空或不可读：{rep.reasons or '无 raw-*.jsonl'}"
              f" ⇒ 闸门**没有被验证**。先跑 `python -m tools.fetch_facts`")
        return ok

    bad = 0

    # ── (1) 溯源字段：每条都必须带 url / 原始发布方 / 日期 ──────────────
    missing = [(f.title[:24], f.url, f.origin_publisher, f.published)
               for f in store
               if not (f.url and f.origin_publisher and f.published)]
    if missing:
        print(f"  [失败] 事实库溯源字段缺失 {len(missing)} 条（例：{missing[0]}）"
              f" ⇒ 没有来源的事实一旦进结论就是**假证据**")
        ok, bad = False, bad + 1

    # ── (2) 闸门分得开：相关非空 / 不相关为空（**只差一个变量**）─────────
    #        这两个查询都是**长文本事件描述**，唯一差别是内容相不相关。
    related = ("家族群里转发了一张聊天截图，说某院士预测广东将发生 8 级以上大地震。"
               "截图里有院士的名字和他的头像。")
    unrelated = ("火星上发现了一座古代城市遗址，考古学家确认是外星文明留下的建筑。")
    # 🔴 第三条探针（2026-10-07 加，纯转述零实体）：整句没有一个实体词，
    #    全是「网上/说法/转发」这类**通用转述语** ⇒ 库里没有任何条目与它
    #    讲同一件事，必须返回空。
    #    为什么非有不可：`text_match` 的两道折扣（虚字 0.25 / df=1 0.4）
    #    的可观测伤害**恰好落在这类查询上**——折扣一旦被丢（如
    #    `for g, _ in qg` 又绕回 `self._idf[g]`），这条查询的 top1 会从
    #    9.58 涨到 21.60（实测）⇒ 过阈值 ⇒ 本断言响。旧的两条查询对
    #    该病灶不敏感（matched 词在 128 条库里 df≥2 且不含虚字，实测
    #    退回前后分数**一模一样**）——「判据的输入集合必须覆盖病灶的
    #    载体」，否则空转的从来不是被测代码，是判据。
    hearsay = ("网上的说法是真的吗？很多人都说是这样，大家都在转发。")

    got_rel = store.search(related)
    got_unrel = store.search(unrelated)
    got_hearsay = store.search(hearsay)
    if not got_rel:
        print("  [失败] 事实库闸门：相关查询**返回空** ⇒ 该召回的没召回，"
              "检索等于没用")
        ok, bad = False, bad + 1
    if got_unrel:
        print(f"  [失败] 事实库闸门：不相关查询返回了 {len(got_unrel)} 条"
              f"（最高分 {max(h.score for h in got_unrel):.2f}）⇒ 阈值没在拦，"
              f"等于把噪声当权威依据喂给模型")
        ok, bad = False, bad + 1
    if got_hearsay:
        print(f"  [失败] 事实库闸门：纯转述零实体查询返回了 {len(got_hearsay)} 条"
              f"（最高分 {max(h.score for h in got_hearsay):.2f}）⇒ 靠虚字/"
              f"df=1 折扣压住的通用转述语**没有被压住**（打分折扣失效？），"
              f"等于把套话当证据")
        ok, bad = False, bad + 1

    # ── (3) 分数必须真的分得开（不相关的最高分 < 相关的最高分）──────────
    if got_rel:
        best_rel = max(h.score for h in got_rel)
        raw_unrel = store.search(unrelated, min_score=0.0) \
            + store.search(hearsay, min_score=0.0)
        best_unrel = max((h.score for h in raw_unrel), default=0.0)
        if best_unrel >= best_rel:
            print(f"  [失败] 事实库分数没分开：相关最高 {best_rel:.2f} "
                  f"≤ 不相关最高 {best_unrel:.2f} ⇒ 阈值取在中间也救不了，"
                  f"是打分函数本身失效（重跑 `run/_probe_fact_store.py` 看分布）")
            ok, bad = False, bad + 1

    # ── (4) 空库 / 空查询不许崩，也不许凭空返回东西 ────────────────────
    empty = fs.FactStore(())
    if empty.search(related) or empty.search(""):
        print("  [失败] 事实库：空库或空查询返回了非空 ⇒ 凭空造事实")
        ok, bad = False, bad + 1

    # ── (5) 加载报告必须如实报丢弃数（"库很干净" vs "偷偷丢了一半"）─────
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "raw-bad.jsonl"
        # 一条缺 url（无法溯源）+ 一条坏 JSON + 一条正常
        p.write_text(
            json.dumps({"title": "没有链接的事实"}) + "\n"
            + "{这不是 JSON}\n"
            + json.dumps({"title": "正常事实", "url": "https://x.example/a",
                          "origin_publisher": "某发布方", "published": "2026-10-07",
                          "level": "权威结论", "body": "正文"})
            + "\n", encoding="utf-8")
        _s, r = fs.FactStore.load(Path(td))
        if r.total != 3 or r.kept != 1 or r.dropped != 2:
            print(f"  [失败] 加载报告没如实记账：total={r.total} kept={r.kept} "
                  f"dropped={r.dropped}（期望 3/1/2）⇒ 「丢了多少」说不清，"
                  f"「库很干净」与「偷偷丢了一半」就长得一样")
            ok, bad = False, bad + 1
        if any(f.title == "没有链接的事实" for f in _s):
            print("  [失败] 缺 `url` 的原料**进了库** ⇒ 无法溯源的事实成了可用事实")
            ok, bad = False, bad + 1

    # ── (6) 同 URL 只留一条（重抓快照不许整文翻倍，2026-10-07）─────────
    #        为什么非有不可：重跑 `--curated` / 列表页重抓都会**整批**重写
    #        同一批 URL。`load` 不去重 ⇒ 同一篇文章两份：检索 top-N 被翻倍
    #        条目挤占（挤掉的是**别的**相关证据）、库容统计虚高，而
    #        「重抓过」与「库里真有两条」在调用方看来一模一样。
    url_n: dict[str, int] = {}
    for f in store:
        url_n[f.url] = url_n.get(f.url, 0) + 1
    dup_urls = [u for u, n in url_n.items() if n > 1]
    if dup_urls:
        print(f"  [失败] 事实库同 URL 重复 {len(dup_urls)} 个"
              f"（例：{dup_urls[0][:60]}）⇒ 同一篇文章存了两份，"
              f"检索 top-N 被翻倍条目挤占、库容虚高（load 去重失效？）")
        ok, bad = False, bad + 1

    #        行为断言：两个快照同 URL ⇒ 只留 fetched_at 新的那份，且计数报出
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "raw-old.jsonl").write_text(json.dumps({
            "title": "旧快照", "url": "https://x.example/dup",
            "origin_publisher": "某发布方", "published": "2026-10-01",
            "level": "权威结论", "body": "旧正文", "fetched_at": "2026-10-07T01:00:00Z",
        }) + "\n", encoding="utf-8")
        (d / "raw-new.jsonl").write_text(json.dumps({
            "title": "新快照", "url": "https://x.example/dup",
            "origin_publisher": "某发布方", "published": "2026-10-01",
            "level": "权威结论", "body": "新正文", "fetched_at": "2026-10-07T02:00:00Z",
        }) + "\n", encoding="utf-8")
        s2, r2 = fs.FactStore.load(d)
        if r2.total != 2 or r2.kept != 1 or r2.deduped != 1 or len(s2) != 1:
            print(f"  [失败] 事实库去重没如实记账：total={r2.total} kept={r2.kept} "
                  f"deduped={r2.deduped}（期望 2/1/1）⇒ 「去了多少重」说不清，"
                  f"「重抓过」与「库里真有两条」就长得一样")
            ok, bad = False, bad + 1
        elif not any(f.title == "新快照" and f.body == "新正文" for f in s2):
            print("  [失败] 事实库去重留错了快照：保住了 fetched_at **早**的那份"
                  " ⇒ 新快照该取代旧快照（内容被重抓就是要更新）")
            ok, bad = False, bad + 1

    # ── (7) 受理工具必须真的查案底（装了 ≠ 会响，2026-10-07）────────────
    #        事实库此前**没有任何生产链路消费它**：判据全绿、检索命中 84 分，
    #        真机裁判照样判「无法判定」，理由明写「不存在官方通报」。
    #        接线落在 arbitrate_claim 的返回值上（不改 capability 面、不动指纹），
    #        这条断言保证接线**不断**：命中要带溯源字段；检索失败不许挡受理。
    from legal_acps import agents as _ag
    from legal_acps.contracts import FactSheet as _FactSheet
    _case = _ag.ArbitrationCase(
        event="某矿泉水厂今年发生两起员工死亡事件，含尸体的水被百姓饮用",
        proposition="某矿泉水厂水源含有尸体，该水已被百姓饮用",
        fact_sheet=_FactSheet(event="矿泉水厂传闻"),
        pro_position="传闻属实", con_position="传闻不实",
    )
    got = _ag.arbitrate_claim(_case)
    mats = got.get("official_materials") or []
    if not mats:
        print("  [失败] 受理没带回案底：arbitrate_claim 未检索事实库"
              f"（error={got.get('official_materials_error') or '无'}）⇒ "
              "官方通报在库里、裁判却看不见，弃权病因原样保留（接线断了？）")
        ok, bad = False, bad + 1
    elif not all(m.get("url") and m.get("origin_publisher") and m.get("published")
                 for m in mats):
        print("  [失败] 案底材料溯源字段缺失（url/原始发布方/日期）"
              " ⇒ 无法被裁判引用的官方通报等于没有")
        ok, bad = False, bad + 1

    #        旁路纪律：检索失败不许挡受理主流程（收案比查库重要）
    import legal_acps.fact_store as _fs_mod
    _orig_cls = _fs_mod.FactStore

    class _BoomStore:
        @classmethod
        def load(cls, *a, **k):
            raise RuntimeError("模拟检索失败")

    _fs_mod.FactStore = _BoomStore      # 运行时取：局部 from-import 会拿到假类
    try:
        got2 = _ag.arbitrate_claim(_case)
        if got2.get("event") != _case.event:
            print("  [失败] 检索失败挡了受理主流程 ⇒ 旁路记录顶掉了主流程"
                  "（查库失败让收案也失败，方向反了）")
            ok, bad = False, bad + 1
        elif not got2.get("official_materials_error"):
            print("  [失败] 检索失败没如实记进 official_materials_error"
                  " ⇒ 「没查到」与「查了但坏了」在裁判看来一模一样")
            ok, bad = False, bad + 1
    finally:
        _fs_mod.FactStore = _orig_cls

    if not bad:
        n = len(store)
        dedup_note = f"、去重 {rep.deduped} 条" if rep.deduped else ""
        print(f"  [通过] 事实库 {n} 条（含快照去重{dedup_note}）：相关查询召回 "
              f"{len(got_rel)} 条、不相关与纯转述查询返回空（闸门分得开）；"
              f"溯源字段齐全；加载如实记账丢弃/去重数；同 URL 不重复；"
              f"受理接线带回案底 {len(mats)} 条；空库/空查询不造事实")

    # ── (8) 媒体核验记录（方案②，2026-10-18）：证据链结论 → 可检索的一手材料 ──
    #        断言链：渲染形态 → FALSE_PAT 安全闸（负对照）→ 真库副本+长查询
    #        命中过阈值（生产保真度）→ 快车道双断言（正对照防空转）→
    #        同指纹去重 → issue_evidence_report 真接线（注入式）→ ① 提示词接线。
    import shutil as _shutil

    from legal_acps.contracts import EvidenceRuling as _EvRuling
    from legal_acps.evidence.contracts import Finding as _Finding
    from legal_acps.fast_lane import official_confirms as _official_confirms

    ev_ruling: dict = {
        "subject": "网传某商场促销现场踩踏的配图（用户主张原文：商场为压消息删了视频）",
        "modality": "图像",
        "suspicion": "高",
        "sha256": "e" * 64,
        "conclusion": "多项线索提示该图经过复制-移动编辑与重压缩，建议线下复检。",
        "findings": [
            {"check": "copy_move", "detail": "检出复制-移动区域 Δ=(312,0)",
             "caveat": "该线索不能证明整图为合成"},
        ],
        "not_run": ["EXIF 已被剥离"],
    }

    # (8a) 正常形态：可渲染、level=一手材料、url 指纹溯源键、溯源字段齐全
    rec, refused = fs.render_evidence_record(ev_ruling)
    if rec is None or refused:
        print(f"  [失败] 核验记录渲染被拒（refused={refused}）⇒ 合法线索记录进不了库")
        ok, bad = False, bad + 1
    elif not (rec["url"] == f"{fs.EVIDENCE_URL_PREFIX}sha256/{'e' * 64}"
              and rec["level"] == "一手材料"
              and rec["origin_publisher"] and rec["published"]):
        print(f"  [失败] 核验记录形态不对：level={rec['level']!r} url={rec['url'][:40]!r}"
              " ⇒ 等不到裁判按 level 分流就先被当成了无法溯源的东西")
        ok, bad = False, bad + 1

    # (8b) FALSE_PAT 安全闸（负对照）：conclusion 混入定性词 ⇒ 拒绝入库并报出模式
    poisoned = dict(ev_ruling)
    poisoned["conclusion"] = ev_ruling["conclusion"] + "经核查该图系编造的谣言。"
    rec2, refused2 = fs.render_evidence_record(poisoned)
    if rec2 is not None or not refused2:
        print("  [失败] 核验记录安全闸没拦：带定性词的正文照常入库"
              " ⇒ 之后同事件文本研判会被快车道直接判「不实」、跳过辩论")
        ok, bad = False, bad + 1

    # (8c) 生产保真度：真库副本 + 长查询（受理检索的真实形态）⇒ 命中且过阈值。
    #      ⚠️ 必须用真库副本：temp 小库 df=1 折扣会把分数打崩（实测 2.29），
    #         而「落库后能不能被生产检索捞到」恰恰取决于真库的 df 结构。
    if rec is not None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for p in fs.FACTS_DIR.glob("raw-*.jsonl"):
                _shutil.copy(p, d / p.name)
            fs.append_evidence_record(rec, root=d)
            store_ev, rep_ev = fs.FactStore.load(d)
            q_long = ("家族群里都在传一张现场照片，说某商场促销活动现场发生踩踏，"
                      "有人被踩伤送医，商场为了压消息把现场视频都删了。"
                      "这张照片是不是真的？这件事是真的吗？")
            hits_ev = store_ev.search(q_long)
            ev_hit = next((h for h in hits_ev
                           if h.fact.url.startswith(fs.EVIDENCE_URL_PREFIX)), None)
            if ev_hit is None:
                print(f"  [失败] 核验记录落库后检索不到（真库副本 top{len(hits_ev)} 里没有）"
                      " ⇒ 方案②的复利通道空转：记录在库里、裁判永远看不见")
                ok, bad = False, bad + 1
            elif ev_hit.score < fs.MIN_SCORE:
                print(f"  [失败] 核验记录命中分数 {ev_hit.score:.2f} < 阈值 {fs.MIN_SCORE}"
                      " ⇒ 过不了 search 的门，等于没检索到")
                ok, bad = False, bad + 1

    # (8d) 快车道双断言：正对照（毒正文 inline 事件词 ⇒ 闸门能响，防空转）
    #      + 真记录正文 ⇒ 闸门不响（结构上不可能触发「不实」快裁）。
    _ev_event = "网传某商场促销现场踩踏"
    poison_inline = "关于某商场促销活动现场踩踏的传闻，经核查系编造的谣言。"
    if not _official_confirms(_ev_event, poison_inline):
        print("  [失败] 快车道对齐层的正对照没响：毒正文（事件词+定性词同窗）"
              "都不触发 ⇒ (8d) 的负断言在空转，安全闸根本没被验证")
        ok, bad = False, bad + 1
    if rec is not None and _official_confirms(_ev_event, rec["body"]):
        print("  [失败] 核验记录正文竟能触发快车道对齐 ⇒ 「线索」获得了"
              "「权威定性」的判定力，快车道会被一条技术线索劫持")
        ok, bad = False, bad + 1

    # (8e) 同指纹去重：同一媒体复检 ⇒ load 只留最新一份（纪律 4 的行为版）
    if rec is not None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fs.append_evidence_record(rec, root=d)
            rec_newer = dict(rec)
            rec_newer["body"] = rec["body"] + "（复检更新：线索等级不变。）"
            rec_newer["fetched_at"] = "99991231T235959Z"
            fs.append_evidence_record(rec_newer, root=d)
            s_dup, r_dup = fs.FactStore.load(d)
            evs = [f for f in s_dup if f.url.startswith(fs.EVIDENCE_URL_PREFIX)]
            if len(evs) != 1 or r_dup.deduped != 1:
                print(f"  [失败] 核验记录同指纹去重失效：{len(evs)} 条（deduped="
                      f"{r_dup.deduped}）⇒ 同一媒体复检一次库里翻一倍，"
                      "检索 top-N 被自家人挤占")
                ok, bad = False, bad + 1
            elif "复检更新" not in evs[0].body:
                print("  [失败] 核验记录去重留错了快照：保住了旧的那份"
                      " ⇒ 复检结论永远更新不进去")
                ok, bad = False, bad + 1

    # (8f) issue_evidence_report 真接线（注入式）：报告落盘 + 记录落库同轮发生；
    #      毒 conclusion ⇒ 记录 refused 但报告照常交付（旁路纪律）。
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _orig_out, _orig_facts = _ag.OUTPUT_DIR, fs.FACTS_DIR
        _ag.OUTPUT_DIR, fs.FACTS_DIR = d, d
        try:
            good_ruling = _EvRuling(
                subject=ev_ruling["subject"], modality="图像", suspicion="高",
                sha256="f" * 64,
                conclusion=ev_ruling["conclusion"],
                reasoning="两项线索（复制-移动区域、压缩历史）共同指向等级「高」；"
                          "EXIF 一项未跑，已在 not_run 点名。",
                findings=[_Finding(check="copy_move", verdict="提示异常",
                                   detail="检出复制-移动区域",
                                   caveat="该线索不能证明整图为合成")],
                not_run=["EXIF 已被剥离"],
            )
            got_rep = _ag.issue_evidence_report(good_ruling)
            live = d / fs.EVIDENCE_LIVE_FILE
            fr = got_rep.get("fact_record") or {}
            if fr.get("status") != "written" or not live.exists():
                print(f"  [失败] 鉴证报告落库接线断了：fact_record={fr}"
                      f"、库文件存在={live.exists()} ⇒ 报告照写、复利通道没接上"
                      "（「装了 ≠ 会响」的接线版）")
                ok, bad = False, bad + 1
            bad_ruling = good_ruling.model_copy(
                update={"conclusion": "经核查该图系编造的谣言。"})
            got_rep2 = _ag.issue_evidence_report(bad_ruling)
            fr2 = got_rep2.get("fact_record") or {}
            if fr2.get("status") != "refused" or "saved_to" not in got_rep2:
                print(f"  [失败] 毒报告的双路走反了：fact_record={fr2}、"
                      f"报告交付={'saved_to' in got_rep2} ⇒ 要么闸没拦、"
                      "要么闸拦了把主交付也拦了（旁路纪律被违反）")
                ok, bad = False, bad + 1
        finally:
            _ag.OUTPUT_DIR, fs.FACTS_DIR = _orig_out, _orig_facts

    # (8g) ① 提示词接线（artifact 级）：组合研判分支上了主控提示词、
    #      evidence:// 分流上了裁判提示词 —— 真机行为由 run/e2e_combined 验证。
    _leader_ins = BUILDERS["leader"](DUMMY_MODEL).instructions
    _judge_ins = BUILDERS["fact-judge"](DUMMY_MODEL).instructions
    if "组合研判分支" not in _leader_ins or "必须并发派出" not in _leader_ins:
        print("  [失败] 主控提示词缺组合研判分支（或丢了并发派出约束）"
              " ⇒ 媒体+主张的输入会只走单边，①的缝合没接上")
        ok, bad = False, bad + 1
    if "evidence://" not in _judge_ins or "一手材料" not in _judge_ins:
        print("  [失败] 裁判提示词缺 level 分流 ⇒ 检索到的核验记录会被当成"
              "官方通报引用（线索被当成定性）")
        ok, bad = False, bad + 1

    if not bad:
        print("  [通过] 媒体核验记录（方案②）：渲染形态/安全闸（负对照拦得住）/"
              "真库副本长查询命中过阈值/快车道双断言（正对照响+记录不响）/"
              "同指纹去重留最新/报告-落库同轮接线（毒报告只拒库不拒交付）/"
              "①②提示词接线齐全")
    return ok


def _check_fetch_facts(ok: bool) -> bool:
    """⑦-5i 离线抓取器：**本项目唯一的出网面必须有闸门，且闸门要在发请求之前响**。

    ⚠️ 这是**安全判据**，不是功能判据。为什么非有不可：

      · `tools/fetch_facts.py` 是本项目**唯一**主动对外发 HTTP 的代码。
        出网面是本项目从未有过、且刻意不要的东西（SSRF 底线）——
        既然破例开了一个，就必须证明**它只朝白名单开**。
      · 🔴 「抛了异常」与「闸门拦了」**长得一模一样**：网络不通、对方 403、
        DNS 挂了，都会抛。⇒ 光断言"会抛"是空转的，必须断言
        **请求根本没发出去**（换掉 `urlopen` 做探针，被调用过 = 闸门是假的）。
      · 🔴 白名单是**唯一的边界**：里面一旦出现 `localhost` / 内网段 / 通配符，
        这个出网面就对内网敞开了，而**抓列表页的功能照样跑得好好的**，
        没有任何东西会变红 ⇒ 只能靠这条判据拦。

    其余几条是「解析不许伪造」：`published` 推不出就留空（不许拿抓取时间
    冒充发布时间）、`origin_publisher` 抽不出就留空（不许拿转载平台
    冒充原始发布方）、正文引号要**去干净**（后引号在**中间**，strip 管不到）。
    """
    import urllib.parse
    import urllib.request

    try:
        import tools.fetch_facts as ff
    except ImportError as exc:
        print(f"  [失败] 抓取器不可导入 ⇒ 出网面无从验证：{exc}")
        return False

    print("⑦-5i 离线抓取器（出网闸门 + 解析不许伪造）")
    bad = 0

    # ── (1) 闸门必须在**发起请求之前**拦住（不是靠网络层抛异常）─────────
    sent: list[str] = []
    real_urlopen = urllib.request.urlopen

    def _spy(req, *_a, **_kw):
        sent.append(getattr(req, "full_url", None) or str(req))
        raise AssertionError("闸门失效：请求真的发出去了")

    urllib.request.urlopen = _spy
    try:
        for probe, why in (
            ("http://www.piyao.org.cn/", "非 https"),
            ("https://evil.example.com/x", "白名单外域名"),
            ("https://127.0.0.1:8080/admin", "本机地址"),
        ):
            sent.clear()
            blocked = False
            reason = ""
            try:
                ff._get(probe)
                reason = "正常返回了内容（根本没拦）"
            except ValueError:
                blocked = True
            except Exception as exc:  # noqa: BLE001 - 要的就是"不是 ValueError"
                reason = f"抛的是 {type(exc).__name__} 而不是 ValueError"
            if sent:
                # 🔴 这是最关键的一条：**发出去了**才报错，那不是闸门，是网络层异常
                reason = f"请求已经发到网络（{sent[0]}）才报错 —— 那不是闸门"
            if reason:
                print(f"  [失败] 出网闸门没拦住 {probe}（{why}）：{reason}")
                ok, bad = False, bad + 1
            elif not blocked:  # pragma: no cover - 逻辑上到不了，兜底防静默放行
                print(f"  [失败] 出网闸门对 {probe}（{why}）既没拦也没报错")
                ok, bad = False, bad + 1
    finally:
        urllib.request.urlopen = real_urlopen

    # ── (2) 白名单本身不许对内网/本机/通配敞开（**不写死处数**）──────────
    #        ⚠️ 空集合必须显式判失败：`all([])` 是 True，白名单空了 ⇒ 抓取器
    #        永远抓不到东西，而"零条"与"对方站点改版"在产物里长得一模一样。
    if not ff.ALLOWED_HOSTS:
        print("  [失败] 出网白名单是空的 ⇒ 抓取器永远返回 0 条，"
              "而「零条」与「对方站点改版」长得一样")
        ok, bad = False, bad + 1
    risky = [h for h in ff.ALLOWED_HOSTS
             if "*" in h or re.match(
                 r"(localhost|127\.|0\.0\.0\.0|::1|10\.|192\.168\.|169\.254\."
                 r"|172\.(1[6-9]|2\d|3[01])\.)", h, re.I)]
    if risky:
        print(f"  [失败] 白名单里有本机/内网/通配主机 {risky} ⇒ 唯一出网面"
              f"对内网敞开，而抓列表页的功能**照样正常**，没有别的判据会红")
        ok, bad = False, bad + 1

    # ── (3) 日期解析：**推不出就留空，不许编** ──────────────────────────
    for href, want in (
        ("20260930/abc/c.html", "2026-09-30"),     # 列表页里是**相对路径**
        ("/20260930/abc/c.html", "2026-09-30"),    # 也可能带前导斜杠
        ("2026/09/30/abc.html", ""),               # 不是 8 位连写 ⇒ 不许瞎猜
        ("20261332/abc/c.html", ""),               # 非法日期 ⇒ 留空而不是编
        ("pypt_gywm/index.html", ""),              # 栏目页 ⇒ 空
    ):
        got = ff._date_from_path(href)
        if got != want:
            print(f"  [失败] 日期解析 {href!r} → {got!r}（期望 {want!r}）"
                  f" ⇒ 「没解析出来」与「本来没有日期」在产物里长得一模一样")
            ok, bad = False, bad + 1

    # ── (4) 原始发布方：引号要**去干净**，抽不出就留空（不许拿转载方冒充）──
    origin, date = ff._parse_meta("来源： “网信中国”微信公众号 时间： 2026-09-02")
    if re.search(r"[“”„‟'\"《》]", origin) or "网信中国" not in origin:
        print(f"  [失败] 原始发布方没解析干净：{origin!r} ⇒ 后引号在**中间**，"
              f"`strip()` 只管首尾，会留下 `网信中国”微信公众号` 这种半吊子名字")
        ok, bad = False, bad + 1
    if date != "2026-09-02":
        print(f"  [失败] 正文标注的日期没抽到：{date!r}（期望 '2026-09-02'）"
              f" ⇒ 会退回 URL 路径日期，而实测两者**差一天**")
        ok, bad = False, bad + 1
    if ff._parse_meta("") != ("", ""):
        print("  [失败] 抽不到原始发布方/日期时**没有留空** ⇒ 拿抓取时间或转载平台"
              "冒充，属伪造溯源信息")
        ok, bad = False, bad + 1

    # ── (5) 已落盘原料：外链不许混进来 + 日期要么合法要么空 ──────────────
    facts_dir = Path(__file__).resolve().parent / "facts"
    raws = sorted(facts_dir.glob("raw-*.jsonl")) if facts_dir.is_dir() else []
    n = 0
    n_evidence = 0  # 跳过白名单检查的本系统核验记录数（跳过必须可见）
    if not raws:
        print("  [跳过·不计通过] 没有 `legal_acps/facts/raw-*.jsonl` ⇒ "
              "「外链没混进来」**没有被验证**（先跑 `python -m tools.fetch_facts`）")
    else:
        offsite: list[str] = []
        baddate: list[str] = []
        # 方案②核验记录（url=`evidence://…`，单一来源在 fact_store）：它们是
        # **本系统产出**而非网页抓取原料，溯源纪律由事实库节 (8) 的判据管辖
        # （渲染形态 / 安全闸 / 去重 / 接线）。这里跳过白名单检查，但**单独
        # 计数报出**——「跳过了几条」不进任何输出 = 静默关掉判据的输入集合
        # （10-18 s1 实证：真机落了一条核验记录后本节立刻红，而本地从没跑过
        # 真机探针 ⇒ 本地永远看不见这个红——判据平台相关的活例）。
        from legal_acps.fact_store import EVIDENCE_URL_PREFIX
        n_evidence = 0
        for p in raws:
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                n += 1
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if str(rec.get("url") or "").startswith(EVIDENCE_URL_PREFIX):
                    n_evidence += 1
                    continue
                host = urllib.parse.urlsplit(rec.get("url", "")).hostname or ""
                if host not in ff.ALLOWED_HOSTS:
                    offsite.append(f"{rec.get('title', '')[:20]}@{host or '空'}")
                pub = rec.get("published", "")
                if pub and not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", pub):
                    baddate.append(f"{rec.get('title', '')[:20]}:{pub}")
        if offsite:
            print(f"  [失败] 原料里有 {len(offsite)} 条**不在白名单域**（例："
                  f"{offsite[0]}）⇒ 页眉页脚的外链混进来了，它们抓不到正文、"
                  f"日期与发布方全空，进了库就是**没有来源的事实**")
            ok, bad = False, bad + 1
        if baddate:
            print(f"  [失败] 原料里有 {len(baddate)} 条日期形态不对（例："
                  f"{baddate[0]}）")
            ok, bad = False, bad + 1
        if not n:
            print(f"  [失败] {len(raws)} 个原料文件里**一条记录都没有** ⇒ "
                  f"抓取器静默产出空库，而「零条」与「站点改版」长得一样")
            ok, bad = False, bad + 1
        elif n_evidence:
            print(f"  [注] 原料里另有 {n_evidence} 条本系统核验记录"
                  f"（{EVIDENCE_URL_PREFIX}…，非网页原料，归事实库节 (8) 管辖）")

    if not bad:
        print(f"  [通过] 出网闸门在发请求之前拦住非 https / 白名单外 / 本机地址"
              f"（请求未发出）；白名单无本机内网通配；日期与原始发布方"
              f"推不出即留空；原料 {n} 条全在白名单域"
              + (f"（含 {n_evidence} 条 evidence:// 核验记录，另归 (8) 管）"
                 if n_evidence else ""))
    return ok


def _check_ruling_store(ok: bool) -> bool:
    """⑦-5j 裁定经验库：**先例必须是提示而不是证据，且"可校准"要另算一份数**。

    🔴 这一节的判据全是**防自欺**，因为这个模块最容易造出三种假象：

      1. **循环论证**：拿裁定自己当基准结论 ⇒ 一致率恒等于 100%，
         而"校准"这件事**直接失去意义**。⇒ 断言：只给裁定、不给标注 ⇒
         `graded` 必须为 **0**。
      2. **「N 条经验」被当成「N 条可用于校准的经验」**：绝大多数裁定没有基准
         （没人去核），它们能当先例，**不能**进一致率的分子分母。
         ⇒ 断言 `total` / `graded` / `undetermined` 三个数分开报。
      3. **`accuracy` 的"没有数据"**：`graded == 0` 时它必须是 **`None`**，
         不是 `0.0` —— 0.0 会被读成"全错"，而真相是"没有可比的东西"。
         （本项目 2026-10-05 实证：`all([])` 是 `True` 这个方向的错误最贵。）

    另外两条：
      · **闸门分得开**：相关事件查得到先例、完全不相关的查不到（只差一个变量）。
      · **阈值不许永远蒙着**：`MIN_SCORE` 现在是占位值，库一旦涨到
        `MIN_SCORE_DERIVE_AT` 条而没实测重定，必须**报 pending** ⇒
        判据用 monkeypatch 把门槛降下来，证明这个哨兵**真的会响**。
    """
    import tempfile

    from legal_acps import precedent as pr
    from legal_acps import ruling_store as rs

    print("⑦-5j 裁定经验库（先例≠证据 · 可校准另算 · 阈值哨兵）")
    bad = 0

    # ── 造一份**自带 ground truth** 的裁定库（4 条相关 / 8 条不相关）──────
    #     ⚠️ 为什么是 12 条而不是 2 条：`DF_CUT_RATIO = 0.4` 在小库上极凶 ——
    #     5 条库里任何出现在 ≥3 条的 gram 都会被当虚词丢掉，于是真正讲同一件事的
    #     那几条**互相把特征词挤没了**。12 条库 df_cut = 4，才谈得上"分得开"。
    RELATED = "网传某小区自来水被上游工厂废水污染，居民连夜排队抢购瓶装水。"
    UNRELATED = "火星上发现一座古代城市遗址，考古学家确认是外星文明留下的建筑。"
    water = [
        "网传某小区自来水被上游工厂废水污染，居民连夜排队抢购瓶装水，网传检测报告据称系三个月前拍摄。",
        "某县城自来水被工业园区废水污染，市民排队抢购瓶装水，官方已通报检测结果。",
        "传闻本地自来水遭上游排污影响，居民连夜抢购瓶装水，供水公司出面辟谣。",
        "网传自来水被工厂废水污染，居民排队买瓶装水，检测报告系旧图。",
    ]
    others = [
        "某明星演唱会门票开售即秒罄，粉丝彻夜排队，主办方称将加场。",
        "网传某高校将取消期末考试，学校辟谣称教学安排不变。",
        "传闻某地铁线路月底停运改造，运营方回应系不实信息。",
        "某品牌手机新品发布，线上线下同步开售，首日销量公布。",
        "网传某地即将限购车牌，交管部门称尚未接到通知。",
        "传闻某景区门票将大幅涨价，管理处回应维持原价。",
        "某足球队更换主教练，俱乐部官方公告已发布。",
        "网传某银行将停止房贷，银行辟谣称业务正常办理。",
    ]

    def _write(d: Path, name: str, event: str, verdict: str,
               conf: float, basis: list[str] | None = None) -> None:
        (d / name).write_text(json.dumps({
            "event": event, "verdict": verdict, "confidence": conf,
            "basis": basis or ["依据一", "依据二"],
            "fallacies": [{"kind": "举证倒置", "speaker": "pro"}],
        }, ensure_ascii=False), encoding="utf-8")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "out"
        root.mkdir()
        for i, ev in enumerate(water, 1):
            _write(root, f"ruling-rel{i}.json", ev, "无法判定", 0.6)
        for i, ev in enumerate(others, 1):
            _write(root, f"ruling-oth{i}.json", ev, "不实", 0.8)
        # 🔴 偏食夹具（2026-10-07 服务器实证：30 条裁定只有 13 个不同事件）：
        #    给 rel1 补两份**同文重跑**（文件名排序在 rel1 之前 ⇒ rel1 是最新一轮
        #    且是唯一有标注的一轮 ⇒ 双重优先都指向它）。
        _write(root, "ruling-rel1-copy2.json", water[0], "属实", 0.9)
        _write(root, "ruling-rel1-copy3.json", water[0], "不实", 0.7)

        # ── (1) 闸门分得开：相关非空 / 不相关为空（**只差一个变量**）─────
        #     ⚠️ **如实说明**：在这份 12 条夹具上，真正把不相关挡在外面的是
        #     `MIN_HITS`（至少命中 2 个片段），**阈值一条都没多拦**
        #     （实测 `min_score=0.0` 与默认返回**完全相同**）。
        #     ⇒ 这条断言的是**两道闸门合起来**的效果，**不是**阈值单独的效果 ——
        #     别在文档里说成"阈值在拦"。阈值是第二道闸，真实库变大后才会吃重。
        store, rep = rs.RulingStore.load(root)
        if not rep.ok:
            print(f"  [失败] 裁定库加载为空：{rep.reasons} ⇒ 后面的断言都无意义")
            return False
        got_rel = store.search(RELATED)
        got_unrel = store.search(UNRELATED)
        if not got_rel:
            print("  [失败] 先例闸门：相关事件**查不到**先例 ⇒ 该功能等于没做")
            ok, bad = False, bad + 1
        if got_unrel:
            print(f"  [失败] 先例闸门：不相关事件返回了 {len(got_unrel)} 条"
                  f"（最高分 {max(h.match.score for h in got_unrel):.2f}）"
                  f" ⇒ 把不相干的旧裁定当先例喂给裁判")
            ok, bad = False, bad + 1

        # ── (2) 循环论证闸门：只给裁定、**不给标注** ⇒ `graded` 必须为 0 ──
        if rep.graded != 0:
            print(f"  [失败] 没有外部标注却有 {rep.graded} 条被判为「有基准」"
                  f" ⇒ 基准结论是从裁定自己推的（**循环论证**，一致率恒为 100%）")
            ok, bad = False, bad + 1
        if store.stats()["accuracy"] is not None:
            print(f"  [失败] 一条基准都没有，却给出了 accuracy="
                  f"{store.stats()['accuracy']} ⇒ 「没有可比的东西」被当成了一个数字")
            ok, bad = False, bad + 1

        # ── (3) 有基准的那部分才进一致率（total / graded 分开报）─────────
        gt = Path(td) / "gt.jsonl"
        gt.write_text(
            json.dumps({"source": "ruling-rel1.json", "ground_truth": "不实",
                        "error_reason": "把「无反证」当成了采信理由"}) + "\n"
            + json.dumps({"source": "ruling-oth1.json", "ground_truth": "不实",
                          "error_reason": ""}) + "\n",
            encoding="utf-8")
        store2, rep2 = rs.RulingStore.load(root, ground_truth=gt)
        st = store2.stats()
        if (st["total"], st["graded"], st["correct"]) != (12, 2, 1):
            print(f"  [失败] 统计没把「有基准」与「没基准」分开："
                  f"total={st['total']} graded={st['graded']} correct={st['correct']}"
                  f"（期望 12/2/1）⇒ 「N 条经验」被当成了「N 条可校准的经验」")
            ok, bad = False, bad + 1
        if st["undetermined"] != 10:
            print(f"  [失败] 无基准条数 {st['undetermined']}（期望 10）"
                  f" ⇒ 弃权率算不出来，一致率就是个没分母的分数")
            ok, bad = False, bad + 1
        if st["accuracy"] != 0.5:
            print(f"  [失败] 一致率 {st['accuracy']}（期望 0.5）")
            ok, bad = False, bad + 1
        # 🔴 「verdict 与 ground_truth 必须来自**同一轮**」在这里断（带标注的库）：
        #    代表必须是 rel1 —— 它既是最新一轮、又是被标注的那轮。若折叠错拿成
        #    copy2（verdict=属实、无标注），等于把旧轮的裁定与新轮的基准拼成一对，
        #    一致率就在**数一个从未发生过的配对**。
        rel1g = next((c for c in store2 if c.source == "ruling-rel1.json"), None)
        if rel1g is None or not rel1g.graded or rel1g.verdict != "无法判定":
            print(f"  [失败] 折叠代表选错：带标注库里 rel1 graded="
                  f"{getattr(rel1g, 'graded', None)} verdict="
                  f"{getattr(rel1g, 'verdict', None)!r} ⇒ verdict 与 ground_truth "
                  f"必须来自**同一轮**，拼不同轮 = 伪造配对")
            ok, bad = False, bad + 1

        # ── (3b) 统计必须报**去重后的事件数**（`total` 会被重复轮次灌水）──
        #     🔴 实测（2026-10-07，服务器）：**30 条裁定里只有 13 个不同事件**，
        #     其中「某学校食堂使用过期食材」一个事件被跑了 **25 轮**。
        #     ⇒ `total=30` 会让一致率变成"那一个事件的一致率"，
        #     也会让阈值派生只反映"能不能召回那一个事件"。
        #     **「N 条经验」与「N 个不同事件」必须分开报。**
        st0 = store.stats()
        if st0.get("distinct_events") != len(store):
            print(f"  [失败] 统计没报**去重后**的事件数：distinct_events="
                  f"{st0.get('distinct_events')}（期望 {len(store)}）"
                  f" ⇒ 「N 条经验」会被当成「N 个不同事件」")
            ok, bad = False, bad + 1

        # ── (3c) 同事件折叠：多轮重跑只留一条经验，且**不许悄悄丢** ──────
        #     🔴 为什么要折叠（两层后果，实测）：① `total` 虚报库容
        #     （30 条 ≠ 30 个事件）；② **df 被灌水** ⇒ 近重复项的每个 gram
        #     都超 df_cut ⇒ 全被当虚词丢掉 ⇒ 自召回 16/30 失败（检索整个失效）。
        #     断言四件事：折叠数如实报、kept 不含重复、代表 = 最新且被标注的
        #     那轮、repeats 保留轮数（「反复出现」的信号不许在折叠中丢掉）。
        if rep.kept != 12 or rep.duplicates != 2 or rep.kept_before_fold != 14:
            print(f"  [失败] 同事件折叠没做对：文件 {rep.total} 个、折叠前 "
                  f"{rep.kept_before_fold} 条、折叠后 {rep.kept} 条、折叠掉 "
                  f"{rep.duplicates}（期望 14→12、fold 2）⇒ 重跑的旧裁定还在冒充"
                  f"独立经验")
            ok, bad = False, bad + 1
        rel1 = next((c for c in store if c.source == "ruling-rel1.json"), None)
        if rel1 is None or rel1.repeats != 3:
            print(f"  [失败] 折叠后找不到代表轮或轮数不对：rel1.repeats="
                  f"{getattr(rel1, 'repeats', None)}（期望 3）⇒ 「同类事件已裁定"
                  f"过几轮」这个信号在折叠中丢了")
            ok, bad = False, bad + 1
        if rel1 is not None and rel1.verdict != "无法判定":
            # 代表必须是 rel1（文件名序=时间正序里最新的一轮），而不是任何一份
            # copy —— copy2 的裁定是「属实」。⚠️ graded 的检查**必须**放到下面
            # 带标注的 store2 上做（第一遍 load 没有 gt，`graded` 恒为 False）。
            print(f"  [失败] 折叠代表选错：拿到了 verdict={rel1.verdict!r}"
                  f"（期望 '无法判定'，即最新那轮）⇒ 旧一轮的裁定冒充了当前经验")
            ok, bad = False, bad + 1
        if st0.get("raw_total") != 14:
            print(f"  [失败] stats 没报折叠前的原始裁定数：raw_total="
                  f"{st0.get('raw_total')}（期望 14）⇒ 「12 个事件、14 份裁定书」"
                  f"与「12 个事件、12 份裁定书」就说不清了")
            ok, bad = False, bad + 1

        # ── (4) 渲染必须带**解释**与**免责**（否则先例会被当权威依据）─────
        text = rs.RulingStore.render(store2.search(RELATED))
        for must, why in (("命中片段", "看不出为什么这条算相关 ⇒ 无法质疑"),
                          ("不是本案的证据", "先例会被当成权威结论直接用")):
            if must not in text:
                print(f"  [失败] 先例渲染缺「{must}」⇒ {why}")
                ok, bad = False, bad + 1

        # ── (5) 阈值哨兵真的会响（库涨到门槛而没实测重定 ⇒ 报 pending）───
        real_at, real_derived = rs.MIN_SCORE_DERIVE_AT, rs.MIN_SCORE_DERIVED
        try:
            rs.MIN_SCORE_DERIVE_AT = 1
            rs.MIN_SCORE_DERIVED = False
            if not store2.stats()["min_score_pending"]:
                print("  [失败] 库已满门槛而 `MIN_SCORE` 仍是占位值，却**没有报 pending**"
                      " ⇒ 占位阈值会被一直当真，而它一个实测数据都没有")
                ok, bad = False, bad + 1
        finally:
            rs.MIN_SCORE_DERIVE_AT, rs.MIN_SCORE_DERIVED = real_at, real_derived

        # ── (6) 注入**真的发生**（行为断言：落在交回给框架的那个值上）─────
        hooks = pr.PrecedentHooks(root=root)

        class _Msg:
            role = "user"

            def __init__(self, t: str) -> None:
                self._t = t

            def text(self) -> str:
                return self._t

        class _State:
            def __init__(self, msgs: list[_Msg]) -> None:
                self.messages = msgs

        class _Ctx:
            def __init__(self, msgs: list[_Msg]) -> None:
                self.state = _State(msgs)
                self.private_state: dict[str, object] = {}

        blocks = hooks.context(_Ctx([_Msg(RELATED)]))
        if not blocks:
            print(f"  [失败] 先例注入没有发生（trace={hooks.trace}）⇒ "
                  f"「装了 Hook」不等于「会响」")
            ok, bad = False, bad + 1
        else:
            if blocks[0].trusted:
                print("  [失败] 先例块被标成 `trusted=True` ⇒ 它会被当证据采信，"
                      "而它只是提示")
                ok, bad = False, bad + 1
        # 不相关事件 ⇒ 一个字都不注入（宁缺勿假）
        hooks2 = pr.PrecedentHooks(root=root)
        if hooks2.context(_Ctx([_Msg(UNRELATED)])):
            print("  [失败] 不相关事件也注入了先例 ⇒ 阈值没在拦")
            ok, bad = False, bad + 1

        # ── (7) 降级不许拖垮主流程：库读不了 ⇒ 返回空、**不抛** ──────────
        hooks3 = pr.PrecedentHooks(root=Path(td) / "不存在")
        try:
            if hooks3.context(_Ctx([_Msg(RELATED)])):
                print("  [失败] 库不存在却注入了先例")
                ok, bad = False, bad + 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [失败] 先例是**加分项**，库读不了却抛了 {type(exc).__name__}"
                  f" ⇒ 拖垮了整条链路")
            ok, bad = False, bad + 1
        if not hooks3.trace:
            print("  [失败] 降级**没有留痕** ⇒ 「这轮没注入」与「注入了但模型没听」"
                  "分不开")
            ok, bad = False, bad + 1

    if not bad:
        print(f"  [通过] 先例闸门分得开（相关 {len(got_rel)} 条 / 不相关 0 条）；"
              f"无标注 ⇒ graded=0、accuracy=None（不是 0.0）；"
              f"有标注才进一致率（12 条中 2 条可校准）；"
              f"渲染带命中解释与免责、注入块 trusted=False；"
              f"阈值哨兵会响；库读不了静默降级且留痕")
    return ok


def _check_calibration(ok: bool) -> bool:
    """⑦-5k 判定校准台账：**值域缺口必须报出来，两种病必须分得开**。

    被测模块：`tools/calibration.py`（第 ④ 步交付物）。它自己就是
    「让判断力可测量」的仪表盘，而仪表盘最危险的失败方式不是报错，
    是**安静地给出误导性的数字**：

      1. **值域缺口不报** —— 基准里出现「不实」而 `RulingVerdict` 值域没有它
         ⇒ 那些样本**结构性不可能判对**。缺口不报，「一致率 0%」就会全背在
         模型头上（2026-10-07 第一份真实台账实测撞上：11 条官方证伪的案子）。
      2. **弃权与判错合并** —— 弃权的病因在证据通路（拿不到信源），判错的
         病因在判断逻辑（如命题偷换），两种病药方完全不同；合成一栏，
         一致率就成了个说不清的数字。
      3. **汇总数自算** —— calibration 不引用 `stats()` 而自己数一遍 ⇒
         两处口径迟早漂移（本项目实证过的病）。⇒ monkeypatch `stats()`
         返回假值，台账必须**跟着变**（引用了才会变）。
      4. **弃权混进校准曲线** —— 弃权没有「预测」，进分桶只会把曲线搅浑。
    """
    import tempfile

    from legal_acps import ruling_store as rs
    from tools import calibration as cal

    print("⑦-5k 判定校准台账（值域含不实 · 缺口要报 · 两种病分开 · 单一来源）")
    bad = 0

    # ── (0) 值域必须含「不实」，且三处枚举不许漂移 ────────────────────
    #     🔴 值域缺口（未决 #16，2026-10-07 补）：旧三态下官方证伪的谣言
    #     （基准 13 条里 11 条）**结构性不可能判对**。补了之后最危险的病变成
    #     「悄悄回退」—— 所以三条断言钉死：① 契约真的接受它（行为断言）；
    #     ② 进指纹的 capability description 枚举里有它；③ 不进指纹的
    #     instructions 枚举里也有它（同一判定写两处必然漂移，第三处是文档）。
    from legal_acps import agents as _ag
    from legal_acps.contracts import FactRuling as _FactRuling
    try:
        _FactRuling(event="测试", verdict="不实", confidence=0.9, reasoning="测试")
    except Exception as exc:  # noqa: BLE001
        print(f"  [失败] 值域缺「不实」：FactRuling 拒绝了 verdict=不实（{type(exc).__name__}）"
              f" ⇒ 官方证伪的案子结构性判不对（未决 #16 回退）")
        ok, bad = False, bad + 1
    if "不实" not in _ag.issue_ruling.capability.description:
        print("  [失败] 值域缺「不实」：issue_ruling 的 capability description 枚举里没有它"
              f"（现为 {_ag.issue_ruling.capability.description!r}）⇒ 模型不知道能选它")
        ok, bad = False, bad + 1
    #     ⚠️ instructions 的断言**锚到枚举行**（含「确有其事」的行必须同含
    #     「不实」），不能只查整段含不含 —— 判定指导段（「何时选不实」）
    #     里本来就满是「不实」三个字，整段查 ⇒ 断言恒真、退回形态空转
    #     （2026-10-07 元验证用例 7 实测抓出）。
    _enum_lines = [ln for ln in _ag.FACT_JUDGE_INSTRUCTIONS.splitlines()
                   if "确有其事" in ln]
    if not _enum_lines or any("不实" not in ln for ln in _enum_lines):
        print("  [失败] 值域缺「不实」：FACT_JUDGE_INSTRUCTIONS 的枚举行里没有它"
              " ⇒ 提示词与契约两处漂移（Enum 有、提示词不让选）")
        ok, bad = False, bad + 1

    # ── 夹具：5 个互不相似的事件（折叠不会并组），4 条有基准 ──────────
    #     弃权 / 判对 / 判错 / 值域内判对 各一，另有 1 条无基准。
    #     其中 g1、g3 的基准是「部分属实」—— 刻意选**值域外**的取值来测
    #     缺口检测。⚠️ 2026-10-07 值域补「不实」之前，这里用的是「不实」
    #     本尊；补完之后「不实」进值域了，夹具改用仍值域外的「部分属实」
    #     —— 缺口检测这个**机制**仍要有自己的判据（不能因为一个缺口修好
    #     就让整节断言变空）。
    FIX = [
        # (事件, 裁定, 置信度, 基准, 期望角色)
        ("网传某小区自来水被工厂废水污染，居民排队抢购瓶装水。",
         "无法判定", 0.5, "部分属实", "弃权且值域外"),
        ("有人用 AI 换脸视频冒充亲属实施电信诈骗。",
         "确有其事", 0.9, "确有其事", "判对"),
        ("网传某银行即将倒闭，大批储户集中取款。",
         "确有其事", 0.85, "部分属实", "判错且值域外"),
        ("某明星演唱会门票开售即秒罄，粉丝彻夜排队。",
         "断章取义", 0.7, "断章取义", "判对且值域内"),
        ("网传某高校将取消期末考试，学校辟谣称教学安排不变。",
         "无法判定", 0.6, "", "无基准"),
    ]

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "out"
        root.mkdir()
        gt_lines = []
        for i, (event, verdict, conf, gt, _role) in enumerate(FIX, 1):
            name = f"ruling-cal{i}.json"
            (root / name).write_text(json.dumps({
                "event": event, "verdict": verdict, "confidence": conf,
                "basis": ["依据一"], "fallacies": [],
            }, ensure_ascii=False), encoding="utf-8")
            if gt:
                gt_lines.append(json.dumps(
                    {"source": name, "ground_truth": gt, "error_reason": ""},
                    ensure_ascii=False))
        gt = Path(td) / "gt.jsonl"
        gt.write_text("\n".join(gt_lines) + "\n", encoding="utf-8")

        store, rep = rs.RulingStore.load(root, ground_truth=gt)
        if not rep.ok or rep.graded != 4:
            print(f"  [失败] 夹具没搭好：ok={rep.ok} graded={rep.graded}"
                  f"（期望 4）⇒ 后面的断言都在数错东西")
            return False

        # ── (1) 弃权 / 判对 / 判错三分类，分得开 ──────────────────────
        sp = cal.abstain_split(store)
        if sp != {"abstain": 1, "right": 2, "wrong": 1}:
            print(f"  [失败] 弃权与判错没分开：abstain={sp.get('abstain')} "
                  f"right={sp.get('right')} wrong={sp.get('wrong')}"
                  f"（期望 1/2/1）⇒ 两种病因不同的病被并成了一栏，"
                  f"一致率就是个说不清的数字")
            ok, bad = False, bad + 1

        # ── (2) 值域缺口必须报出来（结构性偏差先于一切模型归因）───────
        gaps = cal.domain_gap(store)
        if gaps != ["部分属实（2 条）"]:
            print(f"  [失败] 值域缺口没报出来：domain_gap={gaps!r}"
                  f"（期望 ['部分属实（2 条）']）⇒ 基准里 2 条值域外取值"
                  f"会被算成模型的错，而它**结构性地不可能判对**")
            ok, bad = False, bad + 1

        # ── (3) 台账渲染必须含缺口标记与缺口详情 ─────────────────────
        text = cal.render(store)
        if cal.DOMAIN_GAP_MARK not in text or "部分属实（2 条）" not in text:
            print(f"  [失败] 台账没把值域缺口写进正文（标记 "
                  f"{cal.DOMAIN_GAP_MARK!r}、详情 '部分属实（2 条）'）"
                  f" ⇒ 读台账的人会把结构性偏差当模型偏差")
            ok, bad = False, bad + 1

        # ── (4) 闸门分得开：基准全落在值域内 ⇒ 缺口为空（只差一个变量）──
        #     ⚠️ 夹具里值域外的是 g1、g3 两条 —— 改的时候要**全改**，
        #     只改一条的话「缺口为空」的期望本身就是错的（2026-10-07 实测踩到）。
        gt_lines[0] = json.dumps({"source": "ruling-cal1.json",
                                  "ground_truth": "断章取义", "error_reason": ""},
                                 ensure_ascii=False)
        gt_lines[2] = json.dumps({"source": "ruling-cal3.json",
                                  "ground_truth": "断章取义", "error_reason": ""},
                                 ensure_ascii=False)
        gt2 = Path(td) / "gt2.jsonl"
        gt2.write_text("\n".join(gt_lines) + "\n", encoding="utf-8")
        store2, _ = rs.RulingStore.load(root, ground_truth=gt2)
        if cal.domain_gap(store2):
            print(f"  [失败] 基准全在值域内却报了缺口 "
                  f"{cal.domain_gap(store2)!r} ⇒ 缺口检测只会恒真，"
                  f"它就成了台账里的固定装饰")
            ok, bad = False, bad + 1

        # ── (5) 混淆矩阵数对（判错落在哪一格要看得见）─────────────────
        conf = cal.confusion(store)
        if conf.get(("确有其事", "部分属实")) != 1:
            print(f"  [失败] 混淆矩阵没数对：(确有其事, 部分属实)="
                  f"{conf.get(('确有其事', '部分属实'))}（期望 1）"
                  f" ⇒ 错在哪里一眼看不见")
            ok, bad = False, bad + 1

        # ── (6) 弃权不进置信度分桶（弃权没有「预测」）─────────────────
        buckets = cal.confidence_buckets(store)
        firm_n = sum(int(b["n"]) for b in buckets)
        if firm_n != 3:
            print(f"  [失败] 置信度分桶把弃权或无基准样本算了进去："
                  f"桶内共 {firm_n} 条（期望 3 —— 只有给出明确判断且有基准的）"
                  f" ⇒ 弃权没有「预测」，进曲线只会把校准曲线搅浑")
            ok, bad = False, bad + 1

        # ── (7) 单一来源：monkeypatch stats() ⇒ 台账必须跟着变 ────────
        #     calibration 的库况数字**只能**引用 `stats()`；自算一遍 ⇒
        #     两处口径迟早漂移。引用了的证明 = 换掉 stats() 它就变。
        real_stats = store.stats
        fake = dict(real_stats())
        fake.update(total=999, raw_total=1999, graded=123, accuracy=None,
                    fallacies={"某话术": 88})
        store.stats = lambda: fake  # type: ignore[method-assign]
        try:
            text2 = cal.render(store)
        finally:
            store.stats = real_stats  # type: ignore[method-assign]
        if "999 个事件" not in text2 or "某话术：88" not in text2:
            print("  [失败] 台账的汇总数没有引用 stats()（monkeypatch 后没跟着变）"
                  " ⇒ 同一个数存在两处，口径漂移只是时间问题")
            ok, bad = False, bad + 1

    if not bad:
        print("  [通过] 值域含「不实」且三处枚举（契约/description/instructions）不漂移；"
              "弃权/判对/判错三分类分得开（1/2/1）；缺口检测如实报出"
              "（部分属实 2 条）且闸门分得开；混淆矩阵数对；弃权不进置信度分桶；"
              "台账汇总数引用 stats()（monkeypatch 后跟着变）")
    return ok


def _check_audio_gate(ok: bool) -> bool:
    """⑦-5f 音频委派闸门：带音频的轮次里，**委派出手之前不许出终稿**。

    2026-10-06 真机第二次复跑夹出的根因：`audio_modality` 的降级把一份
    **完整的技术测量报告**塞进了主控上下文 ⇒ 主控手上已经有读数 ⇒ 自己出
    技术结论、直接 `legal_issue_verdict`，委派链一次都没走 —— 那一轮
    `status=completed`、台账齐全、零异常，而 `output/` 里没有新的
    `evidence-*.json`（终稿里那些"仪器读数"正是降级文本里抄来的）。

    ⚠️ 提示词里那条顺序铁律当时**已经写了**（⑦-5e 守着它），真机上模型
    仍然不照做 ⇒ 「提示词改了 ≠ 模型照做」第二次坐实 ⇒ 只能靠闸门。
    所以这里**全部是行为断言**：真跑一遍 `wrap_tool_call`，看它拦不拦、
    handler 到底有没有被调用（"挂上了"不等于"会响"）。

    ⚠️ **闸门要"分得开"**：下面每一对只差一个变量的输入都必须给出**不同**
    结论，否则它就不是闸门，而是一个永远（或永不）触发的开关。
    """
    from wit import AudioPart, VideoPart

    from .audio_gate import (
        BLOCKS_KEY,
        DELEGATE_TOOL,
        DEFAULT_MAX_BLOCKS,
        AudioDelegationGateHooks,
    )
    from .termination import VERDICT_TOOL

    class _Msg:
        def __init__(self, parts: list) -> None:
            self.role = "user"
            self.content = parts

    class _State:
        def __init__(self, parts: list) -> None:
            self.metadata: dict = {}
            self.messages = [_Msg(parts)]

    class _Ctx:
        def __init__(self, state: object) -> None:
            self.state = state
            # ⚠️ 故意留成 None：闸门**优先**用 `state.metadata`（每轮新建），
            #    这条顺带证明它不依赖 `private_state`。
            self.private_state = None

    class _Call:
        def __init__(self, name: str) -> None:
            self.name = name
            self.id = "call-1"

    AUDIO = AudioPart(mime_type="audio/wav", data=b"RIFF" + b"\x00" * 40)
    VIDEO = VideoPart(mime_type="video/mp4", data=b"\x00\x00\x00\x18ftyp")

    async def _drive(parts: list, names: list[str]) -> tuple[list[dict], list[str], dict]:
        """把一串工具调用喂给闸门。返回 `(各次返回, handler 收了哪些, 记账袋)`。

        判据看的是**交回给调用方的那个值**与 **handler 有没有被放行**，
        两件事各要一条 —— 只断言返回值，会出现"报了错但工具其实跑了"。
        """
        state = _State(list(parts))
        ctx = _Ctx(state)
        hook = AudioDelegationGateHooks()
        seen: list[str] = []

        async def handler(tc: object) -> dict:
            seen.append(getattr(tc, "name", "?"))
            return {"status": "ok", "tool": getattr(tc, "name", "?")}

        outs = [
            await hook.wrap_tool_call(ctx, _Call(n), handler) for n in names
        ]
        return outs, seen, state.metadata

    # ---- ① 有音频 + 一次都没派 ⇒ 必须拦，且 handler 不能被放行 ----
    outs1, seen1, bag1 = asyncio.run(_drive([AUDIO], [VERDICT_TOOL]))
    if outs1[0].get("status") != "error":
        print(
            f"  [失败] 音频闸门：有音频且没委派出手，`{VERDICT_TOOL}` 竟然放行了"
            " ⇒ 主控会拿降级报告自己出技术结论，音频链被静默绕过"
        )
        return False
    if seen1:
        print(
            f"  [失败] 音频闸门：报了错却仍然调用了工具 {seen1}"
            " ⇒ 工具其实跑了，只报错没拦住"
        )
        return False
    if bag1.get(BLOCKS_KEY) != 1:
        print(
            f"  [失败] 音频闸门：拦了一次，记账袋里 {BLOCKS_KEY} 是"
            f" {bag1.get(BLOCKS_KEY)!r}、应为 1 ⇒ 拦没拦过无从观测"
        )
        return False
    if DELEGATE_TOOL not in str(outs1[0].get("error")):
        print(
            f"  [失败] 音频闸门：拒绝里没有点名 `{DELEGATE_TOOL}`"
            " ⇒ 模型只知道不行、不知道下一步做什么，只会重试同一个调用"
        )
        return False

    # ---- ② 同样的音频，派过一次 ⇒ 立即放行（成功或失败都算）----
    _, seen2, _ = asyncio.run(_drive([AUDIO], [DELEGATE_TOOL, VERDICT_TOOL]))
    if seen2 != [DELEGATE_TOOL, VERDICT_TOOL]:
        print(
            f"  [失败] 音频闸门：委派过后仍然不放行（handler 收到的 {seen2}）"
            " ⇒ 闸门会把 run 卡死，比「委派没走」更糟"
        )
        return False

    # ---- ③ 纯文本轮次 ⇒ 放行（闸门不许影响不带音频的链路）----
    _, seen3, _ = asyncio.run(_drive([], [VERDICT_TOOL]))
    if seen3 != [VERDICT_TOOL]:
        print(f"  [失败] 音频闸门：纯文本轮次也被拦了（handler 收到的 {seen3}）")
        return False

    # ---- ④ 只有视频 ⇒ 放行（闸门只对音频响，这是"分得开"的反向那一半）----
    _, seen4, _ = asyncio.run(_drive([VIDEO], [VERDICT_TOOL]))
    if seen4 != [VERDICT_TOOL]:
        print(
            f"  [失败] 音频闸门：只有视频的轮次也被拦了（handler 收到的 {seen4}）"
            " ⇒ 闸门管宽了，会挡住没实测过的链路"
        )
        return False

    # ---- ⑤ 别的工具 ⇒ 放行 ----
    _, seen5, _ = asyncio.run(_drive([AUDIO], ["legal_some_other_tool"]))
    if seen5 != ["legal_some_other_tool"]:
        print(f"  [失败] 音频闸门：无关工具也被拦了（handler 收到的 {seen5}）")
        return False

    # ---- ⑥ 兜底放行：连拦 N 次仍不委派 ⇒ 第 N+1 次放行 ----
    outs6, seen6, _ = asyncio.run(
        _drive([AUDIO], [VERDICT_TOOL] * (DEFAULT_MAX_BLOCKS + 1))
    )
    if seen6 != [VERDICT_TOOL]:
        print(
            f"  [失败] 音频闸门：连拦 {DEFAULT_MAX_BLOCKS} 次之后**仍不放行**"
            f"（handler 收到的 {seen6}）⇒ 模型不委派时整个 run 变成零产物"
        )
        return False
    if [o.get("status") for o in outs6] != ["error"] * DEFAULT_MAX_BLOCKS + ["ok"]:
        print(
            f"  [失败] 音频闸门：拦/放的次序不对 {[o.get('status') for o in outs6]}"
            f" ⇒ 应先拦 {DEFAULT_MAX_BLOCKS} 次再放行"
        )
        return False

    # ---- ⑦ 挂载：主控必须真的挂上（现场派生，不写死清单）----
    agents = _role_agents()
    missing = [
        r for r in ("leader",)
        if not any(isinstance(h, AudioDelegationGateHooks)
                   for h in getattr(agents.get(r), "hooks", []))
    ]
    if missing:
        print(
            f"  [失败] 音频闸门：{missing} 没挂这个 Hook ⇒ 上面六条断言都在测"
            "一个**没接进链路**的对象（「装了」≠「会响」的另一半）"
        )
        return False
    over = sorted(
        r for r, a in agents.items()
        if r != "leader"
        and any(isinstance(h, AudioDelegationGateHooks) for h in getattr(a, "hooks", []))
    )
    if over:
        print(
            f"  [失败] 音频闸门：{over} 也挂了它 ⇒ 那些角色没有终稿工具，"
            "挂上去是死对象"
        )
        return False
    print(
        f"  [通过] 音频闸门：有音频且没委派出手时拦住 `{VERDICT_TOOL}`"
        f"（拒绝里点名 `{DELEGATE_TOOL}`），派过一次/纯文本/只有视频/别的工具"
        f" 都放行，连拦 {DEFAULT_MAX_BLOCKS} 次后兜底放行；主控已挂载"
    )
    return ok


def _modality_partner_names() -> list[str]:
    """Leader 应当被交代的下游名字 —— 取自 Partner **自己注册的** `display_name`。"""
    return [r.partner_name for r in _modality_rows()]


def _leader_leaked_partner_names() -> list[str]:
    """主控提示词里**不该出现**的下游注册名，实际出现了哪些。

    判据是"两边的名字一致"，不是"我抄对了没"：清单来自各 Partner 自己注册的
    `display_name`，所以 Partner 改名而提示词没跟上时这里也会响。
    ⚠️ 空名字要**排除掉**再比：`"" in text` 恒为真，一条空名字会让这条断言
    **静默通过** —— 那正好是最需要它响的时候。
    """
    return sorted(
        r.partner_name
        for r in _modality_rows()
        if r.partner_name and r.partner_name in LEADER_INSTRUCTIONS
    )


def _leader_missing_branches() -> list[str]:
    """主控提示词里**应当有**却缺失的鉴伪分支标签。

    约定 `f"{模态}鉴伪分支"` —— 与 Partner 名共用同一套词（`图像` / `音频` / `视频`），
    所以加一个模态时正反两条判据会一起跟上，不必手改清单。
    """
    labels = [f"{m.value}鉴伪分支" for m in EvidenceModality]
    return [lbl for lbl in labels if lbl not in LEADER_INSTRUCTIONS]


def _check_human_debate(ok: bool) -> bool:
    """⑦-6 人机辩论驱动器：台账 / 回放 / 抽取 / 终止 / 隔离 五组离线行为断言。

    为什么必须**离线**断言（而不是"真机跑过一轮就行"）：
      · 人机辩论的循环逻辑（回放拼接、失败记账、concede 传播、重试）在一次
        真机对跑里**只走一条路径** —— concede 那场永远不会暴露「失败轮没记账」；
      · 注入假 invoke / 假 user_source 后，每条路径都能**定向走一遍**，
        断言落在台账文件的真实字节上（不是函数返回值的自说自话）。
    元验证：`run/meta_verify_human_debate.py`（回放 / 抽取 / 记账 / concede
    四处锚点，逐处退回旧形态要求本节变红）。
    """
    import asyncio
    import tempfile
    from types import SimpleNamespace

    from legal_acps import human_debate as hd
    from legal_acps import ruling_store as rs

    print("⑦-6 人机辩论驱动器（台账 · 回放 · 抽取 · 终止 · 隔离）")
    bad = 0
    TOOL = hd.TOOL_ID

    def _submit(result, status="success"):
        return {"tool": TOOL, "status": status, "result": result, "error": None}

    def _run(observations):
        return SimpleNamespace(status="completed", iterations=2,
                               observations=observations)

    # ── (1) 回放完整：第 3 轮输入必须含前两轮的用户与正方内容 ──────────
    #     无框架会话 ⇒ 记忆全靠显式回放，漏一轮正方就当那轮没发生过。
    hist = [
        {"round": 1, "user": "用户一轮论点甲", "pro": "正方一轮论点乙",
         "confidence": 0.7},
        {"round": 2, "user": "用户二轮论点丙", "pro": "正方二轮论点丁",
         "confidence": 0.5},
    ]
    text3 = hd.build_round_input("事件描述XYZ", "该传闻属实", hist,
                                 "用户三轮论点戊", 3)
    missing = [s for s in ("论点甲", "论点乙", "论点丙", "论点丁", "论点戊",
                           "round=3", "该传闻属实") if s not in text3]
    if missing:
        print(f"  [失败] 人机辩论回放：第 3 轮输入缺 {missing} ⇒ 正方看不见"
              "完整历史，会当漏掉的那轮没发生过（对抗性检验失效）")
        ok, bad = False, bad + 1
    text1 = hd.build_round_input("事件描述XYZ", "该传闻属实", [],
                                 "用户一轮论点甲", 1)
    if "辩论历史" in text1 or "round=1" not in text1:
        print("  [失败] 人机辩论回放：第 1 轮输入不该有历史段"
              "（或没让模型从 round=1 开始）")
        ok, bad = False, bad + 1

    # ── (2) 抽取口径：只认成功的 submit 调用，多条成功取最后 ──────────
    #     失败调用的 result 残渣（真机 observation 的 error 字段形态未承诺
    #     为 null）不许被当成论点 —— 按保守侧断言。
    pro1 = {"round": 1, "stance": "hold", "confidence": 0.4,
            "key_point": "一", "rebuts": []}
    pro2 = {"round": 2, "stance": "hold", "confidence": 0.5,
            "key_point": "二", "rebuts": [1]}
    junk = {"round": 2, "stance": "hold", "confidence": 0.9,
            "key_point": "失败残渣", "rebuts": []}
    got = hd.extract_pro_argument([
        {"tool": "other.tool", "status": "success", "result": {"round": 7}},
        _submit(pro1),
        _submit(junk, status="error"),
    ])
    if not (got and got.get("key_point") == "一"):
        print(f"  [失败] 人机辩论抽取：失败调用/别的工具的 result 被当成了论点"
              f"（拿到 {got and got.get('key_point')!r}）⇒ 错误残渣会进台账当正方论点")
        ok, bad = False, bad + 1
    got_last = hd.extract_pro_argument([_submit(pro1), _submit(pro2)])
    if not (got_last and got_last.get("round") == 2):
        print("  [失败] 人机辩论抽取：多次成功没取最后一条"
              "（重试语义：最后一次成功才作数）")
        ok, bad = False, bad + 1
    if hd.extract_pro_argument([]) is not None or \
            hd.extract_pro_argument(
                [{"tool": "x", "status": "success", "result": {}}]) is not None:
        print("  [失败] 人机辩论抽取：没有成功的 submit 调用却返回非 None"
              " ⇒ 「只写散文 = 轮失败」这条判据被抽空")
        ok, bad = False, bad + 1

    # ── (3) 终止决策：concede 传播 / 上限兜底 / 未到上限继续 ───────────
    if hd.round_decision({"stance": "concede"}, 1, 6) != "concede":
        print("  [失败] 人机辩论终止：concede 没有终止辩论"
              " ⇒ 用户不知道正方已认输，空转到上限")
        ok, bad = False, bad + 1
    if hd.round_decision({"stance": "hold"}, 6, 6) != "max_rounds":
        print("  [失败] 人机辩论终止：到上限没停 ⇒ 无限循环的确定性兜底没了")
        ok, bad = False, bad + 1
    if hd.round_decision({"stance": "hold"}, 2, 6) is not None:
        print("  [失败] 人机辩论终止：没到上限也不许继续 ⇒ 辩论被掐死在第 2 轮")
        ok, bad = False, bad + 1

    # ── (4) 整循环行为：假 invoke + 假 user_source，断言落在台账字节上 ──
    def _drive(invoke, user_args, *, max_rounds=6, retries=1):
        """离线跑一场；user_args 逐轮吐出（耗尽 = 用户退出）。"""
        with tempfile.TemporaryDirectory() as td:
            ledger = hd.DebateLedger("probe", directory=Path(td) / "debates")
            src = list(user_args)

            def user_source(n):
                return src[n - 1] if n - 1 < len(src) else None

            outcome = asyncio.run(hd.run_debate(
                "测试事件", pro_stance="该传闻属实", max_rounds=max_rounds,
                user_source=user_source, ledger=ledger, invoke=invoke,
                retries=retries, sid="probe", mode="probe"))
            lines = [json.loads(ln) for ln in
                     ledger.path.read_text(encoding="utf-8").splitlines()
                     if ln.strip()]
            return outcome, lines

    def _hold_arg(round_no):
        return {"round": round_no, "stance": "hold", "confidence": 0.6,
                "key_point": f"正方第{round_no}轮论点",
                "rebuts": [round_no - 1] if round_no > 1 else []}

    # (4a) concede 传播 + 每轮必落盘（用户原文 / 正方五字段都在 round 行）
    async def _concede_invoke(text, round_no, attempt):
        return _run([_submit({"round": round_no, "stance": "concede",
                              "confidence": 0.1, "key_point": "我方无法自洽",
                              "rebuts": [round_no - 1] if round_no > 1 else []})])

    outcome, lines = _drive(_concede_invoke, ["用户开题论点"])
    types = [l["type"] for l in lines]
    r1 = next((l for l in lines if l["type"] == "round"), None)
    if outcome != "concede" or types != ["session_open", "round", "session_close"] \
            or not r1 or r1.get("user_argument") != "用户开题论点" \
            or not (r1.get("pro") or {}).get("stance") == "concede" \
            or lines[-1].get("rounds") != 1:
        print(f"  [失败] 人机辩论循环：concede 场景 outcome={outcome} "
              f"台账形态={types} ⇒ 认输信号或每轮落盘被破坏")
        ok, bad = False, bad + 1

    # (4b) 上限兜底 + 第 2 轮**实际发给正方**的输入含第 1 轮回放（行为断言）
    seen: list[str] = []

    async def _hold_invoke(text, round_no, attempt):
        seen.append(text)
        return _run([_submit(_hold_arg(round_no))])

    outcome, lines = _drive(_hold_invoke, ["用户一轮", "用户二轮", "用户三轮"],
                            max_rounds=2)
    rounds = [l for l in lines if l["type"] == "round"]
    if outcome != "max_rounds" or len(rounds) != 2 or lines[-1].get("rounds") != 2:
        print(f"  [失败] 人机辩论循环：上限场景 outcome={outcome} "
              f"轮记录 {len(rounds)} 条（期望 2）⇒ 上限兜底或落盘被破坏")
        ok, bad = False, bad + 1
    if len(seen) < 2 or "用户一轮" not in seen[1] or "正方第1轮论点" not in seen[1]:
        print("  [失败] 人机辩论循环：第 2 轮实际发给正方的输入不含第 1 轮回放"
              " ⇒ 拼接函数与主循环脱节（回放只在单元测试里对，链路上没走）")
        ok, bad = False, bad + 1

    # (4c) 用户直接退出：0 轮、只有开收两条
    outcome, lines = _drive(_hold_invoke, [])
    types = [l["type"] for l in lines]
    if outcome != "user_exit" or types != ["session_open", "session_close"] \
            or lines[-1].get("rounds") != 0:
        print(f"  [失败] 人机辩论循环：退出场景 outcome={outcome} "
              f"台账形态={types} ⇒ 没开辩也留了假轮次（或没关账）")
        ok, bad = False, bad + 1

    # (4d) 异常留痕 + 重试成功：失败先记账（round_failed），成功紧随其后
    async def _flaky_invoke(text, round_no, attempt):
        if attempt == 1:
            raise ValueError("模拟网络炸")
        return _run([_submit(_hold_arg(round_no))])

    outcome, lines = _drive(_flaky_invoke, ["用户一轮", "用户二轮"], max_rounds=2)
    failed = [l for l in lines if l["type"] == "round_failed"]
    rounds = [l for l in lines if l["type"] == "round"]
    order_ok = all(
        lines.index(f) < min(
            (i for i, l in enumerate(lines)
             if l["type"] == "round" and l["round"] == f["round"]),
            default=len(lines))
        for f in failed)
    if outcome != "max_rounds" or len(failed) != 2 or len(rounds) != 2 \
            or not all("ValueError" in str(f.get("reason")) for f in failed) \
            or not order_ok:
        print(f"  [失败] 人机辩论循环：异常场景 outcome={outcome} "
              f"failed={len(failed)} rounds={len(rounds)} ⇒ 异常没留痕、"
              "没重试、或失败记在成功之后（顺序即正确性）")
        ok, bad = False, bad + 1

    # (4e) 只写散文（没有成功的 submit 调用）：重试耗尽 ⇒ error，全部留痕
    async def _essay_invoke(text, round_no, attempt):
        return _run([{"tool": "some.essay", "status": "success", "result": {}}])

    outcome, lines = _drive(_essay_invoke, ["用户一轮"], retries=1)
    failed = [l for l in lines if l["type"] == "round_failed"]
    if outcome != "error" or len(failed) != 2 or lines[-1].get("rounds") != 0:
        print(f"  [失败] 人机辩论循环：散文场景 outcome={outcome} "
              f"failed={len(failed)}（期望 2 = 重试 1 + 首跑 1）"
              " ⇒ 「必须调工具」的兜底被抽空")
        ok, bad = False, bad + 1

    # ── (5) 台账 append-only + 纯 LF ─────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        led = hd.DebateLedger("appendcheck", directory=Path(td))
        led.open_session(event="e", pro_stance="s", max_rounds=1,
                         mode="probe", retries=0)
        first = led.path.read_bytes()
        led.close(outcome="user_exit", rounds=0)
        second = led.path.read_bytes()
        if not second.startswith(first) or second == first:
            print("  [失败] 人机辩论台账：close 不是追加在后"
                  " ⇒ append-only 单一事实源被破坏")
            ok, bad = False, bad + 1
        if b"\r" in second:
            print("  [失败] 人机辩论台账：写出了 CR ⇒ 破坏纯 LF 同步纪律"
                  "（read_bytes 数 CR LF 的判据会响）")
            ok, bad = False, bad + 1

    # ── (6) 与经验库隔离：debates/ 里的辩词（含同名毒饵）不许被读到 ────
    #     正方立场是分配的、未经裁定的对抗内容 —— 进先例 = 污染判据。
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "ruling-real.json").write_text(json.dumps({
            "event": "网传某地自来水被污染，居民抢购瓶装水",
            "verdict": "不实", "confidence": 0.8,
            "basis": ["官方通报"], "fallacies": []}, ensure_ascii=False),
            encoding="utf-8")
        debates = root / "debates"
        debates.mkdir()
        (debates / "20260101T000000Z.jsonl").write_text(json.dumps({
            "ts": "2026-01-01T00:00:00+00:00", "type": "round", "round": 1,
            "user_argument": "网传某地自来水被污染，居民抢购瓶装水，我认为是真的",
            "pro": {"round": 1, "stance": "hold", "confidence": 0.9,
                    "key_point": "该传闻完全属实", "rebuts": []}},
            ensure_ascii=False) + "\n", encoding="utf-8")
        # 毒饵：连文件名都伪装成 ruling-*（load 一旦变成递归 glob 就中招）
        (debates / "ruling-poison.json").write_text(json.dumps({
            "event": "网传某地自来水被污染，居民抢购瓶装水",
            "verdict": "属实", "confidence": 0.99,
            "basis": [], "fallacies": []}, ensure_ascii=False), encoding="utf-8")
        _store, rep = rs.RulingStore.load(root)
        if rep.total != 1:
            print(f"  [失败] 人机辩论隔离：经验库读到了 debates/ 里的辩词"
                  f"（total={rep.total}，期望 1）⇒ 分配立场的对抗内容"
                  "会污染先例")
            ok, bad = False, bad + 1

    # ── (7) 方案③证据注入：渲染 / 注入零变化 / 循环集成 / 拒收不拉重依赖 ──
    #     体验层的核心承诺：用户提交的媒体证据在开辩前被本地核验，技术线索
    #     连同 caveat 注入**每一轮**正方输入。四组断言把承诺钉死：
    #       (7a) 渲染形态（纯函数，假记录——等级 / caveat / 铁律句 / 未核验原因）
    #       (7b) 注入被采纳 + 默认零变化（老路径逐字节不动）
    #       (7c) 整循环集成（evidence 台账行 + brief 进每轮输入——行为断言）
    #       (7d) 拒收路径不拉重依赖（numpy/PIL 进 check_local 进程 = 判据
    #            自己成为故障源，与 3c 探针泄漏判据同一条纪律）
    _ev_verified = {
        "name": "scene.jpg", "path": "/tmp/scene.jpg", "status": "verified",
        "modality": "image", "media_type": "image/jpeg", "byte_size": 1024,
        "sha256": "abcdef0123456789" * 4, "suspicion": "中",
        "rationale": "一项提示异常：复制-移动",
        "findings": [
            {"check": "copy_move", "verdict": "未提示异常", "strength": None,
             "detail": "无信号", "caveat": "阴性不证明干净"},
            {"check": "ela", "verdict": "提示异常", "strength": 0.6,
             "detail": "ELA 亮区集中在一处", "caveat": "压缩伪影也能造成"},
            {"check": "exif", "verdict": "无法判定", "strength": None,
             "detail": "EXIF 被剥掉", "caveat": "社交平台转存常见"},
        ],
    }
    _ev_refused = {"name": "note.txt", "path": "/tmp/note.txt",
                   "status": "refused", "reason": "三个模态的容器判定都认不出"}

    # (7a) 渲染形态
    brief = hd.render_evidence_brief([_ev_verified, _ev_refused])
    _need = ["综合线索等级：中", "ELA 亮区集中在一处", "caveat：压缩伪影也能造成",
             "其余 1 项未提示异常", "未核验", "三个模态的容器判定都认不出",
             "未经检验的主张", hd.EVIDENCE_BRIEF_TITLE, hd.EVIDENCE_BRIEF_RULES]
    _miss = [s for s in _need if s not in brief]
    if _miss or hd.render_evidence_brief([]) != "":
        print(f"  [失败] 证据渲染：brief 缺 {_miss or '（或空记录未返回空串）'}"
              " ⇒ 注入段丢等级/caveat/铁律，正方拿到的是无护栏的裸线索")
        ok, bad = False, bad + 1

    # (7b) 注入被采纳 + 默认零变化（复用 (1) 的 text3/text1：不传 brief 的
    #      老调用必须与旧形态完全一致——锚不许被新参数悄悄改变）
    text_ev = hd.build_round_input(
        "事件描述XYZ", "该传闻属实", hist, "用户三轮论点戊", 3,
        evidence_brief=brief)
    if "已核验证据技术线索" not in text_ev or "ELA 亮区集中在一处" not in text_ev:
        print("  [失败] 证据注入：brief 没有进正方输入 ⇒ 核验做了白做"
              "（渲染对了但没接上链路）")
        ok, bad = False, bad + 1
    if "已核验证据技术线索" in text3 or "已核验证据技术线索" in text1:
        print("  [失败] 证据注入：不传 brief 的老输入混进了证据段"
              " ⇒ 默认零变化被破坏（⑦-6 旧锚判据全部失真）")
        ok, bad = False, bad + 1

    # (7c) 整循环集成：evidence 行进台账且在 session_open 之后；brief 进每轮输入
    _seen_ev: list[str] = []

    async def _ev_invoke(text, round_no, attempt):
        _seen_ev.append(text)
        return _run([_submit(_hold_arg(round_no))])

    with tempfile.TemporaryDirectory() as td:
        ledger = hd.DebateLedger("probe-ev", directory=Path(td) / "debates")
        outcome = asyncio.run(hd.run_debate(
            "测试事件", pro_stance="该传闻属实", max_rounds=2,
            user_source=lambda n: f"用户第{n}轮" if n <= 2 else None,
            ledger=ledger, invoke=_ev_invoke, retries=1, sid="probe-ev",
            mode="probe",
            evidence_records=[_ev_verified, _ev_refused]))
        lines = [json.loads(ln) for ln in
                 ledger.path.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        ev = next((l for l in lines if l["type"] == "evidence"), None)
        types = [l["type"] for l in lines]
        ev_ok = (
            ev is not None
            and types.index("evidence") == 1  # 紧跟 session_open
            and len(ev.get("files") or []) == 2
            and (ev["files"][0].get("suspicion") == "中")
            and (ev["files"][1].get("status") == "refused")
            and ev["files"][0].get("detail") is None  # 只存索引级事实
        )
        if outcome != "max_rounds" or not ev_ok:
            print(f"  [失败] 证据循环：outcome={outcome} evidence行形态={ev and '有' or '无'}"
                  " ⇒ 核验事实没进台账（或进了结论级内容，违反只存事实纪律）")
            ok, bad = False, bad + 1
        if len(_seen_ev) < 2 or not all(
                "已核验证据技术线索" in t for t in _seen_ev[:2]):
            print(f"  [失败] 证据循环：{_seen_ev and len(_seen_ev)} 轮输入里"
                  " brief 没有全程在场 ⇒ 线索只在第 1 轮可见，正方第 2 轮"
                  "就当证据没核验过")
            ok, bad = False, bad + 1

    # (7d) 拒收路径不拉重依赖（快照前后比对，同 3c 泄漏判据形态）
    import sys as _sys7
    _heavy7 = ("numpy", "PIL")
    _before7 = {m for m in _heavy7 if m in _sys7.modules}
    with tempfile.TemporaryDirectory() as td:
        _nope = Path(td) / "不存在.jpg"
        _tiny = Path(td) / "太小.bin"
        _tiny.write_bytes(b"\xff\xd8\xff" + b"x" * 3)   # 6 字节 < 12
        r1_ = hd.verify_evidence_file(_nope)
        r2_ = hd.verify_evidence_file(_tiny)
    _leaked7 = {m for m in _heavy7 if m in _sys7.modules} - _before7
    if r1_.get("status") != "refused" or "读不了" not in str(r1_.get("reason")) \
            or r2_.get("status") != "refused" \
            or _leaked7:
        print(f"  [失败] 证据拒收：r1={r1_.get('status')} r2={r2_.get('status')}"
              f" 泄漏={_leaked7 or '无'} ⇒ 拒收要么没留原因，要么把 numpy/PIL"
              " 拉进了自检进程（判据自己成为故障源——缺依赖的机器自检起不来）")
        ok, bad = False, bad + 1

    if bad == 0:
        print("  [通过] 人机辩论：回放 / 抽取 / 终止 / 循环 / append-only /"
              " 隔离 / 证据注入（渲染·零变化·循环·拒收）全部离线断言绿"
              "（真机对跑另见 output/debates/）")
    return ok


def _check_experience_partition(ok: bool) -> bool:
    """⑦-13 体验应用信息分区：三桶受理 · 诱导方隔离墙 · 可选追问 · 零变化。

    背景（2026-10-19 用户拍板）：真实谣言生态的解剖结构是**非对称信息**——
    指控方手里有（或声称有）当事人没见过的材料，当事人手里有指控方不知道
    的实情。受理官把已知信息按「谁知道」分成 shared/mine/theirs 三桶；
    诱导方只拿 shared+theirs，当事人与裁判看得到 mine。

    本节守四条，全部是**行为断言**（不是 grep 源码）：
      (1) 契约：IntakeCase 有分区与追问字段、默认值零变化；主链路
          ArbitrationCase **没有**这两个字段（防有人把体验字段倒灌进
          已过审角色共用的契约）。
      (2) 🔴 隔离墙：假分区喂 build_deceiver_prompt —— shared/theirs 的
          标记在、mine 的标记**不在**。墙在拼接处（代码层），不在提示词层。
      (3) 零变化：没有分区时 build_deceiver_prompt 与旧版**逐字节相同**
          （stub 流程 / 老判据一个不破）；渲染函数返回空串。
      (4) 相位机：forge _run 注入带 followup 的受理结论 —— start 停在
          awaiting_details、reply 被相位闸拒绝、skip 开辩、give_details 的
          补充文本真的进了第二次受理输入；且**会话级**诱导方输入同样不含
          mine 标记（墙不只存在于纯函数里）。
    """
    print("⑦-13 体验应用信息分区（三桶受理 · 诱导方隔离墙 · 可选追问 · 零变化）")
    bad = 0
    import asyncio as _aio

    from legal_acps.contracts import ArbitrationCase
    from legal_acps.experience import partition as xp
    from legal_acps.experience.roles import IntakeCase
    from legal_acps.experience.session import ExperienceSession

    def _fail(msg: str) -> None:
        nonlocal ok, bad
        print(f"  [失败] {msg}")
        ok, bad = False, bad + 1

    # ── (1) 契约与零变化基座 ─────────────────────────────────────────
    base = IntakeCase(
        event="e", proposition="他收受了好处",
        fact_sheet={"event": "e", "items": [], "missing": [], "notes": ""},
        pro_position="指控成立", con_position="指控不成立",
    )
    if base.followup != "" or bool(base.partition):
        _fail("分区契约：IntakeCase 默认值不是空分区+空追问 ⇒ 零变化基座被破坏"
              "（stub 流程会莫名暂停）")
    if "partition" in ArbitrationCase.model_fields or \
            "followup" in ArbitrationCase.model_fields:
        _fail("主链路契约：ArbitrationCase 被加上了 partition/followup 字段"
              " ⇒ 体验专属字段倒灌进已过审角色共用的契约（必须只留在 IntakeCase）")
    # 假分区能通过契约校验（分区对象本身可用）
    payload = base.model_dump(mode="json")
    payload["partition"] = {
        "shared": [{"fact": "帖子公开发出", "note": ""}],
        "mine": [{"fact": "当晚在外地", "note": "登机牌"}],
        "theirs": [{"fact": "他说有聊天记录", "note": "只是声称"}],
    }
    try:
        IntakeCase.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        _fail(f"分区契约：合法分区 payload 校验不过（{type(exc).__name__}）"
              " ⇒ 受理结论带分区就会被拒")

    # ── (2) 🔴 隔离墙（纯函数级）─────────────────────────────────────
    case_part = {
        "proposition": "他收受了好处", "pro_position": "指控成立",
        "fact_sheet": {"event": "e", "items": [], "missing": [], "notes": ""},
        "partition": {
            "shared": [{"fact": "公开帖里写着指控原文·共享标记A1", "note": ""}],
            "mine": [{"fact": "我当晚在杭州出差有登机牌·独知标记B7", "note": "只有我知道"}],
            "theirs": [{"fact": "他说手里有转账聊天记录·声称标记C3", "note": "没见过材料"}],
        },
    }
    dp = xp.build_deceiver_prompt(case_part, "【指控方】开场X", 1, "标准", "分寸Y")
    if "共享标记A1" not in dp or "声称标记C3" not in dp:
        _fail("隔离墙：诱导方输入缺 shared/theirs ⇒ 指控方连自己声称的材料都"
              "拿不到，话术没法围绕信息差构建")
    if "独知标记B7" in dp:
        _fail("🔴 隔离墙：诱导方输入里出现了 mine 的内容 ⇒ 当事人自述的实情"
              "泄给了指控方（体验层最危险的失真：铁证被对方提前知道）")
    av = xp.render_accuser_view(case_part)
    if "独知标记B7" in av or "声称标记C3" not in av:
        _fail("隔离墙：render_accuser_view 形态不对（mine 泄漏或缺 theirs）")

    # ── (3) 零变化（无分区 = 旧形态逐字节相同）───────────────────────
    case_plain = {
        "proposition": "他收受了好处", "pro_position": "指控成立",
        "fact_sheet": {"event": "e", "items": [
            {"item": "有人发帖", "source_level": "来源不明", "source": "未获取到"}],
            "missing": [], "notes": ""},
    }
    import json as _json
    expected_old = (
        "═══ 本局命题 ═══\n"
        "他收受了好处\n\n"
        "═══ 可核验事实清单 ═══\n"
        f"{_json.dumps(case_plain['fact_sheet'], ensure_ascii=False, indent=2)}\n\n"
        "═══ 你方（指控方）立场 ═══\n"
        "指控成立\n\n"
        "═══ 交锋记录（最新在最后）═══\n"
        "【指控方】开场X\n\n"
        "这是第 2 轮（本局难度：标准）。请用话术构建你的发言，"
        "并调用 `debate_trap_argument` 提交：message 给用户看、"
        "traps 诚实自曝你用了哪些手法。\n"
        "本局难度的分寸：" + "分寸Y"
    )
    got_plain = xp.build_deceiver_prompt(case_plain, "【指控方】开场X", 2, "标准", "分寸Y")
    if got_plain != expected_old:
        _fail("零变化：无分区时诱导方输入与旧版**不是逐字节相同** ⇒ stub 流程 /"
              " 前端判据 / 元验证锚全部失真（分区改造的最小承诺被破坏）")
    for fn in (xp.render_accuser_view, xp.render_user_view, xp.render_full_partition):
        if fn(case_plain) != "":
            _fail(f"零变化：{fn.__name__} 对无分区 case 没有返回空串")
    # 用户视图与揭示三桶：mine 必须在场（他自己 / 裁判当然看得到）
    uv = xp.render_user_view(case_part)
    rv = xp.build_reveal_prompt(case_part, "交锋Z", "[]")
    if "独知标记B7" not in uv:
        _fail("用户视图：mine 不在 ⇒ 当事人看不到自己的实情被整理成什么样")
    if "独知标记B7" not in rv or "信息分区" not in rv:
        _fail("揭示输入：三桶全貌缺位 ⇒ 裁判看不见信息差，揭示退化为纯话术汇总"
              "（本次改造对揭示的最低要求就是『带上三桶』）")
    if "信息分区" in xp.build_reveal_prompt(case_plain, "交锋Z", "[]"):
        _fail("零变化：无分区时揭示输入混进了「信息分区」段 ⇒ 旧形态被改")
    if "信息差教学" in xp.build_reveal_prompt(case_plain, "交锋Z", "[]"):
        _fail("零变化：无分区时揭示输入混进了教学指令段 ⇒ 旧形态被改"
              "（教学话术只对有分区的局生效）")

    # ── (4) 相位机（forge _run 的会话级行为断言）─────────────────────
    # 🔴 夹具纪律：下面的会话级断言找的是 A1/B7/C3 三个标记，所以 forge 的
    # 受理结论必须带 case_part 那份**含标记**的分区——若沿用 payload 的无标记
    # 分区，theirs 断言恒红、mine 断言恒绿（空转），两边都不名实相符。
    # （fact_sheet/proposition 用 payload 的原份，保证确定性守门照常放行。）
    case_followup = _json.loads(_json.dumps(payload))
    case_followup["partition"] = _json.loads(_json.dumps(case_part["partition"]))
    case_followup["followup"] = "他手里说有什么材料？你自己知道哪些是他不知道的？"

    def _forge(prompts_log):
        async def _run(runtime, prompt, *, tool, what, **kw):
            prompts_log.append((tool, prompt))
            if tool == "case.accept":
                return _json.loads(_json.dumps(case_followup, ensure_ascii=False))
            if tool == "case.open":
                return {"worth_debating": True, "sufficiency": "勉强",
                        "gaps": [], "note": "（forge）立题"}
            if tool == "debate.trap_argument":
                return {"message": "（forge）话术发言", "traps": []}
            return {}
        return _run

    log: list[tuple[str, str]] = []
    s1 = ExperienceSession(use_stub=True)
    s1._run = _forge(log)  # type: ignore[method-assign]
    st = _aio.run(s1.start("有人在群里说我拿了回扣"))
    if st.get("phase") != "awaiting_details" or not st.get("followup") \
            or st.get("pro_rounds") != 0:
        _fail(f"相位机：受理官给了 followup 却没暂停开辩（phase={st.get('phase')}，"
              f"pro_rounds={st.get('pro_rounds')}）⇒ 可选追问名存实亡")
    # 相位闸：awaiting_details 时辩白必须被拒（补充信息 ≠ 辩白，两条路不能混）
    try:
        _aio.run(s1.reply("我现在就要辩"))
    except RuntimeError:
        pass
    else:
        _fail("相位机：awaiting_details 时 reply 没有被拒 ⇒ 用户的补充信息会被"
              "当成辩白混进交锋记录")
    # 跳过 → 开辩；会话级隔离墙：诱导方输入同样不含 mine
    st2 = _aio.run(s1.skip_details())
    deceivers = [p for t, p in log if t == "debate.trap_argument"]
    if st2.get("phase") != "debating" or st2.get("pro_rounds") != 1 or not deceivers:
        _fail(f"相位机：skip_details 没有开辩（phase={st2.get('phase')}，"
              f"pro_rounds={st2.get('pro_rounds')}）")
    elif "独知标记B7" in deceivers[0]:
        _fail("🔴 会话级隔离墙：skip 后诱导方实际收到的输入含 mine ⇒ 墙只在"
              "纯函数里成立、链路上没走通")
    elif "声称标记C3" not in deceivers[0]:
        _fail("会话级隔离墙：诱导方输入缺 theirs ⇒ 指控方的声称材料没随局入场")

    # ── (5) 信息差教学话术（板块=服务端固定文案；裁判=把概念落到原话）──
    # 哨兵全从 partition 常量现场取（单一来源：概念句改文案判据跟着走）。
    ig = xp.build_info_gap_review(case_followup)
    if not isinstance(ig, dict) or ig.get("title") != xp.INFO_GAP_TITLE:
        _fail("教学板块：build_info_gap_review 没产出以 INFO_GAP_TITLE 为题的"
              "结构化板块 ⇒ 复盘页没有信息差这堂课")
    else:
        if xp.INFO_GAP_CLAIM_NOTE not in str(ig.get("theirs_note")) or \
                xp.INFO_GAP_CARD_NOTE not in str(ig.get("mine_note")) or \
                xp.INFO_GAP_FIELD_NOTE not in str(ig.get("shared_note")):
            _fail("教学板块：概念句与 partition 常量不一致 ⇒ 前端拿到的话术"
                  "不是教学设计定稿的那份（单一来源被抄了第二份）")
        joined = str(ig)
        for mark in ("共享标记A1", "独知标记B7", "声称标记C3"):
            if mark not in joined:
                _fail(f"教学板块：三桶标记 {mark} 缺失 ⇒ 板块没把三桶对照带给用户")
    if xp.build_info_gap_review(case_plain) is not None:
        _fail("零变化：无分区时 build_info_gap_review 没返回 None"
              " ⇒ 老局复盘页会凭空长出信息差板块")
    # 揭示输入的教学指令段：有分区才出现（概念落到原话的任务交给裁判）
    rv2 = xp.build_reveal_prompt(case_followup, "交锋Z", "[]")
    if "信息差教学" not in rv2 or "举证责任" not in rv2:
        _fail("教学指令段：有分区时揭示输入缺「信息差教学」段 ⇒ 裁判不知道"
              "怎么用三桶教学（summary/advice 的话术退化回通用模板）")
    # 会话级：reveal 结果真的带上了板块（服务端算，不依赖裁判交结论）
    rev = _aio.run(s1.reveal(None))
    ig_live = rev.get("info_gap_review")
    if not isinstance(ig_live, dict) or "独知标记B7" not in str(ig_live) \
            or xp.INFO_GAP_CARD_NOTE not in str(ig_live):
        _fail("教学板块：reveal 结果没带 info_gap_review（或内容不对）"
              " ⇒ 前端复盘页拿不到信息差板块数据")

    # give_details：补充文本必须真的进第二次受理的输入，且新结论被采纳
    log2: list[tuple[str, str]] = []
    s2 = ExperienceSession(use_stub=True)
    merged_case = _json.loads(_json.dumps(payload, ensure_ascii=False))
    merged_case["proposition"] = "他收受了好处（已按补充修订）"
    # 同上：分区换成含 B7 标记那份，末尾的 mine 隔离断言才不空转。
    merged_case["partition"] = _json.loads(_json.dumps(case_part["partition"]))
    calls = {"accept": 0}

    async def _run2(runtime, prompt, *, tool, what, **kw):
        log2.append((tool, prompt))
        if tool == "case.accept":
            calls["accept"] += 1
            return dict(merged_case if calls["accept"] > 1 else case_followup)
        if tool == "case.open":
            return {"worth_debating": True, "sufficiency": "勉强",
                    "gaps": [], "note": "（forge）立题"}
        if tool == "debate.trap_argument":
            return {"message": "（forge）话术发言", "traps": []}
        return {}

    s2._run = _run2  # type: ignore[method-assign]
    _aio.run(s2.start("有人在群里说我拿了回扣"))
    st3 = _aio.run(s2.give_details("我当晚在杭州出差，登机牌还在，他说的那天我根本不在本市"))
    accepts = [p for t, p in log2 if t == "case.accept"]
    if st3.get("phase") != "debating" or len(accepts) < 2:
        _fail(f"相位机：give_details 没有触发第二次受理并开辩"
              f"（phase={st3.get('phase')}，受理 {len(accepts)} 次）")
    else:
        if "杭州出差" not in accepts[1]:
            _fail("相位机：用户的补充回答没有进第二次受理的输入 ⇒ 补充被静默丢弃")
        if st3.get("case", {}).get("proposition") != "他收受了好处（已按补充修订）":
            _fail("相位机：补充后的新受理结论没有替换旧结论 ⇒ 用户答了也白答")
        if "独知标记B7" in (p for t, p in log2 if t == "debate.trap_argument").__next__():
            _fail("🔴 会话级隔离墙：give_details 路径的诱导方输入含 mine")

    if bad == 0:
        print("  [通过] 体验信息分区：三桶契约 / 诱导方隔离墙（纯函数级+会话级）/"
              " 无分区逐字节零变化 / 相位机（暂停·闸·跳过·补充被采纳）/"
              " 信息差教学话术（固定文案板块+裁判教学指令段+会话级注入）全部绿"
              "（真机对跑另见 run/_probe_experience_partition.py）")
    return ok


def _check_debate_ruling(ok: bool) -> bool:
    """⑦-7 辩论裁定（阶段 2）：辩词全文进裁判输入 · 坏台账必炸 · 产出衔接经验库。

    阶段 2 的语义（2026-10-07 步骤 0 实证后定案）：人机辩论台账交给裁判
    （fact-judge）**直跑不登录**裁决，`fact_issue_ruling` 落盘
    `output/ruling-*.json` ⇒ 有 verdict、经裁定 ⇒ 合法进经验库。

    离线能断的三件事（真机的「裁判真的出裁定书」由
    `run/_probe_debate_ruling.py` + CLI 真跑判）：
      1. **辩词全文进输入**：漏一轮，裁判就当那轮没发生过 —— 它裁决的是
         这场辩论，不是这个事件名；
      2. **「不要 discover/delegate」框架在场**：不登录直跑的命门 ——
         裁判 instructions 是「受理→discover→派发→裁定」的严格序列，
         没有这句它会试着找辩手（不登录必然失败，白烧步数、裁定书
         只剩终止 Hook 兜底）。步骤 0 实测：带这句 iterations=3 干净走完；
      3. **坏台账必炸**：没有轮次的空场交裁判 = 让它凭空裁定，失败形态
         会伪装成「跑通但裁定书很空」—— 比当场炸危险得多。

    元验证：`run/meta_verify_debate_ruling.py`（三处退回用例）。
    """
    import tempfile

    from legal_acps import debate_ruling as dr
    from legal_acps import ruling_store as rs

    print("⑦-7 辩论裁定（辩词进输入 · 坏台账必炸 · 产出衔接经验库）")
    bad = 0

    # ── (1) 辩词全文 + 离线框架 + 事件，一个都不许缺 ──────────────────
    rounds_fixture = [
        {"round": 1, "user_argument": "用户一轮原文甲",
         "pro": {"round": 1, "stance": "hold", "confidence": 0.5,
                 "key_point": "正方一轮论点乙", "rebuts": []}},
        {"round": 2, "user_argument": "用户二轮原文丙",
         "pro": {"round": 2, "stance": "hold", "confidence": 0.4,
                 "key_point": "正方二轮论点丁", "rebuts": [1]}},
        {"round": 3, "user_argument": "用户三轮原文戊",
         "pro": {"round": 3, "stance": "concede", "confidence": 0.1,
                 "key_point": "正方三轮论点己", "rebuts": [2]}},
    ]
    text = dr.build_judge_input("待裁定事件描述XYZ", rounds_fixture)
    missing = [s for s in ("原文甲", "论点乙", "原文丙", "论点丁", "原文戊",
                           "论点己", "待裁定事件描述XYZ") if s not in text]
    if missing:
        print(f"  [失败] 辩论裁定输入：缺 {missing} ⇒ 裁判看不见完整辩论，"
              "裁决的就不是这场辩论（漏一轮它当那轮没发生过）")
        ok, bad = False, bad + 1
    if "不要 acps_discover" not in text or "不要 acps_delegate" not in text:
        print("  [失败] 辩论裁定输入：缺「辩手离线、不要派发」框架 ⇒ 裁判会"
              "按标准流程找辩手（不登录必失败，步数被烧光，裁定书只剩兜底）")
        ok, bad = False, bad + 1

    # ── (2) 坏台账必炸：五种形态都要 SystemExit，不许静默空跑 ─────────
    deaths_expected = {
        "不存在": lambda p: None,
        "坏 JSON 行": lambda p: p.write_text("{不是json}\n", encoding="utf-8"),
        "无 session_open": lambda p: p.write_text(
            json.dumps({"type": "round", "round": 1}) + "\n", encoding="utf-8"),
        "无 round（空场）": lambda p: p.write_text(
            json.dumps({"type": "session_open", "event": "e"}) + "\n",
            encoding="utf-8"),
        "open 无事件": lambda p: p.write_text(
            json.dumps({"type": "session_open", "event": ""}) + "\n",
            encoding="utf-8"),
    }
    for name, make in deaths_expected.items():
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.jsonl"
            make(p)
            died = False
            try:
                dr.load_debate(p)
            except SystemExit:
                died = True
            if not died:
                print(f"  [失败] 辩论裁定台账：{name} 没有当场炸 ⇒ 空场/坏场"
                      "交裁判 = 凭空裁定")
                ok, bad = False, bad + 1

    # 好台账必须能读（防判据把正常路径也掐死）
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "good.jsonl"
        p.write_text(
            json.dumps({"type": "session_open", "event": "事件E"},
                       ensure_ascii=False) + "\n"
            + json.dumps({"type": "round", "round": 1, "user_argument": "论点",
                          "pro": {"round": 1, "stance": "hold", "confidence": 0.5,
                                  "key_point": "K", "rebuts": []}},
                         ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            ev, rs_ = dr.load_debate(p)
            if ev != "事件E" or len(rs_) != 1:
                print(f"  [失败] 辩论裁定台账：好台账读出来不对（event={ev!r} "
                      f"rounds={len(rs_)}）⇒ 判据把正常路径掐死了")
                ok, bad = False, bad + 1
        except SystemExit as exc:
            print(f"  [失败] 辩论裁定台账：好台账被当成坏台账炸了（{exc}）"
                  "⇒ 阶段 2 整条路走不了")
            ok, bad = False, bad + 1

    # ── (3) 产出衔接经验库：同一 output/ 目录 + 集合差判据 ────────────
    #     落盘由裁判自己的 fact_issue_ruling 工具完成（写 output/ruling-*.json），
    #     这里断言的是衔接关系而不是复制那份逻辑：
    #       · debate_ruling 看的产出目录 == RulingStore 读的目录（天然进库）；
    #       · new_rulings 是集合差：空差 = 没交付（status=completed 不算数）。
    if dr.OUTPUT_DIR != rs.OUTPUT_DIR:
        print(f"  [失败] 辩论裁定产出：debate_ruling 与 RulingStore 看的不是"
              f"同一个 output/（{dr.OUTPUT_DIR} vs {rs.OUTPUT_DIR}）"
              "⇒ 裁定书落了盘却进不了经验库")
        ok, bad = False, bad + 1
    if dr.new_rulings({"ruling-a.json"}, {"ruling-a.json"}) != []:
        print("  [失败] 辩论裁定判据：没新增时 new_rulings 非空 ⇒ "
              "「唯一硬判据」被抽空")
        ok, bad = False, bad + 1
    if dr.new_rulings({"ruling-a.json"}, {"ruling-a.json", "ruling-b.json"}) \
            != ["ruling-b.json"]:
        print("  [失败] 辩论裁定判据：新增检测算错了 ⇒ 交付判定不可信")
        ok, bad = False, bad + 1
    # 落盘形态最后一环：产出名必须是 RULING_GLOB 认识的形态（ruling-*.json）
    if not rs.RULING_GLOB.startswith("ruling-"):
        print("  [失败] 辩论裁定判据：RULING_GLOB 变了形态而本节没跟上 —— "
              "产出与库的衔接断言已失效，要人工重核")
        ok, bad = False, bad + 1

    if bad == 0:
        print("  [通过] 辩论裁定：辩词全文 / 离线框架 / 坏台账必炸 / "
              "产出衔接 全部离线断言绿（真机出裁定书另见 "
              "run/_probe_debate_ruling.py）")
    return ok


def _check_law_advisor(ok: bool) -> bool:
    """⑦-8 法条库与法律咨询顾问：防幻觉的双闸门都要**真的会响**。

    law-advisor 的价值主张 = 「建议引用真实法条」——这条主张的可靠性
    全押在两道闸门上，任何一道空转，交付的就是**编造的法条**（法律场景
    里最危险的幻觉形态）：

      ① 检索闸门（`law_store`）：相关咨询召回正确条文 / 库外领域返回空
         （含同义扩展的行为断言——口语「借钱不还」必须锚到「借款」）；
      ② 校验闸门（`law.submit_advice`）：伪造法名条号 → rejected；
         真法条 + 改写摘录 → rejected；真法条 + 逐字摘录 → ok 且溯源
         **由库补全**（模型填的 URL 不可信）。

    另两条：
      · 溯源齐全：每条法文带 source_url / effective_date —— 缺了就是
        「无法核对的法律依据」；
      · 能力边界单一来源：提示词提到的法律名 ⊆ 库 meta 的清单
        （扩库后旧提示词不报错，但提到库外法律必红——宁保守勿漂移）。
    """
    print("⑦-8 法条库与法律咨询顾问（检索闸门 · 校验闸门 · 溯源 · 能力边界）")
    bad = 0
    from legal_acps import law_store as ls
    from legal_acps.agents import (  # noqa: F401  (行为断言用)
        LAW_ADVISOR_INSTRUCTIONS, lookup_articles, submit_advice,
    )
    from legal_acps.contracts import CitedArticle, LegalAdvice

    store, rep = ls.LawStore.load()
    if not rep.ok:
        print(f"  [失败] 法条库为空或不可读（{rep.reasons or '无内容'}）—— "
              "先跑 `python -m tools.import_laws`（见其 docstring）")
        return False
    print(f"  库况：{rep.kept} 条 / {len(rep.laws)} 部法律（丢弃 {rep.dropped}）")

    # ── (1) 溯源齐全：每条法文必须带官方来源与生效日期 ────────────────
    missing = [a.id for a in store
               if not (a.source_url and a.effective_date and a.content.strip())]
    if missing:
        print(f"  [失败] 法条溯源字段缺失 {len(missing)} 条（例 {missing[0]}）"
              " ⇒ 无法核对的法律依据进了建议书就是假引用")
        ok, bad = False, bad + 1

    # ── (2) 检索闸门分得开（只差一个变量：库内 / 库外领域）────────────
    hit = store.search("朋友借了我五万块钱一直不还，怎么起诉")
    if not hit or "借款" not in hit[0].article.content:
        print(f"  [失败] 法条检索：借款咨询没召回借款条文（top1="
              f"{hit[0].article.citation if hit else '空'}）⇒ 正确法条进不了"
              "建议书，建议只剩空话")
        ok, bad = False, bad + 1
    unhit = store.search("我被公司违法辞退了，劳动仲裁的时效是一年吗")
    if unhit:
        print(f"  [失败] 法条检索：库外领域（劳动仲裁）返回了 {len(unhit)} 条"
              f"（top1 {unhit[0].score:.2f}）⇒ 能力边界失守——law-advisor 会"
              "给劳动纠纷硬配上不相干的法条")
        ok, bad = False, bad + 1

    # ── (3) 同义扩展的行为断言（口语必须被锚到法言法语）──────────────
    if "借款" not in ls.expand_query("朋友借钱不还"):
        print("  [失败] 同义扩展：口语「借钱不还」没有被锚到「借款」⇒ "
              "bigram 首尾不相交的召回缺口重新打开（正确条文进不了 top-N）")
        ok, bad = False, bad + 1
    if ls.expand_query("正常查询没有口语词") != "正常查询没有口语词":
        print("  [失败] 同义扩展：不含口语词的查询被改写 ⇒ 扩展越权"
              "（只许追加锚点，不许动原文）")
        ok, bad = False, bad + 1

    # ── (4) 校验闸门：三种引用形态必须给三种结论 ────────────────────
    fake = submit_advice(LegalAdvice(
        situation_summary="x", applicable=True,
        cited_articles=[CitedArticle(
            law_name="中华人民共和国消费者权益保护法",
            article_number="第55条", excerpt="退一赔三")],
        capability_note="", reasoning="x"))
    if fake.get("status") != "rejected" or not fake.get("invalid_citations"):
        print(f"  [失败] 引用校验：库外法条（消保法 55 条）没被拦下"
              f"（status={fake.get('status')}）⇒ 模型凭记忆编的法条会"
              "原样进建议书——防幻觉闸门空转")
        ok, bad = False, bad + 1

    real = next(a for a in store if a.id == "CIVIL_CODE_667_v1")
    paraphrase = submit_advice(LegalAdvice(
        situation_summary="x", applicable=True,
        cited_articles=[CitedArticle(
            law_name=real.law_name, article_number=real.article_number,
            excerpt="借款合同就是借款人要还钱给贷款人的合同（改写版）")],
        capability_note="", reasoning="x"))
    if paraphrase.get("status") != "rejected":
        print("  [失败] 引用校验：改写过的条文摘录被放行 ⇒ 「逐字摘录」"
              "红线失守，摘录会替模型夹带私货")
        ok, bad = False, bad + 1

    verbatim = submit_advice(LegalAdvice(
        situation_summary="x", applicable=True,
        cited_articles=[CitedArticle(
            law_name=real.law_name, article_number=real.article_number,
            excerpt=real.content[:20])],
        capability_note="", reasoning="x"))
    v = (verbatim.get("verified") or [{}])[0]
    if verbatim.get("status") != "ok" or not v.get("source_url"):
        print(f"  [失败] 引用校验：逐字摘录没通过（status={verbatim.get('status')}）"
              "或溯源没由库补全 ⇒ 正常路径被掐死，或溯源还是模型自填")
        ok, bad = False, bad + 1

    # ── (5) lookup 工具：素材带原文与能力边界清单 ────────────────────
    got = lookup_articles("邻居装修噪音太大想起诉")
    if not got.get("articles") or not got.get("supported_laws"):
        print(f"  [失败] lookup 工具：素材为空或没带能力边界清单"
              f"（articles={len(got.get('articles') or [])}, "
              f"laws={len(got.get('supported_laws') or [])}）⇒ 模型没有"
              "可引用的原文，也没有能力边界的事实来源")
        ok, bad = False, bad + 1
    if got.get("articles") and "content" not in got["articles"][0]:
        print("  [失败] lookup 工具：返回的条文没有 content 字段 ⇒ "
              "模型只能凭记忆复述条文，逐字摘录无从谈起")
        ok, bad = False, bad + 1

    # ── (6) 能力边界单一来源：提示词法律名 ⊆ 库 meta 清单 ────────────
    extra = [n for n in ("民法典", "刑法", "公司法", "专利法", "增值税法",
                         "企业所得税法", "个人所得税法", "税收征收管理法")
             if n not in "".join(rep.laws)
             and n in LAW_ADVISOR_INSTRUCTIONS]
    if extra:
        print(f"  [失败] 能力边界：提示词提到了库外的法律 {extra} ⇒ "
              "两份清单已经漂移（提示词说的库没有，库有的提示词没说）")
        ok, bad = False, bad + 1

    if bad == 0:
        print("  [通过] 法条库与法律顾问：检索 / 扩展 / 校验双闸门 / 溯源 /"
              " 能力边界 全部离线断言绿（真机注册送审另见手册 #23）")
    return ok


def _check_lead_intake(ok: bool) -> bool:
    """⑦-9 线索受理官：防污染闸门、判重、队列纪律都要**真的会响**。

    lead-intake 的价值主张 = 「一条合规的待核查线索」。它最危险的两种病：

      ① **泄答案**：claim 自带官方定性 / 处罚结果 ⇒ 下游裁判的输入被污染
         （基准污染同型——gt 扩充踩过的坑，线索侧同罪）；
      ② **队列漂移**：queue/consumed 是两份 append-only 台账，pending 必须
         **读时现场算**——存一份"状态"副本必然与事实漂移。

    单一来源断言：`derive_gt_cases.FALSE_PAT is lead_store.FALSE_PAT`
    （**同一个对象**，不是"值相等"——一致性断言挡不住抄得一字不差）。
    """
    print("⑦-9 线索受理官（防污染闸门 · 判重 · 队列纪律 · 单一来源）")
    bad = 0
    import tempfile

    from legal_acps import lead_store as lstore
    from legal_acps.agents import LEAD_INTAKE_INSTRUCTIONS
    from legal_acps.contracts import LeadSubmission

    # ── (1) 类别清单单一来源：现场派生自案例库（手抄清单加类别必漏）────
    from legal_acps.case_library import CASES

    want_cats = sorted({c.category for c in CASES} | {"其他"})
    if lstore.categories() != want_cats:
        print("  [失败] 类别清单与案例库派生结果不一致 —— 出现了第二份清单")
        ok, bad = False, bad + 1

    # ── (2) 闸门分得开（只差一个变量：claim 里有没有定性/处罚/坏类别）────
    #    ⚠️ 探针自带隔离参数（tmp wit_dir）：不碰真实队列。
    with tempfile.TemporaryDirectory() as tmp:
        good_claim = "网上都在传某地自来水有怪味不能喝了，这是真的吗"
        cases = [
            ("正常线索", LeadSubmission(claim=good_claim, category="饮用水"), "ok"),
            ("自带定性", LeadSubmission(
                claim="网上都在传某地自来水有怪味，官方辟谣说这是虚假信息",
                category="饮用水"), "rejected"),
            ("自带处罚", LeadSubmission(
                claim="网上都在传某地自来水有怪味，造谣的人已被行政拘留",
                category="饮用水"), "rejected"),
            ("claim 太短", LeadSubmission(claim="真的假的", category="饮用水"), "rejected"),
            ("坏类别", LeadSubmission(claim=good_claim, category="美食"), "rejected"),
        ]
        for name, lead, want in cases:
            r = lstore.intake(lead, wit_dir=tmp)
            if r.get("status") != want:
                print(f"  [失败] 闸门「{name}」：status={r.get('status')!r}"
                      f" 期望 {want!r} ⇒ 防污染/防糊弄闸门空转")
                ok, bad = False, bad + 1
            if want == "rejected" and not (r.get("refusal_reason") and r.get("how_to_fix")):
                print(f"  [失败] 闸门「{name}」：拒绝不带 refusal_reason/how_to_fix"
                      " ⇒ 结构化返回纪律失守（工具入口失败也必须带出路）")
                ok, bad = False, bad + 1

        # 坏类别拒绝时必须附合法清单（模型据此自纠——这是它学会类别的唯一途径）
        r_bad = lstore.intake(
            LeadSubmission(claim=good_claim, category="美食"), wit_dir=tmp)
        if r_bad.get("status") == "rejected" and not r_bad.get("categories"):
            print("  [失败] 坏类别拒绝没附 categories 清单 ⇒ 模型无从自纠，只能反复撞墙")
            ok, bad = False, bad + 1

        # ── (3) 判重：同一传闻（标点/空白变体）再报 ⇒ ok + duplicate 且队列不涨 ──
        n0 = len(lstore.pending(wit_dir=tmp))
        dup = lstore.intake(
            LeadSubmission(
                claim="网上都在传 某地自来水有怪味，不能喝了，这是真的吗？",
                category="饮用水"), wit_dir=tmp)
        if dup.get("status") != "ok" or not dup.get("duplicate_of"):
            print(f"  [失败] 判重：同一传闻重复报送 status={dup.get('status')!r}"
                  f" duplicate_of={dup.get('duplicate_of')!r} ⇒ 队列会被同一事件灌满")
            ok, bad = False, bad + 1
        if len(lstore.pending(wit_dir=tmp)) != n0:
            print("  [失败] 判重：重复报送仍进了队列 ⇒ 判重闸门空转")
            ok, bad = False, bad + 1

        # ── (4) 队列纪律：consume 后 pending 现场归零，queue 一行不删 ─────
        leads = lstore.pending(wit_dir=tmp)
        lid = leads[0]["lead_id"] if leads else ""
        if not lid or not lstore.consume(lid, consumer="自检", wit_dir=tmp):
            print("  [失败] consume：登记消费失败（lead_id 不在队列里？）")
            ok, bad = False, bad + 1
        if lstore.pending(wit_dir=tmp):
            print("  [失败] pending：消费后仍非空 ⇒ pending 不是读时现场算"
                  "（或 consume 没写台账）")
            ok, bad = False, bad + 1
        q_rows = lstore._read(lstore.queue_path(tmp))
        if len(q_rows) != n0:
            print(f"  [失败] append-only：queue 行数 {len(q_rows)} ≠ 受理数 {n0}"
                  " ⇒ 台账被改写/删除过（只许 append）")
            ok, bad = False, bad + 1

    # ── (5) 单一来源：derive 工具与 lead_store 用的是**同一个正则对象** ──
    from tools.derive_gt_cases import FALSE_PAT as derived_false

    if derived_false is not lstore.FALSE_PAT:
        print("  [失败] 单一来源：derive_gt_cases.FALSE_PAT 不是 lead_store 的"
              "同一个对象 ⇒ 两把尺子已经分家（值相等不算数——抄得一字不厚也会漂移）")
        ok, bad = False, bad + 1

    # ── (6) 指令红线锚（在场性；退回形态由 meta_verify_lead_intake 证明会红）──
    for anchor in ("不做真伪判断", "不得夹带官方定性", "不得夹带处罚结果"):
        if anchor not in LEAD_INTAKE_INSTRUCTIONS:
            print(f"  [失败] 受理官 instructions 缺红线锚「{anchor}」"
                  " ⇒ 防泄答案的一半没写在提示词里")
            ok, bad = False, bad + 1

    # ── (6b) 类别清单必须进提示词（2026-10-11 真机实证逼出来的）─────────
    #    原「清单只在工具返回里」逼模型第一轮**盲猜**（实测猜「教育」被拒），
    #    重交那一轮撞上 provider 的 DSML 工具调用文本化（调用语法进了正文、
    #    从未执行）⇒ 队列零落盘。清单插值自 categories()（单一来源仍是
    #    CASES），这里断「在场」：哪个类别不在 instructions 里 = 插值静默
    #    失效（改回手写提示词 / 插值行被删就会这样）。类别清单现场派生，
    #    不在这里写死名单。
    for cat in lstore.categories():
        if f"「{cat}」" not in LEAD_INTAKE_INSTRUCTIONS:
            print(
                f"  [失败] 受理官 instructions 缺类别「{cat}」⇒ 模型第一轮"
                "只能盲猜，猜错重交的那轮没有任何保障"
                "（真机 10-11 实证：重交轮 DSML 文本化，队列零落盘）"
            )
            ok, bad = False, bad + 1

    if bad == 0:
        print("  [通过] 线索受理官：防污染闸门 / 判重 / 队列纪律 / 单一来源"
              " 全部离线断言绿（真机注册送审另见手册 #29）")
    return ok


def _check_fast_lane(ok: bool) -> bool:
    """⑦-10 快车道分级裁决（2026-10-12 · 检索优先、辩论降级为库更新器）。

    它最危险的两种病（与线索受理官同型，但病灶换了位置）：

      ① **自我确认回路**：快裁→自动入库→下次命中先例→又快裁。经验库的
         入库本来就是无条件滴灌（`fact_issue_ruling` 落盘、`load()` 全量收），
         不加资格规则这条回路**结构性存在**。切断点 = 「快裁不能自我复制」：
         fast + 无 gt 标注 ⇒ 无触发资格（`RulingCase.fast_lane_qualified`）。
      ② **模型自报车道**：lane 是代码的判定。若把 lane 交给模型声明，
         「我检索到了先例」与「其实没有」长得一模一样，判据无从下手——
         FactRuling 契约里**刻意没有** lane 字段，落盘时由 `decide_lane` 重算。

    单一来源断言（同对象）：对齐筛子 `derive._shares_sub is
    fast_lane.shares_substring`；定性正则 `fast_lane.FALSE_PAT is
    lead_store.FALSE_PAT`。
    """
    print("⑦-10 快车道分级裁决（资格规则 · 代码判道 · 落盘重算 · 单一来源）")
    bad = 0
    import tempfile
    from types import SimpleNamespace

    import legal_acps.agents as agents_mod
    from legal_acps import fast_lane as fl
    from legal_acps.contracts import FactRuling, RulingVerdict
    from legal_acps.ruling_store import RulingCase, RulingHit, RulingStore

    # ── (1) 单一来源（同对象断言——一致性挡不住抄得一字不差）──────────
    from tools import derive_gt_cases as derive

    if derive._shares_sub is not fl.shares_substring:
        print("  [失败] 对齐筛子分家：derive._shares_sub 不是 fast_lane."
              "shares_substring 的同一个对象 ⇒ gt 派生与快车道触发两把尺子"
              "各改各的，漂移从此静默")
        ok, bad = False, bad + 1
    from legal_acps.lead_store import FALSE_PAT as _FP
    if fl.FALSE_PAT is not _FP:
        print("  [失败] fast_lane.FALSE_PAT 不是 lead_store 的同一个对象"
              " ⇒ 定性窗口筛子与受理闸门分家")
        ok, bad = False, bad + 1

    # ── (2) 资格规则四形态（回路切断点的行为证明）────────────────────
    #    debate 无标注 = 有资格（历史裁定与辩论道产出，祖先链上有对抗）
    #    debate 有标注 = 有资格；fast 有标注 = 有资格（人核比「无对抗」硬）
    #    fast 无标注   = **无资格**——快裁不能自我复制，唯一红点就是它。
    def _case(lane: str, graded: bool) -> RulingCase:
        return RulingCase(
            source=f"ruling-t-{lane}-{'g' if graded else 'n'}.json",
            event="网传城东自来水厂水源被污染致多小区停水",
            verdict="不实", confidence=0.8,
            ground_truth="不实" if graded else "",
            lane=lane,
        )

    for lane, graded, want in (
        ("debate", False, True), ("debate", True, True),
        ("fast", True, True), ("fast", False, False),
    ):
        got = _case(lane, graded).fast_lane_qualified
        if got != want:
            print(f"  [失败] 资格规则（lane={lane}, graded={graded}）="
                  f"{got}，应为 {want} —— {'快裁自我复制回路被打开' if (lane == 'fast' and not graded) else '合格先例被误拦'}")
            ok, bad = False, bad + 1
    # 历史裁定缺 lane 字段 ⇒ 默认 debate ⇒ 有资格（事实陈述不是宽容默认）
    hist = RulingCase(source="ruling-hist.json", event="旧事件",
                      verdict="不实", confidence=0.7)
    if hist.lane != "debate" or not hist.fast_lane_qualified:
        print("  [失败] 历史裁定（无 lane 字段）未按辩论道处理 ⇒ 75 份存量"
              "裁定会被整体逐出快车道资格")
        ok, bad = False, bad + 1

    # ── (3) decide_lane 分得开（临时经验库隔离；fact_hits=[] 隔离事实库）──
    #    只差一个变量的输入 ⇒ 不同结论：同事件合格先例→fast；
    #    同事件 fast 无资格先例→debate（同一事件、只差资格，必须分得开）；
    #    不同事件合格先例→debate。
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out"
        out.mkdir()
        ev = "网传城东自来水厂水源被污染致多小区停水，住户连夜抢购瓶装水"

        def _wr(name: str, event: str, lane: str | None) -> None:
            obj = {"event": event, "verdict": "不实", "confidence": 0.8,
                   "basis": ["官方通报"], "fallacies": []}
            if lane:
                obj["lane"] = lane
            (out / name).write_text(json.dumps(obj, ensure_ascii=False),
                                    encoding="utf-8")

        # (3a) 同事件·辩论道先例 ⇒ fast
        _wr("ruling-flA.json", ev, "debate")
        got_a = fl.decide_lane(ev, fact_hits=[], ruling_root=out)
        if got_a["lane"] != "fast" or not got_a["qualified_precedents"]:
            print(f"  [失败] 同事件合格先例未触发快车道：lane={got_a['lane']}"
                  f"（先例命中 {len(got_a['qualified_precedents'])} 条）"
                  " ⇒ 「辩完入库供复用」空转")
            ok, bad = False, bad + 1
        # (3b) 同事件·快车道先例（无资格）⇒ 必须拦下（回路切断端到端）
        _wr("ruling-flB.json", ev, "fast")
        got_b = fl.decide_lane(ev, fact_hits=[], ruling_root=out)
        # flB 无资格但 flA 合格仍在库 ⇒ 仍 fast；真正的切断测试要 flA 不在。
        out2 = out.parent / "out2"
        out2.mkdir()
        def _wr2(name: str, event: str, lane: str | None) -> None:
            obj = {"event": event, "verdict": "不实", "confidence": 0.8,
                   "basis": ["官方通报"], "fallacies": []}
            if lane:
                obj["lane"] = lane
            (out2 / name).write_text(json.dumps(obj, ensure_ascii=False),
                                     encoding="utf-8")
        _wr2("ruling-flB.json", ev, "fast")
        got_b2 = fl.decide_lane(ev, fact_hits=[], ruling_root=out2)
        if got_b2["lane"] != "debate" or got_b2["qualified_precedents"]:
            print(f"  [失败] 快车道自己的裁定触发了下一次快车道：lane={got_b2['lane']}"
                  " ⇒ 自我确认回路没有被切断（fast+无标注必须无触发资格）")
            ok, bad = False, bad + 1
        # (3c) 不同事件·合格先例 ⇒ debate（对齐是事件级，相似不算）
        out3 = out.parent / "out3"
        out3.mkdir()
        (out3 / "ruling-flC.json").write_text(json.dumps({
            "event": "网传某市地铁三号线施工挖断主水管全市停水24小时",
            "verdict": "不实", "confidence": 0.7, "basis": ["官方通报"],
            "fallacies": [], "lane": "debate"}, ensure_ascii=False), encoding="utf-8")
        got_c = fl.decide_lane(ev, fact_hits=[], ruling_root=out3)
        if got_c["lane"] != "debate":
            print(f"  [失败] 不同事件触发了快车道：lane={got_c['lane']}"
                  " ⇒ 事件级对齐失守（相似≠同一事件，误开快车道比漏开危险）")
            ok, bad = False, bad + 1
        # (3d) 检索故障 ⇒ lane=debate + lane_error 如实记（旁路不挡主流程）
        class _Boom:
            def __iter__(self):
                raise RuntimeError("探针注入的检索故障")

        got_d = fl.decide_lane(ev, fact_hits=_Boom(), ruling_root=out)
        if got_d["lane"] != "debate" or not got_d["lane_error"]:
            print(f"  [失败] 检索故障未保守回辩论道：lane={got_d['lane']}"
                  f" lane_error={got_d['lane_error']!r} ⇒ 快车道宁可漏开不可误开")
            ok, bad = False, bad + 1

    # ── (4) issue_ruling 落盘重算打标（注入式：不信自报，也不信库巧合）──
    #    塞一个假 decide_lane 返回 fast ⇒ 落盘/返回标必须跟随注入值。
    #    旧版「落盘 == 现场重算」的一致性断言依赖真实库内容——探针事件
    #    在库里判什么断言就跟着什么，偶然绿与必然绿分不开；注入 fast
    #    才能证明打标真的在走重算这条路（两边不是恰好都给了 debate）。
    #    ⚠️ 2026-10-13 起 issue_ruling 对 UNDECIDABLE 裁定会自动入复研判
    #       队列（B 出路）⇒ 本段必须把 leads_dir 也指到临时目录——
    #       否则探针事件会写进真实 .wit/leads/，被每日研判当成真活跑。
    with tempfile.TemporaryDirectory() as td:
        old_dir = agents_mod.OUTPUT_DIR
        agents_mod.OUTPUT_DIR = Path(td)
        from legal_acps import lead_store as _ls_mod

        _orig_leads_dir_104 = _ls_mod.leads_dir
        _ls_mod.leads_dir = lambda wit_dir=None: Path(td) / "leads"  # type: ignore[assignment]
        try:
            probe_ev = "网传城西农贸市场检出问题猪肉已致多人住院就诊"
            ruling = FactRuling(
                event=probe_ev, verdict=RulingVerdict.UNDECIDABLE,
                confidence=0.5, reasoning="⑦-10 判据探针：验证落盘重算打标",
            )
            _orig = fl.decide_lane

            def _fake_lane(*_a, **_k):
                return {"lane": "fast", "lane_error": "",
                        "aligned_official": None, "qualified_precedents": []}

            fl.decide_lane = _fake_lane
            try:
                ret = agents_mod.issue_ruling(ruling)
                saved = json.loads(Path(ret["saved_to"]).read_text(encoding="utf-8"))
                if ret.get("lane") != "fast" or saved.get("lane") != "fast":
                    print(f"  [失败] 落盘/返回缺 lane 标或未跟随重算注入值："
                          f"ret={ret.get('lane')!r} saved={saved.get('lane')!r}"
                          " ⇒ 打标走了别的路径（漂移）或根本没打")
                    ok, bad = False, bad + 1
            finally:
                fl.decide_lane = _orig
            # 失败路径：重算炸了 ⇒ lane=debate + lane_error 记 relane（不挡落盘）
            def _boom(*_a, **_k):
                raise RuntimeError("探针注入的重算故障")

            fl.decide_lane = _boom
            try:
                ret2 = agents_mod.issue_ruling(ruling)
                saved2 = json.loads(Path(ret2["saved_to"]).read_text(encoding="utf-8"))
                if saved2.get("lane") != "debate" or "relane" not in str(saved2.get("lane_error")):
                    print(f"  [失败] 重算故障未保守打 debate：lane={saved2.get('lane')}"
                          f" lane_error={saved2.get('lane_error')!r}")
                    ok, bad = False, bad + 1
            finally:
                fl.decide_lane = _orig
        finally:
            agents_mod.OUTPUT_DIR = old_dir
            _ls_mod.leads_dir = _orig_leads_dir_104  # type: ignore[assignment]

    # ── (5) load 解析 lane（缺字段=历史辩论道；不认识的值也归 debate）────
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        (out / "ruling-x1.json").write_text(json.dumps({
            "event": "事件甲", "verdict": "不实", "confidence": 0.8,
            "lane": "fast"}, ensure_ascii=False), encoding="utf-8")
        (out / "ruling-x2.json").write_text(json.dumps({
            "event": "事件乙", "verdict": "不实", "confidence": 0.8},
            ensure_ascii=False), encoding="utf-8")
        (out / "ruling-x3.json").write_text(json.dumps({
            "event": "事件丙", "verdict": "不实", "confidence": 0.8,
            "lane": "不认识的值"}, ensure_ascii=False), encoding="utf-8")
        store, _rep = RulingStore.load(out)
        lanes = {c.event: c.lane for c in store}
        if (lanes.get("事件甲") != "fast" or lanes.get("事件乙") != "debate"
                or lanes.get("事件丙") != "debate"):
            print(f"  [失败] lane 解析口径错：{lanes} ⇒（应为 甲=fast、乙=debate"
                  " 缺字段、丙=debate 不认识值保守归辩论道）")
            ok, bad = False, bad + 1

    # ── (6) 先例渲染：无资格先例必须打标且排在合格先例之后 ────────────
    ok_hit = RulingHit(
        case=RulingCase(source="ruling-ok.json", event="合格先例事件",
                        verdict="不实", confidence=0.9, ground_truth="不实",
                        lane="debate"),
        match=SimpleNamespace(score=9.0, matched=("片段甲",)),
    )
    fast_hit = RulingHit(
        case=RulingCase(source="ruling-fast.json", event="无资格快裁事件",
                        verdict="不实", confidence=0.9, lane="fast"),
        match=SimpleNamespace(score=8.0, matched=("片段乙",)),
    )
    text = RulingStore.render([fast_hit, ok_hit])  # 故意把 fast 排前
    if "无对抗快裁" not in text:
        print("  [失败] 先例渲染缺「无对抗快裁」打标 ⇒ 模型看不见资格差异，"
              "「进池但降权」的降权是句空话")
        ok, bad = False, bad + 1
    elif text.index("合格先例事件") > text.index("无资格快裁事件"):
        print("  [失败] 合格先例没有排在无资格先例之前 ⇒ 降权只打了标没让位")
        ok, bad = False, bad + 1

    # ── (7) instructions 车道锚在场（退回形态由 meta_verify_fast_lane 证红）──
    from legal_acps.agents import FACT_JUDGE_INSTRUCTIONS as FJI
    for anchor in (
        "跳过下面第 2–4 步",            # fast 分支：命中即裁，不烧辩手预算
        "不得以「先例相似」为借口跳过辩论",  # debate 红线：相似不是免辩理由
        "仅辩论道",                     # 2/3/4 步的结构标注
        "你无权改判",                   # 车道是代码的判定，不是模型的声明
        "本案此前已研判过、当时证据不足",   # fast 命中「无法判定」先例 ≠ 可复用
                                       # 结论（A 出路 2026-10-13：负缓存语义）
    ):
        if anchor not in FJI:
            print(f"  [失败] 裁判 instructions 缺车道锚「{anchor}」"
                  " ⇒ 分级流程的执行半边没写在提示词里")
            ok, bad = False, bad + 1

    # ── (7b) A 出路（2026-10-13）：无法判定 ⇒ 主控向用户要材料 ──────────
    #    「查不了」不是终点——材料是「下次能查」的唯一通路；这一句不写在
    #    主控提示词里，模型会把弃权当成完事，用户永远不知道还能补材料。
    from legal_acps.agents import LEADER_INSTRUCTIONS as LI
    if "拿到新材料后会重新研判" not in LI:
        print("  [失败] 主控 instructions 缺「无法判定要材料」锚"
              "（拿到新材料后会重新研判）⇒ A 出路的执行半边没写在提示词里")
        ok, bad = False, bad + 1

    if bad == 0:
        print("  [通过] 快车道分级：资格规则四形态 / 决策分得开 / 落盘重算打标"
              " / 单一来源同对象 / 渲染降权打标 全部离线断言绿")
    return ok


def _check_lead_recon(ok: bool) -> bool:
    """⑦-11 线索复研判（B2 出路 · 2026-10-13）——「查不了」不是终点。

    它要防的两种病（与受理官同族，但位置换了）：

      ① **死数据**：受理官的队列只写不读（「队列由每日研判链路取用」是
         提示词里承诺过的事），复研判请求若也没有消费者 = 又一摊死数据。
         判据 = next_work 真的能从两个来源选出活、消费真的能闭环。
      ② **打水漂**：库没长肥（指纹没变）就重查——同样的空库只会再弃权
         一次。判据 = eligible 必须比指纹（分得开），且「pending 非空但
         全不合格」时 next_work 必须返回 None（不是把不合格当可跑）。

    单一来源：闸门 = `lead_store.validate_lead`（同一把尺子，行为断言：
    受理官拒的文本复研判也拒）；判重 = `lead_store.lead_id_of`（同一把
    归一化尺子）；台账纪律 = append-only + 待处理读时现场算。
    """
    print("⑦-11 线索复研判（闸门同尺 · 指纹分得开 · 消费闭环 · 旁路不挡主流程）")
    bad = 0
    import tempfile
    from types import SimpleNamespace

    import legal_acps.agents as agents_mod
    from legal_acps import lead_recon, lead_store
    from legal_acps.contracts import FactRuling, LeadSubmission, RulingVerdict

    def _fail(msg: str) -> None:
        # 🔴 `ok` 也必须 nonlocal：只声明 `bad` 的话 `ok = False` 赋给
        # 本函数的局部变量，失败传不出去 —— 打了 [失败] 却照样全绿
        # （静默给安全结论是最危险缺陷，⑦-11 首跑实证）。
        nonlocal ok, bad
        print(f"  [失败] {msg}")
        ok = False
        bad += 1

    # ── (1) 闸门同尺（行为断言：受理官拒的，复研判也拒）────────────
    dirty = "网传某地发生连环盗窃案，警方通报系谣言切勿转发"
    short = "很短的传闻"
    r_dirty = lead_recon.queue_retry(dirty, "ruling-gate.json", epoch="e1")
    r_short = lead_recon.queue_retry(short, "ruling-gate.json", epoch="e1")
    if r_dirty.get("status") != "rejected":
        _fail(f"含定性词的事件未过闸门：{r_dirty} ⇒ 复研判与受理官不是"
              "同一把尺子（答案被喂给下游裁判的口子在这里重开）")
    if r_short.get("status") != "rejected":
        _fail(f"超短事件未过闸门：{r_short}")

    with tempfile.TemporaryDirectory() as td:
        wit = Path(td)
        ev = "网传某市地铁二号线列车车门夹住乘客背包致全线晚点四十分钟"

        # ── (2) 写侧：入队 / 判重 / 多 epoch 只认最新（读时现场算）────
        q1 = lead_recon.queue_retry(ev, "ruling-a.json", wit_dir=wit, epoch="e1")
        q2 = lead_recon.queue_retry(ev, "ruling-b.json", wit_dir=wit, epoch="e1")
        if q1.get("status") != "queued" or q2.get("status") != "duplicate":
            _fail(f"写侧形态错：首次={q1.get('status')} 重复={q2.get('status')}"
                  "（同 (lead_id, epoch) 待处理内不许重复入队）")
        pend = lead_recon.pending_retries(wit_dir=wit)
        if len(pend) != 1:
            _fail(f"待处理应有 1 条，实际 {len(pend)}")
        lead_recon.queue_retry(ev, "ruling-c.json", wit_dir=wit, epoch="e2")
        pend2 = lead_recon.pending_retries(wit_dir=wit)
        if len(pend2) != 1 or pend2[0].get("store_epoch") != "e2":
            _fail(f"同一事件多 epoch 待处理应只认最新：{len(pend2)} 条、"
                  f"epoch={pend2[0].get('store_epoch') if pend2 else None}"
                  "（每次库长肥只值得查一次）")

        # ── (3) 指纹分得开：epoch 相同 = 不合格；变了 = 合格 ──────────
        if lead_recon.eligible_retries(wit_dir=wit, epoch="e2"):
            _fail("指纹没变（e2==e2）却判合格 ⇒ 拿同一个空库再弃权一次，"
                  "纯烧预算")
        if not lead_recon.eligible_retries(wit_dir=wit, epoch="e3"):
            _fail("指纹变了（e2→e3）却判不合格 ⇒ 「等库长肥」的闭环断了")
        # 「pending 非空但全不合格」必须是没有活，不是把不合格当可跑
        if lead_recon.next_work(wit_dir=wit, epoch="e2") is not None:
            _fail("全部重查都不合格时 next_work 应返回 None"
                  "（没有活是常态，不许拿不合格的活凑数）")

        # ── (4) 消费闭环：查一次终态；没查过不许被标成查过 ──────────
        work = lead_recon.next_work(wit_dir=wit, epoch="e3")
        if not work or work.get("kind") != "retry" or not work.get("label", "").startswith("lead:"):
            _fail(f"next_work 形态错：{work}（应为 retry、label 前缀 lead:）")
        if lead_recon.consume_work({"kind": "retry", "id": "lead-nope:e9"},
                                   wit_dir=wit):
            _fail("消费了台账里不存在的请求 ⇒ 凭空编收尾记录")
        if not lead_recon.consume_work(work, wit_dir=wit):
            _fail("消费失败（应成功）")
        if lead_recon.pending_retries(wit_dir=wit):
            _fail("消费后仍有待处理 ⇒ 重查死循环（每次库长肥查一次的"
                  "语义被打破）")

        # ── (5) 来信优先（受理官队列终于有了消费者）+ idle 常态 ──────
        clean = "网传某小区物业连夜撤走全体保安，业主称物业费纠纷无人处理"
        cat = next(iter(set(lead_store.categories()) - {"其他"}), "其他")
        intake_ret = lead_store.intake(
            LeadSubmission(claim=clean, category=cat), wit_dir=wit)
        if intake_ret.get("status") != "ok":
            _fail(f"来信入库失败：{intake_ret}（测试前置，非被测对象）")
        lead_recon.queue_retry(ev, "ruling-d.json", wit_dir=wit, epoch="e4")
        w = lead_recon.next_work(wit_dir=wit, epoch="e4")
        if not w or w.get("kind") != "lead":
            _fail(f"群众来信与重查并存时应优先来信：{w}"
                  "（用户在等，重查等得起）")
        lead_recon.consume_work(w, wit_dir=wit)
        # 重查合格的前提是**库自入队以来变过**：入队时是 e4 形态，跑
        # next_work 时库已长肥到 e5 ⇒ e4 记录合格。🔴 不能用同一个
        # epoch 问——「没变 ⇒ 不合格」正是指纹闸门要证的行为（旧探针
        # 拿同一个 e4 还能拿到 retry，靠的是已修复的旧 epoch 复活缺陷）。
        w2 = lead_recon.next_work(wit_dir=wit, epoch="e5")
        if not w2 or w2.get("kind") != "retry":
            _fail(f"来信消费后应轮到合格重查：{w2}")
        lead_recon.consume_work(w2, wit_dir=wit)
        if lead_recon.next_work(wit_dir=wit, epoch="e5") is not None:
            _fail("两个来源都空了 next_work 应为 None（空集合纪律："
                  "没有活不许编一个出来）")

    # ── (6) issue_ruling 集成：无法判定⇒入队 / 决定性⇒不入 / 旁路不挡 ──
    with tempfile.TemporaryDirectory() as td:
        old_dir = agents_mod.OUTPUT_DIR
        agents_mod.OUTPUT_DIR = Path(td) / "output"
        _orig_leads_dir = lead_store.leads_dir
        lead_store.leads_dir = lambda wit_dir=None: Path(td) / "leads"  # type: ignore[assignment]
        try:
            undec_ev = "网传城西客运站凌晨发生持械斗殴致两人重伤"
            r1 = agents_mod.issue_ruling(FactRuling(
                event=undec_ev, verdict=RulingVerdict.UNDECIDABLE,
                confidence=0.4, reasoning="⑦-11 探针：证据不足"))
            rq1 = (r1.get("requeue") or {})
            retries_file = Path(td) / "leads" / "retries.jsonl"
            rows = [json.loads(ln) for ln in
                    retries_file.read_text(encoding="utf-8").splitlines()
                    if ln.strip()] if retries_file.exists() else []
            if rq1.get("status") != "queued" or len(rows) != 1:
                _fail(f"无法判定裁定未入复研判队列：requeue={rq1}、"
                      f"队列 {len(rows)} 条（B 出路写侧没接上）")
            elif rows[0].get("ruling_source") != Path(r1["saved_to"]).name:
                _fail(f"复研判记录的 ruling_source={rows[0].get('ruling_source')!r}"
                      " 与裁定书文件名对不上（溯源字段不许含糊）")
            # 决定性裁定不入队（分得开）
            r2 = agents_mod.issue_ruling(FactRuling(
                event="网传某地凌晨三点自来水变成淡蓝色",
                verdict=RulingVerdict.FALSE, confidence=0.9,
                reasoning="⑦-11 探针：决定性"))
            if (r2.get("requeue") or {}).get("status") != "skipped":
                _fail(f"决定性裁定也入了队：{r2.get('requeue')}"
                      "（重查是给「查不了」的，不是给「查清了」的）")
            # 旁路失败不挡裁定交付
            _orig_qr = lead_recon.queue_retry

            def _boom(*_a, **_k):
                raise RuntimeError("探针注入的入队故障")

            lead_recon.queue_retry = _boom  # type: ignore[assignment]
            try:
                r3 = agents_mod.issue_ruling(FactRuling(
                    event="网传某高中要求家长轮流到校值守午休",
                    verdict=RulingVerdict.UNDECIDABLE,
                    confidence=0.4, reasoning="⑦-11 探针：旁路故障"))
            finally:
                lead_recon.queue_retry = _orig_qr  # type: ignore[assignment]
            if not Path(r3["saved_to"]).exists() or (r3.get("requeue") or {}).get("status") != "error":
                _fail(f"入队故障挡了裁定交付或未如实记：saved={r3.get('saved_to')}"
                      f" requeue={r3.get('requeue')}（旁路记录不许顶掉主流程）")
        finally:
            agents_mod.OUTPUT_DIR = old_dir
            lead_store.leads_dir = _orig_leads_dir  # type: ignore[assignment]

    # ── (7) examine_one：真跑消费闭环 / 闸门拦下不消费 / idle 常态 ────
    with tempfile.TemporaryDirectory() as td:
        wit = Path(td) / "wit"
        out_dir = Path(td) / "out"
        out_dir.mkdir()
        ev = "网传某公园湖面一夜之间出现大量死鱼，周边居民不敢开窗"
        lead_recon.queue_retry(ev, "ruling-e.json", wit_dir=wit, epoch="e-old")
        from legal_acps import run_ledger
        from legal_acps.case_round import Check

        def _stub_spawn(text, *, label, timeout_s, cwd):
            # 真实子进程会做的两件事：落一份产物 + 记一轮台账（开+收）
            (out_dir / "verdict-fakelead.json").write_text("{}", encoding="utf-8")
            rec = run_ledger.open_run(label=label, role="leader", wit_dir=str(wit))
            run_ledger.close_run(rec, status="completed", iterations=3, wit_dir=str(wit))
            return 0, "[stub] 假子进程跑完", False, 0.1

        res = lead_recon.examine_one(
            wit_dir=str(wit), timeout_s=30, spawn=_stub_spawn,
            port_check=lambda: Check("ports", True, "探针注入：跳过"),
            artifacts_dir=out_dir, epoch="e-new",
        )
        if res.get("outcome") != "ran" or res.get("consumed") is not True:
            _fail(f"研判轮应跑完并消费：outcome={res.get('outcome')}"
                  f" consumed={res.get('consumed')} {res.get('skip_reason')}")
        elif lead_recon.pending_retries(wit_dir=str(wit)):
            _fail("研判轮跑完消费后仍有待处理")
        elif res.get("exit_code") != 0:
            _fail(f"注入形态下体检应全绿：exit={res.get('exit_code')}、"
                  f"checks={res.get('checks')}")
        # 闸门拦下 ⇒ 不跑也不消费（跳过 ≠ 消费）
        lead_recon.queue_retry(ev, "ruling-f.json", wit_dir=str(wit), epoch="e-new2")
        from legal_acps import case_round as cr_mod
        _orig_busy = cr_mod.busy_gate
        cr_mod.busy_gate = lambda runs: SimpleNamespace(
            name="busy", ok=False, detail="探针注入：有一轮在跑",
            to_json=lambda: {"name": "busy", "ok": False, "detail": "探针注入"},
        )
        try:
            res2 = lead_recon.examine_one(
                wit_dir=str(wit), timeout_s=30, spawn=_stub_spawn,
                port_check=lambda: Check("ports", True, "探针注入：跳过"),
                artifacts_dir=out_dir, epoch="e-changed",
            )
        finally:
            cr_mod.busy_gate = _orig_busy
        if (res2.get("outcome") != "skipped" or res2.get("exit_code") != 2
                or not lead_recon.pending_retries(wit_dir=str(wit))):
            _fail(f"闸门拦下应跳过且不消费：outcome={res2.get('outcome')}"
                  f" exit={res2.get('exit_code')}（没跑过的工作项不许被标成查过）")
        # idle = 常态 = 退出码 0（不是失败、也不是跳过）。⚠️ 要制造
        # 「没有活」，epoch 必须与仍待处理的 e-new2 记录**相同**（库没
        # 长肥 ⇒ 不合格 ⇒ 没活）；用别的 epoch 会让 e-new2 变合格 ⇒
        # 真跑一轮，而 stub 第二次写的是同名产物 ⇒ 体检如实报红。
        res3 = lead_recon.examine_one(
            wit_dir=str(wit), timeout_s=30, spawn=_stub_spawn,
            port_check=lambda: Check("ports", True, "探针注入：跳过"),
            artifacts_dir=out_dir, epoch="e-new2",
        )
        if res3.get("outcome") != "idle" or res3.get("exit_code") != 0:
            _fail(f"没有活应是 idle+0：outcome={res3.get('outcome')}"
                  f" exit={res3.get('exit_code')}（idle 是常态，不许当失败）")

    # ── (8) 指纹真的会变（分得开的物理基础）────────────────────────
    with tempfile.TemporaryDirectory() as td:
        fr = Path(td)
        ep1 = lead_recon.store_epoch(facts_root=fr)
        (fr / "raw-probe.jsonl").write_text("{}", encoding="utf-8")
        ep2 = lead_recon.store_epoch(facts_root=fr)
        if ep1 == ep2 or lead_recon.store_epoch(facts_root=fr) != ep2:
            _fail(f"事实库指纹不随库变化：{ep1} → {ep2}"
                  "（「等库长肥」失去判据）")

    if bad == 0:
        print("  [通过] 线索复研判：闸门同尺 / 指纹分得开 / 消费闭环"
              " / 写侧集成 / 研判轮行为 全部离线断言绿")
    return ok


def _check_negative_controls(ok: bool) -> bool:
    """⑦-12 系统性负控集（喂无害热点，看会不会硬造问题；#102 拍板 2026-10-13）。

    与 gt 校准（假阴方向）互补，本节测**假阳方向**：
      (1) 结构纪律——id/text 唯一、维度现场派生且每维度 ≥2 条、≥20 条起建
          （缩水要人拍板，不许悄悄缩）；
      (2) 闸门不误拒——全部无害文本干净通过 `validate_lead`（闸门防的是
          污染，不是无害文本；混入非负控文本在这里也会红，两个方向都抓）；
      (3) 判道不误触——**真事实库** + 空经验库 ⇒ 全部落辩论道：无害热点
          被对齐到证伪材料 = 把一件真事错配到另一件事的官方否定上；
      (4) 正对照（防空转）——从事实库**现场派生**一条必对齐事件断言它
          触发 fast：「全 debate」必须与「对齐筛子坏了」分得开。
    """
    print("⑦-12 系统性负控集（无害热点不误触 · 闸门不误拒 · 正对照防空转）")
    bad = 0
    import tempfile
    from types import SimpleNamespace

    from legal_acps import fact_store as fs_mod
    from legal_acps import fast_lane as fl
    from legal_acps import lead_store, negative_controls as ncs
    from legal_acps.contracts import LeadSubmission
    from legal_acps.lead_store import FALSE_PAT

    def _fail(msg: str) -> None:
        # 🔴 ok 必须一起 nonlocal（⑦-11 首跑实证：只声明 bad 的话失败传不出去）
        nonlocal ok, bad
        print(f"  [失败] {msg}")
        ok = False
        bad += 1

    # ── (1) 结构纪律（扩集 = 加数据行，判据零改动）──────────────────
    ids = [c.id for c in ncs.CONTROLS]
    dup_ids = {i for i in ids if ids.count(i) > 1}
    if dup_ids:
        _fail(f"负控 id 重复：{sorted(dup_ids)}（台账/判据靠 id 指名道姓）")
    texts = [c.text for c in ncs.CONTROLS]
    dup_texts = {t[:20] for t in texts if texts.count(t) > 1}
    if dup_texts:
        _fail(f"负控文本重复：{sorted(dup_texts)}（同话题抄两遍会把覆盖面虚报成两倍）")
    if len(ncs.CONTROLS) < 20:
        _fail(f"负控集仅 {len(ncs.CONTROLS)} 条（用户拍板约 20 条起建、之后会"
              "扩大——缩水必须是人拍的板，不许悄悄缩）")
    thin = {d: len(v) for d, v in ncs.by_dimension().items() if len(v) < 2}
    if thin:
        _fail(f"维度覆盖薄于 2 条：{thin}（单条不成系统性证据；维度清单现场"
              "派生自数据——加维度即自动进本断言）")
    missing = [c.id for c in ncs.CONTROLS if not (c.why_harmless or "").strip()]
    if missing:
        _fail(f"why_harmless 缺失：{missing}（无害依据要逐条人核，不许整批抄入）")

    # ── (2) 闸门不误拒：全部干净通过受理闸门 ────────────────────────
    for c in ncs.CONTROLS:
        reasons = lead_store.validate_lead(
            LeadSubmission(claim=c.text, category="其他"))
        if reasons:
            _fail(f"{c.id} 未过受理闸门：{reasons}——负控集要求无害文本零阻力"
                  "通过：要么闸门误拦（防的是污染不是无害文本），要么数据混入了"
                  "非负控文本，两个方向都要查")

    # ── (3) 判道不误触：真事实库 + 空经验库 ⇒ 全部辩论道 ─────────────
    with tempfile.TemporaryDirectory() as td:
        empty_root = Path(td)
        for c in ncs.CONTROLS:
            info = fl.decide_lane(c.text, ruling_root=empty_root)
            if info["lane"] != "debate":
                titles = [str(a.get("title", ""))[:30]
                          for a in info["aligned_official"]]
                _fail(f"{c.id} 误触快车道：lane={info['lane']}、官方对齐 {titles}"
                      "——无害热点被错配到证伪材料（近邻域错配正是负控要抓的）")

    # ── (4) 正对照（防空转）：定性窗口内文本必对齐、必触发 fast ───────
    #    「全 debate」与「对齐筛子恒否」在 (3) 里长得一模一样——没有这条，
    #    ⑦-12 可以在对齐彻底坏掉时照常全绿。
    store_f, _rep = fs_mod.FactStore.load()
    debunked = [f for f in store_f if FALSE_PAT.search(getattr(f, "body", "") or "")]
    if not debunked:
        _fail("事实库里竟没有一条含定性词的材料——正对照派生不出，(3) 的"
              "全 debate 无法与「库被清空」区分")
    else:
        made = False
        with tempfile.TemporaryDirectory() as td2:
            for f in debunked:
                if made:
                    break
                for m in FALSE_PAT.finditer(getattr(f, "body", "") or ""):
                    if made:
                        break
                    for seg in (
                        (getattr(f, "body", "") or "")[max(0, m.start() - 24):m.start()],
                        (getattr(f, "body", "") or "")[m.end():m.end() + 24],
                    ):
                        seg = seg.strip()
                        if len(seg) < 4:
                            continue
                        hit = SimpleNamespace(fact=f, score=9.9)
                        info = fl.decide_lane(
                            seg, fact_hits=[hit], ruling_root=Path(td2))
                        if info["lane"] == "fast" and info["aligned_official"]:
                            made = True
                            break
        if not made:
            _fail("正对照派生失败：定性窗口内的文本竟对不上 official_confirms"
                  "——(3) 的全 debate 是空转的（对齐筛子恒否同样全绿）")

    if bad == 0:
        print(f"  [通过] 系统性负控集：{len(ncs.CONTROLS)} 条无害热点"
              f"（{len(ncs.by_dimension())} 维度）全部落辩论道、闸门零误拦；"
              "正对照从事实库现场派生命中 ⇒ 非空转"
              "（退回形态由 meta_verify_negative_controls 证明会红）")
    return ok


def section(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def _unit_sleep_delay(body: str) -> int:
    """单元里用来**错开启动**的 `ExecStartPre=/bin/sleep N`；没写就是 0。

    ⚠️ 提出来是因为有两处要问同一个问题（3b ④ 判"延迟两两不同 / 小于启动超时"，
    3d 判"偏移闸门的门槛大于最大错开延迟"）。各写一份正则，加角色时必然漏一处，
    而漏掉的那处**不会报错** —— 它只是继续用一个过期的数字做判断。
    """
    m = re.search(r"^ExecStartPre=/bin/sleep\s+(\d+)", body, re.M)
    return int(m.group(1)) if m else 0


def _unit_line(body: str, pattern: str) -> str | None:
    """在 systemd 单元文本里按**行首锚定**找一条指令，返回那一行（没有则 `None`）。

    ⚠️ 为什么必须行首锚定，而不是 `"Type=oneshot" in body` 这种子串匹配：
       单元文件里到处是 `#` 注释，而**注释里会提到指令名**。本项目的
       `acps-case-round.service` 注释里就写着「刻意不写 `[Install]`」——
       用子串匹配的话，这句话会把"没有 `[Install]`"这条判据**当场判成失败**。
       （2026-10-05 第一版就是这样被自己的注释绊倒的，`check_local` 3d 当场报红。）
       ⇒ 要锚的是**结构位置**（行首、所属段），不是"文件里出现过这个词"。
       这正是「判据不许退化成 grep 源码」的一个具体形态。

    返回整行而不是 `bool`：失败信息能带上原文，读的人不用再打开文件对一遍。
    """
    m = re.search(pattern, body, re.M)
    return m.group(0) if m else None


def _script_line(body: str, needle: str) -> str | None:
    """在 shell 脚本里找**非注释行**中含有 `needle` 的第一行。理由同 `_unit_line`。

    shell 的注释以 `#` 开头（允许前导空白）。不做这个排除的话，
    一句「这里没写 `acps-case-round.timer`」的注释就能让"名字对不上"的判据变绿。
    """
    for line in body.splitlines():
        if needle in line and not line.lstrip().startswith("#"):
            return line
    return None


#: 服务模式钉死的步数预算 —— **现场从 SDK 派生**，不写死 8：
#: 写死的话 SDK 哪天改了默认值，用到它的断言就会在错误的假设下"继续通过"。
#: ⚠️ 必须放在**模块级**：原先它只是 `check_guards()` 里的**局部变量**，
#:    于是 3c 节（发起轮台账）想复用就只能再写一份 —— 那就是"同一事实写两处"，
#:    而两处必然漂移。（2026-10-04 加 3c 节时当场踩到 `NameError`。）
SERVICE_MAX_STEPS: int = RunConfig().max_steps


def _sdk_wait_timeout_seconds() -> float | None:
    """单次 `acps_task(action="wait")` 的墙钟上限，**现场从 SDK 读**。

    它决定"主控的等待窗口 ≈ 等待步数 × 本值"，是步数预算换算成秒数的唯一系数。
    ⚠️ 读不到就返回 `None`（**不写死 45**）：写死的话 SDK 哪天把默认值改了，
    这里的换算会在错误的前提下继续成立 —— 那正是本文件存在的意义所在。
    """
    try:
        from wit.acps.direct import AipCoordinator

        value = getattr(AipCoordinator, "DEFAULT_WAIT_TIMEOUT_SECONDS", None)
    except Exception:  # noqa: BLE001 - 探测本身失败不该让整轮自检崩掉
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _roles_without_ports() -> list[str]:
    """`BUILDERS` 里有代码、但 `PORTS` 里没登记端口的角色。

    ⚠️ **现场派生**（`BUILDERS` ∩ 补集），不写死清单 —— 这条判据要防的恰恰是
    "加了一个角色却忘了给它配端口"，写死清单等于把判据自己关掉。
    """
    return sorted(r for r in BUILDERS if r not in PORTS)


def _report_unregistered_ports() -> bool:
    """「有没有角色没登记端口」的**唯一报出处**。返回是否通过。

    ⚠️ 必须在本文件**任何**按角色取端口的循环**之前**跑。原因不是"顺序讲究"，
    而是一个 2026-10-05 实测出来的事实：**哪一段代码先撞上 `KeyError` 取决于
    `.env` 里有没有 `ACPS_PUBLIC_HOST`** ——

      · 设了它 ⇒ `build_config()` 走到 `endpoint_for()`，止于**第 2 节**，
        全脚本只留下 78 行和一段栈；
      · 没设它 ⇒ 第 2 节侥幸过去，先崩的是**第 3 节**的 `PORTS[role]`。

    也就是说**同一份代码在两台机器上停在不同地方**，而"看板"的职责恰恰是
    "不管停在哪，都得先把缺的是哪个角色说清楚"。把诊断放在某一节里、指望它
    恰好排在前面 ⇒ 就会有半数的机器上一句诊断都看不到（服务器那次正是如此，
    元验证用例 C7 在服务器上**空转了整整一轮**而本地却是绿的 —— 本地绿是运气）。
    """
    missing = _roles_without_ports()
    if missing:
        print(f"  [失败] 这些角色没有登记端口：{missing}")
        return False
    return True


def check_contracts() -> bool:
    section("1. 评分量表自检")
    ok = True
    # 分档必须完整覆盖 -100..100 且不重叠
    covered: set[int] = set()
    for lo, hi, band, _ in HARM_BAND_TABLE:
        for v in range(lo, hi + 1):
            if v in covered:
                print(f"  [失败] {v} 被多个分档覆盖")
                ok = False
            covered.add(v)
    missing = sorted(set(range(-100, 101)) - covered)
    if missing:
        print(f"  [失败] 未覆盖的分值：{missing[:10]}")
        ok = False
    if ok:
        print("  [通过] 分档完整覆盖 -100..100，无重叠")
    for probe in (-100, -30, 0, 15, 55, 90, 100):
        print(f"    harm_index={probe:>4}  ->  {band_of(probe).value}")

    # ── 停用断言（2026-10-07）：煽动 / 危害两项判定不许悄悄复活 ──────────
    #     背景：这两项原依赖的专家（煽动识别 / 危害评估）已软废弃，
    #     用户口径 = 最终输出**不出现**煽动性 / 危害性文字。字段保留只为
    #     契约兼容。退化形态：① 字段默认值被删回必填；② 提示词停用段被删。
    section("1b. 主控停用断言（煽动 / 危害两项判定）")
    from .contracts import FinalVerdict as _FV
    from .agents import LEADER_INSTRUCTIONS as _LI
    try:
        probe = _FV(subject="测试", truth_score=50, conclusion="测试", action_advice="测试")
        if probe.is_incitement is not False or probe.harm_index != 0:
            print(f"  [失败] 主控停用断言：FinalVerdict 默认值不是 false/0"
                  f"（is_incitement={probe.is_incitement}, harm_index={probe.harm_index}）"
                  " ⇒ 两项已停用的判定又变回了必填输出")
            ok = False
        else:
            print("  [通过] FinalVerdict 两项停用字段默认 false/0（不传也合法）")
    except Exception as exc:  # noqa: BLE001
        print(f"  [失败] 主控停用断言：FinalVerdict 不传停用字段构造失败"
              f"（{type(exc).__name__}: {exc}）⇒ 字段被改回必填 = 停用已回退")
        ok = False
    # 提示词侧：停用指令必须在场（锚定「固定填」这个动作指令，唯一载体）
    if "固定填" not in _LI or "不得出现" not in _LI:
        print("  [失败] 主控停用断言：LEADER_INSTRUCTIONS 缺停用指令"
              "（固定填 false/0 + 输出文字不得出现相关表述）"
              " ⇒ 模型会继续把煽动/危害当判定输出")
        ok = False
    else:
        print("  [通过] LEADER_INSTRUCTIONS 含停用指令（固定填 + 禁止表述）")
    return ok


def check_nesting() -> bool:
    """嵌套结构的契约与拓扑自检（2026-09-29 改造后新增）。

    这一节防的是「结构被悄悄改回去」。两条最容易退化的性质：
      · 裁判必须**独立存在**，而不是又让辩手兼任 —— 兼任就是既当运动员又当裁判；
      · 正方不得重新长出委派能力，否则它会再次变成子会话 Leader。

    另外校验误导性话术齐全（两层：证据层 4 + 修辞层 6）—— 它们是"反诈辨识"的原料，
    缺一类就少一课；而**没归层**的话术会被前端分组悄悄丢掉，同样算缺陷。
    """
    section("2c. 嵌套结构自检（主控 → 网络裁判 → 正/反方）")
    ok = True

    # ① 角色齐备、端口唯一
    # 这里是**刻意写死的快照**，不是"又一处会漂移的副本"：
    # 2c 要回答的是"结构是不是我们**打算要的样子**"，所以它必须是一份独立的主张，
    # 不能从 BUILDERS 派生（那就永远自洽、也就永远查不出问题）。
    # 与脚本清单保持一致由 3b 负责（那条是"必须相等"，所以必须派生）。
    expected_roles = {
        # ⚠️ 这两个自 2026-10-02 起**软废弃**：不部署、不进主控链路，
        #    但**仍在 BUILDERS 里**（代码保留；平台注册已不在——2026-10-13
        #    #126 实测复核，重新启用需重走注册与审核）。
        #    "在 BUILDERS 里" ≠ "要部署" —— 部署清单是 DEPLOY_ROLES，由下面单独断言。
        "incitement",
        "harm",
        "leader",
        "fact-judge",
        "debate-pro",
        "debate-con",
        # 2026-09-30 新增：图像鉴伪（纯 Partner，由主控中继调用）。
        # 它是加分项证据鉴证的第一个模态，不属于"主控 → 裁判 → 正/反方"那条链，
        # 所以在下面第 ② 步的链关系里**不参与**（只要求角色齐备 + 端口唯一）。
        "evidence-image",
        # 2026-10-02 新增：证据鉴证 Leader —— 证据链的内层 Leader，
        # 与 `fact-judge` 同形（S1 Partner ＋ S2 Leader）。
        # 主控**只认这一个鉴伪入口**，由它去组织下游的鉴伪专家；
        # 它存在的理由是步数预算（详见 agents.py 里 build_evidence_leader 的注释）。
        "evidence-leader",
        # 2026-10-02 新增：音频鉴伪（证据鉴证的第二个模态）。
        # ⚠️ 它是**加在证据链的第三级上（与 evidence-image 平级）**，
        #    不是加在图像底下 —— 后者会撞 `MAX_CHAIN_DEPTH = 3`。
        #    同理不进第 ② 步的"主控 → 裁判 → 正/反方"链关系。
        "evidence-audio",
        # 2026-10-02 新增：视频鉴伪（证据鉴证的第三个模态）。
        # 同样是**第三级、与图像 / 音频平级**；链深 3/3 已到顶。
        "evidence-video",
        # 2026-10-07 新增：法律咨询顾问（纠纷初期建议，引用已核验法条库）。
        # 纯 Partner（叶子，与辩手同形），挂在主控下（链深 1，与网络裁判同层），
        # **不进**第 ② 步的"主控 → 裁判 → 正/反方"链关系 —— 它不在辩论链上。
        # 它的叶子形态（链熔断 / 不委派 / 兜底关闭）由护栏节按辩手同款断言守着。
        "law-advisor",
        # 2026-10-11 新增：线索受理官（「群众来信」→ 待核查队列）。
        # 与法律咨询顾问完全同形的叶子；不在辩论链上，叶子形态由护栏节守着
        # （现与辩手/法律咨询顾问同一组断言）。受理行为由 ⑦-9 节守着。
        "lead-intake",
    }
    if set(BUILDERS) != expected_roles:
        print(f"  [失败] 角色集合不符：{sorted(BUILDERS)}（后续检查跳过）")
        return False
    print(f"  [通过] {len(BUILDERS)} 个角色齐备：{sorted(BUILDERS)}")

    # ①-补 软废弃的结构主张（2026-10-02）。三件事必须**同时**成立 ——
    #     缺任何一条，"软废弃"就已经漂成别的东西了：
    #       · 软废弃的角色仍在 BUILDERS 里  → 代码没被删，可重新启用；
    #       · 它们不在 DEPLOY_ROLES 里      → 真的没在跑，主控链路已摘净；
    #       · 两者并起来正好等于全部角色     → 没有第三个去向（比如被静默删除）。
    #     少了这条断言，最容易发生的退化是"软废弃慢慢变成彻底删除"——
    #     而现象是**一切全绿**，直到某天想重新启用才发现代码没了。
    soft, deploy, all_roles = set(SOFT_RETIRED), set(DEPLOY_ROLES), set(BUILDERS)
    soft_problems: list[str] = []
    if not soft <= all_roles:
        soft_problems.append(f"不在 BUILDERS 里（代码被删了？）：{sorted(soft - all_roles)}")
    if soft & deploy:
        soft_problems.append(f"仍在部署清单里（没真停？）：{sorted(soft & deploy)}")
    if soft | deploy != all_roles:
        soft_problems.append(
            f"并集 != 全部角色：多出 {sorted((soft | deploy) - all_roles)}、"
            f"漏了 {sorted(all_roles - (soft | deploy))}"
        )
    if soft_problems:
        print(f"  [失败] 软废弃清单不自洽：{'；'.join(soft_problems)}")
        ok = False
    else:
        print(
            f"  [通过] 软废弃 {len(soft)} 个（{sorted(soft)}）：代码保留、不部署，"
            f"与部署清单（{len(deploy)} 个）互补"
        )
    # ⚠️ 先查"有没有登记"，再算"有没有冲突"。直接写 `PORTS[r]` 会在**缺登记**时
    #    抛 `KeyError` —— 而看板的职责恰恰是**把结构问题报出来**：一次 traceback
    #    只告诉人"脚本坏了"，不告诉人"少给哪个角色配了端口"，而且它还发生在
    #    "后续检查"之前，于是整份看板只留下一行栈。
    #    （2026-10-02 元验证用例 7 实测踩到：把 `evidence-audio` 从 PORTS 里去掉，
    #      报出来的不是诊断而是一段 `KeyError` 栈。）
    #    ⚠️ 2026-10-05：**这句诊断从这里搬走了**。它原先写在本节，而本节是**第 4 个**
    #    执行的 —— 遇到缺登记时脚本早就崩在前面某一节（崩在哪一节取决于 `.env`），
    #    于是这句"先查有没有登记"的注释在**半数机器上**是自我安慰。
    #    现在唯一报出处是 `_report_unregistered_ports()`（第 2 节），
    #    本节只负责**别在它之后又踩一次**，并据此决定要不要报"端口互不冲突"
    #    —— 缺了角色还说"端口不冲突"，那是把不完整的事实写成正面结论。
    registered = [PORTS[r] for r in BUILDERS if r in PORTS]
    if len(set(registered)) != len(registered):
        print(f"  [失败] 端口冲突：{registered}")
        ok = False
    elif not _roles_without_ports():
        print(f"  [通过] 端口互不冲突：{sorted(registered)}")

    # ② 内层 Leader：裁判必须同时具备「被委托」与「委托」两种能力
    judge_hooks = {getattr(h, "id", ""): h for h in BUILDERS["fact-judge"](DUMMY_MODEL).hooks}
    has_deleg = any(k.startswith("acps.aip.delegation") for k in judge_hooks)
    has_guard = "legal.termination-guard" in judge_hooks
    if has_deleg and has_guard:
        print("  [通过] 网络裁判同时具备「被委托」与「委托」能力 → 内层 Leader 成立")
    else:
        print(
            f"  [失败] 网络裁判职责不全：delegation={has_deleg} termination={has_guard}"
            "（缺任一项，两级嵌套就不成立）"
        )
        ok = False

    guard = judge_hooks.get("legal.termination-guard")
    if guard is not None:
        if getattr(guard, "expected_partners", None) == 2:
            print("  [通过] 裁判等待 2 位下游（正方 + 反方）")
        else:
            print(
                f"  [失败] 裁判预期下游数应为 2，实际 {getattr(guard, 'expected_partners', None)}"
            )
            ok = False

    # ②-补 证据鉴证 Leader（2026-10-02 新增）—— 形态与裁判一致，但**必须有它自己的断言**。
    #
    #   为什么不能只断言"裁判是内层 Leader"就算完：这两个角色的**退化方式不同**。
    #   裁判断链 = "没人组织辩论"，一眼能看出来；而证据链多这一层**是为了省步数预算**
    #   （服务模式 `max_steps=8` 钉死，`RunConfig` frozen）—— 它一旦退化成
    #   "主控又直接对接鉴伪专家"，**不会报任何错**，只是模态一多就装不下、链路悄悄变慢变错。
    ev_hooks = {getattr(h, "id", ""): h for h in BUILDERS["evidence-leader"](DUMMY_MODEL).hooks}
    ev_deleg = any(k.startswith("acps.aip.delegation") for k in ev_hooks)
    ev_guard_id = "legal.termination-guard"
    ev_inbox_id = "legal.evidence-inbox"
    ev_guard = ev_guard_id in ev_hooks
    ev_inbox = ev_inbox_id in ev_hooks
    if ev_deleg and ev_guard and ev_inbox:
        print("  [通过] 证据鉴证 Leader 具备「委托 / 强制交付 / 收件箱」三件能力")
    else:
        print(
            f"  [失败] 证据鉴证 Leader 职责不全：delegation={ev_deleg} "
            f"termination={ev_guard} inbox={ev_inbox}"
        )
        ok = False

    ev_guard_hook = ev_hooks.get(ev_guard_id)
    if ev_guard_hook is not None:
        if getattr(ev_guard_hook, "expected_partners", None) == 1:
            print("  [通过] 证据鉴证 Leader 等待 1 位下游（鉴伪专家）")
        else:
            print(
                "  [失败] 证据鉴证 Leader 预期下游数应为 1，实际 "
                f"{getattr(ev_guard_hook, 'expected_partners', None)}"
                "（`_decide` 的判据是 `max(expected, delegations)`，写大了会永不触发收尾）"
            )
            ok = False

    # 关系断言（不是"挂了没有"）：收件箱的**路二**要接管 `acps_delegate`，
    # 挂在**没有**该工具的角色上就是死代码；反过来，**会委派却没挂它**，
    # 图片字节就永远到不了下游（模型工具层只认 uri，字节只能由代码塞）。
    # ⚠️ 本项目已有前车之鉴：`DebateChainGuardHooks` 曾挂在没有 `acps_delegate`
    #    的辩手身上，整整空转了一轮真机。**先问"谁来调它"，再问"调了管不管用"。**
    ev_inbox_hook = ev_hooks.get(ev_inbox_id)
    if ev_inbox_hook is not None:
        takes_over = bool(getattr(ev_inbox_hook, "delegate_tool", ""))
        if ev_deleg != takes_over:
            print(
                "  [失败] 证据鉴证 Leader 的委派能力与收件箱接管不匹配："
                f"会委派={ev_deleg} 接管={takes_over}"
                "（会委派却不接管 ⇒ 材料送不出去；不委派却接管 ⇒ 死代码）"
            )
            ok = False
        else:
            print("  [通过] 证据鉴证 Leader：会委派 ⇔ 收件箱接管 acps_delegate")

    # 链深：主控(1) → 证据鉴证 Leader(2) → 图像鉴伪(3)，**正好等于上限**。
    # 这条断言的意思不是"现在能跑"，而是"**再往下挂一级就会撞墙**"：
    # 将来加 audio/video 时若想再插一层，必须先改 `MAX_CHAIN_DEPTH` 并重估环检测。
    if MAX_CHAIN_DEPTH < 3:
        print(
            f"  [失败] 链深上限 {MAX_CHAIN_DEPTH} < 3："
            "「主控 → 证据鉴证 Leader → 图像鉴伪」这条链会被熔断拒绝"
        )
        ok = False
    else:
        print(
            f"  [通过] 证据链深度 3 ≤ 上限 {MAX_CHAIN_DEPTH}"
            "（已到顶：加模态时不要再插层级）"
        )

    # 方案 A 的全部收益是「主控只认识**一个**鉴伪入口」。
    # 正向判据：主控必须认识「证据鉴证 Leader」。
    # 反向判据：主控的 instructions 里**不得出现**下游专家的注册名 ——
    #   它一旦知道这个名字，就会绕过 Leader 直接派发，这一层就被架空了。
    #   （写"不得出现"而不是"不得直接派发"是因为后者要靠语义判断，判不了；
    #     而"知不知道这个名字"是**可判**的结构事实。）
    #
    #   ⚠️ 两侧的清单**都从模态枚举现场派生**，一个手抄的名字都没有：
    #      反向 = 各 Partner **自己注册的** `display_name`（断言的是"两边名字一致"，
    #             而不是"我抄对了没"）；
    #      正向 = 分支标签 `f"{模态}鉴伪分支"`（与 Partner 名共用同一套词）。
    #      加一个模态时这两条会一起跟上；只加角色不加分支、或反过来在分支里写回
    #      下游注册名，都会让绿变红。
    leaked_names = _leader_leaked_partner_names()
    missing_branches = _leader_missing_branches()
    if "证据鉴证 Leader" not in LEADER_INSTRUCTIONS:
        print(
            "  [失败] 主控 instructions 不认识「证据鉴证 Leader」"
            "（证据链断在顶层，用户看不到鉴伪能力）"
        )
        ok = False
    elif leaked_names:
        # ⚠️ 只查图像那一个时，"顺手在新写的音频 / 视频分支里写下游注册名"这种做法
        #    **不会被任何断言拦下** —— 而后果一模一样：主控知道名字就会绕过
        #    证据鉴证 Leader 直接派发。
        print(
            "  [失败] 主控 instructions 里出现了下游专家的注册名"
            "（" + "、".join(_quote(n) for n in leaked_names) + "）"
            "（它一旦知道这个名字就会绕过证据鉴证 Leader 直接派发，方案 A 被架空）"
        )
        ok = False
    elif missing_branches:
        # 只加角色、不加分支的后果，是那条链从顶层就断了 —— 而所有离线断言
        # （角色建起来了、Hook 挂对了、端口登记了）仍然全绿。
        print(
            "  [失败] 主控 instructions 里没有这些鉴伪分支："
            + "、".join(_quote(lbl) for lbl in missing_branches)
            + "（角色建好了，但用户带这类材料来时主控不知道该往哪派 —— 断在顶层）"
        )
        ok = False
    else:
        print(
            "  [通过] 主控只认「证据鉴证 Leader」一个鉴伪入口"
            f"（{len(EvidenceModality)} 条分支都在："
            + " + ".join(m.value for m in EvidenceModality)
            + "），且不知道下游专家的注册名"
        )

    # ③ 误导性话术齐全（两层：证据层 4 + 修辞层 6）
    #
    # ⚠️ 这里原来是 `kinds == want`（**精确相等**），已改成"至少包含"。原写法的毛病：
    #    · 新增一层话术时它会报「话术类型缺失：[]」—— 缺失集**是空的**，
    #      报出来的失败信息指错方向（明明是"多了"，却说"缺了"）；
    #    · 它把"枚举里出现了别的值"当成缺陷，而枚举本来就会长。
    #    真正要守的是"**不许少**"（少一类就少一课）；"多了什么"由下面分层检查去看。
    kinds = {k.value for k in FallacyKind}
    want = {k.value for k in FALLACY_EVIDENCE_KINDS}
    if want <= kinds:
        print(f"  [通过] 证据层话术齐全（{len(want)} 类）：{sorted(want)}")
    else:
        print(f"  [失败] 证据层话术缺失：{sorted(want - kinds)}（反诈辨识会少一课）")
        ok = False
    rhet = {k.value for k in FALLACY_RHETORIC_KINDS}
    if rhet <= kinds:
        print(f"  [通过] 修辞层话术齐全（{len(rhet)} 类）：{sorted(rhet)}")
    else:
        print(f"  [失败] 修辞层话术缺失：{sorted(rhet - kinds)}")
        ok = False
    # 新增了话术却忘了归层 —— 前端按层分组时它会**静默消失**（不报错、只是不显示）
    unlayered = sorted(k for k in kinds if not fallacy_layer(k))
    if unlayered:
        print(f"  [失败] 有话术没有归层：{unlayered}（前端分组会把它丢掉）")
        ok = False

    # ④ 裁定书契约的关键字段
    need = {"verdict", "confidence", "fallacies", "basis", "disputed_points"}
    fields = set(FactRuling.model_fields)
    if need <= fields:
        print(f"  [通过] 裁定书契约含关键字段：{sorted(need)}")
    else:
        print(f"  [失败] 裁定书缺字段：{sorted(need - fields)}")
        ok = False

    # ⑤ 受理契约必须带「可判真假的命题」
    if "proposition" in ArbitrationCase.model_fields:
        print("  [通过] 受理契约含 proposition（事件先被加工成可判真假的命题）")
    else:
        print("  [失败] 受理契约缺 proposition —— 含混传闻没法被裁定")
        ok = False

    # ⑥ discovery 是**全网**语义检索，instructions 必须按 name 钉住自己人。
    #    实测查「辩论 反驳」返回 7 条只有 1 条是本项目的；不钉名字，模型就会
    #    抓到外部团队的同类 Agent，被委派边界拒绝后白烧预算（裁判只有 8 步）。
    #    ⚠️ 护栏在代码里（身份闸门）只是兜底，**省预算的那一半必须写在提示词里**。
    #
    #    ⚠️ ⑥-补 软废弃（2026-10-02）：主控不再委派两位专家，改为自己完成三项判定。
    #       2026-10-13 #126 实测：平台注册已不在、discovery 召不回自己人 ⇒ 停用声明
    #       从「必要防线」降级为**纵深防御**（防未来有人重注册同名角色被误派——
    #       断言保留，理由换新）。不写这一句，同名角色一旦出现，模型照样会把它们
    #       找回来、派出去、再等一个本不该存在的下游（本项目实测过这类失败 ——
    #       主控一轮里派出 3 位、只等 2 位，第三位的结论永远进不了回答）。
    #       注意这与下面"钉住自己人"是**方向相反**的两件事：一个要求认得自己人，
    #       一个要求避开已停用的人 —— 所以分开断言，不要合并成一条。
    retired = ["煽动性识别", "社会危害性评估"]
    if "已停用" not in LEADER_INSTRUCTIONS:
        print(
            "  [失败] 主控 instructions 没声明两个专家已停用 —— "
            "discovery 仍能召回它们，模型会照派（软废弃最容易漏掉的一半）"
        )
        ok = False
    elif not all(n in LEADER_INSTRUCTIONS for n in retired):
        print(
            f"  [失败] 停用声明没点名到具体角色：缺 "
            f"{[n for n in retired if n not in LEADER_INSTRUCTIONS]}"
            "（不点名，模型不知道'已停用'说的是谁）"
        )
        ok = False
    else:
        print(f"  [通过] 主控 instructions 已声明 {len(retired)} 个软废弃专家并禁用委派")

    pinned = {
        # ⚠️ 主控现在有**四个可能的下游分支**（事实核查 → 网络裁判 / 鉴伪 →
        #    证据鉴证 Leader / 法律咨询 → 法律咨询顾问 / 线索报送 → 线索受理官），
        #    四个名字都要钉住 —— 少写一个，那条分支就会在 discovery 的全网检索里
        #    抓到别人的同类 Agent，白烧预算。
        "leader（主控）": (
            LEADER_INSTRUCTIONS,
            ["网络裁判", "证据鉴证 Leader", "法律咨询顾问", LEAD_INTAKE_NAME],
        ),
        "fact-judge（裁判）": (FACT_JUDGE_INSTRUCTIONS, ["辩论正方", "辩论反方"]),
        # ⚠️ 证据鉴证 Leader 的下游**按模态分支**，名字表**从模态枚举现场派生**
        #    （`_modality_partner_names`，取的是 Partner 自己注册的 `display_name`）。
        #    手抄一份的后果：加模态时它不报错，只是让新模态在 discovery 的全网检索里
        #    抓到别的团队的同类 Agent —— 而 Leader 的预算只有 8 步。
        "evidence-leader（证据鉴证）": (
            EVIDENCE_LEADER_INSTRUCTIONS, _modality_partner_names()
        ),
    }
    for who, (text, names) in pinned.items():
        # ⚠️ 空名字必须单独报：`"" in text` 恒为真，一条空名字会让"钉住下游"
        #    这条断言**静默通过**（比如某个 Partner 被删了、`display_name` 取不到）。
        blanks = [i for i, n in enumerate(names) if not n]
        missing = [n for n in names if n and n not in text]
        if blanks:
            print(
                f"  [失败] {who} 的下游名单里有 {len(blanks)} 个空名字（位置 {blanks}）—— "
                "空名字在子串判定里恒为真，会让这条断言静默通过"
            )
            ok = False
        if missing:
            print(
                f"  [失败] {who} 的 instructions 没有按 name 钉住自己人：缺 {missing}"
                "（discovery 会返回别人的同类 Agent，白烧步数）"
            )
            ok = False
        if not blanks and not missing:
            print(f"  [通过] {who} 的 instructions 已按 name 钉住 {len(names)} 个下游")

    # ⑦-0 裁判并发扇出（2026-10-11 加）：正反方必须**同一步并发派出**。
    #     哨兵断言（与钉名断言同形态）：instructions 必须同时含「并发派出」
    #     的正向指令与「不要等正方论点」的反向禁令——只写正向，模型仍可能
    #     顺序派发白烧 2 步（派发一步 + 等待一步）；被改回顺序形态时这条必响。
    fanout_anchors = ["同一条消息里并发派出", "不要等正方论点回来再派反方"]
    fanout_missing = [a for a in fanout_anchors if a not in FACT_JUDGE_INSTRUCTIONS]
    if fanout_missing:
        print(
            f"  [失败] 裁判 instructions 缺并发扇出指令：{fanout_missing}"
            "（顺序派发 = 白烧 2 步，8 步预算撑不住）"
        )
        ok = False
    else:
        print("  [通过] 裁判 instructions 已要求正反方同一步并发扇出（含反向禁令）")

    # ⑦ 模态接线：**四处同一事实，全部现场派生**（2026-10-02 加视频时新增）。
    #
    #    同一个事实在这里散成四处（见 `_modality_rows` 的 docstring）。加一个模态只改
    #    其中两处**不会报任何错** —— 症状只是那个模态的材料收不进来 / 派不出去，
    #    而链路上每一环看起来都是成功的。所以这里一律"从枚举派生"，不手抄清单。
    rows = _modality_rows()

    # ⑦-1 枚举 ⇔ 证据角色清单，**两个方向都查**：
    #      · 枚举里有的模态必须有 `evidence-<kind>` 角色 → 否则该模态的材料没人接；
    #      · `evidence-*` 角色必须有对应枚举成员 → 否则它是个**孤儿**：
    #        建起来了、端口也占了、提示词里也有它，但永远没有任何模态能派到它。
    live_roles = {r for r in BUILDERS if r.startswith("evidence-")}
    want_roles = {"evidence-leader"} | {r.role for r in rows}
    if live_roles != want_roles:
        print(
            "  [失败] 模态枚举与证据角色清单不同步："
            f"多出 {sorted(live_roles - want_roles)}、缺 {sorted(want_roles - live_roles)}"
            "（缺 = 该模态的材料没人接；多 = 孤儿角色，永远收不到活）"
        )
        ok = False
    else:
        print(
            f"  [通过] {len(rows)} 个模态 ⇔ {len(live_roles)} 个证据角色"
            f"（{'、'.join(sorted(live_roles))}）"
        )

    # ⑦-2 每个模态的下游必须有一个**非空**注册名 —— 空名字在子串判定里恒为真。
    blank_roles = sorted(r.role for r in rows if not r.partner_name)
    if blank_roles:
        print(
            f"  [失败] 这些模态的下游没有注册名：{blank_roles}"
            "（角色没建起来 / 没有 display_name ⇒ 「钉住下游」那条断言会静默通过）"
        )
        ok = False
    else:
        print(
            "  [通过] 每个模态的下游都有非空注册名："
            + "、".join(_quote(r.partner_name) for r in rows)
        )

    # ⑦-3 收件箱要收的模态：叶角色 ⊇ 自己的模态，Leader ⊇ 全部模态。
    #      最容易漏的正是**第三处** —— 忘了列进 `kinds`，那种附件就收不进来 ⇒
    #      模型看不到句柄 ⇒ 派发时无字节可带 ⇒ 下游回一句"没有材料可看"，
    #      **而链路上每一环看起来都是成功的**。
    kind_problems: list[str] = []
    for r in rows:
        if r.agent is not None and r.modality.kind not in r.inbox_kinds:
            kind_problems.append(
                f"{r.role} 的 kinds={r.inbox_kinds} 不含 {r.modality.kind!r}"
            )
    leader_kinds = tuple(getattr(ev_inbox_hook, "kinds", ()) or ())
    all_kinds = {r.modality.kind for r in rows}
    missing_at_leader = sorted(all_kinds - set(leader_kinds))
    if missing_at_leader:
        kind_problems.append(
            f"证据鉴证 Leader 的 kinds={leader_kinds} 缺 {missing_at_leader}"
        )
    if kind_problems:
        print(
            "  [失败] 收件箱没把模态收全：" + "；".join(kind_problems)
            + "（附件收不进来 ⇒ 下游只能回「没有材料可看」，且每一环都像成功）"
        )
        ok = False
    else:
        print(
            f"  [通过] 收件箱覆盖全部 {len(all_kinds)} 个模态："
            f"各叶角色收自己的，Leader 收 {'、'.join(sorted(all_kinds))}"
        )

    # ⑦-3b 模态画像表（`KIND_PROFILES`）也必须覆盖全部模态。
    #      ⚠️ 漏一项**不会报错**：`profile_for` 会退化成一份中性画像
    #      （`part_class="FilePart"`、`uri_arg="material_uri"`），于是下游收到的是
    #      "一份文件"而不是"一段视频"，而且它还会被塞进一个**不存在**的工具参数名里 ——
    #      这种失败在日志里只表现为"模型没调用工具"。所以要在**编译期**就把它钉住。
    missing_profiles = sorted(all_kinds - set(KIND_PROFILES))
    if missing_profiles:
        print(
            f"  [失败] 模态画像表缺项：{missing_profiles}"
            "（`profile_for` 会静默退化成中性画像 ⇒ 下游收到 FilePart + 一个不存在的参数名）"
        )
        ok = False
    else:
        print(
            f"  [通过] 模态画像表覆盖全部 {len(all_kinds)} 个模态："
            + "、".join(f"{k}({KIND_PROFILES[k].counter})" for k in sorted(all_kinds))
        )

    # ⑦-3c 多模态角色**绝不许**把没登记模态的附件猜成第一顺位模态
    #      （原写法 `self.kinds[0]`：主控 / 证据鉴证 Leader 的 kinds 是
    #       ("image","audio","video")，一份视频查不到模态时会被说成"图片"、
    #       句柄被写进 `image_uri`，日志里只表现为"模型没调用工具"）。
    #      单模态角色（`kinds` 只有一项）允许沿用那一项 —— 那时猜与不猜等价，
    #      而退回中性口径反而会让下游收到 `FilePart`、按模态分派找不到它。
    #      ⚠️ 判据是**行为**（真调一次 `_fallback_kind()`），不是"源码里有没有那行"。
    from .evidence_hooks import UNKNOWN_KIND

    guessers: list[str] = []
    for role, agent in sorted(_role_agents().items()):
        hook = next(
            (h for h in getattr(agent, "hooks", [])
             if getattr(h, "id", "") == "legal.evidence-inbox"),
            None,
        )
        if hook is None:
            continue
        kinds = tuple(getattr(hook, "kinds", ()) or ())
        if len(kinds) <= 1:
            continue
        got = hook._fallback_kind()  # noqa: SLF001 - 这里要验的正是它的行为
        if got != UNKNOWN_KIND:
            guessers.append(f"{role}（kinds={kinds} → 兜底成 {got!r}）")
    if guessers:
        print(
            "  [失败] 这些多模态角色会把「类型未登记」的附件猜成一个具体模态："
            + "；".join(guessers)
            + "（猜错的代价是注入给模型的参数名指向 `image_uri` ⇒ 日志里只看到"
              "「模型没调用工具」）"
        )
        ok = False
    else:
        print(
            "  [通过] 多模态角色的附件兜底走**中性口径**（不猜第一顺位）；"
            "单模态角色仍沿用唯一那一项"
        )

    # ⑦-4 落点上限不变量：收件箱（落点）必须**至少**能吃下每一条取用方能吃下的
    #      最大字节数。反了会怎样：材料在**落点**就被拒，理由却写成"太大" ——
    #      而它其实只是超过了某一个取用方的上限，调用方完全看不懂该改哪一头。
    #      （2026-10-02 加视频时把落点从 16 MiB 抬到 64 MiB，正是因为视频链的
    #        「声明长度 vs 实际长度」判据**必须拿到完整文件**，不能截断。）
    #
    #      ⚠️ 先查"每个模态都登记了取用方模块"再算上限：漏登记就**报失败**，
    #        不是静默跳过 —— 那正是这条不变量最容易被绕过去的方式。
    unregistered = sorted(all_kinds - set(_FETCH_MODULE_BY_KIND))
    if unregistered:
        print(
            f"  [失败] 这些模态没有登记取用方模块：{unregistered}"
            "（`_FETCH_MODULE_BY_KIND` 缺项 ⇒ 上限不变量会**静默跳过**它）"
        )
        ok = False
    else:
        limits: dict[str, int] = {}
        limit_problems: list[str] = []
        for kind, path in sorted(_FETCH_MODULE_BY_KIND.items()):
            module = importlib.import_module(path)
            names = sorted(
                n for n in vars(module) if re.fullmatch(r"MAX_[A-Z0-9_]*BYTES", n)
            )
            if not names:
                limit_problems.append(f"{path} 里找不到 `MAX_*BYTES` 上限常量")
                continue
            # 一个模块里万一有多个上限：取最大值 —— 那才是"取用方能吃下的最大字节数"。
            limits[kind] = max(getattr(module, n) for n in names)
        if limit_problems:
            print(f"  [失败] 取用方上限读不出来：{'；'.join(limit_problems)}")
            ok = False
        else:
            biggest_kind = max(limits, key=lambda k: limits[k])
            biggest = limits[biggest_kind]
            detail = "、".join(f"{k}={_mib(limits[k])} MiB" for k in sorted(limits))
            if MAX_STAGE_BYTES < biggest:
                print(
                    f"  [失败] 落点上限 {_mib(MAX_STAGE_BYTES)} MiB < 取用方上限最大值"
                    f"（{biggest_kind} {_mib(biggest)} MiB）：材料会在**落点**被拒，"
                    f"理由却写成「太大」—— 而它只是超过了 {biggest_kind} 的上限"
                )
                ok = False
            else:
                print(
                    f"  [通过] 落点上限 {_mib(MAX_STAGE_BYTES)} MiB ≥ 各链取用方上限"
                    f"（{detail}）"
                )

    # ⑦-5 视频降级 Hook（PPT 4-11 的 `ContentTransform`）：**凡可能看到入站
    #      `VideoPart` 的角色都必须挂**。
    #
    #      判据**现场派生**，不手抄名单：单一事实源是"该角色的收件箱收不收 video"
    #      —— 视频字节只从收件箱那条路进来，所以"收 video" ⇔ "会看到 VideoPart"。
    #      `VIDEO_CAPABLE_ROLES` 是它的一个**声明式副本**，两者必须一致
    #      （副本漂了就等于文档在骗人）。
    #
    #      ⚠️ 少挂一个的后果**实测**（`tools/verify_video.py` 第 12 节）是
    #      `UnsupportedContentError: … cannot accept 'video' content for role 'user'`
    #      ⇒ **整个 run 抛异常**，用户得到的是一句"服务异常"。
    #      这不是"少一条线索"，所以它必须是**编译期就红**的一条。
    agents_by_role = _role_agents()
    sees_video = sorted(
        r for r, a in agents_by_role.items() if "video" in _inbox_kinds_of(a)
    )
    declared_video = sorted(VIDEO_CAPABLE_ROLES)
    if sees_video != declared_video:
        print(
            "  [失败] 「谁会看到视频」与声明不一致："
            f"实测收 video 的角色 {sees_video} vs 声明 {declared_video}"
            "（声明漂了，下一个维护者就会照错的名单去挂 Hook）"
        )
        ok = False
    missing_fallback = [
        r for r in sees_video
        if "legal.video.fallback" not in _hook_ids_of(agents_by_role[r])
    ]
    if missing_fallback:
        print(
            f"  [失败] 这些角色会收到视频字节却没挂视频降级 Hook：{missing_fallback}"
            "（实测后果是 `UnsupportedContentError` ⇒ 整个 run 抛异常，不是少一条线索）"
        )
        ok = False
    if not missing_fallback and sees_video == declared_video:
        print(
            f"  [通过] 视频降级 Hook 覆盖全部 {len(sees_video)} 个会看到视频的角色"
            f"（{'、'.join(sees_video)}）"
        )

    # ⑦-5b 音频降级 Hook（2026-10-06）：与 ⑦-5 **逐行同形**，理由也同形 ——
    #      "谁会看到音频" 的单一事实源同样是"该角色的收件箱收不收 audio"，
    #      `AUDIO_CAPABLE_ROLES` 只是它的**声明式副本**。
    #
    #      ⚠️ 这一条比视频那条**更该有**：音频侧的降级器此前**根本不存在**
    #      （全仓搜不到 `AudioFallbackHooks`），而"没人核"与"核过了没问题"
    #      长得一模一样 —— 这正是 #122 能长期存在的原因。
    #      这条判据本身，就是"有人盯着"这件事的落地形态。
    #
    #      ⚠️ 少挂一个的后果与视频侧**同形**（不是"少一条线索"）：
    #      `AudioPart` 一路走到 provider ⇒ 厂商侧 HTTP 422
    #      `unknown variant 'input_audio'` ⇒ **整个 run 抛异常**。
    #      2026-10-06 真机实测：`run/e2e_audio_live.py` 约 **1 秒**就挂了。
    sees_audio = sorted(
        r for r, a in agents_by_role.items() if "audio" in _inbox_kinds_of(a)
    )
    declared_audio = sorted(AUDIO_CAPABLE_ROLES)
    if sees_audio != declared_audio:
        print(
            "  [失败] 「谁会看到音频」与声明不一致："
            f"实测收 audio 的角色 {sees_audio} vs 声明 {declared_audio}"
            "（声明漂了，下一个维护者就会照错的名单去挂 Hook）"
        )
        ok = False
    missing_audio_fallback = [
        r for r in sees_audio
        if "legal.audio.fallback" not in _hook_ids_of(agents_by_role[r])
    ]
    if missing_audio_fallback:
        print(
            f"  [失败] 这些角色会收到音频字节却没挂音频降级 Hook：{missing_audio_fallback}"
            "（实测后果是厂商侧 HTTP 422 ⇒ 整个 run 抛异常，不是少一条线索）"
        )
        ok = False
    if not missing_audio_fallback and sees_audio == declared_audio:
        print(
            f"  [通过] 音频降级 Hook 覆盖全部 {len(sees_audio)} 个会看到音频的角色"
            f"（{'、'.join(sees_audio)}）—— 产物是「如实说明 + 代码量出来的测量报告」"
        )

    # ⑦-5c **降级真的会发生吗**（2026-10-06 真机复跑夹出：上面那条全绿，功能却是坏的）
    #
    #      ⚠️ ⑦-5 / ⑦-5b 只断言「Hook 挂没挂」。它们**结构上不可能**发现
    #      「框架从不调用这个变换」—— 而这正是 2026-10-06 真实发生的事：
    #      两轮真机都 `HTTP 422 unknown variant 'input_audio'`，而 ⑦-5b 一直报[通过]。
    #
    #      根因（用框架自己的 `prepare_messages_for_provider` 实测）：
    #      `ContentTransform` **只在 provider 自报「不支持」该模态时才被应用**；
    #      本 provider 按 OpenAI 报文形状对 `audio` 自报支持 ⇒ 不显式声明有效能力，
    #      变换就永远不会被调用。
    #      ⇒ **只查挂载点的判据，必须再配一条问「它真的被调用了吗」的判据。**
    ok = _check_audio_degrade_actually_fires(ok)

    # ⑦-5d **token 预算**（2026-10-06 晚上：音频链跑通了降级，却撞上 token 上限）
    #      上面那条证的是「降级会发生」，这一条证的是「模型有额度把话说完」：
    #      实测 `completion_tokens = 8192`（**正好打满** `DEFAULT_MAX_TOKENS`）、
    #      `reasoning_tokens = 7256` ⇒ `finish_reason = length` ⇒ 没有正文、
    #      没有工具调用 ⇒ 1 轮即 `completed`、**零产物**。
    #      ⚠️ 两条判据**缺一不可**：只放大不留痕，下次换个环节照样静默空转。
    ok = _check_token_budget(ok)

    # ⑦-5e **委派顺序**：每个鉴伪分支都要说清「必须在出结论之前派」。
    #      ⑦-5d 证的是"模型有额度把话说完"，这一条证的是"它**有机会**去派" ——
    #      真机实测：主控第一轮就提交了结论 ⇒ 工具被锁 ⇒ 音频委派再没发生，
    #      而那一轮 `status=completed`、零异常。**有额度 ≠ 有顺序。**
    ok = _check_delegation_order(ok)

    # ⑦-5f **音频委派闸门**：⑦-5e 证的是"提示词写清了顺序"，这一条证的是
    #      "模型不照做时**还有东西拦着它**" —— 2026-10-06 第二次真机复跑：
    #      铁律已在提示词里（⑦-5e 全绿），主控仍然拿降级文本里那份**完整测量报告**
    #      自己出技术结论、直接交终稿，委派链一次都没走。
    #      ⇒ 「提示词改了 ≠ 模型照做」第二次坐实 ⇒ 补一道**闸门**。
    #      **有顺序 ≠ 会执行。**
    ok = _check_audio_gate(ok)

    # ⑦-5g **「送到」与「取得到」分得开**：⑦-5f 证的是"链会走"，这一条证的是
    #      "**判据自己**问对了问题" —— 2026-10-06 全量探针：字节三跳**都在盘上**
    #      （三本收件箱都有那份 `7efb800d….bin`），而判据 A′ 报 `1/3`，
    #      因为它是用 `inbox.resolve()` 问的（带**归属**校验 ⇒ 探针进程
    #      去解析常驻服务落的字节**必然**被拒）。
    #      ⇒ 判据红的是「我取不到」，它要证的却是「没送到」—— 两者长得一模一样。
    #      **链是通的 ≠ 判据问的是那件事。**
    ok = _check_inbox_landed(ok)

    # ⚠️ ⑦-5h / 5i / 5j **不在这里调用**（2026-10-07 修正）：它们最初沿用了
    #    「往 check_nesting 里塞 5 系列」的旧模式，结果 5j 一红、汇总行报的却是
    #    「嵌套结构」—— **一个根因两行**，排查时会把人引向完全错误的方向
    #    （当天实证：以为 2c 坏了，其实是新加的 5j）。
    #    ⇒ 这三个判据在 `main()` 的 results 里各占一行，**谁红谁自己出列**。

    # ⑦-6 从**命令行**真的启得动每一个已注册角色 —— 2026-10-03 的实测教训。
    #
    #      `run_agent` 原先把 `--role` 的可选值抄成一张字面量表，抄到
    #      `evidence-image` 就没接着抄 ⇒ 那天注册 evidence-leader / audio / video
    #      三个角色时，三条命令**全被 argparse 拒掉**（exit 2，屏幕上只有一行
    #      `invalid choice`）；而调用方又把输出 `| grep` 掉、用管道吃掉了退出码
    #      ⇒ 看上去"整批注册跑完了"，实际一个都没提交。
    #      现在 `run_agent` 的 choices 由 `BUILDERS` 派生（结构上不可能忘），
    #      这里补的是**行为级**那一半：真跑一次 CLI，把 argparse 回显的可选值
    #      与 `BUILDERS` 逐个对一遍。
    #      （只看源码里有没有写 `choices=sorted(BUILDERS)` 是**自省**，
    #       不属于行为断言 —— 本项目不认那种。）
    import os
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "legal_acps.run_agent", "__no_such_role__"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    # argparse 会把可选值原样回显：(choose from 'incitement', 'harm', ...)
    listed = set(re.findall(r"'([a-z0-9][a-z0-9\-]*)'", proc.stderr or ""))
    unlisted = sorted(set(BUILDERS) - listed)
    if proc.returncode == 0 or unlisted:
        print(
            f"  [失败] 命令行启不动这些已注册角色："
            f"{unlisted or '（传入不存在的角色却没报错 —— CLI 失去了这道闸门）'}"
        )
        ok = False
    else:
        print(
            f"  [通过] `run_agent --role` 覆盖全部 {len(BUILDERS)} 个已注册角色"
            "（清单由 `BUILDERS` 现场派生，加角色不可能漏）"
        )

    return ok


def check_agents() -> bool:
    section("2. Agent 编译与工具接线")
    ok = True
    # ⚠️ 缺端口必须先报出来，再往下编译 —— 否则 `build_config()` 会在
    #    `endpoint_for()` 里抛异常，脚本止于一段栈，一句诊断都没留下，
    #    而"看板"的职责恰恰是把结构问题报出来。（详见 `_report_unregistered_ports`
    #    里那段 2026-10-05 的实测：报不报得出来还取决于 `.env` 有没有 `ACPS_PUBLIC_HOST`。）
    if not _report_unregistered_ports():
        ok = False
    unregistered = set(_roles_without_ports())
    for role in BUILDERS:
        if role in unregistered:
            continue  # 诊断已在上面报出；这里再取端口只会把"一句诊断"换成"一段栈"
        agent = BUILDERS[role](DUMMY_MODEL)
        # 传 acps= 才会生成 ACS 草稿；不传则 CompiledAgent.acs_draft 为 None
        compiled = agent.compile(workspace=f".wit/{role}", acps=build_config(role=role))
        tools = [t.name for t in compiled.tools]
        draft = compiled.acs_draft or {}
        skills = [s["id"] for s in draft.get("skills", [])]
        diags = [(d.level, d.message) for d in compiled.diagnostics]

        print(f"\n  [{role}] agent_id = {compiled.agent_id}")
        print(f"        对外技能(ACS skills) = {skills}")
        print(f"        模型可调用工具        = {tools}")
        if compiled.hooks:
            print(f"        生命周期 Hook        = {[h.id for h in compiled.hooks]}")
        # 注意：acps_discover / acps_delegate / acps_task 是 AipDelegationHooks
        # 在每次 run 开始时注入的，不会出现在编译期的 tools 列表里。
        # 所以这里检查的是"委派 Hook 是否挂上"，而不是"工具是否存在"。
        hook_ids = [h.id for h in compiled.hooks]
        if role in ("leader", "fact-judge", "evidence-leader"):
            if not any(h.startswith("acps.aip.delegation") for h in hook_ids):
                print(f"    [失败] {role} 未挂载 AipDelegationHooks，将无法发现和委派 Partner")
                ok = False
            else:
                print("    [通过] 已挂载委派 Hook；acps_discover/acps_delegate/acps_task")
                print("           将在每次 run 开始时注入给模型（编译期不可见属正常）")
        # —— AMP 扩展 Hook：访问记录（2026-09-30 发现）——
        #    wit 的 access 记录是 **opt-in** 的：不挂 `AmpAccessLoggingHooks`，
        #    runtime 的 `_amp_direct_access_observer()` 就返回 None，
        #    `.wit/<role>/logs/` 里只剩 heartbeat/system，**平台侧访问次数永远是 0**。
        #    而「应用成效 10 分」与「附加分③ 访问频次 4 分」都吃这份数据 ——
        #    项目此前一处都没挂，所以"零访问"不是平台不记，是没启用。
        amp_ids = [getattr(h, "id", "") for h in compiled.amp_hooks]
        if not any(a == "acps.amp.access" for a in amp_ids):
            print(f"    [失败] {role} 未挂 AmpAccessLoggingHooks —— 平台访问次数会永远为 0")
            ok = False
        else:
            print("    [通过] 已挂 AMP 访问记录 Hook（acps.amp.access）")

        if diags:
            print(f"    [警告] 诊断信息：{diags}")
            if any(lv == "error" for lv, _ in diags):
                ok = False

    # —— 提示词里"去发现谁"必须与平台上公布的名字**逐字一致**（#94，2026-10-04）——
    #    缺陷形态（实测）：主控提示词的三个模态分支把 「证据鉴证 Leader」手写了三遍，
    #    其中一处还写着"`name` 必须是**这五个字**" —— 而它是
    #    **4 个汉字 + 1 个空格 + 6 个字母**，任何读法下都不是五个字。
    #    模型照这个数去拼名字（或认定名字里不该有空格）⇒ **发现不到**该 Agent，
    #    而失败表现是"没找到"，看上去像平台索引问题，**不像提示词写错**。
    #    ⇒ 本断言两边都**现场派生**，谁改了都当场响：
    #       · 提示词那侧 → 名字由 `EVIDENCE_LEADER_NAME` 常量插进来；
    #       · 平台那侧   → 名字取自刚**编译出来的 ACS 草稿**（`name` 字段）。
    #    正面判据之外，还要挡住"手写计数"回潮：计数与真名无关，
    #    只会被模型当成硬约束（见下 `个字` 那一条）。
    published = ""
    try:
        draft = BUILDERS["evidence-leader"](DUMMY_MODEL).compile(
            workspace=".wit/evidence-leader", acps=build_config(role="evidence-leader")
        ).acs_draft or {}
        published = str(draft.get("name") or "")
    except Exception as exc:  # noqa: BLE001 - 编不出来是**失败**，不是"跳过"
        print(f"\n  [失败] 取不到证据鉴证 Leader 的 ACS 草稿名（{type(exc).__name__}: {exc}）"
              " ⇒ 本条无法核对，按失败处理（不跳过）")
        ok = False
    if published != EVIDENCE_LEADER_NAME:
        print(
            f"\n  [失败] 证据鉴证 Leader 公布的名字 {published!r} 与提示词用的常量 "
            f"{EVIDENCE_LEADER_NAME!r} 不一致 ⇒ 主控会照着提示词里的名字去发现，指错人"
        )
        ok = False
    if LEADER_INSTRUCTIONS.count(EVIDENCE_LEADER_NAME) == 0:
        print(
            "\n  [失败] 主控提示词里没有把该名字**逐字**写出来 ⇒ 模型只能自己猜一个名字"
            "（「没找到」会被误判成平台索引问题）"
        )
        ok = False
    for line in LEADER_INSTRUCTIONS.splitlines():
        if EVIDENCE_LEADER_NAME in line and "个字" in line:
            print(
                f"\n  [失败] 提示词在提到该名字的同一行里还写着字数：{line.strip()[:80]!r}"
                " —— 名字含 ASCII 空格与字母，任何「几个字」的计数都可能被模型当成硬约束"
            )
            ok = False

    if ok:
        print(f"\n  [通过] {len(BUILDERS)} 个 Agent 全部编译成功，无 error 级诊断")
        print(
            f"  [通过] 提示词里的「{EVIDENCE_LEADER_NAME}」与平台公布的 `name` 逐字一致"
            "（两侧均现场派生），且提示词不再手写任何字数"
        )
    return ok


def check_guards() -> bool:
    """自检两个"防翻车"机制：终止强制 + 辩论熔断。

    这两处都是**自己补的协议空缺 / 逻辑护栏**，一旦被误删，现象是"演示时才炸"，
    所以必须能在离线自检里一眼看出来。
    """
    section("2b. 护栏自检（终止强制 / 辩论熔断）")
    ok = True

    # —— 终止强制 Hook 是否挂在了该挂的角色上
    #
    # 2026-09-29 两级嵌套改造后的分工：
    #   leader      外层 Leader → 出 FinalVerdict
    #   fact-judge  内层 Leader → 出 FactRuling（新增；此前由 debate-pro 兼任裁判）
    #   debate-{pro,con}  叶子辩手 → 出 DebateArgument（各自一轮论点）
    #
    # ⚠️ 辩手**也必须挂**终止强制。它们虽然不再有下游，但"必须把论点交回"这件事
    #    同样不能只靠提示词 —— 改造时一度把这层摘了，等于把"模型愿意调才调"
    #    当成保证，裁判可能收到空论点。
    expected = {
        "leader": ("legal.termination-guard", "legal_issue_verdict"),
        "fact-judge": ("legal.termination-guard", "fact_issue_ruling"),
        "debate-pro": ("legal.termination-guard", "debate_submit_argument"),
        "debate-con": ("legal.termination-guard", "debate_submit_argument"),
        # 2026-10-07：法律咨询顾问与辩手同款交付兜底 —— 强制工具是建议书。
        "law-advisor": ("legal.termination-guard", "law_submit_advice"),
        # 2026-10-11：线索受理官同款 —— 强制工具是受理记录。
        "lead-intake": ("legal.termination-guard", "lead_submit_lead"),
    }
    for role, (hook_id, tool) in expected.items():
        agent = BUILDERS[role](DUMMY_MODEL)
        hooks = {h.id: h for h in agent.hooks}
        guard = hooks.get(hook_id)
        if guard is None:
            print(f"  [失败] {role} 缺少 {hook_id}（下游/辩手返回后将不保证有交付物）")
            ok = False
            continue
        actual = getattr(guard, "conclusion_tool", None)
        if tool and actual != tool:
            print(f"  [失败] {role} 的强制工具应为 {tool}，实际 {actual}")
            ok = False
        else:
            print(f"  [通过] {role} 已挂 {hook_id}（强制工具 {actual}）")

    # —— 两位辩手 + 法律咨询顾问 + 线索受理官都必须是**纯 Partner**：
    #    · 必须保留链熔断（否则委托链会无限延伸）；
    #    · 不得挂委派 Hook（组织辩论是裁判的职责，辩手不该发起子会话；
    #      法律咨询顾问同理 —— 它给建议，不组织任何人；线索受理官同理 ——
    #      它受理线索，不组织任何人）；
    #    · 兜底必须关闭（第 5 层只会拼 FinalVerdict 形状，辩手走它就会往
    #      output/ 塞"看着像主控结论"的文件，污染唯一判据；对建议书/
    #      受理记录同理）。
    for role in ("debate-pro", "debate-con", "law-advisor", "lead-intake"):
        agent = BUILDERS[role](DUMMY_MODEL)
        hooks = {getattr(h, "id", ""): h for h in agent.hooks}
        has_chain = "legal.debate.chain-guard" in hooks
        has_deleg = any(k.startswith("acps.aip.delegation") for k in hooks)
        guard = hooks.get("legal.termination-guard")
        fallback_off = guard is not None and getattr(guard, "fallback_on_failure", None) is False
        checks = [
            (has_chain, "已挂链熔断（纯 Partner 定位）"),
            (not has_deleg, "未挂委派 Hook（组织辩论是裁判的职责）"),
            (fallback_off, "交付物兜底已关闭（不会往 output/ 塞伪结论）"),
        ]
        for good, why in checks:
            if not good:
                print(f"  [失败] {role}：{why}")
                ok = False
            else:
                print(f"  [通过] {role}：{why}")

    # —— 内层 Leader 的步数预算：`reserve_steps` 语义是"还剩几步就开始兜底"，
    #    值越大触发越早。裁判完整流程 4–5 步（受理→发现→并发派发正反→等齐→裁定，
    #    2026-10-11 起正反方同一步并发扇出），8 步预算下余量充足，
    #    但仍传 0 —— 只在真正的最后一步兜底，把"等辩手"的步还给模型。
    #    这是**行为断言**：参数若错，这里就会提前拿到 budget-guard。
    judge_guard_for_budget = next(
        (
            h
            for h in BUILDERS["fact-judge"](DUMMY_MODEL).hooks
            if getattr(h, "id", "") == "legal.termination-guard"
        ),
        None,
    )
    if judge_guard_for_budget is None:
        print("  [失败] fact-judge 上找不到 legal.termination-guard")
        ok = False
    else:
        got_rs = getattr(judge_guard_for_budget, "reserve_steps", None)
        if got_rs != 0:
            print(
                f"  [失败] 裁判的 reserve_steps 应为 0（只在最后一步兜底），实际 {got_rs}"
                " —— 会在反方论点到达前就强制出裁定"
            )
            ok = False
        else:
            print("  [通过] 裁判 reserve_steps=0（不会劫持「等辩手」的步）")

    # —— 强制文案里的字段名必须与角色契约一致
    #    裁判的 FactRuling 没有 consistency_note 字段，写死会让系统指错地方
    if judge_guard_for_budget is not None and getattr(
        judge_guard_for_budget, "note_field", None
    ) == "consistency_note":
        print("  [失败] 裁判的强制文案仍在指示写 consistency_note（FactRuling 没有该字段）")
        ok = False
    else:
        print("  [通过] 裁判的强制文案指向 FactRuling 真实存在的字段")

    # —— 终止判定：三条条件各测一遍
    def arg(round_, stance, conf):
        from .contracts import DebateArgument, DebateStance

        return DebateArgument(
            round=round_,
            stance=DebateStance(stance),
            confidence=conf,
            key_point="probe",
        )

    probes = [
        ("轮数上限", [arg(DEBATE_MAX_ROUNDS, "hold", 0.9)], True),
        ("认输", [arg(1, "hold", 0.9), arg(1, "concede", 0.4)], True),
        ("连续塌陷", [arg(1, "hold", 0.9), arg(2, "hold", 0.7), arg(2, "hold", 0.2)], True),
        ("正常继续", [arg(1, "hold", 0.9), arg(2, "hold", 0.85)], False),
    ]
    for label, history, expect_stop in probes:
        got = should_terminate(history).stop
        mark = "通过" if got == expect_stop else "失败"
        if got != expect_stop:
            ok = False
        print(f"  [{mark}] 终止判定 · {label}：stop={got}（期望 {expect_stop}）")

    # —— 链深熔断 / 环检测
    chain_probes = [
        ("正常跳", (("A",), "B"), True),
        ("环 A→B→A", (("A", "B"), "A"), False),
        ("超深度", (("A", "B", "C"), "D"), False),
    ]
    for label, (chain, target), expect_ok in chain_probes:
        try:
            hop = plan_next_hop(chain, target)
            got_ok, detail = True, f"depth={hop.depth}"
        except DebateChainError as exc:
            got_ok, detail = False, str(exc)[:36]
        mark = "通过" if got_ok == expect_ok else "失败"
        if got_ok != expect_ok:
            ok = False
        print(f"  [{mark}] 委托链 · {label}：放行={got_ok}（{detail}）")

    # —— 终止强制：默认**不得**使用 tool_choice
    #    推理模型（DeepSeek thinking）会直接 HTTP 400，整轮 run 崩掉（2026-09-29 实测）
    guard = next(
        (
            h
            for h in BUILDERS["leader"](DUMMY_MODEL).hooks
            if getattr(h, "id", "") == "legal.termination-guard"
        ),
        None,
    )
    if guard is None:
        print("  [失败] leader 上找不到 legal.termination-guard")
        ok = False
    elif getattr(guard, "force_tool_choice", None) is not False:
        print("  [失败] 终止 Hook 默认开启了 tool_choice（推理模型会 400 崩掉）")
        ok = False
    else:
        print("  [通过] 终止强制默认不用 tool_choice（改为收敛可用工具，兼容推理模型）")

    # —— 第 5 层兜底：能从专家载荷拼出一份合规结论
    from .termination import build_fallback_verdict, collect_partner_payloads

    class _FakeToolMessage:
        """顶替 wit 的 role=tool 消息：只要 role/name/text() 三个即可。"""

        def __init__(self, payload: dict) -> None:
            self.role = "tool"
            self.name = "acps_task"
            self._payload = payload

        def text(self) -> str:
            return json.dumps(self._payload)

    def _obs(task_ref: str, data: dict) -> "_FakeToolMessage":
        return _FakeToolMessage(
            {
                "status": "success",
                "modelToolName": "acps_task",
                "result": {
                    "task_ref": task_ref,
                    "state": "awaiting-completion",
                    "products": [{"type": "data", "data": data}],
                },
            }
        )

    fallback_msgs = [
        _obs("t1", {"is_incitement": True, "overall_truth_score": 40}),
        _obs("t2", {"harm_index": 70, "dimensions": {}}),
    ]
    payloads = collect_partner_payloads(fallback_msgs)
    if payloads.harm is None or payloads.incitement is None or payloads.truth is None:
        print("  [失败] 第 5 层兜底解析不到专家载荷（模型拒不交付时会没有结论）")
        ok = False
    else:
        verdict = build_fallback_verdict(
            subject="probe", payloads=payloads, reason="check_local"
        )
        good = (
            verdict["harm_index"] == 70
            and verdict["is_incitement"] is True
            and verdict["truth_score"] == 40
            and "兜底" in verdict["consistency_note"]
        )
        if not good:
            ok = False
        print(
            f"  [{'通过' if good else '失败'}] 第 5 层兜底：能从专家载荷拼出结论"
            f"（harm_index={verdict['harm_index']}, is_incitement={verdict['is_incitement']},"
            f" truth_score={verdict['truth_score']}）"
        )

    # -----------------------------------------------------------------
    # 以下两组探针是 2026-09-29 审计发现真实缺陷后补的：
    # 之前只校验"Hook 的构造参数对不对"，不校验"参数有没有真的被用上"，
    # 于是两个缺陷都逃过了自检。现在都改成**行为断言**。
    # -----------------------------------------------------------------
    from .termination import survey

    class _ToolMsg:
        """顶替 wit 的 role=tool 消息，只需 role/name/text()。"""

        role = "tool"

        def __init__(self, tool_name: str, status: str = "success", result=None) -> None:
            self.name = tool_name
            self._status = status
            self._result = result if result is not None else {}

        def text(self) -> str:
            return json.dumps(
                {
                    "modelToolName": self.name,
                    "status": self._status,
                    "result": self._result,
                }
            )

    def _partner_done(ref: str) -> "_ToolMsg":
        return _ToolMsg("acps_task", result={"task_ref": ref, "state": "awaiting-completion"})

    def _partner_pending(ref: str) -> "_ToolMsg":
        return _ToolMsg("acps_task", result={"task_ref": ref, "state": "working"})

    def _partner_dead(ref: str) -> "_ToolMsg":
        return _ToolMsg("acps_task", result={"task_ref": ref, "state": "failed"})

    def _delegated(ref: str) -> "_ToolMsg":
        """一次**成功**的委派观测（派发当下 state=working）。"""
        return _ToolMsg("acps_delegate", result={"task_ref": ref, "state": "working"})

    def _verdict_error() -> "_ToolMsg":
        return _ToolMsg("legal_issue_verdict", status="error")

    # 步数预算现取**模块级**单一事实源 `SERVICE_MAX_STEPS`（见其注释：
    # 原先写在这里是**局部**的，3c 节复用不了 ⇒ 就会被逼着写成第二份）。

    class _FakeCtx:
        def __init__(
            self, messages: list, step: int | None = None,
            max_steps: int = SERVICE_MAX_STEPS,
        ) -> None:
            self.state = type("_S", (), {"messages": messages, "metadata": {}})()
            self.config = type("_C", (), {"max_steps": max_steps})()
            # step=None 表示"拿不到步号"（budget-guard 不参与判定）；
            # 传具体步号才能让 budget-guard 生效 —— 用于验证它与"放弃强制"的优先级。
            self.private_state = {} if step is None else {"step": step}

    # —— 缺陷 1（已修）：交付物工具必须真的参数化进判定
    #    survey() 早期写死 legal_issue_verdict，导致非 Leader 角色交了交付物
    #    却永远判不出"已交付" → 第 4 层收尾 / retry / 跳过兜底 全部失效。
    #    这里用内层 Leader（网络裁判）的交付物工具名做探针。
    custom_ok = (
        survey([_ToolMsg("fact_issue_ruling")], verdict_tool="fact_issue_ruling").verdict_submitted
        is True
        and survey([_ToolMsg("fact_issue_ruling")]).verdict_submitted is False
    )
    if not custom_ok:
        print("  [失败] survey() 没有按 verdict_tool 参数判定（非 Leader 角色的交付物会判不出来）")
        ok = False
    else:
        print("  [通过] survey() 按传入的交付物工具名判定（外层主控 / 网络裁判各认各的）")

    # —— 内层 Leader（网络裁判）交付后必须触发收尾，否则会空转到预算耗尽
    judge_guard = next(
        (
            h
            for h in BUILDERS["fact-judge"](DUMMY_MODEL).hooks
            if getattr(h, "id", "") == "legal.termination-guard"
        ),
        None,
    )
    if judge_guard is None:
        print("  [失败] fact-judge 上找不到 legal.termination-guard")
        ok = False
    else:
        decided = judge_guard._decide(_FakeCtx([_ToolMsg("fact_issue_ruling")]))
        reason = decided[0].reason if decided else None
        finish_ok = reason == "finish-after-verdict" and decided[0].clear_tools is True
        if not finish_ok:
            print(
                f"  [失败] 网络裁判出了裁定后没触发收尾（reason={reason}）"
                "—— 会继续空转到预算耗尽"
            )
            ok = False
        else:
            print("  [通过] 网络裁判出裁定后触发第 4 层收尾（reason=finish-after-verdict）")

    # —— 裁判的步数预算：**行为断言**
    #
    #   第 6 步（step=5）时反方论点还没到，绝不能被强制出裁定；
    #   到了最后一步（step=7）才允许兜底。这一对断言专门钉住
    #   "reserve_steps 语义被理解反"这个真实缺陷：
    #   早期写成 2 时，step=5 会返回 budget-guard（断言必然失败）。
    if judge_guard is not None:
        budget_cases = [
            ("反方未回 + 第 6 步 → 不得强制（要给辩论留空间）", 5, None),
            ("反方未回 + 最后一步 → 必须兜底强制", 7, "budget-guard"),
        ]
        for label, step, expect in budget_cases:
            decided3 = judge_guard._decide(_FakeCtx([_partner_done("t1")], step))
            got3 = decided3[0].reason if decided3 else None
            good3 = got3 == expect
            if not good3:
                ok = False
            print(f"  [{'通过' if good3 else '失败'}] 裁判步数预算 · {label}：reason={got3}（期望 {expect}）")

    # —— 追问轮（2026-10-18）：followup_rounds 的行为契约 ——
    #
    #    每条都是行为断言（打在 _decide 返回的决策对象上）。
    #    ⚠️ 同一 run 的步进推演必须**复用同一个 _FakeCtx**（private_state
    #    挂在 ctx 上、随 run 走）—— 每次新建 _FakeCtx 等于新开一个 run，
    #    offered / last_delegations 全部归零，"模型不追问⇒锁回裁定"
    #    这条就永远测不出来。步进用 state["step"] = n 手动模拟（框架
    #    在真跑时就是这么更新它的）。
    if judge_guard is not None:
        _done2 = [
            _delegated("t1"),
            _delegated("t2"),
            _partner_done("t1"),
            _partner_done("t2"),
        ]

        def _reason_and_tool(decided):
            if not decided:
                return None, None
            return decided[0].reason, decided[0].force_tool

        # ① 默认关闭（followup_rounds=0）⇒ 旧行为零变化：回齐即锁定
        off_guard = LeaderTerminationHooks(
            conclusion_tool="fact_issue_ruling", expected_partners=2
        )
        r, t = _reason_and_tool(off_guard._decide(_FakeCtx(list(_done2), 2)))
        good = (r, t) == ("partners-ready", "fact_issue_ruling")
        if not good:
            ok = False
        print(
            f"  [{'通过' if good else '失败'}] 追问轮 · 默认关闭零变化："
            f"reason={r}, force_tool={t}（期望 partners-ready + 锁定）"
        )

        # ② 同一 run 完整推演：首次回齐 offer → 幂等 → 不追问 → 锁回
        # ⚠️ 不能写成 `f(x := Hook(...)._decide(ctx))`：海象绑定的是 _decide 的
        #    **返回值 tuple**，不是 Hook 实例 —— 后面再调 x._decide 会 AttributeError。
        run_ctx = _FakeCtx(list(_done2), step=2)  # steps_left = 8-3 = 5 ≥ 3
        followup_guard_a = LeaderTerminationHooks(
            conclusion_tool="fact_issue_ruling",
            expected_partners=2,
            followup_rounds=1,
        )
        r1, t1_ = _reason_and_tool(followup_guard_a._decide(run_ctx))
        good1 = (r1, t1_) == ("followup-offer", None)
        # 幂等闸：同一步内 context()/wrap_model_call() 各调一次 _decide，
        # 第二次必须仍是 offer —— 否则工具被锁死、追问功能自我掐死。
        r2, t2_ = _reason_and_tool(followup_guard_a._decide(run_ctx))
        good2 = (r2, t2_) == ("followup-offer", None)
        # 模型选择不追问（输出文本、无新委派）⇒ 下一步必须锁回裁定
        run_ctx.private_state["step"] = 3
        r3, t3_ = _reason_and_tool(followup_guard_a._decide(run_ctx))
        good3_ = (r3, t3_) == ("partners-ready", "fact_issue_ruling")
        for label, g, rv in [
            ("首次回齐 + 余量足 ⇒ offer（不锁工具）", good1, r1),
            ("同一步二次判定 ⇒ 幂等仍 offer", good2, r2),
            ("模型不追问 ⇒ 下一步锁回裁定", good3_, r3),
        ]:
            if not g:
                ok = False
            print(f"  [{'通过' if g else '失败'}] 追问轮 · {label}：reason={rv}")

        # ③ 追问后回齐（rounds=1 用完）⇒ 锁定，不再二次 offer
        q_ctx = _FakeCtx(list(_done2), step=2)
        q_guard = LeaderTerminationHooks(
            conclusion_tool="fact_issue_ruling", expected_partners=2, followup_rounds=1
        )
        q_guard._decide(q_ctx)  # 消耗掉唯一一次 offer
        q_ctx.state.messages += [
            _delegated("q1"),
            _partner_done("q1"),  # 追问只发了一方
        ]
        q_ctx.private_state["step"] = 4
        rq, tq = _reason_and_tool(q_guard._decide(q_ctx))
        goodq = (rq, tq) == ("partners-ready", "fact_issue_ruling")
        if not goodq:
            ok = False
        print(
            f"  [{'通过' if goodq else '失败'}] 追问轮 · 追问回齐即锁定（机会用完不续）："
            f"reason={rq}"
        )

        # ④ rounds=2：第一轮追问回齐、仍有新委派 ⇒ 还能再 offer 一次
        q2_ctx = _FakeCtx(list(_done2), step=2)
        q2_guard = LeaderTerminationHooks(
            conclusion_tool="fact_issue_ruling", expected_partners=2, followup_rounds=2
        )
        q2_guard._decide(q2_ctx)  # 第 1 次 offer
        q2_ctx.state.messages += [_delegated("q1"), _partner_done("q1")]
        q2_ctx.private_state["step"] = 4  # steps_left = 8-5 = 3 ≥ 3
        rq2, tq2 = _reason_and_tool(q2_guard._decide(q2_ctx))
        goodq2 = (rq2, tq2) == ("followup-offer", None)
        if not goodq2:
            ok = False
        print(
            f"  [{'通过' if goodq2 else '失败'}] 追问轮 · rounds=2 追问回齐仍可再 offer："
            f"reason={rq2}"
        )

        # ⑤ 预算闸（2026-10-18 用户拍板由 3 降为 2）：余量 < 2 ⇒ 不 offer 直接锁定；
        #    余量 = 2 ⇒ **必须 offer**（这条正向断言是降闸的意义所在——
        #    没有它，闸值改动只是改了个数，行为没变就是空转）。
        #    真机常态：双方回齐时 steps_left=2（受理→发现→派发→等待→等待 = 5 步）。
        b_ctx = _FakeCtx(list(_done2), step=6)  # steps_left = 8-7 = 1 < 2
        rb, tb = _reason_and_tool(
            LeaderTerminationHooks(
                conclusion_tool="fact_issue_ruling",
                expected_partners=2,
                followup_rounds=1,
            )._decide(b_ctx)
        )
        goodb = (rb, tb) == ("partners-ready", "fact_issue_ruling")
        if not goodb:
            ok = False
        print(
            f"  [{'通过' if goodb else '失败'}] 追问轮 · 预算余量不足（1 步）不给 offer："
            f"reason={rb}（交付物承诺优先）"
        )

        e_ctx = _FakeCtx(list(_done2), step=5)  # steps_left = 8-6 = 2 = 闸值
        re_, te_ = _reason_and_tool(
            LeaderTerminationHooks(
                conclusion_tool="fact_issue_ruling",
                expected_partners=2,
                followup_rounds=1,
            )._decide(e_ctx)
        )
        goode = (re_, te_) == ("followup-offer", None)
        if not goode:
            ok = False
        print(
            f"  [{'通过' if goode else '失败'}] 追问轮 · 余量=闸值(2) 必须 offer："
            f"reason={re_}（降闸的正向断言，防空转）"
        )

        # ⑥ 拿不到步号（幂等闸立不起来）⇒ 保守不 offer
        n_ctx = _FakeCtx(list(_done2), None)
        rn, tn = _reason_and_tool(
            LeaderTerminationHooks(
                conclusion_tool="fact_issue_ruling",
                expected_partners=2,
                followup_rounds=1,
            )._decide(n_ctx)
        )
        goodn = (rn, tn) == ("partners-ready", "fact_issue_ruling")
        if not goodn:
            ok = False
        print(
            f"  [{'通过' if goodn else '失败'}] 追问轮 · 拿不到步号不 offer（保守）："
            f"reason={rn}"
        )

        # ⑦ 角色接线：追问能力确实装在 fact-judge 上（「装了」的角色级断言；
        #    行为已由上六条守住，这条只守配置不漂移）
        got_fr = getattr(judge_guard, "followup_rounds", None)
        goodf = got_fr == 1
        if not goodf:
            ok = False
        print(
            f"  [{'通过' if goodf else '失败'}] 追问轮 · fact-judge 装配 "
            f"followup_rounds=1（实际 {got_fr}）"
        )

    # —— 缺陷 3（已修）：判定优先级 —— "专家已就绪"必须压过"放弃强制"
    #    早期把 verdict_errors 的放弃判定排在 partners-ready 之前，
    #    模型连错两次表单后即使专家都回来了也不再强制，只剩第 5 层兜底。
    leader_guard = next(
        (
            h
            for h in BUILDERS["leader"](DUMMY_MODEL).hooks
            if getattr(h, "id", "") == "legal.termination-guard"
        ),
        None,
    )
    # 判定优先级这一组**自己构造 Hook**：它测的是 `_decide` 的**分支优先级**
    # （"下游已就绪"必须压过"放弃强制"），与"主控现在等几位下游"无关。
    #
    # ⚠️ 旧版直接拿 `BUILDERS['leader']` 的实例来跑 ⇒ 这组断言的语义被角色参数
    #    **悄悄绑住了**：2026-10-02 主控的 `expected_partners` 由 2 改为 1
    #    （软废弃后它最多只派网络裁判一位），场景"还有一位专家没回"随之不再成立，
    #    两条断言凭空失败 —— 而它们要守的机制一条都没坏。
    #    教训：**机制单测要自带参数；角色的参数由专门的断言去守**（见本段末尾）。
    order_guard = LeaderTerminationHooks(expected_partners=2, verbose=False)
    if leader_guard is None:
        print("  [失败] leader 上找不到 legal.termination-guard")
        ok = False
    else:
        order_cases = [
            (
                "专家已就绪 + 表单连错 2 次 → 仍须强制",
                [_partner_done("t1"), _partner_done("t2"), _verdict_error(), _verdict_error()],
                "partners-ready",
                None,
            ),
            (
                "一位专家失败 + 一位返回 → 仍须强制（终态即就绪）",
                [_partner_done("t1"), _partner_dead("t2")],
                "partners-ready",
                None,
            ),
            (
                "专家未回 + 表单错 1 次 → 不得强制（强制会取不到结果）",
                [_partner_done("t1"), _partner_pending("t2"), _verdict_error()],
                None,
                None,
            ),
            # ⚠️ 2026-09-29 审计发现的缺陷：早期"放弃强制"用 return None 提前返回，
            #    顺手把预算兜底(budget-guard)也跳过了。这一步已是最后一步，
            #    必须仍走兜底 —— 否则最后一步主动放弃唯一的兜底机会。
            (
                "专家未回齐 + 表单连错 2 次 + 已是最后一步 → 预算兜底仍须生效",
                [_partner_done("t1"), _verdict_error(), _verdict_error()],
                "budget-guard",
                7,
            ),
        ]
        for label, msgs, expect, step in order_cases:
            decided2 = order_guard._decide(_FakeCtx(msgs, step))
            got = decided2[0].reason if decided2 else None
            good = got == expect
            if not good:
                ok = False
            print(f"  [{'通过' if good else '失败'}] 判定优先级 · {label}：reason={got}（期望 {expect}）")

        # —— 软废弃（2026-10-02）：主控的**预期下游数必须是 1**
        #     它不再委派任何专家，只有「事实核查」附加分支会派 1 位网络裁判。
        #     ⚠️ 判据是 `max(expected_partners, delegations)`：这里若写成 2，
        #     主控只派 1 位时 `1 >= 2` 永不成立 ⇒ 裁判把裁定书交回来了也不会触发
        #     收尾，主控只能干等到预算见底 —— 一个**不报错、只是变慢变差**的坑。
        got_ep = getattr(leader_guard, "expected_partners", None)
        if got_ep != 1:
            print(
                f"  [失败] 主控预期下游数应为 1（软废弃后最多只派网络裁判一位），"
                f"实际 {got_ep} —— partners-ready 会在只派 1 位时永不触发"
            )
            ok = False
        else:
            print("  [通过] 主控预期下游数 = 1（只等网络裁判；不再等两位专家）")

        # —— 缺陷 5（2026-10-04 已修）：主控的 `reserve_steps` **必须也是 0**
        #
        #     ⚠️ 这里原先**没有这条断言**：裁判（fact-judge）与证据鉴证 Leader 都有，
        #        唯独主控漏了 —— 而主控恰恰是"流程最长、等待最久"的那个
        #        （`discover → 派发 → 反复 acps_task(wait) → 出结论`）。
        #        「同一事实散在多处 ⇒ 加角色必漏」，这次漏在了主控身上：
        #        机制参数写在 guard 的构造处，却没有一条断言盯着主控那一处。
        #
        #     实测后果（2026-10-04 真机，判据取自**交付值**）：
        #        · 单次 wait 上限 = SDK 的 `DEFAULT_WAIT_TIMEOUT_SECONDS = 45.0`，
        #          而 `AcpsConfig` **没有** `wait_timeout_seconds` 字段 ⇒ 无旋钮可调；
        #        · `max_steps=8` 又是 frozen（CLI 不传步数 ⇒ 常驻同预算）；
        #        · 于是默认 `reserve_steps=1` 把第 7 步（`steps_left=1`）劫持成
        #          "交最终结论"，而裁定书恰好在**差一步**的位置落盘
        #          （ruling 02:42:10Z / verdict 02:42:20Z）
        #          ⇒ 主控只能报"裁定书尚未返回（多次 wait 超时）"。
        #        · 铁证：那 4 轮 verdict 里**原样带着** `_force_budget_guard` 注入的
        #          「信息不完整 / 未能获取」；10-01 真引用了裁定的几轮则没有。
        got_rs_leader = getattr(leader_guard, "reserve_steps", None)
        if got_rs_leader != 0:
            print(
                f"  [失败] 主控 reserve_steps 应为 0（只在最后一步兜底），"
                f"实际 {got_rs_leader} —— 默认 1 会把「还在等裁定书」那一步锁成交付物，"
                "而裁定书往往就差这一步"
            )
            ok = False
        else:
            print("  [通过] 主控 reserve_steps=0（不会劫持「等裁定书」那一步）")

        # —— 主控步数预算：**行为断言**（与裁判那一对同形，守**语义**而非参数）
        #
        #    第 7 步（step=6 ⇒ steps_left=1）裁定书还没回，绝不能被强制出结论；
        #    最后一步（step=7 ⇒ steps_left=0）才允许兜底。
        #    这一对是**只差一个变量**的输入，专门钉住"参数被改回默认 1"：
        #    真回到 1 时 step=6 会返回 budget-guard，第一条（期望 None）必然失败。
        leader_budget_cases = [
            ("裁定书未回 + 第 7 步 → 不得强制（要把等待还给模型）", 6, None),
            ("裁定书未回 + 最后一步 → 必须兜底强制", 7, "budget-guard"),
        ]
        for label, step, expect in leader_budget_cases:
            decided4 = leader_guard._decide(
                _FakeCtx([_delegated("t1"), _partner_pending("t1")], step)
            )
            got4 = decided4[0].reason if decided4 else None
            good4 = got4 == expect
            if not good4:
                ok = False
            print(
                f"  [{'通过' if good4 else '失败'}] 主控步数预算 · {label}："
                f"reason={got4}（期望 {expect}）"
            )

        # —— 缺陷 6（2026-10-04 已修）：一次性研判必须**自带够用的步数预算**
        #
        #     背景：单次 `acps_task(wait)` 的上限是 SDK 里写死的
        #     `DEFAULT_WAIT_TIMEOUT_SECONDS = 45.0`（`AcpsConfig` 没有对应字段 ⇒
        #     无旋钮），而 `max_steps` 在服务模式又是 8
        #     ⇒ **主控的等待窗口 ≈ 等待步数 × 45s**，步数预算直接决定等不等得到下游。
        #
        #       8 步 = `discover + 派发 + 4×wait + 交结论 + 收尾` ⇒ 窗口 180s；
        #       内层三级嵌套（主控→裁判→正/反→裁定）实测 **179s**
        #       ⇒ 余量不到 1 秒（2026-10-04 真机时间线实测）。
        #
        #     ⚠️ 判据落在**交付出去的那个 RunConfig 对象**上：直接调
        #     `run_agent.research_run_config()`（正是 `runtime.run` 收到的那一份）。
        #     不去 grep 源码里有没有 `max_steps=` —— 那是脆的、也不是行为断言。
        from . import run_agent as run_agent_mod

        got_budget = run_agent_mod.research_run_config().max_steps
        # 服务模式钉死的值**现场派生**，不写死 8（写死就会和 SDK 漂移）
        service_default = RunConfig().max_steps
        if got_budget <= service_default:
            print(
                f"  [失败] 一次性研判步数预算应 > 服务默认的 {service_default}，"
                f"实际 {got_budget} —— 等待窗口只有 {got_budget}×45s 量级，"
                "等不到实测 179s 的内层三级嵌套"
            )
            ok = False
        else:
            print(
                f"  [通过] 一次性研判步数预算 = {got_budget} 步"
                f"（服务默认 {service_default}）"
            )

        # —— 但"预算更大"**本身还不是证据**：值钱的是它换来了多少"还能再等一步"。
        #
        #     判定公式（termination.py `_steps_left` + `_decide` 第 3 层）：
        #         steps_left = max(0, max_steps - (step + 1))
        #         强制出结论 ⇔ steps_left <= reserve_steps
        #     ⇒ 被迫出结论的那一步 = `max_steps - 1 - reserve_steps`，
        #       **它之前的每一步都还允许继续等下游**。
        #
        #     所以这里把 guard 在**两个预算**下逐步走一遍，数出"仍允许等待"的步数。
        #     ⚠️ 步数是**现场走出来的**，不写死"4 次 wait"这类会随提示词漂移的数字。
        #     输入固定为「已派发 + 还没回来」—— 正是生产里"等裁定书"那一刻的形状：
        #     此时 `resolved=0 < target`、`verdict_errors=0`，唯一可能触发的原因
        #     就是 budget-guard，所以数出来的就是**纯粹的等待能力**。
        def _waitable_steps(
            guard: LeaderTerminationHooks, budget: int
        ) -> tuple[int, int | None]:
            """返回 (仍允许继续等下游的步数, 首次被迫出结论的那一步)。"""
            for step in range(budget):
                decided = guard._decide(
                    _FakeCtx(
                        [_delegated("t1"), _partner_pending("t1")],
                        step,
                        max_steps=budget,
                    )
                )
                if decided and decided[0].reason == "budget-guard":
                    return step, step
            return budget, None

        wait_timeout = _sdk_wait_timeout_seconds()
        svc_waitable, svc_forced = _waitable_steps(leader_guard, service_default)
        res_waitable, res_forced = _waitable_steps(leader_guard, got_budget)

        # (a) 两个预算下都**恰在最后一步**才强制。少了这条，"等待能力"可能是
        #     提前强制的假象，也可能是 guard 压根没响（那样 (b) 的"变多"同样没意义）。
        for _budget, _forced in (
            (service_default, svc_forced),
            (got_budget, res_forced),
        ):
            if _forced != _budget - 1:
                print(
                    f"  [失败] 步数预算 {_budget} 下首次强制落在第 {_forced} 步，"
                    f"应为最后一步（第 {_budget - 1} 步）—— 强制点算错了"
                )
                ok = False

        # (b) 一次性研判**必须真的换来更多等待能力**。
        #
        #     ⚠️ 保真说明：在**当前公式**下，本条的成立条件恰好等价于
        #        `got_budget > service_default`（两边的强制点都随 `max_steps` 平移）
        #        ⇒ 所以它**不是**一条独立的数字检查，别当成双重保险。
        #
        #     但它能逮到上面那条数字断言**逮不到的一类真缺陷**：
        #        数字断言只看 `RunConfig.max_steps` 这个**值**，
        #        而本条的判据是 guard 的**行为**（强制点真的跟着预算走了没）。
        #        若哪天 `_steps_left` 改成读某个全局常量、读不到 `ctx.config.max_steps`
        #        （= "预算送下去了，但没人接手"），数字断言照样通过，本条当场失败。
        #        这正是本仓反复栽过的「装了 ≠ 会响」。
        #        （已按此法实测：让 `_waitable_steps` 无视传入预算 ⇒ 本条失败、
        #          数字断言仍通过。）
        if res_waitable <= svc_waitable:
            print(
                f"  [失败] 一次性研判的等待能力没有变大："
                f"{res_waitable} 步 vs 服务默认 {svc_waitable} 步"
                " —— 预算加了却没换来「还能再等一步」，等于白加"
            )
            ok = False
        else:
            if wait_timeout is None:
                _window = "（SDK 里读不到单次 wait 上限，窗口秒数略去）"
            else:
                _window = (
                    f"每步最多等 {wait_timeout:g}s，窗口 "
                    f"{svc_waitable * wait_timeout:.0f}s → "
                    f"{res_waitable * wait_timeout:.0f}s"
                )
            print(
                f"  [通过] 等待能力 {svc_waitable} 步 → {res_waitable} 步"
                f"（预算 {service_default} → {got_budget} 步，{_window}）"
            )

        # ⚠️ 上面只证明了**那个函数会返回**正确预算，没证明它**真的被传给 runtime.run**。
        #    "装了 ≠ 会响" —— 所以再塞一个只记录参数的假 runtime 进去，
        #    走一遍真实的调用链，直接看 `runtime.run` 收到了什么。
        class _RecordingRuntime:
            """不联网、不跑模型，只把 run() 收到的 config 记下来。"""

            def __init__(self) -> None:
                self.seen: object = "（run() 从未被调用）"

            async def run(self, text: str, config: object = None) -> object:
                self.seen = config
                return None

        recorder = _RecordingRuntime()
        asyncio.run(run_agent_mod.run_research(recorder, "自检探针：不跑真模型"))
        wired = getattr(recorder.seen, "max_steps", None)
        if wired != got_budget:
            print(
                f"  [失败] run_agent 实际传给 runtime.run 的 max_steps = {wired}，"
                f"与申明的 {got_budget} 不一致 —— 预算没真正传下去"
            )
            ok = False
        else:
            print(
                f"  [通过] run_agent 确实把 max_steps={wired} 交给了 runtime.run"
                "（用假 runtime 走了一遍真实调用链）"
            )

    # —— 缺陷 4（已修）：预期下游数**不能写死**，实际派出几个就得等几个
    #
    #    2026-09-30 真机逮到：主控的「事实核查」附加分支一轮里派了 **3** 位
    #    （煽动识别 / 危害评估 / 网络裁判），而 `LeaderTerminationHooks` 的
    #    `expected_partners` 只写了 2 → 前两位一交回就触发 partners-ready，
    #    本步工具被锁定为交付物，**主控再也调不了 acps_task 去等第三位**；
    #    网络裁判的裁定书晚 9 秒才落盘，没能进入主控结论。
    #
    #    判据因此改成 `max(预期下限, 本轮实际成功派出数)`：
    #      · 实际派出数 —— "我的依赖都回来了吗"的正确语义；
    #      · 预期下限   —— 防模型一次都没派时 `0 >= 0` 立刻收尾。
    #    这条断言守的是**语义**：参数（expected_partners=2）一个都没改。
    if leader_guard is not None:
        fanout_cases = [
            (
                "标准流程 2 位全回 → 必须触发",
                [_delegated("t1"), _delegated("t2"), _partner_done("t1"), _partner_done("t2")],
                "partners-ready",
            ),
            (
                "派出 3 位只回 2 位 → 不得触发（否则主控等不到第 3 位）",
                [
                    _delegated("t1"),
                    _delegated("t2"),
                    _delegated("t3"),
                    _partner_done("t1"),
                    _partner_done("t2"),
                    _partner_pending("t3"),
                ],
                None,
            ),
            (
                "派出 3 位全部到达终态（含明确失败）→ 必须触发",
                [
                    _delegated("t1"),
                    _delegated("t2"),
                    _delegated("t3"),
                    _partner_done("t1"),
                    _partner_done("t2"),
                    _partner_dead("t3"),
                ],
                "partners-ready",
            ),
            ("一位都没派 → 不得触发（下限保护）", [], None),
        ]
        for label, msgs, expect in fanout_cases:
            decided3 = leader_guard._decide(_FakeCtx(msgs, None))
            got3 = decided3[0].reason if decided3 else None
            good3 = got3 == expect
            if not good3:
                ok = False
            print(f"  [{'通过' if good3 else '失败'}] 扇出与预期 · {label}：reason={got3}（期望 {expect}）")

    # —— 缺陷 2（已修）：委派失败不得污染委托链
    #    早期版本在 await handler 之前就写链，一次网络抖动后"重试同一目标"
    #    会被误判成环，目标在本轮被永久封禁 —— 整场辩论发不起来。
    class _Call:
        def __init__(self, target: str) -> None:
            self.id = "c1"
            self.name = "acps_delegate"
            self.arguments = {"partner_aic": target}

    async def _chain_behaviour() -> tuple[bool, bool, str]:
        guard = DebateChainGuardHooks(self_aic="aic://pro@probe", max_depth=3)
        ctx = _FakeCtx([])
        seen = {"n": 0}

        async def flaky(call):
            seen["n"] += 1
            if seen["n"] == 1:
                raise RuntimeError("模拟网络抖动")
            return {"status": "success", "result": {"task_ref": "t1"}}

        try:
            await guard.wrap_tool_call(ctx, _Call("aic://con@probe"), flaky)
        except RuntimeError:
            pass
        retry = await guard.wrap_tool_call(ctx, _Call("aic://con@probe"), flaky)
        retry_allowed = retry.get("status") != "error"

        # 真环：A → B → A
        cycle_guard = DebateChainGuardHooks(self_aic="aic://A@probe", max_depth=3)
        cctx = _FakeCtx([])
        cctx.private_state["debate_chain"] = ("aic://A@probe", "aic://B@probe")

        async def never(call):
            return {"status": "success"}

        cyc = await cycle_guard.wrap_tool_call(cctx, _Call("aic://A@probe"), never)
        cycle_blocked = cyc.get("status") == "error"
        return retry_allowed, cycle_blocked, str((cyc.get("error") or {}).get("code"))

    retry_allowed, cycle_blocked, cyc_code = asyncio.run(_chain_behaviour())
    if not retry_allowed:
        print("  [失败] 委派失败后的重试被判成了环（目标会被永久封禁，辩论发不起来）")
        ok = False
    elif not cycle_blocked or cyc_code != "debate_chain_rejected":
        print(f"  [失败] 真正的委托环没被拦住（code={cyc_code}）")
        ok = False
    else:
        print("  [通过] 委托链：失败后的重试放行、真环被拒（code=debate_chain_rejected）")

    # ⚠️ **报错的 hint 必须跟着真因走**（2026-10-06 真机夹出，判据当时全绿）：
    #    目标为空时，hint 原先仍写着"委托链已经到顶或出现环，**不要**再向该 Agent
    #    委托" —— 而真因是模型没填 `partner_aic`。模型照着它走，就被推向
    #    "别再派了"这个**恰好相反**的方向（连派 9 次，次次同样的错）。
    #    ⇒ 一条**说错理由**的错误比没有错误更糟：它把人（和模型）引去修别的东西。
    #    ⚠️ 所以这条判据要"分得开"：**目标为空**必须点名 `partner_aic`，
    #    **真环**必须**不**点名 —— 只查"有没有改进"会放过"改成了两处都这么说"。
    class _EmptyCall:
        def __init__(self) -> None:
            self.id = "c1"
            self.name = "acps_delegate"
            self.arguments = {"request": "请检查这段录音"}  # 缺 partner_aic

    async def _hint_probe() -> tuple[dict, dict]:
        async def never(call):
            return {"status": "success"}

        empty_ctx = _FakeCtx([])
        empty = await DebateChainGuardHooks(self_aic="aic://A@probe").wrap_tool_call(
            empty_ctx, _EmptyCall(), never
        )
        cyc_ctx = _FakeCtx([])
        cyc_ctx.private_state["debate_chain"] = ("aic://A@probe", "aic://B@probe")
        cyc = await DebateChainGuardHooks(self_aic="aic://A@probe").wrap_tool_call(
            cyc_ctx, _Call("aic://A@probe"), never
        )
        return (empty.get("error") or {}), (cyc.get("error") or {})

    empty_err, cyc_err = asyncio.run(_hint_probe())
    if empty_err.get("cause") != "missing_target" or "partner_aic" not in str(
        empty_err.get("hint")
    ):
        print(
            "  [失败] 委派目标为空时，报错没说真因（cause="
            f"{empty_err.get('cause')!r}）或没点名 `partner_aic` ⇒ 模型不知道"
            "该补哪个字段，只会照着「链到顶 / 别再派」这条错提示越走越远"
        )
        ok = False
    elif "partner_aic" in str(cyc_err.get("hint")):
        print(
            "  [失败] 真环的报错里也写着 `partner_aic` ⇒ 两种真因被说成了一回事，"
            "模型会去补一个**本来填对了**的字段"
        )
        ok = False
    else:
        print(
            "  [通过] 委派报错按真因分开：目标为空 ⇒ 点名 `partner_aic`；"
            "真环 ⇒ 说链，不再把两件事混成一句"
        )

    # —— 扇出 ≠ 链深（2026-09-30 真机修复后的回归断言）——
    #
    #   真机踩到的：主控的「事实核查」分支要委派 **3** 个伙伴
    #   （煽动性识别 / 社会危害性评估 / 网络裁判），而旧实现把**扇出**累加进链深
    #   → 链长变 4，撞上 MAX_CHAIN_DEPTH=3，**第 3 个委派被判"深度超限"拒绝**。
    #   后果：危害评估专家没能接入，leader 只能在结论里如实写"该系统判定委托链深度超限"。
    #
    #   AAC 语义下这三者是 **depth=2 的同级**，根本不是一条 4 层链。
    #   这条断言守的是**语义**而不是参数 —— 参数（max_depth=3）一个都没改。
    class _Fan:  # noqa: D401 —— 与上面 _Call 同形，只是名字更直白
        def __init__(self, target: str, cid: str = "c1") -> None:
            self.id = cid
            self.name = "acps_delegate"
            self.arguments = {"partner_aic": target}

    async def _fanout_behaviour() -> tuple[list[bool], bool, bool, bool]:
        async def ok_handler(call):
            return {"status": "success", "result": {"task_ref": "t"}}

        guard = DebateChainGuardHooks(self_aic="aic://L@probe", max_depth=3)
        fctx = _FakeCtx([])
        # ① 五个**同级**下游必须全部放行（旧实现在第 3 个就会拒）
        fans = []
        for i, tgt in enumerate(["A", "B", "C", "D", "E"]):
            res = await guard.wrap_tool_call(fctx, _Fan(f"aic://{tgt}@probe", f"c{i}"), ok_handler)
            fans.append(res.get("status") != "error")
        # ② 自己派自己 → 必须被拒（自环）
        s = await guard.wrap_tool_call(fctx, _Fan("aic://L@probe", "cs"), ok_handler)
        self_blocked = s.get("status") == "error"
        # ③ 跨层环：祖先路径里已有 A，B 再派 A → 必须被拒
        g2 = DebateChainGuardHooks(self_aic="aic://B@probe", max_depth=3)
        c2 = _FakeCtx([])
        c2.private_state["debate_chain"] = ("aic://A@probe", "aic://B@probe")
        c2res = await g2.wrap_tool_call(c2, _Fan("aic://A@probe", "cc"), ok_handler)
        ancestor_blocked = c2res.get("status") == "error"
        # ④ 真正的深度熔断仍必须有效：祖先已 3 层，再派一层 → 拒
        g3 = DebateChainGuardHooks(self_aic="aic://D@probe", max_depth=3)
        c3 = _FakeCtx([])
        c3.private_state["debate_chain"] = ("aic://A@probe", "aic://B@probe", "aic://C@probe")
        c3res = await g3.wrap_tool_call(c3, _Fan("aic://E@probe", "cd"), ok_handler)
        depth_blocked = c3res.get("status") == "error"
        return fans, self_blocked, ancestor_blocked, depth_blocked

    fans, self_blocked, ancestor_blocked, depth_blocked = asyncio.run(_fanout_behaviour())
    if not all(fans):
        print(
            f"  [失败] 同级扇出被误杀：5 个同级下游只放行了 {sum(fans)} 个"
            "（扇出被当成链深 —— 主控的第 3 个伙伴会永远接不上）"
        )
        ok = False
    elif not self_blocked:
        print("  [失败] 自己派给自己没被拦住（自环）")
        ok = False
    elif not ancestor_blocked:
        print("  [失败] 跨层环 A→B→A 没被拦住")
        ok = False
    elif not depth_blocked:
        print("  [失败] 真正的深度熔断失效了（祖先已 3 层还能再派）")
        ok = False
    else:
        print(
            "  [通过] 委托链语义：同级扇出不受限（5/5 放行）、自环与跨层环被拒、"
            "嵌套超深被拒"
        )

    # —— #123 **跨进程**的委托链（2026-10-06 落地）——
    #
    #    此前 `DebateChainGuardHooks` **只读** `state.metadata["debate_chain"]`、
    #    从来没有代码写它 ⇒ 生效的只有**本进程**那一半。跨进程的 A→B→A **挡不住**：
    #    B 在另一个进程里，它眼里的链是 `(B,)`，"再派回 A" 看起来完全合法。
    #    ⇒ 「补上 AAC 链深 / 环约束」这个创新点，在跨进程这一段上一直是空的
    #       （而离线自检全绿 —— 它只测本进程那一半）。
    #
    #    现在两头都接上：出站把链并进 `acps_delegate` 的 `request`
    #    （只能走 `{"type": "data"}` 项 —— 实测该工具的 schema 是
    #    `additionalProperties: False` + `required: [partner_aic, request]`，
    #    顶层**加不了**字段），入站在 `on_run_start` 里把它挑回 `state.metadata`。
    #
    #    ⚠️ 判据**两头各一条**：只测"出站并进去了"，会被"并进去了但下游读不出来"
    #       骗过（反之亦然）—— 而这两种失败在产物里长得一模一样：链深都没生效。
    #    ⚠️ 还要**分得开**：B 再派 A（环）必须拒、B 派 C（新目标）必须放行。
    #       只测"被拒"，会被"一律拒绝"的实现骗过。
    from wit.kernel.content import DataPart as _DataPart, TextPart as _TextPart

    class _PartMsg:
        def __init__(self, parts: list) -> None:
            self.role = "user"
            self.content = parts

    class _DelegCall:
        """**普通对象**形态的调用（不是 dataclass ⇒ 走"就地改写"那条路）。"""

        def __init__(self, target: str, request: object, cid: str = "c1") -> None:
            self.id = cid
            self.name = "acps_delegate"
            self.arguments = {"partner_aic": target, "request": request}

    def _real_call(target: str, request: object, cid: str = "c1"):
        """**真 `ToolCall`** 形态（frozen dataclass ⇒ 走 `dataclasses.replace` 那条路）。

        ⚠️ 两种形态**都要测**：线上给的是后者，而"换了条路就静默不传"正是
           那种判据跑在另一条路上全绿的失败。
        """
        from wit import ToolCall

        return ToolCall(id=cid, name="acps_delegate",
                        arguments={"partner_aic": target, "request": request})

    async def _cross_process(inbound_parts: list, make_call=_DelegCall) -> dict:
        ctx = _FakeCtx([_PartMsg(inbound_parts)])
        guard = DebateChainGuardHooks(self_aic="aic://B@probe", max_depth=3)
        await guard.on_run_start(type("_E", (), {"context": ctx})())

        async def _delegate(target: str, request: object) -> tuple[dict, object]:
            calls: list = []

            async def h(tc, _c=calls):
                _c.append(tc)
                return {"status": "success", "result": {"task_ref": "t"}}

            res = await guard.wrap_tool_call(
                ctx,
                make_call(f"aic://{target}@probe", request, f"c-{target}"),
                h,
            )
            return res, (calls[0] if calls else None)

        back, _ = await _delegate("A", "请复核这一段")
        fresh, fresh_call = await _delegate("C", "请复核这一段")
        return {
            "metadata": dict(ctx.state.metadata),
            "inbound": list(guard.inbound_chain_trace),
            "outbound": list(guard.outbound_chain_trace),
            "back_status": back.get("status"),
            "back_code": (back.get("error") or {}).get("code"),
            "fresh_status": fresh.get("status"),
            "fresh_request": (
                getattr(fresh_call, "arguments", {}).get("request") if fresh_call else None
            ),
        }

    MARKED = [
        _TextPart(text="请核查"),
        _DataPart(data={CHAIN_PAYLOAD_KEY: ["aic://A@probe", "aic://B@probe"]}),
    ]
    want = ["aic://A@probe", "aic://B@probe"]

    def _carry_failures(xp: dict, want: list[str], label: str) -> list[str]:
        """把一组跨进程断言的失败原因列出来（两种调用形态共用同一套）。"""
        bad: list[str] = []
        if xp["metadata"].get(CHAIN_PAYLOAD_KEY) != want:
            bad.append(
                f"入站没把上游的委托链挑回 `state.metadata`（实得 "
                f"{xp['metadata'].get(CHAIN_PAYLOAD_KEY)!r}）"
            )
        if xp["back_status"] != "error" or xp["back_code"] != "debate_chain_rejected":
            bad.append(
                f"跨进程环 A→B→A 没被拦住（status={xp['back_status']!r} "
                f"code={xp['back_code']!r}）"
            )
        if xp["fresh_status"] == "error":
            bad.append("派一个全新目标也被拒（把扇出当成了链深）")
        req = xp["fresh_request"]
        if not (
            isinstance(req, list)
            and any(
                isinstance(i, dict)
                and i.get("type") == "data"
                and (i.get("data") or {}).get(CHAIN_PAYLOAD_KEY) == want + ["aic://C@probe"]
                for i in req
            )
            and any(
                isinstance(i, dict) and "请复核这一段" in str(i.get("text") or "")
                for i in req
            )
        ):
            bad.append(f"出站没把链并进 `request`（或丢了正文）：{req!r}")
        if any(not e.get("injected") for e in xp["outbound"]):
            bad.append("有委派没能并进载荷（台账里留了 injected=False）")
        if not xp["outbound"]:
            bad.append("出站台账是空的 ⇒ 无从判断链有没有送出去")
        return [f"{label}：{b}" for b in bad]

    # 两种调用对象形态（普通对象 / 真 `ToolCall`）**都要**把链带出去。
    xp = asyncio.run(_cross_process(MARKED))
    xp_real = asyncio.run(_cross_process(MARKED, _real_call))
    failures = _carry_failures(xp, want, "普通调用对象") + _carry_failures(
        xp_real, want, "真 ToolCall"
    )
    if failures:
        print(
            "  [失败] 跨进程链：" + "；".join(failures)
            + " ⇒ 下游读不到链（或收到的任务少了内容），跨进程的环与超深都挡不住"
        )
        ok = False
    else:
        print(
            "  [通过] 跨进程链：入站挑回 `debate_chain`、出站并进 `request`"
            "（正文保留）、A→B→A 被拒而新目标放行 —— 普通对象与真 `ToolCall` 两种形态都成立"
        )

    # —— 入站挑不到时**不许伪造**（"没带链"与"带了空链"必须分得开）——
    plain = asyncio.run(_cross_process([_TextPart(text="没有链的普通任务")]))
    if CHAIN_PAYLOAD_KEY in plain["metadata"]:
        print(
            "  [失败] 跨进程链：入站**没有**链标记，`state.metadata` 里却出现了"
            f" `{CHAIN_PAYLOAD_KEY}` ⇒ 「我是第一跳」与「上游给了个空链」被混成"
            "一回事，而两者的处置完全不同"
        )
        ok = False
    elif plain["back_status"] == "error":
        print(
            "  [失败] 跨进程链：没有入站链时（= 我是第一跳），派发**第一个**下游"
            "就被拒了 ⇒ 链深判定把「起点」算成了越界，正常链路起不来"
        )
        ok = False

    # —— 出站**并不进去**时必须留痕（不许静默）——
    #    一个"链没传过去而没人知道"的世界，比"链传过去了"看起来完全一样，
    #    而排查时只能靠猜。所以 `injected=False` 也要进台账。
    _, ok_str = request_with_chain("正文", ("A",))
    _, ok_arr = request_with_chain([{"type": "text", "text": "正文"}], ("A",))
    _, ok_bad = request_with_chain(object(), ("A",))
    if not (ok_str and ok_arr) or ok_bad:
        print(
            f"  [失败] 跨进程链：`request_with_chain` 的形态判定错了"
            f"（str={ok_str} arr={ok_arr} 其它={ok_bad}）⇒ 要么正文被丢、"
            "要么把认不出的形态当成注入成功"
        )
        ok = False

    # —— 入站只认 `DataPart` 里的结构化数据，**不认正文里的字样** ——
    #    正文是下游（乃至模型）都能写的东西；认它就等于把链深交给对方自报。
    if inbound_chain([_TextPart(text=f"{CHAIN_PAYLOAD_KEY}=['X']")]) != ():
        print(
            "  [失败] 跨进程链：从**正文**里也能挑出委托链 ⇒ 链深变成对方自报，"
            "想绕开的只要在自己的请求里写一句就行"
        )
        ok = False
    elif inbound_chain([_DataPart(data={"other": 1}), _DataPart(data={CHAIN_PAYLOAD_KEY: ["X"]})]) != ("X",):
        print("  [失败] 跨进程链：多个 `DataPart` 时挑错了链（应挑带标记的那个）")
        ok = False

    # —— 接管委派时**不许吃掉**别人并进请求里的结构化数据（#123 的另一半）——
    #    `EvidenceInboxHooks` 在有附件时会**重写**整个 `request`。它若不带走
    #    外层刚并进去的链标记，跨进程链深就**恰恰在有附件的那几条链上**失效 ——
    #    而判据跑在没有附件的那条路上，全绿。⇒ 「没人核」与「核过了」长得一样。
    from .evidence import inbox as _ib2
    from .evidence_hooks import EvidenceInboxHooks as _InboxHook
    from .evidence_hooks import structured_data_items as _sdi

    marker_only = [{"type": "text", "text": "看图"}, {"type": "data", "data": {CHAIN_PAYLOAD_KEY: ["A"]}}]
    if [d["data"] for d in _sdi(marker_only)] != [{CHAIN_PAYLOAD_KEY: ["A"]}]:
        print(
            f"  [失败] 跨进程链：`structured_data_items` 没挑出链标记"
            f"（实得 {_sdi(marker_only)!r}）⇒ 接管时会把它丢掉"
        )
        ok = False
    elif _sdi("纯字符串") != [] or _sdi(None) != []:
        print("  [失败] 跨进程链：`structured_data_items` 对非数组入参没返回空表")
        ok = False
    else:
        import tempfile

        from wit import ImagePart as _ImagePart

        with tempfile.TemporaryDirectory() as tmp18:
            ws18 = Path(tmp18)

            class _Run18:
                def __init__(self) -> None:
                    self.calls: list[dict] = []

                async def aip_delegate(self, state, *, partner_aic, request):
                    self.calls.append({"partner_aic": partner_aic, "request": list(request)})
                    return {"status": "accepted"}

            class _Ctx18:
                def __init__(self, messages: list, runtime: object) -> None:
                    self.state = type("_S", (), {"messages": messages, "metadata": {}})()
                    self.private_state: dict = {}
                    self.workspace = ws18
                    self.runtime = runtime
                    self.config = None

            payload18 = b"\xff\xd8\xff\xe0" + b"cross-process-chain" * 4
            rt18 = _Run18()
            ctx18 = _Ctx18(
                [_PartMsg([_ImagePart(mime_type="image/jpeg", data=payload18)])], rt18
            )
            hook18 = _InboxHook()
            handle18 = _ib2.handle_for(payload18)

            async def _takeover18() -> None:
                await hook18.on_run_start(type("_E", (), {"context": ctx18})())

                async def passthrough(tc):
                    return {"status": "ok"}

                await hook18.wrap_tool_call(
                    ctx18,
                    _DelegCall(
                        "aic://down@probe",
                        [
                            {"type": "text", "text": f"看图 {handle18}"},
                            {"type": "data", "data": {CHAIN_PAYLOAD_KEY: ["A", "B"]}},
                        ],
                    ),
                    passthrough,
                )

            asyncio.run(_takeover18())
            sent18 = rt18.calls[0]["request"] if rt18.calls else []
            kinds18 = [type(p).__name__ for p in sent18]
            if not rt18.calls:
                print("  [失败] 跨进程链：接管分支根本没被触发 ⇒ 这条断言证明不了任何事")
                ok = False
            elif "DataPart" not in kinds18:
                print(
                    f"  [失败] 跨进程链：接管委派时把结构化数据吃掉了（发出去的只有 {kinds18}）"
                    " ⇒ 跨进程链深**在有附件的链上**静默失效，而判据跑在没附件那条路上全绿"
                )
                ok = False
            elif not any(
                type(p).__name__ == "DataPart"
                and getattr(p, "data", {}).get(CHAIN_PAYLOAD_KEY) == ["A", "B"]
                for p in sent18
            ):
                print("  [失败] 跨进程链：接管时带走的 `DataPart` 内容不对 ⇒ 链在半路被改写")
                ok = False
            else:
                print(
                    "  [通过] 跨进程链：接管委派（带附件那条路）会把外层并进请求里的"
                    "链标记原样带走"
                )

    # —— 挂载位置断言（2026-09-30 新增）——
    #
    #   这是同一天里发现的第二种"装了但不生效"：
    #   链熔断 Hook 只有挂在**会委派的** Agent 上才有效。挂在叶子节点（辩手）上，
    #   因为叶子根本没有 `acps_delegate` 这个工具，`wrap_tool_call` 永远不被触发
    #   —— 等于装了一个从不响的警报器。改造期间它一度只挂在两位辩手上，
    #   使「补上 AAC 链深约束」这个创新点在**真机上完全空转**。
    #
    #   而当时自检是全绿的：自检自己构造调用喂给 Hook，测得出"拦得住"，
    #   却测不出"线上根本没人调它"。所以这里断言的必须是**关系**，不是行为：
    #
    #       挂了 AipDelegationHooks（能委派） ⇒ 必须挂 DebateChainGuardHooks
    #
    #   这一条如果没人守，下次调整 Hook 时会以完全相同的方式静默退化。
    SELF_AIC = "1.2.156.3088.1.BUPT.81M3FX.X1J5R5.HKBBAM.018M"
    delegating_roles: list[str] = []
    guard_missing = False
    for role in BUILDERS:
        probe = build_agent(role, DUMMY_MODEL, self_aic=SELF_AIC)
        can_delegate = any(isinstance(h, AipDelegationHooks) for h in probe.hooks)
        has_guard = any(isinstance(h, DebateChainGuardHooks) for h in probe.hooks)
        if not can_delegate:
            continue
        delegating_roles.append(role)
        if not has_guard:
            print(f"  [失败] {role} 能委派却没挂链熔断/身份校验 —— 该 Hook 会被静默空转")
            ok = False
            guard_missing = True
    if not guard_missing:
        print(
            f"  [通过] 挂载位置：{len(delegating_roles)} 个会委派的 Agent"
            f"（{'、'.join(delegating_roles)}）都挂了链熔断 + 身份校验"
        )

    # —— 账号前缀派生（2026-09-30 新增）——
    #   本项目 AIC 是 10 段，账号级 = **前 7 段**（OID 5 段 + 机构 + 账号）。
    #   ⚠️ 我第一版写成"前 6 段"，只取到机构级 —— 同机构不同账号会被当成自己人。
    #   所以把这几个真实样本钉成断言（含外部的那个真实 AIC）。
    prefix_cases = [
        (
            "1.2.156.3088.1.BUPT.81M3FX.COUJXB.F9XC6K.0J6D",
            "1.2.156.3088.1.BUPT.81M3FX",
            "本项目 AIC",
        ),
        (
            "1.2.156.3088.1.0001.00001.EZTK03.PABX6D.0JUV",
            "1.2.156.3088.1.0001.00001",
            "外部团队 AIC（真实踩到的那条）",
        ),
        ("<self>", "", "占位串 → 判不出来"),
        ("", "", "空串"),
        ("1.2.3.4.5.6", "", "段数不足 → 判不出来"),
    ]
    prefix_ok = True
    for aic, want, why in prefix_cases:
        got = account_prefix_of(aic)
        if got != want:
            print(f"  [失败] 账号前缀派生 · {why}：得到 {got!r}（期望 {want!r}）")
            ok = False
            prefix_ok = False
    if prefix_ok:
        print("  [通过] 账号前缀派生：本项目 / 外部团队 / 占位串 / 段数不足，五种入参均正确")

    # —— 身份闸门：**行为断言**（2026-09-30 新增）——
    #
    #   拦的是 discovery 语义检索捞到的**外部团队 Agent**：实测查「辩论 反驳」
    #   返回 7 条，只有 1 条是本项目的反方。提示词管不住模型选谁，
    #   所以在派发边界上兜一道 —— 这里断言的就是"真的兜住了"。
    FOREIGN_AIC = "1.2.156.3088.1.0001.00001.EZTK03.PABX6D.0JUV"
    trust_guard = DebateChainGuardHooks(self_aic=SELF_AIC)
    trust_cases = [
        ("自家 AIC → 放行", SELF_AIC, None),
        ("外部 AIC → 拒绝", FOREIGN_AIC, "foreign_partner_rejected"),
        ("空目标 → 放行（交给链检测去报「目标为空」，职责分开）", "", None),
    ]
    for label, target, want_code in trust_cases:
        err = trust_guard._check_partner_trust(target)
        got_code = None if err is None else str(err.to_error().get("code"))
        good = got_code == want_code
        if not good:
            ok = False
        print(
            f"  [{'通过' if good else '失败'}] 身份闸门 · {label}：code={got_code}（期望 {want_code}）"
        )

    allowed_guard = DebateChainGuardHooks(self_aic=SELF_AIC, allowed_aics=[FOREIGN_AIC])
    if allowed_guard._check_partner_trust(FOREIGN_AIC) is not None:
        print("  [失败] 白名单里的外部 AIC 仍被拒 —— 将来无法接入外部伙伴")
        ok = False
    else:
        print("  [通过] 身份闸门 · 白名单放行：显式允许时仍可接入外部伙伴（留了活口）")

    # fail-open 是**刻意**的设计：拿不到本账号前缀时宁可不保护，也不能误杀合法委派。
    # 所以这一条的期望与上面相反 —— 它守的是"别把护栏变成误杀"。
    open_guard = DebateChainGuardHooks()
    if open_guard._check_partner_trust(FOREIGN_AIC) is not None:
        print("  [失败] 未传 self_aic 时把所有目标判成外部 —— 会把演示链路直接掐死")
        ok = False
    else:
        print("  [通过] 身份闸门 · 无身份信息时 fail-open（不误杀，只是不保护）")

    if ok:
        print("\n  [通过] 护栏齐全：结论必被提交、委托链不成环、委派只发给自己人")
    return ok



def check_protocols() -> bool:
    section("3. 协议声明与端口")
    ok = True
    # 同一个闸门，同一套理由（见第 2 节与 `_report_unregistered_ports`）：
    # 这里是 `PORTS[role]` 的裸下标，没有 `ACPS_PUBLIC_HOST` 时**它先崩**。
    # 缺登记的诊断由第 2 节统一报出 —— 一句诊断写三处，改一处必漏另一处。
    if not _report_unregistered_ports():
        ok = False
    unregistered = set(_roles_without_ports())
    for role in BUILDERS:
        if role in unregistered:
            continue
        agent = BUILDERS[role](DUMMY_MODEL)
        kinds = []
        for p in agent.protocols:
            if isinstance(p, AcpsDirect):
                kinds.append(f"AcpsDirect(modes={tuple(p.modes)})")
            elif isinstance(p, AcpsGroup):
                kinds.append("AcpsGroup")
            else:
                kinds.append(type(p).__name__)
        print(f"  {role:<12} port={PORTS[role]:<5} protocols={kinds}")
        if not agent.protocols:
            print(f"    [失败] {role} 没有声明任何 ACPs 协议，无法互联")
            ok = False
    ports = list(PORTS.values())
    if len(ports) != len(set(ports)):
        print("  [失败] 端口重复")
        ok = False
    if ok:
        print(f"  [通过] {len(BUILDERS)} 个角色均声明了协议，且端口不冲突")
    return ok


def check_scripts() -> bool:
    """启停脚本与文本卫生自检（2026-09-30 审计后新增）。

    同类缺陷今天连中两次，都属于"跑起来才炸、而自检全绿"的那种：

      · ``scripts/serve.sh`` 整篇是 CRLF（286 行全部 ``\\r\\n``，连 shebang 都带）——
        推上服务器后 bash 会逐行报 ``$'\\r': command not found``，脚本根本起不来；
      · ``scripts/verify.sh`` 里把角色清单**写死**成 5 个 —— 断言用的是新事实、
        打印出来的却是旧事实。**看板自己在骗人，比没有看板更糟。**

    所以这里断言三件事：
      ① ``.sh`` / ``.service`` 必须是纯 LF（bash 与 systemd 都不接受 ``\\r``）；
      ② 脚本里出现的"角色清单"必须与 ``acps_config.PORTS`` 完全一致 ——
         同一个事实散在 4 个地方（PORTS / serve.sh / systemd-install.sh /
         本文件），迟早会有一处漏改，漏了就是"新角色起不来"；
      ③ 每个角色都要有对应的 systemd 单元，且单元启动的正是它自己。
    """
    section("3b. 启停脚本与文本卫生")
    ok = True
    root = Path(__file__).resolve().parents[1]
    sh_files = sorted(root.glob("scripts/*.sh"))
    # 🔴 2026-10-05 收口（#129）：这里原先是**按扩展名白名单**收文件的 ——
    #    `.py` 只收 `legal_acps/**` 与 `tools/*`，单元只收 `*.service`。于是
    #    `run/*.py`（**54 个，明明在同步清单里**）、`deploy/systemd/*.timer`、
    #    `deploy/systemd/acps.logrotate`、`run/*.mjs` **全都不在判据范围内** ——
    #    而「推了一份 CRLF 上服务器」与「没推过 CRLF」在自检里**长得一模一样**。
    #    （同一家族的老账：`file_fingerprint.PATTERNS` 因按扩展名收，先后漏过
    #      html / `.mjs` / `.timer` / logrotate 四次。）
    #
    #    ⇒ 判据改成**从同步清单现场派生**：清单是"要推哪些文件"的唯一来源
    #      （`tools.file_fingerprint.PATTERNS`），自检直接消费它，不再自己列目录。
    #      以后新加一类文件会**自动**进判据 —— 不必有人记得来改这一行。
    #
    #    为什么还要**分类**：`.md` / `.html` / `.json` 是数据与渲染物，CR 不会让
    #    bash / systemd / python 出错，且都有成熟的解析器（实测 183 个同步文件里
    #    唯一带 CR 的就是生成物 `release-manifest.json`，25 个）。
    #    ⚠️ 但**没见过的后缀必须报失败**：否则以后加个 `.toml` / `.ini` 又会静默
    #       漏掉 —— 而"再补一条白名单"不是修法，那正是这次改写的起因。
    #
    #    `.py` 为什么也算"可执行文本"：Python 自己两种换行都吃，所以**它坏起来没人管**
    #    ——2026-09-30 我用一个元验证脚本整篇重写 debate.py 时，Windows 的
    #    `write_text()` 把每个 `\n` 翻成 `\r\n`，整篇变 CRLF 并被同步上服务器，
    #    而单元测试与 import 全都正常。**"能跑"正是它危险的地方**。
    EXEC_SUFFIXES = {".py", ".sh", ".service", ".timer", ".logrotate", ".mjs"}
    # `.jsonl` 与 `.json` 同类：纯数据（法条库 law-articles.jsonl 有成熟解析器，
    # CR 不影响 python 读取）—— 但它必须**在归类里**，否则法条库一进同步清单
    # 本节就报"没被归类"，而那正是想要的：新后缀要被人看见，不许静默漏掉。
    DATA_SUFFIXES = {".md", ".html", ".json", ".jsonl"}

    # —— ① 换行符：**凡是要同步给服务器的可执行文本**一律不许出现 CR
    try:
        from tools.file_fingerprint import collect as _collect_synced

        synced: list[str] | None = sorted(_collect_synced(root))
    except Exception as exc:  # noqa: BLE001
        print(
            f"  [失败] 取不到同步清单（{type(exc).__name__}: {exc}）"
            " —— 换行符判据会退化成**空转**，不许当通过"
        )
        synced = None
        ok = False

    if synced is not None:
        suffix_of = [Path(rel).suffix or "(无扩展名)" for rel in synced]
        unclassified = sorted(set(suffix_of) - EXEC_SUFFIXES - DATA_SUFFIXES)
        cr_files = [root / rel for rel, s in zip(synced, suffix_of) if s in EXEC_SUFFIXES]
        if unclassified:
            print(
                f"  [失败] 同步清单里有 {len(unclassified)} 类后缀没被归类：{unclassified}"
                " —— 请加进本节的 EXEC_SUFFIXES 或 DATA_SUFFIXES，"
                "否则这类文件**永远不会**被换行符判据看到"
            )
            ok = False

    if synced is None:
        pass  # 上面已报失败：既不重复报，也不给"通过"
    elif not cr_files:
        # ⚠️ `all([]) == True`：清单派生坏了（一个可执行文本都没有）时，
        #    下面那句"没有 CR ⇒ 通过"会**静默成立**。空集合必须显式判失败。
        print("  [失败] 同步清单里一个可执行文本都没有 —— 清单派生坏了，判据在空转")
        ok = False
    else:
        dirty = [(p, p.read_bytes().count(b"\r")) for p in cr_files if b"\r" in p.read_bytes()]
        if dirty:
            for p, n in dirty:
                why = (
                    "bash/systemd 会逐行报错"
                    if p.suffix in (".sh", ".service", ".timer", ".logrotate")
                    else "文本模式重写文件时被 TranslateNewline 引入"
                )
                print(f"  [失败] {p.relative_to(root).as_posix()} 含 {n} 个 CR（CRLF）—— {why}")
            ok = False
        else:
            counts: dict[str, int] = {}
            for p in cr_files:
                counts[p.suffix] = counts.get(p.suffix, 0) + 1
            breakdown = " + ".join(f"{n} 个 {s}" for s, n in sorted(counts.items()))
            print(
                f"  [通过] 同步清单共 {len(synced)} 个文件，其中 {len(cr_files)} 个可执行文本"
                f"均为纯 LF（{breakdown}）"
            )

    # —— ② bash 语法（能抓到真正的语法级错误；"两个 echo 挤一行"那种抓不到，
    #       那是语义错误，只能靠人读 —— 见手册 §12.6）
    bash = shutil.which("bash")
    if bash:
        broken = []
        for p in sh_files:
            proc = subprocess.run([bash, "-n", str(p)], capture_output=True, text=True)
            if proc.returncode != 0:
                first = (proc.stderr or "").strip().splitlines()
                broken.append((p, first[0] if first else "bash -n 失败"))
        if broken:
            for p, err in broken:
                print(f"  [失败] bash -n {p.relative_to(root).as_posix()}：{err}")
            ok = False
        else:
            print(f"  [通过] {len(sh_files)} 个 .sh 语法检查通过（bash -n）")
    else:
        print("  [跳过] 环境里没有 bash，跳过语法检查")

    # —— ③ 脚本里的角色清单必须**可解释**
    #     取值方式很保守：只认「括号里 ≥2 个"像角色名"的词，且至少一个真的在 PORTS 里」。
    #     后半个条件是防误报的 —— 否则 $(echo a b) 这类命令替换会被当成角色清单。
    #
    #     ⚠️ 2026-10-02 起有**三种**合法清单（软废弃引入了第二种：刻意不部署的那批）：
    #        · PORTS              —— "本项目一共定义了哪些角色"（`ALL_ROLES`）
    #        · SOFT_RETIRED       —— "刻意不部署的那几个"
    #        · PORTS − SOFT_RETIRED —— "实际要跑的"（`ROLES` / `DEPLOY_ROLES`）
    #     任何别的组合都算漂移。并要求三者严格构成一个**划分**（下面 ③-补）。
    #     这样"加了角色忘了改脚本"与"软废弃名单写错"都会被逮到。
    role_like = re.compile(r"^[a-z][a-z0-9-]*$")
    role_set = set(PORTS)
    soft_set = set(SOFT_RETIRED)
    deploy_set = set(DEPLOY_ROLES)
    allowed = {frozenset(role_set), frozenset(soft_set), frozenset(deploy_set)}
    drift: list[tuple[Path, set[str]]] = []
    for p in sh_files:
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\(([A-Za-z0-9_|\s-]+)\)", text):
            tokens = {t for t in re.split(r"[|\s]+", m.group(1)) if t}
            if len(tokens) < 2 or not all(role_like.match(t) for t in tokens):
                continue
            if not (tokens & role_set):
                continue
            if frozenset(tokens) not in allowed:
                drift.append((p, tokens))
    if drift:
        for p, tokens in drift:
            print(
                f"  [失败] {p.relative_to(root).as_posix()} 写死的角色清单不是三种合法清单之一"
                f"（实际 {sorted(tokens)}；允许：全部 {sorted(role_set)} / "
                f"软废弃 {sorted(soft_set)} / 部署 {sorted(deploy_set)}）"
            )
        ok = False
    else:
        print(
            f"  [通过] 脚本里的角色清单均可解释："
            f"全部 {len(role_set)} / 软废弃 {len(soft_set)} / 部署 {len(deploy_set)}"
        )

    # —— ③-补 三者必须构成划分（互补 + 不相交）—— 软废弃名单写错会立刻暴露
    if (soft_set | deploy_set) != role_set or (soft_set & deploy_set):
        print(
            "  [失败] SOFT_RETIRED / DEPLOY_ROLES / PORTS 不构成划分："
            f"并集多出 {sorted((soft_set | deploy_set) - role_set)}、"
            f"漏了 {sorted(role_set - (soft_set | deploy_set))}、"
            f"交集 {sorted(soft_set & deploy_set)}"
        )
        ok = False
    else:
        print(
            f"  [通过] PORTS == SOFT_RETIRED ⊎ DEPLOY_ROLES"
            f"（{len(soft_set)} + {len(deploy_set)} = {len(role_set)}）"
        )

    # —— ④ 每个角色一个单元，且启动的必须是它自己；错开延迟两两不同
    #       （延迟重复 = 两个进程同时登录 Registry，会互相顶掉会话）
    #
    #: systemd 的 `DefaultTimeoutStartSec` 默认值。这是**外部事实**（来自
    #: systemd 自身），不是本项目的可调参数 —— 单元里没显式写 `TimeoutStartSec`
    #: 时，生效的就是它。`systemctl show <unit> -p TimeoutStartUSec` 可现场复核。
    DEFAULT_START_TIMEOUT = 90
    delays: list[int] = []
    #: (角色, 错开秒数, 启动超时秒数) —— 供 ④b 判"起得来吗"
    start_specs: list[tuple[str, int, int]] = []
    for role in sorted(PORTS):
        unit = root / f"deploy/systemd/acps-{role}.service"
        if not unit.exists():
            print(f"  [失败] 缺少 systemd 单元 deploy/systemd/acps-{role}.service")
            ok = False
            continue
        body = unit.read_text(encoding="utf-8", errors="replace")
        if f"run_agent {role}" not in body:
            print(f"  [失败] {unit.name} 的 ExecStart 没有启动角色 {role}")
            ok = False
        delay = _unit_sleep_delay(body)
        delays.append(delay)
        #: ⚠️ 单元里**没写** `TimeoutStartSec` 时必须按 systemd 的默认值 90 判，
        #:    不能"没写就跳过"。跳过的话，把那一行删掉就能让断言变绿 ——
        #:    属于"退回旧形态即通过"的假判据。
        t = re.search(r"^TimeoutStartSec=(\d+)", body, re.M)
        start_specs.append((role, delay, int(t.group(1)) if t else DEFAULT_START_TIMEOUT))
    if len(set(delays)) != len(delays):
        print(f"  [失败] 单元启动延迟有重复：{sorted(delays)}（并发登录会互相顶掉会话）")
        ok = False
    else:
        print(f"  [通过] {len(delays)} 个单元的启动延迟两两不同：{sorted(delays)} 秒")

    # —— ④b 启动超时必须**大于**错开延迟 —— 否则该单元**永远起不来**
    #
    # ⚠️ 2026-10-04 实测缺陷：systemd 默认 `TimeoutStartSec=90s`，而
    #    `ExecStartPre=/bin/sleep N` **也算在启动操作里**。N=96 / 108 时
    #    journalctl 报 `start-pre operation timed out`、进程被 TERM；叠加
    #    `Restart=always` + `RestartSec=60` 就成了**每 60 秒失败一次的死循环**：
    #    单元状态永远停在 `activating`，验收表显示"无监听"，
    #    而它**看起来只是启动慢一点**。evidence-audio / evidence-video 当天第一次
    #    真正被安装，所以这个缺陷此前从未暴露。
    #    判据落在**外部事实**上（系统实际的超时语义），不是"两行数字大小"的巧合。
    bad_timeout = [f"{r}（sleep {d}s ≥ 启动超时 {t}s）" for r, d, t in start_specs if d >= t]
    if bad_timeout:
        print(
            f"  [失败] 有 {len(bad_timeout)} 个单元**永远起不来**："
            f"错开延迟 ≥ 启动超时，systemd 会在 ExecStartPre 阶段把它杀掉"
        )
        for item in bad_timeout:
            print(f"        {item}")
        print("        修法：给单元加 `TimeoutStartSec=`（要比最大延迟大），或调小延迟")
        ok = False
    else:
        worst = max(start_specs, key=lambda s: s[1])
        smallest_tmo = min(t for _, _, t in start_specs)
        print(
            f"  [通过] {len(start_specs)} 个单元都起得来："
            f"最大错开 {worst[1]}s（{worst[0]}）< 启动超时（最小 {smallest_tmo}s）"
        )

    # —— ⑤ 状态解释表必须与 wit 的真实枚举**逐字一致**
    #      `tools/status.py` 的 MEANING 是给人看的说明书。2026-09-30 审计发现：
    #      它只写了 6 个键，其中 `needs_agent_registration` 还是个**不存在的键**
    #      （死键，永不命中）；而真正要人介入的 blocked / rejected / failed
    #      全部落进兜底分支，提示还指向该脚本**根本没有**的 "summary" 段 ——
    #      等于对最需要处理的情况给了误导提示。
    #      枚举属于**外部事实**（来自 wit），只能比对，不能凭印象维护。
    try:
        import typing

        from wit.acps.provisioner import EnrollmentStatus
        from tools.status import MEANING as _STATUS_MEANING

        real_states = set(typing.get_args(EnrollmentStatus))
        missing = sorted(real_states - set(_STATUS_MEANING))
        dead = sorted(set(_STATUS_MEANING) - real_states)
        if missing or dead:
            if missing:
                print(
                    f"  [失败] tools/status.py 缺 {len(missing)} 个真实状态"
                    f"（这些会落进兜底、显示成「未知状态」）：{missing}"
                )
            if dead:
                print(f"  [失败] tools/status.py 有 {len(dead)} 个不存在的死键：{dead}")
            ok = False
        else:
            print(
                f"  [通过] 状态解释表与 wit 的 EnrollmentStatus 逐字一致"
                f"（{len(real_states)} 个状态全收录、无死键）"
            )

        # 光有"解释表"还不够：**分类**（要不要人工处理）也必须穷尽。
        # 否则 wit 新增一个状态时，它会静默落进"无需处理" —— 与死键同源的病：
        # 都是"外部事实变了，本地看板不吭声"。这里要求三张表合起来正好覆盖全集。
        from tools.status import (
            BENIGN_STATES as _BENIGN,
            SYNTHETIC_ERROR as _SYNTHETIC,
            TROUBLED_STATES as _TROUBLED,
        )

        universe = real_states | {_SYNTHETIC}
        union = _TROUBLED | _BENIGN
        unclassified = sorted(universe - union)
        unknown = sorted(union - universe)
        overlap = sorted(_TROUBLED & _BENIGN)
        problems: list[str] = []
        if unclassified:
            problems.append(f"有状态**未归类**（既不 troubled 也不 benign）：{unclassified}")
        if unknown:
            problems.append(f"归类表里有不存在的状态：{unknown}")
        if overlap:
            problems.append(f"同一状态被同时归为 troubled 与 benign：{overlap}")
        if _SYNTHETIC not in _TROUBLED:
            problems.append(
                f"{_SYNTHETIC}（本脚本合成的异常态）未列入 TROUBLED_STATES"
                " —— 出异常时不会出现在「需要人工处理」汇总里"
            )
        if problems:
            for p in problems:
                print(f"  [失败] 状态分类：{p}")
            ok = False
        else:
            print(
                f"  [通过] 状态分类穷尽且互斥：troubled {len(_TROUBLED)} 个"
                f"（含合成异常态 {_SYNTHETIC}）、benign {len(_BENIGN)} 个"
            )
    except Exception as exc:  # noqa: BLE001
        print(
            f"  [失败] 无法比对状态表（{type(exc).__name__}: {exc}）"
            " —— 要么 wit 的枚举位置变了，要么本机跑不到 tools 包"
        )
        ok = False

    # —— ⑥ 安全判据：Discovery 污染核对**不允许**静默给假的 ✅
    #      `tools/check_discovery_pollution.py` 的全部价值就是回答"污染还在不在"。
    #      早期它只判 `leaked` 为空就打印"无 demo 泄漏 ✅（清理生效）" ——
    #      于是**接口结构一变 / 所有查询都失败 / 全返回 0 条**时，它会把
    #      "什么都没查到"当成"确认没有污染"。这是安全结论里最危险的一类假阳性：
    #      清理没生效，它也说生效了（而本项目**唯一的清理判据**就是这个工具）。
    #      判据现已抽成纯函数 `verdict_of`，这里直接断言它的四种结局 ——
    #      **离线**守着"接口一变就再也不给假的 ✅"。
    try:
        from tools.check_discovery_pollution import RETIRED_AICS, verdict_of

        dead = next(iter(RETIRED_AICS))
        ok_aic = "1.2.156.3088.1.BUPT.81M3FX.COUJXB.F9XC6K.0J6D"  # 不在退役名单里的 AIC
        verdict_cases = [
            ("发现结果里出现已退役 demo → 报污染", {dead: "demo"}, [], 1, False),
            ("全部查询成功且有结果 → 确认无污染", {ok_aic: "正式"}, [], 0, True),
            ("有查询失败 → 无法判定，**不得**给 ✅", {ok_aic: "正式"}, ["事实核查"], 2, False),
            ("所有查询都空 → 无法判定，**不得**给 ✅", {}, [], 2, False),
        ]
        verdict_ok = True
        for label, seen_map, failed, want_code, want_tick in verdict_cases:
            # total_queries 只用于成功行的文案显示，探针里给个固定值即可。
            code, lines = verdict_of(seen_map, failed, 4)
            blob = "\n".join(lines)
            has_tick = "✅" in blob
            good = code == want_code and has_tick == want_tick
            if not good:
                ok = False
                verdict_ok = False
            print(
                f"  [{'通过' if good else '失败'}] 污染核对判据 · {label}："
                f"exit={code}（期望 {want_code}）、含✅={has_tick}（期望 {want_tick}）"
            )
        if verdict_ok:
            print(
                "  [通过] 污染核对：只在「全部查询成功且有结果」时才给 ✅，"
                "查不到一律「无法判定」"
            )
    except Exception as exc:  # noqa: BLE001
        print(
            f"  [失败] 无法加载 check_discovery_pollution.verdict_of（{type(exc).__name__}: {exc}）"
        )
        ok = False

    # —— ⑦ 验收看板不许有"永久假警报"
    #      2026-10-01 实测：`evidence-image`（8449）已定义但**尚未注册到平台**，
    #      于是 `systemd-install.sh verify` **每一次**都会报"有检查项未通过" ——
    #      三条判定全部命中（单元 not-found、开机自启 not-found、port_of 里没有它的端口）。
    #      永久红 == 真失败被淹没在里面，与 ③ 里那条"看板自己在骗人"是同一家族。
    #
    #      判据取"该角色**是否已注册**"（`.wit/<role>/acps/acs.final.json` 在不在）：
    #      平台只能调用已注册的角色 ⇒ "未注册 ⇒ 不该部署"是现场可推导的事实，
    #      不必再造一份"应部署清单"（那本身就是又一次"同一事实散在多处"）。
    #      这里**离线**钉住两件事：判据真的被用上、结论数量不再硬编码。
    board = root / "scripts/systemd-install.sh"
    try:
        board_text = board.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^do_verify\(\)\s*\{(.*?)^\}", board_text, re.M | re.S)
        verify_body = m.group(1) if m else ""
        if not verify_body:
            print(f"  [失败] 在 {board.name} 里找不到 do_verify() 函数体")
            ok = False
        else:
            board_problems: list[str] = []
            # 判据"被真的用上"要看 do_verify() 体内；判据"是什么"看全文
            # （`is_registered()` 定义在函数外，两者范围不同 —— 混成一个范围会
            #   出现"名字改了但判据没改"或"判据在但没用"都能蒙过去的假通过）。
            if "is_registered" not in verify_body:
                board_problems.append(
                    "do_verify() 没有调用注册判据 —— 未注册角色的「未部署」"
                    "会被当成失败，看板将**永远**显示未通过"
                )
            if "acs.final.json" not in board_text:
                board_problems.append(
                    "注册判据不是『存在 acs.final.json』—— 判据写错等于分流写错，"
                    "「未注册」与「单元被误删」会分不开"
                )
            if "${#ROLES[@]}" in verify_body:
                board_problems.append(
                    "do_verify() 结论行用 ${#ROLES[@]} 计数 —— 未部署的角色也会被算进"
                    "「N 个角色」，报出的数字大于实际检查过的"
                )
            if board_problems:
                for p in board_problems:
                    print(f"  [失败] 验收看板：{p}")
                ok = False
            else:
                print("  [通过] 验收看板：未注册角色的「未部署」不算失败，结论数量来自实测")
    except Exception as exc:  # noqa: BLE001
        print(f"  [失败] 无法检查验收看板（{type(exc).__name__}: {exc}）")
        ok = False

    if ok:
        print("\n  [通过] 启停链路卫生：脚本可直接部署，角色清单不会悄悄漂移")
    return ok


async def check_acs() -> bool:
    section("4. ACS 草稿生成（离线）")
    ok = True
    # 同一个闸门（见第 2 节）：这一节也要 `build_config(role=role)`，
    # 缺登记时它同样会先抛栈 —— 而它是**最后**一节，崩在这里等于前面几节
    # 报出来的诊断全被一个 traceback 盖住、连"自检结论"都打印不出来。
    if not _report_unregistered_ports():
        ok = False
    unregistered = set(_roles_without_ports())
    for role in BUILDERS:
        if role in unregistered:
            continue
        agent = BUILDERS[role](DUMMY_MODEL)
        runtime = WitRuntime(
            agent,
            workspace=f".wit/{role}",
            acps=build_config(role=role),
        )
        draft = runtime.acs_draft()
        path = runtime.save_acs()
        print(f"\n  [{role}] 草稿已保存 -> {path}")
        print(f"        protocolVersion={draft['protocolVersion']}  aic={draft['aic'] or '（注册后分配）'}")
        print(f"        active={draft['active']}  endPoints={draft['endPoints'] or '（未设置 public_endpoint）'}")
        for s in draft["skills"]:
            print(f"        skill: {s['id']}  in={s['inputModes']} out={s['outputModes']}")
        result = await runtime.validate()
        status = result.get("status") if isinstance(result, dict) else result
        print(f"        validate() -> {status if status is not None else '无问题'}")
        if isinstance(result, dict) and result.get("diagnostics"):
            print(f"        diagnostics: {result['diagnostics']}")
    return ok


def check_provenance() -> bool:
    """3c. 发起轮台账：**哪一轮是谁发起的**必须能被唯一指认。

    为什么单开一节（2026-10-04 实测，不是推测）：`.wit/leader/acps/aip-tasks.sqlite3`
    里每一行 outbound 的 `senderId` **都是主控自己的 AIC**（`…X1J5R5.HKBBAM.018M`），
    没有 pid、没有 run id、也没有标签 —— 同一天并发跑的四轮（人工真机轮 × 3 +
    自动复查 × 1）在权威库里**长得一模一样**；当时能分清谁是谁，只因为自动复查
    恰好另外维护了一份记忆文件。**那是运气，不是机制。**

    ⚠️ 判据取向：断言必须落在**行为**上。尤其"模型跑着的时候台账是绑着的"这一条 ——
       `agents.write_trace_sidecar` 正是靠 `run_ledger.current_run()` 才知道自己属于
       哪一轮；只要绑定没生效，整条溯源链就断了，而它**不会报错**（只会静默降级）。
       这正是"装了 ≠ 会响"，所以必须用假 runtime 真跑一遍。
    """
    section("3c. 发起轮台账（溯源：哪一轮是谁发起的）")
    ok = True

    import asyncio
    import os
    import tempfile
    from datetime import datetime, timedelta, timezone

    from . import run_ledger as ledger
    from .run_agent import research_run_config, run_research_ledgered
    from tools.aip_sessions import SessionRef

    #: 假 runtime 交出来的**结果字段**。定义一次、四处引用
    #: （两个假 runtime / 两处"返回的记录必须带上它"断言），
    #: 因为它们证明的是**同一条接线**（结果字段有没有从 run 结果走到台账记录里）。
    #: ⚠️ 写死多份就会漂移：一处改成 10、另一处还是 9，"接线断了"就再也测不出来。
    _FAKE_STATUS = "completed"
    _FAKE_ITERS = 9

    class _FakeRuntime:
        """只记录"调用时台账绑没绑"，不做任何真实网络/模型动作。"""

        def __init__(self, *, boom: bool = False, wit_dir=None) -> None:
            self.boom = boom
            self.wit_dir = wit_dir
            self.seen = None
            self.config_seen = None
            self.on_disk = None

        async def run(self, text, config=None):
            self.seen = ledger.current_run()
            self.config_seen = config
            # ⚠️ 在"模型正在跑"的这一刻读一次盘：台账的**存在理由**就是
            #    「先落盘、再跑」——进程随后被 kill 也要留得下痕迹。
            #    只在跑完之后查盘是查不出这件事的（那时收尾记录也在了）。
            if self.wit_dir is not None:
                self.on_disk = ledger.read_runs(wit_dir=self.wit_dir, role="leader")
            if self.boom:
                raise RuntimeError("自检故意抛的异常")
            return type(
                "_Run",
                (),
                {
                    "status": _FAKE_STATUS,
                    "iterations": _FAKE_ITERS,
                    "final_text": "",
                    "observations": [],
                },
            )()

    def _session(sid: str, start: datetime, *, dur: int = 100) -> SessionRef:
        return SessionRef(
            session_id=sid,
            owner_aic="AIC",
            created_at=start,
            updated_at=start + timedelta(seconds=dur),
            created_raw="",
            updated_raw="",
        )

    def _dead_pid() -> int:
        """拿一个**确实已经死掉**的 pid —— 判「进程还在不在」必须有真样本。

        ⚠️ 只 `wait()` 是**不够的**（2026-10-04 实测）：Windows 上 `Popen` 对象
        自己握着进程句柄 ⇒ 进程对象不销毁 ⇒ `os.kill(pid, 0)` 照样成功
        ⇒ 一个死进程会被判成"活着"。必须把对象删掉并 gc，句柄才真正关掉。
        """
        import gc
        import subprocess
        import sys

        child = subprocess.Popen([sys.executable, "-c", ""])
        child.wait()
        pid = child.pid
        del child
        gc.collect()
        return pid

    t0 = datetime(2026, 10, 4, 6, 40, 0, tzinfo=timezone.utc)

    with tempfile.TemporaryDirectory() as tmp:
        # ---- (a) 正常一轮：绑定必须在模型执行期间生效，收尾后必须解绑 ----
        fake = _FakeRuntime(wit_dir=tmp)
        _, rec_a = asyncio.run(
            run_research_ledgered(fake, "自检文案", label="selftest", wit_dir=tmp)
        )
        if fake.seen is None:
            print("  [失败] `runtime.run` 执行期间 `run_ledger.current_run()` 为 None "
                  "⇒ 台账没绑定 ⇒ sidecar 永远拿不到 run_id（且不会报错）")
            ok = False
        elif fake.seen.run_id != rec_a.run_id:
            print(f"  [失败] 执行期间绑的是 {fake.seen.run_id}，收尾记录却是 {rec_a.run_id}")
            ok = False
        # ⚠️ **返回给调用方的必须是"收尾后"那条**。第一版返回的是 `open_run` 那条
        #    （`finished_at` 为空）⇒ 调用方拿它问 `unfinished` 得 True，
        #    把**已经跑完**的轮读成「还在跑」（写这段的自检脚本当场就掉进去了）。
        #    ⚠️ 这条**只能断在 `rec_a` 上**：退回形态下"盘上那条收尾记录"照样是对的
        #    （`close_run` 照写），读回断言与 run_id 断言**全都照样通过** ——
        #    错的只是"交回去的那个对象"。实测过：退回后整节 exit 0、一声没响。
        if rec_a.unfinished or rec_a.finished_at is None:
            print(f"  [失败] 包装返回的是**开轮**记录（`finished_at` 为空）⇒ 调用方会把"
                  f"跑完的轮读成「还在跑」：run_id={rec_a.run_id}")
            ok = False
        elif rec_a.status != _FAKE_STATUS or rec_a.iterations != _FAKE_ITERS:
            print(f"  [失败] 返回的收尾记录没带上结果字段（status={rec_a.status!r}、"
                  f"iterations={rec_a.iterations!r}）⇒ 调用方读不到「这一轮怎么结束的」")
            ok = False
        # ⚠️ 台账的**存在理由**就是「先落盘、再跑」：被 kill 的那一轮正是靠这条
        #    才留得下痕迹。所以判据必须取在**跑的那一刻**（`on_disk` 是假 runtime
        #    在 `run()` 里现场读的盘）—— 跑完之后再查是查不出这件事的，
        #    那时收尾记录也已经在了，看不出来"落盘到底发生在跑之前还是之后"。
        if fake.on_disk is None:
            print("  [失败] 假 runtime 没在跑的时候读一次盘 ⇒ 本断言证明不了任何事")
            ok = False
        elif not any(r.run_id == rec_a.run_id and r.unfinished for r in fake.on_disk):
            print("  [失败] 跑的时候台账里还没有这一轮的**开**记录（读到 "
                  f"{[(r.run_id, r.unfinished) for r in fake.on_disk]}）"
                  " ⇒ 不是「先落盘、再跑」；进程中途被 kill 就什么都不剩"
                  " —— 而那正是这本台账存在的理由")
            ok = False
        if ledger.current_run() is not None:
            print("  [失败] run 结束后台账仍绑着 ⇒ 下一轮会读到上一轮的 run_id")
            ok = False
        # ⚠️ 保真说明：这是一条**接线断言**（逮"台账包装把 config 丢了"），
        #    不是预算**数值**的第二重保险 —— 数值的判据在 2b 节。
        #    它成立的前提是"包装仍然走 run_research"，所以两边都从
        #    `research_run_config()` 这一个来源取，不另写一个字面量。
        if fake.config_seen is None:
            print("  [失败] 台账包装没有把 RunConfig 传给 runtime.run ⇒ "
                  "一次性研判会退回服务模式默认预算（这正是 #93 修的那个洞）")
            ok = False
        elif fake.config_seen.max_steps != research_run_config().max_steps:
            print(f"  [失败] 传下去的预算与 research_run_config() 不一致："
                  f"{fake.config_seen.max_steps} vs {research_run_config().max_steps}")
            ok = False

        runs = ledger.read_runs(wit_dir=tmp, role="leader")
        if len(runs) != 1 or runs[0].status != _FAKE_STATUS or runs[0].iterations != _FAKE_ITERS:
            print(f"  [失败] 台账读回不对：{[(r.run_id, r.status, r.iterations) for r in runs]}")
            ok = False
        elif runs[0].unfinished:
            # 盘上开/收两条都在，折叠后却仍是「未收尾」⇒ 折叠没把收尾字段合上。
            print(f"  [失败] 收尾记录已落盘，读回却是「未收尾」：{runs[0].run_id}"
                  " ⇒ `read_runs` 的按 `run_id` 折叠丢了 `finished_at`")
            ok = False

        # ---- (b) 负对照：异常路径也必须留痕（否则"这一轮怎么没的"无从查起）----
        boom = _FakeRuntime(boom=True)
        raised = False
        try:
            asyncio.run(run_research_ledgered(boom, "炸", label="boom", wit_dir=tmp))
        except RuntimeError:
            raised = True
        if not raised:
            print("  [失败] 假 runtime 抛异常却没传出来（自检本身失效）")
            ok = False
        boom_rec = [
            r for r in ledger.read_runs(wit_dir=tmp, role="leader") if r.label == "boom"
        ]
        if not boom_rec or not (boom_rec[0].status or "").startswith("raised:"):
            print(f"  [失败] 异常轮次在台账里没有留痕：{[r.status for r in boom_rec]}")
            ok = False
        if ledger.current_run() is not None:
            print("  [失败] 异常路径没有解绑台账")
            ok = False

        # ---- (c) **已收尾**的并发两轮：各认各的会话 ----
        with tempfile.TemporaryDirectory() as tmp2:
            r1 = ledger.open_run(label="manual", role="leader", text="一", wit_dir=tmp2, now=t0)
            r1 = ledger.close_run(r1, status="completed", wit_dir=tmp2, now=t0 + timedelta(seconds=90))
            r2 = ledger.open_run(
                label="automation", role="leader", text="二", wit_dir=tmp2,
                now=t0 + timedelta(minutes=13),
            )
            r2 = ledger.close_run(
                r2, status="completed", wit_dir=tmp2, now=t0 + timedelta(minutes=16)
            )
            sess = [_session("s-one", t0 + timedelta(seconds=20)),
                    _session("s-two", t0 + timedelta(minutes=13, seconds=30))]
            claimed, contested = ledger.attribute_sessions([r1, r2], sess)
            if claimed != {"s-one": r1.run_id, "s-two": r2.run_id} or contested:
                print(f"  [失败] 并发两轮未能各认各的会话：唯一={claimed} 争议={contested}")
                ok = False

            # ---- (d) 负对照：两轮窗口都覆盖同一条会话 ⇒ 必须判"有争议"、**不许二选一** ----
            r3 = ledger.open_run(
                label="overlap", role="leader", text="三", wit_dir=tmp2,
                now=t0 + timedelta(minutes=13, seconds=10),
            )
            claimed2, contested2 = ledger.attribute_sessions([r2, r3], sess)
            if "s-two" not in contested2 or "s-two" in claimed2:
                print("  [失败] 两轮窗口重叠时仍然替人二选一了 —— 这正是要禁止的猜测："
                      f"唯一={claimed2} 争议={contested2}")
                ok = False

            # ---- (e) 未收尾的轮次：右端必须**开放**（它可能还在跑）----
            r4 = ledger.open_run(
                label="open", role="leader", text="四", wit_dir=tmp2,
                now=t0 + timedelta(minutes=40),
            )
            claimed3, _ = ledger.attribute_sessions(
                [r4], [_session("s-late", t0 + timedelta(hours=3))]
            )
            if claimed3 != {"s-late": r4.run_id}:
                print("  [失败] 未收尾轮次的窗口右端没有开放 ⇒ 还在跑的那一轮会判成"
                      f"「没有归属」：{claimed3}")
                ok = False

            # ---- (f) **单轮视角也必须看得见别的轮** ----
            #    第一版 `session_for_run(run, …)` 内部只传自己那一轮（`[run]`）⇒
            #    窗口列表只剩一条、`len(hits)` 恒为 1 ⇒ "有争议"那条分支**永远走不到**
            #    ⇒ 并发轮次下照样替人二选一 —— 而 `write_trace_sidecar` 走的正是
            #    这个函数，等于**模块要防的那件事在它自己的关键路径上没防住**。
            #    （2026-10-04 复查实测抓到；本断言即它的回归防线。）
            #    ⚠️ 判据落在行为上：**只传自己**时会认领（这是预期），
            #       **带上重叠的另一轮**时必须拒绝。两条都要断，否则"永远拒绝"也过。
            solo, _why_solo = ledger.session_for_run([r2], r2.run_id, sess)
            both, why_both = ledger.session_for_run([r2, r3], r2.run_id, sess)
            if solo != "s-two":
                print(f"  [失败] 单轮视角的前置不成立（期望认领 s-two，实得 {solo!r}）"
                      " ⇒ 本断言证明不了任何事")
                ok = False
            elif both is not None:
                print(f"  [失败] `session_for_run` 带上重叠的另一轮后仍认领了 {both!r}"
                      " ⇒ 它内部又把判据的输入裁到只剩自己了")
                ok = False
            elif "别的轮次" not in why_both:
                print(f"  [失败] 拒绝认领的理由没指名道姓（含'别的轮次'才对）：{why_both!r}")
                ok = False

    # ---- (j) `read_runs` 必须按**绝对时刻**排，不许按 `started_at` 字符串 ----
    #    同一秒内两种 ISO 形态会**排反**：`…07:22:11.4Z` 里的 `.`(0x2E) 小于
    #    `…07:22:11Z` 里的 `Z`(0x5A)，于是**晚发生的那一轮排到前面**。
    #    这不只是显示问题：顺序决定"谁是后一轮"，而那是认领与审计都要读的。
    #    ⚠️ 判据必须构造**字符串序与时间序相反**的那一对（只差微秒、一个带小数
    #      一个不带）。拿两条"按时间排也恰好按字符串排"的样本是**测不出来**的。
    with tempfile.TemporaryDirectory() as tmp3:
        def _raw_line(run_id: str, started_at: str) -> str:
            # ⚠️ 用 `RunRecord.to_json()` 现场生成，**不手写那十几个键** ——
            #    手写就等于把记录的形状抄了第二遍，加一个字段就漂移一次。
            record = ledger.RunRecord(
                run_id=run_id, role="leader", label="x", pid=1, ppid=1,
                started_at=started_at, text_sha256="", text_head="", argv=(),
            )
            return json.dumps(record.to_json(), ensure_ascii=False, sort_keys=True)

        order_file = ledger.ledger_path(tmp3, "leader")
        order_file.parent.mkdir(parents=True, exist_ok=True)
        order_file.write_text(
            _raw_line("early-plain", "2026-10-04T07:22:11Z") + "\n"
            + _raw_line("late-frac", "2026-10-04T07:22:11.4Z") + "\n",
            encoding="utf-8", newline="\n",
        )
        got_order = [r.run_id for r in ledger.read_runs(wit_dir=tmp3, role="leader")]
        if got_order != ["early-plain", "late-frac"]:
            print("  [失败] `read_runs` 没按**绝对时刻**排：按 `started_at` 字符串排会把"
                  f"晚发生的那一轮排到前面（实得 {got_order}，应为 "
                  "['early-plain', 'late-frac']）")
            ok = False

    # ---- (i) **全部真机探针**也必须走同一份台账 ----
    #    三个探针（`run/e2e_leader_image`、`run/e2e_video_live`、`run/e2e_audio_live`）
    #    直驱 `runtime.run`，**不经过** `run_research_ledgered`。2026-10-04 实测：
    #    跑完一轮探针，台账仍然只有 2 条 —— 台账保留了它本来要消灭的那个盲区，
    #    只是盲区从「人工轮 vs 自动复查轮」挪到了「人工/自动 vs 探针」。
    #
    #    ⚠️ 本节的**输入集合是现场派生的，不是手写名单**（2026-10-05 改）：
    #       `run/e2e_*.py` 按**目录**收，逐个校验"暴露了 `run_probe()` **且** 登记进
    #       `_PROBES` 表"。原先只写死两个模块名 —— 那正是「登记表写死 = 静默漏项」：
    #       音频侧整场没有 e2e 脚本（#122），而"没人核"与"核过了没问题"长得一模一样。
    #       ⚠️ 加一个探针**只加一行**（`_PROBES` 里那一行），其余断言全部按表走 ——
    #          "给每个探针各抄一段断言"是漏项发生的地方（复制到第三个时就会漏掉一条）。
    #
    #    探针与一次性研判有两点差别，断言必须**按这两点**取，否则"覆盖了探针"只是句口号：
    #      ① 输入是**部件列表**（带附件），不是纯文本；
    #      ② 角色**由探针自己决定** —— `e2e_video_live` 跑的是叶子角色
    #         `evidence-video`，而两个带附件的 leader 探针跑的是 `leader`
    #         ⇒ 台账必须落到 `.wit/<role>/runs.jsonl`；**落错角色不会报错**，
    #            只会把这一轮记到别人的文件里（所以必须正反两面都断）。
    #         ⚠️ 于是 `leader` 那一本必须容忍**两个**探针各自一条记录，
    #            而"两条都还在"本身是个新事实：第二个 leader 探针**没有**把
    #            第一个覆盖掉（`runs.jsonl` 是只增不改的，折叠按 `run_id`）。
    #
    #    ⚠️ 判据落在**行为**上：调的是探针自己的 `run_probe()`，不是自检另写一段等价代码。
    #       退化成"grep 源码里有没有 run_ledgered"是不行的 —— 本项目写死过这条：
    #       "grep 到了"并不等于"运行时真会走那条路"。
    with tempfile.TemporaryDirectory() as tmp4:
        import sys as _sys

        # 先量一次：import 探针**不许**把重依赖拉进自检进程（否则这一节在缺 Pillow /
        # numpy 的机器上会把自检本身搞崩 —— 那是最糟的失败形态：判据成了故障源）。
        # ⚠️ 这里的"重依赖"名单要跟着探针一起长：音频探针用的是
        #    `run/audio_samples.py`（numpy + `wave`），图像/视频那两个用的是
        #    `run/samples.py`（numpy + Pillow）。名单短了 = 这条判据自己漏项。
        _HEAVY = ("numpy", "PIL")
        _before = {m for m in _HEAVY if m in _sys.modules}
        try:
            from run import e2e_audio_live as probe_aud
            from run import e2e_combined as probe_comb
            from run import e2e_fast_lane as probe_fast
            from run import e2e_lead_intake as probe_lead
            from run import e2e_leader_image as probe_img
            from run import e2e_stance_swap as probe_swap
            from run import e2e_video_live as probe_vid
        except Exception as exc:  # noqa: BLE001 - 起不来是**失败**，不是"跳过"
            print(f"  [失败] 无法 import 真机探针（{type(exc).__name__}: {exc}）"
                  " ⇒ 本节只能作罢，而'静态检查'证明不了'运行时真会走那条路'。"
                  "最常见原因：不在仓库根目录下跑自检（探针按 `run.<模块>` 导入）")
            probe_img = probe_vid = probe_aud = probe_lead = probe_fast = probe_swap \
                = probe_comb = None
            ok = False
        _leaked = sorted({m for m in _HEAVY if m in _sys.modules} - _before)
        if _leaked:
            print(f"  [失败] import 探针把重依赖拉进了自检进程：{_leaked}"
                  " ⇒ 探针里的 `run.samples` / `run.audio_samples` 被挪回了模块级导入。"
                  "挪回去**不会报错**，只会让自检在缺 Pillow / numpy 的机器上起不来")
            ok = False

        #: 探针 → (跑哪个角色, 要不要传 RunConfig)。**这张表就是判据的输入集合。**
        #: ⚠️ 角色必须与探针**真正**跑的角色一致 —— 写错了不会报错，
        #:    只会替"这一轮记到了别人的文件里"背书。
        _PROBES: list[tuple[str, object, str, bool]] = [
            ("run/e2e_audio_live.py", probe_aud, "leader", True),
            # 组合研判（#170–#173）：主控带图+文字主张两链并发。跑 **leader**
            # （fact-judge / evidence-leader 走常驻服务），预算用 research 的
            # 14 步（组合分支 = 2 discover + 2 delegate + 等待 + 交付）⇒ needs_config=True。
            ("run/e2e_combined.py", probe_comb, "leader", True),
            ("run/e2e_fast_lane.py", probe_fast, "leader", True),
            ("run/e2e_lead_intake.py", probe_lead, "leader", True),
            ("run/e2e_leader_image.py", probe_img, "leader", True),
            # 立场对调双跑实验（#124）：跑的是 **fact-judge**（进程内起裁判、
            # 辩手走常驻），预算刻意用服务默认 8 步（RunConfig() 默认值）而
            # 非 research 的 14 步 ⇒ needs_config=False（3c 对 False 行不查
            # config——它查的是 research 预算有没有传丢，本探针不用那份预算）。
            ("run/e2e_stance_swap.py", probe_swap, "fact-judge", False),
            ("run/e2e_video_live.py", probe_vid, "evidence-video", False),
        ]

        # ---- 清单完整性：`run/e2e_*.py` **按目录现场派生**，逐个必须在表里 ----
        #    ⚠️ 这条守卫针对的正是本节自己的病：**手写名单必然漏项**。音频侧整场
        #       没有 e2e 脚本（#122），而"没人核"与"核过了没问题"长得一模一样。
        #       守卫只保证"新探针不会悄悄溜过去"，保证不了"该有的探针根本没有文件"
        #       —— 后者只能靠人，所以这里**明说**它管不到，别让人误以为它管得到。
        #    ⚠️ 这里**故意只读源码文本、不 import**：import 一个尚未登记的模块
        #       可能把 numpy 拉进来（污染上面那条泄漏判据）、也可能在缺依赖的机器上
        #       直接把自检搞崩 —— 判据不许自己成为故障源。
        #       这不违反"判据不许退化成 grep 源码"：**行为判据**仍然是下面
        #       真跑 `run_probe()` 那一段，这里只是"清单有没有登记"。
        _root = Path(__file__).resolve().parents[1]
        _probe_files = sorted(p.name for p in (_root / "run").glob("e2e_*.py"))
        _registered = {Path(name).name for name, _m, _r, _c in _PROBES}
        _unregistered = [n for n in _probe_files if n not in _registered]
        _vanished = sorted(_registered - set(_probe_files))
        if not _probe_files:
            print("  [失败] `run/e2e_*.py` 一个都没派生出来 ⇒ 本节的输入集合是空的，"
                  "守卫在空转（`all([])` 是 True）")
            ok = False
        if _unregistered:
            print(f"  [失败] 有真机探针没登记进 3c 的 `_PROBES` 表：{_unregistered}"
                  " ⇒ 它的台账行为**没有任何判据盯着**（而「没人核」与"
                  "「核过了没问题」长得一模一样）。加一行即可")
            ok = False
        if _vanished:
            print(f"  [失败] `_PROBES` 里登记了不存在的探针文件：{_vanished}"
                  " ⇒ 名单比磁盘旧，这一行永远「驱动」不了任何东西（静默空转）")
            ok = False
        for name in _probe_files:
            text = (_root / "run" / name).read_text(encoding="utf-8")
            if "def run_probe(" not in text:
                print(f"  [失败] run/{name} 没有 `run_probe()` ⇒ 自检驱动不了它，"
                      "它的台账行为只能靠人肉检查（而「看起来对」证明不了「跑起来对」）")
                ok = False

        class _FakePartsRuntime:
            """假 runtime：入参是**部件列表**（探针走的就是这条路），不是纯文本。"""

            def __init__(self) -> None:
                self.seen = None
                self.parts_seen = None
                self.config_seen = None

            async def run(self, parts, config=None):
                self.seen = ledger.current_run()
                self.parts_seen = parts
                self.config_seen = config
                # ⚠️ 这里交出去的结果字段**必须真被断言到**（见下面 `rec` 那一段）。
                #    第一版交的是 `{"status": "completed", "iterations": 5}`，
                #    而全节没有一行读过它 —— 那是**死数据**：看上去在测，
                #    实际上"结果字段有没有走到台账记录里"在这条路上完全没人管。
                return type(
                    "_Run", (), {"status": _FAKE_STATUS, "iterations": _FAKE_ITERS}
                )()

        #: 本轮真正被驱动的行（import 失败的行不在里面，那种情况 `ok` 已经是 False）。
        _drive_rows = [row for row in _PROBES if row[1] is not None]

        if _drive_rows:

            async def _drive(mod, *, role, **kw):
                """驱动**探针自己的** `run_probe()`，返回 `(假 runtime, 它返回了什么)`。"""
                fake = _FakePartsRuntime()
                got = await mod.run_probe(
                    fake, ["部件甲", "部件乙"], "自检文案",
                    role=role, wit_dir=tmp4, **kw,
                )
                return fake, got

            def _record(probe_name: str, got: object):
                """探针必须返回 `(结果, 台账记录)`；形状不对 ⇒ 报失败并返回 None。

                ⚠️ 这一段**必须单独存在**，不能直接 `_, rec = got` 解包：
                   元验证的退回形态正是"只返回 run 结果"（= #97 之前的写法），
                   解包会抛 `TypeError` 被外层 `except` 吞成一句"抛异常了" ——
                   失败是响了，但**响的理由不对**：那证明的是"退回后代码崩了"，
                   而不是"这条判据抓到了'没记台账'"。
                """
                if not (isinstance(got, tuple) and len(got) == 2):
                    print(f"  [失败] {probe_name} 的 run_probe() 返回的是 "
                          f"{type(got).__name__}，不是 (结果, 台账记录)"
                          " ⇒ 这一轮没进台账（#97 之前它就是这样直驱 runtime.run 的）")
                    return None
                return got[1]

            _fakes: dict[str, object] = {}
            _recs: dict[str, object] = {}
            for _name, _mod, _role, _needs_config in _drive_rows:
                _kw = {"config": research_run_config()} if _needs_config else {}
                try:
                    _fake, _got = asyncio.run(_drive(_mod, role=_role, **_kw))
                except Exception as exc:  # noqa: BLE001
                    print(f"  [失败] 驱动 {_name} 的 run_probe() 抛异常"
                          f"（{type(exc).__name__}: {exc}）")
                    ok = False
                    continue
                _fakes[_name] = _fake
                _recs[_name] = _record(_name, _got)
                if _recs[_name] is None:
                    ok = False

            # ---- 逐个探针：绑定 / 标签 / 收尾形态 / 结果字段 / 附件预算 ----
            for _name, _mod, _role, _needs_config in _drive_rows:
                fake = _fakes.get(_name)
                rec = _recs.get(_name)
                if fake is None:
                    continue  # 已经在上面的 except 里报过了
                if fake.seen is None:
                    print(f"  [失败] {_name} 探针执行期间台账没绑住"
                          " ⇒ 探针轮次在溯源链上是隐形的")
                    ok = False
                elif rec is not None and fake.seen.run_id != rec.run_id:
                    print(f"  [失败] {_name} 执行期间绑的是 {fake.seen.run_id}，"
                          f"收尾记录却是 {rec.run_id}")
                    ok = False
                if rec is not None and rec.label != ledger.PROBE_LABEL:
                    print(f"  [失败] {_name} 记的标签是 {rec.label!r}，"
                          f"应为 {ledger.PROBE_LABEL!r} —— 探针轮会与"
                          "「没自报的人工轮」混成一类")
                    ok = False
                # ⚠️ 探针那条路也必须交回**收尾后**的记录，并带上结果字段。
                #    与 (a) 同样的道理，但**不能靠 (a) 顶替**：那条走的是
                #    `run_research_ledgered`，这条走的是探针自己的 `run_probe()`
                #    —— 「同一份实现」是现在的巧合，不是判据能依赖的前提。
                if rec is not None and (rec.unfinished or rec.finished_at is None):
                    print(f"  [失败] {_name} 的 run_probe() 返回的是**开轮**记录"
                          "（`finished_at` 为空）⇒ 调用方会把跑完的探针轮读成「还在跑」")
                    ok = False
                elif rec is not None and (
                    rec.status != _FAKE_STATUS or rec.iterations != _FAKE_ITERS
                ):
                    print(f"  [失败] {_name} 返回的收尾记录没带上结果字段"
                          f"（status={rec.status!r}、iterations={rec.iterations!r}）"
                          " ⇒ 探针轮只知道「跑过」，不知道跑成什么样")
                    ok = False
                if fake.parts_seen is None or len(fake.parts_seen) != 2:
                    print(f"  [失败] {_name} 没把**部件列表**原样交给 runtime"
                          f"（实收 {fake.parts_seen!r}）")
                    ok = False
                # 附件预算必须仍然传下去（带附件的探针这条路上也得有这条接线断言）：
                # 传丢了**不会报错**，只会让探针轮退回服务模式默认预算（= #93 那个洞）。
                if _needs_config and fake.config_seen is None:
                    print(f"  [失败] {_name} 没有把 RunConfig 传给 runtime.run ⇒ "
                          "会退回服务模式默认预算（这正是 #93 修的那个洞）")
                    ok = False
                elif _needs_config and (
                    fake.config_seen.max_steps != research_run_config().max_steps
                ):
                    print(f"  [失败] {_name} 传下去的预算与 research_run_config() 不一致："
                          f"{fake.config_seen.max_steps} vs "
                          f"{research_run_config().max_steps}")
                    ok = False

            # ---- 角色路由：**正反两面都断** ----
            #    只断"记到 evidence-video 了"是不够的：若实现把角色**写死成 leader**，
            #    那一面会同时成立而不被发现 —— 所以还要断 `.wit/leader` 那一本里
            #    **恰好**是那两个 leader 探针的记录（不多、不少、不重）。
            #    ⚠️ 期望值按表**现场派生**，不写死个数：两个 leader 探针各自一条，
            #       `read_runs` 按 `run_id` 折叠开/收两行 ⇒ 这里同时盯着
            #       "第二个 leader 探针有没有把第一个覆盖掉"。
            _want: dict[str, list[str]] = {}
            for _name, _mod, _role, _needs_config in _drive_rows:
                if _recs.get(_name) is not None:
                    _want.setdefault(_role, []).append(_recs[_name].run_id)
            for _role, _ids in sorted(_want.items()):
                _got = [
                    r.run_id for r in ledger.read_runs(wit_dir=tmp4, role=_role)
                ]
                if sorted(_got) != sorted(_ids):
                    print(f"  [失败] 探针轮次没按角色落盘：`.wit/{_role}/runs.jsonl` 里是 "
                          f"{sorted(_got)}，应为 {sorted(_ids)}"
                          " —— 角色写死成 leader、或同一角色上互相覆盖就会这样")
                    ok = False
            # ⚠️ 泄进**任何**无关角色都算失败，不是只查一个写死的 `fact-judge`：
            #    名字写死就会漏掉新加的无关角色（那正是本节刚犯过的错）。
            _stray = sorted(
                p.parent.name
                for p in Path(tmp4).glob("*/runs.jsonl")
                if p.parent.name not in _want
            )
            if _stray:
                print(f"  [失败] 探针的轮次泄进了无关角色的台账：{_stray}")
                ok = False

        if ledger.current_run() is not None:
            print("  [失败] 探针跑完后台账仍绑着 ⇒ 下一轮会读到上一轮的 run_id")
            ok = False

    # ---- (g) 台账/会话库（**仓库相对**）与运行时 workspace（**CWD 相对**）必须同根 ----
    #    台账是仓库相对的（与 sidecar 读会话库的 `WIT_DIR` 同根），而
    #    `WitRuntime(workspace=".wit/leader")` 是 CWD 相对的 ⇒ 二者只在
    #    "CWD == 仓库根"时重合。不重合时**不报错**，只是静默退回旧口径 ——
    #    所以这条前提必须有断言盯着，不能靠祈祷。**现场派生，不写死路径。**
    from .agents import WIT_DIR

    db_repo = (WIT_DIR / "leader" / "acps" / "aip-tasks.sqlite3").resolve()
    db_cwd = Path(".wit/leader/acps/aip-tasks.sqlite3").resolve()
    if db_repo != db_cwd:
        print("  [失败] 仓库相对根与 CWD 相对根不是同一个 ⇒ 换个目录跑，台账就会与 "
              f"sidecar 读的那份会话库分家（表现是静默退回旧口径）：{db_repo} vs {db_cwd}")
        ok = False

    # ---- (h) 默认标签**不许替调用方猜归属** ----
    saved = os.environ.pop("ACPS_RUN_LABEL", None)
    try:
        if ledger.label_from_env() != "unlabeled":
            print(f"  [失败] 未设 ACPS_RUN_LABEL 时默认标签是 {ledger.label_from_env()!r}，"
                  "应为 'unlabeled' —— 默认成 manual 就是替调用方猜了一个归属")
            ok = False
    finally:
        if saved is not None:
            os.environ["ACPS_RUN_LABEL"] = saved

    # ---- (k) 人肉入口的「未收尾」必须按 `finished_at` 判，**不按 `status`** ----
    #    通用包装允许结果对象不带 status（那时如实记 `None`），而那种轮次**已经收尾**；
    #    按 status 判会把它显示成「未收尾」—— **一条错的显示比 `?` 更糟**：
    #    它会让人去找一个根本不存在的僵尸进程。
    #    ⚠️ 判据取在**入口的真实输出**上：另写一份等价判断顶替不了"它真的被用上了"
    #      （本项目那条：「送出去」与「被采纳」是两件事）。
    with tempfile.TemporaryDirectory() as tmp5:
        r_open5 = ledger.open_run(label="o", role="leader", text="一", wit_dir=tmp5, now=t0)
        r_nostatus5 = ledger.open_run(label="n", role="leader", text="二", wit_dir=tmp5, now=t0)
        ledger.close_run(r_nostatus5, status=None, wit_dir=tmp5, now=t0)
        r_done5 = ledger.open_run(label="d", role="leader", text="三", wit_dir=tmp5, now=t0)
        ledger.close_run(r_done5, status="completed", wit_dir=tmp5, now=t0)

        # ⚠️ 再加一条**进程已死**的未收尾轮次（#95 第 4 项的口径）：它必须显示成
        #    「中断」而不是「未收尾」。两者混成一个字，前者会让人去等一个**永远不会
        #    再收尾**的进程，后者会让人把一个还在跑的轮当成死轮去回收。
        #    这条记录直接手写一行落盘（`open_run` 用的是本进程的 pid，写不出"已死"）。
        ghost5 = ledger.RunRecord(
            run_id="selftest-ghost", role="leader", label="ghost", pid=_dead_pid(), ppid=1,
            started_at=t0.isoformat().replace("+00:00", "Z"),
            text_sha256="", text_head="幽灵轮", argv=(),
        )
        with open(ledger.ledger_path(tmp5, "leader"), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(ghost5.to_json(), ensure_ascii=False, sort_keys=True) + "\n")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc5 = ledger._main(["--wit-dir", tmp5, "--role", "leader"])
        text5 = buf.getvalue()

        def _row_of(run_id: str) -> str:
            return next((ln for ln in text5.splitlines() if ln.startswith(run_id)), "")

        if rc5 != 0:
            print(f"  [失败] 人肉入口返回了 {rc5}（应为 0）")
            ok = False
        rows = {
            "真正未收尾的轮": _row_of(r_open5.run_id),
            "已收尾但无 status": _row_of(r_nostatus5.run_id),
            "已收尾且有 status": _row_of(r_done5.run_id),
            "未收尾但进程已死": _row_of(ghost5.run_id),
        }
        blank = [k for k, v in rows.items() if not v]
        if blank:
            print(f"  [失败] 人肉入口没打出这几行：{blank} ⇒ 本断言证明不了任何事")
            ok = False
        else:
            if "(未收尾)" not in rows["真正未收尾的轮"]:
                print("  [失败] 真正没跑完的那一轮没显示成「未收尾」"
                      f"（它右侧该开放）：{rows['真正未收尾的轮']!r}")
                ok = False
            if "(未收尾)" in rows["已收尾但无 status"] or "?" not in rows["已收尾但无 status"]:
                print("  [失败] **已收尾**、只是结果对象没带 status 的轮被显示成「未收尾」"
                      f"（改成按 `status` 判就会这样）：{rows['已收尾但无 status']!r}"
                      " ⇒ 会让人去找一个根本不存在的僵尸进程")
                ok = False
            if "completed" not in rows["已收尾且有 status"]:
                print("  [失败] 有 status 的已收尾轮没显示它的 status："
                      f"{rows['已收尾且有 status']!r}")
                ok = False
            if "(中断)" not in rows["未收尾但进程已死"]:
                print("  [失败] **未收尾、但进程已经不在了**的轮没显示成「中断」"
                      f"（实得 {rows['未收尾但进程已死']!r}）⇒ 它与「还在跑」被混成一类："
                      "要么让人去等一个永远不会收尾的进程，"
                      "要么让人把一个还在跑的轮当成死轮去回收")
                ok = False

    # ---- (l0) 两个包装的入参形态不同，**传错必须在入口就响亮拒绝** ----
    #    两个名字几乎一样的包装收的东西不一样：
    #      · `run_research_ledgered(runtime, …)` —— **带 `.run` 的对象**；
    #      · `run_ledger.run_ledgered(runner, …)` —— **零参工厂**。
    #    2026-10-04 本自检自己踩了两次。传错**不是**出一句清楚的报错，而是深处
    #    那句 `AttributeError: 'function' object has no attribute 'run'`，
    #    它会被下面 (l) 的「记账失败必须不抛」当成**记账的锅**报出来 ——
    #    判据响了，但**响的理由是错的**，排查的人会去读 `run_ledger` 找那个
    #    根本不存在的缺陷，而真正写错的那一行就在他眼皮底下。
    #
    #    ⚠️ 这条判据断的是**拒绝的形态**，不是"会不会抛"：光抛异常没用 ——
    #       `AttributeError` 也抛。必须断**类名**与**出路**都在，否则等于没写。
    try:
        # ⚠️ 这个探针**必须**自带一个临时 `wit_dir`：守卫在的时候参数根本走不到
        #    `wit_dir`（第一句就抛了）；可一旦守卫被退回（元验证用例 R 干的正是
        #    这件事），这一轮就会**真的**走进 `run_ledgered` —— 不传的话它用仓库
        #    默认根，也就是往**真实台账** `.wit/leader/runs.jsonl` 里写两条垃圾
        #    记录、还要去抢真锁（本地只是凭空造出那个文件，**服务器上那是权威
        #    溯源文件**）。而 `.wit` 被 `tools/tree_diff` 明确排除
        #    （"运行期数据，每次跑都会变"）⇒ 这种污染**在任何「批前批后全树比对」
        #    里都看不见**，所以只能在这里自己断住它。
        with tempfile.TemporaryDirectory() as tmp_l0:
            asyncio.run(
                run_research_ledgered(
                    lambda: None,  # 故意传一个回调（零参工厂的形态）
                    "自检：入参形态",
                    label="lf0",
                    wit_dir=tmp_l0,
                )
            )
        print("  [失败] 往 `run_research_ledgered()` 里传回调**没有在入口被拒**"
              " ⇒ 使用者要等到深处那句 `'function' object has no attribute 'run'`，"
              "而它会被读成记账模块的缺陷")
        ok = False
    except TypeError as exc:
        if "run_ledger.run_ledgered" not in str(exc):
            print("  [失败] 拒绝的理由没指出**可执行的出路**（该用哪个函数）"
                  f"：{exc} ⇒ 只是把 `AttributeError` 换了个类名，排查成本没降")
            ok = False
    except BaseException as exc:  # noqa: BLE001
        print(f"  [失败] 入参形态不对时抛的是 {type(exc).__name__}（应为 TypeError）"
              f"：{exc}")
        ok = False

    # ---- (l) 记账写失败：**只降级，绝不改主流程**（#101）----
    #    台账是**旁路记录**（模块自己的说法）。可它在三个出口上都曾能改变主流程：
    #      ① `open_run` 在任何 `try` 之外 ⇒ 磁盘满/只读挂载会让 `runner`
    #         **一次都不被调用** —— 一轮本来能做成的研判，因为一本旁路账而彻底不做；
    #      ② 收尾一抛 ⇒ 调用方**永远拿不到**那个已经正确落盘的产物，
    #         还会看到"这一轮失败了"—— 一条**错信号**；
    #      ③ 异常路径里再抛 ⇒ 把"这一轮为什么失败"**顶掉**成记账的失败，
    #         排查的人会去追一个与失败无关的原因。
    #
    #    ⚠️ 判据必须落在**行为**上：runner 到底被没被调用、result 到底交没交回、
    #      抛出来的到底是**哪个**异常 —— 三条都不能靠读代码判断。
    #    ⚠️ ③ 与 ② 共用同一处退回形态（`close_run` 的落盘降级）：**一处退回同时证明
    #      两条判据**。这不是偷懒，是因为"原异常被顶掉"本来就是"收尾会抛"的后果；
    #      分开写两处退回反而会让人以为它们是两个独立的修复。
    real_append = ledger._append

    def _dead_append(path, record):  # noqa: ANN001 - 自检用的假落盘
        raise OSError("自检模拟：磁盘满 / 只读")

    with tempfile.TemporaryDirectory() as tmp_l1:
        # ① 开记录写不进去 ⇒ `runner` **仍须被调用**
        #
        #    ⚠️ 两个包装的入参形态**不一样**，这里踩过一次：
        #       `run_research_ledgered(runtime, …)` 收的是**带 `.run` 的对象**；
        #       `run_ledger.run_ledgered(runner, …)` 收的才是**零参工厂**
        #       （工厂是刻意的：协程对象一造出来就被排期，"先落盘再跑"会失效）。
        #       把回调传进前者会当场抛 `AttributeError: 'function' object has
        #       no attribute 'run'`，而它**恰好会被下面"记账失败必须不抛"那条判据
        #       抓成「整轮直接抛了异常」** —— 判据响了，但响的理由是错的，
        #       同时 `calls6["n"]` 会是 0，两条失败一起刷屏，真正的信号被淹掉。
        #       **自检自己写错，等于判据没写。**
        calls6 = {"n": 0}

        class _CountingRuntime(_FakeRuntime):
            """给 `_FakeRuntime` 叠一个调用计数。

            ⚠️ 用**子类**、不另写一个假 runtime：结果字段只许有一处定义
               （见 `_FAKE_STATUS` 上方那段注释），另写一份迟早漂移 ——
               一处改成 10、另一处还是 9，"接线断了"就再也测不出来。
            """

            async def run(self, text, config=None):  # noqa: ANN001
                calls6["n"] += 1
                return await super().run(text, config=config)

        rec1 = None
        ledger._append = _dead_append
        try:
            _res1, rec1 = asyncio.run(
                run_research_ledgered(
                    _CountingRuntime(), "记账甲", label="lf1", wit_dir=tmp_l1
                )
            )
        except Exception as exc:  # noqa: BLE001
            print("  [失败] 记账写不进去时整轮直接抛了异常"
                  f"（{type(exc).__name__}: {exc}）—— 一本**旁路账**把一轮研判变成了"
                  "一次失败，与模块自己写的「台账是旁路记录」直接矛盾")
            ok = False
        finally:
            ledger._append = real_append
        if calls6["n"] != 1:
            print(f"  [失败] 台账写不进去时 `runner` 一次都没被调用（实调 {calls6['n']} 次）"
                  " ⇒ 一轮本来能做成的研判因为**旁路账**而彻底不做")
            ok = False
        if rec1 is not None and not (rec1.ledger_error or ""):
            print("  [失败] 记账失败被**静默**吞掉了：返回的记录里没有 `ledger_error`"
                  f"（实得 {rec1.ledger_error!r}）⇒ 没人知道这一轮的溯源缺了一环")
            ok = False

    # ② 只有收记录写不进去 ⇒ 调用方**仍须拿到 `result`**
    #
    #    ⚠️ **每一小节各用一个自己的临时目录**（2026-10-04 实测踩到）：
    #       退回 `close_run` 的落盘降级之后，①的收尾会抛 ⇒
    #       `run_ledgered` 异常分支里那句 `_release_run_lock_quietly` **走不到**
    #       ⇒ **锁泄漏**；于是同一个 `wit_dir` 里的 ② 与 ③ 会被轮次锁挡住、
    #       压根跑不起来，而 ③ 的那条 `except RuntimeError` **正好把 `RunLocked`
    #       一起吞掉**（`RunLocked` 就是 `RuntimeError` 的子类）——
    #       结果：③ 一声不响地「通过」，而它测的东西已经没了。
    #       ⇒ 判据之间**不许共用会产生残留的目录**：一个小节的失败形态
    #         一旦能改变下一小节的前提，后面的判据就全部作废。
    seq6 = {"n": 0}

    def _flaky_append(path, record):  # noqa: ANN001
        seq6["n"] += 1
        if seq6["n"] >= 2:
            raise OSError("自检模拟：磁盘满 / 只读")
        return real_append(path, record)

    with tempfile.TemporaryDirectory() as tmp_l2:
        rec2 = None
        got2 = None
        ledger._append = _flaky_append
        try:
            got2, rec2 = asyncio.run(
                run_research_ledgered(_FakeRuntime(), "记账乙", label="lf2", wit_dir=tmp_l2)
            )
        except Exception as exc:  # noqa: BLE001
            print("  [失败] 收尾记账失败时调用方**拿不到这一轮的结果**：包装把记账的"
                  f"异常抛了出来（{type(exc).__name__}: {exc}）"
                  " ⇒ 产物其实已经正确落盘，却被报成了失败（**错信号**）")
            ok = False
        finally:
            ledger._append = real_append
        if got2 is not None and getattr(got2, "status", None) != _FAKE_STATUS:
            print(f"  [失败] 记账失败后交回的结果对象不对：{got2!r}")
            ok = False
        if got2 is not None and rec2 is not None and not (rec2.ledger_error or ""):
            print("  [失败] 收尾记账失败被静默吞掉了（`ledger_error` 为空）"
                  f"：{rec2.ledger_error!r}")
            ok = False

    # ③ runner 抛异常 + 记账也写不进去 ⇒ 抛出来的必须**仍是原异常**
    #    （同样自己一个临时目录，理由见 ② 上方那段）
    #    ⚠️ 用 `_FakeRuntime(boom=True)` 而不是另写一个抛异常的假 runtime：
    #       "假 runtime 会抛什么"同样只许有一处定义，否则这里改了 boom 的
    #       类型、那边还是 RuntimeError，就会把"原异常被顶掉"测成通过。
    with tempfile.TemporaryDirectory() as tmp_l3:
        ledger._append = _dead_append
        try:
            asyncio.run(
                run_research_ledgered(
                    _FakeRuntime(boom=True), "记账丙", label="lf3", wit_dir=tmp_l3
                )
            )
            print("  [失败] 假 runtime 抛了异常却没有传出来（自检本身失效）")
            ok = False
        except BaseException as exc:  # noqa: BLE001
            # ⚠️ **必须按精确类型认，不能用 `isinstance(exc, RuntimeError)`**：
            #    `RunLocked` 就是 `RuntimeError` 的子类（见 `run_ledger.RunLocked`），
            #    而"被轮次锁挡住、这一轮**压根没跑**"与"原异常照原样抛出来"
            #    是**两件完全不同的事**。
            #    实测踩到：退回 `close_run` 的落盘降级 ⇒ ①的锁泄漏 ⇒ ③ 被锁挡住，
            #    而这里原来写的是 `except RuntimeError: pass` ⇒ **一声不响地通过**。
            #    ⇒ 判据被自己一个更宽的 `except` 关掉了，而它看起来完全正常。
            if type(exc) is RuntimeError:
                pass
            elif isinstance(exc, RuntimeError):
                print("  [失败] 记账丙这一轮**压根没跑起来**（被轮次锁挡住："
                      f"{type(exc).__name__}: {exc}）⇒ 本断言的前提不成立，"
                      "它证明不了任何事（常见成因：上一小节的锁没被释放）")
                ok = False
            else:
                print("  [失败] 记账失败把**原异常顶掉了**：拿到的是 "
                      f"{type(exc).__name__}: {exc}，而这一轮真正失败的原因是 `RuntimeError`"
                      " ⇒ 排查的人会去追一个跟失败无关的原因")
                ok = False
        finally:
            ledger._append = real_append

    # ---- (m) 轮次互斥：同一角色同时只许一轮（#95 第 2 项），
    #          陈旧锁回收 = 被打断轮次的**回收口径**（#95 第 4 项）----
    #    归属能不能算清，前提是**同时只有一轮在跑**：(d) 那条只在撞车之后如实说
    #    "说不清"，它**不阻止撞车**；而两轮共用同一个 `.wit/<role>/`（一个收件箱、
    #    一套产物目录）会真的互相污染。
    #
    #    ⚠️ 判据取向：**被拒绝的那一轮不许在台账里留下任何东西**。留一条"未收尾"
    #      就等于台账里多了一个永远收不掉的幽灵 —— 而那正是本台账要消灭的东西。
    #    ⚠️ 「陈旧锁能不能接管」完全取决于「死进程能不能被认出来」。这条在 Windows
    #      上最容易骗过人：`os.kill(pid, 0)` 对"不存在"报的是 `WinError 87`，
    #      **不是** POSIX 的 `ESRCH` —— 只写 POSIX 那一支，Windows 侧会一路判成
    #      "可能活着"，于是陈旧锁永远接管不了，而且**不报任何错**。
    if ledger.pid_alive(os.getpid()) is not True:
        print("  [失败] `pid_alive` 连**本进程**都认不出来（实得 "
              f"{ledger.pid_alive(os.getpid())!r}）⇒ 本节下面几条全都证明不了任何事")
        ok = False
    if ledger.pid_alive(0) is not None:
        print("  [失败] `pid_alive(0)` 应为 `None`（无从判断 ⇒ fail-closed），实得 "
              f"{ledger.pid_alive(0)!r}")
        ok = False
    dead_pid6 = _dead_pid()
    if ledger.pid_alive(dead_pid6) is not False:
        print(f"  [失败] 一个**确实已经退出**的进程（pid={dead_pid6}）被判成活着的"
              f"（实得 {ledger.pid_alive(dead_pid6)!r}）⇒ 陈旧锁永远接管不了，"
              "而且不报任何错。⚠️ 最常见的成因：只处理了 POSIX 的 `ESRCH`，"
              "漏了 Windows 的 `WinError 87`")
        ok = False

    # 持锁期间第二轮必须被拒，且**台账里一个字都不加**
    with tempfile.TemporaryDirectory() as tmp7:
        gate7 = asyncio.Event()

        class _HoldRuntime:
            async def run(self, text, config=None):  # noqa: ANN001
                gate7.set()
                await asyncio.Event().wait()  # 一直持有，等外面取消
                return type("_R", (), {"status": _FAKE_STATUS, "iterations": _FAKE_ITERS})()

        holder7 = _HoldRuntime()

        async def _round_a():
            # ⚠️ 传的是**运行时对象**，不是回调 —— 见 (l) 段开头那条注释。
            return await run_research_ledgered(
                holder7, "互斥甲", label="hold", wit_dir=tmp7
            )

        async def _exclusion_probe():
            task = asyncio.create_task(_round_a())
            # ⚠️ 等"任务真的进到 runner 里"这个**可观测条件**，不等一个猜出来的
            #    时长 —— 猜短了第二轮会抢在锁之前进来（那这条就假过了）。
            await asyncio.wait_for(gate7.wait(), timeout=60)
            before = len(ledger.read_runs(wit_dir=tmp7, role="leader"))
            refused7 = False
            why7 = ""
            fix7 = ""
            try:
                ledger.open_run_locked(
                    label="乙", role="leader", text="互斥乙", wit_dir=tmp7
                )
            except ledger.RunLocked as exc:
                refused7 = True
                why7 = str(exc)
                fix7 = exc.how_to_fix
            after = len(ledger.read_runs(wit_dir=tmp7, role="leader"))
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=60)
            except asyncio.CancelledError:
                pass
            except BaseException:  # noqa: BLE001
                pass
            return refused7, why7, fix7, before, after

        refused7, why7, fix7, before7, after7 = asyncio.run(_exclusion_probe())
        if not refused7:
            print("  [失败] 同一角色已经有一轮在跑时，**第二轮照样开始了**"
                  " ⇒ 两轮共用同一个收件箱与产物目录，归属会被搅成一团说不清的账")
            ok = False
        elif not why7 or not fix7:
            print("  [失败] 拒绝时没同时给出「指名道姓的原因」与「可执行的出路」"
                  f"（reason={why7!r} fix={fix7!r}）⇒ 等于把排查成本转嫁给下一个人")
            ok = False
        if after7 != before7:
            print(f"  [失败] **被拒绝的那一轮在台账里留了痕**（{before7} → {after7} 条）"
                  " ⇒ 台账里多一条永远收不了尾的「未收尾」幽灵，"
                  "而那正是这本台账存在的理由要消灭的东西")
            ok = False
        # 正常结束（含被取消）之后**不许留锁**，否则下一轮要等它变成"陈旧锁"才走得动
        if ledger.lock_path(tmp7, "leader").exists():
            print("  [失败] 一轮结束后锁没被释放："
                  f"{ledger.lock_path(tmp7, 'leader')} ⇒ 下一轮会被自己的上一轮挡住")
            ok = False

    # 陈旧锁：持有者进程已不在 ⇒ 接管，并把那一轮在台账里收成「中断」
    with tempfile.TemporaryDirectory() as tmp8:
        old8 = ledger.open_run(label="old", role="leader", text="旧", wit_dir=tmp8, now=t0)
        lock8 = ledger.lock_path(tmp8, "leader")
        lock8.parent.mkdir(parents=True, exist_ok=True)
        lock8.write_text(
            json.dumps(
                {"run_id": old8.run_id, "pid": _dead_pid(), "label": "old",
                 "started_at": t0.isoformat().replace("+00:00", "Z")},
                ensure_ascii=False, sort_keys=True,
            ),
            encoding="utf-8", newline="\n",
        )
        try:
            _new8, reclaimed8 = ledger.open_run_locked(
                label="new", role="leader", text="新", wit_dir=tmp8, now=t0
            )
        except ledger.RunLocked as exc:
            print("  [失败] 持有者进程**已经不在了**，锁却没能被接管"
                  f"（{exc}）⇒ 一次中断会把后续的所有轮次都堵死")
            ok = False
            reclaimed8 = None
        else:
            if reclaimed8 is None:
                print("  [失败] 接管了陈旧锁，却**没有回收**被打断的那一轮"
                      " ⇒ 它在台账里永远停在「未收尾」，"
                      "后来的人分不清它是「还在跑」还是「早就没了」")
                ok = False
            elif reclaimed8.status != ledger.ABANDONED_STATUS:
                print(f"  [失败] 被打断轮次收尾用的状态是 {reclaimed8.status!r}，"
                      f"应为 {ledger.ABANDONED_STATUS!r} —— 换成 `failed` 就等于把"
                      "「没收到消息」说成「坏消息」，"
                      "而它其实可能跑成了、只是收尾那一步没落盘")
                ok = False
            folded8 = [
                r for r in ledger.read_runs(wit_dir=tmp8, role="leader")
                if r.run_id == old8.run_id
            ]
            if not folded8 or folded8[0].unfinished:
                print(f"  [失败] 盘上那条被打断的轮次读回仍是「未收尾」：{folded8}")
                ok = False

    # 锁文件在、但内容读不出 ⇒ **fail-closed**（不许当成"没有锁"直接接管）
    with tempfile.TemporaryDirectory() as tmp9:
        lock9 = ledger.lock_path(tmp9, "leader")
        lock9.parent.mkdir(parents=True, exist_ok=True)
        lock9.write_text("{半截", encoding="utf-8", newline="\n")
        try:
            ledger.open_run_locked(label="x", role="leader", text="x", wit_dir=tmp9, now=t0)
            print("  [失败] 锁文件存在但**读不出持有者**时，照样开始了新一轮"
                  " ⇒ 无法判断持有者是死是活，却替它做了「已经没了」的判断")
            ok = False
        except ledger.RunLocked:
            pass
        if ledger.ledger_path(tmp9, "leader").exists():
            print("  [失败] 上面那次拒绝仍然往台账里写了东西 ⇒ 拒绝的轮次不该留痕")
            ok = False

    # ---- (n) 收件箱句柄的**跨轮次归属**（#95 第 3 项）----
    #    模块的 docstring 从第一天就写着「收件箱里的字节只属于带来它的那次任务」，
    #    而代码里**从来没有过这条判定**：`resolve()` 只在**文件不存在**时才拒绝。
    #    句柄是内容哈希 ⇒「这份字节在」与「这份字节归我」是两件事；
    #    留下的后果是一次**溯源错误**：B 轮落下的字节被 C 轮取去当证据，
    #    而报告上看不出任何异常。
    #
    #    ⚠️ 判据必须**正反两面都断**：只断"不同归属被拒"是不够的 ——
    #       "永远拒绝"同样能通过。所以还要断"同归属必须取得回来"，
    #       以及"谁都没绑归属时必须照常通过"（那是 `tools/verify_*` 与离线核对的
    #       世界；把那条一起关掉，就是判据自己成了故障源）。
    from .evidence import inbox as _inbox

    with tempfile.TemporaryDirectory() as tmp10:
        root10 = Path(tmp10)
        payload10 = b"\xff\xd8\xff\xe0" + b"selftest-owner" * 8
        handle10 = _inbox.handle_for(payload10)

        def _try10(owner: str) -> str:
            token = _inbox.bind_owner(owner)
            try:
                _inbox.resolve(handle10, root=root10)
                return "ok"
            except _inbox.InboxRefused as exc:
                return str(exc)
            finally:
                _inbox.unbind_owner(token)

        # ⓪ `""` 这个取值必须**两头一致**：没有任何绑定时 `current_owner()` 就是它，
        #    那么 `bind_owner('')` 也必须**就是绑它**，不许"悄悄现造一个"。
        #    两边不一致的后果不是报错，而是「没有归属」这个世界**不可达** ——
        #    想构造它的探针会在不知情的情况下测成"另一个归属"，
        #    而它看起来完全像"在测那个世界"。
        #    （2026-10-04 实测踩到：下面那条前置断言就是因此假失败的，
        #     而报出来的文案是"这条闸门把离线工具一起关掉了"，与真实成因无关。）
        token_sym10 = _inbox.bind_owner("")
        sym10 = _inbox.current_owner()
        _inbox.unbind_owner(token_sym10)
        if sym10 != "":
            print("  [失败] 显式绑上那个表示「没有归属」的取值却拿不到它"
                  f"（实得 {sym10!r} —— `ctx:` / `run:` 开头就是**现造了一个**）"
                  " ⇒ 「没有归属」这个世界不可达：想测它的探针会在不知情的情况下"
                  "测成「另一个归属」，而它看起来完全像在测那个世界")
            ok = False
        # 前面任何环节绑了归属却忘了解绑，都会让**后面所有**以"没有归属"为前提的
        # 断言在另一个世界里跑 —— 而它们照样会通过。所以先把环境值本身断一次。
        ambient10 = _inbox.current_owner()
        if ambient10 != "":
            print(f"  [失败] 走到这里时上下文里还绑着归属 {ambient10!r}"
                  " ⇒ 前面某个环节绑了没解（`unbind_owner` 必须放在 `finally` 里）；"
                  "此后所有「没有归属」的断言都在另一个世界里跑，而且照样会通过")
            ok = False

        # ① **谁都没绑归属**时（`""` = "不属于任何一次 run"），落 + 取必须成功
        #    —— 那是 `tools/verify_*` 与离线核对脚本的世界。
        #    ⚠️ 落盘这一步也**显式绑到 `""`**，不靠"环境恰好是空的"：
        #       判据的前提必须由判据自己钉住，否则它会随上游的一行改动而换世界。
        token_stage10 = _inbox.bind_owner("")
        try:
            _inbox.stage(payload10, root=root10, mime_type="image/jpeg")
        finally:
            _inbox.unbind_owner(token_stage10)
        if _try10("") != "ok":
            print("  [失败] 谁都没绑归属时反而取不回来 ⇒ 这条闸门把 `tools/verify_*` "
                  "与离线核对一起关掉了（判据成了故障源，是最糟的失败形态）")
            ok = False

        token10 = _inbox.bind_owner("run:A")
        _inbox.stage(payload10, root=root10, mime_type="image/jpeg")
        _inbox.unbind_owner(token10)

        if _try10("run:A") != "ok":
            print("  [失败] 自己落的字节自己取不回来（同一归属）"
                  " ⇒ 这条闸门会把正常链路也一起挡住")
            ok = False
        cross10 = _try10("run:B")
        if cross10 == "ok":
            print("  [失败] **B 轮次读到了 A 轮次落的句柄** —— 这正是本条要消灭的"
                  "跨轮次泄漏：一份证据的归属被跨轮次推断，而报告上看不出任何异常")
            ok = False
        elif "别的轮次" not in cross10:
            print(f"  [失败] 跨轮次确实被拒了，但理由没指名道姓（含'别的轮次'才对）："
                  f"{cross10[:60]!r}")
            ok = False

        # 旧版元数据（没有 `staged_by`）⇒ **fail-closed**：不问清楚就不给用
        _inbox._meta_path(root10, _inbox.parse_handle(handle10)).write_text(
            '{"sha256":"","size":0,"mime_type":"image/jpeg","name":null}',
            encoding="utf-8", newline="\n",
        )
        legacy10 = _try10("run:A")
        if legacy10 == "ok":
            print("  [失败] 元数据里**没有归属记录**时放行了 ⇒ 旧版落下的字节"
                  "可以被任意一轮取去当证据（fail-closed 的口径反了）")
            ok = False
        elif "没有归属记录" not in legacy10:
            print(f"  [失败] 无归属记录的拒绝理由不对（含'没有归属记录'才对）："
                  f"{legacy10[:60]!r}")
            ok = False

    # ④ 归属 id 的**形态**（#95 第 2 项）：跑在发起轮里时，归属必须能**直接对上台账**
    #    （`run:<run_id>`）；没有发起轮（服务模式、离线脚本）才现造 `ctx:<uuid>`。
    #    ⚠️ 这条此前**一条判据都没有**：`_mint_owner()` 的 `run:` 分支全靠代码自证。
    #       而「归属 = 发起轮的 run_id」被**三处**同时承诺（`inbox` 的模块头、
    #       `_mint_owner`、`evidence_hooks.on_run_start`）—— 同一事实写三处正是
    #       漂移的温床，所以必须有一条**行为**判据把它钉住。
    #    ⚠️ 两头都要断：只断「跑在轮里就是 `run:` 开头」，会被一个**恒铸 `run:`**
    #       的假实现骗过 —— 那会把「没有发起轮」的场景也伪造出一个归属，
    #       溯源反而更坏（**宁可没有出处，也不要一个错的出处**）。
    with tempfile.TemporaryDirectory() as tmp11:
        inside11: list[str] = []
        rid11: list[str] = []

        async def _probe11() -> None:
            run11 = ledger.current_run()
            rid11.append(run11.run_id if run11 is not None else "")
            # ⚠️ 这里**复刻 hook 的那一句**（`evidence_hooks.on_run_start`）：
            #    要断的是「发起轮里 `bind_owner()` 铸出什么」，不是别的什么东西。
            token11 = _inbox.bind_owner()
            try:
                inside11.append(_inbox.current_owner())
            finally:
                _inbox.unbind_owner(token11)

        _, rec11 = asyncio.run(
            ledger.run_ledgered(
                _probe11,
                text="owner-form",
                label="selftest",
                role="leader",
                wit_dir=tmp11,
            )
        )
        got11 = inside11[0] if inside11 else "（这一轮压根没跑起来）"
        if not rid11 or rid11[0] != rec11.run_id:
            print("  [失败] 发起轮里读不到本轮的 `run_id`"
                  f"（拿到 {rid11!r}，本轮是 {rec11.run_id}）"
                  " ⇒ 下面那条断言的前提不成立，它证明不了任何事")
            ok = False
        elif got11 != f"{_inbox.OWNER_RUN_PREFIX}{rec11.run_id}":
            print(f"  [失败] 发起轮里铸出的归属是 {got11!r}，而本轮 `run_id` 是 "
                  f"{rec11.run_id} ⇒ 归属对不上台账：落下的字节在元数据里只剩"
                  "一个查不到出处的 id，「这份字节是哪一轮带来的」就成了"
                  "一句无从核对的描述，而报告上一片正常")
            ok = False

        # 反面：**没有发起轮**时必须现造 `ctx:` —— 不许也写成 `run:`。
        # ⚠️ 前提由判据自己钉住：上面那轮若忘了解绑，这里测的就不是「没有发起轮」
        #    那个世界，而是「还在上一轮里」—— 而它照样会通过。
        if ledger.current_run() is not None:
            print("  [失败] 走出发起轮之后 `current_run()` 还绑着"
                  " ⇒ 上一轮没解绑；此后所有以「没有发起轮」为前提的断言"
                  "都在另一个世界里跑，而且照样会通过")
            ok = False
        token11b = _inbox.bind_owner()
        try:
            outside11 = _inbox.current_owner()
        finally:
            _inbox.unbind_owner(token11b)
        if not outside11.startswith(_inbox.OWNER_CTX_PREFIX):
            print(f"  [失败] 没有发起轮时铸出的归属是 {outside11!r}"
                  f" ⇒ 期望以 {_inbox.OWNER_CTX_PREFIX!r} 开头"
                  "（服务模式与离线脚本走的正是这条路；写成 `run:` "
                  "等于替它们伪造了一个轮次）")
            ok = False

    # ---- (o) 锁的**边界**：只放自己那把；接管别人时不许凭空造记录 ----
    #    这两条都写在 docstring 里、原先**一条判据都没有**：
    #      · `release_run_lock` ：「只认自己：替**别人**解锁会把两条轮次的边界擦掉」；
    #      · `_reclaim_interrupted`：「台账里没有这一轮、或它本来就已经收尾 ⇒
    #        返回 `None`，**不编一条出来**。回收的职责是补上缺失的收尾，
    #        不是制造一条收尾」。
    #    ⚠️ 两条都必须**带正面判据**：只断"不许替别人解"会被"永远返回 False 的实现"
    #       骗过；只断"不许造记录"会被"永不接管"骗过（那等于锁永远回收不了）。
    from dataclasses import replace as _dc_replace

    with tempfile.TemporaryDirectory() as tmp14:
        owner14 = ledger.open_run(
            label="甲", role="leader", text="甲", wit_dir=tmp14, now=t0,
            run_id="run-owner-14",
        )
        ledger.acquire_run_lock(owner14, wit_dir=tmp14, role="leader")
        lock14 = ledger.lock_path(tmp14, "leader")
        if not lock14.exists():
            print("  [失败] 自检前置不成立：`acquire_run_lock` 没把锁建出来 ⇒ "
                  "下面几条都证明不了任何事")
            ok = False
        else:
            intruder14 = _dc_replace(owner14, run_id="run-intruder-14")
            if ledger.release_run_lock(intruder14, wit_dir=tmp14, role="leader"):
                print("  [失败] 一个**不是锁主人**的轮次把锁释放了"
                      " ⇒ 两条轮次的边界被擦掉：前一轮还没结束，后一轮就能进来，"
                      "而两边共用同一个收件箱与产物目录")
                ok = False
            if not lock14.exists():
                print("  [失败] 别人那次释放调用把锁删掉了 ⇒ 互斥当场失效")
                ok = False
            # 正面判据：锁主人自己必须解得掉。少了这一半，"永远返回 False"也能过。
            if not ledger.release_run_lock(owner14, wit_dir=tmp14, role="leader"):
                print("  [失败] 锁主人自己反而解不掉自己的锁"
                      " ⇒ 一轮结束后锁永远留着，下一轮要等它变成陈旧锁才走得动")
                ok = False
            elif lock14.exists():
                print("  [失败] 锁主人释放之后锁还在 ⇒ 下一轮会被自己的上一轮挡住")
                ok = False

    # 陈旧锁的持有人**在台账里根本不存在**（台账被截断/换过目录，锁却留下来了）
    #   ⇒ 接管可以，但**不许凭空造一条收尾**。
    #   造出来的那一条会让台账里出现一个**从来没发生过的轮次** —— 而这本台账的
    #   全部价值，就在于"盘上每一条都对应一次真实发生过的发起"。
    with tempfile.TemporaryDirectory() as tmp13:
        lock13 = ledger.lock_path(tmp13, "leader")
        lock13.parent.mkdir(parents=True, exist_ok=True)
        lock13.write_text(
            json.dumps(
                {"run_id": "ghost-not-in-ledger", "pid": _dead_pid(), "label": "ghost",
                 "started_at": t0.isoformat().replace("+00:00", "Z")},
                ensure_ascii=False, sort_keys=True,
            ),
            encoding="utf-8", newline="\n",
        )
        try:
            _new13, reclaimed13 = ledger.open_run_locked(
                label="接管幽灵", role="leader", text="x", wit_dir=tmp13, now=t0
            )
        except ledger.RunLocked as exc:
            print("  [失败] 陈旧锁的持有人**根本不在台账里**时接管被拒了"
                  f"（{exc}）⇒ 这把锁无法自愈，只能靠人手工去删")
            ok = False
            reclaimed13 = None
        else:
            if reclaimed13 is not None:
                print("  [失败] 台账里没有那一轮，却报出了一个回收结果"
                      f"（{reclaimed13.run_id}）⇒ 回收的职责是**补上缺失的收尾**，"
                      "不是**制造一条收尾**")
                ok = False
            roles13 = ledger.read_runs(wit_dir=tmp13, role="leader")
            ghosts13 = [r for r in roles13 if r.run_id == "ghost-not-in-ledger"]
            if ghosts13:
                print("  [失败] 接管陈旧锁时**凭空造出了一条台账记录**"
                      f"（{ghosts13[0].run_id}）⇒ 台账里多了一个从来没发生过的轮次，"
                      "而它会与真实的一轮一样被读出来、被审计、被认领")
                ok = False
            if len(roles13) != 1:
                print(f"  [失败] 接管之后的台账不是**恰好本轮一条**（实得 {len(roles13)} 条）"
                      " ⇒ 要么造了幽灵，要么把本轮自己的记录漏了")
                ok = False

    # ---- (p) CLI 收口：被轮次锁拒绝是**预期结果**，不许渲染成一屏 traceback（#103）----
    #    为什么必须离线驱动：`main()` 那条路要先 `up_until_ready` 抢平台会话，
    #    之后才轮到轮次锁 ⇒ 在开发机上**根本走不到**"被拒绝"那一步。
    #    于是把收口抽成 `run_agent.run_cli(factory)`：自检塞一个**零参工厂**进来，
    #    就能驱动**真实那段代码**（与 `run_research_ledgered` / 探针的 `run_probe`
    #    同一个理由）。⚠️ 工厂、不是现成的协程对象：协程一造出来就排期。
    #
    #    ⚠️ 断言落在**交回给调用方的退出码**与**真正打出去的那段文本**上，
    #       而且**三个方向都要断**（只断"被拒时打得好"是不够的）：
    #        ① 被拒 → 退出码 2、原因原文、出路原文，**没有** traceback；
    #        ② 正常跑完 → 退出码 0 **且一个字都不打**（永远在响的警报 = 没有警报）；
    #        ③ **别的 `RuntimeError`** → 必须原样抛出去。
    #       ⚠️ ③ 的样本**必须是 `RuntimeError`**：`RunLocked` 正是它的子类
    #          （`run_ledger.RunLocked(RuntimeError)`）⇒ 拿 `ValueError` 当探针
    #          **分不开** `except RuntimeError` 这个退化形态 —— 那种写法会把真正的
    #          运行故障一起吞成一句安静的退出码，而用 `ValueError` 时控制组照样通过。
    #          （本项目纪律：闸门要"分得开" —— 一对只差一个变量的输入必须要求不同结论。）
    #
    #    ⚠️ 判据不许退化成"grep 源码里有没有 `except run_ledger.RunLocked`"：
    #       "写到了"并不等于"运行时真会走到"（本项目为这条误判过）。
    from .run_agent import run_cli as _run_cli

    def _capture_cli(factory):
        """跑一次 CLI 收口，把 `(退出码, stdout, stderr, 抛出来的异常)` 一起交回来。

        ⚠️ 被拒那段话是打到 **stderr** 的（`print(..., file=sys.stderr)`）⇒ 只收
           stdout 会得到空串，而"空"看上去与"什么都没打"一样 —— 一条**已经修好**的
           实现会被判成没修。反过来只收 stderr 也不行：那样"把拒绝写进 stdout"
           这种话就查不出来了。所以**两个流都收、再拼起来断**。
        """
        out_buf, err_buf = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                code = _run_cli(factory)
        except BaseException as exc:  # noqa: BLE001 - 方向③要看见"原样抛出去"
            return None, out_buf.getvalue(), err_buf.getvalue(), exc
        return code, out_buf.getvalue(), err_buf.getvalue(), None

    #: 探针用的原因 / 出路**与真实那两段同形态**（都能指名道姓）。判据逐字比对它们
    #: 有没有被打出来 —— 只断"打了字"会被"另一个还没跑完"这类空话骗过。
    _refusal_reason = "同角色已有另一轮在跑：pid=424242，锁在 .wit/leader/run.lock"
    _refusal_fix = "确认它真的不在了就删掉那把锁，或者等它结束再跑"

    def _raise_locked():
        async def _boom():
            raise ledger.RunLocked(_refusal_reason, _refusal_fix)
        return _boom()

    def _well_behaved():
        async def _fine():
            return None
        return _fine()

    def _raise_other():
        async def _boom():
            raise RuntimeError("自检故意抛的别的运行错误")
        return _boom()

    code_deny, out_deny, err_deny, raised_deny = _capture_cli(_raise_locked)
    text_deny = out_deny + err_deny
    if raised_deny is not None:
        print("  [失败] 抢不到轮次锁时 `run_cli` 把异常抛了出去"
              f"（{type(raised_deny).__name__}）⇒ 调用方看到的仍是一屏栈，"
              "而不是两行能照做的出路"
              "（`case_round._spawn_round` 起子进程走的正是这条入口）")
        ok = False
    else:
        if code_deny != 2:
            print(f"  [失败] 被轮次锁拒绝时退出码是 {code_deny!r}，应为 2"
                  " ⇒ 调用方分不出「这次没开始跑」与「跑出问题了」")
            ok = False
        for _label, _needle, _why in (
            ("原因原文", _refusal_reason,
             "只说「另一个还没跑完」等于把排查成本转嫁给下一个人"),
            ("出路原文", _refusal_fix,
             "没有「照做就能继续」的那一步，拒绝就只是刁难"),
        ):
            if _needle not in text_deny:
                print(f"  [失败] 被拒时的输出里没有{_label}（{_needle!r}）⇒ {_why}")
                ok = False
        if "Traceback" in text_deny:
            print("  [失败] 被轮次锁拒绝时打出了 traceback ⇒ **预期结果被渲染成崩溃**："
                  "人会去查一个根本不存在的故障，而真正的出路一个字都没打")
            ok = False
        # ⚠️ 上面那条只认字面量 `Traceback`。换成 `traceback.print_exc()` 或把异常对象
        #    直接拼进文本，字面量就不在输出里了 ⇒ 再断一条**形状不同、指向同一件事**的：
        #    原因只许出现**一次**（走 traceback 的话它会在异常那一行里出现第二次）。
        #    两条都在断"有没有把栈打给人看"，但取证方式不同，所以不是重复。
        if text_deny.count(_refusal_reason) > 1:
            print(f"  [失败] 被拒时那段原因出现了 {text_deny.count(_refusal_reason)} 次"
                  " ⇒ 拒绝话术之外还把异常本身打了一遍（多半是 traceback）")
            ok = False

    code_fine, out_fine, err_fine, raised_fine = _capture_cli(_well_behaved)
    if raised_fine is not None or code_fine != 0:
        print(f"  [失败] 正常跑完时 `run_cli` 的退出码是 {code_fine!r}"
              f"（抛出了 {raised_fine!r}），应为 0 ⇒ 调用方无法区分成功与被拒")
        ok = False
    elif out_fine or err_fine:
        print(f"  [失败] 正常跑完时仍打了字（stdout={out_fine!r} stderr={err_fine!r}）"
              " ⇒ 「被拒绝」在日志里不再有区分度（**永远在响的警报 = 没有警报**）")
        ok = False

    code_other, _out_other, _err_other, raised_other = _capture_cli(_raise_other)
    if not isinstance(raised_other, RuntimeError) or isinstance(
        raised_other, ledger.RunLocked
    ):
        print(f"  [失败] 别的**运行错误**没有被原样抛出去（实得 {raised_other!r}，"
              f"退出码 {code_other!r}）⇒ `RunLocked` 是 `RuntimeError` 的子类，"
              "把捕获写成 `except RuntimeError` 会把真正的运行故障也变成一句安静的"
              "退出码 —— 那正是「静默给安全结论」，本项目最危险的失败形态")
        ok = False

    if ok:
        print("  [通过] 台账：**先落盘、再跑**（跑的那一刻盘上已有开记录）、执行期间绑定生效、"
              "异常与未收尾均留痕，包装交回的是**收尾后**那条记录（带 status/iterations）；"
              "读回按 `run_id` 折叠、按**绝对时刻**排；"
              "并发可唯一指认、窗口重叠时拒绝认领、单轮视角也看得见别的轮；"
              "全部真机探针（`run/e2e_*.py` **按目录现场派生**、逐个点名登记、带附件，"
              "各记各的角色目录且同一角色上互不覆盖）也走同一份台账；"
              "人肉入口的「未收尾」按 `finished_at` 判、且把**进程已死**的轮另标为「中断」；"
              "台账与会话库同根；"
              "**记账写失败只降级**（runner 照跑、result 照交回、原异常照原样抛，失败写进 "
              "`ledger_error`）；**轮次互斥**（持锁期间第二轮被拒且台账零写入、结束后必放锁、"
              "陈旧锁被接管并回收成 abandoned、坏锁 fail-closed，"
              "且**只放自己那把锁**、**不给台账里不存在的轮次编收尾**）；"
              "**收件箱句柄只归带来它的那一轮**（同归属可取 / 跨轮次被拒 / 无归属世界照常）；"
              "**CLI 收口**（被轮次锁拒绝 → 退出码 2 + 原因/出路原文且没有 traceback，"
              "正常跑完 → 退出码 0 且一个字都不打，别的 `RuntimeError` 照原样抛）；"
              "（本节各条负对照由 `run/meta_verify_run_ledger.py` 与 "
              "`run/meta_verify_evidence.py` 逐条证明会失败）")
    return ok


def check_case() -> bool:
    """3d. 真实案例定期研判（#27）：案例库的形态、轮换的**行为**、四项体检的牙齿。

    为什么要单开一节 —— 这一节回答的不是"代码写没写"，而是三件**别处都答不了**的事：

      1. **轮换真的在换案子吗**。一个"永远取第一个"的实现，所有形态断言都能通过，
         而现象是"排行榜上一堆重复的同一段材料、其它 12 个案子永远轮不到"——
         它**不报错**。
      2. **四项体检分得开吗**。判据要的不只是"会响"，而是"一对只差一个变量的输入
         给出**不同**的结论"（本项目纪律：闸门要「分得开」，不只「会响」）。
         所以下面每一项都是**成对**断言的。
      3. **每天跑一次的定时器，数字对得上吗**。`case_round.ROUND_TIMEOUT_S`（子进程上限）
         与单元里的 `TimeoutStartSec`（systemd 上限）必须**前者小于后者**，否则
         900 秒那一轮会被 systemd 先切断，而日志里只会写"服务启动超时" —— 一个
         完全不指向真因的措辞。两个数字躺在两个文件里，只有放在一起看才有意义。
         同理：偏移闸门的门槛必须**大于**所有单元的错开延迟，否则"等常驻服务稳定"
         这句话就是假的。

    ⚠️ 本节**不启子进程、不联网、不碰仓库与 `.wit`**：唯一写盘的地方是 §⑥ 在
       **系统临时目录**里放一个合成台账（`render_ledger_md` 的 `wit_dir` 入参就是
       为这件事留的口子），随用随删。真跑一轮是 `scripts/case_round.sh` 的事；
       这一节保证的是"它跑起来之前，该成立的都成立"。各条负对照由
       `run/meta_verify_case_round.py` 逐条证明会失败。
    """
    section("3d. 真实案例定期研判（#27：轮换 / 四项体检 / 定时器数字）")
    ok = True

    import dataclasses
    import os
    from pathlib import Path as _Path

    from . import case_library as cl
    from . import case_round as cr
    from .run_ledger import ledger_path

    root = _Path(__file__).resolve().parents[1]

    # ── ① 案例库自己的形态 ────────────────────────────────────────────────
    problems = cl.self_check()
    if problems:
        for p in problems:
            print(f"  [失败] 案例库自检：{p}")
        ok = False
    else:
        print(f"  [通过] 案例库：{len(cl.CASES)} 条真实案例 / "
              f"{len(cl.categories())} 类，id 与文本均唯一、每条都有出处与争点、"
              f"文本长度都在 [{cl.MIN_TEXT_CHARS}, {cl.MAX_TEXT_CHARS}] 内")

    # ── ② 轮换：**成对**验证「真的在换」（不只看它挑得对不对）──────────────
    from .run_ledger import RunRecord

    def _run(label: str, run_id: str, *, closed: bool = True, pid: int = 0) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            role="leader",
            label=label,
            pid=pid,
            ppid=0,
            started_at="2026-10-05T00:00:00Z",
            text_sha256="0" * 64,
            text_head="probe",
            argv=("check_local",),
            finished_at="2026-10-05T00:01:00Z" if closed else None,
            status="completed" if closed else None,
            iterations=8 if closed else None,
        )

    first = cl.CASES[0].id
    plan0 = cl.plan_next([])
    plan1 = cl.plan_next([_run(cl.case_label(first), "r1")])
    if plan0.case is None or plan0.case.id != first:
        print(f"  [失败] 空台账时应当挑库里第一条（{first}），实得 "
              f"{plan0.case.id if plan0.case else None}")
        ok = False
    elif plan1.case is None or plan1.case.id == first:
        # ⚠️ 这一条才是"真的在换"：只断言"空台账挑第一条"的实现，永远取第一条也能过。
        print(f"  [失败] 跑过 {first} 一轮之后**又挑了同一个案子** ⇒ 轮换没在换"
              "（现象是其它案子永远轮不到，而且不报错）")
        ok = False
    else:
        print(f"  [通过] 轮换：空台账 → {first}；跑过它一轮 → 换成 {plan1.case.id}")

    lap_runs = [_run(cl.case_label(c.id), f"lap1-{c.id}") for c in cl.CASES]
    plan_lap1 = cl.plan_next(lap_runs)
    plan_lap2 = cl.plan_next(lap_runs + [_run(cl.case_label(c.id), f"lap2-{c.id}") for c in cl.CASES])
    if (plan_lap1.lap, plan_lap2.lap) != (1, 2):
        print(f"  [失败] 圈数算错：跑满一圈应为 1（实得 {plan_lap1.lap}）、"
              f"两圈应为 2（实得 {plan_lap2.lap}）")
        ok = False
    elif plan_lap1.case is None or plan_lap1.case.id != first or plan_lap1.total_rounds != len(cl.CASES):
        print(f"  [失败] 跑满一圈后应当回到 {first} 且总数 {len(cl.CASES)}，"
              f"实得 {plan_lap1.case.id if plan_lap1.case else None} / {plan_lap1.total_rounds}")
        ok = False
    else:
        print(f"  [通过] 圈数：跑满一圈 lap=1 且回到 {first}，两圈 lap=2（共 "
              f"{len(cl.CASES)} 条案例）")

    # 台账里一轮是**开/收两行**：折叠过的与原始两行必须给出**同一个**答案。
    folded = _run(cl.case_label(first), "rf")
    unfolded = [folded, folded]
    pf, pu = cl.plan_next([folded]), cl.plan_next(unfolded)
    if (pf.total_rounds, pf.deduped_rows) != (1, 0) or (pu.total_rounds, pu.deduped_rows) != (1, 1):
        print("  [失败] 未折叠的台账被判成两轮（或折叠过的被判成一整轮）——"
              f"实得 folded={pf.total_rounds}/dedup={pf.deduped_rows}、"
              f"unfolded={pu.total_rounds}/dedup={pu.deduped_rows}；"
              "重复计数是静默的，会让轮换按一个偏大的数挑案子")
        ok = False
    elif pf.case is None or pu.case is None or pf.case.id != pu.case.id:
        print("  [失败] 折叠前后挑出的案子不一致")
        ok = False
    else:
        print("  [通过] 折叠口径：同一 run_id 的开/收两行 = 一轮（未折叠输入也能得到同一答案）")

    # 认不出的 id / 有开无收 都要**如实报出来**，不能静默计入某个案子。
    ghost = cl.plan_next([_run("case:no-such-case", "rg"),
                          _run(cl.case_label(first), "ru", closed=False)])
    if ghost.unknown != ("no-such-case",) or ghost.unfinished != ("ru",):
        print(f"  [失败] 认不出的 id / 未收尾的轮次没有被如实报出："
              f"unknown={ghost.unknown} unfinished={ghost.unfinished}")
        ok = False
    elif ghost.counts.get(first, 0) != 1:
        print("  [失败] 未收尾的那一轮没有被计入轮次（材料已经喂进链路，应当算一轮）")
        ok = False
    else:
        print("  [通过] 台账瑕疵如实报出：认不出的 id → unknown，有开无收 → unfinished")

    # ── ③ label 反解：正/负对照，且**一个空格之差必须分得开** ──────────────
    good = cl.case_id_from_label(cl.case_label(first))
    bad_inputs = ["", "case:", "case:a:b", "automation", "CASE:" + first, "x" + cl.CASE_ID_PREFIX + first]
    bad_got = [cl.case_id_from_label(s) for s in bad_inputs]
    sneaky = cl.case_id_from_label(cl.case_label(first) + " ")
    if good != first or any(v is not None for v in bad_got):
        print(f"  [失败] label 反解：正常输入 {good!r}（期望 {first!r}），"
              f"但畸形输入也认出了 {[s for s, v in zip(bad_inputs, bad_got) if v is not None]}")
        ok = False
    elif sneaky == first or cl.get_case(sneaky or "") is not None:
        print(f"  [失败] 末尾多一个空格的 label 被当成正常 id（{sneaky!r}）——"
              "手工改过的台账会**静默**少算/错算一轮")
        ok = False
    else:
        print(f"  [通过] label 反解：`{cl.case_label(first)}` → {first}；"
              f"空/畸形/前缀不全 6 种都不认；末尾多一个空格**不会**被悄悄修好"
              f"（会落进 unknown 里被看见）")

    # ── ④ 四项体检：每一项都成对断言（只差一个变量 ⇒ 结论必须不同）─────────
    case0 = cl.CASES[0]
    cl_ok = cr.check_ledger([], [_run(cl.case_label(first), "rk")], case0)
    cl_bad_new = cr.check_ledger([_run(cl.case_label(first), "rk")],
                                 [_run(cl.case_label(first), "rk")], case0)
    cl_bad_label = cr.check_ledger([], [_run(cl.case_label(cl.CASES[1].id), "rk")], case0)
    cl_bad_open = cr.check_ledger([], [_run(cl.case_label(first), "rk", closed=False)], case0)
    cl_bad_raise = cr.check_ledger(
        [], [dataclasses.replace(_run(cl.case_label(first), "rk"), status="raised:TimeoutError")], case0)
    ledger_bad = [c for c in (cl_bad_new, cl_bad_label, cl_bad_open, cl_bad_raise) if c.ok]
    if not cl_ok.ok or ledger_bad:
        print(f"  [失败] 台账体检分不开：正常应为通过（实得 {cl_ok.ok}）；"
              f"「没新增/标签写错/有开无收/异常收尾」四项里有 {len(ledger_bad)} 项被判成了通过")
        ok = False
    else:
        print("  [通过] 体检①台账：正常 → 通过；没新增 / 标签写错 / 有开无收 / raised:* "
              "四种都判失败")

    none_art = cr.ArtifactSnapshot(frozenset(), frozenset(), frozenset())
    only_sidecar = cr.ArtifactSnapshot(
        frozenset({f"{cr.VERDICT_PREFIX}x{cr.SIDECAR_SUFFIX}"}), frozenset(), frozenset()
    )
    with_verdict = cr.ArtifactSnapshot(
        frozenset({f"{cr.VERDICT_PREFIX}x.json"}), frozenset(), frozenset()
    )
    a_bad = cr.check_artifacts(none_art, none_art)
    a_sidecar = cr.check_artifacts(none_art, only_sidecar)
    a_ok = cr.check_artifacts(none_art, with_verdict)
    if not (a_ok.ok and not a_bad.ok and not a_sidecar.ok):
        print(f"  [失败] 产物体检分不开：无产物应为失败（实得 {a_bad.ok}）、"
              f"只有 sidecar 也应为失败（实得 {a_sidecar.ok}）、有 verdict 应为通过"
              f"（实得 {a_ok.ok}）")
        ok = False
    else:
        print(f"  [通过] 体检②产物：新增 `{cr.VERDICT_PREFIX}*.json` → 通过；"
              f"没有 → 失败；**只有 sidecar**（`*{cr.SIDECAR_SUFFIX}`）也不算 → 失败")

    # 凭据判据的**成对样本**：干净行与可疑行各一组，两边都必须分得开。
    # ⚠️ 干净行里刻意放了 `duration_ms: 401`（裸 401 是耗时数字，不是未授权）——
    #    判据写成裸匹配的话，它会把正常日志报成凭据事故，于是变成永远在响的警报。
    clean_lines = ["某角色启动正常", "routine line", "duration_ms: 401, ok", "state=working"]
    dirty_lines = [
        "Unhandled: Unauthorized from registry",
        "Could not validate credentials",
        "HTTP 401 from registry",
        "listener 处于 blocked",
    ]
    false_pos = [line for line in clean_lines if cr._CRED_RE.search(line)]
    missed = [line for line in dirty_lines if not cr._CRED_RE.search(line)]
    if false_pos or missed:
        print(f"  [失败] 凭据判据分不开：干净样本误报 {false_pos}；可疑样本漏报 {missed}")
        ok = False
    else:
        print("  [通过] 体检④凭据判据：4 条正常行零误报（含裸 `401` 这类耗时数字）；"
              "Unauthorized / Could not validate / 带上下文的 401 / blocked 全部命中")

    _ss_sample = [
        "State  Recv-Q Send-Q      Local Address:Port  Peer Address:PortProcess",
        "LISTEN 0      4096            0.0.0.0:8445       0.0.0.0:*",
        "LISTEN 0      4096               [::]:8450          [::]:*",
        "ESTAB  0      0            10.0.0.1:22        10.0.0.2:51234",
    ]
    if not (cr._listen_ports(_ss_sample) == {8445, 8450}
            and cr._listen_ports([ln for ln in _ss_sample if not ln.startswith("LISTEN")]) == set()):
        print("  [失败] ss 输出解析：只该认 LISTEN 行，实得 "
              f"{sorted(cr._listen_ports(_ss_sample))}")
        ok = False
    else:
        print("  [通过] 体检③端口：`ss -ltn` 解析只认 LISTEN 行（ESTAB 行不算）")

    # 两个闸门：**量不出来**与**量出来了**必须分开（方向刚好相反）。
    real_age = cr.youngest_resident_age_s
    try:
        # ⚠️ 打桩时**故意把明细字典留空**：闸门不许自己再从字典里 `min()` 一遍。
        #    第一版就是那样写的，于是"有秒数但没有明细"这种**不一致输入**会让它
        #    抛 `ValueError` —— 一个体检用的闸门在输入不完整时崩掉，比它给个笨答案
        #    糟得多。这个桩当场抓到了它。
        cr.youngest_resident_age_s = lambda roles=DEPLOY_ROLES: (None, None, {}, "假的：量不出来")
        g_unknown = cr.offset_gate()
        cr.youngest_resident_age_s = lambda roles=DEPLOY_ROLES: (5, "leader", {}, "假的：刚起来")
        g_young = cr.offset_gate()
        cr.youngest_resident_age_s = lambda roles=DEPLOY_ROLES: (600, "leader", {}, "假的：早就起来了")
        g_old = cr.offset_gate()
    finally:
        cr.youngest_resident_age_s = real_age
    if not (g_unknown.ok and not g_young.ok and g_old.ok):
        print(f"  [失败] 偏移闸门分不开：量不出来应为**放行**（实得 {g_unknown.ok}）、"
              f"刚起来应为拦下（实得 {g_young.ok}）、早就起来应为放行（实得 {g_old.ok}）")
        ok = False
    else:
        print("  [通过] 偏移闸门：量不出来 → 放行但报出原因；服务刚起来 → 拦下；"
              "早就起来 → 放行")

    alive_run = _run(cl.case_label(first), "ra", closed=False, pid=os.getpid())
    dead_run = _run(cl.case_label(first), "rd", closed=False, pid=999999999)
    done_run = _run(cl.case_label(first), "rc")
    if not (not cr.busy_gate([alive_run]).ok and cr.busy_gate([dead_run]).ok
            and cr.busy_gate([done_run]).ok):
        print("  [失败] 忙闸门分不开：「有开无收且进程活着」应拦下；"
              "「有开无收但进程已死」应放行（那是被打断的轮次，不是在跑的轮次）")
        ok = False
    else:
        print("  [通过] 忙闸门：在跑 → 拦下；已收尾 → 放行；**有开无收但进程已死 → 放行**"
              "（判据取进程存活这个可观测事实，不取猜出来的超时值）")

    # ── ⑤ 定时器：两个文件里的数字必须对得上 ──────────────────────────────
    svc = root / "deploy/systemd" / cr.SERVICE_NAME
    tmr = root / "deploy/systemd" / cr.TIMER_NAME
    sh = root / "scripts/case_round.sh"
    installer = root / "scripts/systemd-install.sh"
    missing = [p.name for p in (svc, tmr, sh, installer) if not p.exists()]
    if missing:
        print(f"  [失败] 案例研判的接线文件缺失：{missing}")
        ok = False
    else:
        sbody = svc.read_text(encoding="utf-8", errors="replace")
        tbody = tmr.read_text(encoding="utf-8", errors="replace")
        shbody = sh.read_text(encoding="utf-8", errors="replace")
        ibody = installer.read_text(encoding="utf-8", errors="replace")
        problems2: list[str] = []
        # ⚠️ 全部走**行首锚定**（`_unit_line` / `_script_line`）：这两个文件里都写着
        #    解释性注释，而注释里**提到了这些指令名**。子串匹配会被自己的注释骗到。
        if not _unit_line(sbody, r"^Type=oneshot\s*$"):
            problems2.append("单元不是 Type=oneshot（它跑完即退，没有「服务在跑」这个状态）")
        if _unit_line(sbody, r"^\[Install\]\s*$"):
            problems2.append("单元写了 [Install] ⇒ 可能被 enable 成「开机跑一轮」，"
                             "而开机正是常驻服务刚起来、最不该登录 Registry 的时刻")
        if not _unit_line(sbody, r"^ExecStart=.*scripts/case_round\.sh\s*$"):
            problems2.append("ExecStart 没有走 scripts/case_round.sh"
                             "（人手工跑与定时器跑必须是同一条命令）")
        m_tmo = re.search(r"^TimeoutStartSec=(\d+)", sbody, re.M)
        tmo = int(m_tmo.group(1)) if m_tmo else 90
        if tmo <= cr.ROUND_TIMEOUT_S:
            problems2.append(
                f"TimeoutStartSec={tmo} 不大于 case_round.ROUND_TIMEOUT_S="
                f"{cr.ROUND_TIMEOUT_S} ⇒ 子进程还没到自己那一档就被 systemd 切断，"
                "而日志只会写「服务启动超时」，指向不了真因"
            )
        log_path = re.search(r"^StandardOutput=append:(\S+)", sbody, re.M)
        if not log_path or not log_path.group(1).endswith(f"/{cr.LOGS_REL}/case-round.log"):
            problems2.append(
                f"单元的输出路径末尾不是 /{cr.LOGS_REL}/case-round.log"
                f"（实得 {log_path.group(1) if log_path else None}）—— "
                "而 `case_round` 读日志增量时用的是它自己派生的 logs 目录"
            )
        if not _unit_line(tbody, r"^OnCalendar="):
            problems2.append("定时器没有 OnCalendar")
        if not _unit_line(tbody, r"^Persistent=true\s*$"):
            problems2.append("定时器没有 Persistent=true ⇒ 关机错过的时刻不会补跑")
        if not _unit_line(tbody, rf"^Unit={re.escape(cr.SERVICE_NAME)}\s*$"):
            problems2.append(f"定时器没有显式 Unit={cr.SERVICE_NAME}"
                             "（默认值虽然存在，但读的人看不出它指向谁）")
        if not _unit_line(tbody, r"^WantedBy=timers\.target\s*$"):
            problems2.append("定时器的 [Install] 不是 timers.target ⇒ 开机不会启用")
        if not _script_line(shbody, "legal_acps.case_round"):
            problems2.append("scripts/case_round.sh 没有（在非注释行里）调 "
                             "legal_acps.case_round —— 它不再是唯一入口")
        # ⚠️ shell 侧没法 import 本模块，所以它必然有一份**字面量副本**。
        #    这份副本只能靠这里当裁判：python 常量改名而 shell 没跟，
        #    `case-install` 会去装一个不存在的文件 —— 而那要等到有人真去装才发现。
        for name in (cr.SERVICE_NAME, cr.TIMER_NAME):
            if not _script_line(ibody, name):
                problems2.append(f"scripts/systemd-install.sh 的非注释行里没有 {name}"
                                 "（它按字面量去装单元，改名后不会自动跟上）")
        if problems2:
            for p in problems2:
                print(f"  [失败] 定时器接线：{p}")
            ok = False
        else:
            print(f"  [通过] 定时器接线：{cr.SERVICE_NAME} = oneshot / 无 [Install] / "
                  f"ExecStart 走 case_round.sh / TimeoutStartSec={tmo}s > "
                  f"ROUND_TIMEOUT_S={cr.ROUND_TIMEOUT_S}s；{cr.TIMER_NAME} = "
                  f"OnCalendar + Persistent + Unit 显式 + timers.target；输出落 logs/；"
                  "shell 侧的两个字面量与 python 常量一致")

    # 期望端口必须**排除软废弃**：把它们算进去，体检③就永远红（假警报）。
    want = cr.expected_ports()
    retired_ports = sorted(PORTS[r] for r in SOFT_RETIRED if r in PORTS)
    if want != sorted(PORTS[r] for r in DEPLOY_ROLES) or set(retired_ports) & set(want):
        print(f"  [失败] 期望端口集合不对：{want}（软废弃的 {retired_ports} 不该在内）")
        ok = False
    else:
        print(f"  [通过] 体检③的期望端口现场派生自 DEPLOY_ROLES：{len(want)} 个，"
              f"**不含**软废弃的 {retired_ports}（含进去就会永远报失败）")

    # 专项记录不许与发起轮台账同文件（同文件 = 两种记录互相污染，且**都还读得出来**）
    if cr.rounds_ledger_path() == ledger_path():
        print("  [失败] 专项记录与发起轮台账指向同一个文件 ⇒ 两种记录会互相污染")
        ok = False
    else:
        print(f"  [通过] 专项记录独立：{cr.ROUNDS_LEDGER_NAME} ≠ {ledger_path().name}")

    # 偏移闸门的门槛必须**大于**所有单元的错开延迟，否则"等常驻服务稳定"是句空话。
    units = sorted((root / "deploy/systemd").glob("acps-*.service"))
    delays = {
        p.name: _unit_sleep_delay(p.read_text(encoding="utf-8", errors="replace"))
        for p in units
        if p.name != cr.SERVICE_NAME
    }
    worst = max(delays.values()) if delays else 0
    if cr.OFFSET_MIN_S <= worst:
        print(f"  [失败] 偏移闸门门槛 {cr.OFFSET_MIN_S}s 不大于最大错开延迟 {worst}s ⇒ "
              "它可能在常驻服务还没登录完 Registry 的时候就放行")
        ok = False
    else:
        print(f"  [通过] 偏移闸门门槛 {cr.OFFSET_MIN_S}s > 最大错开延迟 {worst}s")

    # ── ⑥ 答辩材料：数字必须现场算，且不许 vacuous 取真 ────────────────────
    # ⚠️ 这一节的每一条都**必须成对**：单看"它能印出 2 / 13"是分不开"写死的"与
    #    "派生的"——因为今天 `len(CASES)` 恰好就是 13。
    import json as _json
    import tempfile as _tempfile
    from dataclasses import replace as _dc_replace

    _OK4 = [{"name": n, "ok": True, "detail": ""}
            for n in ("ledger", "artifacts", "ports", "credentials")]
    _BAD4 = [dict(c) for c in _OK4]
    _BAD4[1]["ok"] = False

    def _row(**kw: object) -> dict:
        base = {
            "at": "2026-10-05T00:00:00Z", "outcome": "ran", "case_id": first,
            "category": "食品安全", "exit_code": 0, "duration_s": 1.0,
            "run_id": "r1", "checks": _OK4,
        }
        base.update(kw)
        return base

    def _render(rows: list[dict], *, cases: tuple | None = None) -> str:
        """在**临时目录**里放一个合成台账再渲染。仓库与 `.wit` 一个字节都不碰。"""
        with _tempfile.TemporaryDirectory() as td:
            p = cr.rounds_ledger_path(td, "leader")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                "".join(_json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                encoding="utf-8",
            )
            real = cl.CASES
            try:
                if cases is not None:
                    cl.CASES = cases  # type: ignore[assignment]
                return cr.render_ledger_md(td, "leader")
            finally:
                cl.CASES = real  # type: ignore[assignment]

    def _cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip("|").split("|")]

    def _cell(md: str, label: str) -> str:
        for line in md.splitlines():
            if line.startswith("|") and label in line:
                cells = _cells(line)
                if len(cells) >= 2:
                    return cells[1]
        return "（没有这一行）"

    def _count(md: str, label: str) -> int | None:
        raw = _cell(md, label).replace("*", "").strip()
        return int(raw) if raw.isdigit() else None

    def _truth_streak(rows: list[dict]) -> int:
        """从末尾往前数连续通过。**判据自己算，不取材料自己的标记。**"""
        n = 0
        for r in reversed(rows):
            if cr.round_passed(r):
                n += 1
            else:
                break
        return n

    # ⑥-1 分母**现场派生**（`len(CASES)`），不是写死的处数。
    grown = cl.CASES + tuple(_dc_replace(cl.CASES[0], id=f"c-probe-{i}") for i in range(3))
    den_now = _cell(_render([_row()]), "覆盖到的案例")
    den_grown = _cell(_render([_row()], cases=grown), "覆盖到的案例")
    if den_now != f"1 / {len(cl.CASES)}" or den_grown != f"1 / {len(grown)}":
        print(f"  [失败] 材料里「覆盖到的案例」的分母没有现场派生：库 {len(cl.CASES)} 条时"
              f"应为 `1 / {len(cl.CASES)}`（实得 `{den_now}`）、临时加长到 {len(grown)} 条时"
              f"应为 `1 / {len(grown)}`（实得 `{den_grown}`）"
              "—— 写死的分母加一条案例就变成一眼错的数字，而且不报错")
        ok = False
    else:
        print(f"  [通过] 材料分母**现场派生**：{len(cl.CASES)} 条 → `{den_now}`、"
              f"临时加长到 {len(grown)} 条 → `{den_grown}`"
              "（写死的 13 分不开这一对 —— 所以这条判据必须改一次库长度）")

    # ⑥-2 空体检结果**不算通过**。`all([])` 是 `True`。
    md_empty = _render([_row(checks=[])])
    md_full = _render([_row()])
    n_empty = _count(md_empty, "四项体检全过")
    n_full = _count(md_full, "四项体检全过")
    marks = ""
    for line in md_empty.splitlines():
        if line.startswith("| 1 |"):
            cells = _cells(line)
            if len(cells) > 8:
                marks = cells[8]
    if (n_empty, n_full) != (0, 1):
        print(f"  [失败] 空体检结果被算成了「四项体检全过」：空 checks 得 {n_empty}"
              f"（应为 0）、四项都过得 {n_full}（应为 1）"
              "—— `all([])` 是 True，方向是**假通过**")
        ok = False
    elif "✅" in marks:
        print(f"  [失败] 空体检结果那一行的「体检」列是全过标记（{marks!r}）"
              "—— 同一份材料里两处自相矛盾（汇总说没通过、明细说全过）")
        ok = False
    else:
        print(f"  [通过] 空体检结果**不算通过**：汇总计 0（四项都过的那份计 {n_full}），"
              f"逐轮明细那一格是 {marks!r} —— 两处一致，没有把 `all([])` 当成通过")

    # ⑥-3 「连续」必须真的是连续的（累计通过数 ≠ 连续通过数）。
    streak_inputs = {
        "过/失败/过（关键：累计 2、连续只有 1）":
            [_row(run_id="r1"), _row(run_id="r2", checks=_BAD4), _row(run_id="r3")],
        "失败/过": [_row(run_id="r1", checks=_BAD4), _row(run_id="r2")],
        "过/没跑（被闸门拦下）": [_row(run_id="r1"),
                                 _row(run_id="r2", outcome="skipped", checks=[])],
        "过/过（真的连续）": [_row(run_id="r1"), _row(run_id="r2")],
    }
    bad_streak: list[str] = []
    for name, rows_in in streak_inputs.items():
        md = _render(rows_in)
        truth = _truth_streak(rows_in)
        want = [truth] if truth else []
        got = [int(x) for x in re.findall(r"连续跑通\s*(\d+)\s*轮", md)]
        if got != want:
            bad_streak.append(f"{name}：材料声称 {got}、真值 {want}")
    if bad_streak:
        print("  [失败] 材料里的「连续跑通 N 轮」与真实的末端连续段不符"
              "（把**累计**通过数当成连续数，等于在给评审的材料上写假话）："
              + "；".join(bad_streak))
        ok = False
    else:
        print("  [通过] 材料只数**末尾连续**通过段：四组输入（含「过/失败/过」）"
              "声称的连续轮数都等于现场独立算出的真值，累计通过数另列一行")

    # ⑥-4 「跑完那一刻」与「事后读台账」必须是**同一个**通过判定。
    rel_bad: list[str] = []
    probe_checks = [
        (),
        tuple(cr.Check(n, True, "") for n in ("ledger", "artifacts", "ports", "credentials")),
        tuple(cr.Check(n, n != "ports", "") for n in ("ledger", "artifacts", "ports", "credentials")),
    ]
    for checks in probe_checks:
        res: object = cr.CaseRoundResult(
            at="x", outcome="ran", case=None, label="", gates=(), checks=checks, exit_code=0
        )
        if res.ok != cr.round_passed(res.to_json()):  # type: ignore[attr-defined]
            rel_bad.append(f"checks={len(checks)}：ok={res.ok} "  # type: ignore[attr-defined]
                           f"但 round_passed={cr.round_passed(res.to_json())}")  # type: ignore[attr-defined]
    if rel_bad:
        print("  [失败] 「当场结论」与「事后材料」用的不是同一个通过判定："
              + "；".join(rel_bad) + "（两处各写一遍必然漂移，而它决定材料写几轮）")
        ok = False
    else:
        print("  [通过] 通过判定**单一来源**：`CaseRoundResult.ok` ≡ "
              "`round_passed(to_json())`（空体检、部分失败两种输入两边结论一致）")

    # ⑥-5 「覆盖到的案例」只数**真的跑过**的轮次。
    md_skip = _render([_row(outcome="skipped", case_id="c-water-01", checks=[], run_id=None)])
    cov_skip = _cell(md_skip, "覆盖到的案例")
    cov_ran = _cell(_render([_row()]), "覆盖到的案例")
    if cov_skip != f"0 / {len(cl.CASES)}":
        print(f"  [失败] 「覆盖到的案例」把**没跑**的轮次也算进去了：一条 skipped 记录"
              f"得到 `{cov_skip}`（应为 `0 / {len(cl.CASES)}`）"
              "—— 它没往发起轮台账写任何东西，轮换下次还会再挑它，"
              "与紧邻的「真跑了」「被闸门拦下」两行自相矛盾")
        ok = False
    elif cov_ran != f"1 / {len(cl.CASES)}":
        print(f"  [失败] 真跑过的轮次没被算进覆盖：得到 `{cov_ran}`"
              f"（应为 `1 / {len(cl.CASES)}`）")
        ok = False
    else:
        print(f"  [通过] 覆盖面只数**真跑过**的轮次：只跳过 → `{cov_skip}`、"
              f"真跑一轮 → `{cov_ran}`（与「真跑了」「被闸门拦下」两行不再打架）")

    if ok:
        print("  [通过] 案例研判：轮换**真的在换**、四项体检**成对分得开**、"
              "定时器的两个超时对得上、门槛大于错开延迟、"
              "答辩材料的数字**现场派生**且不做 vacuous 通过"
              "（各条负对照由 `run/meta_verify_case_round.py` 逐条证明会失败）")
    return ok


def check_mcp() -> bool | None:
    """3e. 本机 MCP 入口的**接线**判据（#121 收口 / #128 接进这道闸）。

    这一节回答的是"文档承诺的东西**接上线了没有**"：
    `docs/aicomp/{音频,视频}鉴伪使用说明.md` §2 一直写着"在客户端的 MCP 配置里加一项"，
    而 2026-10-05 实测 `~/.workbuddy/mcp.json` **不存在** —— 文档承诺 ≠ 接了线。
    这是本项目「装了 ≠ 会响」的**配置版**：配置写错（少 `cwd`、裸 `python`、
    模块名拼错）不会立刻报错，一直要到某次真的调用失败为止。

    **三态**返回值 —— ⚠️ `None` **不是**"通过"：

      · `True`  —— 配置在、与使用说明逐字一致、两条入口都真的起得来（stdio 握手）；
      · `False` —— 配置在但判据报红（真缺陷：计入「自检结论」并让本脚本非零退出）；
      · `None`  —— **本机不成立**：没有这份配置 ⇒ **判不了**。
        客户端配置是**每台机器一份**的（在 `~` 下，不进仓库、不进同步清单），
        所以服务器上必然没有它 —— 那种情况必须**响亮跳过**，绝不能静默算通过。

    ⚠️ 判据本体在 `run/verify_mcp_config.py`，**不在这里再写一遍**
       （"同一判定写两处必然漂移"；而且使用说明里引的就是那条命令行）。
       这里只负责**跑它、再把它的退出码翻成结论**。

    ⚠️ 为什么还额外看 stdout 里有没有「结论：」——`1` 这个退出码是**多义的**：
       "判据报红"与"脚本自己崩了（未捕获异常）"都是 1。只看退出码会把
       "判据成为故障源"混进"配置真错了"（本项目纪律：两者必须分开判）。
       所以：`3 → None`；`0 → True`；`1 且打印了「未通过」结论 → False`；
       **其余一律按"判据自己坏了"报失败**，并把 stderr 尾部带出来。
    """
    section("3e. 本机 MCP 入口接线（文档承诺 ↔ 实际配置）")
    import sys

    root = Path(__file__).resolve().parents[1]
    script = root / "run" / "verify_mcp_config.py"
    cfg_path = Path.home() / ".workbuddy" / "mcp.json"
    if not script.is_file():
        print(f"  [失败] 找不到判据脚本 {script.relative_to(root).as_posix()}")
        return False

    # ⚠️ `-u`：不加它，一旦被 SIGTERM 掐断就连表头都看不见
    #    （「卡住」与「没跑」长得一模一样）。
    try:
        proc = subprocess.run(
            [sys.executable, "-u", str(script)],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
        )
    except subprocess.TimeoutExpired:
        print("  [失败] 判据脚本 240s 没跑完（握手卡住？）—— 不能算通过")
        return False

    out = proc.stdout or ""
    err = proc.stderr or ""
    for line in out.splitlines():
        print(f"    {line}")
    if err.strip():
        print(f"    （stderr 尾部）{err.strip()[-300:]}")

    if proc.returncode == 0:
        print("  [通过] 两条本机 MCP 入口与使用说明一致，且都真的起得来（stdio 握手）")
        return True
    if proc.returncode == 3:
        print(f"  [跳过] 本机没有 {cfg_path.as_posix()} —— **无法判定**，不是通过。")
        return None
    if proc.returncode == 1 and "结论：**未通过**" in out:
        print("  [失败] 接线判据给出了「未通过」结论 —— 逐条见上面的输出")
        return False
    print(
        f"  [失败] 判据脚本异常结束（退出码 {proc.returncode}、且没给出「未通过」结论）"
        " —— 坏的是**判据自己**，不是配置"
    )
    return False


def check_environment() -> None:
    section("5. 环境与凭据现状")
    print(describe("leader"))
    print()
    import os

    for key in ("ACPS_USERNAME", "ACPS_PASSWORD", "ACPS_PUBLIC_HOST", "ACPS_PUBLIC_ENDPOINT",
                "OPENAI_MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        value = os.environ.get(key)
        if key in ("ACPS_PASSWORD", "OPENAI_API_KEY"):
            shown = "已设置（已隐藏）" if value else "未设置"
        else:
            shown = value or "未设置"
        print(f"  {key:<22} = {shown}")


def main() -> None:
    load_env()  # 自动读项目根目录的 .env

    results = [
        ("数据契约", check_contracts()),
        ("Agent 编译", check_agents()),
        ("护栏（终止/熔断）", check_guards()),
        ("嵌套结构", check_nesting()),
        ("协议声明", check_protocols()),
        ("脚本卫生", check_scripts()),
        ("发起轮台账", check_provenance()),
        ("真实案例定期研判", check_case()),
        # ⚠️ ⑦-5h/5i/5j 各占一行（2026-10-07 从 check_nesting 里拆出来）：
        #    塞进别人的节里，红的时候报的是别人的名字 —— 当天实证：
        #    5j 断言红、「嵌套结构」出列，把排查引向了完全错误的方向。
        ("事实库", _check_fact_store(True)),
        ("离线抓取器", _check_fetch_facts(True)),
        ("裁定经验库", _check_ruling_store(True)),
        ("判定校准台账", _check_calibration(True)),
        # ⑦-6 人机辩论驱动器（2026-10-07）：离线行为断言，独立成节的理由
        # 同 ⑦-5h~5j —— 塞进别人的节里，红的时候报的是别人的名字。
        ("人机辩论", _check_human_debate(True)),
        # ⑦-7 辩论裁定（阶段 2，2026-10-07）：辩词经裁判出正式裁定书。
        ("辩论裁定", _check_debate_ruling(True)),
        # ⑦-8 法条库与法律咨询顾问（2026-10-07 新角色 law-advisor）。
        ("法条库与法律顾问", _check_law_advisor(True)),
        # ⑦-9 线索受理官（2026-10-11 新角色 lead-intake）：独立成节的理由同上。
        ("线索受理官", _check_lead_intake(True)),
        # ⑦-10 快车道分级裁决（2026-10-12）：检索优先、辩论降级为库更新器。
        #    独立成节的理由同上——红的时候报自己的名字。
        ("快车道分级", _check_fast_lane(True)),
        # ⑦-11 线索复研判（2026-10-13）：UNDECIDABLE 的 B 出路有了消费者；
        #    排在 ⑦-10 之后——issue_ruling 的入队接线先在 ⑦-10(4) 被证明，
        #    这节证明队列另一侧（闸门/指纹/消费闭环）。
        ("线索复研判", _check_lead_recon(True)),
        # ⑦-12 系统性负控集（2026-10-13，#102 拍板）：喂无害热点看会不会
        #    硬造问题——假阳方向，与 gt 校准（假阴方向）互补。
        ("系统性负控集", _check_negative_controls(True)),
        # ⑦-13 体验应用信息分区（2026-10-19，功能④改造）：受理官三桶 +
        #    诱导方隔离墙（代码层）+ 可选追问相位机。独立成节的理由同上
        #    ——红的时候报自己的名字。
        ("体验信息分区", _check_experience_partition(True)),
    ]
    # 🔴 本机 MCP 接线是**三态**的（通过 / 失败 / 本机不成立），**不能**塞进
    #    `results`：那张表只有 True/False，`None` 一进去就会被 `if ok` 当成"通过"
    #    —— 而"这台机器没有这份配置"恰恰是**没判定**，不是通过（跳过 ≠ 通过）。
    mcp_ok = check_mcp()
    acs_ok = asyncio.run(check_acs())
    results.append(("ACS 草稿", acs_ok))
    check_environment()

    section("自检结论")
    for name, ok in results:
        print(f"  {'[通过]' if ok else '[失败]'}  {name}")
    if mcp_ok is None:
        print("  [跳过]  本机 MCP 入口接线 —— 本机无该配置，**未判定**（跳过 ≠ 通过）")
    else:
        print(f"  {'[通过]' if mcp_ok else '[失败]'}  本机 MCP 入口接线")
    failed = [n for n, ok in results if not ok]
    if mcp_ok is False:
        failed.append("本机 MCP 入口接线")
    if failed:
        raise SystemExit(f"\n以下环节未通过：{', '.join(failed)}")
    if mcp_ok is None:
        print("\n  ⚠️ 「本机 MCP 入口接线」**未判定**（本机没有那份客户端配置）—— 跳过 ≠ 通过：")
        print("     它是**每台机器一份**的配置（在 `~` 下、不进仓库与同步清单）⇒ 服务器上必然跳过。")
        print("     要判它，请在**装了配置的那台机器**上跑：")
        print("       ./.venv/Scripts/python.exe run/verify_mcp_config.py")
    print("\n  本地层面全部就绪。这一步之后，能否真正互联只取决于：")
    print("    1) .env 里的 ACPS_USERNAME / ACPS_PASSWORD 是否有效（已自助注册，见 ACCOUNT.txt）")
    print("    2) Partner 是否有一个公网可达的 public_endpoint（裸 IP 即可）")
    print("    3) 平台是否已人工审核通过（查：python -m tools.status）")
    print("  证书不用管：wit 在 enroll() 时会自动向平台 CA 申请签发。")


if __name__ == "__main__":
    main()
