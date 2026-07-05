#!/usr/bin/env bash
# launchd：Buy signal radar · 09:00–13:20 每 5 分 · advisory email only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_buy-signal-radar.log"
EXIT=0

mkdir -p "${ROOT}/logs/intraday"
: >>"${LAUNCHD_LOG}"

exec >>"${LAUNCHD_LOG}" 2>&1

WD="$(date '+%u')"
H=$((10#$(date '+%H')))
M=$((10#$(date '+%M')))
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi
if [[ "${H}" -lt 9 ]] || [[ "${H}" -gt 13 ]] || { [[ "${H}" -eq 13 ]] && [[ "${M}" -gt 20 ]]; }; then
  echo "skip: outside window $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd buy-signal-radar tick $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_BUY_SIGNAL_RADAR="${RUN_BUY_SIGNAL_RADAR:-1}"
export RUN_BUY_SIGNAL_EMAIL="${RUN_BUY_SIGNAL_EMAIL:-1}"
export C18ACC_KBAR_SYNC="${C18ACC_KBAR_SYNC:-1}"
export SIGNAL_RADAR_USE_FUBON="${SIGNAL_RADAR_USE_FUBON:-1}"

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/run_buy_signal_radar.py" 2>&1)"
EXIT=$?
set -e
echo "${OUT}"

if echo "${OUT}" | grep -q 'BUY_SIGNAL_RADAR=1'; then
  EXTRA_LINES="$(echo "${OUT}" | grep -E 'BUY_SIGNAL_RADAR|buy ' || true)"
  export JOB_NOTIFY_EXTRA=$'C0 買進訊號（advisory · 人工確認）\n'"${EXTRA_LINES}"
  "${ROOT}/scripts/buy_signal_notify.sh" "${EXIT}" || true
fi

echo "=== launchd buy-signal-radar end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
