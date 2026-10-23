# run/ · 探针与元验证（不是生产入口）

> 这里 95 个 `.py` 都是**验证/调查/样本**脚本，不是常驻服务。
> 生产入口见 [ARCHITECTURE.md](../ARCHITECTURE.md) §3。
> 新增脚本前先问：能不能放到 `tools/` 或 `legal_acps/<domain>/`；
> 如果确实是一次性调查，放 `attic/<年月>/`，不要放这里。

## 四类脚本

| 前缀/目录 | 是什么 | 怎么用 |
|---|---|---|
| `e2e_*.py` | **真机探针**：真的 discover、委派、带附件；`check_local` 的“真机探针”节按目录派生 | `python -m run.e2e_combined --brief` |
| `meta_verify_*.py` | **离线元验证**：证明某条判据/负控会失败，不需要平台 | `python -m run.meta_verify_negative_controls` |
| `probe_*.py` / `_probe_*.py` | 历史调查/一次性探针；结论已进 `docs/` | 只在复现旧结论时跑 |
| `*_samples.py` / `diag_*.py` / `gen_*.py` | 样本生成、诊断、文档生成 | 按文件头 docstring 跑 |

## 其他历史/一次性（11 个）

这些不带 `e2e_` / `meta_verify_` / `probe_` 前缀，但确实存在；**不是生产入口**，
按文件头 docstring 使用，不要再复制一份：

| 脚本 | 性质 | 说明 |
|---|---|---|
| `batch_stance_swap.py` | 批量实验 | #124 立场对调，批量版；调用 e2e 机制 |
| `check_meta_residue.py` | 元验证 | 元验证跑完后的残留核对 |
| `rehearse_experience.py` | 诊断/演练 | 把演练场实际输出原样打印，供人工看内容 |
| `remote_inbox_probe.py` | 探针 | 跨机部署后远端收件箱核对 |
| `samples.py` | 样本 | 图像合成样本构造函数，被 probe/diag 复用 |
| `verify_mcp_config.py` | 配置判据 | 本机 MCP 配置与 stdio 握手验证 |
| `_append_gt_batch.py` | 一次性数据 | 批量追加人工 GT |
| `_enrich_rulings.py` | 一次性数据 | 充实裁定经验库 |
| `_matrix_video.py` | 一次性样本 | 视频 `duration_mismatch` 样本矩阵 |
| `_relabel_staged_gt.py` | 一次性数据 | GT 重标 `c-staged-01` |
| `_verify_domain_fix.py` | 一次性验证 | 真机验证「不实」值域修复 |

## 台账口径

`.wit/leader/runs.jsonl` **每条 run 有两行**：start（`status=null`）与 finish，
两行共享同一个 `run_id`。按行数统计会翻倍；正确口径是
`unique(run_id)` 或只数 finish 行。`aip_tasks` 是平台任务库，不能和它混算。

## 常用入口

```bash
# 组合研判（主控同一条消息并发派证据官与裁判）
python -m run.e2e_combined --brief

# 快车道 A/B（库内命中 vs 库外新案）
python -m run.e2e_fast_lane

# 图像/音频/视频真机探针（需要对应服务与附件）
python -m run.e2e_leader_image
python -m run.e2e_audio_live
python -m run.e2e_video_live

# 负控集：证明“该拒的会拒”
python -m run.meta_verify_negative_controls

# MCP 配置（本机有配置才判；服务器上按设计跳过）
python run/verify_mcp_config.py
```

## 三条规则

1. **不要移动 `e2e_*.py`**：`check_local` 的“真机探针”节是
   `run/e2e_*.py` **按目录现场派生**的；移走 = 自检输入集合为空。
2. **不要新增 `run/` 脚本当生产入口**：常驻服务只走
   `legal_acps.run_agent` / `case_round` / `experience.server`。
3. **元验证必须能失败**：`meta_verify_*` 的职责是证明判据不是空转；
   只打印“通过”不算。

## 历史归档

已从仓根移入 [attic/2026-10/](../attic/2026-10/) 的一次性脚本：
`_probe_discover_migrated.py`、`_probe_xmachine_ledger.py`、
`step1..4`、`_remote_cmd*.sh` 等。需要时按归档说明取回。
