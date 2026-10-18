"""**真机验证 · 人机辩论 × 证据核验注入**（方案③ · #174–#176）。

问的问题：用户在反诈体验辩论里提交一张"现场照片"，系统开辩前本地核验出
**复制-移动线索**并把技术线索注入每一轮正方输入——这条链路在真模型上
真的成立吗？

场景设计（体验层的本色形态）：
  · 事件 = 库外新案「星湖湾商场踩踏」（与 e2e_combined 同一虚构事件族，
    刻意不撞真库）；
  · **用户是受骗方**：坚称传闻为真，还"拿到现场照片"（= 复制-移动样本图，
    `make_images(1)`，Δ=(312,0)——与 e2e_combined 同款样本）；
  · 正方立场被分配为**质疑方**（「该传闻缺乏依据」）——它拿到的正是系统
    核验出的线索，用来检验用户的"铁证"。

判据（全部只认本轮事实，软硬分开）：

  E1｜核验落台账：evidence 行 status=verified、suspicion 非空、sha256
      与样本字节哈希一致（rec 由 verify_evidence_file 现算，不写死）。
  E2｜**注入被采纳**：on_input 收集的每一轮正方输入都含
      ① 段标题（EVIDENCE_BRIEF_TITLE，固定文案）
      ② **位移值 312**（copy_move 的 detail 写「位移向量 Δ=(312, 0)」——
         本批样本独有参数，模型猜不出、提示词里没有 ⇒ 它在输入里
         只可能是核验结论真被渲染进去了）
      ③ caveat 句（护栏随线索同行）。
      ⚠️ 只断言「正方**看到了**」；正方是否在论点里**引用**线索是模型的
         自由（合法路径），只作观察打印，不进退出码——「判据跟着模型
         措辞走 = 不是判据」。
  E3｜辩论真跑完：outcome ∈ {max_rounds, concede, user_exit} 且
      round 行数 == 实际轮数、每轮有正方五字段（台账事实）。

用法（s1 仓根；需要 numpy/Pillow——本探针是真机探针，重依赖合法）：
    ./.venv/bin/python -m run._probe_debate_evidence
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv_lite import load_env

load_env(ROOT / ".env")

from legal_acps import human_debate as hd  # noqa: E402

#: 事件（库外新案，与 e2e_combined 的星湖湾同族但断言不依赖库内容）。
EVENT = "网传星湖湾商场周年庆促销发生踩踏，多人受伤，商场删监控压消息"

#: 正方被分配的立场：**质疑方**（体验层形态——用户的"铁证"被系统核验
#: 出拼接线索后，正方用线索检验它）。
PRO_STANCE = "该传闻缺乏可靠依据"

#: 用户两轮论点（受骗方口吻：手里有"现场照片"）。
USER_ARGS = [
    "我手里有当时现场的实拍照片，画面就是商场中庭，一看就是踩踏后的样子。"
    "有图有真相，这件事还有假？",
    "照片是亲戚在现场拍的转给我的，普通人拍的照片还能有假？"
    "你要是质疑照片，请拿出它被改过的证据来。",
]

#: 样本独有参数（make_images(1) 固定 Δ=(312,0)；探针现场读回，不写死断言）。
SENTINEL_DX = 312


async def main() -> int:
    # ── 造样本 + 本地核验（开辩前，与 CLI 主路径同一函数）──────────────
    from run.e2e_leader_image import make_images  # 惰性：numpy/Pillow 重依赖

    sample = make_images(1)[0]   # make_images 返回 [(字节, 位移), …]
    data, dx = sample
    assert dx == SENTINEL_DX, f"样本位移变了（{dx}）⇒ 哨兵跟着改，别硬凑"
    out_dir = ROOT / "output_probes"
    out_dir.mkdir(exist_ok=True)
    ev_path = out_dir / "debate-evidence-sample.jpg"
    ev_path.write_bytes(data)

    print("=" * 70)
    print("  真机验证 · 人机辩论 × 证据核验注入（方案③）")
    print("=" * 70)
    rec = hd.verify_evidence_file(ev_path)
    print(f"  证据：{ev_path.name} {len(data)} 字节  位移 Δ=({dx},0)")
    print(f"  核验：status={rec.get('status')}  等级={rec.get('suspicion')}  "
          f"模态={rec.get('modality')}/{rec.get('media_type')}")
    if rec.get("status") != "verified":
        print(f"  ❌ 核验都没过（{rec.get('reason')}）—— 后面无从谈起")
        return 1

    # ── 真跑一场 2 轮辩论（真模型 · 本地直跑正方 · 不登录）────────────
    sid = hd._now_stamp()
    ledger = hd.DebateLedger(sid)
    seen: list[tuple[int, str]] = []

    def on_input(round_no: int, text: str) -> None:
        seen.append((round_no, text))

    def on_round(round_no: int, pro: dict) -> None:
        print(f"\n── 第 {round_no} 轮 · 正方（立场 {pro.get('stance')} "
              f"信心 {pro.get('confidence')}）──")
        print(f"   {str(pro.get('key_point') or '')[:200]}")

    print(f"  会话 {sid}：2 轮，正方立场 = {PRO_STANCE}；正在跑……\n")
    from legal_acps.llm import build_model

    outcome = await hd.run_debate(
        EVENT, pro_stance=PRO_STANCE, max_rounds=2,
        user_source=lambda n: USER_ARGS[n - 1] if n <= len(USER_ARGS) else None,
        ledger=ledger, model=build_model(), sid=sid, mode="script", retries=1,
        evidence_records=[rec], on_input=on_input, on_round=on_round)

    lines = [json.loads(ln) for ln in
             ledger.path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # ── 判据 ────────────────────────────────────────────────────────────
    results: list[tuple[str, bool, str]] = []

    ev = next((l for l in lines if l["type"] == "evidence"), None)
    ev_files = (ev or {}).get("files") or []
    e1 = bool(ev_files) and ev_files[0].get("status") == "verified" \
        and bool(ev_files[0].get("suspicion")) \
        and str(ev_files[0].get("sha256") or "") == str(rec.get("sha256"))
    results.append(("E1 核验落台账", e1,
                    f"evidence 行 files={len(ev_files)} 等级="
                    f"{ev_files[0].get('suspicion') if ev_files else '无'}"
                    f" sha256 {'一致' if ev_files and ev_files[0].get('sha256') == rec.get('sha256') else '不一致/缺'}"))

    rounds_seen = [t for _, t in seen]
    e2 = bool(rounds_seen) and all(
        hd.EVIDENCE_BRIEF_TITLE in t and f"({SENTINEL_DX}, 0)" in t
        for t in rounds_seen)
    per = [f"第{n}轮: 标题{'✓' if hd.EVIDENCE_BRIEF_TITLE in t else '✗'}"
           f" 位移{'✓' if f'({SENTINEL_DX}, 0)' in t else '✗'}"
           for n, t in seen]
    results.append(("E2 注入被采纳（每轮输入）", e2,
                    "；".join(per) + f"（位移 ({SENTINEL_DX}, 0) 是本批样本独有参数）"))

    round_lines = [l for l in lines if l["type"] == "round"]
    e3 = outcome in ("max_rounds", "concede", "user_exit") \
        and len(round_lines) == len(rounds_seen) and len(round_lines) >= 1 \
        and all((l.get("pro") or {}).get("key_point") for l in round_lines)
    results.append(("E3 辩论真跑完", e3,
                    f"outcome={outcome} 轮记录 {len(round_lines)} 条、"
                    f"输入 {len(rounds_seen)} 轮"))

    print("\n" + "─" * 70 + "\n  判据\n" + "─" * 70)
    for name, ok_, detail in results:
        print(f"  [{'✅' if ok_ else '❌'}] {name}：{detail}")

    # 软观察（不进退出码）：正方论点有没有用上线索——模型的自由，不是判据
    clues_used = [l["round"] for l in round_lines
                  if str(l.get("pro", {}).get("key_point") or "")
                  .find(str(SENTINEL_DX)) >= 0
                  or "拼接" in str(l.get("pro", {}).get("key_point") or "")
                  or "复制" in str(l.get("pro", {}).get("key_point") or "")]
    print(f"\n  [观察·不进退出码] 正方论点引用线索的轮次：{clues_used or '无'}"
          "（引用是模型的自由；判据只认『正方看到了』）")
    print(f"  台账：{ledger.path}")

    return 0 if all(o for _, o, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
