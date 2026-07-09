#!/usr/bin/env bash
# launchd：Buy signal radar · 09:00–13:20 每 5 分 · advisory email only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_buy-signal-radar.log"
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
if [[ "${H}" -lt 9 ]] || [[ "${H}" -gt 13 ]] || { [[ "${H}" -eq 13 ]] && [[ "${M}" -gt 20 ]]; }; then
  echo "skip: outside window $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd buy-signal-radar tick $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_BUY_SIGNAL_RADAR="${RUN_BUY_SIGNAL_RADAR:-1}"
export RUN_BUY_SIGNAL_EMAIL="${RUN_BUY_SIGNAL_EMAIL:-1}"
export RUN_ABC_V3_F1_SUBMIT_EMAIL="${RUN_ABC_V3_F1_SUBMIT_EMAIL:-1}"
export ABC_V3_F1_ORDER_ENABLED="${ABC_V3_F1_ORDER_ENABLED:-1}"
export ABC_V3_F1_AUTO_SUBMIT="${ABC_V3_F1_AUTO_SUBMIT:-1}"
export ABC_V3_F1_BUDGET_TWD="${ABC_V3_F1_BUDGET_TWD:-20000}"
export ABC_V3_F1_MAX_ENTRIES_DAY="${ABC_V3_F1_MAX_ENTRIES_DAY:-5}"
export ABC_V3_F1_HOLD_DAYS="${ABC_V3_F1_HOLD_DAYS:-3}"
export ABC_V3_F1_REENTRY_DISCOUNT_PCT="${ABC_V3_F1_REENTRY_DISCOUNT_PCT:-2.0}"
export ABC_V3_F1_MAX_NOTIONAL_SYMBOL="${ABC_V3_F1_MAX_NOTIONAL_SYMBOL:-60000}"
export C18ACC_KBAR_SYNC="${C18ACC_KBAR_SYNC:-1}"
export SIGNAL_RADAR_USE_FUBON="${SIGNAL_RADAR_USE_FUBON:-1}"

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/run_buy_signal_radar.py" 2>&1)"
EXIT=$?
set -e
echo "${OUT}"

if echo "${OUT}" | grep -q 'BUY_SIGNAL_RADAR=1'; then
  EXTRA_LINES="$(echo "${OUT}" | grep -E \
    'BUY_SIGNAL_RADAR|買進|觀測命中|多軌重疊|▸ ABC v3|已送單|自動下單|· 略過|buy ' || true)"
  export JOB_NOTIFY_EXTRA=$'買進 / ABC v3·f1 observe 命中（advisory · 已啟用自動下單）\n'"${EXTRA_LINES}"
  "${ROOT}/scripts/buy_signal_notify.sh" "${EXIT}" || true
elif echo "${OUT}" | grep -q 'ABC_V3_F1_SUBMIT=1'; then
  EXTRA_LINES="$(echo "${OUT}" | grep -E \
    'ABC_V3_F1_SUBMIT|▸ ABC v3|已送單|成交|略過' || true)"
  export JOB_NOTIFY_EXTRA=$'ABC v3+f1 委託已送出（重試成功 · 非 observe 首次信）\n'"${EXTRA_LINES}"
  RUN_BUY_SIGNAL_EMAIL="${RUN_ABC_V3_F1_SUBMIT_EMAIL:-1}" \
    "${ROOT}/scripts/buy_signal_notify.sh" "${EXIT}" || true
fi

echo "=== launchd buy-signal-radar end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
