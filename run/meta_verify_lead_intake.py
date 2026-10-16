"""元验证：lead-intake（线索受理官）的五处要害锚点，退回旧形态必须让
check_local 对应断言变红。

被测的判据（`legal_acps/check_local.py` 的 ⑦-9「线索受理官」节 + 嵌套节
「钉住下游」）与被修的缺陷形态：

  A. 防污染闸门·定性词 —— claim 自带官方定性（谣言/不实/辟谣系）必须拦。
     退回形态 = 闸门只记不拦（`pass`）：把答案的后半段喂给下游裁判，
     基准污染在线索侧复发。
  B. 防污染闸门·处罚词 —— claim 自带处罚结果必须拦（处罚是核查下游产物）。
     退回形态同 A。
  C. 判重不许空转 —— 同一传闻重复报送必须返回 duplicate 且队列不涨。
     退回形态 = `if False:`（判重分支死掉）：队列被同一事件灌满，
     每日研判的「待处理」口径失真。
  D. 单一来源 —— derive_gt_cases 的 FALSE_PAT 必须**就是** lead_store 的
     那个对象（import，不是抄一份）。退回形态 = 本地再定义一份：
     两把尺子分家后，一边收紧另一边不知道，漂移从此静默。
  E. 主控必须按 name 钉住「线索受理官」—— LEAD_INTAKE_NAME 插值进
     LEADER_INSTRUCTIONS。退回形态 = 不点名（"同类 Agent"）：
     discovery 全网语义检索捞到第三方同类 Agent，白烧主控预算。
  F. 类别清单必须**进提示词** —— `_LEAD_CATEGORY_TEXT` 插值进
     LEAD_INTAKE_INSTRUCTIONS。退回形态 = 清单置空（模型第一轮盲猜）：
     真机 10-11 实证，盲猜被拒后的重交轮撞上 DSML 工具调用文本化，
     队列零落盘。清单仍单一来源（CASES → categories()），插值是消费方。

⚠️ 退回形态的 `old` 与 `new` 互不为子串；退回后文件仍语法正确（A/B 用
带注释的 pass 块，C 用 `if False:`，D 用本地再定义，F 用置空赋值——
都是合法 Python）。
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
        "name": "防污染闸门·定性词必须拦（不许只记不拦）",
        "why": "claim 自带官方定性 = 把答案喂给下游裁判——基准污染在线索侧复发；"
               "闸门只剩台账作用时，线索队列会系统性夹带结论。",
        "file": ROOT / "legal_acps" / "lead_store.py",
        "new": (
            "    if FALSE_PAT.search(claim):\n"
            "        reasons.append(\n"
            "            \"claim 里含官方定性词（如「谣言/不实/辟谣」系表述）：\"\n"
            "            \"定性是核查链路的结论，线索输入不得自带——请只转述传闻本身\"\n"
            "        )"
        ),
        "old": (
            "    if FALSE_PAT.search(claim):  # 退回形态：闸门只记不拦\n"
            "        pass"
        ),
        "needle": "[失败] 闸门「自带定性」",
    },
    {
        "name": "防污染闸门·处罚词必须拦（不许只记不拦）",
        "why": "处罚结果是核查链路的下游产物；出现在线索里同样是泄答案——"
               "下游会顺着处罚倒推定性，对抗性检验塌掉。",
        "file": ROOT / "legal_acps" / "lead_store.py",
        "new": (
            "    if PUNISH_PAT.search(claim):\n"
            "        reasons.append(\n"
            "            \"claim 里含处罚结果（拘留/罚款/判刑等）：处罚是核查下游的产物，\"\n"
            "            \"线索里不该有——请删去后重新提交\"\n"
            "        )"
        ),
        "old": (
            "    if PUNISH_PAT.search(claim):  # 退回形态：闸门只记不拦\n"
            "        pass"
        ),
        "needle": "[失败] 闸门「自带处罚」",
    },
    {
        "name": "判重闸门不许空转（重复报送必须报 duplicate）",
        "why": "判重分支死掉 ⇒ 同一传闻灌满队列，「待处理」口径失真，"
               "每日研判被同一事件反复占用——覆盖面塌成单一事件。",
        "file": ROOT / "legal_acps" / "lead_store.py",
        "new": "    if lid in existing:",
        "old": "    if False:",
        "needle": "[失败] 判重：同一传闻重复报送",
    },
    {
        "name": "单一来源：derive 不许本地再抄一份 FALSE_PAT",
        "why": "两把「不许把答案喂给裁判」的尺子必须**是同一个对象**——"
               "抄一份后一边收紧另一边不知道，从此静默漂移（值相等不算数："
               "一致性断言挡不住抄得一字不差）。",
        "file": ROOT / "tools" / "derive_gt_cases.py",
        "new": "from legal_acps.lead_store import FALSE_PAT, PUNISH_PAT  # noqa: E402",
        "old": (
            "import re as _re  # 退回形态：本地再抄一份\n"
            "FALSE_PAT = _re.compile(r'不属实|情况不实')\n"
            "PUNISH_PAT = _re.compile(r'拘留|罚款')"
        ),
        "needle": "[失败] 单一来源：derive_gt_cases.FALSE_PAT 不是",
    },
    {
        "name": "主控必须按 name 钉住「线索受理官」",
        "why": "不点名 ⇒ discovery 全网语义检索捞到第三方同类 Agent，"
               "白烧主控的 8 步预算（与网络裁判 / 证据鉴证 Leader / "
               "法律咨询顾问同一条纪律）。",
        "file": ROOT / "legal_acps" / "agents.py",
        # ⚠️ 2026-10-13 锚点随实现搬家：主控提示词措辞精简（删「名字由
        #    本仓库的常量插进来，你照抄即可」句），锚若不跟着走，残留核对
        #    会永远红（新形态×0）而负对照永远跑不了。
        "new": (
            "- 用 `acps_discover` 以「线索 受理 报送 传闻」为查询词找到 `name` 为\n"
            "  **「{LEAD_INTAKE_NAME}」**的 Agent（`name` 必须**逐字一致**；"
            "其余同名的第三方 Agent 一概不用），"
        ),
        "old": (
            "- 用 `acps_discover` 以「线索 受理 报送 传闻」为查询词找到同类 Agent\n"
            "  （其余同名的第三方 Agent 一概不用），"
        ),
        "needle": "[失败] leader（主控） 的 instructions 没有按 name 钉住自己人",
    },
    {
        "name": "类别清单必须进提示词（不许让模型第一轮盲猜）",
        "why": "清单只在工具返回里 ⇒ 模型第一轮盲猜类别（真机实测猜「教育」被拒），"
               "重交那一轮撞上 DSML 工具调用文本化（调用语法进正文、从未执行）"
               "⇒ 队列零落盘——线索受理链整链白跑。插值是 categories() 的"
               "消费方，单一来源仍是 CASES。",
        "file": ROOT / "legal_acps" / "agents.py",
        "new": (
            '_LEAD_CATEGORY_TEXT = "、".join(f"「{c}」" for c in _lead_categories())'
        ),
        "old": (
            '_LEAD_CATEGORY_TEXT = ""  # 退回形态：清单不进提示词（模型第一轮盲猜）'
        ),
        "needle": "[失败] 受理官 instructions 缺类别",
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
