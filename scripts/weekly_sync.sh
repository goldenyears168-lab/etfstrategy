#!/usr/bin/env bash
# 方案 C · 排程③「週日深度補庫」：Beta +（P0+ 後）基本面、成分股批次。
# 建議：週日 20:00；不取代平日早盤／收盤排程。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
SRC="${ROOT}/src"
export PYTHONPATH="${SRC}${PYTHONPATH:+:${PYTHONPATH}}"
LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/weekly_sync_$(date '+%Y%m%d').log"

WEEKLY_REPORT=0
for arg in "$@"; do
  case "$arg" in
    --weekly-report) WEEKLY_REPORT=1 ;;
    * )
      echo "Usage: $0 [--weekly-report]" >&2
      exit 2
      ;;
  esac
done

log_line() {
  echo "$@"
  echo "$@" >>"$LOG_FILE"
}

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  eval "$("$PYTHON" -c "from project_dotenv import shell_export_dotenv; print(shell_export_dotenv())")"
  set +a
fi

if [[ "$WEEKLY_REPORT" -eq 1 ]]; then
  echo "weekly_sync（週報 → 終端 + log）→ ${LOG_FILE}"
fi
log_line "=== weekly-deep 週日深度補庫 $(date '+%Y-%m-%dT%H:%M:%S%z') ==="

FAILED=0

run_step() {
  local label="$1"
  shift
  log_line "--- ${label} ---"
  if [[ "$WEEKLY_REPORT" -eq 1 ]]; then
    if "$@" 2>&1 | tee -a "$LOG_FILE"; then
      log_line "OK: ${label}"
    else
      log_line "WARN: ${label}"
      FAILED=1
    fi
  elif "$@" >>"$LOG_FILE" 2>&1; then
    log_line "OK: ${label}"
  else
    log_line "WARN: ${label}"
    FAILED=1
  fi
}

run_step "stock_beta (FinMind/Yahoo)" \
  "$PYTHON" "${SRC}/sync_stock_beta.py" --sync-db

run_step "benchmark constituents (0050)" \
  "$PYTHON" "${SRC}/sync_benchmark_constituents.py" --quiet

if [[ -f "${SRC}/sync_fundamentals.py" ]]; then
  run_step "fundamentals (L8/L8.5)" \
    "$PYTHON" "${SRC}/sync_fundamentals.py" --sync-db
else
  log_line "SKIP: sync_fundamentals.py 不存在"
fi

if [[ "${RUN_STOCK_MARKET_SYNC:-0}" == "1" ]]; then
  run_step "constituent market+institutional batch" \
    "$PYTHON" "${SRC}/sync_stock_market_daily.py" \
    --sync-db --lookback-days "${STOCK_MARKET_LOOKBACK_DAYS:-90}" --quiet
else
  log_line "SKIP: constituent market（RUN_STOCK_MARKET_SYNC=0）"
fi

if [[ "${RUN_CHIP_SYNC:-0}" == "1" ]]; then
  run_step "chip margin/lending/daytrade batch" \
    "$PYTHON" "${SRC}/sync_stock_chip_daily.py" \
    --sync-db --lookback-days "${CHIP_LOOKBACK_DAYS:-21}" --quiet
else
  log_line "SKIP: chip extended（RUN_CHIP_SYNC=0）"
fi

if [[ "${RUN_SPONSOR_CHIP_SYNC:-0}" == "1" ]]; then
  run_step "Sponsor branch+block (Top N)" \
    "$PYTHON" "${SRC}/sync_stock_sponsor_daily.py" \
    --sync-db --top-n "${SPONSOR_CHIP_TOP_N:-20}" --quiet
else
  log_line "SKIP: Sponsor 分點/鉅額（RUN_SPONSOR_CHIP_SYNC=0）"
fi

if [[ "${RUN_FACTOR_VALIDATION:-0}" == "1" ]]; then
  run_step "factor validation (alphalens-style)" \
    "$PYTHON" "${ROOT}/scripts/run_factor_validation.py" --write-reports --quiet
else
  log_line "SKIP: factor validation（RUN_FACTOR_VALIDATION=0）"
fi

if [[ "$WEEKLY_REPORT" -eq 1 ]]; then
  "$PYTHON" "${SRC}/report_summary.py" --mode weekly 2>&1 | tee -a "$LOG_FILE" || true
fi

log_line "=== weekly-deep finished exit=${FAILED} ==="
exit "$FAILED"
