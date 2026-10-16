# ADP Discovery 索引复查报告：「网络裁判」

- **执行时间**：2026-09-30 16:46 ~ 16:52（GMT+8）
- **目标角色**：网络裁判
- **AIC**：`1.2.156.3088.1.BUPT.81M3FX.83WI6B.TF6QRX.0E91`
- **服务端点**：`https://150.109.154.16:8448`（端口 8448，服务 active）
- **平台后端**：`ACPS_BACKEND=wt` → `https://wt.ioa.pub/discovery/acps-adp-v2/discover`
- **执行方式**：服务器 `~/witrelate` 下 `./.venv/bin/python` + `wit.acps.discovery.AcpsDiscoveryClient`
  （调用范式对齐 `tools/check_discovery_pollution.py`）；全程只读，未改动服务与代码。

---

## 一、结论

| 检查项 | 结果 |
| --- | --- |
| ①「网络裁判」能否被 ADP discovery 检索到 | **能 ✅**（5 个查询词中命中 4 个） |
| ② `output/` 下是否新增 `ruling-*.json` | **是 ✅** —— `ruling-20260930T085009Z.json`（3182 B，16:50:09） |
| ③ leader 最终结论是否引用裁判的裁定结论 | **否 ❌** —— 写的是「任务状态为失败、无产品交付」 |

**一句话**：15:40 的「检索不到」已确认是平台侧**新增索引异步重建延迟**（本次实测延迟约 1 小时量级），
现已建好；但复验暴露出**另一个独立问题**——裁定书实际已产出并落盘，leader 侧却判定委派失败、未引用裁定。

---

## 二、Discovery 检索明细（2026-09-30 16:47）

前缀 `1.2.156.3088.1.BUPT.81M3FX` 为本账号；其余为外部团队 Agent。

| 查询词 | 命中数 | 「网络裁判」是否召回 |
| --- | --- | --- |
| `网络裁判` | 10 | ✅ |
| `事实核查` | 12 | ✅ |
| `事实核查 网络裁判 裁定` | 2 | ✅ |
| `网络裁判 裁定` | 6 | ❌ |
| `裁判` | 1 | ✅（唯一命中） |

### 全部召回结果（去重后 21 条）

**本账号（前缀 `…BUPT.81M3FX`）：2 条**

| name | aic |
| --- | --- |
| **网络裁判** | `1.2.156.3088.1.BUPT.81M3FX.83WI6B.TF6QRX.0E91` |
| 煽动性识别 Partner | `1.2.156.3088.1.BUPT.81M3FX.COUJXB.F9XC6K.0J6D` |

**外部团队（19 条，非本账号前缀）**

| name | aic |
| --- | --- |
| 认知辩论·决策编排智能体 | `1.2.156.3088.1.BUPT.VGE19L.ZPC3MO.0.0ODW` |
| 认知辩论·通用辩论智能体 | `1.2.156.3088.1.BUPT.VGE19L.2RGM1Y.0.09V7` |
| 认知辩论·事实核查智能体 | `1.2.156.3088.1.BUPT.VGE19L.BE0FQ9.0.09LT` |
| 论文评审与学术写作智能体 | `1.2.156.3088.1.BUPT.L2K8YU.Q8JP6H.1DNDU6.190G` |
| 事件时间线与背景梳理智能体 | `1.2.156.3088.1.BUPT.L2K8YU.QM4M99.3USBVM.0GOD` |
| 科研智能体协作工作台 | `1.2.156.3088.1.BUPT.L2K8YU.7LFQWP.QT8L11.1BF9` |
| 信息来源与事实核验智能体 | `1.2.156.3088.1.BUPT.L2K8YU.JWCQL2.8DFGI8.0FKI` |
| 研究设计与数据分析智能体 | `1.2.156.3088.1.BUPT.L2K8YU.7AS8QU.INQ4LY.0WL6` |
| 新闻研判与简报协调智能体 | `1.2.156.3088.1.BUPT.L2K8YU.6MG2PD.KYH34C.16L0` |
| 众擎Agent | `1.2.156.3088.1.BUPT.G4J19Z.C7NFHV.OXDJOU.1BIS` |
| 研枢-Critic-Partner | `1.2.156.3088.1.BUPT.VTF97R.KZIPMP.JG4YTQ.02JY` |
| 证链联研总控智能体 | `1.2.156.3088.1.BUPT.V2OEMK.L1BJZI.6SHRCD.02P7` |
| 证链联研总控智能体 | `1.2.156.3088.1.BUPT.V2OEMK.8F6Q2P.5DFSEW.04FV` |
| 证链来源核验智能体 | `1.2.156.3088.1.BUPT.V2OEMK.SNH8OB.O2SC18.01G5` |
| 证链来源核验智能体 | `1.2.156.3088.1.BUPT.V2OEMK.RBR7U4.BGSK3N.11J3` |
| 证链冲突侦测智能体 | `1.2.156.3088.1.BUPT.V2OEMK.RHH3AY.XKKQ77.0J6F` |
| 证链冲突侦测智能体 | `1.2.156.3088.1.BUPT.V2OEMK.V2F9QQ.WFZS7M.0A28` |
| 证链证据分级智能体 | `1.2.156.3088.1.BUPT.V2OEMK.YKM8PA.K2O2EJ.13RI` |
| 证链证据分级智能体 | `1.2.156.3088.1.BUPT.V2OEMK.W12RL4.G7QDHG.0MLO` |

> 已删除的 3 个 demo Agent（`…F56BVO.0QYM` / `…IYKGM1.0X32` / `…9D5VMV.1DZ8`）**未再出现**，
> discovery 无污染残留。

---

## 三、两级嵌套端到端复验（16:49 ~ 16:50）

命令：

```bash
./.venv/bin/python -m legal_acps.run_agent leader \
  --input "请对这条网传信息做事实核查并发起辩论：网传某学校食堂使用过期食材，学生堵门讨要说法，据称后厨画面是上周拍摄的。"
```

- leader 侧：`status = completed`，**迭代 7 轮**，退出码 0。
- 工具调用轨迹：`acps.discover` ×3 → `acps.delegate` ×3 → `acps.task` ×4 → `legal.issue_verdict` ×1
  （全部 success）。leader 的 AIC `…81M3FX.X1J5R5.HKBBAM.018M`，注册状态 active。
- 结构化结论落盘：`output/verdict-20260930T085028Z.json`

### 检查项 ①：`output/` 是否新增 `ruling-*.json` → **是**

| 文件 | 大小 | 时间 | 说明 |
| --- | --- | --- | --- |
| `output/ruling-20260930T085009Z.json` | 3182 B | **16:50:09（本次新增）** | 目标产物 |
| `output/ruling-20260930T074432Z.json` | 2917 B | 15:44（旧，15:40 那次测试留下的） | 对比用 |

新增裁定书内容**完整且合规**，证明确实走通了「裁判 → 辩手」的内层嵌套：

- `verdict`：`无法判定`，`confidence`：0.3
- `basis` 明确标注引用了 **「正方第 1 轮论证（acps_delegate 返回原文）」** 与
  **「反方第 1 轮反驳（acps_delegate 返回原文）」** —— 内层两级嵌套的辩论链路真实执行过。
- 另含 `disputed_points`（4 条）、`reasoning`（采证八项材料全部未获取到，双方均无一手证据，
  「零对零」→ 无法判定）、`disclaimer`。

### 检查项 ②：leader 结论是否引用裁判裁定 → **否**

leader 最终文本第四节原文：

> **四、辩论裁定说明**
> 您要求的辩论分支已尝试委派"网络裁判"组织正反双方辩论，但该任务**未能产出裁定书（任务状态为失败、无产品交付）**，
> 特此如实说明，未以任何编造内容补足。

- leader **未**写「未找到网络裁判」（说明 discovery 问题确已解决，检索到了目标）。
- 但它**也没有引用任何裁定内容**：`verdict-20260930T085028Z.json` 中「裁判」仅出现 1 次、
  「裁定」3 次，全部来自上面这段「失败说明」，`consistency_note` 里没有任何裁定结论。

---

## 四、遗留问题（已排除索引原因）

**现象**：裁判侧已把裁定书写入 `output/`（16:50:09），leader 侧却在同一时刻把该任务判为
**失败/无产品交付**（verdict 16:50:28）。⇒ 问题不在 discovery 索引，而在**两级嵌套的「任务状态回传 / 等待」环节**。

**已排查**：
- 裁定书内容完整、时间戳落在本次运行窗口内 → 裁判进程确实执行并交付了。
- `logs/fact-judge.log`、`logs/leader.log` 最后写入时间均为 **15:48**，本次运行窗口内**无新增日志**。
- `journalctl -u fact-judge` / `-u leader --since 16:44` 均为 `No entries`（该时段无 journal 记录）。
- 15:44 的旧裁定书 `ruling-20260930T074432Z.json` 同样存在，而当时 leader 报「未找到网络裁判」——
  说明「裁定书落盘」与「leader 侧任务状态」早就不一致，本次只是让它更明显。

**待查方向（未执行，等指示）**：
1. `acps_task(action="wait")` 的等待窗口与裁判嵌套辩论耗时是否匹配（`request_timeout_seconds=90`，
   内层要再跑一轮 discover + 双方委派 + 等待，可能被判定超时）。
2. 裁判完成任务后的**返回载荷（DTO）**是否真的包含裁定内容，还是只落盘不返回。
3. 定位两个服务真正的日志落点（`logs/` 与 journald 均无记录，可能在 `run/` 或 systemd 单元配置的别处）。
