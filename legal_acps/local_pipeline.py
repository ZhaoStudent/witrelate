"""本地流水线 —— 不走 ACPs，直接在进程内串起两个专家。

为什么要这个东西：ACPs 解决的是「跨进程/跨机器的可信互联」，那是工程问题。
你的项目真正的难点在**判断质量**（prompt 写得对不对、量表合不合理），
而那个问题不需要联网就能调。所以先用这个脚本把业务逻辑调好，
再去碰注册和证书 —— 否则一旦注册失败，你分不清是逻辑错了还是网络错了。

用法（在 witrelate 目录下）：

    # 用假模型验证接线，不需要任何 key
    ./.venv/bin/python -m legal_acps.local_pipeline --stub

    # 用真实模型跑（自动读 .env 里的 OPENAI_MODEL / OPENAI_API_KEY）
    ./.venv/bin/python -m legal_acps.local_pipeline --text "待研判文本"
"""

from __future__ import annotations

import argparse
import asyncio
import json

from wit import WitRuntime

from dotenv_lite import load_env

from .agents import build_harm_partner, build_incitement_partner
from .contracts import band_of, band_rationale
from .acps_config import build_config
from .stub_model import StubModel

DEMO_TEXT = (
    "最近治安这么差，还不都是那些外地来的打工的闹的。"
    "他们都该滚出去，别让他们进厂，本地人才有活路。"
)


def _extract_tool_results(observations: list[dict]) -> dict[str, dict]:
    """从 run 的 observations 里取出每个工具的结构化返回值。

    注意：observations 里的 key 是**能力 id**（带点，如 `speech.detect_incitement`），
    而工具名是点换成下划线的形式（`speech_detect_incitement`）。
    两种写法都登记一遍，调用方用哪个都取得到。
    """
    out: dict[str, dict] = {}
    for obs in observations:
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


def _canonical(results: dict[str, dict]) -> list[tuple[str, dict]]:
    """只保留带点的规范键，避免重复打印。"""
    return [(k, v) for k, v in results.items() if "." in k]


async def main(text: str, *, use_stub: bool, verbose: bool) -> None:
    if use_stub:
        model = StubModel(verbose=verbose)
        mode = "假模型（StubModel，结论为写死的示例值）"
    else:
        from .llm import build_model

        model = build_model()
        mode = "真实模型"
    print(f"模型：{mode}\n")

    # ---- 阶段一：煽动性识别 + 真伪拆解 -----------------------------------
    print("=" * 68)
    print("阶段一  Partner A：incitement-detector")
    print("=" * 68)
    # 注意：传 acps= 只是因为 Agent 声明了 AcpsDirect 协议，编译时需要这个配置对象；
    # 这里**不调用** up_until_ready()/serve()，所以不会发起任何注册或网络请求。
    runtime_a = WitRuntime(
        build_incitement_partner(model),
        workspace=".wit-local/incitement",
        acps=build_config(role="incitement"),
    )
    run_a = await runtime_a.run(
        "请分析下面这段文本：\n\n" + text
    )
    print(f"status={run_a.status}  迭代={run_a.iterations} 轮")
    a_results = _extract_tool_results(run_a.observations)
    for name, payload in a_results.items():
        print(f"\n[{name}]")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    # ---- 阶段二：社会危害评估（把上游结论一并递过去）---------------------
    print()
    print("=" * 68)
    print("阶段二  Partner B：harm-assessor")
    print("=" * 68)
    runtime_b = WitRuntime(
        build_harm_partner(model),
        workspace=".wit-local/harm",
        acps=build_config(role="harm"),
    )
    upstream = json.dumps(a_results, ensure_ascii=False, indent=2)
    run_b = await runtime_b.run(
        f"待研判文本：\n{text}\n\n上游专家的结论如下：\n{upstream}\n\n"
        "请在此基础上完成社会危害性评估。"
    )
    print(f"status={run_b.status}  迭代={run_b.iterations} 轮")
    b_results = _extract_tool_results(run_b.observations)
    for name, payload in _canonical(b_results):
        print(f"\n[{name}]")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    # ---- 汇总 -------------------------------------------------------------
    harm = b_results.get("harm_assess_social_impact")
    if not harm:
        print("\n[提示] 没有拿到危害评估结果。你可以考虑：")
        print("       · 确认模型的 tool call 能力（部分模型/网关不支持 function calling）")
        print("       · 打开 --verbose 看假模型的接线轨迹")
        return

    index = int(harm["harm_index"])
    print()
    print("=" * 68)
    print("汇总")
    print("=" * 68)
    print(f"  harm_index = {index:+d}   档位：{band_of(index).value}")
    print(f"  依据：{band_rationale(index)}")
    print(f"  为真程度   = {a_results.get('facts_verify_claims', {}).get('overall_truth_score', '?')}/100")
    print(f"  煽动强度   = {a_results.get('speech_detect_incitement', {}).get('severity', '?')}/100")
    print("\n  对照：真实部署时，上面这两步会由 Leader 通过 ACPs 派发到两个独立进程，")
    print("        你拿到的结构化结论应当与本地产出完全一致 —— 这正是把契约先钉死的价值。")


def cli() -> None:
    load_env()  # 自动读项目根目录的 .env

    parser = argparse.ArgumentParser(description="本地串起两个专家，验证业务逻辑")
    parser.add_argument("--text", "-t", default=DEMO_TEXT, help="待研判文本")
    parser.add_argument("--stub", action="store_true", help="用假模型，不需要 API key")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印假模型的接线轨迹")
    args = parser.parse_args()
    asyncio.run(main(args.text, use_stub=args.stub, verbose=args.verbose))


if __name__ == "__main__":
    cli()
