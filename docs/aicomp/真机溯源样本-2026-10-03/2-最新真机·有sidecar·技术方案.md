### 结论溯源表（机制 5）

> ⚠️ 本文件由 `python -m tools.trace --md` **自动生成**，数据来自平台任务库。
> 生成时刻：2026-10-03T20:52:00+08:00　·　定局依据：sidecar verdict-20261003T101123Z.meta.json

#### 表 1 · 本次研判

| 项 | 值 |
|---|---|
| 输入原文 | 网传某学校食堂使用过期食材，学生堵门讨要说法，据称后厨画面是上周拍摄的。 |
| 局（session） | `session-1e61d97e-530d-453e-8cd8-815e5cb67691` |
| 总耗时 | 139.4 秒 |
| 参与 Agent | 4 个（debate-con、debate-pro、fact-judge、leader） |
| 跨智能体调用次数 | 3 |

#### 表 2 · 委托链（每一跳都可回任务库核对）

| # | 层 | 发起 → 接收 | 状态 | 耗时 | 产物 | 任务号 | 配对 |
|---:|---:|---|---|---:|---|---|---|
| 1 | 0 | leader → fact-judge | pending | 139.4s | — | `task_70293a5b8b8e` | 硬 |
| 2 | 1 | fact-judge → debate-pro | slow | 33.5s | product-task-2df69f2d-1e66-4fd2-8a79-8325ee186905 | `task_397e5bab84b8` | 硬 |
| 3 | 1 | fact-judge → debate-con | slow | 48.8s | product-task-71812c51-4f10-4d8e-8b9e-4065bfb49ac4 | `task_cbde3f64a51b` | 硬 |

#### 表 2b · 跨层归属（**全部为推断**，无协议级硬键）

| 上级任务号 | 下级会话 | 关系 | 证据 | 判据 |
|---|---|---|---|---|
| `task_70293a5b8b8e` | …20f85efa1e4f | parent-child | **推断** | fact-judge 的该会话起止时间落在 task_70293a5b8b8e 的生命周期 [2026-10-03T10:08:55.058536+00:00 .. 2026-10-03T10:11:14.453780+00:00] 之内；跨层无协议级硬键（外层 session ≠ 内层 session，实测） |

#### 表 3 · 结论字段溯源

| 结论字段 | 值 | 来源 | 证据强度 |
|---|---|---|---|
| `subject` | 网传某学校食堂使用过期食材，学生堵门讨要说法，据称后厨画面是上周拍摄的。 | 主控综合（链上无对应下游产物） | **推导** |
| `harm_index` | 10 | 主控综合（链上无对应下游产物） | **推导** |
| `band` | 低风险 | 主控综合（链上无对应下游产物） | **推导** |
| `is_incitement` | False | 主控综合（链上无对应下游产物） | **推导** |
| `truth_score` | 30 | 主控综合（链上无对应下游产物） | **推导** |
| `consistency_note` | 三项判定依据：①煽动性——原文为"网传/据称"转述句式，无号召、无指向性攻击，判为不构成煽动；②为真程度——含可核查断… | 主控综合（链上无对应下游产物） | **推导** |
| `conclusion` | 这段言论是对一条校园食品安全消息的转述，通篇以"网传""据称"框定，属于未经核实的公共议题传言。它没有号召任何行动、没… | 主控综合（链上无对应下游产物） | **推导** |
| `action_advice` | 建议平台进一步核查消息来源与后厨画面的原始拍摄时间，暂不据此扩散或定性。 | 主控综合（链上无对应下游产物） | **推导** |
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
python -m tools.trace --verdict /home/ubuntu/witrelate/output/verdict-20261003T101123Z.json --md
sqlite3 .wit/leader/acps/aip-tasks.sqlite3 \
  "SELECT task_ref,direction,remote_aic,state,created_at FROM aip_tasks WHERE session_id='session-1e61d97e-530d-453e-8cd8-815e5cb67691'"
```

本页数据文件：
  · `.wit/incitement/acps/aip-tasks.sqlite3`
  · `.wit/harm/acps/aip-tasks.sqlite3`
  · `.wit/leader/acps/aip-tasks.sqlite3`
  · `.wit/debate-pro/acps/aip-tasks.sqlite3`
  · `.wit/debate-con/acps/aip-tasks.sqlite3`
  · `.wit/fact-judge/acps/aip-tasks.sqlite3`

生成器：`tools/trace.py`（**不写入任何任务库**）
