#!/usr/bin/env bash
# ETF 核心 daily sync（持股研究為主）：
#   1. 6 檔已掛牌 ETF + IX0001 + IR0002 → daily_bars（TEJ 優先，FinMind 備援）
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

ensure_python_deps() {
  if ! "$PYTHON" -c "import yaml" 2>/dev/null; then
    log_line "  安裝缺漏依賴 PyYAML（E0 IPS 需要）…"
    "$PYTHON" -m pip install -q PyYAML
  fi
}

DB="${ROOT}/data/stocks.db"
LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/daily_sync_$(date '+%Y%m%d').log"

# 00407A 未掛牌：不拉 TEJ 日線（仍可在 KGIFUND 持股步驟 SKIP）
eval "$("$PYTHON" "${SRC}/project_config.py" shell-export)"

QUIET=0
SHOW_REPORT=0
MORNING_REPORT=0
EXECUTION_EVAL=0
MODE=""
for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=1 ;;
    --holdings-report) QUIET=1; SHOW_REPORT=1 ;;
    --execution-eval|--morning-report) QUIET=1; MORNING_REPORT=1; EXECUTION_EVAL=1 ;;
    --retry|--market-only|--holdings-only) MODE="$arg" ;;
    * )
      echo "Usage: $0 [--quiet|--holdings-report|--execution-eval|--morning-report] [--retry|--market-only|--holdings-only]" >&2
      exit 2
      ;;
  esac
done

PYTHON_QUIET=()
[[ "$QUIET" -eq 1 ]] && PYTHON_QUIET=(--quiet)

_report_to_terminal() {
  [[ "$MORNING_REPORT" -eq 1 || "$SHOW_REPORT" -eq 1 ]]
}

log_line() {
  if [[ "$QUIET" -eq 1 || "$SHOW_REPORT" -eq 1 ]] && ! _report_to_terminal; then
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
  if [[ "$QUIET" -eq 1 ]] && ! _report_to_terminal; then
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
  # 有 Perplexity 金鑰且未明確關閉時，預設收盤拉新聞
  if [[ -n "${PERPLEXITY_API_KEY:-}" && "${RUN_NEWS_SYNC:-}" != "0" ]]; then
    RUN_NEWS_SYNC=1
  fi
  if [[ -n "${PERPLEXITY_API_KEY:-}" && "${RUN_PERPLEXITY_SUMMARY:-}" != "0" ]]; then
    RUN_PERPLEXITY_SUMMARY=1
  fi
  if [[ -n "${PERPLEXITY_API_KEY:-}" && "${RUN_PERPLEXITY_VERIFY:-}" != "0" ]]; then
    RUN_PERPLEXITY_VERIFY=1
  fi
  log_line "已載入 .env（TEJ=$([ -n "${TEJ_API_KEY:-}" ] && echo set || echo missing) FinMind=$([ -n "${FINMIND_TOKEN:-}" ] && echo set || echo missing) Perplexity=$([ -n "${PERPLEXITY_API_KEY:-}" ] && echo set || echo missing)）"
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

if [[ "$SHOW_REPORT" -eq 1 ]]; then
  echo "收盤持股雷達執行中… 終端僅顯示摘要 · 詳細 log → ${LOG_FILE}"
  log_only "daily_sync 執行中（holdings-report / human digest）… ${LOG_FILE}"
  log_only ""
elif [[ "$MORNING_REPORT" -eq 1 ]]; then
  echo "daily_sync（執行評估 → 終端 + log）→ ${LOG_FILE}"
  log_line "daily_sync 執行中（execution-eval）… 完整 log：${LOG_FILE}"
  log_line ""
elif [[ "$QUIET" -eq 1 ]]; then
  echo "daily_sync (quiet) → ${LOG_FILE}"
  log_only "daily_sync 執行中（quiet）… 完整 log：${LOG_FILE}"
  log_only ""
else
  log_line "daily_sync 執行中… 完整 log：${LOG_FILE}"
  log_line ""
fi

FAILED=0
AUX_FAILED=0
SYNC_T0=$(date +%s)

_step_elapsed() {
  local t_start=$1
  echo $(( $(date +%s) - t_start ))
}

_run_step_inner() {
  local label="$1"
  local fail_kind="$2"
  shift 2
  local ok=0
  local t_start
  t_start=$(date +%s)
  log_line "--- ${label} ---"
  if [[ "$QUIET" -eq 1 ]]; then
    if env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
      "$@" >>"$LOG_FILE" 2> >(tee -a "$LOG_FILE" >&2); then
      if [[ "$SHOW_REPORT" -ne 1 ]]; then
        echo "OK: ${label} ($(_step_elapsed "$t_start")s)"
      fi
      log_only "OK: ${label} ($(_step_elapsed "$t_start")s)"
      ok=1
    else
      echo "WARN: ${label} ($(_step_elapsed "$t_start")s)" >&2
      log_only "WARN: ${label} ($(_step_elapsed "$t_start")s)"
    fi
  elif env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
    "$@" 2>&1 | tee -a "$LOG_FILE"; then
    log_line "OK: ${label} ($(_step_elapsed "$t_start")s)"
    ok=1
  else
    log_line "WARN: ${label} ($(_step_elapsed "$t_start")s)"
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

# 收盤研究報告模式：Score / Catalyst / Memo 等也印到終端（不只寫 log）
run_step_tee() {
  local label="$1"
  shift
  local t_start ok=0
  t_start=$(date +%s)
  log_line "--- ${label} ---"
  if env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
    "$@" 2>&1 | tee -a "$LOG_FILE"; then
    log_line "OK: ${label} ($(_step_elapsed "$t_start")s)"
    ok=1
  else
    log_line "WARN: ${label} ($(_step_elapsed "$t_start")s)"
  fi
  if [[ "$ok" -eq 0 ]]; then
    AUX_FAILED=1
  fi
}

run_timed_pipe() {
  local label="$1"
  shift
  local t_start
  t_start=$(date +%s)
  log_line "--- ${label} ---"
  if [[ "$QUIET" -eq 1 ]] && ! _report_to_terminal; then
    if "$@" >>"$LOG_FILE" 2>&1; then
      log_only "OK: ${label} ($(_step_elapsed "$t_start")s)"
    else
      log_only "WARN: ${label} ($(_step_elapsed "$t_start")s)"
    fi
  elif "$@" 2>&1 | tee -a "$LOG_FILE"; then
    log_line "OK: ${label} ($(_step_elapsed "$t_start")s)"
  else
    log_line "WARN: ${label} ($(_step_elapsed "$t_start")s)"
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
    SELECT trade_date AS 交易日, captured_at AS 擷取,
           printf('%.2f%%', tx_gap_live_pct) AS TX_gap,
           printf('%.2f%%', te_gap_live_pct) AS TE_gap,
           printf('%.2f%%', te_minus_tx_pct) AS TE减TX
    FROM morning_risk_snapshot
    ORDER BY trade_date DESC
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
  print_flow_attribution_readiness
}

print_flow_attribution_readiness() {
  log_line "--- Flow 歸因自檢（v0.3 · signal_review §0）---"
  if [[ ! -f "$DB" ]]; then
    log_line "  stocks.db 不存在"
    return
  fi
  local has_flow
  has_flow=$(sqlite3 "$DB" \
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='flow_events';" \
    2>/dev/null || echo 0)
  if [[ "$has_flow" != "1" ]]; then
    log_line "  flow_events 表尚未建立（請先跑含 --intent 的收盤鏈）"
    return
  fi

  if [[ "$HOLDINGS" -eq 0 ]]; then
    log_line "  本輪為早盤鏈（--market-only）；今日 flow 快照待收盤 --intent 落地"
    sqlite3 -header -column "$DB" "
      SELECT COUNT(*) AS 事件列,
             COUNT(DISTINCT event_date) AS event_days,
             MAX(event_date) AS 最新_event_date
      FROM flow_events;
    " 2>/dev/null | pipe_out || log_line "  flow_events 查詢失敗"
    return
  fi

  sqlite3 -header -column "$DB" "
    SELECT COUNT(*) AS 事件列,
           COUNT(DISTINCT event_date) AS event_days,
           MIN(event_date) AS 最早,
           MAX(event_date) AS 最新
    FROM flow_events;
  " 2>/dev/null | pipe_out || log_line "  flow_events 查詢失敗"

  sqlite3 -header -column "$DB" "
    SELECT net_side AS 方向, COUNT(*) AS 列數
    FROM flow_events
    WHERE event_date = (SELECT MAX(event_date) FROM flow_events)
    GROUP BY net_side
    ORDER BY net_side;
  " 2>/dev/null | pipe_out || true

  local last_event bar_max days_after h1_ready
  last_event=$(sqlite3 "$DB" "SELECT MAX(event_date) FROM flow_events;" 2>/dev/null || echo "")
  bar_max=$(sqlite3 "$DB" \
    "SELECT MAX(trade_date) FROM stock_daily_bars WHERE source='finmind';" \
    2>/dev/null || echo "")
  if [[ -z "$last_event" ]]; then
    log_line "  尚無 flow_events；請確認收盤鏈已跑 --changes --intent"
    return
  fi
  if [[ -z "$bar_max" ]]; then
    log_line "  成分股 K 線為空；請設 RUN_STOCK_MARKET_SYNC=1 或跑 weekly_sync"
    return
  fi

  days_after=$(sqlite3 "$DB" "
    SELECT COUNT(DISTINCT trade_date)
    FROM stock_daily_bars
    WHERE source = 'finmind' AND trade_date > '${last_event}';
  " 2>/dev/null || echo 0)

  h1_ready=$(sqlite3 "$DB" "
    SELECT COUNT(*)
    FROM flow_events fe
    WHERE EXISTS (
      SELECT 1 FROM stock_daily_bars b0
      WHERE b0.stock_id = fe.stock_id AND b0.source = 'finmind'
        AND b0.trade_date = fe.event_date
    )
    AND EXISTS (
      SELECT 1 FROM stock_daily_bars b1
      WHERE b1.stock_id = fe.stock_id AND b1.source = 'finmind'
        AND b1.trade_date = (
          SELECT MIN(d) FROM (
            SELECT DISTINCT trade_date AS d
            FROM stock_daily_bars
            WHERE source = 'finmind' AND trade_date > fe.event_date
            ORDER BY d ASC LIMIT 1
          )
        )
    );
  " 2>/dev/null || echo 0)

  log_line "  最新 event_date=${last_event} · 成分股 K 線最新=${bar_max} · event 後交易日數=${days_after}"
  log_line "  H+1 可追蹤（粗估）${h1_ready}/$(sqlite3 "$DB" "SELECT COUNT(*) FROM flow_events;" 2>/dev/null || echo 0) 列"

  if [[ "${days_after:-0}" -lt 1 ]]; then
    log_line "  §0 Coverage：H+1～H+10 Available 可能為 0（K 線尚未晚於 event_date）"
    log_line "  → 下個交易日早盤/成分股 sync 後再跑 scripts/策略回顧.command"
  elif [[ "${h1_ready:-0}" -eq 0 ]]; then
    log_line "  §0：已有 event 後交易日，但個股/outcome 對齊失敗；查 stock_daily_bars 覆蓋"
  else
    log_line "  §0：可跑策略回顧（signal_review --lookback-event-days 20）"
  fi
}

print_repeat_help() {
  log_line "--- 重複按會怎樣 ---"
  log_line "  [1] 日線 TEJ      upsert 覆寫，不會多出一筆；只刷新 synced_at"
  log_line "  [2] 持股（EZMoney/凱基/群益/野村）官網未更新 → Skip，DB 不變（正常）"
  log_line "  [3] 科技風險      TSM/SOX 日線 + 台指 gap → tech_risk_daily_snapshot（upsert）"
  log_line "  [3b] 早盤雷達    TX/TE 即時 gap → morning_risk_snapshot（需 FINMIND_TOKEN）"
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
    --benchmark-codes "$BENCHMARK_CODES" \
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

  run_step_optional "morning futures snapshot (TX/TE live gap)" \
    "$PYTHON" "${SRC}/sync_morning_futures.py" \
    "${PYTHON_QUIET[@]}" \
    --sync-db
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
    STOCK_MKT_ARGS=(
      "${PYTHON_QUIET[@]}"
      --sync-db
      --lookback-days "${STOCK_MARKET_LOOKBACK_DAYS:-60}"
    )
    [[ "${STOCK_MARKET_FORCE_REFRESH:-0}" == "1" ]] && STOCK_MKT_ARGS+=(--force-refresh)
    run_step_optional "constituent market+institutional (FinMind)" \
      "$PYTHON" "${SRC}/sync_stock_market_daily.py" "${STOCK_MKT_ARGS[@]}"
  else
    log_line "--- constituent stock market (FinMind) ---"
    log_line "  SKIP（RUN_STOCK_MARKET_SYNC=0；設 1 啟用成分股價+法人）"
  fi

  CHANGES_CMD=(
    "$PYTHON" "${SRC}/sync_etf_holdings.py"
    --etf-codes "$ETF_CODES_HOLDINGS"
    --changes
    --intent
  )
  if [[ "$SHOW_REPORT" -eq 1 ]]; then
    CHANGES_CMD+=(--human)
  fi
  if [[ "${RUN_UNIVERSE_REPORT:-1}" == "0" ]]; then
    CHANGES_CMD+=(--no-universe)
  fi
  run_timed_pipe "holdings changes + 跨 ETF 共識 + Research Universe" \
    "${CHANGES_CMD[@]}" || true

  PIPELINE_ARGS=("$PYTHON" "${SRC}/pipeline_evening.py")
  [[ "$QUIET" -eq 1 ]] && PIPELINE_ARGS+=(--quiet)
  [[ "$SHOW_REPORT" -eq 1 ]] && PIPELINE_ARGS+=(--show-report)
  PIPELINE_ARGS+=(--etf-codes "$ETF_CODES_HOLDINGS")
  run_timed_pipe "evening research pipeline" \
    "${PIPELINE_ARGS[@]}" || true
fi

if [[ "$SHOW_REPORT" -eq 1 ]]; then
  run_timed_pipe "evening human digest" \
    "$PYTHON" "${SRC}/evening_digest.py" \
    --etf-codes "$ETF_CODES_HOLDINGS" || true
elif [[ "$MORNING_REPORT" -eq 1 ]]; then
  ensure_python_deps
  EE_ARGS=("$PYTHON" "${SRC}/execution_eval.py" --mode pre_open)
  if [[ "${RUN_ORDER_INTENT:-1}" == "1" ]]; then
    EE_ARGS+=(--persist)
  else
    EE_ARGS+=(--preview)
  fi
  run_timed_pipe "execution eval (pre_open)" \
    "${EE_ARGS[@]}" || true
  if [[ "${RUN_ORDER_INTENT:-1}" == "0" ]]; then
    log_line "--- order intents (E0) ---"
    log_line "  僅預覽（RUN_ORDER_INTENT=0；設 1 寫入 order_intents + execution_eval 報告）"
  else
    log_line "--- order intents (E0) ---"
    log_line "  已寫入 order_intents + reports/*_execution_eval.md"
  fi
  print_flow_attribution_readiness
elif [[ "$QUIET" -eq 0 ]]; then
  print_db_summary
  print_repeat_help
else
  if [[ "$SHOW_REPORT" -eq 1 ]]; then
    print_db_summary
  else
    print_db_summary >>"$LOG_FILE" 2>/dev/null || true
  fi
  print_repeat_help >>"$LOG_FILE" 2>/dev/null || true
fi
log_line "=== daily_sync finished exit=${FAILED} total=$(( $(date +%s) - SYNC_T0 ))s ==="

echo ""
if [[ "$FAILED" -eq 0 ]]; then
  if [[ "$AUX_FAILED" -eq 1 ]]; then
    if [[ "$SHOW_REPORT" -eq 1 ]]; then
      echo "✓ 持股研究完成 (exit=0)；部分選用步驟 WARN，見上方與 log"
    elif [[ "$MORNING_REPORT" -eq 1 ]]; then
      echo "✓ 執行評估完成 (exit=0)；部分選用步驟 WARN，見上方與 log"
    else
      echo "✓ 持股研究完成 (exit=0)；日線/法人未更新，見 log"
    fi
  elif [[ "$SHOW_REPORT" -eq 1 ]]; then
    echo "✓ 收盤持股雷達完成 (exit=0) · 摘要見上方"
  elif [[ "$MORNING_REPORT" -eq 1 ]]; then
    echo "✓ 執行評估完成 (exit=0)"
  else
    echo "✓ daily_sync 完成 (exit=0)"
  fi
else
  if [[ "$MORNING_REPORT" -eq 1 ]]; then
    echo "✗ 執行評估同步失敗 (exit=${FAILED})"
  else
    echo "✗ 持股同步失敗 (exit=${FAILED})"
  fi
fi
echo "完整 log：${LOG_FILE}"
exit "$FAILED"
