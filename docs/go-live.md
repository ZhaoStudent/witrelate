# 上线手册 —— legal_acps 从注册到运行

> 更新于 2026-09-22 23:00
> 项目根目录：`~/witrelate`（服务器 `ssh acps`）

## 一、一句话现状

三个 Agent（煽动性识别、社会危害评估、主控）已用开发者账号 **`legal-acps-demo`** 提交平台审核，
当前状态 `pending_review`。**审核是平台侧人工动作，我们无法本地推进；一旦通过，一条命令即可全部上线。**

```bash
ssh acps
cd ~/witrelate
./.venv/bin/python -m tools.status          # 查审核状态
bash scripts/serve.sh start                 # 三个角色一起上线（审核通过后）
```

## 二、已经打通并验证过的环节

| 环节 | 状态 | 实测证据 |
|---|---|---|
| 服务器 | ✅ | Ubuntu 24.04.4 LTS / 2 核 / 可用内存 3.1Gi + 1.9Gi Swap / 系统盘 69G |
| 公网 HTTPS 端点 | ✅ | 8443–8447 五个端口从本地（郑州）实测均返回 HTTPS 200 |
| Python 环境 | ✅ | `.venv`（3.14）+ `acps_sdk 2.2.0` + `wit_framework 2.1.0` |
| 开发者账号 | ✅ | `legal-acps-demo`，角色 `CLIENT`，可登录（凭据见服务器 `ACCOUNT.txt`，权限 600） |
| 模型接入 | ✅ | DeepSeek `deepseek-v4-pro`，真实调用返回正常（`local_pipeline` 已跑通两阶段研判） |
| Agent 注册 | ✅ 已提交 | 三条 registration ID，见下表 |
| 实名认证 | 不需要 | 平台实测：提交 Agent 注册**不要求**实名；`GET /verification/identity` 返回 `null` 也能正常提交 |
| mTLS 证书 | ⏳ 自动 | 审核通过后 wit 自动向平台 CA 申请。平台已确认证书 SAN 写的是 **IP** `150.109.154.16`，因此**不需要域名、不需要备案** |
| 服务监听 | ⏳ 待审核 | 现在启动会返回 `blocked / acps_identity_not_active`，属预期行为 |

### 三条 Registration ID

| 角色 | 端口 | Registration ID | 能力 |
|---|---|---|---|
| `incitement` | 8443 | `01a0c992-a3f9-734a-929c-f4fefc612f13` | `speech.detect_incitement`、`facts.verify_claims` |
| `harm` | 8444 | `01a0c993-2097-7477-bfe1-8bae1b63c950` | `harm.assess_social_impact` |
| `leader` | 8445 | `01a0c99e-2e87-754e-a5b1-1c55e9c7bd3b` | `legal.issue_verdict` |

## 三、审核通过后要做的三件事

```bash
ssh acps
cd ~/witrelate

# 1) 确认状态已变成 active
./.venv/bin/python -m tools.status

# 2) 启动全部三个角色（后台常驻，日志写到 logs/）
bash scripts/serve.sh start

# 3) 看结果：三个端口都应显示「监听中」
bash scripts/serve.sh status
```

> **不用再手敲 `set -a && . ./.env && set +a`** —— `dotenv_lite.py`（仓库根目录）
> 已经让所有入口脚本自动读取 `.env`。只有你自己写新脚本调 API 时才需要 import 它。

想确认对外真的可达，在**你自己电脑**上跑：

```bash
curl -k --noproxy '*' -m 10 https://150.109.154.16:8443/health
```

（`--noproxy '*'` 是必须的，你本机 shell 里设了 `http_proxy`，不加会走代理导致误判。）

## 四、日常命令速查

| 目的 | 命令 |
|---|---|
| 查审核 / 注册状态 | `./.venv/bin/python -m tools.status` |
| 启动全部角色 | `bash scripts/serve.sh start` |
| 只启动一个 | `bash scripts/serve.sh start harm` |
| 停止全部 | `bash scripts/serve.sh stop` |
| 看进程与端口 | `bash scripts/serve.sh status` |
| 跟踪日志 | `bash scripts/serve.sh logs harm` |
| 提交/恢复某个角色的注册 | `./.venv/bin/python -m legal_acps.run_agent harm --register-only` |
| 本地跑一次完整研判（不用注册） | `./.venv/bin/python -m legal_acps.local_pipeline --text "待研判文本"` |
| 主控跑一次真实研判 | `./.venv/bin/python -m legal_acps.run_agent leader --input "待研判文本"` |
| 看自己名下所有 Agent | `./.venv/bin/python -m tools.portal agents` |
| 查平台最近上线的 Agent | `./.venv/bin/python -m tools.portal recent` |
| 查看/核对开发者账号密码 | `cat ~/witrelate/ACCOUNT.txt`（权限 600） |
| 验证凭据是否真能登录 | `./.venv/bin/python -m tools.verify_login` |

## 五、如果审核不通过

平台审核结果会反映在 `tools.status` 的状态里。常见原因与处理：

1. **ACS 信息不完整**：`run_agent <角色> --register-only` 会重新读取最新的 `acs.draft.json` 并覆盖草稿。
2. **endpoint 不可达**：确认 8443–8447 在腾讯云防火墙里是放行的，且 `ACPS_PUBLIC_HOST` 仍是 `150.109.154.16`。
3. **要撤销重来**：
   ```bash
   ./.venv/bin/python -m tools.portal agents              # 找到 agent_id
   ./.venv/bin/python -m tools.portal cancel <agent_id>   # 撤销审核申请
   ./.venv/bin/python -m tools.portal delete-agent <agent_id>
   ```

## 六、这套东西的关键结论（踩过的坑都在这里）

1. **四个平台地址不用自己申请**，`AcpsConfig.ioa_pub()` 已内置；凭据就是登录注册平台的账号。
2. **证书完全不用手动申请**：审核通过后 wit 调 `issue_certificate_pair()` 自动签发「客户端证书 + 服务端证书 + 信任包」。
3. **可以用裸 IP，不需要域名和备案**：平台签发的证书 SAN 里放的就是 IP。
4. **账号必须存在于 ACPs 注册库**（`https://www.ioa.pub/registry`）。梧桐网页账号若不在这个库里，`login()` 会返回 `needs_login`。平台支持自助注册，只要用户名 + 密码，无需短信验证码。
5. **`public_endpoint` 不能带路径后缀**，只写到 `https://主机:端口`；wit 自己的监听器挂在 `/rpc`、`/notification/*`、`/stream`、`/health` 上。
6. **登录接口是 OAuth2 表单**（`application/x-www-form-urlencoded`），不是 JSON。
7. **审核通过前监听器一定是 blocked**，这不是代码问题。
8. **模块名不要叫 `platform.py`**：会劫持 Python 标准库的 `platform`，导致 `openai` 库循环导入。本项目已改名 `acps_config.py`，所以脚本必须用 `python -m` 方式运行。
9. **`.env` 由脚本自己加载**（`dotenv_lite.py`，零依赖）。`python -m xxx` 不会自动读 `.env`，忘了 source 就会看到 `RuntimeError: Missing Registry developer credentials`。已把所有入口脚本接上，以后直接用 `./.venv/bin/python -m tools.status` 即可。
10. **`.env` 里的值可以带引号，但别写成"带引号的密码文件"**。`.env` 里密码含特殊字符时用引号包起来是对的（`dotenv_lite` 与 shell source 都会剥掉），但 **ACCOUNT.txt 这种给人看的文件必须存不带引号的明文**，否则照抄去登录会 401。当初生成 ACCOUNT.txt 用了 `tr -d "\x27"` 想剥引号——**GNU `tr` 不认 `\x27` 这种十六进制写法（只认八进制 `\047`）**，结果是引号原样写进了文件。用 `tools.verify_login --fix` 已修正。
11. **判断"凭据文件写坏了"不能只看登录成功**：`verify_login.py` 内部会替调用方剥引号，所以即使文件里有引号，登录照样 200。必须比较**原始值**与剥离后的差异（`raw != stripped`）才能发现。
12. **平台上有两套互不相通的账号库**，别搞混：

    | 后端 | 网页入口 | 账号 |
    |---|---|---|
    | `www.ioa.pub/registry` | **没有网页**（纯 API，`GET /` 返回 `Welcome to the Agent Internet Backend API`） | 项目账号 `legal-acps-demo` 在这里 |
    | `wt.ioa.pub/registry-server` | `wt.ioa.pub/registry/`（梧桐控制台） | 你原来那个 wt 账号在这里 |

    所以在 wt 网页登 `legal-acps-demo` 会报 `USER_NOT_FOUND`——**这是正常的**，不是账号坏了。
    ACPs 侧的账号只能用 API 验证：`./.venv/bin/python -m tools.verify_login`。

## 七、还没做的（后期学习后可以继续）

- 从 2 个专用 Agent 扩到 5 个（在 `acps_config.py` 的 `PORTS` 加端口，在 `agents.py` 的 `BUILDERS` 加角色）。
- AMP 运行日志上报（平台端点 `wt.ioa.pub:19092`，Kafka/TLS），用于平台的运行监控与链路追踪。
- 让主控通过 Discovery 动态找 Agent，而不是写死两个 Partner。
- 把模型换成更严谨的版本、给判定加人物角色化的 prompt（当前口径已写在 `contracts.py` 的评分量表里）。
