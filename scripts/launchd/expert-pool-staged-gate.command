#!/usr/bin/env bash
# launchd：專家池漏斗閘門 · 週一至五 09:00／09:01／09:05／09:25

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_expert-pool-staged-gate.log"
EXIT=0

mkdir -p "${ROOT}/logs/intraday"
: >>"${LAUNCHD_LOG}"

exec >>"${LAUNCHD_LOG}" 2>&1

WD="$(date '+%u')"
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd expert-pool-staged-gate tick $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_EP_STAGED_GATE="${RUN_EP_STAGED_GATE:-1}"
export ORDER_EP_STAGED_GATE_DRY_RUN="${ORDER_EP_STAGED_GATE_DRY_RUN:-1}"

if [[ "${RUN_EP_STAGED_GATE}" != "1" ]]; then
  echo "skip: RUN_EP_STAGED_GATE=${RUN_EP_STAGED_GATE}"
  exit 0
fi

PYTHON="${ROOT}/.venv-fubon/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv-fubon python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/order/run_expert_pool_staged_gate.py" 2>&1)"
EXIT=$?
set -e
echo "${OUT}"
exit "${EXIT}"
