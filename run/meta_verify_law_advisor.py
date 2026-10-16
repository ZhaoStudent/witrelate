"""元验证：law-advisor（法律咨询顾问）的五处要害锚点，退回旧形态必须让
check_local 对应断言变红。

被测的判据（`legal_acps/check_local.py` 的 ⑦-8「法条库与法律顾问」节 +
嵌套节的「钉住下游」）与被修的缺陷形态：

  A. 检索前必须做同义扩展 —— `search()` 里那行 `expand_query(query)`。
     退回形态 = `expanded = query`（扩展还在函数里，但**没人调它**）：
     bigram 首尾不相交的召回缺口重新打开（实证：退回后借款咨询 top5 全空），
     正确条文进不了建议书，"引用真实法条"的价值主张空转。
  B. 校验闸门必须**只记不拦变拦** —— `submit_advice` 里「先判退回、后置 ok」。
     退回形态 = 两句换个顺序（先置 ok 再判退回）：invalid_citations 照记，
     status 却是 "ok" —— 模型凭记忆编的法条原样进建议书，闸门只剩台账作用。
  C. 阈值闸门（宁缺勿假）—— `MIN_SCORE = 22.0` 是实测分布定出的线。
     退回形态 = 0.0（实证：库外领域查询命中 5 条）：law-advisor 会给劳动
     纠纷硬配上不相干的法条 —— 返回一条不相关法条比不返回更糟。
  D. 能力边界单一来源 —— 库 meta 的 laws 清单。
     退回形态 = meta 行漏掉一部法律（专利法）：提示词还提着它、库却说没有
     —— 正是「重导库少了一部、提示词没跟上」这个真实漂移形态。⚠️ 这条
     改的是**数据文件**（law-articles.jsonl），语法检查按 JSONL 口径走。
  E. 主控必须按 name 钉住「法律咨询顾问」—— `LAW_ADVISOR_NAME` 插值进
     LEADER_INSTRUCTIONS。退回形态 = 不点名（"找到同类 Agent"）：
     discovery 的全网语义检索会捞到第三方同类 Agent，白烧主控的 8 步预算。

⚠️ 退回形态的 `old` 与 `new` 互不为子串（B 是同一三行块换序，天然满足）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERIFIER = [sys.executable, "-m", "legal_acps.check_local"]

CASES = [
    {
        "name": "检索前必须做同义扩展（不许跳过）",
        "why": "GramIndex 字符 bigram 首尾不相交——「借了」与「借款」零交集；"
               "不扩展则正确条文进不了 top-N，建议书只剩空话。",
        "file": ROOT / "legal_acps" / "law_store.py",
        "new": "        expanded = expand_query(query)",
        "old": "        expanded = query",
        "needle": "[失败] 法条检索：借款咨询没召回借款条文",
    },
    {
        "name": "校验闸门必须拦（不许只记不拦）",
        "why": "「先置 ok 再判退回」= invalid_citations 照记、status 却是 ok ——"
               "编造的法条原样进建议书，防幻觉第二道闸只剩台账作用。",
        "file": ROOT / "legal_acps" / "agents.py",
        "new": (
            '    if out["invalid_citations"]:\n'
            '        return out\n'
            '    out["status"] = "ok"'
        ),
        "old": (
            '    out["status"] = "ok"\n'
            '    if out["invalid_citations"]:\n'
            '        return out'
        ),
        "needle": "[失败] 引用校验：库外法条",
    },
    {
        "name": "阈值闸门 22.0 不许退化成 0",
        "why": "宁缺勿假：阈值空转则库外领域也返回法条（实证 5 条），"
               "law-advisor 会给劳动纠纷硬配不相干条文。",
        "file": ROOT / "legal_acps" / "law_store.py",
        "new": "MIN_SCORE = 22.0   # 实测分布见模块 docstring；改库必须重跑",
        "old": "MIN_SCORE = 0.0   # 实测分布见模块 docstring；改库必须重跑",
        "needle": "[失败] 法条检索：库外领域",
    },
    {
        "name": "库 meta 的法律清单不许丢（能力边界单一来源）",
        "why": "「重导库少一部、提示词没跟上」的真实漂移形态：提示词提着专利法、"
               "库却说没有 ⇒ 两份清单漂移，能力声明失真。",
        "file": ROOT / "legal_acps" / "laws" / "law-articles.jsonl",
        "new": '"laws": ["中华人民共和国专利法", "中华人民共和国个人所得税法"',
        "old": '"laws": ["中华人民共和国个人所得税法"',
        "needle": "[失败] 能力边界",
    },
    {
        "name": "主控必须按 name 钉住「法律咨询顾问」",
        "why": "不点名 ⇒ discovery 全网语义检索捞到第三方同类 Agent，"
               "白烧主控的 8 步预算（与网络裁判 / 证据鉴证 Leader 同一条纪律）。",
        "file": ROOT / "legal_acps" / "agents.py",
        # ⚠️ 2026-10-13 锚点随实现搬家：主控提示词措辞精简（删「名字由
        #    本仓库的常量插进来，你照抄即可」句），锚若不跟着走，残留核对
        #    会永远红（新形态×0）而负对照永远跑不了。
        "new": (
            "- 用 `acps_discover` 以「法律 初期 咨询 建议」为查询词找到 `name` 为\n"
            "  **「{LAW_ADVISOR_NAME}」**的 Agent（`name` 必须**逐字一致**；"
            "其余同名的第三方 Agent 一概不用），"
        ),
        "old": (
            "- 用 `acps_discover` 以「法律 初期 咨询 建议」为查询词找到同类 Agent\n"
            "  （其余同名的第三方 Agent 一概不用），"
        ),
        "needle": "[失败] leader（主控） 的 instructions 没有按 name 钉住自己人",
    },
]


def _b(value: object) -> bytes:
    return str(value).encode("utf-8")


def syntax_ok(path: Path) -> bool:
    """`.py` 走 py_compile；数据文件（.jsonl）按「每行可解析为 JSON」判。"""
    if path.suffix == ".py":
        return subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)], capture_output=True
        ).returncode == 0
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                json.loads(ln)
        return True
    except Exception:  # noqa: BLE001
        return False


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
