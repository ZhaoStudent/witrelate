"""打印"要同步到服务器的那些文件"的 md5 指纹，用于**双向核对**。

为什么单独做成一个工具，而不是在同步命令里塞一段临时 python：

  * 同步清单（哪些文件算"该项目要发布的代码"）**只能有一处定义**，
    否则"本地算了 73 个、服务器算了 70 个"这种事会反复发生，
    而它看起来只是"数量对不上"，实质是**有文件根本没同步**。
  * 双向核对要求两边用**同一段代码**去枚举同一批文件 —— 同一段代码就必然同源。
  * Git Bash 自带的 `md5sum` 会给二进制模式的路径加 `*` 前缀，两边格式还对不齐。

用法：
    本地：.venv/Scripts/python.exe -m tools.file_fingerprint > /tmp/local.md5
    服务器：.venv/bin/python -m tools.file_fingerprint > /tmp/remote.md5
    比对：python -m tools.file_fingerprint --diff /tmp/local.md5 /tmp/remote.md5

输出格式固定为 `md5<TAB>相对路径`（LF 结尾、按路径排序），便于直接 diff。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

#: 不参与同步的目录（运行时状态 / 虚拟环境 / 缓存）。
SKIP_DIRS = frozenset({
    ".venv", ".git", "__pycache__", ".wit", ".wit-local", ".wit-t",
    "logs", "output", ".vscode", ".workbuddy", "test",
})

#: (基准目录, glob)。**这是同步范围唯一的定义处。**
PATTERNS: tuple[tuple[str, str], ...] = (
    ("legal_acps", "**/*.py"),
    # ⚠️ 非 .py 的**资产**同样要收 —— 2026-10-01 实测发现的漏项：
    #    旧清单只有 `legal_acps/**/*.py`，而体验应用的前端是
    #    `legal_acps/experience/web/index.html`，于是 81 个文件的清单里
    #    **一个 html 都没有**。本地跑得好好的（文件就在磁盘上），
    #    同步到服务器后 `GET /` 会因为找不到 index.html 直接失败 ——
    #    "本地能跑"恰恰是这件事最容易被当成"没问题"的原因。
    #    **加资产目录时，必须一并加到这里**；`tools/verify_experience.py`
    #    里有一条断言专门守着它。
    ("legal_acps", "**/*.html"),
    # ⚠️ 法条库数据（2026-10-07，#law-advisor）：`legal_acps/laws/law-articles.jsonl`
    #    是 law-advisor 的知识本体 —— 它不在清单里，服务器上就是一个**空库**：
    #    本地 check_local 全绿（文件就在磁盘上）、lookup 全空、建议书只剩
    #    「能力不足」。这是同一类漏项的第 5 处（前四见下）。**按目录收**。
    ("legal_acps", "laws/**/*"),
    # ⚠️ 第 6 处（2026-10-07 深夜实测发现）：`legal_acps/rulings/ground_truth.jsonl`
    #    （基准标注）同样不在任何清单里 —— 它在服务器上被人工重标（校准交接的
    #    固定步骤），而同步清单不收它 ⇒ **本地永远是旧版**，下次从本地推送
    #    就会把服务器上的新标注**静默覆盖回去**。与 laws 同病同修：按目录收。
    ("legal_acps", "rulings/**/*"),
    ("tools", "*.py"),
    ("run", "*.py"),
    # ⚠️ 非 .py 的**脚本**同样要收 —— 与上面 html 那条是同一个坑的第二次：
    #    2026-10-01 加了 `run/verify_frontend_flow.mjs`（用 node 真跑前端脚本的
    #    行为验证），而 `("run", "*.py")` 收不到 .mjs，于是"新加的文件**又**不在
    #    清单里"。判据不是"它现在跑不跑得到"，而是"清单是不是全部源码" ——
    #    只要按扩展名白名单收，**下次换个扩展名就还会漏**。
    #    改这条 PATTERNS 时请连同 verify_experience 里那条覆盖断言一起想。
    ("run", "*.mjs"),
    ("scripts", "*.sh"),
    # ⚠️ **按目录收，不再按扩展名白名单** —— 前两次都是"换个扩展名就再漏一次"：
    #    第 3 次（2026-10-05，#27）：`("deploy", "**/*.service")` 收不到
    #    `deploy/systemd/acps-case-round.timer`（**仓库里第一个 .timer**），
    #    而它正是服务器上排期用的那个单元 —— 不推 ⇒ `case-install` 装不了定时器。
    #    同一次还发现 `deploy/systemd/acps.logrotate` 也一直不在清单里，
    #    而 `scripts/systemd-install.sh` 第 163 行要 `install "$UNIT_SRC/acps.logrotate"`
    #    ⇒ 那句在服务器上**从来就找不到源文件**。
    #    ⇒ 收 `deploy/**/*`：以后加任何扩展名都不会再漏。
    #    判据侧同步改成**现场派生**（`tools/verify_experience.py`：磁盘上 deploy/ 下
    #    有哪个文件，清单里就必须有哪个），而不是"再补一条白名单"。
    ("deploy", "**/*"),
    # `docs/` 顶层的材料同样要收（`docs/aicomp/*.md` 一直在收，顶层却不在 ——
    # 同一类漏项的第 4 处）。关键证据：`acps-case-round.service` 的注释里**点名**
    # 了 `/home/ubuntu/witrelate/docs/上线操作手册.md`，而它恰恰不在任何清单里，
    # 服务器上那份一直是靠**另行手工推**同步的（实测：两边 md5 一致，但那不是判据）。
    # ⚠️ 只收 `*.md`、**不递归**：`docs/acps-specs/` 是第三方规格拷贝，服务器上
    #    本来就没有，刻意不推。
    ("docs", "*.md"),
    ("docs/aicomp", "*.md"),
    ("acs", "**/*.py"),
)

#: 零散的单文件。
EXTRA_FILES: tuple[str, ...] = ("dotenv_lite.py", "release-manifest.json")


def collect(root: Path) -> list[Path]:
    """按 PATTERNS + EXTRA_FILES 枚举，返回相对 `root` 的路径（已排序、去重）。"""
    found: set[str] = set()
    for base, pat in PATTERNS:
        for p in root.glob(f"{base}/{pat}"):
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            found.add(rel.as_posix())
    for name in EXTRA_FILES:
        if (root / name).is_file():
            found.add(name)
    return [Path(s) for s in sorted(found)]


def fingerprint(root: Path) -> str:
    lines = []
    for rel in collect(root):
        digest = hashlib.md5((root / rel).read_bytes()).hexdigest()
        lines.append(f"{digest}\t{rel.as_posix()}")
    return "\n".join(lines) + ("\n" if lines else "")


def _parse(text: str, label: str) -> dict[str, str]:
    """解析指纹文件。**格式不对就报错退出，不要"尽力而为"**。

    ⚠️ 这里踩过一次：远端把日志行（`backup: xxx.tgz`）和指纹混在同一个流里，
    旧实现把它当成一条指纹，键是**空字符串** —— 于是 diff 只报
    「仅服务器有 1 个（= 服务器上多出来的）：+ 」（后面什么都没有），
    看起来像"有一处说不清的差异"，实际是**解析器在吃垃圾**。
    这类"悄悄把垃圾当成数据"的行为，比直接报错危险得多：它会让人去查一个不存在的问题。
    """
    out: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        digest, sep, path = line.partition("\t")
        if not sep or len(digest) != 32 or not path.strip():
            raise SystemExit(
                f"{label} 第 {lineno} 行不是 `md5<TAB>路径` 格式，拒绝继续：\n  {line!r}\n"
                "（常见原因：把命令的日志输出和指纹混进了同一个流/同一个重定向）"
            )
        out[path.strip()] = digest.strip()
    if not out:
        raise SystemExit(f"{label} 是空的 —— 指纹文件没生成成功。")
    return out


def diff(left_path: str, right_path: str) -> int:
    """比较两份指纹。**只有在两边文件集合也一致时才判定通过** ——

    只比"相同路径的哈希是否相等"会漏掉最危险的那种情况：
    某个文件只在一边存在（= 根本没同步过去，或者服务器上有本地没有的旧文件）。
    """
    left = _parse(Path(left_path).read_text(encoding="utf-8"), left_path)
    right = _parse(Path(right_path).read_text(encoding="utf-8"), right_path)

    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    mismatched = sorted(p for p in set(left) & set(right) if left[p] != right[p])

    if only_left:
        print(f"[仅本地有] {len(only_left)} 个（= 没同步过去）：")
        for p in only_left:
            print(f"  - {p}")
    if only_right:
        print(f"[仅服务器有] {len(only_right)} 个（= 服务器上多出来的）：")
        for p in only_right:
            print(f"  + {p}")
    if mismatched:
        print(f"[内容不一致] {len(mismatched)} 个：")
        for p in mismatched:
            print(f"  ! {p}\n      本地 {left[p]}\n      服务器 {right[p]}")

    ok = not (only_left or only_right or mismatched)
    total = len(left)
    print(f"共 {total} 个文件：一致 {len(set(left) & set(right)) - len(mismatched)}"
          f" / 不一致 {len(mismatched)} / 单边 {len(only_left) + len(only_right)}")
    print("结论：" + ("✅ 两侧完全一致" if ok else "❌ 存在差异，不要继续下一步"))
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    # ⚠️ 必须把 stdout 钉成 LF。Windows 上 Python 的文本模式 stdout 会把 `\n`
    # 翻成 `\r\n`，于是重定向出来的清单/指纹**每一行都带一个隐藏的 `\r`**：
    # 肉眼看着完全正常，`tar -T list.txt` 却会逐行报
    # `Cannot stat: No such file or directory`（文件名里真的有 `\r`）。
    # 这正是本项目反复踩的那个坑 —— **"能读"不等于"字节对"**。
    try:
        sys.stdout.reconfigure(newline="\n")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # 老版本 / 已重定向的非常规 stdout
        pass

    if argv[:1] == ["--list"]:
        for rel in collect(Path(".")):
            print(rel.as_posix())
        return 0
    if argv[:1] == ["--diff"]:
        if len(argv) != 3:
            print("用法：-m tools.file_fingerprint --diff <本地.md5> <服务器.md5>")
            return 2
        return diff(argv[1], argv[2])
    sys.stdout.write(fingerprint(Path(".")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
