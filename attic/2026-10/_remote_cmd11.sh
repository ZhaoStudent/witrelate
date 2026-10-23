set -u
cd /home/ubuntu/witrelate || exit 1

echo "=== NOW (UTC) ==="; date -u '+%Y-%m-%dT%H:%M:%SZ'
echo "=== NOW (CST) ==="; TZ=Asia/Shanghai date '+%Y-%m-%dT%H:%M:%S%z'

echo '=== [判据1] systemctl list-timers acps-case-round.timer --all ==='
systemctl list-timers acps-case-round.timer --all --no-pager

echo '=== [判据1b] LastTriggerUSec（原样） ==='
systemctl show acps-case-round.timer -p LastTriggerUSec -p LastTriggerUSecMonotonic --no-pager
echo "LASTTRIGGER_VALUE=[$(systemctl show acps-case-round.timer -p LastTriggerUSec --value)]"

echo '=== [判据2] systemctl show acps-case-round.service ==='
systemctl show acps-case-round.service -p ExecMainStartTimestamp -p ExecMainStatus -p Result -p Type -p ActiveState -p SubState -p NRestarts --no-pager

echo '=== [判据2b] service 计数（ExecMainStartTimestamp 是否有值） ==='
EMST="$(systemctl show acps-case-round.service -p ExecMainStartTimestamp --value)"
echo "EXECMAINSTARTTIMESTAMP_VALUE=[$EMST]"

echo '=== [判据3] tail -1 .wit/leader/case_rounds.jsonl ==='
tail -1 .wit/leader/case_rounds.jsonl
echo '--- 行数 ---'
wc -l .wit/leader/case_rounds.jsonl

echo '=== [硬判据] output/verdict-*.json 全量（按时间倒序） ==='
ls -1t output/verdict-*.json 2>/dev/null | grep -v '\.meta\.json' | while read -r f; do
  echo "  $(stat -c '%y' "$f" | cut -d. -f1)  $f"
done
echo '--- output/ 目录尾部 ---'
ls -la --time-style=long-iso output/ | tail -25

echo '=== [诊断] timer 状态 ==='
systemctl status acps-case-round.timer --no-pager || true

echo '=== [诊断] systemd-analyze calendar ==='
systemd-analyze calendar "$(systemctl show acps-case-round.timer -p TimersCalendar --value)" || true

echo '=== [诊断] journalctl timer since 2026-10-06 00:00 ==='
journalctl -u acps-case-round.timer --since "2026-10-06 00:00" --no-pager || true

echo '=== [诊断] journalctl service (全部) ==='
journalctl -u acps-case-round.service --no-pager || true

echo '=== [诊断] 落盘日志更早的记录（若有） ==='
grep -n "case-round" logs/case-round.log 2>/dev/null | tail -20 || echo '(无 case-round.log 或无匹配)'

echo '=== [最后一步] 重生成台账 md ==='
./.venv/bin/python -m legal_acps.case_round --ledger-md > docs/aicomp/案例研判台账.md
echo "LEDGER_EXIT=$?"
echo "SERVER_MD5=$(md5sum docs/aicomp/案例研判台账.md | awk '{print $1}')"
./.venv/bin/python - <<'PYEOF'
p = "docs/aicomp/案例研判台账.md"
b = open(p, "rb").read()
print("SERVER_BYTES=", len(b))
print("SERVER_CRLF_COUNT=", b.count(b"\r\n"))
print("SERVER_LF_COUNT=", b.count(b"\n"))
PYEOF
echo '=== 台账内容（明文，供核对） ==='
cat docs/aicomp/案例研判台账.md
echo '=== LEDGER_B64_BEGIN ==='
base64 -w0 docs/aicomp/案例研判台账.md
echo
echo '=== LEDGER_B64_END ==='
echo '=== DONE ==='
