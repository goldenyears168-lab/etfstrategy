#!/usr/bin/env bash
# launchd 專用（無互動）：週一至五 16:40 RRG mono 每日掃描

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/launchd_rrg-mono-scan.log"
EXIT=1

exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd rrg-mono-scan 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
PYTHON="${ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  EXIT=1
else
  set +e
  "${PYTHON}" "${ROOT}/scripts/run_rrg_mono_daily_brief.py"
  EXIT=$?
  set -e
fi

echo "=== launchd rrg-mono-scan 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

"${ROOT}/scripts/rrg_mono_notify.sh" "${EXIT}" || true

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
