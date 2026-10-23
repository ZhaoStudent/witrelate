# 主控改名留档（2026-10-21）

> 目的：把「法律咨询主控」改名为「法前哨」，同时把 ACS 描述改成
> “事前准备”口径。平台审核通过后，本文件补新 AIC 与验证结果。

## 改名前（已留档）

| 项 | 值 |
|---|---|
| 平台名 | 法律咨询主控 |
| 版本 | 1.0.0 |
| registration id | `01a0e223-392e-7400-bcdc-99fa17adc411` |
| AIC | `1.2.156.3088.1.BUPT.81M3FX.X1J5R5.HKBBAM.018M` |
| endpoint | `https://150.109.154.16:8445/rpc` |
| 状态 | APPROVED / active |
| 快照 | [leader-before.json](leader-before.json)、[agents-before.json](agents-before.json) |

> 旧注册已于 2026-10-21 12:32 通过 `tools.portal delete-agent` 软删除；
> 旧 AIC 不再作为当前身份使用，仅保留在历史任务库与快照中。

## 改名后（提交信息）

| 项 | 值 |
|---|---|
| 平台名 | **法前哨** |
| 版本 | 1.1.0 |
| registration id | `01a15cbb-546d-71de-9bcd-e086d93e9769` |
| AIC | `1.2.156.3088.1.BUPT.81M3FX.6CKYE5.0ZV54U.0IPO` |
| endpoint | `https://150.109.154.16:8445/rpc` |
| ACS 描述 | 在成为当事人之前做准备：核查可疑说法、鉴伪图片/音频/视频、在覆盖范围内给出法律方向、组织回应演练；每一步留痕。 |
| provider.name | 法前哨 |
| provider.organization | `BUPT-LegalAgent-Demo`（历史账号 org；材料里不要露出） |
| 状态 | **APPROVED/active**（2026-10-21 13:36 通过；13:37 签证书、起服务） |

## 当前操作状态（已完成）

- 平台审核已通过，`run_agent leader --register-only` 输出 `active`；
- 新 AIC 已签发：`1.2.156.3088.1.BUPT.81M3FX.6CKYE5.0ZV54U.0IPO`；
- `acps-leader.service` 已启动，8445 在听；listener 自报
  `state=active`、`matchesListener=True`、AMP serving；
- `GET /api/v1/agent/public/recent` 已收录「法前哨」v1.1.0（`active=true`）；
- **平台前台/叮当按名称发现已验证**：2026-10-21 13:36 叮当搜「法前哨」返回
  primary 主控、技能 `legal.issue_verdict`，并进入“可调用智能体”卡片；截图见
  [平台验证截图-2026-10-21](../平台验证截图-2026-10-21/README.md)。
- ⚠️ **Agent 程序化 Discovery 仍滞后**：13:47 `POST /discovery/.../discover`
  仍返回 2 条、不含新 AIC；其他 Agent 用 `acps_discover` 找它可能暂时失败。
  等 ADP 索引刷新后重跑矩阵再下结论。
- 13:44 改名后真机案例轮：`exit_code=0`、新增 `verdict-20261021T012214Z.json`、
  20 轮 / 跑满 1 圈；`check_local` 21 节全通过（MCP 按设计跳过）。
- 14:12 改名后首个**成功外部调用**：外部 AIC `…EZTK03.BS0XTB…` →
  法前哨，`task_96fd0329ecbb` completed（结论“无法判定”）；
  返回原文见 [外部调用结果](../外部调用-2026-10-21/README.md)。
- 14:18 内部自测 6 条（law-advisor → 法前哨，5 条批量 + 1 条 curl）全部
  `completed`；只算自测，不计外部；明细见
  [internal-selftest-2026-10-21.jsonl](../internal-selftest-2026-10-21.jsonl)。
- ✅ 新名称截图已补：[平台验证截图-2026-10-21](../平台验证截图-2026-10-21/README.md)；
  材料/邀请里的旧名截图请替换为这一组。

## 审核过程留档

- 2026-10-21 12:32 旧记录软删除，新记录提交；
- 12:43 `POST /submit` 返回 `400 INVALID_STATUS_TRANSITION`（已 PENDING，不能重复提交）；
- 12:47 仍 `PENDING`，`processed_by_id=null`；
- 13:36 用户确认平台已通过；13:37 `run_agent` 推进到 `active` 并签证书。
