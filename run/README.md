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
