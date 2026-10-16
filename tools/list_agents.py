"""列出「当前后端」下我名下的 Agent（状态 / AIC / endpoint）。

用途：
  * 确认注册与审核进展（比翻网页快，也能看出 endpoint 写对没有）
  * 拿 AIC —— 后续给「已 active 的 AIC」申请一次性 EAB 凭据要用它

后端由 .env 的 ACPS_BACKEND 决定（wt = 比赛指定的梧桐平台，ioa_pub = www.ioa.pub），
所以同一个工具两边都能查。

用法：
    python -m tools.list_agents            # 表格
    python -m tools.list_agents --json     # 原始 JSON
    python -m tools.list_agents --aic      # 只打印 AIC 列表，便于喂给别的命令
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv_lite import load_env  # noqa: E402
from legal_acps.acps_config import BACKENDS, backend  # noqa: E402


def _opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # 显式禁用代理：本机常挂着 http_proxy，会让直连平台变成"假失败"
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx)
    )


def _call(opener, url: str, *, data: bytes | None = None, token: str | None = None,
          method: str | None = None) -> tuple[int, str]:
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def login(opener, registry_url: str) -> str:
    user = os.environ.get("ACPS_USERNAME") or ""
    pwd = os.environ.get("ACPS_PASSWORD") or ""
    if not user or not pwd:
        raise RuntimeError("缺少 ACPS_USERNAME / ACPS_PASSWORD（检查 .env）")
    body = urllib.parse.urlencode({"username": user, "password": pwd}).encode()
    st, txt = _call(opener, f"{registry_url}/api/v1/auth/login", data=body)
    if st != 200:
        raise RuntimeError(f"登录失败 HTTP {st}: {txt[:300]}")
    return json.loads(txt)["access_token"]


def fetch_agents(opener, registry_url: str, token: str) -> list[dict]:
    st, txt = _call(opener, f"{registry_url}/api/v1/agent/client", token=token)
    if st != 200:
        raise RuntimeError(f"读取 Agent 列表失败 HTTP {st}: {txt[:300]}")
    d = json.loads(txt)
    if isinstance(d, list):
        return d
    for k in ("items", "data", "list", "agents"):
        if isinstance(d.get(k), list):
            return d[k]
    return []


def _endpoints(item: dict) -> list[str]:
    acs = item.get("acs")
    if isinstance(acs, str):
        try:
            acs = json.loads(acs)
        except Exception:
            return []
    if not isinstance(acs, dict):
        return []
    return [str(e.get("url")) for e in acs.get("endPoints", []) if isinstance(e, dict)]


def _state(item: dict) -> str:
    """把几个布尔/枚举字段拼成一眼能懂的状态。

    注意：接口没有 `status` 字段（写成 status 会恒为 None）。
    真正的字段是 approval_status / is_active / is_disabled（见平台 OpenAPI 的 AgentResponse）。
    """
    approval = item.get("approval_status")
    parts = [str(approval) if approval is not None else "?"]
    if item.get("is_active"):
        parts.append("active")
    if item.get("is_disabled"):
        parts.append("已禁用")
    if item.get("is_deleted"):
        parts.append("已删除")
    if not item.get("aic"):
        parts.append("无AIC")
    return "/".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="列出当前后端下我名下的 Agent")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    ap.add_argument("--aic", action="store_true", help="只输出 AIC，一行一个")
    args = ap.parse_args()

    load_env()
    name = backend()
    registry_url = BACKENDS[name]["registry_url"]

    if not args.json and not args.aic:
        print(f"平台后端 : {name}")
        print(f"registry : {registry_url}")
        print(f"账号     : {os.environ.get('ACPS_USERNAME')}")
        print()

    opener = _opener()
    token = login(opener, registry_url)
    agents = fetch_agents(opener, registry_url, token)

    if args.json:
        print(json.dumps(agents, ensure_ascii=False, indent=2))
        return 0
    if args.aic:
        for a in agents:
            if a.get("aic"):
                print(a["aic"])
        return 0

    if not agents:
        print("(名下暂无 Agent)")
        return 0

    usable = [a for a in agents if a.get("aic") and a.get("is_active")]
    print(f"共 {len(agents)} 个；其中可发 EAB 凭据的（有 AIC 且 active）= {len(usable)}")
    print()
    for a in agents:
        print(f"  [{_state(a)}]  {a.get('name')}  (v{a.get('version')})")
        print(f"        id  : {a.get('id')}")
        print(f"        aic : {a.get('aic')}")
        eps = _endpoints(a)
        if eps:
            for e in eps:
                print(f"        ep  : {e}")
        else:
            print("        ep  : (无)")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
