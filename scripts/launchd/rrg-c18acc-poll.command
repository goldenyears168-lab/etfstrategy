#!/usr/bin/env bash
# launchd 專用：週一至五 09:00–13:30 · C18acc 5 分鐘 poll（預設 dry-run）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/launchd_rrg-c18acc-poll.log"
TICK_LOG="${ROOT}/logs/rrg_c18acc_poll_tick.log"
EXIT=0

mkdir -p "${ROOT}/logs"
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
export ORDER_C18ACC_APPLY_STATE="${ORDER_C18ACC_APPLY_STATE:-1}"
export C18ACC_KBAR_SYNC="${C18ACC_KBAR_SYNC:-1}"
export RUN_RRG_C18ACC_EMAIL="${RUN_RRG_C18ACC_EMAIL:-1}"
# 今日手動候選（移除或改日期即恢復 PIT fresh mono）
export C18ACC_POOL_OVERRIDE="${C18ACC_POOL_OVERRIDE:-3711,6488,2344,3008}"
export C18ACC_POOL_OVERRIDE_DATE="${C18ACC_POOL_OVERRIDE_DATE:-2026-06-24}"

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
