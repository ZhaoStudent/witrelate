#!/usr/bin/env bash
# 一键验收：把「不依赖平台审核」的部分全部跑一遍，打印验收看板。
#
# 为什么要有这个脚本：
#   项目能否交付，不该由「平台审核批没批」来决定 —— 那是别人的队列。
#   这个脚本证明的是**我们自己的部分已经全部完成**：
#   契约、Agent 编译、协议声明、ACS 合法性、端到端研判链路。
#
# 用法：
#   bash scripts/verify.sh            # 打印看板
#   bash scripts/verify.sh --save     # 顺带写一份 docs/验收报告.md，可发给团队

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

SAVE=0
[ "${1:-}" = "--save" ] && SAVE=1

PY=""
for c in ".venv/Scripts/python.exe" ".venv/bin/python" "python3" "python"; do
  if [ -x "$c" ]; then PY="$c"; break; fi
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then echo "找不到可用的 python，请先在项目根目录建好 .venv"; exit 1; fi

TMP=".verify_report.$$.txt"
: > "$TMP"

# 用 tee 落盘，这样即使在子 shell 里调用也不会丢内容
say() { printf '%s\n' "$1" | tee -a "$TMP"; }

PASS=0; FAIL=0; WAIT=0

say "======================================================================"
say " 法律咨询多智能体项目 · 验收看板"
say " 生成时间：$(date '+%Y-%m-%d %H:%M:%S')"
say " Python  ：$PY"
say "======================================================================"
say ""

# ---- 1. 三份 ACS 的合法性 ------------------------------------------------
say "[1/5] ACS 契约合法性（用官方 acps_sdk 模型校验）"
ACS_OUT=$("$PY" - <<'PY' 2>&1
import glob, json
from acps_sdk.acs import AgentCapabilitySpec as A
ok = True
for f in sorted(glob.glob('acs/*.acs.json')):
    try:
        d = json.load(open(f, encoding='utf-8'))
        A.model_validate(d)
        ports = [e['url'].rsplit(':', 1)[-1] for e in d.get('endPoints', [])]
        print(f"  [通过] {f:28s} {d['name']}  端口={ports}  aic={d.get('aic')!r}")
    except Exception as e:
        ok = False
        print(f"  [失败] {f}: {str(e)[:160]}")
print("  RESULT=" + ("OK" if ok else "NG"))
PY
)
while IFS= read -r l; do
  case "$l" in *RESULT=*) ;; *) say "      $l" ;; esac
done < <(printf '%s\n' "$ACS_OUT")
if printf '%s' "$ACS_OUT" | grep -q 'RESULT=OK'; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi
say ""

# ---- 2. 本地自检 ---------------------------------------------------------
say "[2/5] 本地自检（数据契约 / Agent 编译 / 协议声明 / ACS 草稿）"
CL_OUT=$("$PY" -m legal_acps.check_local 2>&1)
# 🔴 结论要按**三态**取：`[通过] / [失败] / [跳过]`。
#    原先只 grep 前两个，于是"本机没有那份客户端配置 ⇒ 判不了"那一行会被
#    **整行过滤掉**，看板上只剩「8 项通过、0 项失败」—— 跳过被显示成了通过。
#    （这正是 `check_local` 3e 的 `[跳过]` 会踩到的坑：跳过 ≠ 通过，
#      而"看板自己在骗人"比没有看板更糟。）
CL_TAIL=$(printf '%s\n' "$CL_OUT" | sed -n '/自检结论/,$p' | grep -E '\[(通过|失败|跳过)\]')
if [ -z "$CL_TAIL" ]; then
  say "        [失败] 自检脚本未产出结论，原始输出末尾："
  while IFS= read -r l; do say "          $l"; done < <(printf '%s\n' "$CL_OUT" | tail -5)
  FAIL=$((FAIL+1))
else
  N_OK=$(printf '%s\n' "$CL_TAIL" | grep -c '\[通过\]')
  N_NG=$(printf '%s\n' "$CL_TAIL" | grep -c '\[失败\]')
  N_SK=$(printf '%s\n' "$CL_TAIL" | grep -c '\[跳过\]')
  while IFS= read -r l; do say "        $l"; done < <(printf '%s\n' "$CL_TAIL")
  say "        -> $N_OK 项通过，$N_NG 项失败，$N_SK 项跳过未判"
  if [ "$N_NG" -eq 0 ]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi
fi
say ""

# ---- 3. 端到端研判链路 ---------------------------------------------------
say "[3/5] 端到端研判链路（两阶段：煽动识别+真伪拆解 -> 社会危害评估，假模型离线跑）"
LP_OUT=$("$PY" -m legal_acps.local_pipeline --stub 2>&1)
N_DONE=$(printf '%s\n' "$LP_OUT" | grep -c 'status=completed')
SEV=$(printf '%s\n' "$LP_OUT" | grep -E '煽动强度' | tail -1)
TRU=$(printf '%s\n' "$LP_OUT" | grep -E '为真程度' | tail -1)
HARM=$(printf '%s\n' "$LP_OUT" | grep -E 'harm_index *=' | tail -1)
if [ "$N_DONE" -ge 2 ] && [ -n "$HARM" ]; then
  say "        两个阶段均 status=completed（共 $N_DONE 处）"
  [ -n "$SEV" ]  && say "        $SEV"
  [ -n "$TRU" ]  && say "        $TRU"
  [ -n "$HARM" ] && say "        $HARM"
  PASS=$((PASS+1))
else
  say "        [失败] 未取得完整研判结论"
  FAIL=$((FAIL+1))
fi
say ""

# ---- 4. 端口与配置 -------------------------------------------------------
say "[4/5] 端口规划与协议配置"
# 角色/端口清单现场取自 acps_config.PORTS，**不要写死**：
# 写死的结果是"断言用的是新事实、打印出来的是旧事实"，看板自己在骗人（2026-09-30 抓到）。
ROLE_PORTS=$("$PY" -c "from legal_acps.acps_config import PORTS; print(' / '.join(f'{v} {k}' for k, v in PORTS.items()))" 2>/dev/null || true)
if printf '%s' "$CL_OUT" | grep -q '端口不冲突'; then
  say "        [通过] 各角色端口互不冲突（${ROLE_PORTS:-详见 check_local 第 2c/3 节}）"
  PASS=$((PASS+1))
else
  say "        [失败] 端口冲突或未声明协议"
  FAIL=$((FAIL+1))
fi
say ""

# ---- 5. 平台侧状态（需要 .env，不阻塞）----------------------------------
say "[5/5] 平台注册状态（平台侧动作，不计入我方完成度）"
if [ -f .env ]; then
  ST_OUT=$("$PY" -m tools.status 2>&1)
  # 角色清单与 tools.status 用**同一个来源**（agents.BUILDERS），漏掉新角色会看不出来。
  # ⚠️ 列间空白必须用 `[[:space:]]+`（一个或多个）。早期写成 `[[:space:]]`（恰好一个），
  #    而 tools.status 的表格列之间是**多个空格**（`incitement  8443   active`）→
  #    匹配永远为空 → 下面 N_ACT / N_PEND **静默恒为 0**，
  #    "-> N 个已审核通过"这行提示从来没打印过，而看板看起来一切正常。
  #    （2026-09-30 追汇总分支为什么不生效时才发现 —— 与 §12.7 同源：静默为 0 比报错更难发现。）
  ROLES_RE=$("$PY" -c "from legal_acps.agents import BUILDERS; print('^[[:space:]]*(' + '|'.join(BUILDERS) + ')[[:space:]]+')" 2>/dev/null || true)
  # ⚠️ 兜底**不许再写死角色名**（那正是"同一事实散在 4 处"的老毛病：加角色必漏一处，
  #    而且漏掉的那份不会报错，只会静默地少统计一个角色）。改为**换一个来源重新派生**；
  #    两个来源都取不到就**明确报失败**，绝不退回一份可能过期的字面量。
  if [ -z "$ROLES_RE" ]; then
    ROLES_RE=$("$PY" -c "from legal_acps.acps_config import PORTS; print('^[[:space:]]*(' + '|'.join(PORTS) + ')[[:space:]]+')" 2>/dev/null || true)
  fi
  if [ -z "$ROLES_RE" ]; then
    say "        [失败] 无法派生角色清单（agents.BUILDERS 与 acps_config.PORTS 都取不到）"
    FAIL=$((FAIL+1))
  fi
  while IFS= read -r l; do say "      $l"; done < <(printf '%s\n' "$ST_OUT" | grep -E "$ROLES_RE")
  # 只认「角色 端口 状态」行第 3 列，避免匹配到帮助文本里的 "状态会变成 active"
  N_ACT=$(printf '%s\n' "$ST_OUT" | grep -cE "${ROLES_RE}[0-9]+[[:space:]]+active" || true)
  N_PEND=$(printf '%s\n' "$ST_OUT" | grep -cE "${ROLES_RE}[0-9]+[[:space:]]+pending_review" || true)
  if [ "$N_ACT" -gt 0 ]; then
    say "        -> $N_ACT 个已审核通过，可执行 bash scripts/serve.sh start 上线"
  fi
  if [ "$N_PEND" -gt 0 ]; then
    say "        -> $N_PEND 个仍等平台人工审核；我们无法推进，故不计为失败"
  fi
  WAIT=$((WAIT+1))
else
  say "        本机无 .env（凭据在服务器上），跳过。在服务器上跑同一脚本即可看到。"
  WAIT=$((WAIT+1))
fi
say ""

# ---- 汇总 ---------------------------------------------------------------
say "======================================================================"
say " 验收汇总"
say "======================================================================"
say "   我方完成项通过 ： $PASS"
say "   我方完成项失败 ： $FAIL"
say "   等待平台审核   ： $WAIT （平台侧动作，非我方未完成）"
say ""
# ⚠️ 结论必须**现场派生**，不能写死。
#    2026-09-30：审核早已通过、6 个服务也已经在线，看板却还在打印
#    "剩余唯一外部依赖是平台人工审核；审核通过后执行 serve.sh start 即可上线。"
#    —— 与 §12.7 修掉的是同一类缺陷（看板自己在骗人，比没有看板更糟）。
N_ACT=${N_ACT:-0}
N_PEND=${N_PEND:-0}
ROLE_TOTAL=$("$PY" -c "from legal_acps.acps_config import PORTS; print(len(PORTS))" 2>/dev/null || echo 0)
if [ "$FAIL" -ne 0 ]; then
  say "   结论：存在 $FAIL 项未通过，请先修复后再交付。"
elif [ "$N_PEND" -gt 0 ]; then
  say "   结论：我方职责范围内的全部工程内容均已验证通过。"
  say "         还有 $N_PEND 个角色在等平台人工审核；通过后执行"
  say "         run_agent <角色> --register-only 签证书，再 bash scripts/serve.sh restart。"
elif [ "$N_ACT" -ge "$ROLE_TOTAL" ] && [ "$ROLE_TOTAL" -gt 0 ]; then
  say "   结论：$N_ACT/$ROLE_TOTAL 个角色均已审核通过（平台侧 active），无等待项。"
  say "         服务侧是否真在跑，用 bash scripts/serve.sh status 核对 ——"
  say "         本看板只反映平台侧的注册/审核状态。"
else
  say "   结论：我方职责范围内的全部工程内容均已验证通过，无失败项。"
fi
say "======================================================================"

if [ "$SAVE" = "1" ]; then
  mkdir -p docs
  cp "$TMP" docs/验收报告.md
  echo ""
  echo "已写入 docs/验收报告.md"
fi
rm -f "$TMP"
