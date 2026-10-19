"""体验会话编排 —— 当事人替自己辩白，与"编排他的那个人"交锋，最后由裁判揭示。

一次完整会话（**必须以揭示收尾**）：

    ① 受理官          「有人在网上这么说我」 →  可判真假的命题 + 事实清单
    ② 立题守门        确定性检查命题形式（疑问句 / 价值判断 / 方向倒置）
    ③ 裁判·立题       语义判断：这道命题值不值得辩、材料够不够（只给一句话）
    ④ 诱导方（第 1 轮）先声夺人，用话术给当事人一个"很有道理"的开场
    ⑤ 用户（当事人）   用户逐轮回应 —— 他在替自己辩白
    ⑥ 诱导方（第 n 轮）针对他的回应，继续话术化
    ⑦ 裁判·揭示       汇总全场话术，指出他被带偏的环节**与他站住的地方**，给辨识建议

    ⚠️ 命题的方向是「对方在指控他」，所以**正方 = 指控方 = 他现实里要面对的人**，
    反方 = 他自己。方向反了整个体验就变成"教人怎么造谣"。

为什么用**进程内编排**而不是走 ACPs：

  这条链路必须**等人**。ACPs 的任务是"一次 run 跑完就结束"，
  中途没有可以等待真人输入的位置；而体验课的价值恰恰在于用户每一轮都要停下来思考。
  所以编排放在进程内 —— 这与"主链路由 6 个已注册 Agent 通过 ACPs 真实互联"
  并不矛盾：两者是同一套契约的两条执行路径。

  副作用是好的：进程内可以自由设 `max_steps`（服务模式被 wit 钉死在 8 步），
  体验流程因此不受步数预算挤压。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wit import RunConfig, WitRuntime

from ..contracts import group_fallacies
from .opening import inspect_case
from .partition import (
    build_deceiver_prompt,
    build_opening_prompt,
    build_reveal_prompt,
    has_partition,
)
from .roles import build_deceiver, build_intake, build_judge
from .tool_scope import MAX_TOKENS_KEY, ONLY_TOOL_KEY

WORKSPACE_ROOT = Path(".wit-local")

#: 单次 run 的步数上限。进程内编排不受服务模式的 8 步限制。
DEFAULT_MAX_STEPS = 6

#: **揭示**环节的步数预算。
#:
#: 事实（真机实测 2026-10-01 夜）：普通环节「调工具 + 收尾文本」正好 2 步，
#: 而揭示要通读全场交锋记录再汇总，实测 3~5 步。
#:
#: ⚠️ 老实说：揭示最初那次失败，我按"5 步 vs 6 步余量太小"推断是撞了上限，
#:    于是加了这一档。**后来真机诊断把这个推断否掉了** —— 失败的两次尝试都是
#:    `iterations=1`（第一步就结束），把预算加到 24 也一样。
#:    真正的原因是 `REVEAL_MAX_TOKENS`（见下）。这条留着是因为
#:    "揭示的工作量确实最大、给宽一点不花钱"（预算是**上限**不是消耗），
#:    不是因为它是那次故障的解 —— 别把它当成修好的原因。
REVEAL_MAX_STEPS = 12

#: **揭示**环节的输出 token 预算。
#:
#: ⚠️ 这才是揭示偶发失败的真正原因，来自真机诊断：
#:
#:     {"status": "completed", "iterations": 1, "max_steps": 12, "tools_seen": []}
#:     {"status": "completed", "iterations": 1, "max_steps": 24, "tools_seen": []}
#:
#: `iterations=1` + `tools_seen=[]` = 模型第一轮就结束、**一个工具都没调**。
#: 而 `llm.py` 早就把这条失效模式写在模块 docstring 里了：
#: 「若 max_tokens 给小了，token 会被思考过程吃光，content 会是空字符串
#:   —— 表现为『模型没输出』。」
#:
#: 揭示是**全链路上下文最长的一环**（全场交锋记录 + 事实清单 + 话术 JSON），
#: 而 `llm.DEFAULT_MAX_TOKENS = 8192` 是全局写死的、没有按环节区分。
#: 5 轮交锋比 3 轮更长 ⇒ 它成了唯一会偶发"空输出"的一环
#: （这正好解释了观感：轻松档几乎不犯，标准档偶尔犯）。
REVEAL_MAX_TOKENS = 16_384

#: 某个动作第一次没交出结论时，额外重试几次。
#:
#: 依据同样是真机：同一个输入连跑三局，揭示的迭代数是 5 / 3 / **一步都不调**
#: ——"模型这一轮没调工具、直接以 completed 收尾"是**偶发**的。
#: 一次偶发就让用户丢掉刚经历的整局，代价与概率完全不成比例。
RUN_RETRIES = 1

#: 揭示拿不到裁判结论时的**降级文案**。
#:
#: ⚠️ 降级**不等于**把这一页填满。服务端自己算得出来的只有两样：
#:    诱导方逐轮自曝的话术、以及当事人自己标记的命中/多疑/漏标。
#:    「最容易带偏你的地方」「你站得住的地方」「下次怎么防」三节**必须由裁判判断**，
#:    拿不到就**空着**并说明原因 —— **宁可显示"没有"，不显示一段像模像样的话。**
DEGRADED_SUMMARY = (
    "这一局裁判没有交回复盘（模型未提交结论），所以本页只显示服务端自己算得出来的部分："
    "诱导方逐轮自曝的话术，以及你自己标记的准不准。"
    "「最容易带偏你的地方」「你站得住的地方」「下次怎么防」三节没有内容 —— "
    "它们要由裁判判断，我们不会用一段读起来很像的话把它填上。"
)

#: 默认的最大交锋轮数（含诱导方开场）。到顶后强制进入揭示环节。
MAX_EXCHANGE_ROUNDS = 5

#: 难度 → 本局最大交锋轮数。**口径只在这里定义一次**（服务端 / 前端 / 验证都引它）。
#:
#: "困难"给到 7 轮不是为了"更长"，而是为了**多几次在压力下回应的机会**：
#: 被一种手法带偏一次是意外，被同一种手法连着带偏三次才是习惯 ——
#: 这堂课要改的正是习惯。
DIFFICULTY_ROUNDS: dict[str, int] = {"轻松": 3, "标准": 5, "困难": 7}

DEFAULT_DIFFICULTY = "标准"

#: 难度 → 给诱导方的一句话（**覆盖**提示词里"一两条就够"的默认分寸）。
DIFFICULTY_BRIEF: dict[str, str] = {
    "轻松": "只用**一层**话术（证据层或修辞层挑一个），而且要留下明显的追问口子。",
    "标准": "一两处话术即可，证据层与修辞层混着来。",
    "困难": "可以**多叠几层**（两层都用上），并且要针对当事人上一轮的话头反问。",
}


@dataclass
class Turn:
    """一轮发言。"""

    round: int
    role: str  # "pro" = 诱导方（指控方）；"user" = 用户（当事人，替自己辩白）
    text: str
    traps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SessionState:
    event: str = ""
    case: dict[str, Any] | None = None
    #: 立题结论：`{"checks": [...], "judge": {...} | None}`
    #:
    #: `checks` 是**确定性守门**（opening.py）的结果，`judge` 是**裁判模型**的结果。
    #: 刻意**分成两个键**而不是合成一个：合成之后就分不清"这条是程序判的
    #: 还是模型判的"，出问题时不知道该查哪一边 —— 而这两边的排查方式完全不同
    #: （前者看代码，后者看提示词与模型）。
    opening: dict[str, Any] | None = None
    turns: list[Turn] = field(default_factory=list)
    reveal: dict[str, Any] | None = None
    #: 本局最大交锋轮数（由难度决定，见 DIFFICULTY_ROUNDS）
    max_rounds: int = MAX_EXCHANGE_ROUNDS
    #: 当事人在局中标记为"可疑"的轮次号（他自己的判断，用来和真实话术对照）
    suspects: list[int] = field(default_factory=list)
    #: 会话相位（2026-10-19 信息分区改造）：
    #:   · "awaiting_details" —— 受理官问了澄清问题，等当事人补充（可跳过）；
    #:   · "debating"         —— 已开辩（或无需补充直接开辩）。
    #: 老会话 / stub 不产 followup ⇒ 永远不进 awaiting_details，相位只是新增
    #: 事实字段，不动既有判定（finished 仍以 reveal 为准）。
    phase: str = "debating"
    #: 受理官的澄清问题原文（phase=awaiting_details 时非空）。
    followup: str = ""

    @property
    def finished(self) -> bool:
        return self.reveal is not None

    @property
    def pro_rounds(self) -> int:
        return sum(1 for t in self.turns if t.role == "pro")

    def to_dict(self, *, include_hidden: bool = False) -> dict[str, Any]:
        """序列化当前状态。

        ⚠️ `include_hidden=False`（默认）时**剥掉每轮的 traps** ——
        那是诱导方的自曝标记，一旦送到前端就等于当场揭穿，体验课就废了。
        只有揭示环节结束后，`reveal.traps` 才允许公开（那是教学设计的一部分）。
        """
        turns: list[dict[str, Any]] = []
        for t in self.turns:
            item: dict[str, Any] = {"round": t.round, "role": t.role, "text": t.text}
            if include_hidden:
                item["traps"] = t.traps
            turns.append(item)
        return {
            "event": self.event,
            "case": self.case,
            "opening": self.opening,
            "finished": self.finished,
            "phase": self.phase,
            "followup": self.followup,
            "pro_rounds": self.pro_rounds,
            "max_rounds": self.max_rounds,
            "suspects": list(self.suspects),
            "turns": turns,
            "reveal": self.reveal,
        }


class RevealRequired(RuntimeError):
    """会话还没揭示就想结束 —— 体验应用的红线。"""


def _group_traps(traps: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把话术按**层**分好再交给前端。

    ⚠️ 分层**委托给 `contracts.group_fallacies`**，这里只做"dict → 列表"的形状转换。
    原因两条，都是踩过的坑：

    1. 分层口径只存在于 `contracts` 一处。这里再写一份 `kind` 判断，
       等于把口径抄第二遍 —— 加一类话术时必漏一处（本项目在
       `PORTS` / `serve.sh` / `systemd-install.sh` 上已经踩过一次同样的坑）。
    2. 这里**原先确实**是另写的一份，于是 `contracts.group_fallacies`
       成了没人调用的死代码 —— 而它其实还带着一个真缺陷（只认 `.kind`，
       对 dict 一律归"未归类"）。**同一功能两处实现，缺陷会被复制一份，
       验证却只覆盖其中一条路径。**

    空层不输出（前端不必为此写判断）；认不出层的话术**一定保留**，
    落在 `未归类` 组里显式暴露 —— 静默丢掉最难查：链路全绿，用户只是"少看到一条"。
    """
    return [
        {"layer": name, "traps": items}
        for name, items in group_fallacies(traps).items()
        if items
    ]


def _suspect_report(
    suspects: list[int] | None, turns: list[Turn]
) -> dict[str, Any]:
    """把当事人自己的"怀疑"与真实话术对照。

    ⚠️ 这份对照**必须在服务端算**：真值（哪几轮**真的**用了话术）只存在于
    `Turn.traps`，而它从不进公开视图（外泄=当场揭穿）。前端只能看到 `message`，
    让它自己"猜"出命中率，猜出来的数字是编的 —— 那比不给更糟。

    ⚠️ 两个方向都要报：`hit`（抓对了）与 `false_alarm`（多疑了）。
    只报命中率会把"见谁都怀疑"教成好习惯 —— 现实中过度怀疑同样会毁掉沟通，
    而这个应用教的是**分寸**，不是**戒备**。
    """
    truth = sorted({t.round for t in turns if t.role == "pro" and t.traps})
    marked = sorted({int(n) for n in suspects or []})
    truth_set = set(truth)
    marked_set = set(marked)
    return {
        "marked": marked,
        "hit": [n for n in marked if n in truth_set],
        "false_alarm": [n for n in marked if n not in truth_set],
        "missed": [n for n in truth if n not in marked_set],
        "truth": truth,
    }


def _extract_tool_results(observations: list[dict[str, Any]] | None) -> dict[str, dict]:
    """从 run 的 observations 里取出结构化返回值。

    key 同时登记「能力 id（带点）」与「工具名（点换下划线）」两种写法，
    调用方用哪个都取得到。
    """
    out: dict[str, dict] = {}
    for obs in observations or []:
        name = obs.get("tool") or obs.get("name") or ""
        result = obs.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                continue
        if not (isinstance(result, dict) and name):
            continue
        out[name] = result
        out[name.replace(".", "_")] = result
    return out


class ExperienceSession:
    """一局反诈体验。每个实例独立，不共享状态。"""

    def __init__(
        self,
        *,
        use_stub: bool = False,
        verbose: bool = False,
        max_steps: int = DEFAULT_MAX_STEPS,
        difficulty: str = DEFAULT_DIFFICULTY,
    ) -> None:
        if use_stub:
            from ..stub_model import StubModel

            model: Any = StubModel(verbose=verbose)
        else:
            from ..llm import build_model

            model = build_model()

        self._max_steps = max(2, int(max_steps))
        # ⚠️ 认不出的难度**回落到默认档**，但不改写调用方传进来的原文：
        #    一个笔误（"普通" / "hard"）不该让整局开不了，也不该被静默当成
        #    "用户选了轻松" —— 回落是回落，别装作用户选过。
        self.difficulty = (
            difficulty if difficulty in DIFFICULTY_ROUNDS else DEFAULT_DIFFICULTY
        )
        self.verbose = verbose
        self._intake = WitRuntime(
            build_intake(model), workspace=str(WORKSPACE_ROOT / "exp-intake")
        )
        self._judge = WitRuntime(
            build_judge(model), workspace=str(WORKSPACE_ROOT / "exp-judge")
        )
        self._deceiver = WitRuntime(
            build_deceiver(model), workspace=str(WORKSPACE_ROOT / "exp-deceiver")
        )
        self.state = SessionState()
        #: 最近一次 `_run` 的诊断（取不到结论时由调用方写进错误信息）。
        self.last_run: dict[str, Any] | None = None

    # -- 对外三个动作 ------------------------------------------------------

    async def start(self, event: str) -> dict[str, Any]:
        """开局：受理官受理 → 立题守门 + 裁判立题 → 诱导方先声夺人。"""
        event = (event or "").strip()
        if not event:
            raise ValueError("请先说你被人怎么编排了")
        self.state = SessionState(
            event=event, max_rounds=DIFFICULTY_ROUNDS[self.difficulty]
        )

        # ① 受理官（对应主链路的 legal-consult-leader）
        case = await self._run(
            self._intake,
            "**当事人**说，有人在网上这样编排他：\n\n"
            f"{event}\n\n"
            "请把**指向他的那条指控**整理成一个可判真假的命题、一份可核验事实清单、"
            "「指控方 / 当事人」两方立场"
            "（正方 = 指控成立；反方 = 指控不成立，由他自己来当），"
            "并按「谁知道」把已知信息分进 shared/mine/theirs 三桶。"
            "分区信息明显不足时，可以在 followup 里问当事人**一个**澄清问题。"
            "调用 `case_accept` 提交。",
            tool="case.accept",
            what="受理",
        )
        if case is None:
            raise RuntimeError(
                "受理环节没有拿到结构化结论。排查顺序：\n"
                "  1. 模型是否支持 function calling；\n"
                "  2. 是否缺 OPENAI_MODEL / OPENAI_API_KEY（见 .env）；\n"
                "  3. 用 --stub 跑一遍，看是不是接线问题。"
            )
        self.state.case = case

        # 追问是**可选的**（2026-10-19 用户拍板）：受理官给了 followup 才暂停，
        # 用户可答（give_details）可跳过（skip_details）；stub / 信息够时
        # followup 为空 ⇒ 直接开辩，老流程一个字不变。
        followup = str(case.get("followup") or "").strip()
        if followup:
            self.state.phase = "awaiting_details"
            self.state.followup = followup
            return self.state.to_dict()
        await self._after_intake()
        return self.state.to_dict()

    async def give_details(self, text: str) -> dict[str, Any]:
        """当事人回答受理官的澄清问题 → 重新受理（分区按新信息修订）→ 开辩。"""
        if self.state.phase != "awaiting_details":
            raise RuntimeError("当前没有待回答的受理问题。")
        text = (text or "").strip()
        if not text:
            # 空回答 = 事实上的跳过；交给同一个出口，别让前端多学一种调用。
            return await self.skip_details()
        case0 = self.state.case or {}
        merged = await self._run(
            self._intake,
            "当事人先前说：\n\n"
            f"{self.state.event}\n\n"
            "你上一轮的受理结论（命题 / 事实清单 / 立场 / 信息分区）：\n\n"
            f"{json.dumps(case0, ensure_ascii=False, indent=2)}\n\n"
            f"你随后问了当事人：**{self.state.followup}**\n\n"
            f"当事人补充回答：\n\n{text}\n\n"
            "请**更新**受理结论（命题/事实清单/立场/shared-mine-theirs 分区都按"
            "新信息修订，补充的信息归入合适的桶——他自己知道实情放 mine，"
            "对方声称的材料放 theirs，双方都看得见的放 shared）。"
            "**这次不要再追问**（followup 必须留空）。调用 `case_accept` 提交。",
            tool="case.accept",
            what="受理·补充",
        )
        notice = ""
        if merged is None:
            # 旁路纪律：补充受理失败不报废整局 —— 沿用原结论开辩，如实告知。
            # （用户没做错任何事，为他保留一局完整体验比抛 502 更重要。）
            notice = "（补充受理这一步没有成功，本局沿用先前的受理结论继续。）"
        else:
            self.state.case = merged
        await self._after_intake()
        if notice:
            return self.state.to_dict() | {"notice": notice}
        return self.state.to_dict()

    async def skip_details(self) -> dict[str, Any]:
        """当事人跳过澄清问题 —— 用现有受理结论直接开辩。"""
        if self.state.phase != "awaiting_details":
            raise RuntimeError("当前没有待回答的受理问题。")
        await self._after_intake()
        return self.state.to_dict()

    async def _after_intake(self) -> None:
        """受理完成（无论是否经过追问）后的共同出路：立题 + 诱导方开场。"""
        self.state.phase = "debating"
        self.state.followup = ""
        # ②③ 立题（确定性守门 + 裁判语义立题）
        await self._opening()
        # ④ 诱导方第 1 轮
        await self._deceiver_round()

    async def reply(self, user_text: str) -> dict[str, Any]:
        """当事人替自己辩白 → 指控方回应。"""
        if self.state.finished:
            raise RuntimeError("本局已结束（已揭示）。要再体验请重新开局。")
        if self.state.phase == "awaiting_details":
            # 相位闸：没解决澄清问题就进辩场，会把"补充信息"当成"辩白"——
            # 语义完全不同的两条路，不能悄悄替用户选。
            raise RuntimeError(
                f"受理官还想问你一个问题：{self.state.followup} "
                "（可以回答，也可以跳过；回答后辩论才会开始。）"
            )
        user_text = (user_text or "").strip()
        if not user_text:
            raise ValueError("发言不能为空")
        if not self.state.case:
            raise RuntimeError("还没有开局，请先调用 start()")

        self.state.turns.append(
            Turn(round=self.state.pro_rounds, role="user", text=user_text)
        )
        if self.state.pro_rounds >= self.state.max_rounds:
            # 到顶了：不再让诱导方继续，直接提示该揭示了。
            # 逼用户走揭示环节，是本应用的红线。
            return self.state.to_dict() | {"notice": "已达本局轮数上限，请进入复盘揭示。"}
        await self._deceiver_round()
        return self.state.to_dict()

    async def reveal(self, suspected: list[int] | None = None) -> dict[str, Any]:
        """收尾揭示 —— **必须走到这一步，本局才算完成**。

        `suspected` 是当事人在局中标记为"可疑"的轮次号。它的作用是把这堂课
        从「看裁判表演」变成「他自己先判一次，再跟真相对照」——
        同一份内容，带着判断听和被动听，留下的东西差得很远。
        """
        if suspected is not None:
            # 去重 + 按数字排序。前端可能因重复点击送来重复值，
            # 不去重的话"标了 3 次同一轮"会被算成"标了 3 轮"。
            # 非数字一律丢弃（模型/前端不该送进来，但这类脏值不值得崩一整局）。
            self.state.suspects = sorted(
                {int(n) for n in suspected if str(n).lstrip("-").isdigit()}
            )
        if self.state.reveal is not None:
            return self.state.reveal
        if not self.state.case:
            raise RuntimeError("还没有开局，无可揭示的内容")

        case = self.state.case or {}
        traps_all = [t for turn in self.state.turns for t in turn.traps]
        judge_out = await self._run(
            self._judge,
            # 提示词构建抽成了纯函数（partition.build_reveal_prompt）——
            # 信息分区段在函数里加（有分区才出现），判据可直接对它做行为断言。
            build_reveal_prompt(
                case,
                self._transcript(),
                json.dumps(traps_all, ensure_ascii=False, indent=2),
            ),
            tool="case.reveal",
            what="揭示",
            # ⚠️ 揭示的两项预算都单独给：它是**上下文最长**的一环。
            #    token 那条才是真机故障的解；步数那条只是"给宽不花钱"，
            #    别把两者混为一谈（见两个常量的注释）。
            max_steps=REVEAL_MAX_STEPS,
            max_tokens=REVEAL_MAX_TOKENS,
        )
        if judge_out is None:
            # ⚠️ **不抛错报废整局** —— 用户已经聊完 N 轮，把整局丢掉是最坏的结果。
            #    降级保留"服务端自己算得出来"的那部分（话术清单 + 当事人对照），
            #    三节需要裁判判断的内容**空着**并说明原因。
            #    `judge_ok=False` 是给前端与断言看的**显式标记**：
            #    没有它，"降级"会变成一个安静的常态 —— 页面照常能看，
            #    而没人知道裁判其实一直没干活（本项目最忌讳的那种缺陷）。
            judge_out = {
                "judge_ok": False,
                "hot_spots": [],
                "solid_moves": [],
                "advice": [],
                "summary": DEGRADED_SUMMARY,
            }
        else:
            judge_out = {**judge_out, "judge_ok": True}
        # ⚠️ 与每轮的 `round` **同源**：交锋轮数由**服务端注入**，不采纳裁判的
        #    自报值（`DeceptionReveal` 里已刻意没有 `rounds` 字段，这里补真值）。
        #    前端用它渲染"本局共交锋 N 轮"（index.html → renderReveal）——
        #    这个数字一旦由模型给，用户复盘时看到的轮数就可能是假的；而服务端
        #    本来就知道（`pro_rounds` = 诱导方发言数 = 交锋轮数）。
        #    用 `{**judge_out, ...}` 新建对象：即便上游意外带了旧 `rounds`（例如
        #    契约被改回去、或测试注入了脏值），也在这里被覆盖掉。
        self.state.reveal = {
            **judge_out,
            "rounds": self.state.pro_rounds,
            # ⚠️ 分层的**输入**按裁判有没有交回结论来选：
            #    · 裁判交回了 → 用它汇总的那份（它读过全场，比逐轮自曝更完整）；
            #    · 裁判没交回（降级）→ 用**诱导方逐轮自曝的原始记录**。
            #      这份服务端手上本来就有（`Turn.traps`），不依赖裁判 ——
            #      降级页因此仍然能给出真实的话术清单，而不是一句"没有内容"。
            "trap_groups": _group_traps(
                (judge_out.get("traps") or []) if judge_out.get("judge_ok") else traps_all
            ),
            # 当事人自己的判断 vs 真实话术。**同样由服务端算**，理由与 trap_groups
            # 一致：真值（哪几轮真的用了话术）只存在于 `Turn.traps`，它从不外发。
            "suspect_report": _suspect_report(self.state.suspects, self.state.turns),
        }
        return self.state.reveal

    # -- 内部 -------------------------------------------------------------

    async def _opening(self) -> None:
        """立题：**先自己判（确定性），再让裁判判（语义）**。两步都不阻断开局。

        为什么两步都要：

          · 只有确定性守门 → 判不了"这道命题值不值得辩"这类语义问题；
          · 只有模型立题   → 模型拒答 / 输出非法 JSON 时，**唯一的把关点直接失效**，
            而它恰恰是最不该失效的一层（"方向写反了整个体验就变成教人怎么造谣"）。

        ⚠️ 与受理环节的区别：受理拿不到结论就**必须**抛错（没有命题根本没法辩），
        而立题拿不到结论**不抛错** —— 但也不假装成功：`judge` 保持 `None`，
        前端会显式显示"裁判未给出立题意见"。**静默给出安全结论是最危险的缺陷**，
        这里采用的是同一原则：宁可显示"没有"，不显示一个假的"一切正常"。
        """
        case = self.state.case or {}

        # ② 确定性守门 —— 纯函数，不依赖模型
        checks = inspect_case(case)

        # ③ 裁判语义立题（提示词构建在 partition.build_opening_prompt ——
        #    裁判是中立方，有分区时附三桶全貌）
        judge = await self._run(
            self._judge,
            build_opening_prompt(case),
            tool="case.open",
            what="立题",
        )

        self.state.opening = {"checks": checks, "judge": judge}

    async def _run(
        self,
        runtime: WitRuntime,
        prompt: str,
        *,
        tool: str,
        what: str,
        max_steps: int | None = None,
        max_tokens: int | None = None,
    ) -> dict | None:
        """跑一轮，取回 `tool` 的结构化结论；取不到就**再试一次**。

        这一层做的每一件事都有真机依据（不是"保险起见"）：

        1. **本轮只暴露目标工具 + 给足 token 预算** —— 经 `RunConfig.metadata`
           交给 `ToolScopeHooks`。两条都是揭露出过故障的地方（详见 `tool_scope.py`）。
        2. **预算是逐次可覆盖的** —— 揭示的上下文比任何环节都长。
        3. **失败了再试一次** —— 见 `RUN_RETRIES`。

        取不到时把**诊断**留在 `self.last_run`：
        "跑到第几步、什么状态、**模型输出了多少字**、这轮调过哪些工具"。
        ⚠️ `final_text_len` / `reasoning_len` 是**决定性**的两项：
        揭示那次故障就是靠它们（配合 `iterations=1`）才分清"模型用文本回答了"
        还是"思考吃光 token、输出为空"—— 后者才是真因，前者只是我的第一个猜测。
        """
        model_tool = tool.replace(".", "_")
        budget = max(2, int(self._max_steps if max_steps is None else max_steps))
        tokens = int(max_tokens) if max_tokens else 0
        attempts: list[dict[str, Any]] = []

        for _ in range(RUN_RETRIES + 1):
            metadata: dict[str, Any] = {ONLY_TOOL_KEY: model_tool}
            if tokens:
                metadata[MAX_TOKENS_KEY] = tokens
            run = await runtime.run(
                prompt,
                # ⚠️ 收敛手段是"本轮 tools 只剩一个"，**不是** `tool_choice`：
                #    推理模型的 thinking 模式遇到 tool_choice 直接 HTTP 400。
                RunConfig(max_steps=budget, metadata=metadata),
            )
            results = _extract_tool_results(run.observations)
            got = results.get(tool) or results.get(model_tool)
            attempts.append(
                {
                    "status": run.status,
                    "iterations": run.iterations,
                    "max_steps": budget,
                    "max_tokens": tokens or "（沿用默认）",
                    "final_text_len": len(run.final_text or ""),
                    "reasoning_len": len(run.reasoning_text or ""),
                    "tools_seen": sorted(
                        {
                            name
                            for name in (
                                str(obs.get("tool") or obs.get("name") or "")
                                for obs in run.observations or []
                            )
                            if name
                        }
                    ),
                }
            )
            if self.verbose:
                mark = "" if got is not None else "  ⚠ 未取到结论"
                print(
                    f"    [{what}] status={run.status} 迭代={run.iterations} 轮"
                    f" 预算={budget} token={tokens or '默认'}"
                    f" 输出={len(run.final_text or '')}字/思考{len(run.reasoning_text or '')}字{mark}"
                )
            if got is not None:
                self.last_run = {"tool": model_tool, **attempts[-1]}
                return got
            # 没拿到就换个条件再试：步数与 token 都往上放 ——
            # 空输出最常见的原因（思考吃光 token）正好是这两个变量管的。
            budget *= 2
            tokens = tokens * 2 if tokens else 0
        self.last_run = {"tool": model_tool, "attempts": attempts}
        return None

    async def _deceiver_round(self) -> None:
        """让诱导方针对最近一条用户发言（或开场）输出一轮带话术的回应。"""
        case = self.state.case or {}
        nxt = self.state.pro_rounds + 1
        # 🔴 信息隔离墙在 build_deceiver_prompt 里（代码层）：它只拼
        #    shared+theirs，mine 无路可进 —— 提示词层"叮嘱别偷看"不算墙。
        prompt = build_deceiver_prompt(
            case, self._transcript(), nxt,
            self.difficulty, DIFFICULTY_BRIEF.get(self.difficulty, ""),
        )
        out = await self._run(
            self._deceiver, prompt, tool="debate.trap_argument", what=f"诱导方第{nxt}轮"
        )
        if out is None:
            # 诱导方哑火时不能让整局崩掉：给一句中性过渡，并如实告知
            self.state.turns.append(
                Turn(round=nxt, role="pro", text="（本轮没有生成内容，可以直接进入复盘。）")
            )
            return
        # ⚠️ 轮次号一律用服务端的账本 `nxt`，**绝不采纳模型给的 `out["round"]`**。
        #
        #    旧写法是 `int(out.get("round") or nxt)` —— `X or Y` 的语义是"X 为真就用 X"，
        #    而 `out` 正是**诱导方那个模型**调用 `debate_trap_argument` 时自己填的参数。
        #    于是"记录里的字段"交给了"被记录的那一方"来写，后果有三：
        #      · 模型写 1 → 每一轮都显示"第 1 轮"（复盘时用户正是靠轮次回忆"我第几轮被带偏"）；
        #      · 模型写 "第一轮" → `int()` 直接 ValueError，冒泡到 Web 层变成 HTTP 400，
        #        用户明明输入得好好的却收到"发送失败"；
        #      · 模型写 99 / 2.5 / 0 → 静默篡改 / 无声截断 / 因 falsy 而"偷偷"回落。
        #    这条链的教学价值就是一份**可信的记录**，所以轮次号必须由服务端自己数
        #    （`nxt = pro_rounds + 1`，服务端本来就知道第几轮，根本不需要问模型）。
        #    与红线②同源：`traps` 自曝标记绝不外泄；记录字段绝不外授。
        self.state.turns.append(
            Turn(
                round=nxt,
                role="pro",
                text=str(out.get("message") or ""),
                traps=list(out.get("traps") or []),
            )
        )

    def _transcript(self) -> str:
        lines: list[str] = []
        for turn in self.state.turns:
            who = "【指控方】" if turn.role == "pro" else "【当事人·你】"
            lines.append(f"{who} {turn.text}")
        return "\n".join(lines)

    # -- 安全阀 -----------------------------------------------------------

    def assert_revealed(self) -> None:
        """离开前必须已揭示。Web 层在"结束会话"时调用它。"""
        if not self.state.finished:
            raise RevealRequired(
                "本局还没有走收尾揭示。出于教学伦理，体验应用不允许"
                "在没有复盘的情况下结束 —— 否则就变成真的在误导人了。"
            )


__all__ = [
    "DEFAULT_DIFFICULTY",
    "DIFFICULTY_BRIEF",
    "DIFFICULTY_ROUNDS",
    "MAX_EXCHANGE_ROUNDS",
    "ExperienceSession",
    "RevealRequired",
    "SessionState",
    "Turn",
]
