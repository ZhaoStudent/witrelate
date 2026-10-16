"""一眼看清所有角色的注册/审核状态。

用法（在 witrelate 目录下）：
    ./.venv/bin/python -m tools.status

它会依次向 Registry 查询每个角色的当前状态，并给出下一步该做什么。
已经提交过审核的角色不会重复提交（wit 会识别已有 registration）。

凭据来源：自动读取项目根目录的 .env，无需手动 source。
"""

from __future__ import annotations

import asyncio
import os
import sys

from wit import RegistryPasswordCredentials, WitRuntime

from dotenv_lite import load_env
from legal_acps.acps_config import PORTS, build_config
from legal_acps.agents import BUILDERS, build_agent
from legal_acps.llm import build_model

# 角色清单直接取自 Agent 注册表，避免这里漏加新角色
ROLES = tuple(BUILDERS)

# 状态 → 人话解释。
#
# ⚠️ 键必须与 wit 的 `EnrollmentStatus` **逐字一致**。实测枚举（2026-09-30，从
#    wit.acps.provisioner 直接读出，不是猜的）一共 15 个：
#        needs_login / draft_ready / submitted / pending_review / approved /
#        entity_registered / issuing_certificates / finalizing / active /
#        serving / blocked / unconfigured / rejected / failed / needs_renewal
#    早期这里只写了 6 个，其中 `needs_agent_registration` 还是个**不存在的键**
#    （死键，永不命中）；而真正需要人介入的 blocked / rejected / failed
#    全都落到兜底分支，打印"未知状态 —— 见下方 summary"，
#    偏偏本脚本**根本没有 summary 段** —— 等于对最需要处理的情况给了误导提示。
MEANING: dict[str, tuple[str, str]] = {
    "needs_login": ("凭据不可用", "检查 .env 里的 ACPS_USERNAME / ACPS_PASSWORD"),
    "draft_ready": ("草稿就绪待提交", "运行 run_agent <角色> --register-only 提交审核"),
    "submitted": ("已提交", "稍等片刻再查，平台正在接收"),
    "pending_review": ("等平台人工审核", "无法本地推进，等平台批准后重跑本命令"),
    "approved": ("平台已批准", "继续重跑本命令，完成实体注册与证书签发"),
    "entity_registered": ("实体已注册", "继续重跑本命令，完成证书签发"),
    "issuing_certificates": ("正在签发证书", "稍等片刻再查本命令"),
    "finalizing": ("正在收尾落盘", "稍等片刻再查本命令"),
    "active": ("已激活", "可以直接启动服务：run_agent <角色>"),
    "serving": ("已在线服务中", "无需操作；要停止用 scripts/serve.sh stop"),
    "blocked": ("⚠️ 平台侧阻塞", "审核未过 / endpoint 不可达 / 身份未激活，看该角色日志"),
    "unconfigured": ("配置缺失", "检查 .env 的 ACPS_* 与 ACPS_PUBLIC_HOST / ACPS_PUBLIC_ENDPOINT"),
    "rejected": ("❌ 审核被拒", "按平台反馈修改后重新提交（改 ACS 字段会换注册指纹）"),
    "failed": ("❌ 注册失败", "看 run_agent <角色> --register-only 的详细输出；多为凭据/网络/证书问题"),
    "needs_renewal": ("证书待续期", "重跑本命令会自动续期"),
}

#: `up_until_ready` 抛异常时本脚本**自己合成**的状态（不属于 wit 的 EnrollmentStatus）。
#: 早期它是散在代码里的字面量 "ERROR"，没有任何断言保证它真的与写入处一致 ——
#: 一旦有人把写入处改掉，这个键就变成永不命中的死键，而"异常也要被看见"这条就悄悄失效了。
SYNTHETIC_ERROR = "ERROR"

#: 必须让人看见的状态 —— 它们不会自愈。早期版本只汇总 pending/active，
#: 最该被看见的失败类状态一条都不提，看板"看着挺正常"。
TROUBLED_STATES = frozenset(
    {"blocked", "rejected", "failed", "unconfigured", "needs_login", SYNTHETIC_ERROR}
)

#: 其余状态：进行中 / 已正常 / 会自愈 —— 不需要人工介入。
#:
#: ⚠️ 它与 TROUBLED_STATES **互补且互斥**，由 `check_local` 3b ⑤ 断言：
#:        TROUBLED ∪ BENIGN == EnrollmentStatus ∪ {SYNTHETIC_ERROR}
#:    于是 wit 一旦**新增状态**，就必须到这里显式归类，否则自检报失败 ——
#:    不会再出现"新状态悄悄落进'无需处理'"（与 MEANING 表的双向差集同源）。
BENIGN_STATES = frozenset(
    {
        "draft_ready",
        "submitted",
        "pending_review",
        "approved",
        "entity_registered",
        "issuing_certificates",
        "finalizing",
        "active",
        "serving",
        "needs_renewal",
    }
)


async def main() -> None:
    env_file = load_env()
    print(f"[env] {env_file or '（未找到 .env，改用现有环境变量）'}")

    if not (os.environ.get("ACPS_USERNAME") and os.environ.get("ACPS_PASSWORD")):
        sys.exit(
            "缺少 ACPS_USERNAME / ACPS_PASSWORD。\n"
            "  预期它们在 ~/witrelate/.env 里。检查这一行：\n"
            "      grep -c '^ACPS_USERNAME=.' .env      # 输出 1 表示已填\n"
            "  若 .env 不在当前目录，请先 cd ~/witrelate。"
        )

    creds = RegistryPasswordCredentials.from_env()
    rows: list[tuple[str, str, str]] = []

    for role in ROLES:
        agent = build_agent(role, build_model())
        config = build_config(role=role)
        runtime = WitRuntime(agent, workspace=f".wit/{role}", acps=config)
        try:
            result = await runtime.up_until_ready(credentials=creds)
            status = str(result.status)
            # 兜底文案**不能**指向本脚本没有的东西（早期写"见下方 summary"，而这里没有 summary 段）
            label, advice = MEANING.get(
                status,
                (
                    "未知状态（本工具未收录）",
                    "请把下面的原始状态反馈给维护者；先跑 run_agent <角色> --register-only 看细节",
                ),
            )
            rows.append((role, status, f"{label} —— {advice}"))
        except Exception as exc:  # noqa: BLE001
            # 用常量而不是字面量 "ERROR" —— 见 SYNTHETIC_ERROR 的说明
            rows.append((role, SYNTHETIC_ERROR, f"{type(exc).__name__}: {exc}"))

    print("=" * 76)
    print(f"{'角色':<12}{'端口':<7}{'状态':<18}说明")
    print("-" * 76)
    for role, status, note in rows:
        print(f"{role:<12}{PORTS.get(role, '-'):<7}{status:<18}{note}")
    print("=" * 76)

    pending = [r for r, s, _ in rows if s in ("submitted", "pending_review")]
    active = [r for r, s, _ in rows if s in ("active", "serving")]
    troubled = [(r, s) for r, s, _ in rows if s in TROUBLED_STATES]
    if pending:
        print(f"\n⏳ 等待平台审核：{', '.join(pending)}")
        print("   审核是平台侧人工动作。通过后重跑本命令，状态会变成 active。")
    if active:
        print(f"\n✅ 已激活可直接上线：{', '.join(active)}")
        print("   启动服务：bash scripts/serve.sh start")
    if troubled:
        print("\n⚠️ 需要人工处理（这些状态不会自愈）：")
        for role, st in troubled:
            print(f"   {role:<12}{st}")


if __name__ == "__main__":
    asyncio.run(main())
