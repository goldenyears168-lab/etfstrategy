#!/usr/bin/env bash
# launchd 專用：週一至五 09:00–13:30 · C18acc 5 分鐘 poll（預設 dry-run）
# 背景排程由 install-launchd 安裝 ~/Library/Application Support/com.jackm4.etf/rrg-c18acc-poll.sh（不開 Terminal）
# 手動除錯仍可用：open -gj scripts/launchd/rrg-c18acc-poll.command

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_rrg-c18acc-poll.log"
TICK_LOG="${ROOT}/logs/intraday/rrg_c18acc_poll_tick.log"
EXIT=0

mkdir -p "${ROOT}/logs/intraday"
: >>"${LAUNCHD_LOG}"
: >>"${TICK_LOG}"

exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd rrg-c18acc-poll tick $(date '+%Y-%m-%d %H:%M:%S') ==="

WD="$(date '+%u')"
H=$((10#$(date '+%H')))
M=$((10#$(date '+%M')))
# 1=Mon … 5=Fri · 6–7 skip
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend"
  exit 0
fi
if [[ "${H}" -lt 9 ]] || [[ "${H}" -gt 13 ]] || { [[ "${H}" -eq 13 ]] && [[ "${M}" -gt 30 ]]; }; then
  echo "skip: outside 09:00–13:30"
  exit 0
fi

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_RRG_C18ACC_SCREEN="${RUN_RRG_C18ACC_SCREEN:-1}"
export ORDER_C18ACC_DRY_RUN="${ORDER_C18ACC_DRY_RUN:-1}"
export ORDER_C18ACC_APPLY_STATE="${ORDER_C18ACC_APPLY_STATE:-0}"
export C18ACC_KBAR_SYNC="${C18ACC_KBAR_SYNC:-1}"
export RUN_RRG_C18ACC_EMAIL="${RUN_RRG_C18ACC_EMAIL:-0}"
export RUN_C18ACC_EXTENSION_OVERLAY="${RUN_C18ACC_EXTENSION_OVERLAY:-0}"
export C18ACC_EXTENSION_MODE="${C18ACC_EXTENSION_MODE:-combo_spike}"
export C18ACC_EXTENSION_MIN_SPIKE_PCT="${C18ACC_EXTENSION_MIN_SPIKE_PCT:-4}"
export C18ACC_EXTENSION_MIN_FADE_PCT="${C18ACC_EXTENSION_MIN_FADE_PCT:-1.0}"
export C18ACC_EXTENSION_POLL_MIN="${C18ACC_EXTENSION_POLL_MIN:-1}"
export C18ACC_EXTENSION_WATCH_HEAT="${C18ACC_EXTENSION_WATCH_HEAT:-70}"
export C18ACC_EXTENSION_HEAT="${C18ACC_EXTENSION_HEAT:-70}"
export C18ACC_EXTENSION_DRY_RUN="${C18ACC_EXTENSION_DRY_RUN:-1}"
export C18ACC_EXTENSION_EMIT_INTENT="${C18ACC_EXTENSION_EMIT_INTENT:-0}"
export C18ACC_EXTENSION_APPLY_STATE="${C18ACC_EXTENSION_APPLY_STATE:-0}"
# 候選池：昨收 PIT fresh mono 全池（僅 .env 設 C18ACC_POOL_OVERRIDE 時才用手動名單）

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/run_rrg_mono_swap_accel_screen.py" 2>&1)"
EXIT=$?
set -e
echo "${OUT}"

if echo "${OUT}" | grep -q 'C18ACC_SIGNAL=1'; then
  EXTRA_LINES="$(echo "${OUT}" | grep -E 'C18acc (screen|intent)|C18ACC_SIGNAL' || true)"
  export JOB_NOTIFY_EXTRA=$'本輪觸發動作（dry-run intent）\n'"${EXTRA_LINES}"
  "${ROOT}/scripts/rrg_c18acc_poll_notify.sh" "${EXIT}" || true
fi

echo "=== launchd rrg-c18acc-poll end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
