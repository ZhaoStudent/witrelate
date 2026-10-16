#!/usr/bin/env bash
# legal_acps —— 真实案例定期研判：唯一入口
#
#   bash scripts/case_round.sh                      按轮换挑一个案子，跑一轮 + 四项体检
#   bash scripts/case_round.sh --dry-run            只选案、只跑闸门；不起子进程、不落记录
#   bash scripts/case_round.sh --force              绕过两个闸门（忙 / 偏移）；体检照跑
#   bash scripts/case_round.sh --case c-food-01     指定案子（默认按轮换挑）
#   bash scripts/case_round.sh --help               其余参数透传给模块自己的 argparse
#
# 退出码（**跳过 ≠ 通过**，所以"没跑"必须是独立的一档）：
#   0  跑完，四项体检全过
#   1  跑完，但体检有未通过项
#   2  **没跑** —— 被闸门拦下（同角色已有轮次在跑 / 常驻服务刚起来）
#   3  环境性问题（案例库自检不过、找不到解释器等）
#
# 为什么要有一个 shell 包装，而不是让 systemd 直接调 python：
#   1. **解释器路径只在一个地方决定**。用错解释器会得到一堆 ImportError ——
#      本项目的托管 python 没装 wit，这里已经记过一条「假失败」的教训。
#      serve.sh / verify.sh 都是这么找解释器的，本脚本沿用同一套顺序。
#   2. **人手工跑与定时器跑必须是同一条命令**。两条各写一份的话，
#      "我手工跑明明没问题" 与 "定时器天天失败" 会长期并存，而原因恰恰在那点差别里。
#
# ⚠️ 本脚本**不启停任何服务、不改任何环境变量**：它只做"跑一轮 + 体检"。
#    常驻服务的启停归 serve.sh / systemd 管；两者**刻意分开** ——
#    在常驻进程里塞定时任务会让"平台看到这个 Agent 在线"变成假话。
#
# ⚠️ 唯一的调用方是 `acps-case-round.timer`（见 deploy/systemd/）。
#    手工补跑直接用本脚本即可；被闸门拦下时它会打印**为什么**，以及该怎么处理。

set -uo pipefail

# 仓库根 = 本脚本所在目录的上一级。CWD 必须是仓库根 —— 这不是便利：
# `run_agent` 的 workspace 是 CWD 相对的，而台账与会话库是仓库相对的，
# 两者只在 CWD == 仓库根 时重合（详见 legal_acps/case_round.py 模块头）。
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
PROJ="$(pwd)"

# 解释器发现顺序与 scripts/verify.sh 一致；命中即止。
PY=""
for c in ".venv/Scripts/python.exe" ".venv/bin/python" "python3" "python"; do
  if [ -x "$PROJ/$c" ]; then PY="$PROJ/$c"; break; fi
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done

if [ -z "$PY" ]; then
  echo "[case-round] 找不到可用的 python（找过 .venv/Scripts、.venv/bin 与 PATH）" >&2
  echo "[case-round] 先按部署文档建好 .venv；托管 python 没装 wit，用它只会得到 ImportError。" >&2
  exit 3
fi

# `exec` 让本脚本的退出码**就是**那一轮的退出码（0/1/2/3 一档都不丢）。
exec "$PY" -u -m legal_acps.case_round "$@"
