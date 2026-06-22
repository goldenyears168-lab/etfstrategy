#!/usr/bin/env bash
# launchd 專用（無互動）：RRG mono 收盤前預警（週一至五 13:00）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/launchd_rrg-mono-intraday-watch.log"
EXIT=1

exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd rrg-mono-intraday-watch 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
PYTHON="${ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  EXIT=1
else
  set +e
  "${PYTHON}" "${ROOT}/scripts/run_rrg_mono_intraday_watch.py"
  EXIT=$?
  set -e
fi

echo "=== launchd rrg-mono-intraday-watch 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

"${ROOT}/scripts/launchd/supabase_slot_sync.sh" 1300

"${ROOT}/scripts/rrg_mono_intraday_notify.sh" "${EXIT}" || true

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
