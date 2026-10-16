"""打印每个角色 **ACS 草稿的作者可控字段指纹** —— 用来回答一个问题：

    「我这次改的代码，会不会让已过审的角色需要重新送审？」

为什么单独做成一个工具：这个问题在项目里会被反复问到（改提示词、改 Hook、
改 pydantic 模型、加枚举值……），而每次靠"我记得 ACS 里没有这个"来回答，
迟早会答错一次。把它变成一条**可复现的命令**，结论才有依据。

口径（2026-10-01 实读 `.wit/<role>/acps/acs.draft.json` 核对）：

    作者可控（进指纹）：
        name / description / version / protocolVersion / provider /
        securitySchemes / capabilities / defaultInputModes / defaultOutputModes / skills
    平台补全（**必须排除**，否则每次编译哈希都不一样）：
        aic / endPoints / lastModifiedTime / active / certificate

⚠️ 两个容易想当然的地方：

  1. **ACS 的 `skills[]` 里没有输入 schema**（只有
     `id / name / description / tags / inputModes / outputModes / version`）。
     所以"给某个 capability 的入参模型加一个枚举值"**不进指纹** ——
     但它**改了对外工具 schema**，仍然要重启服务才生效。
  2. 反过来，**把这些枚举值写进 `@capability(description=...)` 或 `tags`**
     （例如描述里列一遍话术名），**就会进指纹**。要改描述，就得走重审。

用法：

    # 改代码前
    .venv/Scripts/python.exe -m tools.acs_fingerprint > /tmp/acs_before.txt
    # 改代码后
    .venv/Scripts/python.exe -m tools.acs_fingerprint > /tmp/acs_after.txt
    diff /tmp/acs_before.txt /tmp/acs_after.txt     # 无输出 = 指纹未变

输出格式：`<md5><TAB><role>`（LF 结尾、按角色排序），可直接 diff。
"""

from __future__ import annotations

import hashlib
import json
import sys

#: 作者可控字段 —— **唯一口径定义处**，改动前先想清楚。
AUTHOR_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "version",
    "protocolVersion",
    "provider",
    "securitySchemes",
    "capabilities",
    "defaultInputModes",
    "defaultOutputModes",
    "skills",
)


def author_view(draft: dict) -> dict:
    """只取作者可控字段，缺失的记成 None（**不要**用 default 掩盖，那样会漏掉真差异）。"""
    return {k: draft.get(k) for k in AUTHOR_FIELDS}


def fingerprint_all() -> list[tuple[str, str]]:
    """编译全部角色（离线、假模型），返回 [(role, md5), ...]。"""
    # 延迟导入：本模块只想被当作工具跑，不想在 import 期就把整个 agent 树拉起来。
    from legal_acps.agents import BUILDERS
    from legal_acps.check_local import DUMMY_MODEL, build_config

    out: list[tuple[str, str]] = []
    for role in BUILDERS:
        agent = BUILDERS[role](DUMMY_MODEL)
        compiled = agent.compile(workspace=f".wit/{role}", acps=build_config(role=role))
        draft = compiled.acs_draft or {}
        payload = json.dumps(
            author_view(draft), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        out.append((role, hashlib.md5(payload).hexdigest()))
    return sorted(out)


def main() -> int:
    try:
        sys.stdout.reconfigure(newline="\n")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    for role, digest in fingerprint_all():
        print(f"{digest}\t{role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
