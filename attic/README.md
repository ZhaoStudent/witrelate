# attic · 归档区

这里不是生产代码，也不在同步清单里。放的是**已经完成使命、但仍可能有史料价值**的
一次性脚本、教程步骤和临时工具。

## 规则

- **生产入口不在这里**：生产入口只有
  `legal_acps.run_agent`、`legal_acps.case_round`、`legal_acps.experience.server`、
  `scripts/serve.sh`、`deploy/systemd/*`；
- 这里的文件**不被任何模块 import**，也不参与 `tools.file_fingerprint` 同步；
- 如果某个脚本又要用，先把它移回 `run/` 或 `tools/`，补测试/说明，再提交；
- 不要把这里当“垃圾桶”：只收**有日期、有来源**的东西。

## 2026-10 批次

见 [2026-10/](2026-10/)。这一批主要是：

| 文件 | 原用途 | 为什么归档 |
|---|---|---|
| `step1_basic_agent.py` … `step4_streaming.py` | 早期 wit 教程的四个最小示例 | 已被 `legal_acps/` 的真实实现取代 |
| `_numbers.py` | 早期数字核对草稿 | 一次性 |
| `_snap_baseline.py` | 早期基线快照草稿 | 一次性 |
| `_reagg_batch.py` | 早期批量重聚合 | 一次性 |
| `_remote_cmd9/10/11.sh` | 早期远端手工命令 | 已由 `deploy/systemd/` + `scripts/` 接管 |
| `_probe_discover_migrated.py` | Discovery 迁移一次性探针 | 结论已进 `docs/`；路径保留在归档 |
| `_probe_xmachine_ledger.py` | 跨机台账一次性探针 | 结论已进 `docs/`；路径保留在归档 |
| `_tmp_latest.py` | 2026-10-21 查任务库的临时脚本 | 一次性 |
| `WirAgent_test.py` | 早期 wit 教程的 `text-assistant` 示例 | 不是项目测试；文件名 `_test.py` 也不会被 pytest 收集 |

> 上线手册里如果还写着“仓根 `_probe_*.py`”，请按
> `attic/2026-10/` 的同名文件找；命令本身不变。
