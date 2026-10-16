"""脚本化假模型 —— 不需要 API key 就能把整条流水线跑通。

用途有两个：
  1. 验证「接线」是否正确：工具名对不对、参数 schema 能不能填、Leader 能不能拿到结果；
  2. 当你的真实模型调不通时，用它在同一套代码路径上做对照，快速定位是"接线坏了"
     还是"模型不听话"。

原理：wit 的 ModelProvider 是**鸭子类型**（Protocol），只要具备
`capabilities` / `supports_content()` / `complete()` 三个成员即可，
不必继承任何基类。所以这里用一个查表返回固定工具调用的假对象冒充模型。

注意：它返回的是**写死的结论**，不含任何真实分析能力。
它证明的是"管道通畅"，不是"判断准确"。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Sequence

from wit import ModelResponse, OpenAIChatProvider, ToolCall

# ---------------------------------------------------------------------------
# 针对本项目各工具写死的返回值
# ---------------------------------------------------------------------------

CANNED_ARGUMENTS: dict[str, dict[str, Any]] = {
    "speech_detect_incitement": {
        "is_incitement": True,
        "kinds": ["煽动地域歧视"],
        "severity": 62,
        "targets": ["某省籍在外务工人员"],
        "evidence_spans": ["他们都该滚出去", "别让他们进厂"],
        "action_calls": ["别让他们进厂"],
        "rationale": "文本以地域身份对特定群体作整体否定，并附带了限制其就业的行动号召，"
        "符合煽动地域歧视的特征；severity 只衡量言论本身的煽动性，不含后果。",
    },
    "facts_verify_claims": {
        "claims": [
            {
                "claim": "某省籍务工人员导致了本地治安问题上升",
                "verdict": "无法核实",
                "truth_score": 50,
                "basis": ["未提供任何统计数据或来源，属于无据断言"],
            }
        ],
        "overall_truth_score": 40,
        "notes": "文本主要是情绪化归因，仅有一条可核查断言且缺乏依据，"
        "按'无法核实'处理并给中性分。",
    },
    "harm_assess_social_impact": {
        "harm_index": 58,
        "band": "",
        "dimensions": {
            "public_order": 60,
            "national_security": 10,
            "group_targeting": 85,
            "real_world_risk": 55,
            "spread_potential": 70,
            "expression_legitimacy": 15,
        },
        "legal_hints": ["《治安管理处罚法》关于寻衅滋事的规定"],
        "reasoning": "上游认定煽动性成立（severity=62）且针对特定地域群体，"
        "group_targeting 高；同时存在限制就业的行动号召，real_world_risk 不低。"
        "因未出现明确的暴力号召，未达'涉嫌违法'档，定在中高风险。",
        "counter_view": "如果发言者是在陈述自己所遭遇的具体纠纷，而非否定整个群体，"
        "则属于情绪化的个人抱怨，表达正当性会显著上升。",
    },
    "legal_issue_verdict": {
        "subject": "（占位）",
        "harm_index": 58,
        "band": "",
        "is_incitement": True,
        "truth_score": 40,
        "consistency_note": "两位专家结论一致，无需取舍。",
        "conclusion": "该文本构成针对特定地域群体的煽动性言论，"
        "其所述因果缺乏依据，社会危害性处于中高风险档。",
        "action_advice": "建议平台进一步核查，暂不建议自行扩散。",
        "disclaimer": "本结论由人工智能辅助生成，仅供法律学习与研究参考，"
        "不构成法律意见，不能替代律师或司法机关的认定。",
    },
    # ---- 反诈体验应用（legal_acps/experience）的写死返回值 ----------------
    # 注意：这些数据**只为验证接线**，内容刻意写得白话，不是真实分析结果。
    "case_accept": {
        # 第一人称场景：用户是**被指控的当事人**，不是旁观的传闻鉴定者。
        "event": "（stub）当事人在群里被人指控泄露公司资料",
        "proposition": "当事人向竞争对手提供了公司内部资料",
        "fact_sheet": {
            "event": "（stub）泄露公司资料指控",
            "items": [
                {
                    "item": "有人称曾看到当事人与竞争对手方的人同桌吃饭",
                    "source_level": "来源不明",
                    "source": "未获取到",
                    "verbatim": "（stub 占位）",
                }
            ],
            "missing": ["帖子原文", "发帖人身份", "公司内部调查结论"],
            "notes": "（stub）占位数据，仅用于验证接线是否通畅。",
        },
        "pro_position": "该指控成立：当事人确实提供了公司内部资料",
        "con_position": "现有材料不足以证明当事人提供过公司内部资料",
    },
    "case_open": {
        # 裁判的开场立题 —— 与 case_reveal 是同一裁判的**两个端点**。
        # 少任何一个，都是"承诺了立题/揭示却只实现了一半"。
        "worth_debating": True,
        "sufficiency": "勉强",
        "gaps": ["（stub）帖子原文与发帖人身份都还没有，当事人自述不能单独作为依据。"],
        "note": "（stub）这一局要辩的是：当事人有没有把公司内部资料提供给竞争对手。",
    },
    "debate_trap_argument": {
        # 刻意不给 "round"：轮次是服务端的账本，契约里也已删掉该字段。
        "message": "（stub）我朋友就在那家公司，他说这事内部早就传开了，"
        "你要真问心无愧，为什么不敢把聊天记录拿出来？别急着给自己辩解。",
        "traps": [
            {
                # ⚠️ 这一条原本被错标成「断章取义」。它其实是**举证倒置** ——
                #    对方没证明自己的主张，反而要求当事人自证清白。
                #    归错层会让复盘页把它放进"证据层"，教出"去查原始出处"这种
                #    对不上的对策。归层正确与否，直接影响这堂课教什么。
                "kind": "举证倒置",
                "excerpt": "你要真问心无愧，为什么不敢把聊天记录拿出来",
                "speaker": "pro",
                "why": "（stub）把「没有提供某项材料」偷换成「心里有鬼」，"
                "用举证责任的错置让人自证清白。",
            }
        ],
    },
    "case_reveal": {
        "event": "（stub）泄露公司资料指控",
        # 刻意不给 "rounds"：交锋轮数由服务端注入（契约里也已删掉该字段）。
        "traps": [
            {
                "kind": "举证倒置",
                "excerpt": "你要真问心无愧，为什么不敢把聊天记录拿出来",
                "speaker": "pro",
                "why": "（stub）把「没有提供某项材料」偷换成「心里有鬼」。",
            },
            {
                "kind": "诉诸情绪",
                "excerpt": "这种事传出去，你家里人怎么看你",
                "speaker": "pro",
                "why": "（stub）既不证明指控，也不回应质疑，改用羞耻感压人。",
            },
        ],
        "hot_spots": [
            "（stub）你在本轮开始解释'为什么和对方吃饭'，话题从'有没有泄露'"
            "被带到了'你的私交是否可疑'上。"
        ],
        # ⚠️ 与 hot_spots 成对：只讲"哪儿被带偏"会把当事人教成被动的。
        "solid_moves": [
            "（stub）你那句'请先把帖子原文拿出来'是对的 —— 先要原始材料，"
            "再谈内容真假，这是最有效的一步。"
        ],
        "advice": [
            "（stub）要求对方给出原始出处，而不是让第三方转述。",
            "（stub）区分「有人说」与「有材料」。",
        ],
        "summary": "（stub）你刚才那种'好像有点道理'的感觉是正常的 —— "
        "被话术带动是人的常态，认得出来就不会被当枪使。",
    },
}


class StubModel:
    """按查表返回工具调用的假模型。

    行为：
      · 本轮请求里若有可用工具 → 直接返回这些工具的调用（只回报一次）
      · 已经出现过工具结果（说明第一轮结束了）→ 返回一句收尾文本
    """

    def __init__(self, *, verbose: bool = False) -> None:
        # 复用真实 Provider 的能力声明，省去手写 ContentKind 集合
        self.capabilities = dataclasses.replace(
            OpenAIChatProvider(model="stub", api_key="stub").capabilities,
            provider="stub",
            streaming=False,  # 本类不实现 stream()
        )
        self.verbose = verbose
        self._real = OpenAIChatProvider(model="stub", api_key="stub")

    # --- Protocol 要求的三件套 -------------------------------------------

    def supports_content(self, part, *, role: str) -> bool:
        return self._real.supports_content(part, role=role)

    async def complete(self, request) -> ModelResponse:
        if self.verbose:
            print(f"    [stub] 收到请求：{len(request.messages)} 条消息，{len(request.tools)} 个工具")

        # 已有工具结果 → 收尾
        if any(m.role == "tool" for m in request.messages):
            return ModelResponse.final("（stub 模型）分析已完成，结论已通过工具提交。")

        calls: list[ToolCall] = []
        for index, tool in enumerate(request.tools):
            name = _tool_name(tool)
            if name in CANNED_ARGUMENTS:
                calls.append(
                    ToolCall(
                        id=f"stub-call-{index}",
                        name=name,
                        arguments=dict(CANNED_ARGUMENTS[name]),
                    )
                )
        if calls:
            if self.verbose:
                print(f"    [stub] 返回工具调用：{[c.name for c in calls]}")
            return ModelResponse.tools(calls)

        # 没有匹配的工具 → 直接给个终稿，避免死循环
        return ModelResponse.final("（stub 模型）没有可用的提交工具，返回占位结论。")

    async def aclose(self) -> None:
        return None


def _tool_name(tool: Any) -> str:
    """从 ModelRequest.tools 的元素里取出工具名。"""
    if isinstance(tool, str):
        return tool
    name = getattr(tool, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(tool, Mapping):
        if isinstance(tool.get("name"), str):
            return tool["name"]
        fn = tool.get("function")
        if isinstance(fn, Mapping) and isinstance(fn.get("name"), str):
            return fn["name"]
    return ""


__all__: Sequence[str] = ["CANNED_ARGUMENTS", "StubModel"]
