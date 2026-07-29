#!/usr/bin/env bash
# launchd / 手動：處置股專家池跟單觀測
# 週一至五 20:35 · 有可跟訊號才寄信 · 不下單

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
LAUNCHD_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/launchd_second-disp-expert-pool-watch.log"
RUN_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/second_disp_expert_pool_watch_${STAMP}.log"

mkdir -p "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs"
exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd second-disp-expert-pool-watch 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

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
  "${PYTHON}" "${ROOT}/scripts/research/run_second_disp_expert_pool_watch.py" \
    2>&1 | tee "${RUN_LOG}"
  EXIT=${PIPESTATUS[0]}
  set -e
fi

echo "=== launchd second-disp-expert-pool-watch 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ "${EXIT}" -ne 0 ]]; then
  export JOB_NOTIFY_EXTRA="${JOB_NOTIFY_EXTRA:-處置股專家池觀測失敗，詳見 log}"
  /bin/bash "${ROOT}/scripts/job_notify.sh" \
    "處置股專家池" "${EXIT}" "${RUN_LOG}" RUN_SECOND_DISP_EXPERT_POOL_EMAIL || true
fi

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
