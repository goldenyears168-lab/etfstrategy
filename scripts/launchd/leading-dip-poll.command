#!/usr/bin/env bash
# launchd：Leading Dip Order poll · 09:05–13:25 每 5 分 · 獨立袖套（非 ABC／非 C18 槽）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday/launchd_leading-dip-poll.log"
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
if [[ "${H}" -lt 9 ]] || [[ "${H}" -gt 13 ]]; then
  echo "skip: outside window $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi
if [[ "${H}" -eq 9 && "${M}" -lt 5 ]]; then
  echo "skip: before 09:05 $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi
if [[ "${H}" -eq 13 && "${M}" -gt 25 ]]; then
  echo "skip: after 13:25 $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd leading-dip-poll tick $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_LEADING_DIP_POLL="${RUN_LEADING_DIP_POLL:-1}"
export RUN_LEADING_DIP_SUBMIT_EMAIL="${RUN_LEADING_DIP_SUBMIT_EMAIL:-1}"
export ORDER_LEADING_DIP_ORDER_ENABLED="${ORDER_LEADING_DIP_ORDER_ENABLED:-1}"
export ORDER_LEADING_DIP_DRY_RUN="${ORDER_LEADING_DIP_DRY_RUN:-1}"
export ORDER_LEADING_DIP_AUTO_SUBMIT="${ORDER_LEADING_DIP_AUTO_SUBMIT:-0}"

if [[ "${RUN_LEADING_DIP_POLL}" != "1" ]]; then
  echo "skip: RUN_LEADING_DIP_POLL=${RUN_LEADING_DIP_POLL}"
  exit 0
fi

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/order/run_leading_dip_poll.py" 2>&1)"
EXIT=$?
set -e
echo "${OUT}"
exit "${EXIT}"
