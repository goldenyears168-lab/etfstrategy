#!/usr/bin/env bash
# launchd：限時限價單 · 週一至五 09:05（config/order.yaml timed_limit_orders）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_timed-limit-orders.log"
EXIT=0

mkdir -p "${ROOT}/logs/intraday"
: >>"${LAUNCHD_LOG}"

exec >>"${LAUNCHD_LOG}" 2>&1

WD="$(date '+%u')"
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd timed-limit-orders tick $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_TIMED_LIMIT_ORDERS="${RUN_TIMED_LIMIT_ORDERS:-1}"
export ORDER_TIMED_LIMIT_DRY_RUN="${ORDER_TIMED_LIMIT_DRY_RUN:-1}"

if [[ "${RUN_TIMED_LIMIT_ORDERS}" != "1" ]]; then
  echo "skip: RUN_TIMED_LIMIT_ORDERS=${RUN_TIMED_LIMIT_ORDERS}"
  exit 0
fi

PYTHON="${ROOT}/.venv-fubon/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv-fubon python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/order/run_timed_limit_orders.py" 2>&1)"
EXIT=$?
set -e
echo "${OUT}"
exit "${EXIT}"
