"""演练一局，把**实际输出**原样打印出来（人可读）。

    ./.venv/Scripts/python -m run.rehearse_experience
    ./.venv/Scripts/python -m run.rehearse_experience --real          # 真模型（需 .env）
    ./.venv/Scripts/python -m run.rehearse_experience --difficulty 困难 --mark 1,3
    ./.venv/Scripts/python -m run.rehearse_experience --json          # 再附一份原始 JSON

═══ 为什么需要这个脚本 ═══

离线验证（`tools.verify_experience`）证明的是"接线对、性质没被破坏"，
但它**不展示内容** —— 断言里有 `>= 1`，看不到那句话到底长什么样。

而这一轮要回答的问题恰恰是内容层面的：立题那句话说清了吗？守门有没有误报？
当事人标的轮次和真实话术对得上吗？这些只能"发一句真话进去、看它回了什么"。

所以这个脚本**不做任何修饰**：四段输出（立题 / 交锋 / 自我标记对照 / 揭示）
原样打印，好和坏都照原样显示 —— 它是一份证据，不是一个演示。

⚠️ 默认走假模型（`--stub`）：内容是按表返回的写死示例，
只能证明"链路通、字段齐"，**证明不了判断质量**。
要判断质量必须加 `--real`（读 .env 里的 OPENAI_MODEL / OPENAI_API_KEY）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from legal_acps.experience.session import DIFFICULTY_ROUNDS, ExperienceSession

#: 默认的一次真实输入（第一人称当事人视角，越接近原话越好）。
DEFAULT_EVENT = "有人在公司群里说我为了跳槽把内部资料卖给了竞争对手，还说有人看到我跟对方的人一起吃饭"

#: 默认的辩白（当事人会说的话，刻意保留"普通人第一反应"的粗糙）。
DEFAULT_REPLIES = [
    "这件事我完全不知情，请先把帖子的原文和发布时间拿出来。",
    "我确实和对方的人吃过饭，那是行业会议认识的，跟资料没有任何关系。",
    "你们没有任何材料就认定我做了，这样传出去对我的影响你们负责吗？",
    "如果拿不出原始记录，我要求你在群里澄清。",
]

LINE = "─" * 72


def head(title: str) -> None:
    print()
    print(LINE)
    print(f"  {title}")
    print(LINE)


async def rehearse(args: argparse.Namespace) -> int:
    session = ExperienceSession(
        use_stub=not args.real,
        difficulty=args.difficulty,
        verbose=args.verbose,
    )
    marks = {int(x) for x in args.mark.split(",") if x.strip().isdigit()}

    head("输入（当事人自述）")
    print(f"  {args.event}")
    print(f"  模型：{'假模型（内容为写死示例，只验证接线）' if not args.real else '真实模型'}")
    print(f"  难度：{args.difficulty} → 本局上限 {DIFFICULTY_ROUNDS[args.difficulty]} 轮")

    state = await session.start(args.event)

    head("① 立题（确定性守门 + 裁判）")
    case = state.get("case") or {}
    print(f"  命题：{case.get('proposition') or '（未给出）'}")
    opening = state.get("opening") or {}
    judge = opening.get("judge")
    if judge:
        print(
            f"  裁判：{'值得辩' if judge.get('worth_debating') else '不值得辩'}"
            f" · 材料{judge.get('sufficiency')}"
        )
        print(f"  裁判的话：{judge.get('note')}")
        for gap in judge.get("gaps") or []:
            print(f"  ⚠ 还缺：{gap}")
    else:
        print("  裁判：**未给出立题意见**（本局照常进行，但这一环没有把关）")
    checks = opening.get("checks") or []
    if checks:
        for chk in checks:
            flag = "⛔" if chk.get("level") == "block" else "⚠"
            print(f"  {flag} 守门[{chk.get('code')}]：{chk.get('message')}")
    else:
        print("  守门：未发现问题（命题形式与事实清单都过关）")
    print(f"  本局上限：{state.get('max_rounds')} 轮")

    head("② 交锋")
    for turn in state.get("turns") or []:
        who = "指控方" if turn["role"] == "pro" else "当事人"
        print()
        print(f"  【{who} · 第 {turn['round']} 轮】")
        print(f"    {turn.get('text')}")
        if turn["role"] == "pro" and turn["round"] in marks:
            print("    （你标了这句有问题）")

    for i, reply in enumerate(args.replies):
        if session.state.finished:
            break
        # ⚠️ 轮次号取**服务端的账本**（`pro_rounds`），不是 `pro_rounds + 1`。
        #    这正是本项目反复踩的那件事：轮次是服务端数的，别处再算一遍就一定会错。
        #    （这个脚本第一版写成了 `+ 1`，于是当事人的每一句都比实际大一轮 ——
        #     而它连"看起来很奇怪"都不算，只是悄悄地和对面差一位。
        #     加这个脚本、用真实输入跑一遍的价值就在这里。）
        print()
        print(f"  【当事人 · 第 {session.state.pro_rounds} 轮】")
        print(f"    {reply}")
        state = await session.reply(reply)
        notice = state.get("notice")
        if notice:
            print(f"    （{notice}）")
            break
        last = (state.get("turns") or [])[-1]
        print()
        print(f"  【指控方 · 第 {last['round']} 轮】")
        print(f"    {last.get('text')}")
        if last["round"] in marks:
            print("    （你标了这句有问题）")

    head("③ 揭示（实际输出）")
    reveal = await session.reveal(suspected=sorted(marks) or None)

    print(f"  本局共交锋 {reveal.get('rounds')} 轮")
    for group in reveal.get("trap_groups") or []:
        print()
        print(f"  ── {group.get('layer')} ──")
        for trap in group.get("traps") or []:
            print(f"    · [{trap.get('kind')}] 「{trap.get('excerpt')}」")
            print(f"      {trap.get('why')}")

    report = reveal.get("suspect_report") or {}
    print()
    print("  ── 你自己标的，准不准 ──")
    print(f"    你标了：{report.get('marked') or '（一个都没标）'}")
    print(f"    抓对了：{report.get('hit') or []}")
    print(f"    多疑了：{report.get('false_alarm') or []}")
    print(f"    漏掉了：{report.get('missed') or []}")
    print(f"    真值（真的用了话术的轮次）：{report.get('truth') or []}")

    for key, title in (
        ("hot_spots", "最容易带偏你的地方"),
        ("solid_moves", "你这几句是站得住的"),
        ("advice", "下次怎么防"),
    ):
        items = reveal.get(key) or []
        print()
        print(f"  ── {title} ──")
        if items:
            for item in items:
                print(f"    · {item}")
        else:
            print("    （空）")
    print()
    print("  ── 复盘 ──")
    print(f"    {reveal.get('summary')}")

    if args.json:
        head("④ 原始 JSON（未经整理，供核对字段）")
        print(json.dumps({"state": state, "reveal": reveal}, ensure_ascii=False, indent=2))

    print()
    print(LINE)
    print("  演练结束。以上是**实际输出**，不是设计文档里的理想输出。")
    print(LINE)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="演练一局并打印实际输出")
    parser.add_argument("--event", default=DEFAULT_EVENT, help="当事人自述")
    parser.add_argument("--difficulty", default="标准", choices=list(DIFFICULTY_ROUNDS))
    parser.add_argument("--mark", default="", help="你标为可疑的轮次号，逗号分隔，如 1,3")
    parser.add_argument(
        "--reply",
        action="append",
        dest="replies",
        help="当事人的一句辩白，可重复传；不传则用内置的四句",
    )
    parser.add_argument("--real", action="store_true", help="用真模型（需 .env）")
    parser.add_argument("--json", action="store_true", help="附一份原始 JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印每轮迭代信息")
    args = parser.parse_args()
    args.replies = args.replies or DEFAULT_REPLIES

    if args.real:
        from dotenv_lite import load_env

        load_env()

    return asyncio.run(rehearse(args))


if __name__ == "__main__":
    sys.exit(main())
