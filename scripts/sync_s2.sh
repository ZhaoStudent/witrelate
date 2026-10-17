#!/usr/bin/env bash
# sync_s2.sh —— 代码同步 s1 → s2（#125 双机部署后的固定纪律）
#
# 用法（本机 Git Bash，任何目录均可）：
#   bash <仓库路径>/scripts/sync_s2.sh            # 全量推送 + md5 终审
#   bash <仓库路径>/scripts/sync_s2.sh --check    # 只做 md5 终审，不推送
#
# 通道（2026-10-14 实测验证）：本机双跳中转只有 ~130KB/s 且撞超时；
# ssh-agent 转发让 s1 直推 s2（香港内网，秒级）。
#
# 固化自 #125 首迁的纪律（硬规矩 8 全文适用）：
#   · 单连接 tar（一条管道推完，不开并发 SSH）
#   · 🔴 .env 每机一份且 ACPS_PUBLIC_HOST 机器相关 —— **永远不同步 .env**
#   · 排除名单只有一份（EXC_DIRS/EXC_FILES），tar 与 find 都从它生成
#   · md5 清单现场派生（不落仓库）、LC_ALL=C、**全量 diff 文件名与内容**
#     （只比条数会漏——10-13 教训：「同名文件内容不同」也要点出名）
#   · 文件字节原样传输（tar 不做换行转换；仓库纪律纯 LF）
#
# 退出码：0 = 推送完成且终审一致（--check：当前一致）；1 = 有差异或失败。

set -euo pipefail

KEY="${ACPS_KEY:-$HOME/.ssh/acps-hk-key.pem}"
S1="${ACPS_S1:-ubuntu@150.109.154.16}"
S2="${ACPS_S2:-ubuntu@101.33.124.181}"

# ── 排除名单（唯一事实源；tar 与 find 两处都从这里生成）────────────────
# 目录：运行时/身份数据/本机 git 元数据
EXC_DIRS=(.venv .git .wit .wit-local .wit-t output output_probes logs backups __pycache__)
# 文件：密钥与账号（🔴 .env 永不同步）+ s1 仓根 Oct-7 死副本（零引用已实证）
EXC_FILES=(.env .env.bak.* ACCOUNT.txt agents.py calibration.py contracts.py)

TAR_EXCLUDES=""
for d in "${EXC_DIRS[@]}"; do TAR_EXCLUDES+=" --exclude=./$d"; done
TAR_EXCLUDES+=" --exclude=./.wit.bak-*"   # 历史备份目录（glob 形态）
for f in "${EXC_FILES[@]}"; do TAR_EXCLUDES+=" --exclude=./$f"; done

FIND_PRUNE="-name ${EXC_DIRS[0]}"
for d in "${EXC_DIRS[@]:1}"; do FIND_PRUNE+=" -o -name $d"; done
FIND_PRUNE+=" -o -name '.wit.bak-*'"
FIND_SKIP=""
for f in "${EXC_FILES[@]}"; do FIND_SKIP+=" ! -name '$f'"; done

manifest() {  # $1 = ssh 目标；输出 <md5>  <文件名> 清单（LC_ALL=C 排序）
  ssh -i "$KEY" -o BatchMode=yes "$1" \
    "cd ~/witrelate && find . \\( $FIND_PRUNE \\) -prune -o -type f $FIND_SKIP -print \
     | LC_ALL=C sort | LC_ALL=C xargs -r md5sum"
}

echo "== sync_s2: $S1 → $S2（$(date '+%F %T')）=="

if [ "${1:-}" != "--check" ]; then
  echo "== 1/3 推送（s1 直推 s2，agent 转发；单连接 tar）=="
  eval "$(ssh-agent -s)" >/dev/null
  trap 'ssh-agent -k >/dev/null 2>&1 || true' EXIT
  ssh-add "$KEY" 2>/dev/null || true
  ssh -A -i "$KEY" -o BatchMode=yes "$S1" \
    "cd ~/witrelate && tar czf -$TAR_EXCLUDES . \
     | ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new '$S2' \
       'cat > /tmp/witrelate-sync.tgz'"
  ssh -i "$KEY" -o BatchMode=yes "$S2" \
    'mkdir -p ~/witrelate && tar xzf /tmp/witrelate-sync.tgz -C ~/witrelate \
     && echo "   解压完成"'
fi

echo "== 2/3 双侧 md5 清单（现场派生）=="
manifest "$S1" > /tmp/sync_s2_s1.md5
manifest "$S2" > /tmp/sync_s2_s2.md5
n1=$(wc -l < /tmp/sync_s2_s1.md5)
n2=$(wc -l < /tmp/sync_s2_s2.md5)
echo "   s1: $n1 个文件 ｜ s2: $n2 个文件"

echo "== 3/3 全量 diff 文件名与内容 =="
if diff /tmp/sync_s2_s1.md5 /tmp/sync_s2_s2.md5; then
  echo "✅ 两端 $n1 个文件逐字节一致（$(date '+%F %T')）"
  exit 0
fi
echo "❌ 存在差异（见上方 diff：< 侧 = s1，> 侧 = s2）。"
echo "   两边文件数不等先归因（谁多了/少了什么），再决定重推或补单文件。"
exit 1
