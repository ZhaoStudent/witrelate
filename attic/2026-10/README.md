# 2026-10 归档

这些文件在 2026-10-21 的结构收口中从仓库根移入，**不是生产入口**。

| 文件 | 性质 |
|---|---|
| `step1_basic_agent.py` | wit 教程示例 |
| `step2_capability.py` | wit 教程示例 |
| `step3_hooks.py` | wit 教程示例 |
| `step4_streaming.py` | wit 教程示例 |
| `_numbers.py` | 早期数字草稿 |
| `_snap_baseline.py` | 早期基线草稿 |
| `_reagg_batch.py` | 早期批量重聚合 |
| `_remote_cmd9.sh` / `10` / `11` | 早期远端手工命令 |
| `_probe_discover_migrated.py` | Discovery 迁移一次性探针 |
| `_probe_xmachine_ledger.py` | 跨机台账一次性探针 |
| `_tmp_latest.py` | 2026-10-21 临时任务库查询 |

需要复用时，先移回 `run/` 或 `tools/`，不要直接从 `attic/` import。
