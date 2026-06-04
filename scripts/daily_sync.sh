#!/usr/bin/env bash
# ETF 核心 daily sync（持股研究為主）：
#   1. 6 檔已掛牌 ETF + IX0001 + IR0002 → daily_bars (TEJ only)
#   2. EZMoney / 凱基 / 群益 / 野村官網持股 → etf_holdings
#   3. TSM ADR / SOX / 台指期 gap → tech_risk_daily_snapshot
#   4. 持股 changes + 跨 ETF 共識（≥2 snapshot_date）
# 選用：ENABLE_FINMIND_SIGNAL=1 才跑法人（需 FinMind 權限；403 時請勿開啟）

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
SRC="${ROOT}/src"
export PYTHONPATH="${SRC}${PYTHONPATH:+:${PYTHONPATH}}"

DB="${ROOT}/data/stocks.db"
LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/daily_sync_$(date '+%Y%m%d').log"

# 00407A 未掛牌：不拉 TEJ 日線（仍可在 KGIFUND 持股步驟 SKIP）
ETF_CODES="00981A,00403A,009816,00980A,00982A,00992A"
ETF_CODES_EZMONEY="00981A,00403A"
ETF_CODES_KGIFUND="009816,00407A"
ETF_CODES_CAPITALFUND="00982A,00992A"
ETF_CODES_NOMURA="00980A"
ETF_CODES_HOLDINGS="${ETF_CODES_EZMONEY},${ETF_CODES_KGIFUND},${ETF_CODES_CAPITALFUND},${ETF_CODES_NOMURA}"

QUIET=0
MODE=""
for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=1 ;;
    --retry|--market-only|--holdings-only) MODE="$arg" ;;
    * )
      echo "Usage: $0 [--quiet] [--retry|--market-only|--holdings-only]" >&2
      exit 2
      ;;
  esac
done

PYTHON_QUIET=()
[[ "$QUIET" -eq 1 ]] && PYTHON_QUIET=(--quiet)

log_line() {
  if [[ "$QUIET" -eq 1 ]]; then
    echo "$@" >>"$LOG_FILE"
  else
    echo "$@"
    echo "$@" >>"$LOG_FILE"
  fi
}

log_only() {
  echo "$@" >>"$LOG_FILE"
}

pipe_out() {
  if [[ "$QUIET" -eq 1 ]]; then
    cat >>"$LOG_FILE"
  else
    tee -a "$LOG_FILE"
  fi
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

MARKET=1
HOLDINGS=1

case "$MODE" in
  "" ) ;;
  --retry ) ;;
  --market-only ) HOLDINGS=0 ;;
  --holdings-only ) MARKET=0 ;;
esac

if [[ "$QUIET" -eq 1 ]]; then
  echo "daily_sync (quiet) → ${LOG_FILE}"
  log_only "daily_sync 執行中（quiet）… 完整 log：${LOG_FILE}"
  log_only ""
else
  log_line "daily_sync 執行中… 完整 log：${LOG_FILE}"
  log_line ""
fi

FAILED=0
AUX_FAILED=0

_run_step_inner() {
  local label="$1"
  local fail_kind="$2"
  shift 2
  local ok=0
  log_line "--- ${label} ---"
  if [[ "$QUIET" -eq 1 ]]; then
    if env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
      "$@" >>"$LOG_FILE" 2> >(tee -a "$LOG_FILE" >&2); then
      echo "OK: ${label}"
      log_only "OK: ${label}"
      ok=1
    else
      echo "WARN: ${label}" >&2
      log_only "WARN: ${label}"
    fi
  elif env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
    "$@" 2>&1 | tee -a "$LOG_FILE"; then
    log_line "OK: ${label}"
    ok=1
  else
    log_line "WARN: ${label}"
  fi
  if [[ "$ok" -eq 0 ]]; then
    if [[ "$fail_kind" == "holdings" ]]; then
      FAILED=1
    else
      AUX_FAILED=1
    fi
  fi
}

run_step() {
  _run_step_inner "$1" holdings "${@:2}"
}

run_step_optional() {
  _run_step_inner "$1" aux "${@:2}"
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
    WHERE code IN ('00981A','00403A','009816','00980A','00982A','00992A','IX0001','IR0002')
      AND source = 'tej'
    GROUP BY code, source
    ORDER BY code;
  " 2>/dev/null | pipe_out || log_line "  daily_bars 查詢失敗"
  sqlite3 -header -column "$DB" "
    SELECT code AS 代號, MAX(snapshot_date) AS 最新日, COUNT(*) AS 筆數
    FROM etf_daily_signal_snapshot
    WHERE code IN ('00981A','00403A','009816','00980A','00982A','00992A')
    GROUP BY code
    ORDER BY code;
  " 2>/dev/null | pipe_out || true
  sqlite3 -header -column "$DB" "
    SELECT etf_code AS 代號, MAX(snapshot_date) AS 最新日,
           MAX(holding_count) AS 檔數, source AS 來源
    FROM etf_holdings_meta
    WHERE etf_code IN ('00981A','00403A','009816','00407A','00980A','00982A','00992A')
    GROUP BY etf_code, source
    ORDER BY etf_code;
  " 2>/dev/null | pipe_out || true
  sqlite3 -header -column "$DB" "
    SELECT session_date AS 台股日, us_trade_date AS 美股日,
           printf('%.2f%%', tsm_daily_return_pct) AS TSM,
           printf('%.2f%%', COALESCE(sox_daily_return_pct, smh_daily_return_pct)) AS 半導體,
           printf('%.2f%%', tx_gap_pct) AS 台指gap,
           printf('%.2f%%', te_overnight_pct) AS 電子期
    FROM tech_risk_daily_snapshot
    ORDER BY session_date DESC
    LIMIT 1;
  " 2>/dev/null | pipe_out || true
  sqlite3 -header -column "$DB" "
    SELECT COUNT(DISTINCT stock_id) AS 成分股數,
           COUNT(*) AS K線筆數,
           MAX(trade_date) AS 最新交易日
    FROM stock_daily_bars WHERE source = 'finmind';
  " 2>/dev/null | pipe_out || true
  sqlite3 -header -column "$DB" "
    SELECT COUNT(DISTINCT stock_id) AS 成分股數,
           COUNT(*) AS 法人筆數,
           MAX(trade_date) AS 最新交易日
    FROM stock_institutional_daily WHERE source = 'finmind';
  " 2>/dev/null | pipe_out || true
}

print_repeat_help() {
  log_line "--- 重複按會怎樣 ---"
  log_line "  [1] 日線 TEJ      upsert 覆寫，不會多出一筆；只刷新 synced_at"
  log_line "  [2] 持股（EZMoney/凱基/群益/野村）官網未更新 → Skip，DB 不變（正常）"
  log_line "  [3] 科技風險      TSM/SOX 日線 + 台指 gap → tech_risk_daily_snapshot（upsert）"
  log_line "  [4] changes       僅印報表，不寫 DB；≥2 個 snapshot 日才有表"
  log_line "                  附 grow%、flow（EZMoney amount）、跨 ETF 共識、beta"
  log_line "  同天連按：安全，不會重複累加 snapshot 日"
  log_line "  新 snapshot 日：需官網更新到下一個交易日（持股漏跑一天無法補）"
}

SYNC_PROFILE="${SYNC_PROFILE:-}"
if [[ -n "$SYNC_PROFILE" ]]; then
  log_line "=== daily_sync 排程=${SYNC_PROFILE} $(date '+%Y-%m-%dT%H:%M:%S%z') mode=${MODE:-primary} pid=$$ ==="
else
  log_line "=== daily_sync $(date '+%Y-%m-%dT%H:%M:%S%z') mode=${MODE:-primary} pid=$$ ==="
fi

if [[ "$MARKET" -eq 1 ]]; then
  run_step_optional "core market (6 ETFs + benchmarks, TEJ)" \
    "$PYTHON" "${SRC}/query_stock_prices.py" \
    "${PYTHON_QUIET[@]}" \
    --sync-db --sync-mode hybrid \
    --benchmark-codes IX0001,IR0002 \
    --etf-codes "$ETF_CODES" \
    --history-days 90

  if [[ "${ENABLE_FINMIND_SIGNAL:-0}" == "1" ]]; then
    run_step_optional "ETF signal snapshot (FinMind)" \
      "$PYTHON" "${SRC}/sync_etf_signal.py" \
      "${PYTHON_QUIET[@]}" \
      --etf-codes "$ETF_CODES" --lookback-days 14
  else
    log_line "--- ETF signal snapshot (FinMind) ---"
    log_line "  SKIP（預設關閉；FinMind 403/402 時請勿開啟）"
    log_line "  若要啟用：ENABLE_FINMIND_SIGNAL=1 scripts/daily_sync.sh"
  fi

  run_step_optional "tech risk context (TSM/SOX/TX gap)" \
    "$PYTHON" "${SRC}/sync_tech_risk_context.py" \
    "${PYTHON_QUIET[@]}" \
    --sync-db --history-days 90
fi

if [[ "$HOLDINGS" -eq 1 ]]; then
  run_step "ETF holdings EZMoney (2)" \
    "$PYTHON" "${SRC}/sync_etf_holdings.py" --no-auto-changes \
    "${PYTHON_QUIET[@]}" \
    --etf-codes "$ETF_CODES_EZMONEY" --source ezmoney

  run_step "ETF holdings KGIFund (2)" \
    "$PYTHON" "${SRC}/sync_etf_holdings.py" --no-auto-changes \
    "${PYTHON_QUIET[@]}" \
    --etf-codes "$ETF_CODES_KGIFUND" --source kgifund

  run_step "ETF holdings CapitalFund (2)" \
    "$PYTHON" "${SRC}/sync_etf_holdings.py" --no-auto-changes \
    "${PYTHON_QUIET[@]}" \
    --etf-codes "$ETF_CODES_CAPITALFUND" --source capitalfund

  run_step "ETF holdings Nomura (1)" \
    "$PYTHON" "${SRC}/sync_etf_holdings.py" --no-auto-changes \
    "${PYTHON_QUIET[@]}" \
    --etf-codes "$ETF_CODES_NOMURA" --source nomura

  if [[ "${RUN_STOCK_MARKET_SYNC:-0}" == "1" ]]; then
    run_step_optional "constituent market+institutional (FinMind)" \
      "$PYTHON" "${SRC}/sync_stock_market_daily.py" \
      "${PYTHON_QUIET[@]}" \
      --sync-db --lookback-days "${STOCK_MARKET_LOOKBACK_DAYS:-60}"
  else
    log_line "--- constituent stock market (FinMind) ---"
    log_line "  SKIP（RUN_STOCK_MARKET_SYNC=0；設 1 啟用成分股價+法人）"
  fi

  log_line "--- holdings changes + 跨 ETF 共識 ---"
  "$PYTHON" "${SRC}/sync_etf_holdings.py" --etf-codes "$ETF_CODES_HOLDINGS" --changes --intent 2>&1 | tee -a "$LOG_FILE" || true
fi

print_db_summary
print_repeat_help
log_line "=== daily_sync finished exit=${FAILED} ==="

echo ""
if [[ "$FAILED" -eq 0 ]]; then
  if [[ "$AUX_FAILED" -eq 1 ]]; then
    echo "✓ 持股研究完成 (exit=0)；日線/法人未更新，見 log"
  else
    echo "✓ daily_sync 完成 (exit=0)"
  fi
else
  echo "✗ 持股同步失敗 (exit=${FAILED})"
fi
echo "完整 log：${LOG_FILE}"
exit "$FAILED"
