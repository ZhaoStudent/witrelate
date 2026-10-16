"""收件箱：把「随任务附件进来的字节」落到本进程可读处，用 sha256 句柄引用。

════════════════════════════════════════════════════════════════════════
它补的是哪条路
════════════════════════════════════════════════════════════════════════

在它之前，「看一眼这张图」只有一条入口 —— `fetch.load(uri)`：模型把一条 URI
写进工具参数，代码按策略去取。这条入口有个天生的缺口：

    **模型根本拿不到「用户上传的图片」的字节。**

AIP 的三层能力决定了这件事（2026-10-01 实测）：

    AIP 协议层  `FileDataItem.uri` **或** `bytes`
                —— uri 必须「远程可达」：`data:` / `file://` / 裸路径**全部被拒**
    wit 代码层  `ImagePart(mime_type, uri, data)`
    模型工具层  `TOOL_CONTENT_INPUT_SCHEMA` —— 每个附件 part **只有 `uri`**
                （原文："inline Base64 is not accepted by this model-facing tool."）

用户上传的图是以 `FileDataItem.bytes` 下发的；wit 把它转成 `ImagePart(data=字节)`，
**字节完整地躺在 `state.messages` 里**，代码伸手就能拿到，一点网络都不用。
但模型那一侧只有 URI 通道：它既转述不了几十万 token 的 base64，也不该经手证据字节。
所以需要一个**只有代码能碰**的落点 —— 就是这里。

════════════════════════════════════════════════════════════════════════
为什么不直接把路径发给模型
════════════════════════════════════════════════════════════════════════

如果模型能往 `image_uri` 里填任意本地路径，这个工具就同时变成了一个
**任意的本地文件读取原语** —— 在 7 个角色常驻的那台机器上，
`.ssh/acps-hk-key.pem`（我们自己的 mTLS 私钥）也在射程内。

所以句柄**不是路径，是内容哈希**：

  · 文件名由本模块从 sha256 拼出（`<root>/<sha256>.bin`）—— 纯 hex、无分隔符，
    结构上不可能穿越到目录之外；
  · `resolve()` **只认 64 位小写十六进制**，其余一律拒绝；
  · 模型能传的只是一个 64 位 token：它无论写什么，要么命中"本来就登记在这台
    机器上的那份字节"，要么什么也命中不了。

一句话：**让调用方引用内容，而不是引用位置。**

⚠️ 由此还带来一个必须写下来的性质：**收件箱里的字节只属于"带来它的那次任务"。**
它是入站证据的落点，不是缓存 —— 过期清理由部署侧负责，本模块不猜。

⚠️ 而这句话从写下那天起**只是一句注释**（2026-10-04 补，`#95` 第 3 项）：
`resolve()` 当时只在**文件不存在**时拒绝，**从不问"这份字节是不是本次任务带来的"**。
后果是一次**溯源错误**：B 轮次落下的字节，被 C 轮次取去当证据，
而报告上看不出任何异常 —— 句柄是内容哈希，"这份字节在"与"这份字节归我"是两件事。
现在归属由元数据里的 `staged_by` 承载、由 `resolve()` 强制，口径是：

    落盘时的归属 = `current_owner()`（这次 run 的 id；没有 run 时是空串）
    取用时的归属 = `current_owner()`（同上）
    两者必须**相等**，否则拒绝 —— 归属**不跨轮次推断**，也不跨任务推断。

归属 id 优先取**发起轮**的 `run_id`（能直接对上台账），否则现造一个进程内唯一 id；
空串是一个**如实的**取值，不是兜底：常驻服务模式收的是平台派进来的 inbound 任务，
**根本没有"发起轮"**（`run_ledger.current_run()` 的 docstring 就是这么写的）。
把它和"属于某一轮"混成一个值，就会把"无从判断"伪装成"归这一轮"。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

#: 收件箱目录名。位于 Agent 自己的 workspace 下，不与别的角色共享。
INBOX_DIRNAME = "evidence-inbox"

#: 句柄前缀。`inbox:<sha256>` 同时是 `fetch.classify_uri()` 认得的 scheme。
HANDLE_PREFIX = "inbox:"

#: 单个附件的大小上限。
#:
#: ⚠️ 不变量不是"三个上限值相等"，而是：
#:
#:     **收件箱是三条链共用的落点 ⇒ 它的上限必须 ≥ 每一条取用方的上限。**
#:
#: 取用方各自仍在入口处按**自己**的口径把关（图像 8 MB、音频 16 MiB、
#: 视频 64 MiB），所以这里放宽不等于放行 —— 只是**别让落点比取用方更早卡住**。
#:
#: 这个坑踩过两次，两次的形态一模一样：
#:
#:   · 2026-10 加音频链：音频的分析窗是 8,000,000 个采样（≈190 秒 16 位单声道），
#:     对应 16 MiB；而落点当时与图像同为 8 MB ⇒ **音频那个上限在结构上永远够不着**
#:     （附件先被落点拒了），而两条链各自看都"没问题"。
#:   · 2026-10 加视频链：手机 1 分钟 1080p 就是 60–100 MB（行车记录仪还会更大），
#:     落点若停在 16 MiB，**视频的上限同样永远够不着**。而且视频侧要的是"完整文件"：
#:     `container` 那两项判据（截断 / 尾部多余数据）靠的正是"声明长度 vs 实际长度"，
#:     所以它的上限不是"能解多少帧"，而是"愿不愿意把整个文件读进内存"。
#:
#: 教训与"同一事实散在多处"同源：**一份上限写在几处都不算错，
#: 错的是它们之间的关系没有被写下来、也没有被断言。**
#: 于是这条关系现在由 `legal_acps/check_local.py` **现场派生**地钉住
#: （枚举全部取用方的上限取最大值，再与落点比）—— 以后加一条新链、
#: 忘了抬落点，绿会直接变红，而不是等到线上发现"附件永远进不来"。
#:
#: ⚠️ 抬落点还会**打破音频链的一条旧断言**（`tools/verify_audio.py` 里那条
#: "`too_large` 结构上不可达"）。那不是 bug，而是那条断言把"两个数字恰好相等"
#: 当成了结构性质；现在改成了真实构造。见那处的注释。
MAX_STAGE_BYTES = 64 * 1024 * 1024

#: 一份字节最多记几个归属。
#:
#: 同一份字节可以被多次收进来（内容哈希相同 ⇒ 同一个句柄），每次的归属都算数 ——
#: 所以归属是一个**列表**而不是单值：只留最后一次的话，"先收它的那一轮"
#: 会被自己的下游顶掉，于是它自己反而取不回自己的附件。
#: 上限只是防止无限增长；**超出时丢的是最早的**，丢的后果是"**多拒一次**"（安全方向）。
MAX_OWNERS = 8

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: 文本里出现的句柄形态。**这是"什么算一个句柄"的唯一定义** ——
#: 识别（`find_handles`）与抹除（`evidence_hooks.strip_handles`）共用它。
#:
#: ⚠️ **必须大小写不敏感**。2026-10-02 实测过一次真实缺陷：这里原先只写
#:    `r"inbox:[0-9a-fA-F]{64}"`（前缀大小写敏感），而抹除那一侧用**朴素
#:    replace**、只试过"整串小写"和"整串大写"两种形态 ⇒ 两侧的认可集合对不上：
#:
#:      `inbox:a1b2…`  识别 ✓  抹掉 ✓
#:      `inbox:A1B2…`  识别 ✓  抹掉 ✗  ⇒ 内部哈希泄漏给下游
#:      `INBOX:A1B2…`  识别 ✗  抹掉 ✓  ⇒ **接管落空、字节静默丢失**
#:      `Inbox:a1b2…`  识别 ✗  抹掉 ✗  ⇒ 同上
#:
#:    第三行的后果最重：模型只是把前缀写成了大写，`plan_delegation` 就落空
#:    ⇒ 走原生委派、只送出文本 ⇒ 下游回一句"没有图可看"、整条链白跑，
#:    **而且不报任何错**。`parse_handle()`（单值入口）本来就容忍大写
#:    （`text.lower().startswith(...)`），所以那是同一个判定在两个地方不一致。
#:
#:    改法不是"把两处对齐"，而是**让两侧用同一个正则**：这样"能被识别的
#:    必然能被抹掉"由构造保证，不再依赖两处持续对齐。
HANDLE_IN_TEXT_RE = re.compile(r"inbox:[0-9a-fA-F]{64}", re.IGNORECASE)

#: 当前上下文的收件箱根。
#:
#: 用 `ContextVar` 而不是模块级全局，是因为一个进程内可能先后跑多个 run
#: （服务模式必然如此），而收件箱属于"这次任务带来的那份字节"。
#: 绑晚了或忘了解绑，最坏结果是"引用到上一次任务的字节" —— 对一份**证据**
#: 来说这是溯源错误，所以宁可每次 run 都显式绑定一次。
_root: ContextVar[Path | None] = ContextVar("evidence_inbox_root", default=None)


class InboxRefused(Exception):
    """收件箱引用被拒绝：句柄不合法 / 没有登记过 / 字节已不可读。

    ⚠️ 刻意不继承 `OSError` 也不继承 `ValueError`：调用方（`fetch`）必须
    **显式**决定怎么处理它，而不是被某一族 `except` 顺手接住。
    """


@dataclass(frozen=True)
class InboxItem:
    """收件箱里的一份字节。`handle` 是可以给外部（模型 / 其他 Agent）看的短引用。"""

    handle: str
    sha256: str
    mime_type: str
    size: int
    path: Path
    name: str | None = None


# ---------------------------------------------------------------------------
# 绑定：当前上下文的收件箱在哪
# ---------------------------------------------------------------------------


def inbox_root(workspace: str | os.PathLike[str]) -> Path:
    """给定 workspace，返回它的收件箱目录（不创建）。"""
    return Path(workspace) / INBOX_DIRNAME


def bind_root(root: str | os.PathLike[str]) -> Token:
    """把当前上下文的收件箱根指向 `root`。返回 token，配合 `unbind_root()` 复原。"""
    return _root.set(Path(root))


def unbind_root(token: Token) -> None:
    """复原 `bind_root()` 之前的值。**必须放在 `finally` 里。**"""
    _root.reset(token)


def current_root() -> Path | None:
    """当前上下文的收件箱根；没绑定时为 `None`。"""
    return _root.get()


# ---------------------------------------------------------------------------
# 归属：这批字节是**谁**带来的（跨轮次/跨任务不许互相取用）
# ---------------------------------------------------------------------------

#: 当前上下文的归属 id。用 `ContextVar` 与 `_root` 同款理由：一个进程内可能
#: 先后跑多个 run（服务模式必然如此），而归属属于"这一次任务"。
_owner: ContextVar[str | None] = ContextVar("evidence_inbox_owner", default=None)

#: 归属 id 的前缀，用来在**拒绝文案**里一眼看出它是哪一类（人去排查时有用）。
OWNER_RUN_PREFIX = "run:"
OWNER_CTX_PREFIX = "ctx:"


def _mint_owner() -> str:
    """给"这一次 run"造一个归属 id：优先用**发起轮**的 `run_id`（能直接对上台账）。

    ⚠️ 延迟导入 `run_ledger`：本模块刻意保持"纯标准库、不 import wit"的形态
       （见 `image_attachments` 的说明），而 `run_ledger` 也是纯标准库。
       放模块级会引入一条**本可以不存在**的模块间耦合顺序。
    """
    try:
        from ..run_ledger import current_run

        run = current_run()
    except Exception:  # noqa: BLE001 - 拿不到归属不是错误，退化成"造一个"
        run = None
    if run is not None:
        return f"{OWNER_RUN_PREFIX}{run.run_id}"
    return f"{OWNER_CTX_PREFIX}{uuid.uuid4().hex[:12]}"


def bind_owner(owner: str | None = None) -> Token:
    """把当前上下文的归属指向 `owner`；传 `None` 才现造一个。返回 token。

    ⚠️ 判断"要不要现造"必须看 `is None`，**不能**写 `owner or _mint_owner()`。
       改之前那一版就是 `or`，于是 `bind_owner("")` 会**悄悄现造一个** `ctx:<uuid>`
       —— 而 `current_owner()` 在没人绑定时返回的恰恰是 `""`。
       两者一撞，后果是"**没有归属**"这个世界**变得不可达**：
       任何想构造它的人（自检、离线脚本）都会在不知情的情况下拿到"另一个归属"，
       于是探针测的是**另一个世界**，而它看起来完全像"在测那个世界"。
       （2026-10-04 实测：3c 节 (n) 段的前置断言就是这么假失败了一次，
       报出来的却是"这条闸门把离线工具一起关掉了"—— 一个与真实成因无关的方向。）
       ⇒ 这也是「同一事实不许有两种取值形态」：`""` 既然被定义成"没有归属"，
          那它就必须**能被显式地绑上去**，否则它只是一个取不到的值。
    """
    return _owner.set(_mint_owner() if owner is None else owner)


def unbind_owner(token: Token) -> None:
    """复原 `bind_owner()` 之前的值。**必须放在 `finally` 里。**"""
    _owner.reset(token)


def current_owner() -> str:
    """当前上下文的归属 id。**没有绑定时返回空串**。

    ⚠️ 空串是**如实的**取值，不是兜底（同 `run_ledger.current_run()` 返回 `None`）：
       它表示"这次不属于任何一次 run"—— 离线核对脚本、工具自检就是这种情形。
       此时落盘记 `[""]`、取用认 `""`，两边一致 ⇒ 照常工作，只是**没有任何归属可言**。
       把它和"属于某一轮"混成一个值，就会把"无从判断"伪装成"归这一轮"。
    """
    return _owner.get() or ""


def _owners_of(meta: dict[str, Any]) -> list[str]:
    """从元数据里取归属列表。**取不到就返回空列表**（= "没有记录"，不是"谁都不算"）。"""
    raw = meta.get("staged_by")
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw]


def _show_owner(owner: str) -> str:
    """给人看的归属名。空串要说清是"没有轮次"，不能显示成空白让人以为是漏填。"""
    return owner or "（本次不属于任何一次 run）"


def _owner_refusal(digest: str, owners: list[str], owner: str) -> str:
    """归属不符的拒绝文案：**必须同时给出"谁挡着"与"该改什么"**（全项目口径）。

    ⚠️ 两条出路都落在"让附件随本次任务重发一遍"上，而不是"改元数据"：
       改元数据等于让调用方**自己给自己发通行证**，那这道闸门就形同虚设。
       重发一遍是**真的**把这份字节带进本次任务，句柄还是同一个（内容哈希），
       所以代价只是带宽，不是正确性。
    """
    if not owners:
        return (
            f"收件箱里 {digest[:12]}… 这份字节**没有归属记录**（旧版落的，或元数据丢了）"
            " ⇒ 无法判断它是不是本次任务带来的，**拒绝取用**。"
            "出路：让这份附件随本次任务重发一遍（同一份字节会得到同一个句柄）。"
        )
    who = "、".join(_show_owner(o) for o in owners[:3])
    more = "（还有更多）" if len(owners) > 3 else ""
    return (
        f"收件箱里 {digest[:12]}… 这份字节是**别的轮次/别的任务**带来的"
        f"（落主：{who}{more}），而本次是 {_show_owner(owner)} ⇒ **拒绝取用** ——"
        " 一份证据的归属不能跨轮次推断。"
        "出路：让这份附件随本次任务重发一遍（同一份字节会得到同一个句柄）。"
    )


# ---------------------------------------------------------------------------
# 句柄
# ---------------------------------------------------------------------------


def parse_handle(handle: str) -> str:
    """把 `inbox:<sha256>` 解析成小写 sha256；**任何其它形状都拒绝**。

    这是整个模块唯一的入口校验点，所以它必须窄到没有解释空间：
    多一个字符、少一个字符、出现路径分隔符，都拒绝。
    """
    text = str(handle or "").strip()
    if not text.lower().startswith(HANDLE_PREFIX):
        raise InboxRefused(f"不是收件箱句柄：{text[:48]!r}")
    digest = text[len(HANDLE_PREFIX):].strip().lower()
    if not _SHA256_RE.match(digest):
        raise InboxRefused(
            "收件箱句柄必须是 inbox:<64 位十六进制的 sha256>，"
            f"收到的却是 {digest[:48]!r}"
        )
    return digest


def is_handle(value: Any) -> bool:
    """不抛异常的判定版本（给"这段文本里有没有句柄"这类扫描用）。"""
    try:
        parse_handle(str(value))
    except InboxRefused:
        return False
    return True


def find_handles(text: str) -> list[str]:
    """按出现顺序列出文本里的所有句柄（去重、统一小写）。

    用途是"模型把句柄写进了委派请求"这种场景：主控的模型只能写字符串，
    它照抄一个 64 位 token 比照抄一段 base64 现实得多。
    """
    found: list[str] = []
    for raw in HANDLE_IN_TEXT_RE.findall(str(text or "")):
        handle = HANDLE_PREFIX + raw[len(HANDLE_PREFIX):].lower()
        if handle not in found:
            found.append(handle)
    return found


def handle_for(data: bytes) -> str:
    """算一段字节**将会**得到的句柄（不落盘）。

    与 `stage()` 用同一个算法 —— 这条一致性由 `tools/verify_evidence.py` 钉住。
    单独抽出来是为了让"扫到附件之后先判重、再决定要不要写盘"成为可能：
    重扫一遍消息不该伴随一遍写盘。
    """
    return HANDLE_PREFIX + hashlib.sha256(data).hexdigest()


def _payload_path(root: Path, digest: str) -> Path:
    """由 sha256 拼出数据文件名。

    ⚠️ 这里**没有**任何"和调用方给的名字做拼接"的动作。`digest` 已在
    `parse_handle()` 里校验为纯十六进制，所以拼出来的路径结构上不可能越出 `root`。
    """
    return root / f"{digest}.bin"


def _meta_path(root: Path, digest: str) -> Path:
    return root / f"{digest}.json"


# ---------------------------------------------------------------------------
# 收进来 / 取回去
# ---------------------------------------------------------------------------


def stage(
    data: bytes,
    *,
    root: Path,
    mime_type: str = "",
    name: str | None = None,
) -> InboxItem:
    """把一段字节收进收件箱，返回可引用的条目。

    **幂等**：文件名就是内容的哈希，同一份字节重复收进来只写一次 ——
    所以重跑、重试、同一张图被两个上游各发一遍，都不会把收件箱撑成两份。

    写入走「临时文件 + 原子替换」，避免半个文件被读到：一个只剩一半的图片
    会让所有像素域统计失真，而失真出的结论看上去和真结论一模一样。
    """
    if not data:
        raise InboxRefused("附件里没有字节，没什么可收的。")
    if len(data) > MAX_STAGE_BYTES:
        # ⚠️ 这句写给**三条链**看，所以不能只说"像素域统计"。
        #    对图像是像素统计、对音频是波形与时长、对视频是样本表与容器长度 ——
        #    共同点是：**截断之后算出来的所有量都不是原件的量**，而报告看不出来。
        raise InboxRefused(
            f"附件 {len(data)} 字节，超过 {MAX_STAGE_BYTES} 字节上限 —— "
            "**不做截断**，截断会让长度、时长与所有后续统计失真"
            "（而失真出的结论看上去和真结论一模一样）。"
        )

    digest = hashlib.sha256(data).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    payload = _payload_path(root, digest)

    if not payload.is_file():
        # 带上 pid：同机多进程同时收同一份字节时，各写各的临时文件，
        # 最后由 `os.replace` 决定谁的名字先落地 —— 内容相同，谁赢都对。
        tmp = payload.with_name(f"{payload.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, payload)
        finally:
            tmp.unlink(missing_ok=True)

    # 归属：把**本次**的归属并进列表（幂等，重复收同一份字节不会写两遍）。
    # ⚠️ 这里读一次旧元数据再合并写回：并发 stage 同一份字节时可能丢掉一次追加，
    #    丢的后果是"**多拒一次**"（安全方向），不是"放行了不该放的"。
    owner = current_owner()
    owners = _owners_of(_read_meta(root, digest))
    if owner not in owners:
        owners.append(owner)
    del owners[:-MAX_OWNERS]

    meta = json.dumps(
        {
            "sha256": digest,
            "size": len(data),
            "mime_type": str(mime_type or ""),
            "name": name,
            "staged_by": owners,
            "staged_at": _now_iso(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    _meta_path(root, digest).write_text(meta, encoding="utf-8")

    return InboxItem(
        handle=handle_for(data),
        sha256=digest,
        mime_type=str(mime_type or ""),
        size=len(data),
        path=payload,
        name=name,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_meta(root: Path, digest: str) -> dict[str, Any]:
    """读 sidecar 元数据。**读不到就返回 `{}`**。

    ⚠️ 这个函数**现在参与判定**（2026-10-04 起），与它原来的注释正好相反 ——
       原文写的是"它不参与任何判定，也就没有'坏了会造成错误结论'的风险"。
       加了归属之后这句话**不再成立**：`staged_by` 就在这里，读不到 ⇒ 无归属
       ⇒ `resolve()` 拒绝。这是**刻意的安全方向**（fail-closed）：宁可拒一次，
       也不把一份来路不明的字节当成本次任务的证据。
       ⇒ 「同一事实写两处必然漂移」的同族：**改了一处的语义，必须回头改它的注释**，
         否则下一代会照着一句过期的承诺去改代码。

    仍然成立的那半句：`mime_type` / `name` 只是给报告填字段用的提示，
    真正的判据永远是字节本身（`forensics.analyze()` 自己 sniff 容器类型）。
    """
    try:
        value = json.loads(_meta_path(root, digest).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve(handle: str, *, root: Path | None = None) -> InboxItem:
    """按句柄取回收件箱条目；不合法 / 没登记 / 字节不可读 / **不归本次**时抛 `InboxRefused`。

    ⚠️ 这里**没有任何"退化成路径解析"的分支**。一旦允许把句柄当路径解释，
    "引用内容而不是引用位置"这条论证就全作废了 —— 而它正是本模块存在的理由。
    """
    digest = parse_handle(handle)
    base = root if root is not None else current_root()
    if base is None:
        raise InboxRefused(
            "收件箱未绑定：inbox: 句柄只在「处理带附件任务的那个进程」里有意义。"
        )

    payload = _payload_path(base, digest)
    # 先判 symlink：`is_file()` 对指向文件的符号链接也返回 True，
    # 而符号链接恰恰是"用文件名绕过目录边界"的标准手法。
    if payload.is_symlink() or not payload.is_file():
        raise InboxRefused(
            f"收件箱里没有 {digest[:12]}… 这份字节 —— 可能不属于本次任务，或已被清理。"
        )

    meta = _read_meta(base, digest)
    # ⚠️ 归属判定（2026-10-04 补）：句柄是**内容哈希**，所以"这份字节存在"与
    #    "这份字节属于本次任务"是两件事 —— 后者从模块诞生起就**没人管**，
    #    只在文件不存在时才拒绝。留下的后果是一次**溯源错误**：B 轮落下的字节
    #    被 C 轮取去当证据，而报告上看不出任何异常。
    #    归属**不跨轮次推断**：不相等就拒绝，不给"差不多就是它"这种判断。
    owners = _owners_of(meta)
    owner = current_owner()
    if owner not in owners:
        raise InboxRefused(_owner_refusal(digest, owners, owner))

    try:
        size = payload.stat().st_size
    except OSError:
        size = int(meta.get("size") or 0)

    return InboxItem(
        handle=HANDLE_PREFIX + digest,
        sha256=digest,
        mime_type=str(meta.get("mime_type") or ""),
        size=int(meta.get("size") or size),
        path=payload,
        name=meta.get("name"),
    )


def landed_at(handle: str, *, root: Path) -> str:
    """这份字节**落到这本收件箱里的时间**（`staged_at`，ISO 串）；**不在就返回空串**。

    ⚠️ 与 `resolve()` 的分工是**本模块里最要紧的一处区别**，混用会让判据恒红：

      · `resolve()` 答的是「**我这个上下文**能不能取用这份字节」⇒ 带**归属**校验
        （跨轮次 / 跨进程不许互相取用，2026-10-04 加的闸门）；
      · `landed_at()` 答的是「这份字节**有没有送到**这儿」⇒ **只看盘**，不问归属。

    为什么两件事都必须有（2026-10-06 真机实证）：端到端探针跑在**它自己那个进程**里，
    而下游两跳是**常驻服务**，各自在 `on_run_start` 里 bind 了自己的 `ctx:<uuid>`
    归属 ⇒ 探针去 `resolve()` 下游收件箱**必然**被拒（实测三本全拒，而字节三跳都在
    盘上）。那时判据报出来的是「我取不到」，而它要证的是「没送到」—— 两句话长得
    一模一样，于是**一条通着的链被判成断了**。（纪律：判据报「到处都坏」时，
    先怀疑判据，而不是先信它。）

    ⚠️ 空串的含义**只有**「不在」一种，不许退化成「文件在就算」：
       元数据里缺 `staged_at`、或 `sha256` 与句柄对不上，都算**不在** ——
       否则就把「这份字节确实是这一份」的自证丢掉了（文件名只是文件名）。

    ⚠️ `root` **必填**：「落到**哪一本**收件箱」是这个问题的组成部分，
       取 `current_root()` 兜底会把「没绑定」悄悄变成「不在」。
    """
    digest = parse_handle(handle)
    payload = _payload_path(root, digest)
    # 与 `resolve()` 同一条口径：符号链接不算落在里面（它是绕过目录边界的手法）。
    if payload.is_symlink() or not payload.is_file():
        return ""
    meta = _read_meta(root, digest)
    if meta.get("sha256") != digest:
        return ""
    at = meta.get("staged_at")
    return at if isinstance(at, str) else ""


def load(handle: str, *, root: Path | None = None) -> tuple[bytes, str]:
    """按句柄读出 `(字节, 声明过的 MIME)`。"""
    item = resolve(handle, root=root)
    try:
        data = item.path.read_bytes()
    except OSError as exc:
        raise InboxRefused(f"收件箱里的字节读不出来了：{exc}") from None
    return data, item.mime_type


# ---------------------------------------------------------------------------
# 从内容序列里把附件挑出来
# ---------------------------------------------------------------------------


def image_attachments(
    parts: Iterable[Any],
    *,
    kind: str = "image",
) -> list[tuple[str, bytes, str | None]]:
    """从内容序列里挑出**带内联字节**的附件，返回 `[(mime, 字节, 名字)]`。

    只认 `.data`，**不认 `.uri`**。URI 形态由 `image_references()` 挑出来交给
    上层（它只是一段地址，本模块既不落盘也不取回），两条路各管各的，
    免得出现"到底是谁把它弄进来的"这种说不清的溯源。

    ⚠️ **同一个入站附件，`data` 与 `uri` 只会有一个非空**，两道保证叠在一起：
    ① AIP 层每种 DataItem 只带一种（`wit.acps.aip.data_item_to_content_part` 实测：
    `FileDataItem.bytes` → `ImagePart(data=…)`，`FileDataItem.uri` → `ImagePart(uri=…)`）；
    ② wit 的 `ImagePart` 构造时强制「恰有其一」（两样都给会直接
    `ValueError: ImagePart requires exactly one of uri or data`）。
    所以"只认 data"会**静默丢掉 uri 形态的附件**，必须两条都接。

    对 ContentPart 是**鸭子类型**的（只看 `.type` / `.data` / `.mime_type`），
    刻意**不 import wit**：这样整个模块保持纯标准库，可以在没有框架的环境里
    独立断言，也不会把框架的导入失败传染给图像检测链。
    """
    found: list[tuple[str, bytes, str | None]] = []
    for part in parts or ():
        if getattr(part, "type", None) != kind:
            continue
        data = getattr(part, "data", None)
        if not isinstance(data, (bytes, bytearray)) or not data:
            continue
        raw_name = getattr(part, "name", None)
        found.append(
            (
                str(getattr(part, "mime_type", "") or ""),
                bytes(data),
                str(raw_name) if raw_name else None,
            )
        )
    return found


def image_references(
    parts: Iterable[Any],
    *,
    kind: str = "image",
) -> list[tuple[str, str, str | None]]:
    """从内容序列里挑出**只有地址、没有字节**的附件，返回 `[(mime, uri, 名字)]`。

    这是 `image_attachments()` 的互补面：有内联字节的一律归那边，本函数只收只有地址的。

    ⚠️ wit 的 `ImagePart` 在构造时就强制「uri 与 data 恰有其一」（两样都给会直接
    `ValueError`），所以**现实中**两者不会同时非空。下面的"跳过"因此是**对本模块
    鸭子类型契约的防御**：本模块刻意不 import wit，接受任何带 `.type/.data/.uri`
    的对象 —— 在那种输入里两样都可能有。这一条有专门的断言
    （`verify_evidence.py` 第 9 节，喂的正是绕开 wit 校验的 duck-typed part）。

    本模块对它**只做转述，不做取回**。取不取、能不能取由 `fetch` 的白名单策略
    决定，而"要不要取"的决定权交给上层。理由：一张入站图片里塞个 URL，
    不该等价于一次自动出网 —— 否则任何能给我发任务的对象，都能驱使本进程
    去访问任意地址。**把地址如实转告，比替调用方做决定安全。**

    ⚠️ **不 import wit**，与 `image_attachments()` 同理。
    """
    found: list[tuple[str, str, str | None]] = []
    for part in parts or ():
        if getattr(part, "type", None) != kind:
            continue
        data = getattr(part, "data", None)
        if isinstance(data, (bytes, bytearray)) and data:
            continue  # 有内联字节的归 image_attachments()，不在这里重复报
        uri = getattr(part, "uri", None)
        if not isinstance(uri, str) or not uri.strip():
            continue
        raw_name = getattr(part, "name", None)
        found.append(
            (
                str(getattr(part, "mime_type", "") or ""),
                uri.strip(),
                str(raw_name) if raw_name else None,
            )
        )
    return found


#: 泛化别名。**调用方请用这两个名字。**
#:
#: `image_attachments()` / `image_references()` 这两个名字是历史产物：它们**本来就**
#: 接受 `kind` 参数、本来就不是图像专用的，只是当初只有图像一种模态。
#: 加音频以后，`image_attachments(parts, kind="audio")` 读起来是一句假话
#: （"图像附件，种类=音频"），而**读起来是假话的代码会被照着它字面意思去改**。
#: 所以保留旧名（已有断言按它们写）并给出与 `kind` 语义一致的新名。
attachments = image_attachments
references = image_references


def stage_attachments(
    parts: Iterable[Any],
    *,
    root: Path,
    kind: str = "image",
) -> tuple[list[InboxItem], list[str]]:
    """把内容序列里的内联附件逐个收进收件箱。

    返回 `(收进来的条目, 被拒的原因)`。**逐个处理而不是一损俱损**：
    一次任务里带了 3 张图、其中 1 张超大，另外 2 张不该跟着作废 ——
    但被拒的那张必须留下原因，让调用方知道"少看了一张"。
    """
    items: list[InboxItem] = []
    refused: list[str] = []
    for mime, data, name in image_attachments(parts, kind=kind):
        try:
            items.append(stage(data, root=root, mime_type=mime, name=name))
        except InboxRefused as exc:
            refused.append(str(exc))
    return items, refused


__all__ = [
    "HANDLE_IN_TEXT_RE",
    "HANDLE_PREFIX",
    "INBOX_DIRNAME",
    "MAX_OWNERS",
    "MAX_STAGE_BYTES",
    "InboxItem",
    "InboxRefused",
    "attachments",
    "bind_owner",
    "bind_root",
    "current_owner",
    "current_root",
    "find_handles",
    "handle_for",
    "image_attachments",
    "inbox_root",
    "is_handle",
    "landed_at",
    "load",
    "parse_handle",
    "references",
    "resolve",
    "stage",
    "stage_attachments",
    "unbind_owner",
    "unbind_root",
]
