"""元验证：离线抓取器的出网闸门**真的有牙**（#135 配套，2026-10-07）。

被测的判据都在 `legal_acps/check_local.py` 的 ⑦-5i：

  A. **闸门必须在发起请求之前拦住** —— 非 https / 白名单外 / 本机地址。
  B. **日期解析两种形态都要认** —— 相对路径与带前导斜杠。
  C. **原始发布方的引号要去干净** —— 后引号在**中间**，`strip()` 管不到。

为什么必须元验证（而不是只跑一次 check_local 看它绿）：

  🔴 出网面是本项目**从未有过、且刻意不要的东西**（SSRF 底线）。既然为
  「事实搜集」破例开了一个，就必须证明它**只会朝白名单开**。
  而安全判据最容易变成摆设：**功能照样跑得好好的，没有任何东西会变红** ——
  白名单里塞进 `localhost`，抓列表页照样返回 78 条；闸门删掉，只要网络
  不通也照样抛异常。⇒ 绿不绿说明不了任何事，必须证明「退回旧形态它会红」。

被修的缺陷（**三个都真实发生过**，都在 2026-10-07 当天）：

  1. **白名单闸门**是整个出网面唯一的边界（本模块 `ALLOWED_HOSTS`）。
  2. **日期正则只认带前导斜杠的形态** ⇒ `published` **整列为空**，
     而「没解析出来」与「本来就没有日期」在产物里**长得一模一样**。
  3. **正文写作**「来源： “网信中国”微信公众号」⇒ 后引号在**中间**，
     `strip()` 只管首尾 ⇒ 留下 `网信中国”微信公众号` 这种半吊子名字。

⚠️ 四条纪律（沿用本目录既有约定）：
  1. 退回后的文件**必须仍然语法正确**。
  2. 匹配要锚定 `[失败]` 前缀 —— 通过行的中文文案里本身就有"闸门""日期"这些字。
  3. 整篇读写一律 `read_bytes()` / `write_bytes()`（Windows 的 `write_text()` 会翻 CRLF）。
  4. `try/finally` 兜底复原 —— 失败路径最可能被中断，绝不能把旧形态留在工作区。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERIFIER = [sys.executable, "-m", "legal_acps.check_local"]

FETCH = ROOT / "tools" / "fetch_facts.py"

CASES = [
    {
        "name": "出网闸门不得失效（白名单外的请求不许发出去）",
        "why": "`ALLOWED_HOSTS` 是本项目唯一出网面的唯一边界。闸门一旦失效，"
               "请求就会真的发到任意主机 —— 而网络不通**也会抛异常**，"
               "「被拦了」与「网络挂了」长得一模一样。",
        # ⚠️ 锚点取 `_get()` 里那一行 `raise`，**不能**取 `if host not in
        #    ALLOWED_HOSTS:` —— 那句话在 `collect()` 里还有一份（缩进更深的
        #    那份会把浅的那份当子串 ⇒ 计成 2 处，锚点不唯一）。
        "file": FETCH,
        "new": '        raise ValueError(f"拒绝白名单外的主机：{host}（{url}）")',
        "old": "        pass  # 闸门被摘",
        "needle": "[失败] 出网闸门没拦住",
    },
    {
        "name": "日期解析必须两种路径形态都认（相对路径 + 带前导斜杠）",
        "why": "第一版正则只认 `/20260930/...`，而列表页里的链接是**相对路径**"
               " `20260930/xxx/c.html` ⇒ `published` **整列为空**，"
               "而「没解析出来」与「本来就没有日期」在产物里长得一模一样。",
        "file": FETCH,
        "new": 'm = re.search(r"(?:^|/)(20\\d{2})(\\d{2})(\\d{2})(?:/|$)", href)',
        "old": 'm = re.search(r"/(20\\d{2})(\\d{2})(\\d{2})(?:/|$)", href)',
        "needle": "[失败] 日期解析",
    },
    {
        "name": "原始发布方的引号必须去干净（后引号在中间，strip 管不到）",
        "why": "正文写作「来源： “网信中国”微信公众号」⇒ 后引号在**中间**。"
               "只 strip 首尾会留下 `网信中国”微信公众号`，"
               "而这个字段正是「这条结论有多可信」的依据。",
        # ⚠️ 锚点取整行（`origin = …`）。为什么不能只取字符类
        #    `[“”„‟'\"《》]`：那里头有个**反斜杠**，手写替换串极易少一层转义
        #    ⇒ 退回后语法错误，而语法一错，本次元验证就**证明不了任何事**。
        #    整行里唯一的反斜杠在 `\"`，一次性写对、写完立刻验 `count == 1`。
        "file": FETCH,
        "new": "origin = re.sub(r\"[“”„‟'\\\"《》]\", \"\", m.group(1)).strip(\" 、，,。\")",
        "old": "origin = m.group(1).strip(\" 、，,。\")",
        "needle": "[失败] 原始发布方没解析干净",
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
            print("[步骤9] ❌ 复原后摘要与基线不一致")
            return 1
        print(f"[步骤9] ✅ {len(CASES)} 条用例全部咬中，且源码已完整复原")
        return 0
    finally:
        for p, data in originals.items():
            Path(p).write_bytes(data)


if __name__ == "__main__":
    raise SystemExit(main())
