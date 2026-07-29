#!/usr/bin/env bash
# launchd / 手動：專家池達標 HTML 圖文（K＋RRG）
# 週一至五 20:05 · 無達標不寄 · 不下單

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
LAUNCHD_LOG="${ROOT}/logs/launchd_expert-pool-chart-digest.log"
RUN_LOG="${ROOT}/logs/expert_pool_chart_digest_${STAMP}.log"

mkdir -p "${ROOT}/logs"
exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd expert-pool-chart-digest 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src:${ROOT}/scripts/research"
PYTHON="${ROOT}/.venv/bin/python"

if [[ -r "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/.env" ]]; then
  set +e
  set -a
  # shellcheck disable=SC1090
  source "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/.env" 2>/dev/null
  set +a
  set -e
fi
export RUN_OPS_DIGEST_SYNC="${RUN_OPS_DIGEST_SYNC:-1}"
export RUN_ALERT_EMAIL=0

EXIT=0
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  EXIT=1
else
  set +e
  "${PYTHON}" "${ROOT}/scripts/research/send_expert_pool_chart_digest.py" \
    --lookback-days 3 \
    --skip-if-empty \
    2>&1 | tee "${RUN_LOG}"
  EXIT=${PIPESTATUS[0]}
  set -e
fi

echo "=== launchd expert-pool-chart-digest 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
