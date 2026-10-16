"""反诈体验应用 —— 用户扮演反方，亲身体验被话术带偏的过程。

用法：

    ./.venv/bin/python -m legal_acps.experience.server --stub --port 8090

模块划分：
    roles.py    三个体验专用执行体（受理官 / 诱导方 / 裁判）
    session.py  会话编排（人在回路）
    server.py   FastAPI 服务 + 静态页面

设计红线见 `roles.py` 顶部说明 —— 尤其是「每一局都必须以揭示收尾」。
"""

from .roles import DeceptionReveal, TrapMessage, build_deceiver, build_intake, build_judge
from .session import ExperienceSession, RevealRequired

__all__ = [
    "DeceptionReveal",
    "ExperienceSession",
    "RevealRequired",
    "TrapMessage",
    "build_deceiver",
    "build_intake",
    "build_judge",
]
