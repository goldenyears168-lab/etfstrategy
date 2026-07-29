#!/usr/bin/env bash
# launchd：Sell signal radar · 09:06–13:20 每 5 分 · 宇宙 extension 純訊號

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday/launchd_sell-signal-radar.log"
EXIT=0

mkdir -p "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday"
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
if [[ "${H}" -eq 9 && "${M}" -lt 6 ]]; then
  echo "skip: before 09:06 $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd sell-signal-radar tick $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_SELL_SIGNAL_RADAR="${RUN_SELL_SIGNAL_RADAR:-1}"
export RUN_SELL_SIGNAL_EMAIL="${RUN_SELL_SIGNAL_EMAIL:-1}"
export C18ACC_KBAR_SYNC="${C18ACC_KBAR_SYNC:-1}"
export C18ACC_EXTENSION_MODE="${C18ACC_EXTENSION_MODE:-combo_spike}"
export C18ACC_EXTENSION_MIN_SPIKE_PCT="${C18ACC_EXTENSION_MIN_SPIKE_PCT:-4}"
export C18ACC_EXTENSION_MIN_FADE_PCT="${C18ACC_EXTENSION_MIN_FADE_PCT:-1.0}"
export C18ACC_EXTENSION_POLL_MIN="${C18ACC_EXTENSION_POLL_MIN:-5}"
export SIGNAL_RADAR_UNIVERSE_MIN_HOLD_DAYS="${SIGNAL_RADAR_UNIVERSE_MIN_HOLD_DAYS:-0}"

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/run_sell_signal_radar.py" 2>&1)"
EXIT=$?
set -e
echo "${OUT}"

if echo "${OUT}" | grep -q 'SELL_SIGNAL_RADAR=1'; then
  SKIP_NOTIFY=0
  if [[ "${H}" -eq 9 && "${M}" -lt 12 ]]; then
    SKIP_NOTIFY=1
    echo "skip sell email: before 09:12 open digest window"
  fi
  if [[ "${SKIP_NOTIFY}" -eq 0 ]]; then
    EXTRA_LINES="$(echo "${OUT}" | grep -E 'SELL_SIGNAL_RADAR|sell ' || true)"
    export JOB_NOTIFY_EXTRA=$'持倉賣出 advisory（extension · 人工確認）\n'"${EXTRA_LINES}"
    "${ROOT}/scripts/sell_signal_notify.sh" "${EXIT}" || true
  fi
fi

echo "=== launchd sell-signal-radar end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
