#!/usr/bin/env bash
# launchd：跟單松山 Order poll · 09:25–09:40 每 5 分 · 五日累積淨比95 ∩ !mega + 25m nonfail

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday/launchd_songshan-copytrade-poll.log"
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
if [[ "${H}" -ne 9 || "${M}" -lt 25 || "${M}" -gt 40 ]]; then
  echo "skip: outside 09:25–09:40 $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd songshan-copytrade-poll tick $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_SONGSHAN_COPYTRADE_POLL="${RUN_SONGSHAN_COPYTRADE_POLL:-1}"
export ORDER_SONGSHAN_COPYTRADE_ENABLED="${ORDER_SONGSHAN_COPYTRADE_ENABLED:-1}"
export ORDER_SONGSHAN_COPYTRADE_DRY_RUN="${ORDER_SONGSHAN_COPYTRADE_DRY_RUN:-1}"
export ORDER_SONGSHAN_COPYTRADE_AUTO_SUBMIT="${ORDER_SONGSHAN_COPYTRADE_AUTO_SUBMIT:-0}"

if [[ "${RUN_SONGSHAN_COPYTRADE_POLL}" != "1" ]]; then
  echo "skip: RUN_SONGSHAN_COPYTRADE_POLL=${RUN_SONGSHAN_COPYTRADE_POLL}"
  exit 0
fi

PYTHON="${ROOT}/.venv-fubon/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv-fubon python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/order/run_songshan_copytrade_poll.py" 2>&1)"
EXIT=$?
set -e
echo "${OUT}"
exit "${EXIT}"
