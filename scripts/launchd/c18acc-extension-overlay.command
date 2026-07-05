#!/usr/bin/env bash
# launchd：C18acc extension overlay · 09:06–13:20（1m 有效輪詢 · dry-run intent · 寄信）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_c18acc-extension-overlay.log"
TICK_LOG="${ROOT}/logs/intraday/c18acc_extension_poll_tick.log"
EXIT=0

mkdir -p "${ROOT}/logs/intraday"
: >>"${LAUNCHD_LOG}"
: >>"${TICK_LOG}"

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

echo "=== launchd c18acc-extension tick $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_C18ACC_EXTENSION_OVERLAY="${RUN_C18ACC_EXTENSION_OVERLAY:-1}"
export C18ACC_EXTENSION_DRY_RUN="${C18ACC_EXTENSION_DRY_RUN:-1}"
export C18ACC_EXTENSION_EMIT_INTENT="${C18ACC_EXTENSION_EMIT_INTENT:-0}"
export C18ACC_EXTENSION_APPLY_STATE="${C18ACC_EXTENSION_APPLY_STATE:-0}"
export C18ACC_KBAR_SYNC="${C18ACC_KBAR_SYNC:-1}"

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/run_c18acc_extension_screen.py" 2>&1)"
EXIT=$?
set -e
echo "${OUT}" >>"${TICK_LOG}"
echo "${OUT}"

if echo "${OUT}" | grep -q 'C18ACC_EXTENSION_SIGNAL=1'; then
  EXTRA_LINES="$(echo "${OUT}" | grep -E 'C18ACC_EXTENSION_SIGNAL|alert ' || true)"
  export JOB_NOTIFY_EXTRA=$'Extension 出場訊號（dry-run intent）\n'"${EXTRA_LINES}"
  "${ROOT}/scripts/c18acc_extension_notify.sh" "${EXIT}" || true
fi

echo "=== launchd c18acc-extension end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
