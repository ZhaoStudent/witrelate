"""显式走每一步：login → enroll，打印每一步的真实返回。

用途：`up_until_ready` 一步到位但看不到中间态，卡住时用这个定位。
"""

from __future__ import annotations

import asyncio
import os
import re
import traceback

import httpx

from wit import RegistryPasswordCredentials, WitRuntime

from legal_acps.acps_config import build_config
from legal_acps.agents import build_agent
from legal_acps.llm import build_model

ROLE = "incitement"

_orig_send = httpx.AsyncClient.send


async def _logged_send(self, request, **kwargs):  # type: ignore[no-untyped-def]
    url = str(request.url)
    print(f"    >>> {request.method} {url}")
    if "login" in url or "auth" in url:
        ctype = request.headers.get("content-type")
        body = request.content.decode("utf-8", "replace")
        body = re.sub(r'("password"\s*:\s*")[^"]*', r"\1***", body)
        body = re.sub(r"(password=)[^&]*", r"\1***", body)
        print(f"        content-type = {ctype}")
        print(f"        body         = {body[:300]}")
    return await _orig_send(self, request, **kwargs)


httpx.AsyncClient.send = _logged_send  # type: ignore[method-assign]


def show(tag: str, result) -> None:  # type: ignore[no-untyped-def]
    print(f"\n--- {tag} ---")
    print(f"  status            = {result.status}")
    print(f"  ready             = {result.ready}")
    print(f"  waiting_for_review= {getattr(result, 'waiting_for_review', '?')}")
    for attr in ("kind", "aic", "message", "detail", "next_action", "reasons"):
        value = getattr(result, attr, None)
        if value:
            print(f"  {attr:<18}= {value}")
    with __import__("contextlib").suppress(Exception):
        print("  summary:", result.summary().replace("\n", "\n           "))


async def main() -> None:
    cfg = build_config(role=ROLE)
    print(f"role   = {ROLE}")
    print(f"endpoint = {cfg.public_endpoint}")

    agent = build_agent(ROLE, build_model())
    runtime = WitRuntime(agent, workspace=f".wit/{ROLE}", acps=cfg)
    runtime.save_acs()

    creds = RegistryPasswordCredentials.from_env()
    print(f"username = {creds.username}")

    try:
        result = await runtime.login(credentials=creds)
        show("login()", result)
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ login() 抛异常：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return

    if getattr(result, "ready", False):
        print("\n✅ 已就绪，无需 enroll。")
        return

    if os.environ.get("SKIP_ENROLL") == "1":
        print("\n（SKIP_ENROLL=1，跳过 enroll）")
        return

    try:
        result = await runtime.enroll(credentials=creds)
        show("enroll()", result)
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ enroll() 抛异常：{type(exc).__name__}: {exc}")
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
