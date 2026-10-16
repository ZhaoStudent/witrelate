#!/usr/bin/env bash
# legal_acps —— systemd 托管安装/卸载脚本
#
#   bash scripts/systemd-install.sh install        安装并启用开机自启（幂等，可重复跑）
#   bash scripts/systemd-install.sh uninstall      停用并移除
#   bash scripts/systemd-install.sh verify         只做验收检查，不改动
#   bash scripts/systemd-install.sh case-install   另装「真实案例定期研判」的定时器
#   bash scripts/systemd-install.sh case-uninstall 只卸那个定时器
#
# ⚠️ 为什么案例定时器**不并进 install**（2026-10-05 的取舍）：
#    `install` 现在装的是"平台要不要调用我们"所必需的全部常驻角色（`ROLES`）；
#    而案例定时器是**我们主动出网攒访问量**的东西 —— 两件事的风险面完全不同
#    （前者只是监听，后者会真的登录 Registry、派发任务、花模型额度）。
#    并进去的话，任何一次"重装服务"都会顺手把定时任务也打开，而那时候往往正在
#    排查故障 —— 在最不该加变量的时刻加了一个变量。所以它**单独一条命令**。
#
# 为什么要用 systemd 而不是 nohup：
#   nohup 起的进程在服务器重启、被 OOM 杀掉、或 SSH 断开异常后不会自己回来。
#   比赛要求服务长期在线（应用成效 / 访问频次都依赖真实访问量），
#   而访问量是时间的函数 —— 停一小时就永久少一小时。
#
# 关键设计（两个坑都在代码注释里写明了，别顺手改掉）：
#   1. ExecStartPre=/bin/sleep N  —— 按角色错开启动（N 值见各单元；两两不同是硬要求）。
#      多个进程并发登录 Registry 会互相顶掉会话（实测），必须串行。
#   2. 不配 EnvironmentFile      —— .env 由 run_agent.py 自己 load_env() 读，
#      交给 systemd 解析反而多一个失败点。

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
PROJ="$(pwd)"
UNIT_SRC="$PROJ/deploy/systemd"
UNIT_DST="/etc/systemd/system"
# 顺序 = 错开顺序（见各单元里的 ExecStartPre）
#
# ⚠️ `ALL_ROLES` 必须与 `acps_config.PORTS` **完全一致**（它回答的是"本项目一共
#    定义了哪些角色"）；`ROLES` 是**部署清单** = 全部 − 软废弃。
#    check_local 3b 守着这个等式：ALL_ROLES == SOFT_RETIRED ⊎ ROLES。
ALL_ROLES=(incitement harm leader debate-pro debate-con fact-judge evidence-image evidence-leader evidence-audio evidence-video law-advisor lead-intake)

# ── 软废弃（2026-10-02）────────────────────────────────────────────────────
# `incitement` / `harm` **刻意不部署**：代码与平台注册都保留（重新启用时不必
# 重走注册与审核），但单元不装、统一启停不碰、验收不判失败（见 do_verify 的
# "已停用"分流）。单一事实源：`legal_acps/acps_config.SOFT_RETIRED`。
SOFT_RETIRED=(incitement harm)
ROLES=()
for _role in "${ALL_ROLES[@]}"; do
  for _dead in "${SOFT_RETIRED[@]}"; do
    [ "$_role" = "$_dead" ] && continue 2
  done
  ROLES+=("$_role")
done
unset _role _dead
UNITS=("${ROLES[@]/#/acps-}")
UNITS=("${UNITS[@]/%/.service}")
LOGDIR="$PROJ/logs"

# ── 真实案例定期研判（#27，2026-10-05）────────────────────────────────────
# ⚠️ 这两个名字**不是角色**，也**不进 `UNITS`**：
#    `UNITS` 是"平台会调用到的常驻角色"，它的每一项都要对应一个端口与一份 ACS；
#    而案例定时器是一个 oneshot 任务，不绑端口、不被平台调用。
#    把它塞进 `UNITS` 会同时污染 do_verify 的表格（列里没有它的位置）与
#    "每个单元启动的必须是它自己那个角色"这条断言（`check_local` 3b ④）。
CASE_SERVICE="acps-case-round.service"
CASE_TIMER="acps-case-round.timer"
CASE_LOG="$LOGDIR/case-round.log"

c_g() { printf '\033[32m%s\033[0m\n' "$*"; }
c_y() { printf '\033[33m%s\033[0m\n' "$*"; }
c_r() { printf '\033[31m%s\033[0m\n' "$*"; }

need_root() {
  if ! sudo -n true 2>/dev/null; then
    c_r "这个脚本需要免密 sudo。请先配置，或改用 root 执行。"
    exit 1
  fi
}

# ⚠️ 角色 → 端口**不在这里维护第二份**：`acps_config.PORTS` 是唯一事实源
#    （理由同 serve.sh —— 写死的副本已经漏过一次 evidence-image）。
ROLE_PORTS="$(./.venv/bin/python -c 'from legal_acps.acps_config import PORTS; print(" ".join(f"{k}={v}" for k, v in PORTS.items()))' 2>/dev/null)"
[ -n "$ROLE_PORTS" ] || c_y "  [警告] 无法从 acps_config.PORTS 派生端口表，端口列会显示为空"

port_of() {
  local pair
  for pair in $ROLE_PORTS; do
    if [ "${pair%%=*}" = "$1" ]; then echo "${pair##*=}"; return 0; fi
  done
  echo ""
}

# 这个角色是否**已注册到平台**（判据：存在已提交的 ACS，即 acs.final.json）。
#
# ⚠️ 为什么用"是否注册"而不是再写一份"应该部署哪些角色"的清单：
#    平台只能调用**已注册**的角色 ⇒ "未注册 ⇒ 这台机器上不该跑它"是一条
#    可以从现场推导出来的事实。而再维护一份应部署清单，就又多了一处
#    "同一事实散在多处"（PORTS / serve.sh / systemd-install.sh / 自检 已经是 4 处了）。
#
#    2026-10-01 实测：evidence-image（8449）建好了但**尚未注册**，
#    于是本脚本的验收检查**永远**报"有检查项未通过" —— 永久假警报会让
#    真失败淹没在里面，比没有看板更糟（与 verify.sh 曾把角色表写死 5 个同类）。
is_registered() {
  [ -f "$PROJ/.wit/$1/acps/acs.final.json" ]
}

# 干掉可能残留的 nohup 进程。
# 注意模式写成 'legal_acps[.]run_agent'：方括号让 pgrep/pkill 自身不会匹配到
# 自己所在的命令行（否则 pkill 会把自己的 shell 一起杀掉）。
kill_strays() {
  if pgrep -f 'legal_acps[.]run_agent' >/dev/null 2>&1; then
    c_y "发现残留的 run_agent 进程，先停掉："
    pgrep -af 'legal_acps[.]run_agent' | sed 's/^/    /'
    pkill -f 'legal_acps[.]run_agent' || true
    sleep 3
    pkill -9 -f 'legal_acps[.]run_agent' 2>/dev/null || true
  fi
  rm -f "$PROJ"/run/*.pid 2>/dev/null || true
}

# 备好**一份**日志文件：确保存在、归 ubuntu、权限 644。
#
# ⚠️ systemd 的 `StandardOutput=append:<path>` 是**以单元自己的身份（User=ubuntu）**
#    打开文件的。文件属主不对（比如被 root 建出来）会让单元在启动那一刻直接失败，
#    而屏幕上只有一行 `Failed to open ... Permission denied` —— 与"业务出错"完全不同
#    的一类原因，却长得同样短。所以这里先把属主与权限摆正。
#
# 参数 2 = 是否归档旧内容（1 = 归档并留 `.bak`，0 = 保留追加）。
prepare_one_log() {
  local name="$1" archive="${2:-0}"
  mkdir -p "$LOGDIR"
  if [ "$archive" = "1" ] && [ -s "$LOGDIR/$name.log" ]; then
    mv "$LOGDIR/$name.log" "$LOGDIR/$name.log.$(date +%Y%m%dT%H%M%S).bak"
  fi
  touch "$LOGDIR/$name.log"
  chown ubuntu:ubuntu "$LOGDIR/$name.log" 2>/dev/null || true
  chmod 644 "$LOGDIR/$name.log"
}

prepare_logs() {
  local role
  for role in "${ROLES[@]}"; do
    # 冷启动把旧日志归档，保证这次验收看到的是全新输出
    prepare_one_log "$role" 1
  done
}

do_install() {
  need_root

  echo "== 1/5 停掉可能存在的旧进程（nohup / serve.sh 管理的）=="
  kill_strays
  c_g "    ok"

  echo "== 2/5 安装单元文件到 $UNIT_DST =="
  local u
  for u in "${UNITS[@]}"; do
    if [ ! -f "$UNIT_SRC/$u" ]; then
      c_r "   找不到源文件 $UNIT_SRC/$u"; exit 1
    fi
    sudo install -m 644 -o root -g root "$UNIT_SRC/$u" "$UNIT_DST/$u"
    echo "   $u"
  done
  sudo install -m 644 -o root -g root "$UNIT_SRC/acps.logrotate" /etc/logrotate.d/acps
  echo "   acps.logrotate -> /etc/logrotate.d/acps"

  echo "== 3/5 准备日志文件（systemd 以 ubuntu 身份 append）=="
  prepare_logs
  c_g "    ok"

  echo "== 4/5 daemon-reload + enable（开机自启）=="
  sudo systemctl daemon-reload
  sudo systemctl enable "${UNITS[@]}" 2>&1 | sed 's/^/   /'
  sudo systemctl restart logrotate.service 2>/dev/null || true

  # ⚠️ 这里原本写死"5/5 启动（错开 0/12/24/36/48/60 秒）" —— 加了角色之后
  #    它就成了一句过期事实。数量与耗时**从现场派生**，不再抄一份。
  echo "== 5/5 启动（各单元按自身 ExecStartPre 错开；全部就绪约需 $(( (${#UNITS[@]} - 1) * 12 )) 秒）=="
  sudo systemctl restart "${UNITS[@]}"
  c_g "    已下发启动指令"
}

do_uninstall() {
  need_root
  echo "停用并移除 systemd 单元……"
  sudo systemctl disable --now "${UNITS[@]}" 2>&1 | sed 's/^/   /'
  local u
  for u in "${UNITS[@]}"; do
    sudo rm -f "$UNIT_DST/$u"
  done
  # 兼容早期只装了三个单元的版本
  sudo rm -f "$UNIT_DST/acps-incitement.service" "$UNIT_DST/acps-harm.service" "$UNIT_DST/acps-leader.service"
  # ⚠️ 案例定时器**一定要一起拿走**：它每天会真的登录 Registry、派发任务。
  #    留着它 = 留下一个"服务已经卸了、但每天还在对着空气跑一轮"的定时任务，
  #    而它的失败会以"某某角色连不上"的形式出现 —— 指向的是**已经不存在的东西**。
  if systemctl cat "$CASE_TIMER" >/dev/null 2>&1 || [ -f "$UNIT_DST/$CASE_TIMER" ]; then
    echo "   （顺带移除案例定时器 $CASE_TIMER）"
    do_case_uninstall
  fi
  sudo rm -f /etc/logrotate.d/acps
  sudo systemctl daemon-reload
  c_g "已移除。服务不再开机自启。"
}

# ── 真实案例定期研判的定时器（单独安装，见文件头的取舍说明）──────────────

do_case_install() {
  need_root

  echo "== 1/4 安装案例定时器的单元文件到 $UNIT_DST =="
  local f
  for f in "$CASE_SERVICE" "$CASE_TIMER"; do
    if [ ! -f "$UNIT_SRC/$f" ]; then
      c_r "   找不到源文件 $UNIT_SRC/$f"; exit 1
    fi
    sudo install -m 644 -o root -g root "$UNIT_SRC/$f" "$UNIT_DST/$f"
    echo "   $f"
  done

  echo "== 2/4 准备日志文件（systemd 以 ubuntu 身份 append）=="
  # **不归档**：这份日志是"每天一轮"的流水，历史本身就是它的价值。
  # 只留最近一次的话，"这周到底跑成过几次"就查不出来了。按天轮转交给 logrotate
  # （`acps.logrotate` 的 `logs/*.log` 通配已经覆盖到它）。
  prepare_one_log "case-round" 0
  c_g "    ok"

  echo "== 3/4 daemon-reload + 启用定时器（**只 enable timer**）=="
  # ⚠️ 只 enable **timer**，不 enable service：service 单元刻意没有 `[Install]` 段
  #    （见单元文件里的说明）—— 排期归 timer，别让它在开机时跑一轮。
  sudo systemctl daemon-reload
  sudo systemctl enable --now "$CASE_TIMER" 2>&1 | sed 's/^/   /'

  echo "== 4/4 下一次触发时刻 =="
  systemctl list-timers "$CASE_TIMER" --no-pager 2>/dev/null | sed 's/^/   /' || true
  c_g "    已启用。要立刻补跑一轮：bash scripts/case_round.sh"
  echo "    ⚠️ 被闸门拦下时会以退出码 2 结束（**不算失败**，但也不等于通过）——"
  echo "       原因写在 $CASE_LOG 与本轮的 case_rounds.jsonl 里。"
}

do_case_uninstall() {
  need_root
  echo "停用并移除案例定时器……"
  sudo systemctl disable --now "$CASE_TIMER" 2>&1 | sed 's/^/   /' || true
  # 正在跑的那一轮会被一起停掉（Type=oneshot 默认 KillMode=control-group 会波及子进程）
  sudo systemctl stop "$CASE_SERVICE" 2>/dev/null || true
  sudo rm -f "$UNIT_DST/$CASE_TIMER" "$UNIT_DST/$CASE_SERVICE"
  sudo systemctl daemon-reload
  c_g "已移除。**${#ROLES[@]} 个常驻角色服务不受影响**（它们与本定时器之间没有任何依赖）。"
  echo "   日志保留在 $CASE_LOG：它含每一轮的结论，是答辩材料的一半，别顺手删。"
}

do_verify() {
  local fail=0 role port st
  #: 真正检查了的角色数（不含"未部署"的）—— 结论行不能再写 `ROLES` 的数量，
  #: 那会把 6 个已注册角色说成 7 个（同一处硬编码，2026-10-01 一并修掉）。
  local deployed=0
  local not_deployed=()
  echo "===== 验收检查 ====="
  echo
  printf '  %-12s %-14s %-12s %-10s %s\n' "角色" "systemd" "开机自启" "端口" "常驻内存"
  # ── 软废弃：**先打印、直接跳过判定** ────────────────────────────────────
  # 它们**仍注册在平台上**（所以不属于下面"未注册 ⇒ 未部署"那一类），但刻意不跑。
  # 不把这条分流写出来的话，`st != active` 会让 fail=1 —— 每跑一次 verify 就报一次
  # "有检查项未通过"。**永久假警报会让真失败淹没在里面，比没有看板更糟**
  # （同类教训：2026-10-01 未注册的 evidence-image 曾让本函数永远报失败）。
  local dead
  for dead in "${SOFT_RETIRED[@]}"; do
    printf '  %-12s %-14s %-12s %-10s %s\n' "$dead" "已停用" "软废弃" "-" "–"
  done
  for role in "${ROLES[@]}"; do
    # ⚠️ 单元未安装要**先分流**，别直接进下面那三项判定：
    #    未注册的角色本来就不该部署，把它算成"未通过"是永久假警报。
    #    但**已注册**的角色单元不见了必须报失败 —— 那说明平台会调用到一个不在线的角色。
    if ! systemctl cat "acps-$role.service" >/dev/null 2>&1; then
      if is_registered "$role"; then
        printf '  %-12s %-14s %-12s %-10s %s\n' "$role" "未安装" "-" "无监听" "–"
        c_r "     ↑ 已注册到平台，却没有安装 systemd 单元 —— 平台会调用到一个不在线的角色"
        fail=1
      else
        not_deployed+=("$role")
        printf '  %-12s %-14s %-12s %-10s %s\n' "$role" "未部署" "未注册" "-" "–"
      fi
      continue
    fi
    deployed=$((deployed + 1))
    port="$(port_of "$role")"
    # ⚠️ 不能用 `$(systemctl ... || echo unknown)`：systemctl is-enabled 对 disabled
    #    的单元**既把状态打到 stdout、又返回非零**，于是 `||` 分支也执行 ——
    #    变量里成了两行（"disabled" + "unknown"），表格被撑成两行，
    #    且 `[ "$en" = "enabled" ]` 必然为假 → 看板凭空报"未通过"。
    #    （2026-09-30 实测：fact-judge 未 enable 时就是这么误报的）
    #    正确写法：取首行；首行为空才兜底。
    st="$(systemctl is-active "acps-$role.service" 2>/dev/null | head -n1)"
    [ -n "$st" ] || st="unknown"
    local en
    en="$(systemctl is-enabled "acps-$role.service" 2>/dev/null | head -n1)"
    [ -n "$en" ] || en="unknown"
    local mem="" pid
    pid="$(systemctl show -p MainPID --value "acps-$role.service" 2>/dev/null | head -n1)"
    [ -n "$pid" ] || pid=0
    if [ -n "${pid:-}" ] && [ "$pid" != "0" ] && kill -0 "$pid" 2>/dev/null; then
      local kb
      kb="$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')"
      [ -n "$kb" ] && mem="$(( kb / 1024 )) MiB"
    fi
    local pst
    if ss -ltn 2>/dev/null | grep -q ":$port "; then pst="监听中"; else pst="无监听"; fail=1; fi
    [ "$st" = "active" ] || fail=1
    [ "$en" = "enabled" ] || fail=1
    printf '  %-12s %-14s %-12s %-10s %s\n' "$role" "$st" "$en" "$pst" "${mem:-–}"
  done
  echo
  echo "  --- 监听端口实测 ---"
  # 注意：`|| echo` 后面必须另起一行再写下一句。早期把两个 echo 挤在一行，
  # 第二个 `echo` 被当成第一个 echo 的**位置参数**，于是真的输出 "    (无) echo"。
  #
  # ⚠️ 端口模式**现场派生**。原来是写死的 `:844[3-9]` —— 它只覆盖 8443..8449，
  #    把 8450/8451/8452（evidence-leader / evidence-audio / evidence-video）
  #    整个漏在这一段之外。而"看板少一列"比"看板报错"难发现得多：它看起来
  #    一切正常，直到有人真的去数。（2026-10-05 修。同类教训：本脚本 `is_registered`
  #    那一段记着"再维护一份清单就又多一处散落的事实"；这里则是**抄了一份却不完整**
  #    —— 比"没有清单"更难发现，因为它看上去是有依据的。）
  local port_re="" _pair
  for _pair in $ROLE_PORTS; do
    port_re="${port_re:+$port_re|}:${_pair##*=}([^0-9]|$)"
  done
  if [ -n "$port_re" ]; then
    ss -ltn 2>/dev/null | grep -E "$port_re" | sed 's/^/    /' || echo "    (无)"
  else
    ss -ltn 2>/dev/null | sed 's/^/    /' || echo "    (无)"
  fi
  echo
  echo "  --- 启动器日志关键行（各角色）---"
  for role in "${ROLES[@]}"; do
    echo "    [$role]"
    if [ -f "$LOGDIR/$role.log" ]; then
      grep -aiE "serving|active|state|blocked|error|Traceback|Could not validate" "$LOGDIR/$role.log" \
        | tail -n 4 | sed 's/^/      /'
      [ -s "$LOGDIR/$role.log" ] || echo "      (日志为空)"
    else
      echo "      (无日志文件)"
    fi
  done
  echo
  echo "  --- 常驻内存合计 ---"
  free -m | head -2 | sed 's/^/    /'
  echo
  # ── 真实案例定期研判（#27）────────────────────────────────────────────────
  # ⚠️ 这一节**不进 `fail`**：定时器是"我们主动攒访问量"的可选件，没装不是部署缺陷。
  #    但装了就一定要看得见 —— 它每天会真的登录 Registry、派发任务、花模型额度，
  #    是这台机器上**唯一会主动出网干活**的东西。看不见它就等于看不见一个变量。
  echo "  --- 真实案例定期研判（每轮结果在 case_rounds.jsonl）---"
  if systemctl cat "$CASE_TIMER" >/dev/null 2>&1; then
    local tst ten
    tst="$(systemctl is-active "$CASE_TIMER" 2>/dev/null | head -n1)"
    ten="$(systemctl is-enabled "$CASE_TIMER" 2>/dev/null | head -n1)"
    [ -n "$tst" ] || tst="unknown"
    [ -n "$ten" ] || ten="unknown"
    echo "    定时器 $CASE_TIMER：$tst / $ten"
    systemctl list-timers "$CASE_TIMER" --no-pager 2>/dev/null | head -n2 | sed 's/^/    /'
    if [ -s "$CASE_LOG" ]; then
      # 只捞"结论类"的行：一轮的日志里还有轮换表与子进程输出，全打出来会把结论淹掉。
      grep -aE '^\[case-round\] |总判定|原因：|\[失败\]' "$CASE_LOG" | tail -n 8 | sed 's/^/    /' || true
    else
      echo "    （还没有日志：$CASE_LOG —— 装完之后还没跑过）"
    fi
  else
    c_y "    未安装。装：bash scripts/systemd-install.sh case-install"
    c_y "    （为什么它不并进 install：见本脚本文件头的说明）"
  fi
  echo
  if [ "$fail" = "0" ]; then
    c_g "✅ ${deployed} 个已注册角色：systemd active + 开机自启 enabled + 端口监听中"
    if [ "${#SOFT_RETIRED[@]}" != "0" ]; then
      c_y "   （软废弃：${SOFT_RETIRED[*]} —— 代码与平台注册都保留，刻意不部署；"
      c_y "     重新启用无需重走审核，点名启停即可：serve.sh start <role>）"
    fi
    if [ "${#not_deployed[@]}" != "0" ]; then
      c_y "   （未部署：${not_deployed[*]} —— 尚未注册到平台，平台不会调用，无需在本机运行）"
    fi
  else
    c_r "⚠️ 有检查项未通过，看上面的表格和日志末尾。"
    echo "    进阶排查："
    echo "      systemctl status acps-leader.service --no-pager -l"
    echo "      journalctl -u acps-leader.service -n 50 --no-pager"
  fi
  return "$fail"
}

case "${1:-}" in
  install)   do_install;  echo; sleep 75; do_verify ;;
  uninstall) do_uninstall ;;
  verify)    do_verify ;;
  case-install)   do_case_install ;;
  case-uninstall) do_case_uninstall ;;
  *) sed -n '2,/^$/p' "$0"; exit 1 ;;
esac
