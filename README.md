# witrelate · ACPs 法律合规多智能体

> 在梧桐 ACPs 平台（`wt.ioa.pub`）上，按输入进入
> **事实核查 / 多媒体证据鉴伪 / 法律建议 / 线索受理** 分支；
> 需要时组合，最后给出可追溯结论。
> 另有一个面向当事人的**反诈演练场**（进程内编排，不注册平台）。

**当前状态（2026-10-23）**：12 个角色定义、10 个部署、2 个软废弃；
主链在 s1，证据三叶子在 s2；平台注册/发现/跨机互联/访问已跑通；
每日案例轮已累计 23 轮（17 个 exit0 + 6 个 10-17～10-19 的端口误报 exit1）；
初赛材料收尾中。

> ✅ **2026-10-21 改名完成**：主控平台名已从「法律咨询主控」改为 **「法前哨」**
> （v1.1.0），新 AIC = `1.2.156.3088.1.BUPT.81M3FX.6CKYE5.0ZV54U.0IPO`，
> 8445 已恢复监听；叮当按名称可搜到，public recent 已收录。旧 AIC `…X1J5R5…`
> 已软删除。⚠️ Agent 程序化 `acps_discover` 的 ADP 索引可能还滞后，
> 以 registry/叮当为准，等索引刷新后重跑。变更记录见
> [改名留档](docs/aicomp/改名留档-2026-10-21/README.md)。

**正在做**：

- **两周整改**：11 项清单见 [docs/整改清单.md](docs/整改清单.md)；
- **材料**：技术方案 [PDF](docs/aicomp/技术方案-法前哨.pdf) / [DOCX](docs/aicomp/技术方案-法前哨.docx) 已出；
  答辩 [PPT](docs/aicomp/法前哨-答辩PPT.pptx) / [PDF](docs/aicomp/法前哨-答辩PPT.pdf) 已出；
  5 分钟视频为**可选项**，暂不做；[脚本](docs/aicomp/视频脚本-法前哨.md) 仅留档；
- **外部试用**：目标 3 个不同的人/账号各成功调用一次，见
  [docs/外部试用邀请.md](docs/外部试用邀请.md) 与
  [docs/外部试用记录.md](docs/外部试用记录.md)。

> 🔴 **先读 [docs/当前事实基线.md](docs/当前事实基线.md)。**
> 它回答“今天什么是真的”；本 README 只做导航与快速开始。
> 其他文档是带日期的快照，冲突时以基线为准。
>
> 🧭 **改代码前先读 [ARCHITECTURE.md](ARCHITECTURE.md)。**
> 它写清了生产入口、核心包、探针、实验层的边界，以及“现在不动什么”。

---

## 一分钟看懂

```text
legal-consult-leader（8445，外层主控；平台名「法前哨」）
 ├─ fact-judge（8448，内层裁判）── debate-pro（8446）
 │                                  debate-con（8447）
 ├─ evidence-leader（8450，s1）── evidence-image（8449，s2）
 │                                  evidence-audio（8451，s2）
 │                                  evidence-video（8452，s2）
 ├─ law-advisor（8453）
 └─ lead-intake（8454）

incitement（8443）/ harm（8444）：软废弃，代码保留、不部署、不进主链
```

- **协议**：ACPs / AIP 直连 RPC，mTLS；AIC 身份；Discovery 按语义发现伙伴。
- **链深**：`MAX_CHAIN_DEPTH = 3`；裁判与证据汇总都是“内层 Leader”。
- **交付物**：`FinalVerdict` / `FactRuling` / `EvidenceRuling` / `LegalAdvice` / `LeadSubmission`。
- **跨机**：s1 `150.109.154.16`（7 个角色），s2 `101.33.124.181`（3 个证据叶子）。

---

## 快速开始

### A. 不碰平台：把体验应用跑起来（假模型，不需要 Key）

```bash
# Windows
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install acps_sdk-2.2.0-py3-none-any.whl
.\.venv\Scripts\python.exe -m pip install wit_framework-2.1.0-cp312.cp313.cp314-none-any.whl
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m legal_acps.experience.server --stub --port 8090
```

```bash
# Linux / macOS
python3 -m venv .venv
./.venv/bin/pip install acps_sdk-2.2.0-py3-none-any.whl
./.venv/bin/pip install wit_framework-2.1.0-cp312.cp313.cp314-none-any.whl
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m legal_acps.experience.server --stub --port 8090
```

浏览器打开 `http://127.0.0.1:8090`。这是反诈演练场：受理官立案 → 诱导方与当事人（你）交锋
→ 裁判揭示；**每局必须以揭示收尾**，诱导方的 `traps` 不会进入公开视图。

### B. 真模型 + 梧桐平台：跑一次主链研判

先把 `.env.example` 复制成 `.env`，至少填：

```dotenv
ACPS_BACKEND=wt
ACPS_USERNAME=<梧桐账号>
ACPS_PASSWORD=<密码>
ACPS_PUBLIC_HOST=<本机公网 IP>
OPENAI_MODEL=deepseek-v4-pro
OPENAI_API_KEY=<key>
OPENAI_BASE_URL=https://api.deepseek.com
```

首次注册 / 查审核（平台人工审核不可控）：

```bash
./.venv/bin/python -m legal_acps.run_agent leader --register-only
./.venv/bin/python -m tools.list_agents        # 只读：看平台侧状态
```

审核通过后，起常驻服务（串行启动，避免并发登录顶会话）：

```bash
bash scripts/serve.sh start
bash scripts/serve.sh status
# 或走 systemd：sudo systemctl start acps-leader acps-fact-judge ...
# ⚠️ 两套不要混用：同端口两份进程会互相抢占
```

跑一次真实研判（会真的 discover + 委派；需要伙伴已 active）：

```bash
./.venv/bin/python -m legal_acps.run_agent leader \
  --input "请研判下面这段说法，并做事实核查：……"
```

产物落在 `output/`：`verdict-*.json`（主控）、`ruling-*.json`（裁判）、
`evidence-*.json`（鉴证）。**`status=completed` 不算成功；`output/` 里有没有新文件才算。**

### C. 验证

```bash
./.venv/bin/python -m legal_acps.check_local          # 全量离线自检（很重，几分钟）
./.venv/bin/python -m tools.acs_fingerprint           # 12 个角色的 ACS 指纹
./.venv/bin/python -m tools.verify_experience         # 体验应用 155 项
node run/verify_frontend_flow.mjs                     # 前端双页 52 项（需 Node）
./.venv/bin/python -m tools.metrics --md              # 四项量化指标（需服务器任务库）
```

`check_local` 在服务器上应 `exit 0`；本机缺 `.env`/`ss`/systemd 时会有“跳过”或
“本机不成立”的项。**跳过 ≠ 通过**，看每节标题与退出码，不要只看最后一行。

---

## 目录导航

| 路径 | 是什么 |
|---|---|
| [legal_acps/](legal_acps) | 核心包：Agent 定义、契约、辩论/终止、事实库、快车道、案例轮、台账 |
| [legal_acps/experience/](legal_acps/experience) | 反诈演练场：受理官 / 诱导方 / 裁判 + 信息分区 + 前端 |
| [legal_acps/evidence/](legal_acps/evidence) | 图像鉴伪（EXIF/ELA/噪声/复制-移动） |
| [legal_acps/audio/](legal_acps/audio) | 音频鉴伪（容器/编码/波形域） |
| [legal_acps/video/](legal_acps/video) | 视频鉴伪（容器/帧/时间线） |
| [legal_acps/facts/](legal_acps/facts) | 离线抓取的权威事实库（JSONL，运行时只读） |
| [legal_acps/laws/](legal_acps/laws) | 法条库（8 部法律，逐条 JSONL） |
| [legal_acps/rulings/](legal_acps/rulings) | 人工基准结论（ground truth） |
| [legal_acps/mcp/](legal_acps/mcp) | MCP server：把鉴伪能力暴露给 MCP 客户端 |
| [tools/](tools) | 指标、溯源、指纹、验收、校准等工具 |
| [run/](run) | 真机探针、元验证、一次性诊断（**不是生产入口**；分类见 [run/README.md](run/README.md)） |
| [scripts/](scripts) | serve / verify / case-round / systemd-install / 双机同步 |
| [deploy/systemd/](deploy/systemd) | 12 个角色单元 + 案例轮 timer |
| [docs/](docs) | 上线手册、平台存证、设计与评审分析 |
| [docs/aicomp/](docs/aicomp) | 赛题规则、技术报告大纲、方案与台账（**不是最终提交件**） |
| [docs/product/](docs/product) | 产品形态对比、目录结构、界面设计与静态原型（赛后产品化） |
| [acs/](acs) | ACS 模板（真实身份在 `.wit/<role>/acps/`） |
| [attic/](attic) | 已完成使命的教程/一次性脚本归档（不被 import） |
| `output/` | 运行时产物（gitignored） |
| `.wit/` | 角色身份、证书、任务库、台账（gitignored） |

---

## 文档状态

**当前**：[README.md](README.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [docs/当前事实基线.md](docs/当前事实基线.md) ·
[docs/上线操作手册.md](docs/上线操作手册.md)（工作日志，最新）。

**历史快照**（带日期，不要当现状）：

- [legal_acps/README.md](legal_acps/README.md) —— 2026-09-22，3 Agent 时代；
- [docs/验收报告.md](docs/验收报告.md) —— 2026-09-30，6 Agent 快照；
- [docs/aicomp/初步得分评估.md](docs/aicomp/初步得分评估.md) —— 2026-09-27 基线；
- [docs/aicomp/项目复审与加分路线.md](docs/aicomp/项目复审与加分路线.md) —— 2026-10-03 快照；
- [docs/aicomp/调用说明.md](docs/aicomp/调用说明.md) —— 2026-10-13 快照，部分段落已被后续实测更新；
- [docs/aicomp/第二阶段方案评价与设计.md](docs/aicomp/第二阶段方案评价与设计.md) —— 2026-10-05 设计；
- [docs/aicomp/应用场景重审.md](docs/aicomp/应用场景重审.md) —— 2026-10-01 产品叙事分析。

---

## 已知边界（对外介绍别漏）

1. **成功的外部访问只有 1 次**（2026-10-11 快照）；其余是自测。
2. **95.45% 不是泛化准确率**：22 条人工基准、21 对；没有“断章取义/无法判定”基准；
   事实库与基准同源，检索成分大。
3. **MCP 是 server，不是桥**：没有实现 MCP↔Capability 桥接。
4. **反诈演练场不走 ACPs**：不产生平台访问记录。
5. **证据鉴伪是技术线索，不是司法鉴定**：没有 ground-truth 篡改数据集；
   音频压缩格式、视频帧层有明确边界。
6. **平台侧脆弱**：TLS 证书曾过期、discovery 索引曾丢失、并发登录会顶会话；
   现场演示要有离线 stub / 录屏的 Plan B。

---

## 第三方与许可证

- 随仓库提供两个 wheel：`acps_sdk-2.2.0`、`wit_framework-2.1.0`；
  来源与哈希见 [release-manifest.json](release-manifest.json)。
- 这两个 wheel 的许可证跟各自上游一致；本项目代码的许可证**尚未附 LICENSE 文件**，
  开源前需由作者确定（建议在提交前补上）。
