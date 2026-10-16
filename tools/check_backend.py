"""平台连通性自检 —— 在跑注册之前，先确认"四个地址"真的通。

为什么需要它：注册失败的原因常常混在一起（账号错、地址错、端口没开、
被本机代理拦住）。这个脚本把每一层单独探一遍，让你一眼看出卡在哪。

用法（在 witrelate 目录下）：

    ./.venv/bin/python -m tools.check_backend

它做三件事：

  1. 打印当前后端（ACPS_BACKEND）与四个地址 —— 先确认"我以为连的是哪个平台"
  2. 逐个探测服务端点是否可达，并分辨"服务存在"与"SPA 兜底页"
  3. 如果 .env 里填了凭据，就走一次真实登录，确认账号能在这套平台登录

注意：本机若设了 http_proxy，脚本会自动绕过它（平台域名在境内直连即可），
否则会看到莫名其妙的 502/连接失败。
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

from dotenv_lite import load_env

from legal_acps.acps_config import BACKENDS, backend, build_config

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

# 明确不使用系统代理：这台机器上 http_proxy 指向本地端口，
# 走代理会让境内平台请求出现假失败（之前排查过这个坑）。
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_SSL),
)

OK = "  [通]"
BAD = "  [断]"


def _req(url: str, *, method: str = "GET", data: bytes | None = None,
         headers: dict[str, str] | None = None, timeout: float = 15.0):
    """发一个请求，返回 (状态码, 响应体文本)。异常时状态码为 0。"""
    request = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            return resp.status, resp.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2000).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 探测脚本，任何异常都当作"不通"
        return 0, f"{type(exc).__name__}: {exc}"


def probe(name: str, url: str, *, method: str = "GET",
          expect: tuple[int, ...] = (200, 401, 405), **kw) -> tuple[str, bool]:
    code, body = _req(url, method=method, **kw)
    good = code in expect
    mark = OK if good else BAD
    print(f"{mark} {name:22} HTTP {code:<4} {url}")
    if not good:
        hint = {
            0: "连不上：域名解析/网络/端口未开放（也可能是该端口需要 mTLS 客户端证书，属正常）",
            404: "路径不存在，地址可能写错",
        }.get(code)
        if hint:
            print(f"        → {hint}")
    return name, good


def main() -> int:
    load_env()

    print("=" * 72)
    print(f"平台后端：{backend()}    （由 ACPS_BACKEND 决定）")
    print("=" * 72)
    preset = BACKENDS[backend()]
    for key, value in preset.items():
        override = os.environ.get("ACPS_" + key.upper())
        shown = override or value
        tag = "  ← 被环境变量覆盖" if override else ""
        print(f"  {key:18} = {shown}{tag}")
    print()

    cfg = build_config(role="leader")
    print(f"本机对外地址 public_endpoint = {cfg.public_endpoint or '（未设置）'}")
    print()

    print("-" * 72)
    print("① 服务端点可达性")
    print("-" * 72)
    results: list[bool] = []

    registry = cfg.registry_url.rstrip("/")
    _, ok = probe("registry 健康检查", f"{registry}/health", expect=(200,))
    results.append(ok)
    _, ok = probe("registry 登录接口", f"{registry}/api/v1/auth/login",
                  method="POST",
                  data=b"username=__probe__&password=__probe__",
                  headers={"Content-Type": "application/x-www-form-urlencoded"},
                  expect=(400, 401, 422))
    results.append(ok)

    ca = cfg.ca_url.rstrip("/")
    _, ok = probe("CA 服务", f"{ca}/", expect=(200, 404, 405, 301, 302))
    results.append(ok)

    discovery = cfg.discovery_url.rstrip("/")
    _, ok = probe("Discovery（POST-only）", discovery, method="POST",
                  data=json.dumps({"type": "exploratory", "query": "ping"}).encode(),
                  headers={"Content-Type": "application/json"},
                  expect=(200, 400, 401, 403, 405, 422))
    results.append(ok)

    mtls = cfg.registry_mtls_url
    if mtls:
        print(f"  [--] {'registry mTLS':22} 跳过探测：该端口要求客户端证书，"
              f"裸连必然失败，属正常\n        {mtls}")

    print()
    print("-" * 72)
    print("② 账号登录（需要 .env 里填了 ACPS_USERNAME / ACPS_PASSWORD）")
    print("-" * 72)
    username = os.environ.get("ACPS_USERNAME", "").strip()
    password = os.environ.get("ACPS_PASSWORD", "")
    if not username or not password:
        print("  [--] 未填凭据，跳过。填好后重跑本脚本即可验证账号。")
    else:
        # 引号陷阱：值里带引号会导致 401，但看不出原因（之前踩过）
        if password != password.strip("'\"") or password.strip() != password:
            print("  [!!] ACPS_PASSWORD 首尾含空白或引号，这会直接导致登录失败。")
        code, body = _req(
            f"{registry}/api/v1/auth/login",
            method="POST",
            data=f"username={username}&password={password}".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if code == 200:
            print(OK + f" 登录成功：{username} @ {backend()}")
            results.append(True)
        else:
            err = ""
            try:
                err = json.loads(body).get("error_name", "")
            except Exception:  # noqa: BLE001
                err = body[:120]
            print(BAD + f" 登录失败 HTTP {code}  {err}")
            extra = {
                "USER_NOT_FOUND": "该账号在这套平台不存在 —— 注意 www 和 wt 是两套独立账号库，"
                                  "别把 www 的账号填在 wt 上（反之亦然）。",
                "INVALID_PASSWORD": "密码不对。检查 .env 里是否有多余的引号或空格。",
            }.get(err)
            if extra:
                print(f"        → {extra}")
            results.append(False)

    print()
    print("=" * 72)
    passed = sum(1 for r in results if r)
    print(f"结论：{passed}/{len(results)} 项通过")
    if passed == len(results):
        print("→ 平台侧就绪。可以跑： python -m legal_acps.run_agent incitement --register-only")
    else:
        print("→ 先解决上面标 [断] 的项，再往下走。")
    print("=" * 72)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
