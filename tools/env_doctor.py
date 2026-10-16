"""体检 .env：重复键、等号两侧带空格、值与后端是否自洽。

为什么需要它：手写 .env 出过两次事——
  1) 同一个键写了两遍（新旧账号并存），没人知道哪一行生效；
  2) 写成 `ACPS_USERNAME = xxx`（等号两侧带空格），
     一个按 `PASSWORD=` 匹配的打码脚本因此漏掉了它，密码被原样打印出来。

第 2 条说明：**空格差异会骗过正则**，所以别用手写正则去打码/改文件，用这个工具。

用法：
    python -m tools.env_doctor                      # 只体检，不改文件
    python -m tools.env_doctor --show-secrets       # 连值一起打印（默认打码）
    python -m tools.env_doctor --fix                # 备份后原地规范化
    python -m tools.env_doctor --fix --set ACPS_BACKEND=wt

规则（与对端 dotenv_lite 完全一致，不另立标准）：
  * 重复键 —— **最后一次出现生效**，前面的用 `# [重复键已停用] ` 注释掉
  * `KEY = value` —— 归一化成 `KEY=value`
  * 值两端的引号由 dotenv_lite 负责剥掉，本工具**不动引号**（改了反而会和运行时不一致）

退出码：0 = 干净；1 = 有问题（方便塞进 CI 或 verify.sh）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv_lite import parse_env_file  # noqa: E402

SECRET_HINTS = ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "PRIVATE")
DUP_PREFIX = "# [重复键已停用] "

# 这几个键缺失会直接让流程跑不起来，单独提示
REQUIRED_KEYS = ("ACPS_USERNAME", "ACPS_PASSWORD")


def mask(key: str, value: str | None) -> str:
    """密钥类只报长度，其余原样。"""
    if value is None:
        return "(未设置)"
    if value == "":
        return "(空)"
    if any(h in key.upper() for h in SECRET_HINTS):
        return f"<已设置 len={len(value)} 首字符={value[0]!r}>"
    return value


def scan_lines(path: Path) -> tuple[list[str], list[dict]]:
    """逐行抽取赋值语句。返回 (原始行, 条目列表)。

    条目里同时保留 raw 形态，才能发现「等号两侧有空格」这种差异。
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    entries: list[dict] = []
    for no, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        body = s[len("export "):].lstrip() if s.startswith("export ") else s
        if "=" not in body:
            continue
        raw_key, _, raw_value = body.partition("=")
        entries.append(
            {
                "lineno": no,
                "export": s.startswith("export "),
                "raw_key": raw_key,
                "raw_value": raw_value,
                "key": raw_key.strip(),
                "value": raw_value.strip(),
            }
        )
    return lines, entries


def diagnose(path: Path) -> tuple[list[dict], list[dict], dict[str, str]]:
    """返回 (重复键组, 空格问题, 生效值)。"""
    lines, entries = scan_lines(path)
    by_key: dict[str, list[dict]] = {}
    for e in entries:
        by_key.setdefault(e["key"], []).append(e)

    dups = [{"key": k, "items": v} for k, v in by_key.items() if len(v) > 1]
    spacing = [
        e
        for e in entries
        if e["raw_key"] != e["raw_key"].rstrip()
        or e["raw_value"] != e["raw_value"].lstrip()
    ]
    effective = parse_env_file(path)  # 与运行时同一套解析
    return dups, spacing, effective


def report(path: Path, *, show_secrets: bool) -> int:
    dups, spacing, effective = diagnose(path)
    lines, _ = scan_lines(path)

    print(f".env 位置 : {path}")
    print(f"总行数    : {len(lines)}")
    print()

    problems = 0

    if dups:
        problems += len(dups)
        print(f"[!] 重复键 {len(dups)} 个 —— .env 里后面的胜，只认最后一行：")
        for d in dups:
            print(f"    {d['key']}")
            for i, it in enumerate(d["items"]):
                mark = "生效" if i == len(d["items"]) - 1 else "被覆盖"
                # 这里也必须打码：早期版本裸打印 it["value"]，把密码泄漏到了终端和日志里。
                shown = it["value"] if show_secrets else mask(d["key"], it["value"])
                print(f"        第 {it['lineno']:>3} 行  [{mark}]  {d['key']}={shown}")
        print()
    else:
        print("[+] 无重复键")
        print()

    if spacing:
        problems += len(spacing)
        print(f"[!] 等号两侧有空格 {len(spacing)} 处（会让按 `KEY=` 写的正则匹配不到）：")
        for e in spacing:
            print(f"        第 {e['lineno']:>3} 行  {e['key']} = …")
        print()
    else:
        print("[+] 等号两侧无多余空格")
        print()

    for k in REQUIRED_KEYS:
        if k not in effective:
            problems += 1
            print(f"[!] 缺少必需的 {k}")
    if not any(k in effective for k in REQUIRED_KEYS):
        print()

    print("--- 实际生效的值 ---")
    for k in sorted(effective):
        v = effective[k]
        print(f"  {k:28} = {v if show_secrets else mask(k, v)}")
    print()

    backend = effective.get("ACPS_BACKEND")
    if not backend:
        print("[!] ACPS_BACKEND 未设置 —— 默认走 ioa_pub（www.ioa.pub）")
        print("    如果你填的账号是 wt.ioa.pub 注册的，注册会报 USER_NOT_FOUND。")
        print("    比赛指定平台是 wt，请在 .env 里加一行：ACPS_BACKEND=wt")
        problems += 1
    else:
        print(f"[+] ACPS_BACKEND = {backend}")
    print()

    return 1 if problems else 0


def fix(path: Path, *, sets: list[str]) -> int:
    backup = path.with_name(f"{path.name}.bak.{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    if backup.stat().st_mode & 0o777 != 0o600:
        backup.chmod(0o600)

    lines, entries = scan_lines(path)
    drop: set[int] = set()   # 要注释掉的重复键行号
    rewrite: dict[int, str] = {}  # 行号 -> 新内容

    by_key: dict[str, list[dict]] = {}
    for e in entries:
        by_key.setdefault(e["key"], []).append(e)

    for key, items in by_key.items():
        for it in items[:-1]:
            drop.add(it["lineno"])

    for e in entries:
        if e["lineno"] in drop:
            continue
        if e["raw_key"] != e["raw_key"].rstrip() or e["raw_value"] != e["raw_value"].lstrip():
            prefix = "export " if e["export"] else ""
            rewrite[e["lineno"]] = f"{prefix}{e['key']}={e['value']}"

    # --set：有就改最后一处，没有就追加到末尾
    append: list[str] = []
    for item in sets:
        if "=" not in item:
            print(f"[x] --set 参数格式应为 KEY=VALUE，收到 {item!r}")
            return 2
        key, _, value = item.partition("=")
        key = key.strip()
        if key in by_key:
            target = by_key[key][-1]["lineno"]
            raw_key = by_key[key][-1]["raw_key"]
            prefix = "export " if by_key[key][-1]["export"] else ""
            rewrite[target] = f"{prefix}{raw_key.strip()}={value}"
        else:
            append.append(f"{key}={value}")

    out: list[str] = []
    for no, raw in enumerate(lines, 1):
        if no in drop:
            out.append(f"{DUP_PREFIX}{raw}")
        elif no in rewrite:
            out.append(rewrite[no])
        else:
            out.append(raw)
    if append:
        out.append("")
        out.extend(append)

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    path.chmod(0o600)

    print(f"已备份原文件 -> {backup}")
    if drop:
        print(f"已停用重复键 {len(drop)} 行（保留为注释，可随时恢复）")
    if rewrite:
        print(f"已归一化 {len(rewrite)} 行")
    if append:
        print(f"已新增 {len(append)} 行：{', '.join(a.split('=')[0] for a in append)}")
    print()
    return report(path, show_secrets=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="体检并规范化 .env")
    ap.add_argument("--path", help=".env 路径，默认从当前目录向上找")
    ap.add_argument("--fix", action="store_true", help="备份后原地规范化")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="设置/覆盖某个键（可重复），配合 --fix 使用")
    ap.add_argument("--show-secrets", action="store_true", help="连密钥值一起打印")
    args = ap.parse_args()

    from dotenv_lite import find_env_file

    path = Path(args.path) if args.path else find_env_file()
    if path is None or not path.is_file():
        print("[x] 找不到 .env")
        return 2

    if args.fix:
        return fix(path, sets=args.set)
    if args.set:
        print("[x] --set 需要配合 --fix 使用")
        return 2
    return report(path, show_secrets=args.show_secrets)


if __name__ == "__main__":
    sys.exit(main())
