#!/usr/bin/env bash
# launchd / 手動：南電+金居專家池回看1日共識觀測（達標才寄信）
# 週一至五 20:05 · **已併入 winbond-expert-pool-watch**（install-launchd 退役）
# 若手動跑：與夜間觀測波同政策 — 細節上牆、不個別 SMTP

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
LAUNCHD_LOG="${ROOT}/logs/launchd_specialty-expert-pool-watch.log"
RUN_LOG="${ROOT}/logs/specialty_expert_pool_watch_${STAMP}.log"

mkdir -p "${ROOT}/logs"
exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd specialty-expert-pool-watch 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
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
  "${PYTHON}" "${ROOT}/scripts/research/run_specialty_expert_pool_watch.py" \
    --stock all \
    2>&1 | tee "${RUN_LOG}"
  EXIT=${PIPESTATUS[0]}
  set -e
fi

echo "=== launchd specialty-expert-pool-watch 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ "${EXIT}" -ne 0 ]]; then
  export JOB_NOTIFY_EXTRA="${JOB_NOTIFY_EXTRA:-觀測失敗，詳見 log}"
  /bin/bash "${ROOT}/scripts/job_notify.sh" \
    "南電/金居專家池觀測" "${EXIT}" "${RUN_LOG}" RUN_SPECIALTY_EXPERT_POOL_EMAIL || true
fi

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
