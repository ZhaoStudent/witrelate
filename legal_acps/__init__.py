"""法律咨询多智能体互联项目（基于 ACPs + wit）。

模块划分：
    contracts.py      数据契约与评分量表（-100..100 的判定口径）+ 辩论契约
    acps_config.py    ACPs 四个地址、凭据、公开 endpoint 的配置
    llm.py            模型提供方（OpenAI 兼容接口）
    agents.py         Agent 定义（6 个角色，两级嵌套：主控 → 网络裁判 → 正/反方）
    termination.py    终止强制 Hook：下游返回后必须出交付物，不靠模型自觉
    debate.py         辩论机制：结构化终止判定 + 委托链深度熔断 / 环检测
    stub_model.py     脚本化假模型（无 API key 也能验证接线）
    check_local.py    不联网自检：编译、护栏、ACS 草稿、工具清单
    local_pipeline.py 本地串起两个专家，不需要注册就能调 prompt
    run_agent.py      注册并启动某个角色
    wt_compat.py      wt 平台兼容补丁：修掉 Discovery 的 400 FilterInvalid
    experience/       反诈体验应用（用户扮演反方；进程内编排，不注册到平台）

注意 1：这里**不能**出现叫 `platform.py` 的模块。它会让 `import platform`
（openai SDK 内部就会做）解析到本项目文件而不是标准库，引发循环导入。

注意 2：`wt_compat` 在包初始化时就 import，是**故意**的 —— 它需要在任何
`AcpsDiscoveryClient` 被调用之前生效（包括 check_local、local_pipeline 等
间接入口），见该模块 docstring 里的 400 复现记录。
"""

from . import wt_compat

__all__ = [
    "acps_config",
    "agents",
    "check_local",
    "contracts",
    "debate",
    "llm",
    "local_pipeline",
    "run_agent",
    "stub_model",
    "termination",
    "wt_compat",
]
