### 结论溯源表（机制 5）

> ⚠️ 本文件由 `python -m tools.trace --md` **自动生成**，数据来自平台任务库。
> 生成时刻：2026-10-03T20:52:01+08:00　·　定局依据：verdict 时刻 2026-10-03T08:06:45+00:00 落在 leader 的平台会话窗口内（容差 0s）

> ⚠ **WARNING**：缺少 sidecar（verdict-20261003T080645Z.meta.json 不存在）⇒ 按 verdict 时刻在 leader 的平台会话表里认领到 …c285c8624158（owner=…R5.HKBBAM.018M）；容差 0s。本表把 verdict 归到哪一局靠推断，不是 sidecar 的协议级握手 —— 请核对后再引用。

#### 表 1 · 本次研判

| 项 | 值 |
|---|---|
| 输入原文 | 网传某学校食堂使用过期食材，学生堵门讨要说法，据称后厨画面是上周拍摄的。 |
| 局（session） | `session-6ccfca55-ecd3-4ff1-8106-c285c8624158` |
| 总耗时 | 190.1 秒 |
| 参与 Agent | 4 个（debate-con、debate-pro、fact-judge、leader） |
| 跨智能体调用次数 | 3 |

#### 表 2 · 委托链（每一跳都可回任务库核对）

| # | 层 | 发起 → 接收 | 状态 | 耗时 | 产物 | 任务号 | 配对 |
|---:|---:|---|---|---:|---|---|---|
| 1 | 0 | leader → fact-judge | pending | 190.1s | — | `task_f80e92e5eddb` | 硬 |
| 2 | 1 | fact-judge → debate-pro | slow | 34.4s | product-task-1cbbf5f6-3729-4835-9224-ef90a9a2e4cd | `task_748c2737b90e` | 硬 |
| 3 | 1 | fact-judge → debate-con | slow | 42.3s | product-task-8760467e-0cc1-45a2-9e6f-3f86518abccb | `task_7c54df0afc8f` | 硬 |

#### 表 2b · 跨层归属（**全部为推断**，无协议级硬键）

| 上级任务号 | 下级会话 | 关系 | 证据 | 判据 |
|---|---|---|---|---|
| `task_f80e92e5eddb` | …b6ce5584b6be | parent-child | **推断** | fact-judge 的该会话起止时间落在 task_f80e92e5eddb 的生命周期 [2026-10-03T08:03:26.195065+00:00 .. 2026-10-03T08:06:36.256543+00:00] 之内；跨层无协议级硬键（外层 session ≠ 内层 session，实测） |

#### 表 3 · 结论字段溯源

| 结论字段 | 值 | 来源 | 证据强度 |
|---|---|---|---|
| `subject` | 网传某学校食堂使用过期食材，学生堵门讨要说法，据称后厨画面是上周拍摄的。 | 主控综合（链上无对应下游产物） | **推导** |
| `harm_index` | 20 | 主控综合（链上无对应下游产物） | **推导** |
| `band` | 低风险 | 主控综合（链上无对应下游产物） | **推导** |
| `is_incitement` | False | 主控综合（链上无对应下游产物） | **推导** |
| `truth_score` | 40 | 主控综合（链上无对应下游产物） | **推导** |
| `consistency_note` | 自主研判：1) is_incitement=false——原文"网传……据称……"仅为转述，无号召行为、无指向性攻击动… | 主控综合（链上无对应下游产物） | **推导** |
| `conclusion` | 这段言论是对一起校园食品安全事件的转述，属于未经证实的传闻：其中"食堂使用过期食材""学生堵门讨要说法""后厨画面是上… | 主控综合（链上无对应下游产物） | **推导** |
| `action_advice` | 属未经证实的传闻范畴，建议平台进一步核查信息来源与原始画面；在核实前不宜转发扩散，也不宜据此直接下结论。 | 主控综合（链上无对应下游产物） | **推导** |
| `disclaimer` | 本结论由人工智能辅助生成，仅供法律学习与研究参考，不构成法律意见，不能替代律师或司法机关的认定。 | 主控综合（链上无对应下游产物） | **推导** |

> 强度口径 —— **硬**：该字段名出现在下游返回的产物正文里，行内附有命中片段可供人工复核；它不是平台级认证。　**推导**：按上游材料推导，链上没有原文直接支撑。　**推断**：跨层归属，协议里没有硬键。

#### 表 3b · ⚠ 找不到来源的字段（如实列出，不留空）

| 结论字段 | 原因 |
|---|---|
| `subject` | 链上没有任何下游产物的内容包含该字段名（按词边界匹配，不含产物外壳 id/name）（本局共 2 跳返回了产物） |
| `harm_index` | 链上没有任何下游产物的内容包含该字段名（按词边界匹配，不含产物外壳 id/name）（本局共 2 跳返回了产物） |
| `band` | 链上没有任何下游产物的内容包含该字段名（按词边界匹配，不含产物外壳 id/name）（本局共 2 跳返回了产物） |
| `is_incitement` | 链上没有任何下游产物的内容包含该字段名（按词边界匹配，不含产物外壳 id/name）（本局共 2 跳返回了产物） |
| `truth_score` | 链上没有任何下游产物的内容包含该字段名（按词边界匹配，不含产物外壳 id/name）（本局共 2 跳返回了产物） |
| `consistency_note` | 链上没有任何下游产物的内容包含该字段名（按词边界匹配，不含产物外壳 id/name）（本局共 2 跳返回了产物） |
| `conclusion` | 链上没有任何下游产物的内容包含该字段名（按词边界匹配，不含产物外壳 id/name）（本局共 2 跳返回了产物） |
| `action_advice` | 链上没有任何下游产物的内容包含该字段名（按词边界匹配，不含产物外壳 id/name）（本局共 2 跳返回了产物） |
| `disclaimer` | 链上没有任何下游产物的内容包含该字段名（按词边界匹配，不含产物外壳 id/name）（本局共 2 跳返回了产物） |

#### 复算指引

任何人都可以用下面这些命令，从平台自己的任务库里复现上面每一行：

```bash
python -m tools.trace --verdict /home/ubuntu/witrelate/output/verdict-20261003T080645Z.json --md
sqlite3 .wit/leader/acps/aip-tasks.sqlite3 \
  "SELECT task_ref,direction,remote_aic,state,created_at FROM aip_tasks WHERE session_id='session-6ccfca55-ecd3-4ff1-8106-c285c8624158'"
```

本页数据文件：
  · `.wit/incitement/acps/aip-tasks.sqlite3`
  · `.wit/harm/acps/aip-tasks.sqlite3`
  · `.wit/leader/acps/aip-tasks.sqlite3`
  · `.wit/debate-pro/acps/aip-tasks.sqlite3`
  · `.wit/debate-con/acps/aip-tasks.sqlite3`
  · `.wit/fact-judge/acps/aip-tasks.sqlite3`

生成器：`tools/trace.py`（**不写入任何任务库**）
