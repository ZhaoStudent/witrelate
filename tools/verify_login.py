"""验证凭据能否登录，并修正 ACCOUNT.txt 里被误写入的引号。

用法（在 witrelate 目录下）：
    ./.venv/bin/python -m tools.verify_login           # 只检查
    ./.venv/bin/python -m tools.verify_login --fix     # 检查并把 ACCOUNT.txt 修好

背景：生成 ACCOUNT.txt 时用了 `tr -d "\\x27"` 想剥掉密码外层的单引号，
但 GNU tr 只认八进制 `\\047`，`\\x27` 被当成字面量，于是引号被原样写进文件——
照着它去登录会拿到 401 Invalid Credentials。

本脚本的回显策略：**永不输出明文密码**，只给长度、掩码和首尾字符。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx

from dotenv_lite import load_env

REGISTRY = "https://www.ioa.pub/registry"
ACCOUNT_FILE = Path("ACCOUNT.txt")
TIMEOUT = 40.0
QUOTES = "'\"“”‘’"


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in QUOTES and value[-1] in QUOTES:
        return value[1:-1]
    return value


def _field(line: str) -> str | None:
    """取「用户名       ： xxx」这类行的值。"""
    if ":" not in line and "：" not in line:
        return None
    return re.split(r"[:：]", line, maxsplit=1)[-1].strip()


def parse_account_file(path: Path) -> tuple[str, str, bool]:
    """返回 (用户名, 密码, 文件里是否夹带了多余的引号)。

    注意 had_quotes 必须看**原始值**与剥离后的差异，不能只看登录结果——
    因为本函数已经替调用方剥了引号，登录照样会成功，那样就永远发现不了文件写坏了。
    """
    username = password = ""
    raw_password = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("用户名"):
            username = _field(line) or ""
        elif line.startswith("密码"):
            raw_password = _field(line) or ""
            password = _strip_quotes(raw_password)
    if not username or not password:
        sys.exit(f"无法从 {path} 解析出用户名/密码")
    return username, password, _strip_quotes(username) != username or raw_password != password


def try_login(username: str, password: str, label: str) -> bool:
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{REGISTRY}/api/v1/auth/login",
            data={"username": username, "password": password},  # 必须 form，不是 JSON
        )
    print(f"  [{label}] HTTP {resp.status_code}", end="  ")
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error_name", "")
        except Exception:  # noqa: BLE001
            detail = resp.text[:80]
        print(f"❌ 失败（{detail}）")
        return False
    print("✅ 登录成功")
    token = resp.json().get("access_token")
    with httpx.Client(timeout=TIMEOUT) as client:
        me = client.get(
            f"{REGISTRY}/api/v1/account/me",
            headers={"Authorization": f"Bearer {token}"},
        )
    if me.status_code < 400:
        data = me.json()
        print(f"          账号 ID = {data.get('id')}")
        print(f"          角色    = {data.get('roles')}   已启用 = {data.get('is_active')}")
    return True


def fix_account_file(path: Path, username: str, password: str) -> None:
    """把文件里的密码换成去掉引号后的正确值（用户名同理）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("密码"):
            prefix = line[: line.index("：") + 1] if "：" in line else line[: line.index(":") + 1]
            out.append(f"{prefix} {password}")
        elif stripped.startswith("用户名"):
            prefix = line[: line.index("：") + 1] if "：" in line else line[: line.index(":") + 1]
            out.append(f"{prefix} {username}")
        else:
            out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> None:
    fix = "--fix" in sys.argv

    if not ACCOUNT_FILE.is_file():
        sys.exit(f"找不到 {ACCOUNT_FILE}，请先 cd ~/witrelate")

    username, password, had_quotes = parse_account_file(ACCOUNT_FILE)
    print(f"账号文件    : {ACCOUNT_FILE.resolve()}")
    print(f"用户名      : {username}")
    print(f"密码长度    : {len(password)} 位")
    print(f"密码首尾字符: {password[0]} … {password[-1]}   （便于你自己核对，不给全貌）")
    if had_quotes:
        print("⚠️  检测到用户名外层有多余引号")
    print()

    print("① 用 ACCOUNT.txt 里的凭据登录：")
    ok_file = try_login(username, password, "ACCOUNT.txt")
    print()

    print("② 用自动加载的 .env 凭据登录（验证 dotenv_lite 链路）：")
    load_env()
    import os

    env_user = os.environ.get("ACPS_USERNAME", "")
    env_pass = os.environ.get("ACPS_PASSWORD", "")
    if not env_user or not env_pass:
        print("  ❌ .env 里读不到 ACPS_USERNAME/ACPS_PASSWORD")
        ok_env = False
    else:
        print(f"     读到：用户名 {env_user}，密码 {len(env_pass)} 位")
        ok_env = try_login(_strip_quotes(env_user), _strip_quotes(env_pass), ".env")
    print()

    if had_quotes:
        print("⚠️  ACCOUNT.txt 里的值外层夹带了引号——照抄去网页登录会拿到 401。")
        print()

    if ok_file and ok_env:
        print("结论：两处凭据都可用。")
        if not had_quotes:
            return
        if fix:
            fix_account_file(ACCOUNT_FILE, username, password)
            print(f"已修正 {ACCOUNT_FILE}：去掉外层引号，保留原密码与权限。")
            print("建议重新运行一次本命令，确认 ① 仍然是 ✅。")
        else:
            print("但文件里仍夹着引号，加 --fix 可自动修正。")
        return
    if ok_env and not ok_file:
        print("结论：.env 里的凭据是对的，ACCOUNT.txt 写错了。")
        if fix:
            fix_account_file(ACCOUNT_FILE, username, password)
            print(f"已修正 {ACCOUNT_FILE}（已去掉外层引号，权限保持）。")
            print("请重新运行本命令确认 ① 也变成 ✅。")
        else:
            print("加 --fix 参数可以自动修正 ACCOUNT.txt。")
        return

    print("结论：两处都无法登录，需要重新核对账号。")
    sys.exit(1)


if __name__ == "__main__":
    main()
