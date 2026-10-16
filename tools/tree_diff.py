"""全树 md5 快照 / 比对 —— 元验证**唯一可靠**的判据。

用法（在 witrelate 目录下）：

    ./.venv/Scripts/python.exe -m tools.tree_diff save   # 跑元验证**之前**：记下全树指纹
    ./.venv/Scripts/python.exe -m tools.tree_diff diff   # 跑完之后：比对并给出 改动/新增/删除
    ./.venv/Scripts/python.exe -m tools.tree_diff diff \
        --expect-cases run.meta_verify_trace             # 顺带核对"改动是否都在本轮预期内"

`diff` 全树一致时 exit 0。退出码：

    0  全树与基线逐字节一致
    1  有差异（清单在下面）
    2  用法错误 / 没有基线 / 指定模块导不进来
    3  **有差异落在「本轮预期清单」之外** ⇒ 基线不可用于判本轮（见下）

⚠️ `1` 与 `2` 的区别是**本质的**：`1` = "跑出了结果，清单在下面"；`2` = "**我没有资格给结论**"。
   所以"用法 / 前提不成立"一律走 `2`，且信息打到 **stderr**。两者撞车的话，"你把模块名写错了"
   在退出码上与"树里确实有一点差异"**一模一样** ⇒ 调用方会拿着那份不该信的清单继续往下判。
   （2026-10-04 实测：`raise SystemExit("……")` 的退出码是 **1** 不是 2，于是这道闸门
   形同虚设；且"没有基线"那条**代码里给的是 1**，与本文件自己的承诺都对不上。已修。）

自身探针：`.venv/Scripts/python.exe -m run.probe_tree_diff`（exit 0 = 全绿，9 项）。
        ⚠️ 探针全绿只说明**判据会响**，不说明本轮真的没残留。

════════════════════════════════════════════════════════════════════════
为什么必须有它 —— "已复原 ✅" 只证明"在跑的时候还原过"
════════════════════════════════════════════════════════════════════════

`run/meta_verify_*.py` 会把源文件**退回旧形态**、跑断言、再还原。它的输出里有
一整套 `[n.3] ✅ 已复原`，看起来万无一失。但下面两种情形它一个字都不会报：

  · **中途被强杀**。Windows 上外部强杀（TerminateProcess）根本挂不上 `signal`
    处理器 ⇒ 还原守卫不执行、`finally` 也不执行，文件就留在"退回旧形态"的状态里。
    而那个文件**看起来完全正常、照样能跑**，只有下一次跑断言时才会以几条
    莫名其妙的失败暴露出来。
  · **期间的外部修改被静默覆盖**。元验证启动时把目标文件读进内存当快照，
    结束按**快照**回写 ⇒ 期间任何改动被覆盖掉，而输出仍全显示"已复原"。

所以：**跑完不 diff 等于没跑过。**

════════════════════════════════════════════════════════════════════════
为什么"基线过期"必须能被**判出来**（2026-10-04 补）
════════════════════════════════════════════════════════════════════════

2026-10-03 深夜踩过一次：基线是当天 15:07 记的，之后我又做了好几轮改动，
最后 `diff` 报出"改动 240 个文件（基线只有 199 个）"—— 那份清单里**混着别的轮次
的改动**，而它看起来只是"改动多了一点"。**判据本身变成了误导**。

⚠️ 试过但**不成立**的判据：拿文件 mtime 比基线时间。理由 —— 元验证每跑一个探针都会
   **无条件回写** CASES 文件（`[3] 复原` 那一步），所以一次**完全干净**的元验证跑完，
   这些文件的 mtime 也一定比基线新。于是"mtime 比基线新"会在**每一次干净运行**上响，
   而**一个永远在响的警报与没有警报等价**（本文件下面 `EXCLUDE_DIRS` 里那条同理）。
   更彻底地说：**mtime 只能证明"被写过"，证明不了"被谁写的、属于哪一轮"。**

✅ 成立且可靠的判据：**「本轮只该动哪些文件」是可以由调用方声明的。**
   元验证的 CASES 里就写着它要动哪些文件；把它传进来（`--expect-cases`），
   `diff` 就能给出一个**有意义的断言**：

       改动里出现任何**不属于本轮**的文件 ⇒ 那些改动**不可能由本轮产生**
       ⇒ 基线是更早留下的 ⇒ **本次 diff 不能用来判「本轮有没有残留」**。

   这正好也是"逐条核对每个改动是不是你自己有意改的"那条纪律的**可执行版本** ——
   原先它只能靠人眼，而人眼恰好是 10-03 那次失守的地方。

⚠️ 另一条同样重要的用法：`diff` 的结果是"哪些文件变了"的**清单**，不是"有没有问题"。
   跑完以后应当逐条核对每个改动是不是**你自己有意改的**。

⚠️ 排除项：`.venv` / `.wit`（运行期数据，每次跑都会变）/ `__pycache__` / `.git` /
   `logs` / `output`（产出物）/ `node_modules` / `.pytest_cache` / `.mypy_cache` /
   `.ruff_cache`；以及**基线文件自身**（见 `walk()` 里的注释）。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent

#: 基线文件。点开头 = 明确的生成物，不参与人工维护。
#: 可用环境变量 `TREE_SNAPSHOT` 覆盖 —— 让本工具的**自身探针**能在临时文件上跑，
#: 不去动那份真基线（真基线被弄脏的话，"基线过期"这件事就又要靠人眼发现了）。
#:
#: ⚠️ 覆盖会带来一个**计数上的错位**，看到时别当 bug：`walk()` 排除的是"当前这份
#:    SNAP"；把 SNAP 指到临时目录以后，**真基线 `run/.tree_snapshot.json` 就不再被
#:    排除**，于是它会被当成一个普通文件算进来。实测：真跑 241 个文件，探针跑 242/243。
#:    自洽性不受影响（同一份 SNAP 存、同一份 SNAP 比），只是**跨两种跑法的绝对数不可直接比**。
SNAP = Path(os.environ["TREE_SNAPSHOT"]).resolve() if os.environ.get("TREE_SNAPSHOT") \
    else ROOT / "run" / ".tree_snapshot.json"

#: 快照格式版本。1 = 裸 `{路径: md5}`（无时间戳，2026-10-04 之前的形态）；
#: 2 = `{"_format": 2, "_saved_at": …, "files": {…}}`。
FORMAT = 2

EXCLUDE_DIRS = {
    ".venv", ".wit", "__pycache__", ".git", "logs", "output",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    # ⚠️ 2026-10-02 补：`.workbuddy`（automation-out / memory / tmp）会被**自动化
    #    在任意时刻**写入 —— 与本次元验证毫无关系。不排除它，每次 `diff` 都会
    #    报一条"新增"，而**一个永远在响的警报与没有警报等价**：真出现残留时
    #    它会被淹没在那条假警报里。（同 SNAP 自身被排除的理由。）
    #    没有任何元验证会改 `.workbuddy/**`，所以排除不会掩盖真残留。
    ".workbuddy",
}


def walk() -> dict[str, str]:
    """全树 `相对路径 → md5`。**只读，不改任何文件。**"""
    out: dict[str, str] = {}
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        if p == SNAP:
            # ⚠️ 基线文件**必须排除自己**。写基线时 `walk()` 先算、后写盘，
            #    于是记录下来的永远是上一版哈希 ⇒ 每次 `diff` 都恒报"改动 1 个"。
            #    一个永远在响的警报与没有警报等价 —— 真出现残留时它会被淹没在
            #    那条假警报里。（这个坑本工具的第一版就踩了。）
            continue
        rel = p.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        out[rel.as_posix()] = hashlib.md5(p.read_bytes()).hexdigest()
    return out


def load_snapshot() -> tuple[dict[str, str], str | None, str]:
    """读基线。返回 `(文件表, 记录时间或 None, 时间来源说明)`。

    ⚠️ **两种格式都要吃**：老基线是裸 `{路径: md5}`，没有时间戳。遇到老格式不许
       直接判失败 —— 否则升级本工具就等于"逼人重记一次基线"，而重记基线这件事
       **恰好是 10-03 那次被忘掉的步骤**。
    """
    raw = json.loads(SNAP.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("files"), dict):
        saved = raw.get("_saved_at")
        return ({str(k): str(v) for k, v in raw["files"].items()},
                str(saved) if saved else None,
                "快照里的 _saved_at")
    # 老格式：裸 {路径: md5}。时间只能拿文件 mtime 顶一下，且**必须标明是顶的**。
    mtime = datetime.fromtimestamp(SNAP.stat().st_mtime).astimezone()
    return ({str(k): str(v) for k, v in raw.items()},
            mtime.isoformat(timespec="seconds"),
            "基线文件的 mtime（老格式没有 _saved_at，只能顶一下）")


def age_of(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return "？"
    if t.tzinfo is None:
        t = t.astimezone()
    secs = (datetime.now(t.tzinfo) - t).total_seconds()
    if secs < 0:
        return "未来（时钟不同步？）"
    h, m = divmod(int(secs) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def bail(msg: str, code: int = 2) -> NoReturn:
    """用法 / 前提不成立 ⇒ 打信息到 stderr，并以**明确退出码**收场。

    ⚠️ 千万不要用 `raise SystemExit("……")`（字符串参数）表达错误：那样退出码是 **1**，
       而 `1` 在本工具里**已经有含义**——"跑出了一个结果，清单在下面"。两者撞车以后，
       "你把模块名写错了"和"树里确实有一点差异"在退出码上**一模一样**，
       于是**闸门失效得不声不响**：调用方会拿着那份不该信任的清单继续往下判。
       这正是 10-03 那次事故的形态（判据本身变成了误导），所以此处专门绕开它。
    """
    sys.stderr.write(msg.rstrip("\n") + "\n")
    raise SystemExit(code)


def expected_from_cases(modules: list[str]) -> tuple[set[str], list[str]]:
    """把 `run.meta_verify_*` 的 CASES 里 `file` 字段收集成**仓库相对路径**集合。"""
    expected: set[str] = set()
    notes: list[str] = []
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for mod in modules:
        try:
            m = importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001
            bail(
                f"[diff] ❌ 导不进 `{mod}`（{type(exc).__name__}: {exc}）"
                f" ⇒ 预期清单无从声明，拒绝给出结论"
            )
        cases = getattr(m, "CASES", None)
        if not cases:
            bail(f"[diff] ❌ `{mod}` 里没有 CASES ⇒ 它不是元验证模块？")
        for c in cases:
            p = Path(str(c["file"]))
            try:
                rel = p.relative_to(ROOT).as_posix()
            except ValueError:
                rel = p.as_posix()
            expected.add(rel)
        notes.append(f"{mod}（{len(cases)} 条用例）")
    return expected, notes


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode = args[0] if args else "diff"
    if mode not in ("save", "diff"):
        bail(f"用法：python -m tools.tree_diff [save|diff]（收到 {mode!r}）")

    expects: list[str] = []
    if mode == "diff" and "--expect-cases" in args:
        i = args.index("--expect-cases")
        expects = [a for a in args[i + 1:] if not a.startswith("--")]
        if not expects:
            bail("[diff] --expect-cases 后面要跟至少一个模块名")

    # ⚠️ 预期清单**先解析、再判树**。反过来的话（先判"有没有差异"，没差异就直接
    #    返回 0）会让**写错的模块名一声不吭地通过** —— 你当时以为那道闸门装上了，
    #    其实它压根没被求值；等哪天树脏了它才突然 exit 2，前后行为还不一致。
    #    声明的闸门必须**无条件求值**，与"这次有没有差异"无关。
    expected: set[str] = set()
    expect_notes: list[str] = []
    if expects:
        expected, expect_notes = expected_from_cases(expects)

    now = walk()
    if mode == "save":
        SNAP.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_format": FORMAT,
            "_saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "files": now,
        }
        # ⚠️ 必须显式 `newline="\n"`：Windows 上 `write_text` 会把 `\n` 翻成 `\r\n`。
        #    这份基线**不在同步集里**（`PATTERNS` 收不到 `run/.tree_snapshot.json`），
        #    所以 CRLF 不影响任何双向核对；但本项目的字节纪律是"源码与生成物一律纯 LF"，
        #    留着它只会让下一次"全树 CRLF 粗查"多出一条需要人工解释的噪声。
        with open(SNAP, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(payload, indent=0, sort_keys=True))
        print(f"[save] 已记录 {len(now)} 个文件 → {SNAP.relative_to(ROOT) if SNAP.is_relative_to(ROOT) else SNAP}")
        print(f"[save] 基线时刻 {payload['_saved_at']}（格式 v{FORMAT}）")
        return 0

    if not SNAP.is_file():
        # ⚠️ 这里原先是 `return 1` —— 与本文件顶部承诺的"2 = 没有基线"**对不上**。
        #    文档说有这道闸门、代码里没有，比两道都没有更危险：人照着文档去写调用方，
        #    于是把"还没记基线"当成"有差异"处理。本文件自己的承诺必须由自己兑现。
        bail(f"[diff] ❌ 没有基线（{SNAP}）—— 先跑 `save`")
    old, saved_at, how = load_snapshot()

    changed = sorted(k for k in old if k in now and now[k] != old[k])
    added = sorted(k for k in now if k not in old)
    removed = sorted(k for k in old if k not in now)

    print(f"[diff] 比对 {len(now)} 个文件（基线 {len(old)}）")
    print(f"[diff] 基线时间：{saved_at or '未知'}（距今 {age_of(saved_at) if saved_at else '？'}）"
          f"　来源：{how}")
    print(f"[diff] 改动 = {changed}")
    print(f"[diff] 新增 = {added}")
    print(f"[diff] 删除 = {removed}")

    touched = changed + added + removed
    if not touched:
        print("[diff] ✅ 全树与基线逐字节一致 ⇒ 元验证零残留")
        return 0

    if expects:
        outside = [p for p in touched if p not in expected]
        print(f"[diff] 本轮预期改动清单（{len(expected)} 个，来自 {'、'.join(expect_notes)}）：")
        for p in sorted(expected):
            print(f"          {p}")
        if outside:
            print()
            print(f"[diff] ❌ 有 {len(outside)} 处改动**不在本轮预期清单内**：")
            for p in outside[:20]:
                print(f"          {p}")
            if len(outside) > 20:
                print(f"          …… 还有 {len(outside) - 20} 处")
            print("[diff] ⇒ 这些改动**不可能由本轮产生** ⇒ 基线是更早留下的、跨了别的轮次，")
            print("[diff] ⇒ **本次 diff 不能用来判「本轮有没有残留」**。")
            print("[diff]    先 `python -m tools.tree_diff save` 记一份新基线，再跑到本轮结束来比。")
            return 3
        print("[diff] ✅ 改动全部落在本轮预期清单内（仍须逐条核对是否有**残留**）")
        return 1

    print("[diff] ⚠️ 逐条核对：每一处都必须是**你自己有意改的** —— 不是「看着像」就算。")
    print("[diff] ⚠️ 若这次改动**多得不合理**，先怀疑基线过期（基线时间见上），而不是逐条看下去。")
    print("[diff]    传 `--expect-cases run.meta_verify_xxx` 可以让本工具替你判这件事。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
