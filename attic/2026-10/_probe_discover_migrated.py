"""#125 迁移一次性核查：三个模态角色的旧注册必须从 discovery 召不回。

判据纪律（同 check_discovery_pollution）：
  退出码 1 —— 仍有旧 AIC 出现在发现结果里（删除未生效/污染）；
  退出码 2 —— 无法判定（查询失败，或一条结果都没取到）；
  退出码 0 —— 确认召不回（全部查询成功、至少检视到一条 AIC、均非旧 AIC）。
"""

from __future__ import annotations

import asyncio
import sys

from dotenv_lite import load_env

load_env()

from wit.acps.discovery import AcpsDiscoveryClient  # noqa: E402

from legal_acps.acps_config import build_config  # noqa: E402
from legal_acps import wt_compat  # noqa: E402

#: 被删除的三叶子旧 AIC（2026-10-14 删注册前实测记录）。留着当回归判据。
RETIRED_AICS = {
    "1.2.156.3088.1.BUPT.81M3FX.3JGPMW.9ZWOMP.05Q9": "图像鉴伪 Partner（旧）",
    "1.2.156.3088.1.BUPT.81M3FX.3A82Y9.CDQRMV.0EJR": "音频鉴伪 Partner（旧）",
    "1.2.156.3088.1.BUPT.81M3FX.7V3D5C.K4IG1U.09VS": "视频鉴伪 Partner（旧）",
    # 2026-10-15 追加：endpoint 误指向第一台的那三条（已删，待重注册）。
    "1.2.156.3088.1.BUPT.81M3FX.EVW5AI.SUBQ8V.0BY3": "图像鉴伪 Partner（坏endpoint）",
    "1.2.156.3088.1.BUPT.81M3FX.6QQP6S.VW2NSL.0SEJ": "音频鉴伪 Partner（坏endpoint）",
    "1.2.156.3088.1.BUPT.81M3FX.JLDJG1.X2JVN8.0DXK": "视频鉴伪 Partner（坏endpoint）",
}

QUERIES = ["图像鉴伪", "音频鉴伪", "视频鉴伪", "鉴伪", "证据"]


async def main() -> int:
    wt_compat.apply()
    cfg = build_config(role="leader")
    client = AcpsDiscoveryClient(acps=cfg)

    seen: dict[str, str] = {}
    failed: list[str] = []
    for q in QUERIES:
        try:
            resp = await client.discover(query=q, limit=10, include_unavailable=False)
        except Exception as exc:  # noqa: BLE001
            print(f"### 查询 {q!r} -> ERR {type(exc).__name__}: {str(exc)[:140]}")
            failed.append(q)
            continue
        acs_map = (resp.get("result") or {}).get("acsMap") or {}
        print(f"### 查询 {q!r} -> {len(acs_map)} 条")
        for aic, acs in acs_map.items():
            if not isinstance(aic, str):
                continue
            name = acs.get("name") if isinstance(acs, dict) else None
            seen[aic] = name
            short = ".".join(aic.split(".")[-2:])
            warn = "   <-- 已删除的旧注册！" if aic in RETIRED_AICS else ""
            print(f"    - {name}  (…{short}){warn}")
        print()

    leaked = sorted(a for a in seen if a in RETIRED_AICS)
    if leaked:
        print("结论：发现结果仍有旧注册 —— 召回了：")
        for a in leaked:
            print(f"    {RETIRED_AICS[a]}  {a}")
        return 1
    if failed or not seen:
        print("结论：⚠️ 无法判定 —— 没有取得有效发现结果，不能据此宣布「召不回」。")
        if failed:
            print(f"    失败查询：{failed}")
        if not seen:
            print("    所有查询 0 条 —— 先查接口/网络，再重跑。")
        return 2
    print(
        f"结论：三个旧注册已召不回 ✅（{len(QUERIES)} 查询全部成功、"
        f"共检视 {len(seen)} 条 AIC，均非旧 AIC）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
