"""深挖：已删除的 Agent 为何仍在 Discovery 结果里。

观察点：
  1. 发现结果里那条记录**有没有**任何"已删除"标记（is_deleted / deleted_at / active）
  2. registry 的 client 列表里它是不是已标 is_deleted=True（对比两边的差异）
  3. 重复查询是否有变化（判断是索引延迟还是索引根本没删）
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from dotenv_lite import load_env

load_env()

from wit.acps.discovery import AcpsDiscoveryClient  # noqa: E402

from legal_acps.acps_config import build_config  # noqa: E402
from legal_acps import wt_compat  # noqa: E402

TARGETS = {
    "1.2.156.3088.1.BUPT.81M3FX.YB1DAS.F56BVO.0QYM": "煽动性言论识别 Partner（demo）",
    "1.2.156.3088.1.BUPT.81M3FX.8R0Y7E.IYKGM1.0X32": "法律咨询主控（demo）",
    "1.2.156.3088.1.BUPT.81M3FX.B7G6IE.9D5VMV.1DZ8": "社会危害性评估（demo）",
}


def registry_side() -> None:
    """看 registry 侧这些记录的真实状态（软删除后应 is_deleted=True）。"""
    base = os.environ.get("ACPS_REGISTRY_URL", "https://wt.ioa.pub/registry-server")
    with httpx.Client(timeout=40) as c:
        tok = c.post(
            f"{base}/api/v1/auth/login",
            data={"username": os.environ["ACPS_USERNAME"], "password": os.environ["ACPS_PASSWORD"]},
        ).json()["access_token"]
        items = c.get(
            f"{base}/api/v1/agent/client", headers={"Authorization": f"Bearer {tok}"}
        ).json().get("items") or []

    print("=== registry 侧（/api/v1/agent/client）===")
    for it in items:
        if it.get("aic") in TARGETS:
            print(f"  {it.get('name')}")
            print(f"     is_deleted   = {it.get('is_deleted')}")
            print(f"     deleted_at   = {it.get('deleted_at')}")
            print(f"     is_disabled  = {it.get('is_disabled')}")
            print(f"     is_active    = {it.get('is_active')}")
            print(f"     approval     = {it.get('approval_status')}")


async def discovery_side(label: str) -> dict[str, dict]:
    wt_compat.apply()
    cfg = build_config(role="leader")
    client = AcpsDiscoveryClient(acps=cfg)
    found: dict[str, dict] = {}
    for q in ["煽动性识别", "社会危害性评估", "法律咨询"]:
        resp = await client.discover(query=q, limit=10, include_unavailable=False)
        for aic, acs in ((resp.get("result") or {}).get("acsMap") or {}).items():
            if aic in TARGETS:
                found[aic] = acs
    print(f"=== discovery 侧（第 {label} 次）===")
    if not found:
        print("  （未出现任何已删除的 demo）✅")
    for aic, acs in found.items():
        print(f"  {TARGETS[aic]}")
        print(f"     active = {acs.get('active')}")
        print(f"     字段   = {sorted(acs.keys())}")
        # 有没有任何和"删除"相关的字段
        marked = [k for k in acs if "delet" in k.lower() or "disabl" in k.lower()]
        print(f"     删除相关字段 = {marked or '（无）'}")
    return found


async def main() -> None:
    registry_side()
    print()
    a = await discovery_side("1")
    await asyncio.sleep(45)
    b = await discovery_side("2（45 秒后）")
    print()
    print("=== 判定 ===")
    if not a and not b:
        print("  两次都没有 → 清理已生效")
    elif a and not b:
        print("  第 1 次有、第 2 次没有 → 索引延迟，已自愈 ✅")
    else:
        print("  两次都有 → 发现索引未随软删除更新，需要额外手段 ❌")
        print("  仍在泄漏：")
        for aic in b or a:
            print(f"    {TARGETS[aic]}  {aic}")


if __name__ == "__main__":
    asyncio.run(main())
