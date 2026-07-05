#!/usr/bin/env bash
# Screener / 回測資料 one-shot backfill（TW100 2015+ · etf_watchlist 730d）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
SRC="${ROOT}/src"
export PYTHONPATH="${SRC}${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONUNBUFFERED=1

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  eval "$("$PYTHON" -c "from project_dotenv import shell_export_dotenv; print(shell_export_dotenv())")"
  set +a
fi

SCREENER_ONLY="shareholding,dividend,futures-inst,market-value,technical"
COMMON=(--sync --request-delay 0.35)

echo "=== TW100 screener backfill（2015-01-01 起） $(date '+%Y-%m-%dT%H:%M:%S%z') pid=$$ ==="
"$PYTHON" "${SRC}/backfill_market_data.py" "${COMMON[@]}" \
  --universe tw100 \
  --only "$SCREENER_ONLY" \
  --start-date 2015-01-01 \
  --calendar-days 9999 \
  --chunk-days "${BACKFILL_CHUNK_DAYS:-90}" \
  "$@"

echo ""
echo "=== etf_watchlist screener backfill（730 日） $(date '+%Y-%m-%dT%H:%M:%S%z') ==="
"$PYTHON" "${SRC}/backfill_market_data.py" "${COMMON[@]}" \
  --universe etf_watchlist \
  --only "$SCREENER_ONLY" \
  --calendar-days "${BACKFILL_CALENDAR_DAYS:-730}" \
  --chunk-days "${BACKFILL_CHUNK_DAYS:-90}" \
  "$@"

echo "=== screener backfill 完成 $(date '+%Y-%m-%dT%H:%M:%S%z') exit=0 ==="
