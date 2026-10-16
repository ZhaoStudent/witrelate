"""核对 Discovery 结果里是否还残留已删除的 demo Agent。

用途：删掉历史测试 Agent 后，**唯一能证明清理生效的判据是发现结果**，
而不是看 Agent 列表（软删除的记录仍会出现在列表里，标为「已删除」）。

用法：
    ./.venv/bin/python -m tools.check_discovery_pollution
"""

from __future__ import annotations

import asyncio
import sys
from typing import Mapping, Sequence

from dotenv_lite import load_env

load_env()

from wit.acps.discovery import AcpsDiscoveryClient  # noqa: E402

from legal_acps.acps_config import build_config  # noqa: E402
from legal_acps import wt_compat  # noqa: E402

#: 历史上被删掉的 demo Agent 的 AIC。**留着**——它们是回归判据：
#: 只要这里任何一个重新出现在发现结果里，就说明污染回来了。
RETIRED_AICS = {
    "1.2.156.3088.1.BUPT.81M3FX.YB1DAS.F56BVO.0QYM": "煽动性言论识别 Partner（demo）",
    "1.2.156.3088.1.BUPT.81M3FX.8R0Y7E.IYKGM1.0X32": "法律咨询主控（demo）",
    "1.2.156.3088.1.BUPT.81M3FX.B7G6IE.9D5VMV.1DZ8": "社会危害性评估（demo）",
}

QUERIES = ["煽动性识别", "社会危害性评估", "法律咨询", "事实核查"]


def verdict_of(
    seen: Mapping[str, str],
    failed_queries: Sequence[str],
    total_queries: int,
) -> tuple[int, list[str]]:
    """根据本轮的发现结果给出结论：(进程退出码, 要打印的行)。

    抽成**纯函数**是刻意的：这段判据就是本工具的全部价值所在
    （"污染到底还在不在"），不能只靠"联网跑一次看着对"来保证它正确 ——
    那样它在接口一变就会静默失效。离线自检直接断言这个函数的三种结局。

    三种结局：
      1 —— 仍有污染（发现结果里出现了已退役 demo 的 AIC）；
      2 —— **无法判定**：有查询失败，或一条结果都没取到；
      0 —— 确认无污染（全部查询成功、且至少检视到一条 AIC、均不在退役名单里）。

    ⚠️ 为什么必须有第 2 种：早期只要 `leaked` 为空就打印"无 demo 泄漏 ✅
    （清理生效）"，于是**接口结构变了 / 所有查询都失败 / 全返回 0 条**时，
    本工具会把"什么都没查到"当成"确认没有污染" —— 这是安全结论里最危险的
    一类假阳性：清理明明没生效，它也说生效了。同源纪律：**安全结论宁可
    "无法判定"，也不要给一个假的 ✅**。
    """
    leaked = sorted(a for a in seen if a in RETIRED_AICS)
    lines: list[str] = []

    if leaked:
        lines.append(f"结论：发现结果仍有污染 —— 泄漏 {len(leaked)} 条：")
        for a in leaked:
            lines.append(f"    {RETIRED_AICS[a]}  {a}")
        return 1, lines

    if failed_queries or not seen:
        lines.append(
            "结论：⚠️ 无法判定 —— 本轮没有取得有效的发现结果，**不能**据此宣布「无泄漏」。"
        )
        if failed_queries:
            lines.append(
                f"    · 失败/异常的查询（{len(failed_queries)} 个）："
                + ", ".join(repr(q) for q in failed_queries)
            )
        if not seen:
            lines.append("    · 所有查询都没有返回任何 ACS —— 先确认接口结构/后端地址是否变了。")
        lines.append("    先排除网络与接口问题，再重跑本工具；不要拿这一次的输出当清理证据。")
        return 2, lines

    lines.append(
        "结论：发现结果无 demo 泄漏 ✅"
        f"（{total_queries} 个查询全部成功、共检视 {len(seen)} 条 AIC，均非已退役 demo）"
    )
    return 0, lines


async def main() -> None:
    wt_compat.apply()
    print(f"（已应用 wt 兼容补丁：{wt_compat.is_applied()}）\n")

    cfg = build_config(role="leader")
    client = AcpsDiscoveryClient(acps=cfg)

    seen: dict[str, str] = {}
    failed_queries: list[str] = []
    for q in QUERIES:
        try:
            resp = await client.discover(query=q, limit=10, include_unavailable=False)
        except Exception as exc:  # noqa: BLE001
            print(f"### 查询 {q!r} -> ERR {type(exc).__name__}: {str(exc)[:140]}")
            failed_queries.append(q)
            continue
        acs_map = (resp.get("result") or {}).get("acsMap") or {}
        print(f"### 查询 {q!r} -> {len(acs_map)} 条")
        for aic, acs in acs_map.items():
            if not isinstance(aic, str):
                continue
            # acsMap 的 value 正常是 ACS 字典；结构变了也不能让本工具崩掉，
            # 因为它的唯一价值就是"给出一个可信的结论"。
            name = acs.get("name") if isinstance(acs, dict) else None
            seen[aic] = name
            short = ".".join(aic.split(".")[-2:])
            warn = "   <-- 已删除的 demo！" if aic in RETIRED_AICS else ""
            print(f"    - {name}  (…{short}){warn}")
        print()

    print("=== 汇总：发现结果里出现过的全部 AIC ===")
    if not seen:
        print("  （一条都没有 —— 见下面的结论）")
    for aic, name in seen.items():
        tag = "❌ 已删除的 demo 仍在返回" if aic in RETIRED_AICS else "✅ 正式 Agent"
        print(f"  {tag}  {name}")

    print()
    # ⚠️ 判据全部交给纯函数 verdict_of —— 它被 check_local 离线断言着，
    #    所以"接口一变就静默给假的 ✅"这件事不会再发生。
    code, lines = verdict_of(seen, failed_queries, len(QUERIES))
    for line in lines:
        print(line)
    sys.exit(code)


if __name__ == "__main__":
    asyncio.run(main())
