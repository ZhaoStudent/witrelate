"""立题守门 —— 对**命题本身**的确定性结构检查（不依赖模型）。

═══ 为什么要有这一层 ═══

裁判的「立题」环节此前**只有承诺、没有实现**：模块文档、段标题、
`JUDGE_INSTRUCTIONS` 整整一段、连前端 loading 文案都写了"裁判正在组织辩论"，
但没有任何代码路径会调用裁判去立题 —— `_judge` 只在 `reveal()` 里用过一次。
也就是说，「命题方向写反了当场指出」这道**唯一的把关点**一直是空的。

补实现时这里做了一个刻意的切分：

    结构性问题（有客观答案）  →  本模块，纯函数，确定性
    语义判断（要靠理解）      →  裁判模型（`case.open`，见 roles.py）

**为什么必须切**：命题"方向写反了"（把「他收受了好处」写成「他被冤枉了」）
是整个体验唯一的把关点 —— `roles.py` 里写得很清楚，"方向反了整个体验
就变成教人怎么造谣"。而判方向反没反，**词表就够用**，
没有理由把它交给一个可能拒答、可能输出非法 JSON 的模型。

另一半理由是验证纪律：本项目最贵的教训是"离线全绿、真机必炸"。
反过来同样致命 —— **只有模型才能跑的逻辑，离线验不了，等于没验**。
这一层是纯函数，因此可以在离线断言里被逐条穷举。

⚠️ 本模块**只报告问题，不修复、不改写命题**。命题是受理官的产物，
守门只负责"指出"。悄悄改写命题，等于让当事人看到的指控
与对方实际说的对不上 —— 那比不检查更糟。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# 判据词表
# ---------------------------------------------------------------------------

#: 价值判断词 —— 出现这些词，命题就不再是"可判真假的事实命题"。
#: 例：「他这样做很恶劣」没法辩真假，只能表态。
VALUE_JUDGMENT_WORDS: tuple[str, ...] = (
    "恶劣",
    "可耻",
    "无耻",
    "卑鄙",
    "下作",
    "令人发指",
    "过分",
    "道德败坏",
)

#: 方向倒置的**短语**判据 —— 出现即说明命题写的是「当事人冤不冤」。
#: 只放"单独出现就足够说明问题"的固定说法，不放单个动词（那要靠下面的模式）。
REVERSED_DIRECTION_WORDS: tuple[str, ...] = (
    "是清白的",
    "无辜",
    "名誉受损",
    "名声受损",
    "声誉受损",
)

#: 方向倒置的**模式**判据 —— 动词与主语之间有别的成分时，裸词表会整个失效。
#:
#: ⚠️ 这一组是**用真实输入跑出来的**（`run/rehearse_experience.py`）：
#: 裸词表里写着「被污蔑」，对「他被污蔑了」有效；但对真话里更常见的
#: 「当事人**被公司群里的人**污蔑了」**完全失效** —— 因为"被"和"污蔑"之间
#: 隔着一串别的字，`"被污蔑" in text` 是 False。
#: 而这一层是整个体验**唯一**的把关点（方向反了演练就变成演戏），
#: 所以它不能只对"自己造的例句"有效。
REVERSED_DIRECTION_PATTERNS: tuple[str, ...] = (
    r"被.{0,10}(?:冤枉|污蔑|诬陷|造谣|诽谤|抹黑)",
    r"(?:冤枉|污蔑|诬陷|造谣|诽谤|抹黑)了(?:他|她|当事人|本人|自己)",
    r"(?:他|她|当事人|本人|自己).{0,6}(?:是无辜的|是清白的)",
)

#: 疑问句标记 —— 命题必须是陈述句，问句没法判真假。
QUESTION_MARKS: tuple[str, ...] = ("？", "?")

#: 程度词：为"block / warn"分级服务，集中一处便于答辩。
LEVEL_BLOCK = "block"
LEVEL_WARN = "warn"


def _p(level: str, code: str, message: str) -> dict[str, str]:
    return {"level": level, "code": code, "message": message}


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------


def _is_question(text: str) -> bool:
    stripped = text.strip()
    return stripped.endswith(QUESTION_MARKS) or "难道" in stripped


def inspect_proposition(proposition: str) -> list[dict[str, str]]:
    """检查命题**本身**的形式。返回问题清单（空列表 = 形式上过关）。"""
    text = (proposition or "").strip()
    if not text:
        return [
            _p(
                LEVEL_BLOCK,
                "empty_proposition",
                "受理官没有给出命题 —— 这一局没有可辩的东西。",
            )
        ]

    problems: list[dict[str, str]] = []

    if _is_question(text):
        problems.append(
            _p(
                LEVEL_BLOCK,
                "interrogative_proposition",
                f"命题是疑问句（「{text}」），没法判真假。命题必须是陈述句。",
            )
        )

    hits = [w for w in VALUE_JUDGMENT_WORDS if w in text]
    if hits:
        problems.append(
            _p(
                LEVEL_WARN,
                "value_judgment",
                f"命题里含价值判断（{'、'.join(hits)}）。"
                "「这件事很恶劣」不是可判真假的命题 —— "
                "要辩的是**发生了什么**，不是**该不该骂**。",
            )
        )

    # 方向倒置：短语判据 + 模式判据，两者命中任一即报。
    # 报出的是**实际命中的那段原文**，而不是词表里的词 —— 否则用户拿着
    # "命中了『被污蔑』"去命题里找，是找不到的（那句话里根本没有这四个字）。
    reversed_hits = [w for w in REVERSED_DIRECTION_WORDS if w in text]
    for pattern in REVERSED_DIRECTION_PATTERNS:
        hit = re.search(pattern, text)
        if hit:
            reversed_hits.append(hit.group(0))
    if reversed_hits:
        problems.append(
            _p(
                LEVEL_WARN,
                "direction_reversed",
                f"命题方向疑似反了（命中「{'、'.join(reversed_hits)}」）。"
                "这一局要辩的是**对方对他的指控能不能成立**（例：「他收受了好处」），"
                "不是**他冤不冤** —— 写成后者，演练就变成演戏了。",
            )
        )

    return problems


def inspect_fact_sheet(fact_sheet: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """检查事实清单能不能撑起一场讨论。"""
    sheet = fact_sheet or {}
    items = sheet.get("items") or []
    if not items:
        return [
            _p(
                LEVEL_WARN,
                "thin_fact_sheet",
                "事实清单里没有一条可核验项 —— 辩起来会退化成双方互相表态。",
            )
        ]
    return []


def inspect_case(case: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """对整个受理结论做结构检查。空列表 = 形式上过关。"""
    if not case:
        return [_p(LEVEL_BLOCK, "missing_case", "受理环节没有产出结论，无法立题。")]
    return inspect_proposition(str(case.get("proposition") or "")) + inspect_fact_sheet(
        case.get("fact_sheet")
    )


def has_blocking(problems: list[dict[str, str]] | None) -> bool:
    """问题清单里有没有"不能继续"的（`block`）。"""
    return any((p or {}).get("level") == LEVEL_BLOCK for p in problems or [])


__all__ = [
    "LEVEL_BLOCK",
    "LEVEL_WARN",
    "QUESTION_MARKS",
    "REVERSED_DIRECTION_PATTERNS",
    "REVERSED_DIRECTION_WORDS",
    "VALUE_JUDGMENT_WORDS",
    "has_blocking",
    "inspect_case",
    "inspect_fact_sheet",
    "inspect_proposition",
]
