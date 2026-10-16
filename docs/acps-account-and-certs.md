# ACPs 四件事：账号 / 审核 / 证书 / 模型 Key

> 2026-09-22 实测更新，**2026-09-23 再次更正第 1 节**（两套账号库的存在）。
> 本文的回答**优先于**我此前给你的口头结论——
> 其中「3 个 HTTPS 证书」那条是错的，我在下面第 3 节明确更正。

---

## 结论速览

| 你问的 | 真实情况 | 你要做什么 |
|---|---|---|
| **1. ioa.pub 开发者账号** | 平台上有**两套互不相通的账号库**。项目用的 `legal-acps-demo` 在 **ACPs 注册局**（`www.ioa.pub/registry`，纯 API 无网页）；你原来那个 wt 账号在**梧桐**（`wt.ioa.pub/registry-server`，有网页控制台） | 已注册并写入 `.env`，**无需操作** |
| **2. 实名审核** | 有两个不同的"审核"，别混淆。Agent 注册审核由 wit 自动轮询；账号实名（若平台要求）只能在网站做，wit 无接口 | 先提交注册，看平台返回什么再定 |
| **3. 证书** | ❌ **不需要你申请任何证书**。平台 CA 在 `enroll()` 时自动签发「客户端证书 + 服务端证书 + 信任包」 | **什么都不用做** |
| **4. 模型 API Key** | ✅ 你的 key 实测可用 | 已填入服务器 `.env` |

---

## 1. 账号 —— 有两套互不相通的账号库（2026-09-23 更正）

平台上有两个都叫 registry 的后端，**账号库不共享**：

| 后端 | 网页入口 | 角色 | 项目用的账号在哪 |
|---|---|---|---|
| `https://www.ioa.pub/registry` | **没有网页**（`GET /` 返回 `{"message":"Welcome to the Agent Internet Backend API"}`） | ACPs 注册局，**wit 默认指向它**（`AcpsConfig.ioa_pub()`） | `legal-acps-demo` ✅ |
| `https://wt.ioa.pub/registry-server` | `https://wt.ioa.pub/registry/`（梧桐控制台，pure-admin 前端） | 梧桐科研协作平台自己的注册后端 | 你原来那个 wt 账号 |

实测证据（用**故意写错的密码**做探针，只看错误类型，不泄露真实密码）：

| 用户名 | `wt.ioa.pub/registry-server` | `www.ioa.pub/registry` |
|---|---|---|
| `legal-acps-demo` | `USER_NOT_FOUND` | `INVALID_CREDENTIALS` → **存在** |
| `studentzhao` | `USER_NOT_FOUND` | `USER_NOT_FOUND` |

所以三件事：

1. **你在 wt 网页登 `legal-acps-demo` 报 `USER_NOT_FOUND` 是正常的**——那个账号不在这套库里。
2. **ACPs 侧没有网页可登**。它的账号只能用 API 验证（`tools/verify_login`），浏览器里看不到。
3. 我此前说"`www.ioa.pub` 和 `wt.ioa.pub/registry/` 是同一套账号库"是**错的**，特此更正。

**你要做的**：不需要任何操作。项目凭据 `legal-acps-demo` 已写入服务器 `.env`，
实测登录 HTTP 200、角色 `CLIENT`、`is_active=true`。

想自己看一眼状态：

```bash
ssh acps
cd ~/witrelate
./.venv/bin/python -m tools.verify_login   # 只给长度/掩码/首尾字符，不打印明文
./.venv/bin/python -m tools.portal whoami  # 看角色与账号 ID
```

---

## 2. 审核 —— 有两个，别混淆

### 2.1 Agent 注册审核（这个 wit 全自动，你不用管）

wit 的注册是个状态机，源码里明明白白（`wit/acps/provisioner.pyi`）：

```python
class EnrollmentResult:
    status: EnrollmentStatus
    @property
    def waiting_for_review(self) -> bool: ...   # ← 就是"等审核"
```

`HttpRegistryClient` 里对应的五个方法：

```python
async def login(...)               -> RegistryToken
async def submit_registration(...) -> str            # 提交
async def review_status(...)       -> RegistryReview # 轮询审核状态
async def register_entity(...)     -> RegistryEntity # 拿到 AIC
async def fetch_eab_credential(...)-> dict           # 拿证书签发的入场券
```

从字节码里提取到的审核状态常量：

```
PENDING / APPROVED / REJECTED          # approval_status
"Approved Registry record is missing is_ontology"
```

**所以流程是**：你 `submit` → 平台那边**人来审**你的 Agent 描述（ACS 里的名称、能力、机构信息） → 状态变 `APPROVED` → wit 自动接着去申请 AIC 和证书。
`waiting_for_review` 为真时，你的程序就是在等服务方，不用做任何事。

**这只是一个"Agent 能不能上架"的审核**，不是对你个人身份的核验。

### 2.2 账号实名认证（若平台要求 —— 只能在网站做）

wit 的注册客户端里**没有**任何实名接口。我把整个 `wit/acps/` 的字节码检索了一遍，`verification` 这个词只出现在 `amp.pyc`（监控协议的签名校验），与身份核验无关。

所以：

- **如果平台要求实名**，你只能在网页上提交身份证信息。wit 帮不了，我也帮不了——这是设计如此。
- **如果平台不要求**，这一步直接跳过。

**怎么知道到底要不要？** 不用猜：先把账号密码填好，跑一次注册。如果平台要求实名，`enroll()` 会返回一个明确的阻塞状态和 `next_action` 提示。到时你把报错贴给我。

> 我此前把「实名审核」和「Agent 注册审核」混在一起说了，这里更正：
> **我唯一能确认必须做的是 2.1**；2.2 存不存在，取决于平台策略，需要一次真实调用来确定。

---

## 3. 证书 —— 我之前说错了，这里更正

### 我之前说的（错的）

> "申请 3 个 HTTPS 证书（8443/8444/8445 各一个）"，"需要你决定用域名还是裸 IP"

### 实际情况

**你不需要申请任何证书。** 平台 CA 会在 `enroll()` 时自动给你签发一整套。

**证据一：源码里有"成对签发"的方法**（`wit/acps/ca.pyi`）

```python
class HttpCaClient:
    """Minimal ACME client for ACPs CA Server certificate issuance."""
    async def issue_certificate_pair(
        self, *, aic: str, eab_credential: Mapping[str, Any],
        config: AcpsEnrollmentConfig, public_endpoint: str | None = None,
    ) -> CertificateIssueResult: ...
```

注意 `issue_certificate_pair` —— **一次签发一对**（客户端证书 + 服务端证书），
不是让你自己拿一张证书去配。还有 `_download_trust_bundle()` 负责拉信任包。

**证据二：字节码里的文件名**

```
client_certificate_dir / server_certificate_dir / trust-bundle.pem
_validate_leaf(side=CertificateSide, ...)
```

`CertificatePaths` 的字段（`wit/acps/identity.pyi`）：

```
clientCertificate / clientPrivateKey / serverCertificate / serverPrivateKey / trustBundle
```

**证据三：实跑一次，信任包真的拉下来了**

```
>>> GET https://www.ioa.pub/ca-server/acps-atr-v2/ca/trust-bundle
✅ 成功，拿到 5628 字节（含 3 张证书）
```

**证据四：裸 IP 可用**

证书校验代码里**同时存在** `DNSName` 和 `IPAddress`，还有专门的错误码
`server_public_endpoint_san_mismatch`：

```
IPAddress / ip_address / hostname / DNSName / _dns_name_matches
public_endpoint / rstrip
```

含义：你把 `public_endpoint` 写成 `https://150.109.154.16:8443`，
CA 就会把 `150.109.154.16` 放进证书的 IP SAN 里。

### 所以你要做的

**什么都不用做。** 只需要在 `.env` 里给一个主机：

```
ACPS_PUBLIC_HOST=150.109.154.16
```

端口由代码按角色自动补（8443/8444/8445）。

### 三个由此消失的问题

| 我原以为要处理的 | 实际 |
|---|---|
| 买域名 | ❌ 不用，裸 IP 就行 |
| ICP 备案 | ❌ 不用（香港节点 + 裸 IP 双重豁免） |
| Let's Encrypt 证书 + 6 天/90 天续期 | ❌ 不用，平台 CA 自动续 |

> 顺带：你在浏览器访问 `https://150.109.154.16:8443` 会看到"证书不受信任"警告——
> 那是**临时的 openssl 测试服务**用的自签证书，跟我们项目无关。
> 平台 CA 是私有 CA，ACP 的对方是靠 `trust-bundle.pem` 来验证你的，
> 不需要浏览器信任。

---

## 4. 模型 API Key —— 实测可用

### 你的 key 能跑，实测记录

```bash
$ curl https://api.deepseek.com/v1/models -H "Authorization: Bearer sk-..."
{"object":"list","data":[
  {"id":"deepseek-flash",   "name":"DeepSeek-V4.1-Flash", "context_window":1048576, ...},
  {"id":"deepseek-v4-pro",  "name":"DeepSeek-V4-Pro",     "context_window":1048576, ...}
]}
```

可用的模型是这两个（**注意：不是 `deepseek-chat`**，那个名字在这个环境里已经没有了）：

| 模型 id | 名称 | 建议用途 |
|---|---|---|
| `deepseek-v4-pro` | DeepSeek-V4-Pro | 法律判断主力，写进 `.env` |
| `deepseek-flash` | DeepSeek-V4.1-Flash | 联调省钱，快 |

### 一个坑：它们是推理模型

实测用 `max_tokens=20` 时，返回是这样的：

```json
"message": {"content": "", "reasoning_content": "我们需要回答用户。用户说：…"},
"finish_reason": "length"
```

**`content` 是空字符串**——20 个 token 全被思考过程吃光了。
这不是模型坏了，是预算给少了。所以 `legal_acps/llm.py` 里强制 `max_tokens=8192`。

### 安全提醒

这个 key 已经在聊天里明文出现过。项目跑通之后，建议去 DeepSeek 控制台
**吊销并重新生成**一个，只写进服务器 `.env`（权限 600，已设好）。

---

## 5. 现在的状态：只差两行

服务器上已经做完并验证的：

| 项 | 状态 |
|---|---|
| Python 3.14.7 + acps_sdk 2.2.0 + wit 2.1.0 | ✅ |
| 离线自检 `python -m legal_acps.check_local` | ✅ 四项全通过 |
| 平台 CA 可达、信任包可拉取 | ✅ 5628 字节 / 3 张证书 |
| 注册服务可达、登录接口存在 | ✅ |
| DeepSeek 真实模型两阶段研判 | ✅ 输出非空、结论合理 |
| `harm_index` 分档自动补全 | ✅ 修复（65→高风险，-70→完全正当） |

**唯一阻塞**：`~/witrelate/.env` 里的 `ACPS_USERNAME` / `ACPS_PASSWORD` 是空的。

填好之后，剩下的路径是：

```
enroll() → 平台审核(PENDING→APPROVED) → 拿 AIC → 自动签发证书 → serve() 起监听 → 上线
```

---

## 附：这次顺手修掉的两个真 bug

1. **模块名撞标准库**：`legal_acps/platform.py` 覆盖了 Python 标准库的 `platform`，
   而 `openai` SDK 内部会 `import platform`，结果拿到我们的文件 → 循环导入崩溃。
   已改名 `acps_config.py`。**这也解释了为什么脚本必须用 `python -m legal_acps.xxx` 跑。**
2. **Pydantic v2 不校验默认值**：`band` 字段写了自动补全的 validator，
   但 Pydantic v2 默认 `validate_default=False`，模型不填时 validator 根本不执行，
   输出里一直是 `"band": ""`。已加 `validate_default=True`。

（另：`public_endpoint` 原先被我多拼了 `/acps/aip/v1` 后缀，
经查 wit 的监听器实际挂在 `/rpc`、`/notification/*`、`/stream`、`/health`，
地址只应写到 `主机:端口` 为止。已改。）
