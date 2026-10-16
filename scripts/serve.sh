#!/usr/bin/env bash
# legal_acps —— 服务管理脚本
#
#   bash scripts/serve.sh start             启动全部角色
#   bash scripts/serve.sh start harm        只启动指定角色（按需启停、省内存就靠这个）
#   bash scripts/serve.sh stop              停止全部
#   bash scripts/serve.sh stop harm         停止指定角色
#   bash scripts/serve.sh restart harm      重启指定角色（改过 .env 后必须重启才生效）
#   bash scripts/serve.sh status            查看进程 / 端口 / 内存 / 日志末尾
#   bash scripts/serve.sh logs harm         跟踪某角色日志（Ctrl+C 退出）
#
# 角色与端口：
#   incitement 8443 · harm 8444 · leader 8445 · debate-pro 8446 · debate-con 8447
#   fact-judge 8448 · evidence-image 8449 · evidence-leader 8450 · evidence-audio 8451
#   evidence-video 8452 · law-advisor 8453（法律咨询顾问，2026-10-07 新增：纠纷
#   初期建议，纯 Partner、挂在主控下，不在辩论链上）
#   其中 fact-judge 8448 是网络裁判（两级嵌套的内层 Leader，2026-09-29 新增）；
#   evidence-leader 8450 是证据鉴证的内层 Leader，下挂三个**平级**的模态角色：
#   evidence-image 8449（图像）、evidence-audio 8451（音频，2026-10-02 新增）
#   与 evidence-video 8452（视频，2026-10-02 新增）。
#   ⚠️ incitement(8443) / harm(8444) 自 2026-10-02 起**软废弃**：代码与平台注册
#      都保留，但不部署、不参与统一启停（详见下面的 SOFT_RETIRED）。
#
# ⚠️ 本机若已执行过 scripts/systemd-install.sh，则服务由 systemd 托管，
#    本脚本会自动转交 systemctl（命令用法完全不变）。
#    开机自启 / 崩溃自动重启由 systemd 负责，不要再手工 nohup。
#
# 前置条件：
#   1. ./.env 已填好（ACPS 凭据 + 模型 Key）
#   2. 角色已通过平台人工审核。未通过时监听器状态会是 blocked
#      （acps_identity_not_active）——先用下面这条查审核状态：
#          ./.venv/bin/python -m tools.status

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

# 启动顺序 = 错开顺序。各进程串行登录，间隔 6 秒（见 start_roles）。
#
# ⚠️ `ALL_ROLES` 必须与 `acps_config.PORTS` **完全一致** —— 它回答的是
#    "本项目一共定义了哪些角色"，不是"要跑哪些"（check_local 3b 有断言）。
ALL_ROLES=(incitement harm leader debate-pro debate-con fact-judge evidence-image evidence-leader evidence-audio evidence-video law-advisor lead-intake)

# ── 软废弃（2026-10-02）────────────────────────────────────────────────────
# `incitement` / `harm` 这两个角色**刻意不部署**：代码与平台注册都保留
# （重新启用时不必重走注册与审核），但**统一启停只作用于 `DEPLOY_ROLES`**。
# 单一事实源是 `legal_acps/acps_config.SOFT_RETIRED`，这里与它保持一致；
# 三者的关系由 check_local 3b 的等式断言守着：
#     ALL_ROLES  ==  SOFT_RETIRED  ⊎  DEPLOY_ROLES
# 想单独调试它们：`bash scripts/serve.sh start incitement`（点名就照办）。
SOFT_RETIRED=(incitement harm)
DEPLOY_ROLES=()
for _role in "${ALL_ROLES[@]}"; do
  for _dead in "${SOFT_RETIRED[@]}"; do
    [ "$_role" = "$_dead" ] && continue 2
  done
  DEPLOY_ROLES+=("$_role")
done
unset _role _dead

PY=./.venv/bin/python
LOGDIR=logs
PIDDIR=run

# ⚠️ 角色 → 端口**不在这里维护第二份**：`acps_config.PORTS` 是唯一事实源。
#    这里原本是一张写死的 case 表，结果是 `evidence-image` 加进来之后
#    它一直把端口显示成 "?" —— 正是本项目反复踩过的"同一事实散在多处"
#    （曾散在 PORTS / serve.sh / systemd-install.sh / 自检 共 4 处）。
#    派生失败时（例如 venv 缺依赖）打印一行警告并把端口显示成 "?"，
#    而不是**静默**给出一个缺项的表。
ROLE_PORTS="$("$PY" -c 'from legal_acps.acps_config import PORTS; print(" ".join(f"{k}={v}" for k, v in PORTS.items()))' 2>/dev/null)"
[ -n "$ROLE_PORTS" ] || echo "  [警告] 无法从 acps_config.PORTS 派生端口表，端口列会显示为 ?" >&2

port_of() {
  local pair
  for pair in $ROLE_PORTS; do
    if [ "${pair%%=*}" = "$1" ]; then echo "${pair##*=}"; return 0; fi
  done
  echo "?"
}

# 单元里最大的 ExecStartPre 延迟 —— **现场读单元**，不再抄一份。
# 这里原先把 "0/12/24/36/48/60" 写死在提示语里，加一个角色它就变成假事实；
# 而"等待 75 秒"也会真的不够（单元延迟总和已经超过它）。
max_unit_delay() {
  # 现场读单元目录里最大的 `ExecStartPre=/bin/sleep N`，用来决定"要等多久"。
  # ⚠️ 原先是个**静默失败点**：一旦哪个单元的写法变了（多个空格、带参数、
  #    换成别的命令），sed 匹配不上 ⇒ 该单元被当成 0 秒 ⇒ 统一等待时长静悄悄
  #    短下来 ⇒ 重启后立刻查状态会看到"端口还没监听"，而脚本报的是成功。
  #    所以这里对"有 ExecStartPre 却解析不出秒数"的单元**出声**（走 stderr，
  #    不能污染 stdout —— 这个函数的值是被命令替换取走的）。
  local f n max=0
  for f in deploy/systemd/acps-*.service; do
    [ -f "$f" ] || continue
    n="$(sed -n 's|^ExecStartPre=/bin/sleep[[:space:]]*\([0-9][0-9]*\)$|\1|p' "$f" | head -1)"
    if [ -n "$n" ]; then
      [ "$n" -gt "$max" ] && max="$n"
    elif grep -q '^ExecStartPre=' "$f"; then
      echo "  [警告] $(basename "$f") 的 ExecStartPre 不是可识别的 '/bin/sleep <秒>'，" \
           "本单元的错开延迟按 0 秒计算，统一等待时长可能偏短。" >&2
    fi
  done
  echo "$max"
}

mkdir -p "$LOGDIR" "$PIDDIR"

# ── systemd 托管检测 ────────────────────────────────────────────────
# 执行过 scripts/systemd-install.sh 之后，这些服务由 systemd 托管。
# 此时 serve.sh 绝不能再自己 nohup —— 否则会出现「systemd 一份 + nohup 一份」
# 两个进程抢同一个端口，并且并发登录会把彼此的 Registry 会话顶掉。
# 检测到托管就整体转交 systemctl，命令行习惯保持不变。
SYSTEMD_MODE=0
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
   && systemctl cat acps-incitement.service >/dev/null 2>&1; then
  SYSTEMD_MODE=1
fi

sd_units() {
  if [ "$#" -gt 0 ]; then
    # 显式点名就照办（哪怕单元不存在）—— 明确请求应当得到明确报错，而不是被静默跳过。
    local r
    for r in "$@"; do echo "acps-$r.service"; done
  else
    # 不点名 = "统一操作"，只作用于**已安装**的单元。
    # ⚠️ 若不筛：未安装的角色（如尚未注册到平台的 evidence-image）会让 systemctl 报
    #    "Unit not found"，于是"统一重启"看起来失败了一次 —— 而实际 6 个都重启成功了。
    #    （systemctl 遇未知单元仍会处理其余单元，但退出码非零、输出带红字。）
    # ⚠️ 范围是 `DEPLOY_ROLES`（不含软废弃）。软废弃的角色**已经停了**；
    #    若要单独停它，点名即可（`serve.sh stop incitement` → 走上一个分支）。
    local r
    for r in "${DEPLOY_ROLES[@]}"; do
      systemctl cat "acps-$r.service" >/dev/null 2>&1 && echo "acps-$r.service"
    done
  fi
}

load_env() {
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
}

is_running() {
  local pidfile="$PIDDIR/$1.pid"
  [ -f "$pidfile" ] || return 1
  kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null
}

port_listening() {
  ss -tln 2>/dev/null | grep -q ":$1 "
}

# 进程常驻内存（MiB）。进程不在则返回空。
rss_of() {
  local pid="$1" kb
  kb="$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')"
  [ -n "$kb" ] || return 1
  echo $(( kb / 1024 ))
}

start_one() {
  local role="$1"
  if is_running "$role"; then
    echo "  [跳过]   $role 已在运行（pid $(cat "$PIDDIR/$role.pid")）"
    return 0
  fi
  load_env
  nohup "$PY" -u -m legal_acps.run_agent "$role" >"$LOGDIR/$role.log" 2>&1 </dev/null &
  echo $! >"$PIDDIR/$role.pid"
  echo "  [启动]   $role  pid=$!  端口=$(port_of "$role")  日志=$LOGDIR/$role.log"
}

stop_one() {
  local role="$1"
  local pidfile="$PIDDIR/$role.pid"
  if [ ! -f "$pidfile" ]; then
    echo "  [跳过]   $role 没有 pid 文件"
    return 0
  fi
  local pid
  pid="$(cat "$pidfile")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null
    fi
    echo "  [停止]   $role  pid=$pid"
  else
    echo "  [跳过]   $role 进程已不在"
  fi
  rm -f "$pidfile"
}

show_status() {
  local role port proc pstate mem total=0 any=0
  printf '  %-12s %-26s %s\n' "角色" "进程" "端口 / 内存"
  for role in "${ALL_ROLES[@]}"; do
    port="$(port_of "$role")"
    mem=""
    if is_running "$role"; then
      proc="运行中 pid=$(cat "$PIDDIR/$role.pid")"
      mem="$(rss_of "$(cat "$PIDDIR/$role.pid")")" || mem=""
    else
      proc="未运行"
    fi
    if port_listening "$port"; then
      pstate="$port 监听中"
    else
      pstate="$port 无监听"
    fi
    if [ -n "$mem" ]; then
      pstate="$pstate · 常驻 ${mem} MiB"
      total=$(( total + mem ))
      any=1
    fi
    local tag=""
    [[ " ${SOFT_RETIRED[*]} " == *" $role "* ]] && tag="   [软废弃 · 不参与统一启停]"
    printf '  %-12s %-26s %s%s\n' "$role" "$proc" "$pstate" "$tag"
  done
  if [ "$any" = "1" ]; then
    echo "  （${#ALL_ROLES[@]} 个角色合计常驻约 ${total} MiB）"
  else
    echo "  （当前无角色在运行，不占内存）"
  fi
  echo
  for role in "${ALL_ROLES[@]}"; do
    echo "  --- $role  日志末尾 ($LOGDIR/$role.log)"
    if [ -f "$LOGDIR/$role.log" ]; then
      tail -n 4 "$LOGDIR/$role.log" | sed 's/^/        /'
    else
      echo "        (无日志)"
    fi
  done
}

pick_roles() {
  if [ "$#" -gt 0 ]; then
    printf '%s\n' "$@"
  else
    # 不点名 = 部署清单（不含软废弃），与 systemd 模式下的 `sd_units` 保持一致。
    printf '%s\n' "${DEPLOY_ROLES[@]}"
  fi
}

# 逐个启动，中间留间隔。
# 为什么不能一口气全起：多个进程若同时用同一账号登录 Registry，
# 后登录的会把先前的会话顶掉，表现为后启动的角色打印
#   "注册状态：blocked / RegistryClientError: Could not validate credentials"
# 而单独启动它又是正常的。串行 + 间隔即可稳定通过（已实测）。
start_roles() {
  local first=1 role
  while IFS= read -r role; do
    if [ "$first" = "0" ]; then
      echo "  [等待]   6 秒后启动下一个（避免并发登录互相顶掉会话）"
      sleep 6
    fi
    start_one "$role"
    first=0
  done < <(pick_roles "$@")
}

command="${1:-}"
shift 2>/dev/null || true

# ── systemd 托管模式：整体转交 systemctl ────────────────────────────
if [ "$SYSTEMD_MODE" = "1" ]; then
  case "$command" in
    start)
      echo "检测到 systemd 托管，转交 systemctl 启动……"
      # shellcheck disable=SC2046
      sudo systemctl start $(sd_units "$@")
      echo
      echo "等待 $(( $(max_unit_delay) + 5 )) 秒（各单元按自身 ExecStartPre 错开）……"
      sleep "$(( $(max_unit_delay) + 5 ))"
      bash scripts/systemd-install.sh verify
      ;;
    stop)
      echo "检测到 systemd 托管，转交 systemctl 停止……"
      # shellcheck disable=SC2046
      sudo systemctl stop $(sd_units "$@")
      echo "已停止，systemd 不会再自动拉起你停掉的那些。"
      echo "⚠️ 平台侧状态不会变：Discovery 依旧会返回这些 Agent，只是连不上。"
      echo "   （软废弃的 ${SOFT_RETIRED[*]} 不在统一启停范围内 —— 它们本就没有在跑；"
      echo "     要单独操作请点名，例如 serve.sh stop incitement）"
      ;;
    restart)
      echo "检测到 systemd 托管，转交 systemctl 重启……"
      # shellcheck disable=SC2046
      sudo systemctl restart $(sd_units "$@")
      echo
      echo "等待 $(( $(max_unit_delay) + 5 )) 秒（各单元按自身 ExecStartPre 错开）……"
      sleep "$(( $(max_unit_delay) + 5 ))"
      bash scripts/systemd-install.sh verify
      ;;
    status)
      bash scripts/systemd-install.sh verify
      ;;
    logs)
      role="${1:-incitement}"
      echo "（systemd 托管：应用输出同时写在 $LOGDIR/$role.log 和 journald）"
      exec tail -f "$LOGDIR/$role.log"
      ;;
    *)
      sed -n '2,/^$/p' "$0"
      exit 1
      ;;
  esac
  exit 0
fi

case "$command" in
  start)
    echo "启动 legal_acps 服务……"
    start_roles "$@"
    echo
    echo "等待 8 秒后检查监听……"
    sleep 8
    echo
    show_status
    ;;
  stop)
    echo "停止 legal_acps 服务……"
    while IFS= read -r role; do stop_one "$role"; done < <(pick_roles "$@")
    ;;
  restart)
    echo "重启 legal_acps 服务……"
    while IFS= read -r role; do stop_one "$role"; done < <(pick_roles "$@")
    echo
    start_roles "$@"
    echo
    echo "等待 8 秒后检查监听……"
    sleep 8
    echo
    show_status
    ;;
  status)
    show_status
    ;;
  logs)
    role="${1:-incitement}"
    if [ ! -f "$LOGDIR/$role.log" ]; then
      echo "找不到 $LOGDIR/$role.log"
      exit 1
    fi
    tail -f "$LOGDIR/$role.log"
    ;;
  *)
    sed -n '2,/^$/p' "$0"
    exit 1
    ;;
esac
