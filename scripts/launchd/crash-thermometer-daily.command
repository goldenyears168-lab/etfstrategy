#!/usr/bin/env bash
# launchd / 手動：大跌溫度計（加權8家 + 共識家數）
# 週一至五 09:00 · 通常 asof＝上一交易日（當日 bars/分點尚未齊）· **不寄信**
# 當日收盤後讀數＋郵件：ops-console-evening-sync（20:40）重算＋夜間一封摘要
# 不下單

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
LAUNCHD_LOG="${ROOT}/logs/launchd_crash-thermometer-daily.log"
RUN_LOG="${ROOT}/logs/crash_thermometer_${STAMP}.log"

mkdir -p "${ROOT}/logs"
exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd crash-thermometer-daily 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"

if [[ -r "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/.env" ]]; then
  set +e
  set -a
  # shellcheck disable=SC1090
  source "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/.env" 2>/dev/null
  set +a
  set -e
fi

PYTHON="${ROOT}/.venv/bin/python"
EXIT=0
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  EXIT=1
else
  # 溫度計8家分點需要「全市場」net(股)覆蓋（非 branch-tape-prewarm 的
  # POOLS∪持倉窄範圍），先強制重抓近7個交易日確保完整覆蓋（見 2026-07-23 修正）。
  set +e
  "${PYTHON}" "${ROOT}/scripts/research/backfill_broker_branch_tape.py" \
    --universe-json "${ROOT}/reports/research/branch-footprint-screen/crash_thermometer_panel_universe.json" \
    --start "$(date -v-7d '+%Y-%m-%d' 2>/dev/null || date -d '7 days ago' '+%Y-%m-%d')" \
    --end "$(date '+%Y-%m-%d')" \
    --force --fetch-workers 2 --delay 0.6 \
    2>&1 | tee -a "${RUN_LOG}"
  set -e

  set +e
  # 晨間多半只有昨收資料；不寄信（當日讀數＋郵件改 20:40 evening sync）
  "${PYTHON}" "${ROOT}/scripts/research/run_market_crash_thermometer_dashboard.py" \
    2>&1 | tee -a "${RUN_LOG}"
  EXIT=${PIPESTATUS[0]}
  set -e
fi

echo "=== launchd crash-thermometer-daily 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ "${EXIT}" -ne 0 ]]; then
  export JOB_NOTIFY_EXTRA="${JOB_NOTIFY_EXTRA:-大跌溫度計執行失敗，詳見 log}"
  /bin/bash "${ROOT}/scripts/job_notify.sh" \
    "大跌溫度計" "${EXIT}" "${RUN_LOG}" RUN_CRASH_THERMOMETER_EMAIL || true
fi

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
