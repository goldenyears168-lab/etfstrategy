#!/usr/bin/env bash
# 背景啟動 screener 歷史灌庫（nohup+setsid 脫離終端 · 防重複 · 記錄信號）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date '+%Y%m%d')"
LOG_FILE="${LOG_DIR}/backfill_screener_${STAMP}.log"
PID_FILE="${LOG_DIR}/backfill_screener.pid"
RUNNER="${LOG_DIR}/.backfill_screener_runner.sh"

cat >"$RUNNER" <<EOF
#!/usr/bin/env bash
set +e
LOG_FILE="${LOG_FILE}"
PID_FILE="${PID_FILE}"
ROOT="${ROOT}"

_log() { echo "\$@" >>"\$LOG_FILE"; }

trap '_log "=== daemon EXIT code=\$? \$(date "+%Y-%m-%dT%H:%M:%S%z") ==="' EXIT
trap '_log "=== daemon SIGTERM \$(date "+%Y-%m-%dT%H:%M:%S%z") ==="' TERM
trap '_log "=== daemon SIGHUP \$(date "+%Y-%m-%dT%H:%M:%S%z") ==="' HUP
trap '_log "=== daemon SIGINT \$(date "+%Y-%m-%dT%H:%M:%S%z") ==="' INT

env PYTHONUNBUFFERED=1 bash "\${ROOT}/scripts/backfill_screener_data.sh" "\$@"
EC=\$?
_log "=== daemon finished exit=\${EC} \$(date '+%Y-%m-%dT%H:%M:%S%z') ==="
rm -f "\$PID_FILE"
exit "\$EC"
EOF
chmod +x "$RUNNER"

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE")"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "screener backfill 已在執行（pid=${OLD_PID}）"
    echo "  log: ${LOG_FILE}"
    exit 0
  fi
  echo "WARN: 清除過期 pid=${OLD_PID}" >>"$LOG_FILE"
fi

echo "=== daemon start $(date '+%Y-%m-%dT%H:%M:%S%z') ppid=$$ ===" >>"$LOG_FILE"

if command -v setsid >/dev/null 2>&1; then
  nohup setsid "$RUNNER" "$@" >>"$LOG_FILE" 2>&1 &
else
  nohup "$RUNNER" "$@" >>"$LOG_FILE" 2>&1 &
fi
NEW_PID=$!
echo "$NEW_PID" >"$PID_FILE"

echo "screener backfill 已啟動 pid=${NEW_PID}（nohup+setsid）"
echo "  log: ${LOG_FILE}"
echo "  請在本機 Terminal.app 執行；Cursor 內建終端關閉時可能 SIGHUP 殺掉背景 job"
echo "  tail -f ${LOG_FILE}"
