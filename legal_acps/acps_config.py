"""平台配置 —— 回答"四个地址从哪来"。

结论先说：**这四个地址是梧桐官方平台的公开固定值，wit 已经内置好了，
你不需要自己去找，也不需要自己搭建。**

    registry_url       = https://www.ioa.pub/registry
    registry_mtls_url  = https://www.ioa.pub:9002
    ca_url             = https://www.ioa.pub/ca-server
    discovery_url      = https://www.ioa.pub/discovery/acps-adp-v2/discover

它们分别由官方运行的四个服务提供（详见 README）。用 `AcpsConfig.ioa_pub()`
一行就能全部拿到；只有当你自建一套私有 ACPs 集群时，才需要改成
`AcpsConfig.custom(...)` 填入自己部署的地址。

真正需要你自己解决的只有一样东西：

  * public_endpoint —— 你自己这台 Agent 对外可被访问的地址，写成
    ``https://<公网IP或域名>:<端口>``，不要带路径后缀（见 build_config 注释）。

开发者凭据 ACPS_USERNAME / ACPS_PASSWORD 就是你在 https://www.ioa.pub/ 注册的
那个账号，不是另外申请的东西。证书则完全不用你操心：wit 在 enroll() 时会自动
向平台 CA（ca_url）申请并签发「客户端证书 + 服务端证书 + 信任包」三件套。
"""

from __future__ import annotations

import os

from wit import AcpsConfig, AcpsDirect, AcpsRegistration, AgentCertificateRequest, AgentProviderProfile

# 官方平台预设（打印出来供自检脚本核对，避免"我以为填对了"）
IOA_PUB_PRESET = {
    "registry_url": "https://www.ioa.pub/registry",
    "registry_mtls_url": "https://www.ioa.pub:9002",
    "ca_url": "https://www.ioa.pub/ca-server",
    "discovery_url": "https://www.ioa.pub/discovery/acps-adp-v2/discover",
}

# 梧桐平台（wt.ioa.pub）—— AIC 大赛「智能体互联」主题赛**指定的验证平台**。
# 比赛原文：「未完成平台验证的作品视为未通过比赛必要验证场景」，
# 所以在 www 上注册的 Agent 对比赛不计分，必须切到这里。
#
# 前三个地址逐字取自官方接入文档 https://wt.ioa.pub/docs.html 给的 acps-cli.toml 示例：
#     [registry]  base_url = "https://wt.ioa.pub/registry-server"
#     [ca]        base_url = "https://wt.ioa.pub/ca-server"
#     [discovery] base_url = "https://wt.ioa.pub/discovery"
# registry_mtls_url 官方文档没给（acps-cli 走的是普通 HTTPS 的 registry-server，
# 不需要 mTLS 端口）；这里按 www 侧同样的约定推定为 9002，可用环境变量覆盖。
WT_PRESET = {
    "registry_url": "https://wt.ioa.pub/registry-server",
    "registry_mtls_url": "https://wt.ioa.pub:9002",
    "ca_url": "https://wt.ioa.pub/ca-server",
    "discovery_url": "https://wt.ioa.pub/discovery/acps-adp-v2/discover",
}

BACKENDS = {"ioa_pub": IOA_PUB_PRESET, "wt": WT_PRESET}


def backend() -> str:
    """当前后端：'ioa_pub'（默认，www.ioa.pub）或 'wt'（梧桐，比赛指定）。

    由环境变量 ACPS_BACKEND 决定。两套平台是**独立的账号库与独立的 Agent 索引**，
    切换后端 = 换一个注册局，wit 会因配置指纹变化而重新走一遍注册+证书流程。
    """
    name = (os.environ.get("ACPS_BACKEND") or "ioa_pub").strip().lower()
    if name not in BACKENDS:
        raise RuntimeError(
            f"ACPS_BACKEND 只能是 {sorted(BACKENDS)} 之一，当前为 {name!r}"
        )
    return name

# 每个 Agent 在本机监听的端口，错开避免冲突
#
# 8443–8454 是安全组已放行的端口（腾讯云控制台 → 轻量应用服务器 → 防火墙）。
# ⚠️ 已公布出去的端口不要改：寄存器里的 endpoint 与平台侧探测记录都指向它，
#    换号等于把已有审核结果作废。
PORTS = {
    "incitement": 8443,   # Partner A：煽动性识别 + 真伪判定
    "harm": 8444,         # Partner B：社会危害评估
    "leader": 8445,       # 法前哨（外层 Leader；原名“法律咨询主控”）
    "debate-pro": 8446,   # 正方（裁判的 Partner）
    "debate-con": 8447,   # 反方（裁判的 Partner）
    # 2026-09-29 新增：网络裁判。它同时是「法律咨询主控的 Partner」与
    # 「辩论子会话的 Leader」，构成两级嵌套。此前该职责由 debate-pro 兼任。
    "fact-judge": 8448,   # 网络裁判（内层 Leader）
    # 2026-09-30 新增：图像鉴伪（证据鉴证的第一个模态）。
    # 定位：**纯 Partner，不发起委派** —— 它由「证据鉴证 Leader」调用，
    # 也可以在单机自测时被主控直接调（见 docs/aicomp/evidence-image-方案分析.md）。
    "evidence-image": 8449,   # 图像鉴伪（本地初检 + 复检路径建议）
    # 2026-10-02 新增：证据鉴证 Leader（证据链的内层 Leader）。
    # 定位：**S1 Partner ＋ S2 Leader**，与 `fact-judge` 同形 ——
    #   法律咨询主控 → 证据鉴证 Leader → 图像鉴伪（链深 1→2→3）。
    # 为什么不让主控直接调 evidence-image（原方案 B 形态）：服务模式下
    #   `max_steps=8` 是钉死的，主控自己扛"每个模态一次发现 + 一次派发"，
    #   模态一多就装不下；多一层把这段编排挪进它自己的预算里，且以后加
    #   audio / video 时主控与其它调用方**零改动**。
    # ⚠️ 链深 3 = `MAX_CHAIN_DEPTH` 上限，再往下挂第三级会直接撞墙。
    "evidence-leader": 8450,  # 证据鉴证 Leader（汇总层）
    # 2026-10-02 新增：音频鉴伪（证据鉴证的第二个模态）。
    # 定位与 `evidence-image` **完全同形**：纯 Partner、不发起委派、叶子角色。
    # ⚠️ **正是在这里验证了 §"为什么不让主控直接调 evidence-image" 那条设计**：
    #   加音频模态时，主控、证据鉴证 Leader 的编排结构、乃至主控的端口表
    #   都**一行没改** —— 因为模态编排本来就落在 evidence-leader 自己的预算里。
    #   若当初按方案 B（主控直接调模态角色），这次就得同步改主控提示词 + 终止
    #   Hook 的 expected_partners + 它的 max_steps 预算，而预算只有 8 步。
    "evidence-audio": 8451,  # 音频鉴伪（容器/编码参数层 + 波形域九项）
    # 2026-10-02 新增：视频鉴伪（证据鉴证的第三个模态）。
    # 定位与 `evidence-image` / `evidence-audio` **完全同形**：纯 Partner、
    #   不发起委派、叶子角色；**与它们平级**地挂在 evidence-leader 下面。
    # ⚠️ 链深已到顶：主控(1) → evidence-leader(2) → 本角色(3) == MAX_CHAIN_DEPTH。
    #    **不要再往本角色下面挂第四级**（如"抽帧后交给图像鉴伪"）——
    #    那会撞上限，必须先改 `MAX_CHAIN_DEPTH` 并重新评估环检测。
    #    视频链的帧层因此是**进程内复用**图像链代码（`video/frames.py` 直接调
    #    `evidence.forensics.analyze()`），而不是跨 Agent 派发。
    "evidence-video": 8452,  # 视频鉴伪（容器层七项 + 可零解码取到的帧）
    # 2026-10-07 新增：法律咨询顾问（纠纷初期建议 + 已核验法条引用）。
    # 叶子 Partner、挂主控下（链深 1，与 fact-judge 同层），不发起委派。
    "law-advisor": 8453,  # 法律咨询顾问（八部法律 · 防幻觉硬校验）
    # 2026-10-11 新增：线索受理官（「群众来信」→ 待核查队列）。
    # 叶子 Partner、与辩手同形；设计依据 = 线索侦察文档第 1 步（B2 队列式，
    # 零出网零授权）。⚠️ 8454 是安全组注释声称放行的最后一个端口——
    # 「注释不是证据」，部署后必须从外网实测连通（curl: 超时=被墙 / SSL 错=通）。
    "lead-intake": 8454,  # 线索受理官（防污染闸门：claim 不许自带定性/处罚）
}


# ── 软废弃的角色（2026-10-02）──────────────────────────────────────────────
#
# 用户决定：`incitement` / `harm` 这两个 Agent 是早期"先跑通一遍全流程"的产物，
# 在后续设计里**不再参与主控链路**，暂时废弃 —— 但是**废弃，不是删除**：
# 等将来有把它们用起来的方案，要能低成本重新启用。
#
# "软废弃"具体指三件事：
#   · `BUILDERS` 保留 ⇒ 代码仍可构建，`run_agent incitement` 随时能单独起；
#   · **平台注册仍在**（2026-10-20 分页复查：`tools.list_agents` 共 25 条，
#     二者都在第 2 页，`APPROVED/active`；2026-10-13 的“注册已不在”是
#     旧工具只翻第一页造成的误判）⇒ 重新启用**不必重走注册**，但 Discovery
#     按名字召不回（服务停着/不进索引），安全语义（不部署、不进链路、
#     discovery 召不回）仍成立；主控停用声明仍是纵深防御。
#   · 但**不部署、不参与统一启停、不进主控链路** —— 主控改为自己完成这三项判定。
#
# ⚠️ 这是**单一事实源**：`scripts/serve.sh` / `scripts/systemd-install.sh` 的部署清单
#    与 `legal_acps/check_local.py` 的 3b 自检都从它派生。
#    别再各写一份 —— "同一事实散在多处"是本项目反复踩过的坑（曾散在 4 处）。
SOFT_RETIRED: tuple[str, ...] = ("incitement", "harm")

#: 实际部署的角色 = 全部角色 − 软废弃。统一启停 / 验收检查只作用于它。
DEPLOY_ROLES: tuple[str, ...] = tuple(r for r in PORTS if r not in SOFT_RETIRED)


# ---------------------------------------------------------------------------
# 一次「真实研判」的步数预算
# ---------------------------------------------------------------------------
#
# ⚠️ wit 的**服务模式**把 `max_steps` 钉死在 8（`RunConfig` 默认值就是 8；
#    `WitAgent` 上没有任何预算字段，`serve*()` 也不接受 config）—— 这一条改不了。
#    但**一次性 CLI 路径**可以：`WitRuntime.run(input, config=RunConfig(...))`
#    自带 config 入口，本常量就是喂给它的（见 `run_agent.research_run_config`）。
#
# 为什么必须有这个入口（2026-10-04 真机实测）：
#   主控走「事实核查」分支时要 `discover → 派发 → 反复 acps_task(wait) → 出结论`，
#   而**单次 wait 的上限是 SDK 里写死的** `DEFAULT_WAIT_TIMEOUT_SECONDS = 45.0`
#   （`AcpsConfig` 根本没有 `wait_timeout_seconds` 字段 ⇒ 无旋钮可调）。
#   于是：**主控的等待窗口 ≈ 等待步数 × 45s**，步数预算直接决定了它等不等得到下游。
#
#     · 8 步：`discover + 派发 + 4×wait + 交结论 + 收尾` = 8 ⇒ 窗口 180s
#     · 内层三级嵌套（主控 → 裁判 → 正/反 → 裁定）实测耗时 **179s**
#     ⇒ **余量约 0.5 秒**。那一次收到裁定书纯属侥幸 —— 内层只要慢一点点就再次翻车。
#       实测时间线：裁判派正方 27s → 正方立论 28s → 转派反方 13s → 反方反驳 50s
#                   → 裁判写裁定并交付 60s（= 179s）
#
#   14 步 ≈ 10 次 wait ≈ **450s 窗口**，相对 179s 是 **2.5×** 余量。
#   注意：预算变大**不会**让正常一轮变慢 —— 下游一交回主控立刻收工，
#   只有下游真的卡住时才会多等，而那正是我们想要的行为（宁可多等，也不要
#   交一份"信息不完整"的结论）。
#
# ⚠️ "能调大"这条**不是读 stub 得出的**：`run/probe_max_steps.py` 用"停不下来的
#    假模型"做了行为证明 —— 默认给 8 轮、传 14 就得 14 轮。读签名不算证据。
LEADER_RESEARCH_MAX_STEPS: int = 14


def endpoint_for(role: str, host: str, *, scheme: str = "https") -> str:
    """把「公网主机 + 角色」拼成 public_endpoint。

    host 可以是 IP（150.109.154.16）也可以是域名（acps-legal.example.com）。
    平台 CA 签发证书时，SAN 里放的正是这里的主机名/IP，
    ⚠️ 缺登记时**刻意抛一条带角色名的 `RuntimeError`，而不是让 `PORTS[role]` 裸抛
    `KeyError`**（2026-10-05 实测）：`KeyError: 'evidence-audio'` 只说明"某个字典
    少一个键"，读的人还得自己去查是哪个表、该补什么；而补端口只需要知道**角色名**。
    它同时也让调用方**无法把两种失败混为一谈** —— 后者会让人以为是角色名拼错了。
    """
    if role not in PORTS:
        raise RuntimeError(
            f"角色 {role!r} 没有登记端口 —— `acps_config.PORTS` 里缺这一项，"
            f"补上即可（已登记：{sorted(PORTS)}）"
        )
    return f"{scheme}://{host}:{PORTS[role]}"


def provider_profile() -> AgentProviderProfile:
    """ACS 里的机构信息。注册时会作为 provider 字段提交。

    这些值会写进你对外公开的能力名片（ACS），请如实填写；
    不同 Agent 可共用同一 provider（同一个机构/课题组）。
    """
    return AgentProviderProfile(
        organization=os.environ.get("ACPS_ORG", "BUPT-LegalAgent-Demo"),
        country_code=os.environ.get("ACPS_COUNTRY", "CN"),
        name=os.environ.get("ACPS_PROVIDER_NAME", "法律咨询多智能体课题组"),
        url=os.environ.get("ACPS_PROVIDER_URL") or None,
        email=os.environ.get("ACPS_PROVIDER_EMAIL") or None,
    )


def build_config(
    *,
    role: str,
    public_endpoint: str | None = None,
    modes: tuple[str, ...] = ("rpc",),
) -> AcpsConfig:
    """构造某个角色的 AcpsConfig。

    role             : 'incitement' | 'harm' | 'leader' |
                       'fact-judge' | 'debate-pro' | 'debate-con'
    public_endpoint  : 本机对外地址。Partner 必须填（Leader 要连进来）；
                       Leader 用 rpc 模式可以不填。
    """
    # public_endpoint 只写到「主机:端口」为止，**不要带路径**。
    # wit 的直连监听器自己会挂在这些路径上（读自 wit.acps.direct 的字节码）：
    #     /rpc              JSON-RPC 任务通道
    #     /notification/start|get|set|delete
    #     /stream
    #     /health
    # 注册时 ACS 里公布的 endpoint 会是 f"{public_endpoint}/rpc"。
    # 若这里多写一个路径后缀，对方会去请求一个不存在的地址。
    endpoint = public_endpoint or os.environ.get("ACPS_PUBLIC_ENDPOINT")
    if not endpoint:
        # 没显式给地址时，用 ACPS_PUBLIC_HOST + 该角色的端口拼出来
        host = os.environ.get("ACPS_PUBLIC_HOST")
        if host:
            endpoint = endpoint_for(role, host)
    endpoint = endpoint.rstrip("/") if endpoint else None

    common: dict[str, object] = dict(
        provider=provider_profile(),
        certificate=AgentCertificateRequest(requested_validity=365),
        registration=AcpsRegistration.integrated(),
        request_timeout_seconds=90,
        public_endpoint=endpoint,
    )

    # 自建集群时才走这里：设置 ACPS_USE_CUSTOM=1 并给出四个地址
    if os.environ.get("ACPS_USE_CUSTOM") == "1":
        return AcpsConfig.custom(
            registry_url=_need("ACPS_REGISTRY_URL"),
            registry_mtls_url=os.environ.get("ACPS_REGISTRY_MTLS_URL") or None,
            ca_url=_need("ACPS_CA_URL"),
            discovery_url=_need("ACPS_DISCOVERY_URL"),
            strict_interop=True,
            **common,  # type: ignore[arg-type]
        )

    # ACPS_BACKEND=wt → 梧桐平台（比赛指定）。用 custom() 显式传四个地址。
    # 想禁用 mTLS 注册端点（若该端口在 wt 侧未开放），把
    # ACPS_REGISTRY_MTLS_URL 显式设为空串即可。
    if backend() == "wt":
        preset = dict(WT_PRESET)
        override = os.environ.get("ACPS_REGISTRY_MTLS_URL")
        if override is not None:
            preset["registry_mtls_url"] = override or None
        return AcpsConfig.custom(strict_interop=True, **preset, **common)  # type: ignore[arg-type]

    # ioa_pub() 是官方平台预设：四个地址已内置，且强制 strict_interop=True，
    # 因此它不接受 strict_interop 参数（传了会 TypeError）。
    return AcpsConfig.ioa_pub(**common)  # type: ignore[arg-type]


def _need(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"ACPS_USE_CUSTOM=1 时必填环境变量 {name}（自建 ACPs 集群的地址）"
        )
    return value


def describe(role: str) -> str:
    """给自检脚本用：把当前生效的配置打印成人能读的形式。"""
    cfg = build_config(role=role)
    lines = [
        f"角色              : {role}",
        f"平台后端          : {backend()}（ACPS_BACKEND；wt = 比赛指定的梧桐平台）",
        f"本机监听端口      : {PORTS.get(role, '未定义')}",
        f"registry_url      : {cfg.registry_url}",
        f"registry_mtls_url : {cfg.registry_mtls_url}",
        f"ca_url            : {cfg.ca_url}",
        f"discovery_url     : {cfg.discovery_url}",
        f"public_endpoint   : {cfg.public_endpoint or '（未设置：Leader 用 rpc 模式可不设）'}",
        f"注册形态          : {cfg.registration.kind}",
        f"strict_interop    : {cfg.strict_interop}",
        f"证书密钥算法      : {cfg.certificate_key_algorithm}",
    ]
    return "\n".join(lines)


__all__ = [
    "IOA_PUB_PRESET",
    "WT_PRESET",
    "BACKENDS",
    "backend",
    "PORTS",
    "SOFT_RETIRED",
    "DEPLOY_ROLES",
    "AcpsDirect",
    "build_config",
    "describe",
    "provider_profile",
]
