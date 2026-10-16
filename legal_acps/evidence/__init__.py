"""图像鉴伪（`evidence-image`）的本地初检能力。

分层（每层都有各自的断言，互不越界）：
  * `contracts`  —— 证据契约（请求 / 发现 / 报告 / 复检建议）。**只依赖 pydantic。**
  * `forensics`  —— 纯函数检测核心（Pillow + numpy，无模型、**不联网**、可离线断言）
  * `fetch`      —— 取图入口（检测部分一律不在这层）。
                   本项目**唯一一处主动向外部地址发起请求**的环节。风险在**出站方向**
                   （SSRF / 内网探测），**没有**新增无 mTLS 的入站端口；策略默认拒绝。
  * `pipeline`   —— 把上面两层接起来，并**用固定模板**拼给主控的摘要

## ⚠️ 这个包的导出是**惰性**的（PEP 562），不是随手写的

`legal_acps/agents.py` 需要 `evidence.contracts` 里的请求模型来生成工具 schema。
但**任何** `import legal_acps.evidence.*` 都会先执行本文件 —— 如果这里在模块级
`from .forensics import analyze`，那么**连唯一需要的纯 pydantic 契约**也会顺带
把 numpy / Pillow 拉进来。

后果不是"多装两个包"，而是：**服务器上没装 Pillow 时，6 个已过审 Agent 全部
无法启动**。把一个加分项的风险引到已经跑通、已经过审的主链路上，是不能接受的耦合。
所以这里用模块级 `__getattr__` 把重依赖推迟到**真正取用的那一刻**：

    from legal_acps.evidence import analyze      # 到这一行才 import forensics

这样 `import legal_acps.evidence.contracts` 的代价只有 pydantic，
`legal_acps.agents` 也因此可以在没有图像库的机器上正常加载。

（`__all__` 仍完整列出所有名字，`from ... import *` 与静态检查不受影响。）

⚠️ 本包只做**本地初检**。它的输出是「技术线索 + 不确定度 + 升级建议」，
**不是**真伪认定，也不能替代司法鉴定。所有输出里刻意不存在"真 / 假"这一对取值。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅供静态检查，运行时不执行
    from .contracts import (
        Finding,
        ImageEvidenceOutcome,
        ImageEvidenceRequest,
        ImageEvidenceResult,
        ImageForensicsReport,
        SuspicionLevel,
        UpgradePath,
    )
    from .fetch import FetchRefused, allowed_hosts, check_policy, classify_uri, load
    from .forensics import (
        DISCLAIMER,
        analyze,
        flat_fraction,
        plan_upgrades,
        sha256_of,
        sniff_media_type,
        summarize,
    )
    from .pipeline import inspect, outcome, summary_line

#: 名字 → 所在子模块。惰性导入表（新增导出只要加一行，别写成 if/elif 链）。
_LAZY: dict[str, str] = {
    # contracts：纯 pydantic（agents.py 只需要这一层的请求模型）
    "Finding": "contracts",
    "ImageEvidenceOutcome": "contracts",
    "ImageEvidenceRequest": "contracts",
    "ImageEvidenceResult": "contracts",
    "ImageForensicsReport": "contracts",
    "SuspicionLevel": "contracts",
    "UpgradePath": "contracts",
    # 检测核心：需要 numpy + Pillow
    "DISCLAIMER": "forensics",
    "analyze": "forensics",
    "flat_fraction": "forensics",
    "plan_upgrades": "forensics",
    "sha256_of": "forensics",
    "sniff_media_type": "forensics",
    "summarize": "forensics",
    # 取图：只用标准库
    "FetchRefused": "fetch",
    "allowed_hosts": "fetch",
    "check_policy": "fetch",
    "classify_uri": "fetch",
    "load": "fetch",
    # 管线
    "inspect": "pipeline",
    "outcome": "pipeline",
    "summary_line": "pipeline",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    """PEP 562：按名字惰性导入。理由见模块 docstring 里"为什么必须惰性"那一节。"""
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value      # 缓存，后续访问不再走 __getattr__
    return value


def __dir__() -> list[str]:
    return __all__
