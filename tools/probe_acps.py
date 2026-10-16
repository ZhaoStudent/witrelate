"""探测脚本：让 wit 自己的客户端去访问平台，打印它实际请求的 URL。

用途：在填任何凭据之前，先确认「平台四个地址活着」「证书从哪来」。
不需要任何账号，失败也是有效信息。
"""

from __future__ import annotations

import asyncio
import sys
import traceback

import httpx

from wit import AcpsConfig, AgentProviderProfile
from wit.acps.ca import HttpCaClient
from wit.acps.registry import HttpRegistryClient, RegistryPasswordCredentials

# ioa_pub() 强制要求 provider（或 acs_path）：注册时要作为 ACS 里的机构信息提交
_PROVIDER = AgentProviderProfile(
    organization="BUPT-LegalAgent-Demo",
    country_code="CN",
    name="法律咨询多智能体课题组",
)

# 拦下所有出站请求，把真实 URL 打出来
_orig_send = httpx.AsyncClient.send


async def _logged_send(self, request, **kwargs):  # type: ignore[no-untyped-def]
    print(f"  >>> {request.method} {request.url}")
    return await _orig_send(self, request, **kwargs)


httpx.AsyncClient.send = _logged_send  # type: ignore[method-assign]


async def main() -> int:
    cfg = AcpsConfig.ioa_pub(provider=_PROVIDER)
    print("=" * 68)
    print("配置（来自 AcpsConfig.ioa_pub()，无需自己申请）")
    print(f"  registry_url      = {cfg.registry_url}")
    print(f"  registry_mtls_url = {cfg.registry_mtls_url}")
    print(f"  ca_url            = {cfg.ca_url}")
    print(f"  discovery_url     = {cfg.discovery_url}")
    print(f"  public_endpoint   = {cfg.public_endpoint or '（未设置）'}")
    print("=" * 68)

    results: dict[str, str] = {}

    # 1) CA 信任包：这一步能成功，说明平台 CA 是活的
    print("\n[1/3] 向平台 CA 拉取信任包 trust-bundle …")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            bundle = await HttpCaClient(acps=cfg)._download_trust_bundle(client)
        n = len(bundle) if bundle else 0
        print(f"  ✅ 成功，拿到 {n} 字节（含 {bundle.count(b'BEGIN CERTIFICATE')} 张证书）")
        results["ca_trust_bundle"] = f"OK ({n} bytes)"
    except Exception as exc:
        print(f"  ❌ {type(exc).__name__}: {exc}")
        results["ca_trust_bundle"] = f"FAIL {type(exc).__name__}"

    # 2) 注册服务登录：用假账号，预期「账号或密码错」而不是「接口不存在」
    print("\n[2/3] 用占位账号试探注册服务登录接口 …")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await HttpRegistryClient(acps=cfg).login(
                RegistryPasswordCredentials(username="__probe__", password="__probe__")
            )
        print("  ⚠️ 占位账号竟然登录成功？这不合常理")
        results["registry_login"] = "UNEXPECTED OK"
    except Exception as exc:
        msg = str(exc)
        if "401" in msg or "403" in msg or "credential" in msg.lower() or "密码" in msg:
            print(f"  ✅ 接口存在，拒绝了占位账号（符合预期）：{msg[:160]}")
            results["registry_login"] = "OK (接口存在)"
        else:
            print(f"  ⚠️ {type(exc).__name__}: {msg[:200]}")
            results["registry_login"] = f"UNKNOWN {type(exc).__name__}"
    print("      （真正登录需要你的 ioa.pub 账号，见下方环境变量）")

    # 3) 检查环境里有没有凭据
    print("\n[3/3] 检查环境变量中的开发者凭据 …")
    try:
        creds = RegistryPasswordCredentials.from_env()
        print(f"  ✅ 已读到 ACPS_USERNAME = {creds.username}")
        results["credentials"] = "已配置"
    except Exception as exc:
        print(f"  ⬜ 未配置：{exc}")
        results["credentials"] = "未配置"

    print("\n" + "=" * 68)
    print("结论")
    for k, v in results.items():
        print(f"  {k:<20} {v}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
