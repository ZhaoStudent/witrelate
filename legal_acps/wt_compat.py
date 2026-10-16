"""wt 平台兼容补丁 —— 修掉 Discovery 的 400。

## 现象

Leader 调用 `acps_discover` 时，梧桐平台（wt.ioa.pub）返回：

    HTTP 400
    {"error":{"code":40004,"message":"FilterInvalid",
              "data":"filter 条件不合法：空节点（conditions 与 groups 皆空）。位置 filter。"}}

运行时表现为 leader 连续 8 次 `acps.discover (error)`，
最终只能由主控"独立研判"，专家委托完全没发生 —— 跨智能体互联是空的。

## 根因（实测）

wit SDK 的 discovery 默认 `include_unavailable=True`，
`discovery_filter()` 会生成：

    {"conditions": [{"field": "onlyAvailable", "op": "eq", "value": false}]}

wt 侧的 filter 解析器不认这个形态，判为"空节点"直接 400。
反之 `include_unavailable=False` 时 `discovery_filter()` 返回 `None`，
请求体里不带 `filter`，wt 正常返回 200：

    含 filter（True） ：400 FilterInvalid
    不含 filter（False）：200，acsMap 返回 3 条 ACS

`AcpsConfig` 里没有任何字段能控制这个行为，因此只能在调用层强制。

## 修法

把 `AcpsDiscoveryClient.discover` 的 `include_unavailable` 一律强制为 `False`。

语义上也更正确：Leader 派发任务时本来就只关心**当前可用**的 Partner，
一个不可用的 Agent 发现出来也没法委派。

调用方式：`import` 本模块即自动生效（见文件末尾），入口脚本无需额外代码。
"""

from __future__ import annotations

import functools
from typing import Any, Mapping

from wit.acps.discovery import AcpsDiscoveryClient

_original_discover = AcpsDiscoveryClient.discover
_PATCH_FLAG = "_wt_compat_patched"


@functools.wraps(_original_discover)
async def _discover_without_unavailable_filter(
    self: AcpsDiscoveryClient,
    *,
    query: str,
    limit: int = 5,
    include_unavailable: bool = True,  # noqa: ARG001 —— 有意忽略，一律按 False 发请求
    context: Mapping[str, Any] | None = None,
    filter: Mapping[str, Any] | None = None,  # noqa: A002 —— 同名遮蔽内建，跟随上游签名
) -> dict[str, Any]:
    return await _original_discover(
        self,
        query=query,
        limit=limit,
        include_unavailable=False,
        context=context,
        filter=filter,
    )


def apply() -> None:
    """打补丁（幂等）。重复调用只生效一次。"""
    if getattr(AcpsDiscoveryClient.discover, _PATCH_FLAG, False):
        return
    setattr(_discover_without_unavailable_filter, _PATCH_FLAG, True)
    AcpsDiscoveryClient.discover = _discover_without_unavailable_filter  # type: ignore[method-assign]


def is_applied() -> bool:
    """给自检脚本用：补丁是否已生效。"""
    return bool(getattr(AcpsDiscoveryClient.discover, _PATCH_FLAG, False))


# import 即生效 —— 这样任何 import 到 legal_acps 子模块的入口都能被覆盖到。
apply()

__all__ = ["apply", "is_applied"]
