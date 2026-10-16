# 法律咨询多智能体互联项目

1 个主控 Agent（Leader）+ 2 个专用 Agent（Partner），最终扩到 5 个专用。
基于 ACPs 协议与 wit 框架，判定对象是「煽动性言论 → 真伪程度 → 社会危害性（-100..100）」。

---

## 一、先回答你的三个疑问

### 1. 四个地址是什么、干什么用

它们分别是**四个由平台方运行的外部服务**的入口。你的 Agent 是客户端，不是服务端。

| 地址 | 对应服务 | 在你的项目里干什么 | 你会直接调用吗 |
|---|---|---|---|
| `registry_url` | Registry（注册局） | 提交 Agent 注册申请、查审核状态、领取分配到的 AIC、取回权威版 ACS | 不直接调，`up_until_ready()` 内部用 |
| `registry_mtls_url` | Registry 的 mTLS 端口 | 和上面同一个服务，只是走双向 TLS 通道；从一个已激活的 ontology 身份派生新实体时用 | 不直接调 |
| `ca_url` | CA（证书颁发） | 审核通过后签发 clientAuth / serverAuth 两套证书 + 下载 trust bundle。**没有它就拿不到证书，也就无法和其他 Agent 建立 mTLS 信任** | 不直接调 |
| `discovery_url` | Discovery（发现服务） | 按关键词查"网上有哪些 Agent 能干活"，返回它们的 ACS。你的 Leader 靠它找到那两个 Partner | 不直接调，`acps_discover` 内部用 |

一句话：**Registry 发身份，CA 发证书，Discovery 帮你找人。** 三者构成 ACPs 的"信任底座"。

### 2. 怎么得到它们 —— 答案是：不用找

这四个地址是**官方平台的公开固定值**，wit 已经硬编码在 `AcpsConfig.ioa_pub()` 里：

```
registry_url       https://www.ioa.pub/registry
registry_mtls_url  https://www.ioa.pub:9002
ca_url             https://www.ioa.pub/ca-server
discovery_url      https://www.ioa.pub/discovery/acps-adp-v2/discover
```

跑 `python -m legal_acps.check_local` 的第 5 节就能看到它们被打印出来（本文件末尾也贴了一份）。
所以**你不需要部署任何服务，也不需要申请这四个地址** —— 它们跟你手里那两个 whl 一样，都是官方提供的。

只有在一种情况下你才需要关心"自建"：你要搭一套**私有 ACPs 集群**（比如学校内网隔离）。
那时才用 `AcpsConfig.custom(...)` 换成自己部署的地址。本项目的 `acps_config.py` 已预留：
设 `ACPS_USE_CUSTOM=1` 并给出 `ACPS_REGISTRY_URL` / `ACPS_CA_URL` / `ACPS_DISCOVERY_URL` 即可。

### 3. 真正需要你自己搞定的只有两样

**(a) 开发者凭据 `ACPS_USERNAME` / `ACPS_PASSWORD`** —— ✅ 已解决

这相当于你在 ACPs 网络里的"开发者账号"，注册 Agent 时用来证明"这个 Agent 是我提交的"。
`RegistryPasswordCredentials.from_env()` 只读这两个环境变量。

**实测更正（2026-09-22）**：平台的 OpenAPI 规范是公开可读的
（<https://www.ioa.pub/registry/openapi.json>），里面明确列着 `POST /api/v1/auth/register`——
**账号可以自助注册**，不需要短信验证码，也不需要向平台方申请：

```bash
curl -X POST https://www.ioa.pub/registry/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"你的用户名","password":"你的密码"}'
```

新账号默认带 `CLIENT` 角色，**足以注册 Agent**。本项目已用 `tools/bootstrap_account.py`
注册了 `legal-acps-demo` 并写入 `.env`（凭据同时落在服务器 `ACCOUNT.txt`，权限 600）。

三个容易踩的坑：

- **登录接口是 OAuth2 表单**（`application/x-www-form-urlencoded`），**不是 JSON**。
  用 JSON 提交会返回 422「username/password missing」，极易误判成"账号不存在"。
- **账号必须存在于 ACPs 注册库**。梧桐网页账号若不在这个库里，`login()` 会一直返回
  `needs_login`。判断用户名是否存在的可靠办法：**故意用一个错密码去登录**，
  回 `USER_NOT_FOUND` 说明用户名不存在，回密码错误说明用户名是对的。
- **wit 没有封装账号管理**。注册、查实名状态、撤销/删除 Agent 这类动作走本项目的
  `tools/portal.py`（直接调原始 HTTP 接口）。

另外**实名认证不是必需的**：`POST /api/v1/verification/identity` 确实存在（wit 未封装），
但实测在从未提交（`GET` 返回 `null`）的情况下**不阻塞 Agent 注册**。

**(b) `ACPS_PUBLIC_ENDPOINT` —— 这个才是真门槛**

含义：你的 Agent **自己对外可被访问的地址**。因为 ACPs 是点对点协作网络，
Leader 要主动连到 Partner 上，所以 **Partner 必须是公网可达的**。

这里有三层概念，初学者最容易混：

| 概念 | 含义 | 举例 |
|---|---|---|
| 本地 bind | `serve_direct_partner(host=..., port=...)` 实际绑定的本机 socket | `0.0.0.0:8443` |
| `public_endpoint` | 写进 ACS、告诉全网"来这个地址找我"的公开基地址 | `https://your-domain:8443/acps/aip/v1` |
| DNS / 反代 / 端口映射 | 把公开地址转发到你的 listener | nginx、frp、云厂商负载均衡 |

关键约束：**server 证书的 DNS/IP SAN 必须覆盖 `public_endpoint` 的主机名**，
而且不能是 `0.0.0.0` 这种 bind 地址。

对学生的现实选项（按推荐排序）：

1. **一台便宜的云服务器**（最省心）。公网 IP 固定，把 `public_endpoint` 设成 `https://<公网IP>:端口`，
   并在安全组放行端口。缺点：要花钱，IP 变了要重新注册。
2. **内网穿透**（frp / ngrok / cpolar 之类）。零成本起步，但免费版域名会变，
   而**域名一变就得重新走注册**（因为证书 SAN 对不上），所以别用免费随机域名做长期开发。
3. **学校实验室有公网出口的机器**。最理想，前提是你能拿到固定地址。

⚠️ 注意：证书 SAN 覆盖的是**你注册时申报的那个主机名**。
所以**先把公网地址定下来，再去注册**，否则每换一次地址就要重走一遍注册流程。

---

## 二、文件结构

```
legal_acps/
├── contracts.py       数据契约 + 评分量表（-100..100 的判定口径，核心）
├── acps_config.py     四个地址、凭据、public_endpoint 的配置工厂
├── llm.py             模型提供方（OpenAI 兼容，通义/DeepSeek/Kimi 都能用）
├── agents.py          1 个 Leader + 2 个 Partner 的 Agent 定义
├── stub_model.py      脚本化假模型（无 key 也能跑通全流程）
├── check_local.py     离线自检：契约、编译、协议、ACS 草稿
├── local_pipeline.py  本地串起两个专家，不需要注册就能调 prompt
└── run_agent.py       注册并启动某个角色
```

### 三个 Agent 的分工

| 角色 | agent id | 对外技能 | 干什么 |
|---|---|---|---|
| Partner A | `incitement-detector` | `speech.detect_incitement`<br>`facts.verify_claims` | 识别煽动性（类型 + 强度 + 针对对象 + 原文证据），并拆解事实性断言逐条判真伪 |
| Partner B | `harm-assessor` | `harm.assess_social_impact` | 六维分解 + 给出 `harm_index`（-100..100）+ 法条提示 + 反方视角 |
| Leader | `legal-consult-leader` | `legal.issue_verdict` | 发现并委派两个 Partner，汇总成最终结论并落盘 |

**为什么这么切**：A 的两件事共用同一输入（原始文本），都属于"话语本身的分析"；
B 的输入是 A 的输出，属于"后果评价"。边界最干净，后面扩到 5 个时 Leader 不用改。

**一个关键机制**：`@capability` 修饰的函数**不是计算逻辑，是"提交结论的通道"**。
真正的推理由模型完成，模型分析完再调这个工具把结构化结论交上来。
这样 Leader 拿到的是可校验的 JSON，而不是需要二次解析的自然语言。

---

## 三、操作步骤

### 第 0 步：环境自检（不需要任何凭据）

```bash
cd "C:/Users/Zhao5/Desktop/Mycode/witrelate"
./.venv/Scripts/python.exe -m legal_acps.check_local
```

全绿说明本地层面就绪。这一步之后，能不能互联只取决于凭据和 public_endpoint。

### 第 1 步：用假模型跑通管道（不需要 API key）

```bash
./.venv/Scripts/python.exe -m legal_acps.local_pipeline --stub --verbose
```

会看到两个 Partner 依次产出结构化结论，最后汇总出 `harm_index`。
这证明**接线是对的**（工具名、schema、观测数据都能对上）。

### 第 2 步：换成真实模型调业务逻辑（需要 API key，仍然不需要 ACPs 凭据）

```bash
export OPENAI_MODEL="deepseek-chat"
export OPENAI_API_KEY="sk-..."
export OPENAI_BASE_URL="https://api.deepseek.com/v1"

./.venv/Scripts/python.exe -m legal_acps.local_pipeline --text "你要研判的文本"
```

**这一步是整个项目最该反复打磨的地方**：改 `agents.py` 里的 instructions、
改 `contracts.py` 里的量表锚点，然后重跑，看结论是否稳定、是否符合预期。

### 第 3 步：注册并启动三个 Agent（需要凭据 + 公网地址）

开三个终端窗口（Git Bash / PowerShell 都可以）：

```bash
# 终端 1 —— Partner A
export ACPS_USERNAME="你的账号"
export ACPS_PASSWORD="你的密码"
export ACPS_PUBLIC_ENDPOINT="https://你的公网地址:8443"
export OPENAI_MODEL="deepseek-chat"; export OPENAI_API_KEY="sk-..."
./.venv/Scripts/python.exe -m legal_acps.run_agent incitement

# 终端 2 —— Partner B（注意端口换成 8444，public_endpoint 也要带 8444）
export ACPS_PUBLIC_ENDPOINT="https://你的公网地址:8444"
./.venv/Scripts/python.exe -m legal_acps.run_agent harm

# 终端 3 —— Leader（rpc 模式下 Leader 不需要被回调，可省 public_endpoint）
./.venv/Scripts/python.exe -m legal_acps.run_agent leader --input "待研判的文本"
```

首次跑会停在 `pending_review`，这是**注册局在人工审核**，本地无法推进。
审核通过后重新运行同一条命令即可，状态机从 `pending_review` 继续走到
`approved → issuing_certificates → finalizing → active → serving`。

想先看审核状态、不启动服务，加 `--register-only`。

### 第 4 步：验证

Leader 跑完会打印工具调用轨迹，并在 `output/` 下生成
`verdict-<时间戳>.json`，里面是符合 `FinalVerdict` 契约的最终结论。

---

## 四、扩到 5 个专用 Agent 的路线

现在是 2 个，最终 5 个。扩展时**只需要动两处**，Leader 不用改：

1. 把 Partner A 拆开：
   - `incitement-detector` 只做煽动性识别
   - `fact-checker` 只做事实核查（可以挂上检索工具）
2. 把 Partner B 拆开并新增：
   - `harm-assessor` 保留综合打分
   - `statute-matcher` ← 新增：匹配具体的中国法律条文与量刑档次
   - `case-retriever` ← 新增：检索类案与裁判要旨

5 个 Partner 各占一个端口（8443–8447），`acps_config.py` 的 `PORTS` 加条目即可。
`run_agent.py <role>` 会自动认出新角色。

**Leader 为什么不用改**：它用的是 `acps_discover` 按关键词找 Partner，
而不是硬编码 AIC。新 Partner 只要注册进 Discovery，
Leader 下次 discover 就能看到。

---

## 五、两个必须注意的点

### 1. `harm_index` 这个量表的方向要小心

你的口径是「越高越应反对，越低越应支持」。这个刻度是**双极**的，
把「社会危害性」和「是否该支持」两件事压到了一个数上。它有一个隐患：
"支持/反对"是**价值判断**，而"社会危害性"是**事实评价**，混在一起会让评分标准漂移。

建议（本实现已经这样做了）：把 `expression_legitimacy` 单独作为一维列出来，
并强制要求模型填写 `counter_view`（反方视角）。这样最终那个数字有据可查，
答辩时也能解释"为什么是 58 而不是 78"。

如果你希望更严谨，可以考虑拆成两个输出：
`harm_index`（纯危害）+ `speech_protection`（受保护程度），让结论更可辩护。

### 2. 定位与合规

这个系统的正确定位是**辅助研判工具**，不是自动定性：

- 最终定性权在司法机关，代码里的 `disclaimer` 字段已固定带上免责声明；
- `action_advice` 用词要克制（"建议平台核查"而不是"建议举报某人"）；
- 法条部分由模型生成，**必须人工复核**，模型编法条是很常见的失败模式；
- 做论文/项目展示时，建议同时报告模型的判定一致性（比如让模型对同一批样本跑 3 次，
  看 `harm_index` 的波动范围），这比单次结果可信得多。

---

## 六、排查清单

| 现象 | 原因 |
|---|---|
| `wit requires the bundled acps_sdk` | 激活了错误的虚拟环境，本项目要用 `.venv` |
| `KeyError: 'OPENAI_MODEL'` | 环境变量没设，或换了终端没重设 |
| 停在 `pending_review` | 注册局人工审核中，平台侧动作，等或催 |
| 注册报证书 SAN 不匹配 | `ACPS_PUBLIC_ENDPOINT` 的主机名和你申请的证书对不上，改地址后需重新注册 |
| `acps_discover` 找不到 Partner | Partner 没注册成功，或没在 `serve()` 状态，或关键词对不上它的 tags |
| 模型不调用提交工具 | 该模型不支持 function calling。换模型，或先用 `--stub` 确认不是接线问题 |
| Leader 一直在 refresh 循环 | instructions 里已要求用 `action="wait"`；若模型不听话，可改用 `runtime.aip_*` 程序化调用 |
