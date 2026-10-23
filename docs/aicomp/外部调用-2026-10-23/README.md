# 外部调用记录（2026-10-23）

> 第二次成功外部调用；调用方与 2026-10-21 首次成功是**同一个外部 AIC**。  
> 不是第二个不同账号，不能把“2 次成功”写成“2 个不同账号”。

## 任务

| 项 | 值 |
|---|---|
| 任务号 | `task_917b67602160` |
| 方向 | inbound（外部 AIC → 法前哨） |
| 调用方 AIC | `1.2.156.3088.1.0001.00001.EZTK03.BS0XTB.0V3P` |
| 本地 AIC | `1.2.156.3088.1.BUPT.81M3FX.6CKYE5.0ZV54U.0IPO` |
| 状态 | `completed`（2026-10-23 22:31 CST；UTC 14:31:13） |
| 前置任务 | `task_2909debd88c2`，`canceled`（空正文） |

## 输入

用户在叮当里要求“与正方 agent 展开辩论，低难度”，并给出：

- 对方声称有转账记录、聊天截图（未出示）；
- 我方称当晚在杭州出差，有登机牌和酒店记录（未出示）。

## 实际走了什么

法前哨按**平台主链**处理，没有进入 8090 的演练场：

1. 主控把任务派给 `fact-judge`（8448，网络裁判）；
2. 裁判按事实核查/裁定链处理，命中“举证倒置”；
3. 主控交付 `FinalVerdict`：无法判定（truth_score=50）+ 谁主张谁举证 + 补原始凭证。

> “与正方辩论 / 低难度 / 对方的牌 / 我方的牌”是产品层指令，不是 ACPs 契约参数；  
> 当前平台只暴露 `legal_issue_verdict` 一次性研判能力，没有可交互辩论入口。

## 结论

- 这是一次**真实外部成功调用**，可计入“外部成功次数”；
- 不计入“不同外部账号数”（仍为 1）；
- 不计入“正方辩论能力已验证”（能力边界见上）；
- 证据：任务库 inbound 行 + 叮当回答截图（待补）。

## 复跑口径

```bash
ssh acps
cd ~/witrelate
./.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("file:.wit/leader/acps/aip-tasks.sqlite3?mode=ro", uri=True)
print(c.execute("select task_ref,direction,state,remote_aic,created_at,updated_at from aip_tasks where task_ref='task_917b67602160'").fetchone())
PY
```
