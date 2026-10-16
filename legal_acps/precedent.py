"""先例注入 —— 让裁判每一轮都能看见"同类事件当年是怎么判的"（2026-10-07）。

════════════════════════════════════════════════════════════════════
为什么是 **Hook 注入**，而不是给裁判加一个"查先例"的工具
════════════════════════════════════════════════════════════════════

  · 🔴 **步数预算不够**。裁判的完整流程要 7 步（受理→发现→派正方→等正方→
    派反方→等反方→裁定），而服务模式的 `max_steps = 8` **无解**
    （见 `docs/上线操作手册.md` 未决事项第 10 条：实测辩论支稳定
    `StepBudgetExhausted` 8/8）。再加一个"先去查先例"的工具调用，
    等于从已经撑爆的预算里再扣一步。
  · **工具调用是不确定的**。模型想不起来查，就等于没这个功能；
    而"模型没查"与"库里没有先例"在结果上**长得一模一样**。
    ⇒ 注入必须是**代码做、每轮都做**。

`WitHooks.context()` 返回 `ContextBlock`，由框架拼进上下文，**不消耗步数**。
⇒ 先例是"每轮必定在场"，而不是"看模型心情"。

════════════════════════════════════════════════════════════════════
🔴 它不是"学习"，它是"先例 + 校准"（术语必须准确）
════════════════════════════════════════════════════════════════════

`llm.py` 是固定的 provider，**没有微调管线**，权重一个字都没动。
本模块做的是：把既往裁定**当上下文**喂回去。对外要说「**裁定经验库 +
判定校准**」，不能说「自主学习 / 强化学习」—— 后者一句"奖励函数是什么"就答不上来，
而前者是**真的做了**且**说得清**。

════════════════════════════════════════════════════════════════════
失败必须是**静默降级**，不能拖垮链路
════════════════════════════════════════════════════════════════════

先例是**加分项**：库读不了、检索出错、没有相似先例 —— 任何一种都只让本轮
少一段提示，**不许让这一轮跑失败**。所以所有异常都在本 Hook 内部记进
`self.errors`，不外抛。
（本项目纪律：**旁路记录不许有权限顶掉主流程**。）

⚠️ 但"静默降级"不等于"静默失效"：降级**必须留痕**（`self.trace`），
否则"这轮没注入"与"注入了但模型没听"就分不开。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wit import ContextBlock, HookContext, WitHooks

from .ruling_store import DEFAULT_LIMIT, RulingStore
from .termination import extract_subject

#: 台账键（挂在 `private_state` 上，随这一轮的上下文走，不跨轮泄漏）
STATE_KEY = "legal.precedent"


class PrecedentHooks(WitHooks):
    """给裁判注入同类事件的既往裁定。

    只读：不写产物、不改裁定、不加工具、不消耗步数。
    """

    id = "legal.ruling.precedent"
    #: 越靠外层越先跑；这里与委派/收件箱没有先后依赖，取一个不与既有值撞的层。
    #: （既有：ChainGuard/委派 = 50，收件箱 = 100，终止兜底 = 900）
    priority = 30

    def __init__(self, *, limit: int = DEFAULT_LIMIT,
                 root: Path | None = None, verbose: bool = False) -> None:
        self.limit = max(1, int(limit))
        self.root = root
        self.verbose = verbose
        #: **降级留痕**：每一轮到底发生了什么（取到原文了吗 / 库里有几条 /
        #: 检索出几条 / 为什么没注入）。判据看的也是这一份。
        self.trace: list[dict[str, Any]] = []
        self.errors: list[str] = []

    # ── 取原文（**唯一口径**：与终止兜底共用 `extract_subject`）──────────
    @staticmethod
    def _subject_of(ctx: HookContext) -> str:
        state = getattr(ctx, "state", None)
        messages = getattr(state, "messages", None) or []
        return extract_subject(messages)

    def _lookup(self, ctx: HookContext) -> tuple[str, str]:
        """返回 `(原文, 渲染好的先例文本)`。任何异常都降级成"没先例"。"""
        subject = self._subject_of(ctx)
        rec: dict[str, Any] = {"subject_len": len(subject)}
        self.trace.append(rec)
        if not subject or subject.startswith("（未能从消息历史"):
            rec["skipped"] = "取不到被研判原文"
            return subject, ""
        try:
            store, rep = RulingStore.load(self.root)
        except Exception as exc:  # noqa: BLE001 - 加分项不许拖垮主链路
            rec["skipped"] = f"经验库加载失败：{type(exc).__name__}"
            self.errors.append(f"{type(exc).__name__}: {exc}")
            return subject, ""
        rec["loaded"] = len(store)
        rec["graded"] = sum(1 for c in store if c.graded)
        if not len(store):
            rec["skipped"] = "经验库为空"
            return subject, ""
        hits = store.search(subject, limit=self.limit)
        rec["hits"] = len(hits)
        rec["scores"] = [round(h.match.score, 2) for h in hits]
        if not hits:
            rec["skipped"] = "没有达到阈值的相似先例"
            return subject, ""
        rec["injected"] = [h.case.source for h in hits]
        return subject, RulingStore.render(hits)

    async def on_run_start(self, event: object) -> None:
        """开局就把先例算好存进 `private_state`，`context()` 只管取。

        ⚠️ 为什么要先算：`context()` 会被**每一步模型调用**都问一次，
        在里面检索等于每步都重扫一遍库（小库无所谓，但语义上它属于"这一轮"，
        不是"这一步"）。
        """
        ctx = getattr(event, "context", None)
        if ctx is None:
            return
        state = getattr(ctx, "private_state", None)
        if not isinstance(state, dict):
            return
        try:
            _, text = self._lookup(ctx)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"on_run_start: {type(exc).__name__}: {exc}")
            return
        state[STATE_KEY] = text

    def context(self, ctx: HookContext) -> list[ContextBlock]:
        try:
            state = getattr(ctx, "private_state", None)
            text = state.get(STATE_KEY) if isinstance(state, dict) else None
            if text is None:  # 没走过 `on_run_start`（判据/一次性路径）⇒ 现算
                _, text = self._lookup(ctx)
            if not text:
                return []
            return [ContextBlock(
                title="历史先例（同类事件的既往裁定 · 不是本案证据）",
                value=text,
                kind="text",
                trusted=False,   # 🔴 **不可信内容**：它只是提示，不能当证据采信
            )]
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"context: {type(exc).__name__}: {exc}")
            return []
