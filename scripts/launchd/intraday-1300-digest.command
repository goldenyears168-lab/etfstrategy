#!/usr/bin/env bash
# launchd 專用（無互動）：13:00 盤中研究合併摘要（週一至五 13:02 · VCP+RRG 跑完後）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_intraday-1300-digest.log"
EXIT=1

mkdir -p "${ROOT}/logs/intraday"
exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd intraday-1300-digest 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
PYTHON="${ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  EXIT=1
else
  set +e
  "${PYTHON}" "${ROOT}/scripts/run_intraday_1300_digest.py" \
    --write --notify --require-reports
  EXIT=$?
  set -e
fi

echo "=== launchd intraday-1300-digest 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
