#!/usr/bin/env bash
# ETF 核心 daily sync（4 項必留）：
#   1. 5 檔 ETF + IX0001 + IR0002 → daily_bars (TEJ)
#   2. EZMoney / 凱基官網持股 → etf_holdings
#   3. 法人 snapshot → etf_daily_signal_snapshot (FinMind)
#   4. 持股 changes 輸出（需 ≥2 個 snapshot_date）

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
DB="${ROOT}/data/stocks.db"
LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/daily_sync_$(date '+%Y%m%d').log"

ETF_CODES="00981A,00403A,009816,00988A,00407A"
ETF_CODES_EZMONEY="00981A,00403A,00988A"
ETF_CODES_KGIFUND="009816,00407A"
ETF_CODES_HOLDINGS="${ETF_CODES_EZMONEY},${ETF_CODES_KGIFUND}"

log_line() {
  echo "$@"
  echo "$@" >>"$LOG_FILE"
}

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
  log_line "已載入 .env（TEJ_API_KEY=$([ -n "${TEJ_API_KEY:-}" ] && echo set || echo missing)）"
else
  log_line "警告：未找到 .env，TEJ 同步可能失敗"
fi

MODE="${1:-}"
MARKET=1
HOLDINGS=1

case "$MODE" in
  "" ) ;;
  --retry ) ;;
  --market-only ) HOLDINGS=0 ;;
  --holdings-only ) MARKET=0 ;;
  * )
    echo "Usage: $0 [--retry|--market-only|--holdings-only]" >&2
    exit 2
    ;;
esac

log_line "daily_sync 執行中… 完整 log：${LOG_FILE}"
log_line ""

FAILED=0

run_step() {
  local label="$1"
  shift
  log_line "--- ${label} ---"
  if env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
    "$@" 2>&1 | tee -a "$LOG_FILE"; then
    log_line "OK: ${label}"
  else
    log_line "FAILED: ${label}"
    FAILED=1
  fi
}

print_db_summary() {
  log_line "--- DB 摘要（$(date '+%H:%M:%S')）---"
  if [[ ! -f "$DB" ]]; then
    log_line "  stocks.db 不存在"
    return
  fi
  sqlite3 -header -column "$DB" "
    SELECT code AS 代號, MAX(date) AS 最新交易日, COUNT(*) AS 筆數, source AS 來源
    FROM daily_bars
    WHERE code IN ('00981A','00403A','009816','00988A','00407A','IX0001','IR0002')
      AND source = 'tej'
    GROUP BY code, source
    ORDER BY code;
  " 2>/dev/null | tee -a "$LOG_FILE" || log_line "  daily_bars 查詢失敗"
  sqlite3 -header -column "$DB" "
    SELECT code AS 代號, MAX(snapshot_date) AS 最新日, COUNT(*) AS 筆數
    FROM etf_daily_signal_snapshot
    WHERE code IN ('00981A','00403A','009816','00988A','00407A')
    GROUP BY code
    ORDER BY code;
  " 2>/dev/null | tee -a "$LOG_FILE" || true
  sqlite3 -header -column "$DB" "
    SELECT etf_code AS 代號, MAX(snapshot_date) AS 最新日,
           MAX(holding_count) AS 檔數, source AS 來源
    FROM etf_holdings_meta
    WHERE etf_code IN ('00981A','00403A','00988A','009816','00407A')
    GROUP BY etf_code, source
    ORDER BY etf_code;
  " 2>/dev/null | tee -a "$LOG_FILE" || true
}

print_repeat_help() {
  log_line "--- 重複按會怎樣 ---"
  log_line "  [1] 日線 TEJ      upsert 覆寫，不會多出一筆；只刷新 synced_at"
  log_line "  [2] 法人 FinMind  upsert 覆寫，同上"
  log_line "  [3] EZMoney/凱基持股  官網未更新 → Skip，DB 不變（正常）"
  log_line "  [4] changes       僅印報表，不寫 DB；≥2 個 snapshot 日才有表"
  log_line "  同天連按：安全，不會重複累加 snapshot 日"
  log_line "  新 snapshot 日：需官網更新到下一個交易日（持股漏跑一天無法補）"
}

log_line "=== daily_sync $(date '+%Y-%m-%dT%H:%M:%S%z') mode=${MODE:-primary} pid=$$ ==="

if [[ "$MARKET" -eq 1 ]]; then
  run_step "core market (5 ETFs + benchmarks)" \
    "$PYTHON" query_stock_prices.py \
    --sync-db --sync-mode hybrid \
    --skip-watchlist \
    --benchmark-codes IX0001,IR0002 \
    --etf-codes "$ETF_CODES" \
    --history-days 90

  run_step "ETF signal snapshot (FinMind)" \
    "$PYTHON" sync_etf_signal.py --etf-codes "$ETF_CODES" --lookback-days 14
fi

if [[ "$HOLDINGS" -eq 1 ]]; then
  run_step "ETF holdings EZMoney (3)" \
    "$PYTHON" sync_etf_holdings.py --etf-codes "$ETF_CODES_EZMONEY" --source ezmoney

  run_step "ETF holdings KGIFund (2)" \
    "$PYTHON" sync_etf_holdings.py --etf-codes "$ETF_CODES_KGIFUND" --source kgifund

  log_line "--- holdings changes (if >=2 snapshots) ---"
  "$PYTHON" sync_etf_holdings.py --etf-codes "$ETF_CODES_HOLDINGS" --changes 2>&1 | tee -a "$LOG_FILE" || true
fi

print_db_summary
print_repeat_help
log_line "=== daily_sync finished exit=${FAILED} ==="

echo ""
if [[ "$FAILED" -eq 0 ]]; then
  echo "✓ daily_sync 完成 (exit=0)"
else
  echo "✗ daily_sync 有失敗步驟 (exit=${FAILED})"
fi
echo "完整 log：${LOG_FILE}"
exit "$FAILED"
