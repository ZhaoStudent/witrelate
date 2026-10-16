"""裸调 Registry 登录接口，打印状态码与响应体。

`login()` 只在内部把失败折叠成 needs_login，看不到平台的原话，
这个脚本直接问平台：到底缺什么。
"""

from __future__ import annotations

import json
import os

import httpx

from wit import RegistryPasswordCredentials

creds = RegistryPasswordCredentials.from_env()

REGISTRY = os.environ.get("ACPS_REGISTRY_URL", "https://www.ioa.pub/registry")

candidates = [
    f"{REGISTRY}/api/v1/auth/login",
    f"{REGISTRY}/api/v1/developer/login",
    f"{REGISTRY}/api/v1/login",
]

with httpx.Client(timeout=30.0, follow_redirects=False) as client:
    for url in candidates:
        print("=" * 70)
        print(f"POST {url}")
        payload = {"username": creds.username, "password": creds.password}
        try:
            resp = client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 请求失败：{type(exc).__name__}: {exc}")
            continue

        print(f"  HTTP {resp.status_code}  content-type={resp.headers.get('content-type')}")
        body = resp.text
        # 把 token 之类的敏感串截断，避免刷屏
        try:
            data = json.loads(body)
            body = json.dumps(data, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass
        print("  body:", body[:1200])

        if resp.status_code < 400 and "token" in resp.text.lower():
            keys = list(json.loads(resp.text).keys())
            print(f"\n  ✅ 登录成功，返回字段：{keys}")
            break
