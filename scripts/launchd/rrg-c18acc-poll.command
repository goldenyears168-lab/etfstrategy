#!/usr/bin/env bash
# launchd 專用：週一至五 09:00–13:30 · C18acc 5 分鐘 poll（E@13:00 開窗）
# 背景排程由 install-launchd 安裝 ~/Library/Application Support/com.jackm4.etf/rrg-c18acc-poll.sh（不開 Terminal）
# 手動除錯仍可用：open -gj scripts/launchd/rrg-c18acc-poll.command

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_rrg-c18acc-poll.log"
TICK_LOG="${ROOT}/logs/intraday/rrg_c18acc_poll_tick.log"
EXIT=0

mkdir -p "${ROOT}/logs/intraday"
: >>"${LAUNCHD_LOG}"
: >>"${TICK_LOG}"

exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd rrg-c18acc-poll tick $(date '+%Y-%m-%d %H:%M:%S') ==="

WD="$(date '+%u')"
H=$((10#$(date '+%H')))
M=$((10#$(date '+%M')))
# 1=Mon … 5=Fri · 6–7 skip
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend"
  exit 0
fi
if [[ "${H}" -lt 9 ]] || [[ "${H}" -gt 13 ]] || { [[ "${H}" -eq 13 ]] && [[ "${M}" -gt 30 ]]; }; then
  echo "skip: outside 09:00–13:30"
  exit 0
fi

# Skip if a previous tick is still running (avoids stacked hung polls).
# mkdir-lock：macOS 無 util-linux flock；勿再用 flock（會誤判 skip 整日）.
LOCK_DIR="${ROOT}/logs/intraday/rrg_c18acc_poll.lockdir"
_release_c18acc_lock() {
  rm -rf "${LOCK_DIR}" 2>/dev/null || true
}
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  _old_pid="$(cat "${LOCK_DIR}/pid" 2>/dev/null || true)"
  if [[ -n "${_old_pid}" ]] && kill -0 "${_old_pid}" 2>/dev/null; then
    echo "skip: previous poll still running pid=${_old_pid}"
    exit 0
  fi
  rm -rf "${LOCK_DIR}" 2>/dev/null || true
  if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    echo "skip: previous poll still running"
    exit 0
  fi
fi
echo $$ >"${LOCK_DIR}/pid"
trap _release_c18acc_lock EXIT
rm -f "${ROOT}/logs/intraday/rrg_c18acc_poll.lock" 2>/dev/null || true

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
# Load project .env first so ORDER_*/C18ACC_* live flags win over launcher defaults.
# Never abort the tick if Documents TCC blocks .env (launchd symptom 2026-07-13).
if [[ -f "${ETF_DATA_DIR:-${ROOT}}/.env" ]]; then
  set +e
  set -a
  # shellcheck disable=SC1091
  source "${ETF_DATA_DIR:-${ROOT}}/.env"
  _env_rc=$?
  set +a
  set -e
  if [[ "${_env_rc}" -ne 0 ]]; then
    echo "WARN: cannot source .env rc=${_env_rc} · continue with defaults"
  fi
fi
unset _env_rc 2>/dev/null || true
export RUN_RRG_C18ACC_SCREEN="${RUN_RRG_C18ACC_SCREEN:-1}"
export ORDER_C18ACC_ORDER_ENABLED="${ORDER_C18ACC_ORDER_ENABLED:-1}"
export ORDER_C18ACC_AUTO_SUBMIT="${ORDER_C18ACC_AUTO_SUBMIT:-1}"
export ORDER_C18ACC_DRY_RUN="${ORDER_C18ACC_DRY_RUN:-0}"
export ORDER_C18ACC_APPLY_STATE="${ORDER_C18ACC_APPLY_STATE:-1}"
export ORDER_RESERVED_CASH_TWD="${ORDER_RESERVED_CASH_TWD:-50000}"
export C18ACC_CONFIRM_BARS="${C18ACC_CONFIRM_BARS:-1}"
export C18ACC_NO_TRADE_BEFORE="${C18ACC_NO_TRADE_BEFORE:-13:00}"
export C18ACC_PYRAMID_ADD_ENABLED="${C18ACC_PYRAMID_ADD_ENABLED:-1}"
export C18ACC_KBAR_SYNC="${C18ACC_KBAR_SYNC:-1}"
export RUN_RRG_C18ACC_EMAIL="${RUN_RRG_C18ACC_EMAIL:-0}"
export RUN_C18ACC_EXTENSION_OVERLAY="${RUN_C18ACC_EXTENSION_OVERLAY:-0}"
export C18ACC_EXTENSION_MODE="${C18ACC_EXTENSION_MODE:-combo_spike}"
export C18ACC_EXTENSION_MIN_SPIKE_PCT="${C18ACC_EXTENSION_MIN_SPIKE_PCT:-4}"
export C18ACC_EXTENSION_MIN_FADE_PCT="${C18ACC_EXTENSION_MIN_FADE_PCT:-1.0}"
export C18ACC_EXTENSION_POLL_MIN="${C18ACC_EXTENSION_POLL_MIN:-1}"
export C18ACC_EXTENSION_WATCH_HEAT="${C18ACC_EXTENSION_WATCH_HEAT:-70}"
export C18ACC_EXTENSION_HEAT="${C18ACC_EXTENSION_HEAT:-70}"
export C18ACC_EXTENSION_DRY_RUN="${C18ACC_EXTENSION_DRY_RUN:-1}"
export C18ACC_EXTENSION_EMIT_INTENT="${C18ACC_EXTENSION_EMIT_INTENT:-0}"
export C18ACC_EXTENSION_APPLY_STATE="${C18ACC_EXTENSION_APPLY_STATE:-0}"
# 候選池：昨收 PIT fresh mono 全池（僅 .env 設 C18ACC_POOL_OVERRIDE 時才用手動名單）

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/run_rrg_mono_swap_accel_screen.py" 2>&1)"
EXIT=$?
set -e
echo "${OUT}"

if echo "${OUT}" | grep -q 'ORDER_FILL_REPORT_SENT=1'; then
  echo "fill report already emailed · skip legacy C18acc signal notify"
elif echo "${OUT}" | grep -q 'C18ACC_SIGNAL=1'; then
  # 直接呼叫 venv python（勿 exec Documents/*.sh · TCC 會擋）
  EXTRA_LINES="$(echo "${OUT}" | grep -E 'C18acc (screen|intent)|C18ACC_SIGNAL|ORDER_' || true)"
  EXTRA=$'C18acc 本輪有動作，但尚未產生成交報告（可能 dry-run／送單未回傳）。\n'"${EXTRA_LINES}"$'\n'
  STAMP="$(date '+%Y%m%d')"
  for rel in \
    "reports/daily/${STAMP}_rrg_c18acc_screen.md" \
    "reports/daily/rrg_c18acc_screen.md" \
    "logs/intraday/rrg_c18acc_poll_tick.log"; do
    [[ -f "${ROOT}/${rel}" ]] && EXTRA+="${rel}"$'\n'
  done
  set +e
  "${PYTHON}" "${ROOT}/scripts/notify_job_result.py" \
    --subject-prefix="C18acc 動作（無成交報告）" \
    --exit-code="${EXIT}" \
    --log-path="${LAUNCHD_LOG}" \
    --extra="${EXTRA}" \
    --env-flag="RUN_RRG_C18ACC_EMAIL"
  set -e
fi

echo "=== launchd rrg-c18acc-poll end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
