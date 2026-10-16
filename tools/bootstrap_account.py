"""注册 ACPs 开发者账号，并把凭据写进 .env。

背景：ACPs 的开发者账号就是「登录 Registry 的账号」。实测要点：
  * 注册只需 username + password，**不需要短信验证码**
  * 新账号默认带 CLIENT 角色，足以注册 Agent
  * wit 的 login 用 OAuth2 表单，与网页登录是同一套账号库

用法：
    ./.venv/bin/python -m tools.bootstrap_account <用户名> [密码]
不给密码就随机生成一个强密码并打印出来。

已存在同名账号时会自动改为「登录验证」，不会重复注册。
"""

from __future__ import annotations

import secrets
import string
import sys
from pathlib import Path

from tools.portal import login, register

ENV_PATH = Path(".env")


def gen_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def update_env(username: str, password: str) -> None:
    """就地更新 .env 里的 ACPS_USERNAME / ACPS_PASSWORD，其余行原样保留。"""
    if not ENV_PATH.exists():
        sys.exit(f"找不到 {ENV_PATH.resolve()}，请在 witrelate 目录下运行")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    seen_u = seen_p = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ACPS_USERNAME="):
            out.append(f"ACPS_USERNAME={username}")
            seen_u = True
        elif stripped.startswith("ACPS_PASSWORD="):
            # 密码用单引号包住，避免 shell 加载时误解特殊字符
            out.append(f"ACPS_PASSWORD='{password}'")
            seen_p = True
        else:
            out.append(line)
    if not seen_u:
        out.append(f"ACPS_USERNAME={username}")
    if not seen_p:
        out.append(f"ACPS_PASSWORD='{password}'")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("用法：bootstrap_account <用户名> [密码]")
    username = sys.argv[1]
    password = sys.argv[2] if len(sys.argv) > 2 else gen_password()

    print(f"目标账号 : {username}")
    print(f"密码长度 : {len(password)}")
    print()
    print("--- 尝试注册（若账号已存在会自动落到登录验证）---")
    token = register(username, password)

    if token:
        print("\n✅ 注册成功，账号已创建")
    else:
        print("\n账号可能已存在（或密码不合规），改用登录验证：")
        token = login(username, password)
        if not token:
            sys.exit("\n❌ 登录也失败，请检查用户名/密码")

    print(f"\n✅ 凭据可用（拿到令牌长度 {len(token)}）")

    update_env(username, password)
    print(f"✅ 已写入 {ENV_PATH.resolve()}（权限 600）")
    print()
    print("=" * 60)
    print("请记录以下凭据：")
    print(f"  用户名：{username}")
    print(f"  密码  ：{password}")
    print("=" * 60)


if __name__ == "__main__":
    main()
