#!/usr/bin/env bash
# 歷史市場資料 backfill（約 2 年 K 線 / 法人 / 籌碼）。
# 需 .env：TEJ_API_KEY、FINMIND_TOKEN
# 不含 etf_holdings（官網無法回溯）。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
SRC="${ROOT}/src"
export PYTHONPATH="${SRC}${PYTHONPATH:+:${PYTHONPATH}}"

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  eval "$("$PYTHON" -c "from project_dotenv import shell_export_dotenv; print(shell_export_dotenv())")"
  set +a
fi

CALENDAR_DAYS="${BACKFILL_CALENDAR_DAYS:-730}"
CHUNK_DAYS="${BACKFILL_CHUNK_DAYS:-90}"
MODE="sync"
ONLY=""
EXTRA=()

for arg in "$@"; do
  case "$arg" in
    --report) MODE="report" ;;
    --only=*) ONLY="${arg#--only=}" ;;
    --calendar-days=*) CALENDAR_DAYS="${arg#--calendar-days=}" ;;
    --chunk-days=*) CHUNK_DAYS="${arg#--chunk-days=}" ;;
    --quiet) EXTRA+=(--quiet) ;;
    --max-stocks=*) EXTRA+=("$arg") ;;
    --help|-h)
      echo "Usage: $0 [--report] [--only=etf-bars,stock-market,chip] [--calendar-days=730] [--chunk-days=90] [--quiet]"
      exit 0
      ;;
    * )
      echo "Unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

ARGS=(--calendar-days "$CALENDAR_DAYS" --chunk-days "$CHUNK_DAYS")
[[ -n "$ONLY" ]] && ARGS+=(--only "$ONLY")
ARGS+=("${EXTRA[@]}")

if [[ "$MODE" == "report" ]]; then
  exec "$PYTHON" "${SRC}/backfill_market_data.py" --report "${ARGS[@]}"
fi

echo "backfill_market_data：${CALENDAR_DAYS} 曆日 · chunk=${CHUNK_DAYS}d · 需 TEJ + FinMind"
exec "$PYTHON" "${SRC}/backfill_market_data.py" --sync "${ARGS[@]}"
