#!/usr/bin/env bash
# launchd：09:06 開盤解讀 · 合併 morning + gate + 持倉即時脈動 + 宇宙摘要

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_intraday-open-digest.log"
EXIT=0

mkdir -p "${ROOT}/logs/intraday"
: >>"${LAUNCHD_LOG}"

exec >>"${LAUNCHD_LOG}" 2>&1

WD="$(date '+%u')"
H=$((10#$(date '+%H')))
M=$((10#$(date '+%M')))
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi
if [[ "${H}" -ne 9 || "${M}" -lt 7 || "${M}" -gt 10 ]]; then
  echo "skip: outside 09:08 window $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd intraday-open-digest $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_INTRADAY_OPEN_DIGEST="${RUN_INTRADAY_OPEN_DIGEST:-1}"
export RUN_INTRADAY_OPEN_DIGEST_EMAIL="${RUN_INTRADAY_OPEN_DIGEST_EMAIL:-1}"

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/run_intraday_open_digest.py" \
  --write --notify 2>&1)"
EXIT=$?
set -e
echo "${OUT}"

echo "=== launchd intraday-open-digest end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
