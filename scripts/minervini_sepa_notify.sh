#!/usr/bin/env bash
# Minervini SEPA basket · 月末調倉訊號寄信

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="$(date '+%Y%m%d')"

exec "${ROOT}/scripts/job_notify.sh" \
  "Minervini SEPA basket" "${1:-0}" "logs/launchd_minervini-sepa-basket.log" RUN_MINERVINI_SEPA_EMAIL \
  "reports/daily/${STAMP}_minervini_sepa_basket_daily.md" \
  "reports/daily/minervini_sepa_basket_daily.md" \
  "reports/order/intents/minervini-sepa-basket_$(date '+%Y-%m-%d').json"
