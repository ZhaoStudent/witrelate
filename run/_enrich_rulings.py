"""批量充实裁定经验库 —— 用**不同事件**跑一次性裁判轮（治偏食的源侧，2026-10-07）。

背景：库里 25/30 条裁定是同一食堂事件（每次真机验证都用同一句测试输入）。
一次性路径已实证可行（2026-10-07T03:39Z 那轮：8 步收尾、完整委派链、出裁定）。

本脚本从案例库挑**与已有裁定不重叠**的事件，串行跑一轮辩论。每轮：
  · 输入 = 案例的谣言原文（当事人视角）+ 触发词（明确要求发起辩论并出具裁定）
  · 唯一硬判据 = output/ 新增 ruling-*.json（status=completed 不算数）
  · 串行：同时只跑一轮（并发登录 Registry 会顶掉彼此会话 —— 既有教训）

⚠️ 挑案**现场派生**，不写死清单（2026-10-07 第二版改）：
  第一版 PICKS 是手写的 4 个 id —— 写死清单 = 第二份事实源：今天跑过哪些、
  库里已经有哪些，答案在 `output/ruling-*.json` 里，而清单会跟它漂移。
  ⇒ 现在「该跑谁」在启动那一刻从库里现场算：案例文本与任一既有事件
  用**折叠同一把尺子**（`_norm_event` + `FOLD_RATIO`）比对，重叠 ⇒ 跳过并如实报告。

⚠️ 比对有已知局限（如实说清，不许粉饰）：案例 `text` 是**当事人视角的整段话**
  （含"我想确认两件事…"这类法律咨询尾巴），而库里的 `event` 是裁判提炼出的
  谣言一句话 —— 同一事件的两种表述 ratio 会被尾巴稀释。所以「跳过」判据是
  **保守的**（宁多跑不漏跑）：识别不出来的已覆盖事件会被再跑一轮，落盘后由
  库侧 `_fold_duplicates` 收拾（它们本来就会折成一组）。错跑一轮的代价 ≈ 3 分钟；
  漏跑一个新事件的代价 = 库里少一类经验 —— 前者便宜得多。

用法（服务器上，维护者手动跑）：

    cd ~/witrelate && ./.venv/bin/python -m run._enrich_rulings          # 只跑未覆盖案例
    cd ~/witrelate && ./.venv/bin/python -m run._enrich_rulings --all   # 全量重跑（A/B 的 B 面）

⚠️ 跑之前确认 `acps-case-round.timer` 的下一跳不在本窗口内
   （并发登录 Registry 会顶掉彼此会话 —— `systemctl list-timers acps-case-round.timer`）。

⚠️ `--all` 的语义（2026-10-07，第 ⑥ 步 A/B 实验）：
   已覆盖案例**再跑一轮**，落盘后由库侧 `_fold_duplicates` 收进同组。
   ⚠️ 折叠代表规则是「**有标注的轮优先**」——新轮无标注 ⇒ 旧轮继续当代表
   ⇒ **台账数字不会自己变**。要看 B 面必须由维护者把 `ground_truth.jsonl`
   各条的 `source` 重标到该事件**最新一轮**的裁定文件名（基准是「事件官方
   定性」，与轮次无关，人重标合法；A 面数字已存档于手册 §15.5）。
"""
import argparse

import asyncio
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path("/home/ubuntu/witrelate")
sys.path.insert(0, str(ROOT))

from dotenv_lite import load_env  # noqa: E402

load_env(ROOT / ".env")

from wit import RegistryPasswordCredentials, RunConfig, WitRuntime  # noqa: E402
from legal_acps.acps_config import build_config  # noqa: E402
from legal_acps.agents import build_agent  # noqa: E402
from legal_acps.case_library import CASES  # noqa: E402
from legal_acps.llm import build_model  # noqa: E402
from legal_acps.ruling_store import FOLD_RATIO, RulingStore, _norm_event  # noqa: E402
from legal_acps.run_agent import _active_aic  # noqa: E402

TRIGGER = "请发起辩论核查这件事的真伪，并出具裁定书。"

#: 兜底触发词。**为什么需要**（2026-10-07 实测，7 轮里 2 轮踩中）：
#: 案例文本若以**法律咨询口吻**为主（「我想知道编造的人要负什么责任」「是不是比
#: 凭空造谣更严重」），裁判会把它当成 Leader 转来的咨询**直接作答** ——
#: `iterations=1`、零工具调用、不出裁定书。这不是链路坏了，是**任务被读错了**。
#: ⇒ 第一轮零产物时，用「传闻优先 + 点名裁定工具」的框架**重试一次**。
RETRY_TRIGGER = (
    "【待核查传闻】\n{text}\n\n"
    "请受理这起事实争议：先独立立题采证，再组织正反双方辩论这条传闻是否属实，"
    "最后用 fact_issue_ruling 工具出具裁定书。"
)

#: 覆盖判定的 BM25 分数线。**实测分布定**（2026-10-07，8 个事件的库）：
#:   已覆盖案例（库里已有同类事件）：矿泉水 28.6 / 地震 31.3 / 烟花厂 57.6 /
#:   银行 26.0 / AI换脸 66.8 / 自来水异味 65.5 —— 最低 26.0
#:   未覆盖案例：7.6–15.0 —— 最高 15.0
#: ⇒ 取 20.0（两组中点，距两边都有余量）。
#: ⚠️ 样本只有 13 个案例，是**薄**的实测；但它只影响「这一轮跑不跑」（错跑 =
#: 3 分钟 + 库侧折叠会收拾；漏跑 = 少一类经验），不是运行时闸门。
#: **库事件数明显变化后要重跑这组分布**。
COVER_SCORE = 20.0


def pick_todo(store: RulingStore) -> tuple[list[str], list[str]]:
    """案例库 → (已覆盖的, 待跑的)。**与折叠同一把尺子** + BM25 互补，不另写一套。

    为什么要两把尺子（2026-10-07 实测，缺一不可）：
      · ratio（折叠同款）认不出「案例原文 vs 裁判提炼句」的同一事件 ——
        案例原文带「我想确认两件事…」的咨询尾巴，ratio 被稀释到 0.07–0.66
        （刚跑过的 4 个全在 0.34 以下，识别不出）。
      · BM25 认得出（同一事件共享大量实词，26.0–66.8 分），但对**同题材不同
        事件**也会给分 —— 所以必须与 ratio 取**或**并用，且分数线取在实测
        两组之间（26.0 / 15.0 ⇒ 20.0）。
    两种都命中才算「已覆盖」是不对的（ratio 会漏）；任一命中就算 —— 错杀
    （把未覆盖判成已覆盖）的后果 = 漏跑一个新事件，所以 BM25 线取在两组
    **中点偏保守**的位置。
    """
    covered, todo = [], []
    for c in CASES:
        n = _norm_event(c.text)
        by_ratio = any(
            n == _norm_event(rc.event)
            or SequenceMatcher(None, _norm_event(rc.event), n).ratio() >= FOLD_RATIO
            for rc in store
        )
        by_bm25 = any(h.match.score >= COVER_SCORE
                      for h in store.search(c.text, limit=3, min_score=0.0))
        (covered if (by_ratio or by_bm25) else todo).append(c.id)
    return covered, todo


async def run_one(case_id: str, text: str) -> bool:
    """跑一轮。第一轮零产物 ⇒ 用「传闻优先」框架**重试一次**（见 RETRY_TRIGGER）。"""
    out_dir = ROOT / "output"
    agent = build_agent("fact-judge", build_model(), self_aic=_active_aic("fact-judge"))
    runtime = WitRuntime(agent, workspace=".wit/fact-judge", acps=build_config(role="fact-judge"))
    runtime.save_acs()
    credentials = RegistryPasswordCredentials.from_env()
    await runtime.up_until_ready(credentials=credentials)

    async def _attempt(inp: str) -> tuple[bool, object]:
        before = set(p.name for p in out_dir.glob("ruling-*.json"))
        run = await runtime.run(inp, config=RunConfig(max_steps=24))
        new = sorted(set(p.name for p in out_dir.glob("ruling-*.json")) - before)
        print(f"[{case_id}] status={run.status} iterations={run.iterations} 新增={new or '无'}")
        for obs in run.observations or []:
            print(f"    · {obs.get('tool') or obs.get('name') or '?'} ({obs.get('status', '?')})")
        return bool(new), run

    ok, _ = await _attempt(f"{text}\n\n{TRIGGER}")
    if not ok:
        print(f"[{case_id}] 第一轮零产物（模型没走仲裁流程）→ 用「传闻优先」框架重试")
        ok, _ = await _attempt(RETRY_TRIGGER.format(text=text))
        if not ok:
            print(f"[{case_id}] 重试仍零产物 —— 如实记失败，不许装作跑过")
    return ok


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="全量重跑 13 案（A/B 的 B 面；跳过覆盖判定）")
    ap.add_argument("--case", nargs="+", metavar="CASE_ID",
                    help="只跑指定案例（跳过覆盖判定；治弃权补专文后单案重跑）")
    ap.add_argument("--batch", metavar="DRAFT_JSONL", default="",
                    help="跑校准基准草稿批次（tools.derive_gt_cases 产出、"
                         "人核后的 gt_candidates.jsonl；覆盖判定跳过）")
    args = ap.parse_args()

    store, _ = RulingStore.load()
    st0 = store.stats()
    print(f"跑前库况：{st0.get('total')} 个事件 / {st0.get('raw_total')} 份裁定"
          f"（top_event_share={st0.get('top_event_share'):.3f}）")
    batch: list[tuple[str, str]] = []   # (id, input)
    if args.batch:
        # 🔴 只吃**人核后的**草稿：字段齐全 + ground_truth 一律「不实」
        #    （derive 工具的筛子保证；人核剔除的条目直接从文件里删）。
        draft = Path(args.batch)
        if not draft.is_file():
            raise SystemExit(f"找不到草稿文件：{draft}")
        import json
        for i, ln in enumerate(draft.read_text(encoding="utf-8").splitlines()):
            if not ln.strip():
                continue
            obj = json.loads(ln)
            if obj.get("ground_truth") != "不实":
                raise SystemExit(f"草稿里混进了非「不实」基准：{obj.get('title')}")
            batch.append((f"gt-{i + 1}-{obj['rumor_text'][:12]}", obj["input"]))
        print(f"--batch：跑 {len(batch)} 条人核草稿（覆盖判定跳过）")
    if args.case:
        unknown = [c for c in args.case
                   if c not in {x.id for x in CASES}]
        if unknown:
            raise SystemExit(f"未知案例 id：{unknown}（可选：见 case_library.CASES）")
        todo = list(args.case)
        print(f"--case：只跑 {len(todo)} 案（覆盖判定跳过）：{todo}")
    elif args.batch:
        todo = [cid for cid, _ in batch]
    elif args.all:
        todo = [c.id for c in CASES]
        print(f"--all：全量重跑 {len(todo)} 案（覆盖判定跳过；"
              "跑完须人工重标 ground_truth 的 source 到最新轮，台账才有 B 面）")
    else:
        covered, todo = pick_todo(store)
        print(f"案例库 {len(CASES)} 条：已覆盖 {len(covered)}（{covered}），待跑 {len(todo)}")
    if not todo:
        print("没有待跑的案例 —— 每个案例在库里都已有同类事件的经验。")
        return 0

    wins, fails = [], []
    for cid in todo:
        if args.batch:
            text = next(t for c, t in batch if c == cid)
        else:
            text = next(c.text for c in CASES if c.id == cid)
        try:
            ok = await run_one(cid, text)
        except Exception as exc:  # noqa: BLE001
            print(f"[{cid}] 异常：{type(exc).__name__}: {exc}")
            ok = False
        (wins if ok else fails).append(cid)

    store2, _ = RulingStore.load()
    st = store2.stats()
    print(f"\n跑后库况：{st.get('total')} 个事件 / {st.get('raw_total')} 份裁定"
          f"（top_event_share={st.get('top_event_share'):.3f}）")
    print(f"成功 {len(wins)} {wins}")
    print(f"失败 {len(fails)} {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
