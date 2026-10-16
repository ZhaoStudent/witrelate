"""直连 ACPs Registry 的 HTTP 接口。

为什么需要它：wit 只封装了「开发者凭据登录 → 注册 Agent」这条主路径，
但平台还有几件事必须走原始接口（wit 里没有对应方法）：

    POST /api/v1/auth/register                自助注册账号（无需短信验证码）
    GET  /api/v1/account/me                   查看当前账号
    POST /api/v1/verification/identity        提交实名认证（wit 无此方法）
    GET  /api/v1/verification/identity        查询实名认证状态
    GET  /api/v1/agent/client                 查看自己名下已注册的 Agent
    GET  /api/v1/agent/public/recent          查看平台已审批通过的 Agent

接口契约来源：https://www.ioa.pub/registry/openapi.json

用法（在 witrelate 目录下）：
    ./.venv/bin/python -m tools.portal whoami
    ./.venv/bin/python -m tools.portal agents
    ./.venv/bin/python -m tools.portal recent
    ./.venv/bin/python -m tools.portal identity-status
    ./.venv/bin/python -m tools.portal register <用户名> <密码>
    ./.venv/bin/python -m tools.portal submit-identity <真实姓名> <身份证号>
    ./.venv/bin/python -m tools.portal self-test     # 离线验证回显打码（不联网、不需要凭据）

⚠️ 破坏性操作（delete-agent / cancel）有三道闸门，见 `guard_destructive`：
   ① 注册局白名单（BASE 默认是 www，本项目实际是 wt —— 必须显式覆盖）
   ② 身份回显（会打印待删记录的 name / status / aic / endpoint 让你核对）
   ③ 必须加 `--yes`，否则只做只读核对就退出
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from dotenv_lite import load_env

# 自动读项目根目录的 .env，省掉手写 `set -a && . ./.env && set +a`
load_env()

BASE = os.environ.get("ACPS_REGISTRY_URL", "https://www.ioa.pub/registry")
TIMEOUT = 40.0


#: 响应里需要打码的键（**子串**匹配，大小写不敏感）。
#:
#: 为什么必须有：`login()` 曾用 `_show()` 打印整个登录响应，把
#: `access_token` / `refresh_token` **原样**打进终端（2026-09-30 实测踩到）。
#: 这直接违反「凭据类输出绝不回显」（SKILL「工程约定」第 1 条）——
#: 而那一条正是为了避免敏感内容保护把整条工具结果扣留、等于白跑。
_REDACT_KEY_HINTS = ("token", "secret", "password", "api_key", "apikey", "credential")


def _mask(value: object) -> str:
    """只回显长度，不回显内容 —— 长度足够判断"有没有填"，又不会泄漏。"""
    text = value if isinstance(value, str) else str(value)
    return f"<redacted len={len(text)}>"


def redact(value: object) -> object:
    """**递归**把响应里的凭据字段换成 `<redacted len=N>`。

    必须递归：token/secret 经常藏在嵌套对象或数组里
    （例如 `{"data": {"access_token": ...}}`）。
    """
    if isinstance(value, dict):
        return {
            k: (_mask(v) if isinstance(k, str) and any(h in k.lower() for h in _REDACT_KEY_HINTS) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _show(tag: str, resp: httpx.Response) -> None:
    print(f"  HTTP {resp.status_code}  <- {tag}")
    text = resp.text
    try:
        text = json.dumps(redact(json.loads(text)), ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
    print("  " + text[:1500].replace("\n", "\n  "))


def self_test() -> int:
    """离线自测：证明打码真的生效，且不误伤普通字段。

    依据「工程约定」第 10 条 —— **打码脚本自身也要被测**，
    否则打码工具本身就是泄漏源。整个用例不含任何真实凭据。
    """
    sample = {
        "access_token": "eyJhbGciOi.JIUzI1NiIs.PAYLOAD",
        "refresh_token": "abc123",
        "expires_at": "2026-09-30T15:38:04+08:00",
        "nested": {"client_secret": "s3cr3t", "ok": 1},
        "items": [{"password": "p@ss", "name": "keep-me"}],
    }
    masked = redact(sample)
    print(json.dumps(masked, ensure_ascii=False, indent=2))
    flat = json.dumps(masked, ensure_ascii=False)
    leaked = [s for s in ("eyJhbGciOi", "abc123", "s3cr3t", "p@ss") if s in flat]
    if leaked:
        print(f"\n[失败] 打码后仍能读到明文：{leaked}")
        return 1
    if masked["expires_at"] != sample["expires_at"] or masked["items"][0]["name"] != "keep-me":
        print("\n[失败] 打码误伤了不该打的字段")
        return 1
    print("\n[通过] 凭据字段已打码（含嵌套/数组），普通字段未被误伤。")
    return 0


def register(username: str, password: str) -> str | None:
    """自助注册。平台对「仅用户名+密码」的注册不要求短信验证码。"""
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{BASE}/api/v1/auth/register",
            json={"username": username, "password": password},
        )
    _show("POST /api/v1/auth/register", resp)
    if resp.status_code < 400:
        return resp.json().get("access_token")
    return None


def login(username: str, password: str) -> str | None:
    """OAuth2 表单登录（注意必须是 form-urlencoded，不是 JSON）。"""
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{BASE}/api/v1/auth/login",
            data={"username": username, "password": password},
        )
    _show("POST /api/v1/auth/login", resp)
    if resp.status_code < 400:
        return resp.json().get("access_token")
    return None


def _token_from_env_or_login() -> str:
    username = os.environ.get("ACPS_USERNAME")
    password = os.environ.get("ACPS_PASSWORD")
    if not username or not password:
        sys.exit("需要 ACPS_USERNAME / ACPS_PASSWORD 环境变量")
    token = login(username, password)
    if not token:
        sys.exit("登录失败，见上面的响应")
    return token


def whoami(token: str) -> None:
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(
            f"{BASE}/api/v1/account/me",
            headers={"Authorization": f"Bearer {token}"},
        )
    _show("GET /api/v1/account/me", resp)


def identity_status(token: str) -> None:
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(
            f"{BASE}/api/v1/verification/identity",
            headers={"Authorization": f"Bearer {token}"},
        )
    _show("GET /api/v1/verification/identity", resp)


def submit_identity(token: str, real_name: str, id_number: str, id_type: str = "id_card") -> None:
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{BASE}/api/v1/verification/identity",
            headers={"Authorization": f"Bearer {token}"},
            json={"real_name": real_name, "id_number": id_number, "id_type": id_type},
        )
    _show("POST /api/v1/verification/identity", resp)


def my_agents(token: str) -> None:
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(
            f"{BASE}/api/v1/agent/client",
            headers={"Authorization": f"Bearer {token}"},
        )
    _show("GET /api/v1/agent/client", resp)


def recent_agents() -> None:
    """公开接口，不需要 token。"""
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(f"{BASE}/api/v1/agent/public/recent")
    _show("GET /api/v1/agent/public/recent", resp)


def cancel_agent(token: str, agent_id: str, confirmed: bool = False) -> None:
    """撤销处于「审核中」的 Agent 申请。**默认只做只读核对，需 `--yes` 才真的撤。**"""
    guard_destructive("cancel", agent_id, token, confirmed)
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{BASE}/api/v1/agent/client/{agent_id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
    _show(f"POST /api/v1/agent/client/{agent_id}/cancel", resp)


def fetch_agent_by_id(token: str, agent_id: str) -> dict | None:
    """在自己名下按 id 查一条 Agent，用于**删除前确认身份**。找不到返回 None。"""
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(
            f"{BASE}/api/v1/agent/client",
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    for item in (resp.json().get("items") or []):
        if item.get("id") == agent_id:
            return item
    return None


#: 允许执行破坏性操作（删除/撤销）的注册局白名单。
#: 加白名单的原因：`BASE` 默认是 `https://www.ioa.pub/registry`，
#: 而本项目实际用的是 wt（`https://wt.ioa.pub/registry-server`）。
#: 忘了覆盖 `ACPS_REGISTRY_URL` 时，删除会打到**另一个平台的同名账号**上去。
SAFE_REGISTRY_HOSTS = ("wt.ioa.pub",)


def guard_destructive(action: str, agent_id: str, token: str, confirmed: bool) -> dict:
    """破坏性操作前的三道闸门：注册局白名单 → 身份回显 → 显式确认。

    返回被确认的 Agent 记录，供调用方继续执行。
    """
    from urllib.parse import urlparse

    host = urlparse(BASE).netloc.split(":")[0]
    print(f"  目标注册局 : {BASE}")
    if host not in SAFE_REGISTRY_HOSTS:
        sys.exit(
            f"\n❌ 拒绝执行 `{action}`：注册局 `{host}` 不在白名单 {SAFE_REGISTRY_HOSTS} 内。\n"
            f"   本项目用的是 wt 平台，请显式覆盖后重试：\n"
            f"     ACPS_REGISTRY_URL=https://wt.ioa.pub/registry-server "
            f"./.venv/bin/python -m tools.portal {action} {agent_id}\n"
            f"   （BASE 默认值是 www.ioa.pub —— 忘了覆盖就会操作到另一个平台）"
        )

    record = fetch_agent_by_id(token, agent_id)
    if record is None:
        sys.exit(f"\n❌ 在你名下（{host}）找不到 id = {agent_id} 的 Agent，已中止。")

    acs = record.get("acs") or {}
    print("  待操作记录 :")
    print(f"      id       : {record.get('id')}")
    print(f"      name     : {record.get('name')}")
    print(f"      version  : {record.get('version')}")
    print(f"      status   : {record.get('approval_status')}")
    print(f"      active   : {acs.get('active')}")
    print(f"      aic      : {record.get('aic')}")
    for ep in (acs.get("endPoints") or []):
        print(f"      endpoint : {ep.get('url')}")

    if not confirmed:
        sys.exit(
            f"\n⚠️  未加 `--yes`，已中止（仅做了只读核对，未改动任何东西）。\n"
            f"   确认无误后重跑："
            f"ACPS_REGISTRY_URL=https://wt.ioa.pub/registry-server "
            f"./.venv/bin/python -m tools.portal {action} {agent_id} --yes"
        )
    print(f"\n  ✅ 已确认，执行 {action} ……")
    return record


def delete_agent(token: str, agent_id: str, confirmed: bool = False) -> None:
    """删除自己名下的 Agent。**默认只做只读核对，需 `--yes` 才真的删。**"""
    guard_destructive("delete-agent", agent_id, token, confirmed)
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.request(
            "DELETE",
            f"{BASE}/api/v1/agent/client/{agent_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    _show(f"DELETE /api/v1/agent/client/{agent_id}", resp)


def main() -> None:
    parser = argparse.ArgumentParser(description="ACPs Registry 原始接口工具")
    parser.add_argument(
        "action",
        choices=[
            "whoami",
            "agents",
            "recent",
            "identity-status",
            "register",
            "submit-identity",
            "cancel",
            "delete-agent",
            "self-test",
        ],
    )
    parser.add_argument("args", nargs="*", help="register: <用户名> <密码>；submit-identity: <真实姓名> <身份证号>")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="破坏性操作（delete-agent / cancel）的显式确认。不加则只做只读核对。",
    )
    opts = parser.parse_args()

    if opts.action == "self-test":
        sys.exit(self_test())

    if opts.action == "recent":
        recent_agents()
        return

    if opts.action == "register":
        if len(opts.args) < 2:
            sys.exit("用法：register <用户名> <密码>")
        token = register(opts.args[0], opts.args[1])
        if token:
            print("\n✅ 注册成功，已签发令牌。")
        return

    token = _token_from_env_or_login()
    if opts.action == "whoami":
        whoami(token)
    elif opts.action == "agents":
        my_agents(token)
    elif opts.action == "identity-status":
        identity_status(token)
    elif opts.action == "submit-identity":
        if len(opts.args) < 2:
            sys.exit("用法：submit-identity <真实姓名> <身份证号>")
        submit_identity(token, opts.args[0], opts.args[1])
    elif opts.action == "cancel":
        if not opts.args:
            sys.exit("用法：cancel <agent_id> [--yes]")
        cancel_agent(token, opts.args[0], confirmed=opts.yes)
    elif opts.action == "delete-agent":
        if not opts.args:
            sys.exit("用法：delete-agent <agent_id> [--yes]")
        delete_agent(token, opts.args[0], confirmed=opts.yes)


if __name__ == "__main__":
    main()
