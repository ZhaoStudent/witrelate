"""**真机验证 · 体验应用信息分区（功能④ · #177–#179）**。

问的问题：受理官真把已知信息按「谁知道」分成 shared/mine/theirs 三桶了吗？
诱导方（指控方）真的**只**拿到 shared+theirs（墙在真模型链路上成立吗）？
收尾揭示真的带上了三桶全貌吗？可选追问相位机真跑得通吗？

场景设计（库外新案，与既有探针同族做法——刻意不撞真库）：
  · 用户（当事人）被人公开指控「私吞维修基金回扣」；
  · 事件自述里给足三种信息：公开帖（shared）/ 他声称握有转账聊天记录
    （theirs）/ 我当晚在杭州出差、登机牌还留着（mine）；
  · 当事人三轮辩白**刻意守口如瓶**（不提杭州/登机牌）——mine 哨兵在
    诱导方输入里出现只可能是墙漏了，别拿「用户自己说漏嘴」当托词。

判据（全部只认本轮事实；软硬分开）：

  P1｜受理产出三桶：case.partition 的 shared/mine/theirs 各至少一条 fact
      非空（信息已在自述里给足三样；受理官指令要求分桶）。
  P2｜诱导方输入隔离（**本改造唯一不能错的性质**）：每一条
      debate.trap_argument 输入都不含 mine 哨兵（杭州/登机牌），且开场
      那条含 theirs 哨兵（转账）——指控方连自己声称的材料都拿不到，
      信息差话术就没法围绕它构建。
  P3｜揭示带三桶：case.reveal 输入含「信息分区」段标题 + mine 哨兵
      （裁判中立，三桶全貌必须在场——与 P2 恰好互为反面）；且含
      「信息差教学」指令段（裁判要会把概念落到原话，2026-10-19 教学话术）。
  P5｜信息差复盘板块落进 reveal 结果：state.reveal.info_gap_review 是
      结构化数据（三桶原文 + 三条概念句），由**服务端纯函数**算——
      即便裁判降级（judge_ok=false）板块也必须在（教学不依赖模型）。
  P4｜可选追问相位机（条件判据，两条路都合法）：
      · followup 非空 ⇒ 必须停在 awaiting_details、此时辩白被拒（409）、
        give_details 的补充文本必须进第二次受理输入、且新分区的 mine
        依然不进诱导方输入；
      · followup 空 ⇒ 直接开辩，记为「受理官认为信息已够」（合法路径）。

软观察（不进退出码）：诱导方有没有真的拿「转账记录」施压——那是模型
措辞的自由，「判据跟着模型措辞走 = 不是判据」。

用法（s1 仓根）：
    ./.venv/bin/python -m run._probe_experience_partition
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv_lite import load_env  # noqa: E402

load_env(ROOT / ".env")

from legal_acps.experience.session import ExperienceSession  # noqa: E402
from legal_acps.experience.partition import BUCKET_LABELS  # noqa: E402

#: 事件自述（库外新案）：一句话给足三种信息。
EVENT = (
    "有人在业主群里公开发帖，说我私吞了小区维修基金的回扣，"
    "还说他手里握有我和施工方的微信转账聊天记录。"
    "可那天晚上我其实在杭州出差，登机牌还留着。"
)

#: mine 哨兵：只出现在自述后半句（用户辩白刻意不提 ⇒ 它们在诱导方
#: 输入里出现只可能来自分区泄漏）。
MINE_SENTINELS = ["杭州", "登机牌"]

#: theirs 哨兵：指控方声称的材料关键词（受理官大概率沿用原话）。
THEIRS_SENTINEL = "转账"

#: 当事人三轮辩白：守口如瓶，只逼对方亮牌——不提 mine 半个字。
USER_REPLIES = [
    "他空口白牙就这么一说，你倒是把所谓的记录拿出来给大家看看啊。",
    "拿不出来就一直拖，现在又开始打感情牌拉同情，这不明摆着心虚吗。",
    "群里围观的人越来越多，我就问一句：他到底拿不拿得出实锤？",
]

#: give_details 的补充回答（若受理官追问）：把 mine 说全，看它进不进
#: 第二次受理、以及**依然**不进诱导方输入。
DETAILS_ANSWER = (
    "我当晚在杭州出差，登机牌还留着，同行的同事都能作证；"
    "他说有什么记录我一次都没见过。"
)


async def run_probe() -> int:
    print("=" * 70)
    print("  真机验证 · 体验应用信息分区（受理三桶 / 诱导方隔离墙 / 揭示三桶）")
    print("=" * 70)

    session = ExperienceSession(difficulty="轻松")  # 3 轮，省预算
    log: list[tuple[str, str]] = []
    orig_run = session._run

    async def logged_run(runtime, prompt, *, tool, what, **kw):
        log.append((tool, prompt))
        return await orig_run(runtime, prompt, tool=tool, what=what, **kw)

    session._run = logged_run  # type: ignore[method-assign]

    # ── 开局（受理官受理；followup 非空会停在 awaiting_details）──────
    st = await session.start(EVENT)
    followup = str(st.get("followup") or "")
    print(f"  受理完成：phase={st.get('phase')}  followup={followup or '（无）'}")

    # ── P4 条件相位机 ──────────────────────────────────────────────────
    p4_note = ""
    p4_extra = True  # 各路径的附加硬条件（下面就地验证）
    if st.get("phase") == "awaiting_details":
        # 闸门：此时辩白必须被拒（补充信息 ≠ 辩白）
        reply_rejected = False
        try:
            await session.reply("我现在就要辩")
        except RuntimeError:
            reply_rejected = True
        if not reply_rejected:
            print("  ❌ P4 前置：awaiting_details 时 reply 没被拒")
            return 1
        st = await session.give_details(DETAILS_ANSWER)
        accepts = [p for t, p in log if t == "case.accept"]
        if len(accepts) < 2 or DETAILS_ANSWER[:12] not in accepts[-1]:
            print("  ❌ P4 前置：补充回答没进第二次受理输入")
            return 1
        p4_note = f"追问路径（followup={followup[:30]}…，补充已被受理）"
        p4_extra = len(accepts) >= 2  # 上面 return 1 已挡，这里留正向事实
    else:
        # 免问路径：受理官没追问 ⇒ start 必须直接开辩（停在 debating
        # 且已有诱导方输入），追问才暂停、不追问就暂停 = 相位机坏了。
        p4_note = "免问路径（受理官认为信息已够，followup 为空）"
        p4_extra = st.get("phase") == "debating"

    case = (st.get("case") or {})
    part = (case.get("partition") or {})

    def bucket_facts(key: str) -> list[str]:
        return [
            str((it or {}).get("fact") or "").strip()
            for it in (part.get(key) or [])
            if str((it or {}).get("fact") or "").strip()
        ]

    # ── 辩论 3 轮（真模型；用户守口如瓶）──────────────────────────────
    for i, text in enumerate(USER_REPLIES, 1):
        st = await session.reply(text)
        pro_texts = [t.get("text") or "" for t in (st.get("turns") or [])
                     if t.get("role") == "pro"]
        shown = (pro_texts[-1] if pro_texts else "")[:80].replace("\n", " ")
        print(f"  第 {i} 轮 · 诱导方：{shown}…")

    # ── 收尾揭示（裁判三桶全貌）──────────────────────────────────────
    await session.reveal(None)

    # ── 判据 ──────────────────────────────────────────────────────────
    results: list[tuple[str, bool, str]] = []

    shared, mine, theirs = (bucket_facts(k) for k in ("shared", "mine", "theirs"))
    p1 = bool(shared and mine and theirs)
    results.append(("P1 受理产出三桶", p1,
                    f"shared={len(shared)} mine={len(mine)} theirs={len(theirs)}"
                    f"（{p4_note}）"))

    traps = [p for t, p in log if t == "debate.trap_argument"]
    leaked = [(n + 1, s) for n, p in enumerate(traps)
              for s in MINE_SENTINELS if s in p]
    theirs_seen = bool(traps) and any(THEIRS_SENTINEL in p for p in traps)
    p2 = bool(traps) and not leaked and theirs_seen
    results.append(("P2 诱导方输入隔离（mine 不进 / theirs 在场）", p2,
                    f"{len(traps)} 轮输入；泄漏 {leaked or '无'}；"
                    f"theirs 哨兵{'在' if theirs_seen else '❌缺'}"))

    reveal_prompt = next((p for t, p in log if t == "case.reveal"), "")
    p3 = ("信息分区" in reveal_prompt
          and BUCKET_LABELS["mine"] in reveal_prompt
          and any(s in reveal_prompt for s in MINE_SENTINELS)
          and "信息差教学" in reveal_prompt)
    results.append(("P3 揭示带三桶全貌+教学指令段", p3,
                    f"段标题{'✓' if '信息分区' in reveal_prompt else '✗'}"
                    f" mine 桶{'✓' if BUCKET_LABELS['mine'] in reveal_prompt else '✗'}"
                    f" mine 哨兵"
                    f"{'✓' if any(s in reveal_prompt for s in MINE_SENTINELS) else '✗'}"
                    f" 教学段"
                    f"{'✓' if '信息差教学' in reveal_prompt else '✗'}"))

    # P5 信息差复盘板块（服务端纯函数算，教学不依赖模型）
    rev = session.state.reveal or {}
    ig = rev.get("info_gap_review")
    from legal_acps.experience import partition as _xp
    p5 = isinstance(ig, dict) and ig.get("title") == _xp.INFO_GAP_TITLE \
        and any(s in str(ig.get("mine") or []) for s in MINE_SENTINELS) \
        and THEIRS_SENTINEL in str(ig.get("theirs") or []) \
        and _xp.INFO_GAP_CLAIM_NOTE in str(ig.get("theirs_note")) \
        and _xp.INFO_GAP_CARD_NOTE in str(ig.get("mine_note")) \
        and _xp.INFO_GAP_FIELD_NOTE in str(ig.get("shared_note"))
    results.append(("P5 信息差复盘板块（服务端固定文案）", p5,
                    f"judge_ok={rev.get('judge_ok')}；板块标题"
                    f"{'✓' if isinstance(ig, dict) and ig.get('title') == _xp.INFO_GAP_TITLE else '✗'}"
                    f" 三桶原文{'✓' if isinstance(ig, dict) and any(s in str(ig.get('mine') or []) for s in MINE_SENTINELS) else '✗'}"
                    f" 概念句{'✓' if isinstance(ig, dict) and _xp.INFO_GAP_CLAIM_NOTE in str(ig.get('theirs_note')) else '✗'}"))

    # P4 收尾：追问路径 = 补充触发了第二次受理（前置已挡失败）；免问路径 =
    # start 直接开辩没停在追问相位。附加硬条件就地收进判据。
    results.append(("P4 相位机（" + p4_note + "）", p4_extra,
                    f"受理调用 {len([t for t, _ in log if t == 'case.accept'])} 次"))

    print("\n" + "─" * 70 + "\n  判据\n" + "─" * 70)
    for name, ok_, detail in results:
        print(f"  [{'✅' if ok_ else '❌'}] {name}：{detail}")

    # ── 软观察（不进退出码）：诱导方**论点**有没有拿「转账记录」施压 ──
    pro_points = [t.get("text") or "" for t in (st.get("turns") or [])
                  if t.get("role") == "pro"]
    press = [n + 1 for n, txt in enumerate(pro_points) if THEIRS_SENTINEL in txt]
    print(f"\n  [观察·不进退出码] 诱导方论点引用 theirs 哨兵的轮次：{press or '无'}"
          "（措辞引用是模型的自由）")
    if mine or theirs:
        print("  [观察] 受理官三桶样例：")
        print(f"    shared · {shared[0][:60] if shared else '（空）'}")
        print(f"    mine   · {mine[0][:60] if mine else '（空）'}")
        print(f"    theirs · {theirs[0][:60] if theirs else '（空）'}")

    return 0 if all(o for _, o, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_probe()))
