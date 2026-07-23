#!/usr/bin/env bash
# launchd / 手動：private ops console 夜間上牆
# 週一至五 20:40（晚於 20:35 second-disp）· snapshots + sleeve + holdings
# 僅 mini 安裝；Book 不裝 live launchd。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
LAUNCHD_LOG="${ROOT}/logs/launchd_ops-console-evening-sync.log"
RUN_LOG="${ROOT}/logs/ops_console_evening_sync_${STAMP}.log"

mkdir -p "${ROOT}/logs"
exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd ops-console-evening-sync 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"

if [[ -r "${ROOT}/.env" ]]; then
  set +e
  set -a
  # shellcheck disable=SC1090
  source "${ROOT}/.env" 2>/dev/null
  set +a
  set -e
fi

# Digests parallel wall · default on if unset
export RUN_OPS_DIGEST_SYNC="${RUN_OPS_DIGEST_SYNC:-1}"

PY="${ROOT}/.venv/bin/python"
PY_FUBON="${ROOT}/.venv-fubon/bin/python"
EXIT=0

if [[ ! -x "${PY}" ]]; then
  echo "✗ missing venv python: ${PY}"
  EXIT=1
else
  set +e
  "${PY}" "${ROOT}/scripts/order/write_ops_console_snapshot.py" \
    --kind all --also-digest \
    2>&1 | tee -a "${RUN_LOG}"
  rc=${PIPESTATUS[0]}
  set -e
  if [[ "${rc}" -ne 0 ]]; then
    echo "✗ write_ops_console_snapshot exit=${rc}"
    EXIT=1
  fi

  set +e
  "${PY}" "${ROOT}/scripts/order/write_ops_sleeve_status.py" \
    2>&1 | tee -a "${RUN_LOG}"
  rc=${PIPESTATUS[0]}
  set -e
  if [[ "${rc}" -ne 0 ]]; then
    echo "✗ write_ops_sleeve_status exit=${rc}"
    EXIT=1
  fi
fi

if [[ ! -x "${PY_FUBON}" ]]; then
  echo "⚠ skip holdings sync · missing ${PY_FUBON}"
else
  set +e
  "${PY_FUBON}" "${ROOT}/scripts/order/run_ops_holdings_sync.py" \
    2>&1 | tee -a "${RUN_LOG}"
  rc=${PIPESTATUS[0]}
  set -e
  if [[ "${rc}" -ne 0 ]]; then
    echo "✗ run_ops_holdings_sync exit=${rc}"
    EXIT=1
  fi
fi

echo "=== launchd ops-console-evening-sync 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ "${EXIT}" -ne 0 ]]; then
  export JOB_NOTIFY_EXTRA="${JOB_NOTIFY_EXTRA:-ops console evening sync 失敗，詳見 log}"
  /bin/bash "${ROOT}/scripts/job_notify.sh" \
    "ops console evening sync" "${EXIT}" "${RUN_LOG}" RUN_OPS_CONSOLE_EVENING_EMAIL || true
fi

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
