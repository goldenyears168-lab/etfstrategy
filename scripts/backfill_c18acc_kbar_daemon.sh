#!/usr/bin/env bash
# P0 mono shortlist 1m kbar · 每日續跑至 quota 用盡（nohup+setsid）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date '+%Y%m%d')"
LOG_FILE="${LOG_DIR}/backfill_c18acc_kbar_${STAMP}.log"
PID_FILE="${LOG_DIR}/backfill_c18acc_kbar.pid"
RUNNER="${LOG_DIR}/.backfill_c18acc_kbar_runner.sh"

PYTHON="${PYTHON:-python3}"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PYTHON="${ROOT}/.venv/bin/python"
fi

cat >"$RUNNER" <<EOF
#!/usr/bin/env bash
set +e
LOG_FILE="${LOG_FILE}"
PID_FILE="${PID_FILE}"
ROOT="${ROOT}"
PYTHON="${PYTHON}"

_log() { echo "\$@" >>"\$LOG_FILE"; }

trap '_log "=== daemon EXIT code=\$? \$(date "+%Y-%m-%dT%H:%M:%S%z") ==="' EXIT
trap '_log "=== daemon SIGTERM \$(date "+%Y-%m-%dT%H:%M:%S%z") ==="' TERM
trap '_log "=== daemon SIGHUP \$(date "+%Y-%m-%dT%H:%M:%S%z") ==="' HUP
trap '_log "=== daemon SIGINT \$(date "+%Y-%m-%dT%H:%M:%S%z") ==="' INT

export PYTHONPATH="\${ROOT}/src:\${ROOT}/scripts"
export PYTHONUNBUFFERED=1
"\$PYTHON" "\${ROOT}/scripts/backfill_c18acc_kbar.py" "\$@"
EC=\$?
_log "=== daemon finished exit=\${EC} \$(date '+%Y-%m-%dT%H:%M:%S%z') ==="
rm -f "\$PID_FILE"
exit "\$EC"
EOF
chmod +x "$RUNNER"

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE")"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "c18acc kbar backfill 已在執行（pid=${OLD_PID}）"
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

echo "c18acc mono kbar backfill 已啟動 pid=${NEW_PID}"
echo "  log: ${LOG_FILE}"
echo "  tail -f ${LOG_FILE}"
