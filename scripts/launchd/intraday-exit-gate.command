#!/usr/bin/env bash
# launchd：09:05 組合閘門 · dry-run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_intraday-exit-gate.log"
EXIT=0

mkdir -p "${ROOT}/logs/intraday"
: >>"${LAUNCHD_LOG}"

exec >>"${LAUNCHD_LOG}" 2>&1

WD="$(date '+%u')"
H=$((10#$(date '+%H')))
M=$((10#$(date '+%M')))
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend"
  exit 0
fi
if [[ "${H}" -ne 9 ]]; then
  echo "skip: outside 09:xx gate window $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi
case "${M}" in
  6|10|12|15) ;;
  *)
    echo "skip: outside gate minutes (09:06/10/12/15) $(date '+%Y-%m-%d %H:%M:%S')"
    exit 0
    ;;
esac

echo "=== launchd intraday-exit-gate $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_INTRADAY_EXIT_GATE_EMAIL="${RUN_INTRADAY_EXIT_GATE_EMAIL:-0}"
export INTRADAY_GATE_KBAR_SYNC="${INTRADAY_GATE_KBAR_SYNC:-1}"

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/order/run_intraday_exit_gate.py" \
  --write --persist 2>&1)"
EXIT=$?
set -e
echo "${OUT}"

echo "=== launchd intraday-exit-gate end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
