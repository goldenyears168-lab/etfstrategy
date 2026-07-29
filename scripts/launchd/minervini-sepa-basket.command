#!/usr/bin/env bash
# launchd：Minervini SEPA basket · 16:35 月末調倉檢查（dry-run intent · 寄信）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/launchd_minervini-sepa-basket.log"
EXIT=1

exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd minervini-sepa-basket $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export ORDER_MINERVINI_SEPA_DRY_RUN="${ORDER_MINERVINI_SEPA_DRY_RUN:-1}"
PYTHON="${ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  EXIT=1
else
  set +e
  OUT="$("${PYTHON}" "${ROOT}/scripts/run_minervini_sepa_daily_brief.py" 2>&1)"
  EXIT=$?
  set -e
  echo "${OUT}"
fi

echo "=== launchd minervini-sepa-basket end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if echo "${OUT:-}" | grep -q 'MINERVINI_SEPA_SIGNAL=1'; then
  EXTRA_LINES="$(echo "${OUT}" | grep -E 'MINERVINI_SEPA_SIGNAL|rebalance:' || true)"
  export JOB_NOTIFY_EXTRA=$'本輪月末調倉（dry-run intent）\n'"${EXTRA_LINES}"
  "${ROOT}/scripts/minervini_sepa_notify.sh" "${EXIT}" || true
fi

exit "${EXIT}"
