#!/usr/bin/env bash
# launchd：Morning holdings brief · 週一至五 08:50

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_morning-holdings-brief.log"
EXIT=0

mkdir -p "${ROOT}/logs/intraday"
: >>"${LAUNCHD_LOG}"

exec >>"${LAUNCHD_LOG}" 2>&1

WD="$(date '+%u')"
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd morning-holdings-brief $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export RUN_MORNING_HOLDINGS_EMAIL="${RUN_MORNING_HOLDINGS_EMAIL:-0}"

PYTHON="${ROOT}/.venv-fubon/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/order/morning_holdings_brief.py" \
  --no-futures --sync-db --write 2>&1)"
EXIT=$?
set -e
echo "${OUT}"

echo "=== launchd morning-holdings-brief end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
