"""元验证：快车道分级裁决（⑦-10 + A 出路）的九处要害锚点，退回旧形态必须让
check_local 对应断言变红。

被测的判据（`legal_acps/check_local.py` ⑦-10「快车道分级」节）与被修的缺陷：

  A. 资格规则不许全开 —— `fast_lane_qualified` 退回 `return True`
     （快裁自我复制回路被打开）。
  B. lane 解析白名单不许退回透传 —— 不认识的 lane 值原样入库，会把
     「不认识 = 辩论道」的保守口径打穿。
  C. 先例层检索不许退回 BM25 阈值预筛 —— 小库 IDF 退化（同事件 5.08 分
     < 6.0）会让先例层静默空转；门槛必须是 same_event + 资格。
  D. 裁判 instructions 的快车道分支不许删 —— 删了模型每轮照旧派辩手，
     「命中即裁」的执行半边消失。
  E. issue_ruling 不许不打 lane 标 —— 落盘无 lane ⇒ 经验库无从区分
     快裁与辩论产出，资格规则失去依据。
  F. derive 不许本地再抄一份 _shares_sub —— 对齐筛子单一来源。
  G. 先例渲染不许丢「无对抗快裁」打标 —— 丢了「进池但降权」的降权
     就成了空话（模型看不见资格差异）。

⚠️ 退回形态的 `old` 与 `new` 互不为子串；退回后文件仍语法正确
（A 用恒真 return、B 用透传表达式、C 用删参调用、D 用字符串改写、
E 用整块摘除（打标块连同返回字段一起退回——只摘打标块会留下孤立
except，语法错误 ⇒ 元验证证明不了任何事）、F 用同名遮蔽定义、
G 用等位 pass 块、H/I 用等位注释行（instructions 是 f-string，
任意文本都语法合法）——都是合法 Python）。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERIFIER = [sys.executable, "-m", "legal_acps.check_local"]

CASES = [
    {
        "name": "资格规则不许全开（快裁不能自我复制）",
        "why": "fast+无标注的裁定一旦获得快裁触发资格，「快裁→入库→命中→"
               "又快裁」的自我确认回路就结构性存在，且每一跳都无人核验——"
               "经验库会被无对抗裁定单调灌满。",
        "file": ROOT / "legal_acps" / "ruling_store.py",
        "new": (
            "        return self.lane != \"fast\" or self.graded"
        ),
        "old": (
            "        return True  # 退回形态：资格全开（自我复制回路打开）"
        ),
        "needle": "自我复制",
    },
    {
        "name": "lane 解析必须白名单收紧（不认识的一律辩论道）",
        "why": "透传会让写错/不认识的 lane 值原样入库，资格规则里 "
               "「lane != fast」的判定被绕过——保守口径必须从解析口收死。",
        "file": ROOT / "legal_acps" / "ruling_store.py",
        "new": (
            "                lane=\"fast\" if str(obj.get(\"lane\") or \"\").strip() == \"fast\" else \"debate\","
        ),
        "old": (
            "                lane=str(obj.get(\"lane\") or \"debate\").strip() or \"debate\",  # 退回形态：透传"
        ),
        "needle": "lane 解析口径错",
    },
    {
        "name": "先例层检索不许退回 BM25 阈值预筛（门槛是 same_event）",
        "why": "单/小文档库的 BM25 IDF 退化（2026-10-12 实测：完全相同的事件"
               "文本只得 5.08 分 < MIN_SCORE 6.0）——用它预筛，先例层静默"
               "空转，「辩完入库供下次复用」白建。BM25 只是候选生成器，"
               "真正的门槛是 same_event（ratio≥FOLD_RATIO）+ 资格规则。",
        "file": ROOT / "legal_acps" / "fast_lane.py",
        "new": "        for h in store.search(event, limit=8, min_score=0.0):",
        "old": "        for h in store.search(event, limit=8):  # 退回形态：默认阈值预筛",
        "needle": "未触发快车道",
    },
    {
        "name": "裁判 instructions 的快车道分支不许删",
        "why": "判道在代码、执行在模型——提示词里没有「命中即裁」的分支，"
               "模型每轮照旧派辩手，快车道的全部提速都落空（装了 ≠ 会响）。",
        "file": ROOT / "legal_acps" / "agents.py",
        "new": "**跳过下面第 2–4 步，直接进入第 5 步裁定**",
        "old": "**照常走完每一步**（不区分车道）",
        "needle": "缺车道锚",
    },
    {
        "name": "issue_ruling 必须落盘打 lane 标（重算，不信自报）",
        "why": "落盘无 lane ⇒ 经验库无从区分快裁与辩论产出，资格规则的依据"
               "消失，防自我复制的整条设计塌掉一半。",
        "file": ROOT / "legal_acps" / "agents.py",
        # 整块替换（打标块 + B 出路 requeue 块 + 落盘 + 返回字典）：只退回
        # 打标块会留下孤立的 except（语法错误 ⇒ 元验证证明不了任何事），
        # 不退返回字典里的 "lane" 字段则返回处直接 KeyError——退回形态必须
        # 整体语法自洽。（2026-10-13 B 出路接线后，new 锚随实现搬家，old
        # 仍是「打标与 requeue 都不存在」的最早形态。）
        "new": (
            "    payload[\"lane\"] = \"debate\"\n"
            "    payload[\"lane_error\"] = \"\"\n"
            "    try:\n"
            "        from .fast_lane import decide_lane\n"
            "\n"
            "        lane_info = decide_lane(ruling.event)\n"
            "        payload[\"lane\"] = lane_info[\"lane\"]\n"
            "        payload[\"lane_error\"] = lane_info[\"lane_error\"]\n"
            "    except Exception as exc:  # noqa: BLE001 —— 打标失败不挡裁定落盘\n"
            "        payload[\"lane_error\"] = f\"relane: {type(exc).__name__}: {exc}\"\n"
            "    path.write_text(\n"
            "        __import__(\"json\").dumps(payload, ensure_ascii=False, indent=2), encoding=\"utf-8\"\n"
            "    )\n"
            "    # ── B 出路（2026-10-13）：「无法判定」⇒ 自动记复研判请求（旁路）────\n"
            "    # 库里证据不足不是终点——记一条 retry（事实库长肥后由每日研判重查，\n"
            "    # 见 legal_acps/lead_recon.py）。⚠️ 放在**落盘之后**：被重查的裁定书\n"
            "    # 必须真实存在。旁路纪律：记失败**不挡**裁定交付，如实进返回值\n"
            "    # （requeue.status ∈ queued/duplicate/rejected/error）。\n"
            "    requeue: dict = {\"status\": \"skipped\", \"reason\": \"verdict-decisive\"}\n"
            "    if payload[\"verdict\"] == RulingVerdict.UNDECIDABLE.value:\n"
            "        try:\n"
            "            from .lead_recon import queue_retry\n"
            "\n"
            "            requeue = queue_retry(ruling.event, path.name)\n"
            "        except Exception as exc:  # noqa: BLE001 —— 旁路失败不挡主流程\n"
            "            requeue = {\"status\": \"error\",\n"
            "                       \"error\": f\"{type(exc).__name__}: {exc}\"}\n"
            "    return {\n"
            "        \"saved_to\": str(path),\n"
            "        \"verdict\": payload[\"verdict\"],\n"
            "        \"confidence\": payload[\"confidence\"],\n"
            "        \"fallacies\": [f[\"kind\"] for f in (payload.get(\"fallacies\") or [])],\n"
            "        \"lane\": payload[\"lane\"],\n"
            "        \"requeue\": requeue,\n"
            "    }"
        ),
        "old": (
            "    path.write_text(\n"
            "        __import__(\"json\").dumps(payload, ensure_ascii=False, indent=2), encoding=\"utf-8\"\n"
            "    )\n"
            "    return {\n"
            "        \"saved_to\": str(path),\n"
            "        \"verdict\": payload[\"verdict\"],\n"
            "        \"confidence\": payload[\"confidence\"],\n"
            "        \"fallacies\": [f[\"kind\"] for f in (payload.get(\"fallacies\") or [])],\n"
            "    }"
        ),
        "needle": "缺 lane 标",
    },
    {
        "name": "单一来源：derive 不许本地再抄一份 _shares_sub",
        "why": "对齐筛子定义在 fast_lane（gt 派生与快车道触发共用同一把尺子）——"
               "本地再抄一份后一边收紧另一边不知道，漂移从此静默。",
        "file": ROOT / "tools" / "derive_gt_cases.py",
        "new": (
            "from legal_acps.fast_lane import (  # noqa: E402\n"
            "    ALIGN_MIN_LEN,\n"
            "    ALIGN_WINDOW,\n"
            "    shares_substring as _shares_sub,\n"
            ")"
        ),
        "old": (
            "from legal_acps.fast_lane import (  # noqa: E402\n"
            "    ALIGN_MIN_LEN,\n"
            "    ALIGN_WINDOW,\n"
            ")\n"
            "\n"
            "def _shares_sub(a: str, b: str) -> bool:  # 退回形态：本地再抄一份\n"
            "    return (a or \"\")[:ALIGN_MIN_LEN] in (b or \"\")"
        ),
        "needle": "对齐筛子分家",
    },
    {
        "name": "先例渲染不许丢「无对抗快裁」打标",
        "why": "「进池但降权」的降权必须让模型看得见——不标出资格差异，"
               "模型会把无对抗快裁当成与辩论道裁定同等的依据。",
        "file": ROOT / "legal_acps" / "ruling_store.py",
        "new": (
            "            if not c.fast_lane_qualified:\n"
            "                # 🔴 无资格先例（fast + 无标注）必须打标——不标，防自我确认\n"
            "                #    的降权就是一句空话；模型看不见身份差异就无从降权。\n"
            "                lines.append(\"   ⚠️ 此条为**无对抗快裁**（跳过辩论的快速裁定、\"\n"
            "                             \"未经人核）：参考价值低于经对抗检验的裁定，\"\n"
            "                             \"只作方向提示，不得作为采信依据。\")"
        ),
        "old": "            pass  # 退回形态：不打标",
        "needle": "先例渲染缺",
    },
    {
        "name": "A 出路：fast 命中「无法判定」先例的口径不许删（负缓存要说明）",
        "why": "「无法判定」的合格先例是「此前研判过、证据不足」的记录，不是可"
               "复用的结论——退回复读形态后，裁判会把它当新鲜口吻的弃权交差，"
               "委派方与用户既不知道查过、也不知道该补什么材料（A 出路的"
               "执行半边消失）。",
        "file": ROOT / "legal_acps" / "agents.py",
        "new": (
            "     ⚠️ 若命中的合格先例裁定为「无法判定」：那不是可复用的结论，是\n"
            "     「此前已研判、当时证据不足」的记录——你的裁定同样给「无法判定」，\n"
            "     但 `disputed_points` 须写明「本案此前已研判过、当时证据不足」，\n"
            "     并在 `reasoning` 里列出补齐证据缺口的方向（原始出处 / 截图 /\n"
            "     具体时间地点），供委派方转达用户；不得复读成新鲜口吻的弃权，\n"
            "     更不得借机自由发挥改判。"
        ),
        "old": "     # 退回形态：无法判定先例照常复读",
        "needle": "此前已研判过、当时证据不足",
    },
    {
        "name": "A 出路：主控「无法判定要材料」口径不许删",
        "why": "「查不了」不是终点——原始材料是「下次能查」的唯一通路。这句不"
               "在主控提示词里，模型把弃权当完事，用户永远不知道还能补材料、"
               "系统永远等不到重新研判的输入。",
        "file": ROOT / "legal_acps" / "agents.py",
        "new": (
            "- 裁定为「无法判定」时**主动向用户要材料**——这是把「查不了」变成「下次能查」\n"
            "  的唯一通路：最终回复里明确请用户提供**原始出处**（链接 / 截图 / 在哪看到 /\n"
            "  大概时间），并说明拿到新材料后会重新研判。不得把「无法判定」转述成\n"
            "  「没问题」，也不许替用户脑补一个来源。"
        ),
        "old": "  # 退回形态：无法判定就完事",
        "needle": "无法判定要材料",
    },
]


def _b(value: object) -> bytes:
    return str(value).encode("utf-8")


def syntax_ok(path: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)], capture_output=True
    ).returncode == 0


def run_verifier() -> str:
    r = subprocess.run(
        VERIFIER, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return (r.stdout or "") + (r.stderr or "")


def digests() -> dict[str, str]:
    return {str(c["file"]): hashlib.md5(c["file"].read_bytes()).hexdigest() for c in CASES}


def main() -> int:
    originals = {str(c["file"]): c["file"].read_bytes() for c in CASES}

    for c in CASES:
        raw = originals[str(c["file"])]
        if raw.count(_b(c["new"])) != 1 or _b(c["old"]) in raw:
            print(f"[步骤0] ❌ 前置不成立：{c['name']} 不是预期的'新形态'")
            return 1
    before = digests()
    print(f"[步骤0] ✅ {len(CASES)} 处均为新形态")

    try:
        for i, c in enumerate(CASES, 1):
            path: Path = c["file"]
            name, needle = str(c["name"]), str(c["needle"])
            original = originals[str(path)]

            broken = original.replace(_b(c["new"]), _b(c["old"]))
            if broken.count(_b(c["old"])) != 1 or _b(c["new"]) in broken:
                print(f"[{i}.1] ❌ {name}：替换未按预期发生")
                return 1
            path.write_bytes(broken)
            if not syntax_ok(path):
                print(f"[{i}.1] ❌ {name}：退回后文件语法错误 —— 本次元验证证明不了任何事")
                return 1
            print(f"[{i}.1] ✅ {name}：已退回旧形态（{path.name}），且文件仍语法正确")

            out = run_verifier()
            hit = needle in out
            print(f"[{i}.2] {'✅' if hit else '❌'} {name}：check_local 报出该失败 = {hit}")
            for line in out.splitlines():
                if needle in line:
                    print(f"          {line.strip()}")
            if not hit:
                print(f"[{i}.2] ⚠️ {name} 的断言没响 —— 它是空转的（或匹配词写错了）")
                return 1

            path.write_bytes(original)
            if not syntax_ok(path):
                print(f"[{i}.3] ❌ {name}：复原后语法错误")
                return 1
            print(f"[{i}.3] ✅ {name}：已复原")

        if digests() != before:
            print("[步骤4] ❌ 复原后 md5 与原文不一致")
            return 1
        out2 = run_verifier()
        green = ("[失败]" not in out2) and ("本地层面全部就绪" in out2)
        print(f"[步骤4] {'✅' if green else '❌'} 复原后整体转绿 = {green}")
        return 0 if green else 1
    finally:
        for c in CASES:
            path: Path = c["file"]
            if path.read_bytes() != originals[str(path)]:
                path.write_bytes(originals[str(path)])
                print(f"[finally] 已强制复原 {path.name}")


if __name__ == "__main__":
    raise SystemExit(main())
