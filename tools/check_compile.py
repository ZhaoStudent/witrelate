"""全仓 Python 语法闸门：任何一个 ``.py`` 编译不过就是硬失败。

为什么值得单独一个脚本（2026-10-04 立）——三条理由，缺一条都可以不写它：

  · **这个坑咬过 4 次**。最近一次是 ``legal_acps/check_local.py`` 第 2941 行：
    Python 字符串里嵌了 ASCII 双引号（``它是"还在跑"还是"早就没了"``），
    整份自检**当场跑不起来**。而"跑不起来"的症状是**没有输出** ——
    与"跑完了、全绿"在习惯上很容易被当成同一件事（人只会看到自己期望的那份安静）。
  · **它不可能被它守的文件弄坏**。这一点别的判据都没有：``check_local`` 自己坏了就是
    跑不起来，它的失败形态是"少了一段输出"，不是"一条红"。而本脚本守的是**全仓**，
    坏的那个文件不可能是它自己（真要是它自己，那也是"它没启动"，一眼可见）。
  · **grep 顶不上**。全仓扫"字符串里含 ``"``"命中 **462 处 / 88 文件**，
    几乎全是文档字符串里合法的引用（``{"type":"AipError"}`` 这种），信号被噪声淹没。
    而"能不能编译"是**编译器**给的判定，不是模式匹配。

清单**现场派生**（``rglob``），不手工维护 —— 加文件、加目录都不用改这里
（本项目纪律：「同一事实散在多处 ⇒ 加角色必漏」）。

⚠️ 用内置 ``compile()`` 而**不是** ``py_compile.compile()``：
   · ``compile()`` 就是同一个编译器，但**什么都不写盘**。``py_compile`` 默认会在源文件
     旁边建 ``__pycache__`` ⇒ 一条"只读"的扫描会把工作区改掉，而全树 md5 diff 会把它
     报成意外改动（本项目真踩过：一次"只读"核对把 244 个文件变成 245 个）。
   · ``compile()`` 抛的 ``SyntaxError`` 自带 ``lineno`` / ``msg``；``py_compile`` 把异常
     包进 ``PyCompileError`` 之后，行号只能从 ``exc_value`` 里再掏一次（实测容易漏，
     漏了的表现是"提示退化成一句套话"）。
   · 传的是 **bytes**，不是 ``read_text()`` 的结果：``compile(bytes)`` 会遵守 PEP 263
     的编码声明，也不会因为某个文件不是 UTF-8 就把整次扫描打断。

用法（在 witrelate 目录下）::

    ./.venv/Scripts/python.exe -m tools.check_compile
    ./.venv/Scripts/python.exe -m tools.check_compile --selftest   # 先证明它会响
    ./.venv/Scripts/python.exe -m tools.check_compile --root <目录>
"""

from __future__ import annotations

import argparse
import io
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 不扫的目录。``.wit`` 是本项目的运行期目录（`.wit/<role>/` 下可能有临时脚本），
#: 它里面"能不能编译"不是本项目的约定，扫进去只会制造噪声。
SKIP_DIRS = frozenset({".venv", ".git", "__pycache__", "node_modules", ".mypy_cache", ".wit"})


@dataclass(frozen=True)
class Failure:
    path: Path
    lineno: int | None
    message: str
    line: str | None
    source: bytes | None = None


def _iter_sources(root: Path) -> list[Path]:
    """现场派生待编译清单（**不是**手工维护的列表）。"""
    return sorted(
        p for p in root.rglob("*.py") if not any(part in SKIP_DIRS for part in p.parts)
    )


def _decode(path: Path) -> str | None:
    """尽力把文件读成文本（只为**打印出那一行**，判定不依赖它）。"""
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def scan(root: Path) -> tuple[int, list[Failure]]:
    """编译 ``root`` 下所有 ``.py``。返回 ``(编译总数, 失败清单)``。"""
    failures: list[Failure] = []
    sources = _iter_sources(root)
    for path in sources:
        try:
            source = path.read_bytes()
        except OSError as exc:
            failures.append(Failure(path, None, f"{type(exc).__name__}: {exc}", None))
            continue
        try:
            compile(source, str(path), "exec", dont_inherit=True)
        except SyntaxError as exc:
            text = _decode(path)
            line = None
            if text is not None and exc.lineno:
                lines = text.splitlines()
                if 1 <= exc.lineno <= len(lines):
                    line = lines[exc.lineno - 1]
            failures.append(
                Failure(path, exc.lineno, f"SyntaxError: {exc.msg}", line, source)
            )
        except (ValueError, UnicodeDecodeError) as exc:
            # 例如源码里有 NUL 字节（`ValueError: source code string cannot contain
            # null bytes`）—— 也是硬失败：编译不过就是编译不过，不许静默跳过。
            failures.append(
                Failure(path, None, f"{type(exc).__name__}: {exc}", None, source)
            )
    return len(sources), failures


def _adjacent_literals(source: bytes | None) -> bool:
    """源码里是否存在**两条字符串/名字紧挨着**（中间没有运算符、逗号）。

    这是那个坑的**结构特征**：``"a"b"`` 被编译器切成 ``STRING("a") NAME(b)``，
    于是报「Is this intended to be part of the string?」。
    用真正的 tokenizer 判，**不靠"引号个数"这种猜** —— 实测那条件不成立：
    项目里那行恰有 **6 个** ASCII 双引号（偶数），按奇偶判会**恰好漏掉唯一要抓的那一个**
    （本脚本第一版就是这么写的，selftest 当场把它顶回来了）。

    ⚠️ 已知不完美，不假装它完美：**隐式拼接**（``"a" "b"``）在 Python 里是合法的，
       结构上同样是"两个 STRING 紧挨着"，所以本函数会给出一句不相关的解释。
       因此它**只是"在文件已经编译不过的前提下"给出的解释方向**，
       **判定依据永远只有编译器**给的那一个。
    """
    if not source:
        return False
    tokens: list[tokenize.TokenInfo] = []
    try:
        for token in tokenize.tokenize(io.BytesIO(source).readline):
            tokens.append(token)
    except Exception:  # noqa: BLE001 - 半截也算：要的正是"断掉之前"那对
        pass
    skippable = {
        tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT,
        tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
    }
    significant = [t for t in tokens if t.type not in skippable]
    return any(
        a.type == tokenize.STRING and b.type in (tokenize.STRING, tokenize.NAME)
        for a, b in zip(significant, significant[1:])
    )


def _hint(failure: Failure) -> str | None:
    """给这类错误一句**可执行的**提示（本项目口径：拒绝要能指导行动）。

    ⚠️ 提示只在**判据真的指得出东西**时才给。不硬凑 —— 一句套话提示比没有提示更糟：
       它看上去像分析，其实什么都没说，还会让人以为"已经知道是什么问题了"。
    """
    if _adjacent_literals(failure.source):
        return (
            "这一处**两条字面量紧挨着** ⇒ 多半是双引号字符串里又嵌了一对 `\"`。"
            "本项目踩过 4 次，改法一律是换成中文引号「」"
            '（例如 它是"还在跑"  →  它是「还在跑」）。'
        )
    return None


def run(root: Path) -> int:
    total, failures = scan(root)
    if not failures:
        print(f"[通过] {total} 个 .py 全部编译通过（{root}）")
        return 0
    print(f"[失败] {total} 个 .py 里有 {len(failures)} 个编译不过：")
    for failure in failures:
        try:
            shown = failure.path.relative_to(root).as_posix()
        except ValueError:
            shown = failure.path.as_posix()
        where = f":{failure.lineno}" if failure.lineno else ""
        print(f"  · {shown}{where}")
        print(f"      {failure.message}")
        if failure.line is not None:
            print(f"      源码：{failure.line.strip()}")
        hint = _hint(failure)
        if hint:
            print(f"      ⇒ {hint}")
    return 1


_GOOD = "x = 1\n"
#: 合法：单引号字符串里含 `"` —— 用来证明本脚本**不是**按模式匹配判的
#: （这正是 grep 顶不上的原因：它分不出这对引号合不合法）。
_LEGAL_QUOTE = "y = '说 \"你好\" 就行'\n"
#: 就是在项目里咬了 4 次的那个形态：双引号字符串里又嵌了 `"`。
_TRAP = 'z = "他分不清它是"还在跑"还是"早就没了""\n'


def selftest() -> int:
    """负控：**先证明它会响**（本项目纪律：「装了」≠「会响」）。

    三条断言缺一不可：
      ① 只有合法文件时**必须整体通过**（否则"永远报失败"的实现也能过）；
      ② 真陷阱**必须被逮到、且只逮它**（合法的"单引号里含双引号"不许被误报 ——
        少了这条，一个"见到 `"` 就报错"的实现也能过）；
      ③ 失败时必须给出**指得出东西**的提示（不是一句套话）。
    """
    ok = True
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check_compile_selftest_") as td:
        root = Path(td)
        (root / "good.py").write_text(_GOOD, encoding="utf-8", newline="\n")
        (root / "legal_quote.py").write_text(_LEGAL_QUOTE, encoding="utf-8", newline="\n")

        total, failures = scan(root)
        green = total == 2 and not failures
        print(f"[1] {'✅' if green else '❌'} 只有合法文件时整体通过"
              f"（编译 {total}，失败 {len(failures)}）")
        ok = ok and green

        (root / "trap.py").write_text(_TRAP, encoding="utf-8", newline="\n")
        total, failures = scan(root)
        names = {f.path.name for f in failures}
        hit = total == 3 and names == {"trap.py"}
        print(f"[2] {'✅' if hit else '❌'} 真陷阱必须被逮到、且只逮它"
              f"（编译 {total}，失败 {sorted(names)}）")
        ok = ok and hit

        hint = _hint(failures[0]) if failures else None
        hinted = bool(hint) and "紧挨着" in (hint or "")
        print(f"[3] {'✅' if hinted else '❌'} 失败时给出**指得出东西**的提示")
        if failures:
            print(f"      {failures[0].path.name}:{failures[0].lineno} "
                  f"{failures[0].message}")
            print(f"      源码：{(failures[0].line or '').strip()}")
            print(f"      ⇒ {hint}")
        ok = ok and hinted
    print(f"[自检] {'全部通过' if ok else '有未通过项'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="全仓 Python 语法闸门")
    parser.add_argument("--root", default=None, help="要扫描的根目录（默认仓库根）")
    parser.add_argument("--selftest", action="store_true",
                        help="先证明这个脚本自己会响（负控）")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    return run(Path(args.root).resolve() if args.root else ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
