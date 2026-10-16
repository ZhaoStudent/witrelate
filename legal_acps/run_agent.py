"""注册并启动某个角色。

先跑 `python -m legal_acps.check_local` 确认本地无问题，再用这个脚本联网注册。

用法（在 witrelate 目录下）：

    # 启动六个专用 Partner / 子 Leader（各自开一个终端窗口，或用 scripts/serve.sh）
    ./.venv/bin/python -m legal_acps.run_agent incitement
    ./.venv/bin/python -m legal_acps.run_agent harm
    ./.venv/bin/python -m legal_acps.run_agent fact-judge
    ./.venv/bin/python -m legal_acps.run_agent debate-pro
    ./.venv/bin/python -m legal_acps.run_agent debate-con

    # 再开一个终端，让主控跑一次完整研判
    ./.venv/bin/python -m legal_acps.run_agent leader --input "待研判的文本"

    # 只注册不启动服务（首次可用于观察审核状态）
    ./.venv/bin/python -m legal_acps.run_agent incitement --register-only

角色与端口：incitement 8443 / harm 8444 / leader 8445 /
           debate-pro 8446 / debate-con 8447 / fact-judge 8448

嵌套结构（2026-09-29 改造）：

    legal-consult-leader (8445)
       └─ fact-judge      (8448)   ← 同时是辩论子会话的 Leader
             ├─ debate-pro (8446)
             └─ debate-con (8447)

配置来源：自动读取项目根目录的 .env（无需手动 source），其中：
    ACPS_USERNAME / ACPS_PASSWORD    开发者凭据（平台支持自助注册，见 docs/）
    ACPS_PUBLIC_HOST                 你的公网地址，HTTP 内部会拼成 https://host:port
    OPENAI_MODEL / OPENAI_API_KEY    模型（必填）
    OPENAI_BASE_URL                  第三方模型服务时必填
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

from wit import RegistryPasswordCredentials, RunConfig, WitRuntime

from dotenv_lite import load_env

from . import run_ledger
from .agents import BUILDERS, build_agent
from .llm import build_model
from .acps_config import LEADER_RESEARCH_MAX_STEPS, PORTS, build_config


def _print_enrollment(role: str, result) -> bool:
    print(f"\n[{role}] 注册状态：{result.status}")
    with contextlib.suppress(Exception):
        print(result.summary())
    if result.waiting_for_review:
        print(
            "  → Registry 正在人工审核。这是平台侧动作，本地无法推进。\n"
            "     审核通过后重新运行本命令即可继续（状态会从 pending_review 继续往下走）。"
        )
        return False
    if not result.ready:
        print(
            "  → 尚未就绪，且不是审核中。请检查：\n"
            "     · ACPS_USERNAME / ACPS_PASSWORD 是否正确\n"
            "     · ACPS_PUBLIC_ENDPOINT 是否被证书 SAN 覆盖\n"
            "     · 平台服务是否可达"
        )
        return False
    print(f"  → 已就绪（active）。AIC = {_aic_of(role)}")
    return True


def _identity(role: str) -> tuple[str, str | None, str | None, str]:
    """读该角色的身份状态。

    → (status, **当前有效**的 AIC 或 None, 历史残留的 AIC 或 None, 残留来源文件名)

    ⚠️ 实测（2026-09-30）`.wit/<role>/acps/` 下**只有 `acs.draft.json`**、没有
    `acs.json` —— 早期版本只读 `acs.json`，于是**永远读不到**，恒返回一句
    「见 .wit/<role>/acps/ 下的身份文件」（一个没人会去翻的占位串）。
    AIC 实际落在 `enrollment.json` 的 `review.aic`。按可靠性依次回退。

    ⚠️ 但 `enrollment.json` 里的 AIC **不一定是当前有效的**：改过 ACS 的 Agent
    走 `_restart_local_registration` 重新注册后，`registrationId` 是新的、
    而 `review.aic` 仍是**上一轮遗留的旧值**（实测 `debate-pro` 就报出了那个
    已被删除的旧 AIC）。所以这里把"当前有效"与"历史残留"分开返回，
    由调用方决定怎么用 —— 这是"打印旧事实"那一类缺陷的根因所在。
    """
    import json
    from pathlib import Path

    acps = Path(f".wit/{role}/acps")

    state: dict = {}
    path = acps / "enrollment.json"
    if path.exists():
        with contextlib.suppress(Exception):
            state = json.loads(path.read_text(encoding="utf-8"))
    review = state.get("review") or {}
    aic = review.get("aic")
    status = str(state.get("status") or "")

    # 回退：acs.final.json（上次注册的终态）/ acs.json / acs.draft.json。
    # 注意 acs.final.json 记的是**上一次成功注册**的身份，重新注册期间它
    # 指向的 AIC 即将失效 —— 只能当"历史线索"，不能当"当前身份"。
    fallback = None
    for name in ("acs.final.json", "acs.json", "acs.draft.json"):
        path = acps / name
        if not path.exists():
            continue
        with contextlib.suppress(Exception):
            value = json.loads(path.read_text(encoding="utf-8")).get("aic")
            if value:
                fallback = (str(value), name)
                break

    if status == "active" and aic:
        return status, str(aic), None, "enrollment.json"

    stale = str(aic) if aic else (fallback[0] if fallback else None)
    origin = "enrollment.json" if aic else (fallback[1] if fallback else "")
    return status, None, stale, origin


def _active_aic(role: str) -> str | None:
    """只返回**当前有效**的 AIC；未 active（或未注册）时返回 `None`。

    给 `DebateChainGuardHooks` 用 —— 委托链的链首与"本账号前缀"都必须来自
    **真实生效**的身份。拿历史残留的 AIC 当前缀会得出错误的账号判定，
    进而误杀合法委派；所以宁可返回 None（Hook 侧 fail-open），也不能赌。
    """
    _, active, _, _ = _identity(role)
    return active


def _aic_of(role: str) -> str:
    """人读版：把该角色的身份状态打印成一句话（含"历史残留"的显式标注）。

    规则：**只有 `status == 'active'` 时才把 AIC 报成"当前身份"**；
    其余情况（pending_review / failed / draft_ready / 无状态）读到的任何 AIC
    都是历史残留，必须如实标注 —— 否则日志会把一个已失效（甚至记录已被删除的）
    AIC 报成当前身份，正是"打印旧事实"那一类缺陷。
    """
    acps = f".wit/{role}/acps"
    status, active, stale, origin = _identity(role)
    if active:
        return active
    if stale:
        return (
            f"{stale}"
            f"（⚠️ 历史残留 AIC（来自 {origin}），当前 status={status or '未知'} —— **尚未生效**）"
        )
    return f"（{acps} 下暂无 AIC —— 尚未注册或尚未通过审核）"


async def run_partner(role: str, *, register_only: bool, force_serve: bool = False) -> None:
    # self_aic 只在**已 active** 时才有值 —— 未注册时传 None 是刻意的：
    # DebateChainGuardHooks 拿不到账号前缀就不启用归属校验（fail-open），
    # 而不是拿一个占位串去误判。见 run_agent._active_aic 与 debate.py。
    agent = build_agent(role, build_model(), self_aic=_active_aic(role))
    config = build_config(role=role)
    runtime = WitRuntime(agent, workspace=f".wit/{role}", acps=config)

    print(f"[{role}] public_endpoint = {config.public_endpoint or '（未设置！Partner 必须设置）'}")
    runtime.save_acs()

    credentials = RegistryPasswordCredentials.from_env()
    result = await runtime.up_until_ready(credentials=credentials)
    ready = _print_enrollment(role, result)
    if register_only:
        print(f"[{role}] --register-only 已指定，注册推进完成后退出。")
        return
    if not ready and not force_serve:
        return
    if not ready:
        print(f"[{role}] 审核尚未通过，但 --force-serve 已指定，先尝试启动监听。")

    host = "0.0.0.0"  # 若前方有反向代理，可改为 127.0.0.1
    port = PORTS[role]
    status = await runtime.serve_direct_partner(host=host, port=port)
    print(f"[{role}] listener 已启动：{status.to_dict()}")
    print(f"[{role}] 正在等待主控派发任务…… 按 Ctrl+C 停止。")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runtime.stop_serving()
        print(f"[{role}] 已停止服务。")


def research_run_config() -> RunConfig:
    """一次性「真实研判」的 run 配置。

    ⚠️ 为什么**必须做成一个可 import 的函数**，而不是在 `run_leader` 里临时写一行
       字面量 `RunConfig(max_steps=...)`：
       `legal_acps/check_local.py` 要能断言"主控真的拿到了这份预算"，
       而断言只能落在**交付出去的那个对象**上。做成函数，自检就能把它调出来量；
       写成调用点里的一行字面量，自检只能去 grep 源码 —— 那是脆的、也不是行为断言。
       （反面教材：`reserve_steps` 只写在 guard 的构造处、没有任何断言盯着主控那一处，
        于是主控漏配、悄悄退回默认值，表现是"就差一步收不到裁定书"。）

    预算口径与实测依据见 `acps_config.LEADER_RESEARCH_MAX_STEPS` 的长注释。
    """
    return RunConfig(max_steps=LEADER_RESEARCH_MAX_STEPS)


async def run_research(runtime: WitRuntime, text: str):
    """**唯一**的一次性研判入口。

    ⚠️ 把 `runtime.run(...)` 收进这一个函数，是为了让自检能**行为断言**
       "这份预算真的传下去了"：`check_local` 塞一个只记录参数的假 runtime 进来，
       直接看它收到了什么 config。
       若把 `runtime.run(text, config=...)` 散在调用点里，自检就只能去 grep 源码 ——
       那是脆的，而且"grep 到了"并不等于"运行时真会走那条路"。
    """
    return await runtime.run(text, config=research_run_config())


async def run_research_ledgered(
    runtime: WitRuntime,
    text: str,
    *,
    label: str | None = None,
    wit_dir=None,
):
    """带**发起轮台账**的一次性研判，返回 `(run 结果, 台账记录)`。

    ⚠️ 为什么单独成一个函数（与 `research_run_config()` 同一个理由）：
       自检必须能**行为断言**"模型跑着的时候台账是绑着的" —— 因为
       `agents.write_trace_sidecar` 正是靠 `run_ledger.current_run()` 才知道
       自己属于哪一轮。若把开/绑/收散在 `run_leader` 里，自检只能去 grep 源码，
       而"grep 到了"并不等于"运行时真会走那条路"（本项目踩过：`reserve_steps`
       只写在构造处、没有任何断言盯着，于是主控漏配、悄悄退回默认值）。
       拆出来后，自检塞一个**假 runtime** 进来即可驱动整条路径：
       假 runtime 在自己的 `run()` 里当场读 `current_run()`，就能证明绑定生效。

    **先落盘再跑**：进程随后被 kill 也留得下痕迹。今天 14:56:53 那轮就是被外部
    终止的 —— 它在任务库里停在 `state=working`、`products` 为空，事后只能靠文件
    时间戳猜是谁，而猜出来的归属没有一条硬键。

    ⚠️ 开/绑/收的**实现**不在这里，在 `run_ledger.run_ledgered()` —— 因为真机探针
       （`run/e2e_leader_image`、`run/e2e_video_live`）走的也是同一套动作。
       两个调用点各写一份，迟早漂移；漂移的那一半恰好是没人盯着的那一半（探针）。
       本函数只剩"这一轮跑什么"这一句：`run_research` + `role="leader"`。
    """
    # ⚠️ 两个包装的入参形态**不一样**，这个坑 2026-10-04 在自检里踩了两次：
    #    本函数收的是**带 `.run` 的运行时对象**；`run_ledger.run_ledgered()` 收的
    #    才是**零参工厂**（那个是刻意的：协程对象一造出来就被排期，"先落盘再跑"失效）。
    #    传错的后果**不是**一句清楚的报错，而是深处那句
    #    `AttributeError: 'function' object has no attribute 'run'` —— 它会被
    #    "记账失败必须不抛"那条判据当成**记账的锅**报出来，把人指向完全无关的模块。
    #    所以这里把拒绝**前移到入口**，并给出可执行的出路。
    #    （这不是"永不可能触发"的防守：它有实测样本，见 3c 节 (l) 段。）
    if not callable(getattr(runtime, "run", None)):
        raise TypeError(
            "run_research_ledgered() 要的是**带 .run 的运行时对象**"
            f"（如 WitRuntime / 自检的 _FakeRuntime），收到的是 "
            f"{type(runtime).__name__}；"
            "若手上是一个零参工厂（回调），请直接调 "
            "run_ledger.run_ledgered(runner, text=…, label=…, role=…)。"
        )
    return await run_ledger.run_ledgered(
        lambda: run_research(runtime, text),
        text=text,
        label=label,
        role="leader",
        wit_dir=wit_dir,
    )


async def run_leader(
    *,
    text: str,
    register_only: bool,
    force_serve: bool = False,
    label: str | None = None,
) -> None:
    """Leader 有两种用法：

    * 给了 --input：起一次完整研判（调用各 Partner，汇总出结论后退出）
    * 没给 --input：作为常驻服务运行，对外提供 legal.issue_verdict 能力

    `label` 只对**一次性研判**有意义：它写进发起轮台账（`run_ledger`），用来把
    "这一轮"与平台任务库里那几行关联起来 —— 库里的 `senderId` 永远是主控自己的
    AIC，没有它就无法区分人工轮与自动复查轮（2026-10-04 实测踩到）。
    """
    agent = build_agent("leader", build_model(), self_aic=_active_aic("leader"))
    config = build_config(role="leader")
    # ⚠️ `workspace` 是 **CWD 相对**的（`.wit/leader`），而台账的默认根是**仓库相对**的
    #    （`run_ledger._default_wit_dir()` = `<仓库>/.wit`，与 `agents.WIT_DIR` 同根）。
    #    **台账必须跟 sidecar 实际读的那份会话库同根**（sidecar 读的是仓库相对那份，
    #    见 `agents.write_trace_sidecar`），所以这里**故意不传 `wit_dir`**。
    #    两者只在 "CWD == 仓库根" 时重合 —— 这条前提不靠祈祷：`check_local` 3c 节
    #    有断言盯着它，不重合就当场报失败。
    workspace = ".wit/leader"
    runtime = WitRuntime(agent, workspace=workspace, acps=config)

    runtime.save_acs()

    credentials = RegistryPasswordCredentials.from_env()
    result = await runtime.up_until_ready(credentials=credentials)
    ready = _print_enrollment("leader", result)
    if register_only:
        print("[leader] --register-only 已指定，注册推进完成后退出。")
        return
    if not ready and not force_serve:
        return

    if not text:
        # 常驻服务模式
        status = await runtime.serve_direct_partner(host="0.0.0.0", port=PORTS["leader"])
        print(f"[leader] listener 已启动：{status.to_dict()}")
        if status.to_dict().get("status") == "blocked":
            print("[leader] 注意：监听器处于 blocked，通常是审核还没通过（acps_identity_not_active）。")
        print("[leader] 正在等待外部调用…… 按 Ctrl+C 停止。")
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await runtime.stop_serving()
            print("[leader] 已停止服务。")
        return

    print("\n[leader] 开始研判……\n" + "-" * 60)
    # ⚠️ 这份 config **必须传下去**：wit 服务模式把 max_steps 钉死在 8，
    #    而 8 步只够 4 次 `acps_task(wait)`（180s），内层三级嵌套实测要 179s
    #    ⇒ 余量不到 1 秒。详见 `LEADER_RESEARCH_MAX_STEPS` 的注释。
    print(
        f"[leader] 步数预算 = {LEADER_RESEARCH_MAX_STEPS} 步"
        "（服务模式固定 8 步，改不了；本路径可调）"
    )
    # 台账**先落盘再跑**（见 `run_research_ledgered`）：进程被 kill 也要留得下痕迹。
    # `wit_dir` 不传 ⇒ 用仓库相对默认根（与 sidecar 读的会话库同根，原因见 workspace 处）。
    run, ledger_run = await run_research_ledgered(runtime, text, label=label)
    print("-" * 60)
    print(
        f"[leader] 本轮 run_id = {ledger_run.run_id}  label = {ledger_run.label}"
        f"  pid = {ledger_run.pid}  （台账 .wit/leader/runs.jsonl，只增不改）"
    )
    print(f"[leader] status = {run.status}  迭代 {run.iterations} 轮")
    print(f"[leader] 最终文本：\n{run.final_text}\n")
    if run.observations:
        print("[leader] 工具调用轨迹：")
        for obs in run.observations:
            name = obs.get("tool") or obs.get("name") or "?"
            print(f"  · {name}  ({obs.get('status', '?')}, {obs.get('duration_ms', '?')} ms)")

    # 唯一可信的判据：结构化结论有没有真的被提交。
    # 注意 "status = completed" 并不代表链路成立 —— 模型完全可以只输出一段
    # 自然语言就收尾，此时 output/ 下不会有任何文件（这是实测踩过的坑）。
    verdict_obs = [
        obs
        for obs in (run.observations or [])
        if (obs.get("tool") or obs.get("name")) in ("legal.issue_verdict", "legal_issue_verdict")
    ]
    saved = [
        (o.get("result") or {}).get("saved_to")
        for o in verdict_obs
        if o.get("status") == "success"
    ]
    print()
    if saved:
        print(f"[leader] ✅ 结构化结论已落盘：{saved[-1]}")
    else:
        print(
            "[leader] ⚠️ 没有拿到结构化结论（output/ 下不会新增文件）。\n"
            "         排查顺序：\n"
            "           1. 看上面轨迹里有没有 legal_issue_verdict 这一项；\n"
            "           2. 有但 status=error → 参数未过 schema（模型没按契约填）；\n"
            "           3. 完全没有 → 检查 Leader 是否挂了 LeaderTerminationHooks\n"
            "              （.venv/bin/python -m tools.verify_termination 可离线复现与验证）。"
        )


def refusal_text(exc: run_ledger.RunLocked) -> str:
    """把「抢不到轮次锁」渲染成人能**直接照做**的两段话（原因 + 出路）。

    ⚠️ 两段缺一不可：`refusal_reason` 与 `how_to_fix` 是**两件事**。
       只说"另一个还在跑"等于把排查成本转嫁给下一个人 —— 他连该看哪个文件、
       该杀哪个 pid 都不知道（这条口径与全项目一致，见 `RunLocked` 的 docstring）。
    """
    return (
        "[run_agent] ⛔ **拒绝开始**：同角色已有另一轮在跑（一轮 = 一把锁）。\n"
        f"  原因：{exc.refusal_reason}\n"
        f"  出路：{exc.how_to_fix}\n"
        "  （这是**预期结果，不是崩溃**：两轮共用同一个收件箱与产物目录，\n"
        "    会把「这一轮是谁发起的、产物归谁」搅成一团说不清的账。）"
    )


def run_cli(factory) -> int:
    """CLI 的统一收口：**只**把 `RunLocked` 翻成结构化拒绝，返回退出码。

    ⚠️ 为什么单拎成一个可 import 的函数：`main()` 里那一段**离线驱动不了** ——
       `run_leader` 先 `up_until_ready` 抢平台会话，之后才轮到轮次锁；
       于是"被拒绝时该怎么显示"这条**没有判据**，而**没有判据的修复等于没修**。
       抽成函数后，自检塞一个"必抛 `RunLocked` 的工厂"进来就能驱动**真实那段代码**
       （与 `run_research_ledgered` / 两个探针的 `run_probe` 同一个理由）。

    ⚠️ **只捕获 `RunLocked`**，别的异常照原样抛出去：`except Exception` 一网打尽
       会把真正的崩溃变成一句安静的退出码 —— 那正是"静默给安全结论"。
    ⚠️ `RunLocked` 是 `RuntimeError` 的子类（`run_ledger.RunLocked`）⇒ 写成
       `except RuntimeError` 会**连带**把别的运行错误一起吞掉（自检里踩过一次）。
    ⚠️ 参数是**零参工厂**（返回 awaitable），不是现成的协程对象：协程对象一造出来
       就排期，"先落盘再跑"那条时序就不成立了（与 `run_ledgered` 同款理由）。

    退出码：`0` 正常跑完 / `2` 被轮次锁拒绝（与 argparse 的用法错误同一个码，
    两者都是"这次没有开始跑"，不需要人去看栈）。
    """
    try:
        asyncio.run(factory())
    except run_ledger.RunLocked as exc:
        print(refusal_text(exc), file=sys.stderr)
        return 2
    return 0


def main() -> None:
    load_env()  # 自动读项目根目录的 .env

    parser = argparse.ArgumentParser(description="注册并启动法律咨询多智能体中的某个角色")
    parser.add_argument(
        "role",
        # ⚠️ 角色清单**必须现场派生**（原写法是手抄的表，抄到 `evidence-image`
        #    就没接着抄 ⇒ `evidence-leader` / `evidence-audio` / `evidence-video`
        #    被 argparse 直接拒掉：exit 2、屏幕上只有一行 `invalid choice`。
        #    2026-10-03 注册那三个角色时踩到的正是这一条 —— 而当时还叠了第二个错：
        #    把输出 `| grep` 掉、又用管道吃掉了退出码，于是"整批注册"看上去安静地
        #    跑完了，实际上一个都没提交。派生的好处是**不可能忘**，
        #    代价是出错时会一下子全错 —— 后者显然更好。）**
        choices=sorted(BUILDERS),
        help="要启动的角色",
    )
    parser.add_argument("--input", "-i", default="", help="leader 角色：待研判的文本")
    parser.add_argument(
        "--label",
        default=None,
        help=(
            "leader 一次性研判：发起轮标签（写进 .wit/leader/runs.jsonl）。"
            "不传则取环境变量 ACPS_RUN_LABEL，再否则记 'unlabeled' —— "
            "**故意不默认成 manual**：这条路径分辨不了调用方是谁，"
            "替它猜一个归属，正是本台账要消灭的那类缺陷"
        ),
    )
    parser.add_argument(
        "--register-only", action="store_true", help="只推进注册，不启动服务"
    )
    parser.add_argument(
        "--force-serve",
        action="store_true",
        help="审核还没通过时也尝试启动监听（用来验证服务本身能否常驻）",
    )
    args = parser.parse_args()

    if args.role == "leader":
        def factory():
            return run_leader(
                text=args.input,
                register_only=args.register_only,
                force_serve=args.force_serve,
                label=args.label,
            )
    else:
        def factory():
            return run_partner(
                args.role,
                register_only=args.register_only,
                force_serve=args.force_serve,
            )
    # ⚠️ 走 `run_cli` 而不是直接 `asyncio.run(...)`（#103）：抢不到轮次锁时
    #    "被拒绝"是**预期结果**，不该以一屏 traceback 的形态出现。
    #    这条路也是 `case_round._spawn_round` 起子进程走的那一条 ⇒
    #    它的轮次日志尾部同样受益（少了整段栈，多了两行能照做的出路）。
    raise SystemExit(run_cli(factory))


if __name__ == "__main__":
    main()
