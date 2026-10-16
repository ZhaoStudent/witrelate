"""极简 .env 读取器（零依赖，替代 python-dotenv）。

为什么需要它：witrelate 下面有一批用 `python -m xxx` 跑的小工具，
每次都要求你先 `set -a && . ./.env && set +a` 很容易忘，
忘了就会看到：

    RuntimeError: Missing Registry developer credentials.
    Set ACPS_USERNAME/ACPS_PASSWORD.

所以让脚本自己在启动时读 .env。

用法：
    from dotenv_lite import load_env
    load_env()          # 找到并加载 .env，返回加载的文件路径（没找到则返回 None）

规则：
  * 从当前目录向上最多找 3 层，取第一个存在的 .env
  * 支持 `KEY=value`、`export KEY=value`、`# 注释`、空行
  * 值可以用单引号或双引号包起来（引号会被剥掉）
  * **不覆盖**已经存在且非空的同名环境变量（这样临时 `export` 的优先级更高）
  * 不覆盖但也不报错——静默返回，让调用方继续跑
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_env", "parse_env_file"]


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """把 .env 解析成字典。不做任何环境变量写入。"""
    result: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            result[key] = _strip_quotes(value.strip())
    return result


def find_env_file(start: str | os.PathLike[str] | None = None, max_up: int = 3) -> Path | None:
    """从 start（默认当前目录）向上找 .env。"""
    here = Path(start or os.getcwd()).resolve()
    for _ in range(max_up + 1):
        candidate = here / ".env"
        if candidate.is_file():
            return candidate
        if here.parent == here:
            break
        here = here.parent
    return None


def load_env(
    path: str | os.PathLike[str] | None = None,
    *,
    override: bool = False,
    verbose: bool = False,
) -> Path | None:
    """加载 .env 到 os.environ。返回实际加载的文件路径，没找到则返回 None。"""
    env_file = Path(path) if path else find_env_file()
    if env_file is None or not env_file.is_file():
        return None

    for key, value in parse_env_file(env_file).items():
        current = os.environ.get(key)
        if not override and current:  # 已存在且非空 → 保留外面的值
            continue
        os.environ[key] = value

    if verbose:
        print(f"[env] 已加载 {env_file}")
    return env_file
