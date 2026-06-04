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

log_line() {
  echo "$@"
  echo "$@" >>"$LOG_FILE"
}

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

log_line "=== weekly-deep 週日深度補庫 $(date '+%Y-%m-%dT%H:%M:%S%z') ==="

FAILED=0

run_step() {
  local label="$1"
  shift
  log_line "--- ${label} ---"
  if "$@" >>"$LOG_FILE" 2>&1; then
    log_line "OK: ${label}"
  else
    log_line "WARN: ${label}"
    FAILED=1
  fi
}

run_step "stock_beta (FinMind/Yahoo)" \
  "$PYTHON" "${SRC}/sync_stock_beta.py" --sync-db

if [[ -f "${SRC}/sync_fundamentals.py" ]]; then
  run_step "fundamentals (L8/L8.5)" \
    "$PYTHON" "${SRC}/sync_fundamentals.py" --sync-db
else
  log_line "SKIP: sync_fundamentals.py（尚未實作，見 PRD §22）"
fi

if [[ -f "${SRC}/sync_stock_market_daily.py" ]]; then
  run_step "constituent market+institutional batch" \
    "$PYTHON" "${SRC}/sync_stock_market_daily.py" --sync-db
else
  log_line "SKIP: sync_stock_market_daily.py（尚未實作，見 PRD §22）"
fi

log_line "=== weekly-deep finished exit=${FAILED} ==="
exit "$FAILED"
