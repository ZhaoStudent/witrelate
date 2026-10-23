# 法前哨 · 代码结构

> 这份文件是**结构地图 + 冻结规则**，不是重构提案。
> 2026-10-21 做了一次“不改变行为”的收口：根目录一次性脚本移入
> [attic/](attic/)，并明确生产入口、核心包、探针、实验层的边界。
> 当前事实仍以 [docs/当前事实基线.md](docs/当前事实基线.md) 为准。

---

## 0. 先读这 5 个文件

如果你只想在 30 分钟内看懂这套代码，按这个顺序：

1. [README.md](README.md) —— 产品是什么、怎么跑；
2. [docs/当前事实基线.md](docs/当前事实基线.md) —— 今天什么是真的；
3. [legal_acps/acps_config.py](legal_acps/acps_config.py) —— 12 个角色、端口、软废弃、
   部署集合的**单一事实源**；
4. [legal_acps/agents.py](legal_acps/agents.py) —— 只看 `build_*` 函数和
   `LEADER_INSTRUCTIONS` 等提示词入口，**不要从头通读 147KB**；
5. [legal_acps/run_agent.py](legal_acps/run_agent.py) —— 一个角色怎么注册、怎么起监听。

`legal_acps/check_local.py`（466KB）是**可执行规格**，不是入门读物：
它把契约、终止、链深、证据、体验分区等判据写成断言；改结构时按节跑，不要通读。

---

## 1. 运行拓扑

```text
s1 150.109.154.16
  leader 8445（法前哨，外层 Leader）
   ├─ fact-judge 8448（内层 Leader）─ debate-pro 8446 / debate-con 8447
   ├─ evidence-leader 8450 ─ evidence-image 8449 (s2)
   │                          evidence-audio 8451 (s2)
   │                          evidence-video 8452 (s2)
   ├─ law-advisor 8453
   └─ lead-intake 8454

s2 101.33.124.181
  evidence-image / evidence-audio / evidence-video

experience 8090（s1，进程内编排，不注册平台）
  └─ 受理官 / 诱导方 / 裁判 + 信息分区 + 前端
```

- **12 个角色定义、10 个部署、2 个软废弃**（incitement 8443 / harm 8444）。
- 链深上限 `MAX_CHAIN_DEPTH = 3`；证据链已到顶。
- 平台名：**法前哨** v1.1.0，AIC `…6CKYE5.0ZV54U.0IPO`。
- 旧名「法律咨询主控」/旧 AIC 只存在于历史截图和任务库，不再作为当前身份。

---

## 2. 代码分层

| 层 | 路径 | 是什么 | 不是什么 |
|---|---|---|---|
| 核心包 | `legal_acps/` | Agent 构建、契约、终止、辩论、事实/法条库、证据鉴伪、案例轮、台账 | 不是脚本堆 |
| 体验层 | `legal_acps/experience/` | 反诈演练场，进程内编排，独立前端 | **不注册 ACPs**，不产生平台访问 |
| MCP | `legal_acps/mcp/` | 把鉴伪能力暴露给 MCP 客户端 | 不是 ACPs 桥；设计里的“桥”是未来工作 |
| 运维工具 | `tools/` | 平台查询、指纹、指标、状态、清理 | 不是业务逻辑 |
| 探针/元验证 | `run/` | e2e 真机探针、离线元验证、一次性调查、样本生成 | **不是生产入口** |
| 启停脚本 | `scripts/` | `serve.sh`、`sync_s2.sh`、`verify.sh`、systemd 安装 | 与 systemd 二选一，不能同端口混跑 |
| 部署单元 | `deploy/systemd/` | 每个角色一个 unit + timer + logrotate；体验应用有 `.service.example` 模板 | 不在这里写业务 |
| 材料构建 | `docs/build/` | 技术方案/PPT/产品设计文档的构建与排版脚本 | **不是运行时**，不进同步清单 |
| 事实与材料 | `docs/` | 基线、整改清单、上线手册、参赛材料 | 旧文档是带日期的快照 |
| 归档 | `attic/` | 已完成使命的教程/一次性脚本 | 不被 import，不进同步清单 |

### 同步清单

哪些文件会推到服务器，**只由**
[`tools/file_fingerprint.py`](tools/file_fingerprint.py) 的 `PATTERNS` 定义。
新增目录/扩展名时改这里，不要手写第二份清单。

---

## 3. 生产入口（只有这些）

```bash
# 单个角色：注册 / 起监听（systemd 背后跑的就是它）
./.venv/bin/python -m legal_acps.run_agent leader
./.venv/bin/python -m legal_acps.run_agent fact-judge

# 每日案例轮（timer 调它；默认只跑一轮）
./.venv/bin/python -m legal_acps.case_round

# 体验应用（独立进程，不注册平台）
./.venv/bin/python -m legal_acps.experience.server --stub --port 8090

# 全量离线自检（不是生产服务）
./.venv/bin/python -m legal_acps.check_local
```

开发期启动全部常驻角色：

```bash
bash scripts/serve.sh start
bash scripts/serve.sh status
```

> ⚠️ `scripts/serve.sh` 与 `systemd` **不要混用**；同端口两份进程会互相抢占。

---

## 4. 已知结构债（不假装没有）

| # | 债 | 现状 | 为什么现在不动 |
|---:|---|---|---|
| 1 | `agents.py` 147KB、`check_local.py` 466KB | 单文件过大 | 已过审 ACS 与结构断言绑得紧；赛前拆 = 高风险 |
| 2 | `run/` 95 个脚本 | 探针/元验证/一次性调查混在一起 | 已补 [run/README.md](run/README.md) 分类；不移动 e2e（check_local 按目录派生） |
| 3 | `experience/` 不接 ACPs | 独立演示层 | 赛前改会动前端与信息分区；报告里明写边界 |
| 4 | 身份闸门只认账号前缀 | `DebateChainGuardHooks` 拦外部团队，但不校验 ACS `name` | 加 name allowlist 要 AIC→name 映射；赛后再做 |
| 5 | MCP 说法大于代码 | 代码是 MCP server | 报告改成“能力开放”，桥解放未来工作 |
| 6 | 12 个角色 | 超过最初 5 个 | 已冻结新增；软废弃 2 个，不删代码 |
| 7 | 文档多版本 | 3/6/12 Agent 的旧文 | 已加日期横幅；以基线为准 |

---

## 5. 冻结规则（改代码前先看）

1. **不新增 Agent**。需要新能力，先问能不能放进现有角色/现有契约。
2. **不把新逻辑塞进 `agents.py` / `check_local.py`**。新领域逻辑放
   `legal_acps/<domain>/`，新判据放 `run/meta_verify_*.py`。
3. **不在仓库根新建 `_tmp_*` / `_probe_*` / `step*.py`**。一次性东西放
   `attic/<年月>/`，并在 `attic/README.md` 记来源。
4. **不手改已过审 ACS 的 `name/version/skills`**。改这些字段要重审、换 AIC；
   只改提示词/注释时也要先跑 `tools.acs_fingerprint` 确认作者可控字段没变。
5. **不把“跳过”写成“通过”**。`check_local` 的 `[跳过]` 不等于绿。
6. **不改平台身份做实验**。当前身份是法前哨 `…6CKYE5…`；旧 AIC 已删除。

---

## 6. 改结构时的最小回归

```bash
# 1) 语法/导入
./.venv/bin/python -m compileall -q legal_acps tools

# 2) 指纹：12 个角色一个都不能少
./.venv/bin/python -m tools.acs_fingerprint

# 3) 全量自检（几分钟；MCP 在本机无配置时按设计跳过）
./.venv/bin/python -m legal_acps.check_local

# 4) 真机一轮（会真的 discover + 委派；默认只跑一轮）
./.venv/bin/python -m legal_acps.case_round

# 5) 平台侧
./.venv/bin/python -m tools.list_agents | grep -A3 法前哨
```

---

## 7. 比赛后再做（不在本次范围）

- 拆 `agents.py`：角色定义 / 提示词 / 构建器分离；
- 拆 `check_local.py` 为 `legal_acps/checks/` 包；
- `experience/` 接通一次 ACPs 证据核验，或明确永久独立；
- 身份闸门加 ACS `name` allowlist + 回归；
- 角色收敛（12 → 更少），删除软废弃代码；
- 把 `run/` 的 95 个脚本收敛成 `e2e/` + `meta/` + `archive/` 三组。

> 这些都要等初赛材料整理、提交完成后再动。现在只做“降低熵、不改行为”的收口。
