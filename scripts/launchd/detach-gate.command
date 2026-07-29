#!/usr/bin/env bash
# launchd：Detach Gate（台美脫鉤閘門）· 09:40–12:30 每 5 分 · RED → 半倉買一

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday/launchd_detach-gate.log"
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
if [[ "${H}" -lt 9 ]] || [[ "${H}" -gt 12 ]]; then
  echo "skip: outside window $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi
if [[ "${H}" -eq 9 && "${M}" -lt 40 ]]; then
  echo "skip: before 09:40 $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi
if [[ "${H}" -eq 12 && "${M}" -gt 30 ]]; then
  echo "skip: after 12:30 $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd detach-gate tick $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_DETACH_GATE="${RUN_DETACH_GATE:-1}"
export RUN_DETACH_GATE_EMAIL="${RUN_DETACH_GATE_EMAIL:-1}"
export ORDER_DETACH_GATE_DRY_RUN="${ORDER_DETACH_GATE_DRY_RUN:-1}"
export ORDER_DETACH_GATE_ORDER_ENABLED="${ORDER_DETACH_GATE_ORDER_ENABLED:-1}"
export ORDER_DETACH_GATE_AUTO_SUBMIT="${ORDER_DETACH_GATE_AUTO_SUBMIT:-1}"

if [[ "${RUN_DETACH_GATE}" != "1" ]]; then
  echo "skip: RUN_DETACH_GATE=${RUN_DETACH_GATE}"
  exit 0
fi

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/order/run_detach_gate_poll.py" 2>&1)"
EXIT=$?
set -e
echo "${OUT}"

echo "=== launchd detach-gate end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
