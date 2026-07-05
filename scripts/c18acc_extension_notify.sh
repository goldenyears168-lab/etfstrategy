#!/usr/bin/env bash
# C18acc extension overlay · 出場訊號寄信

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="$(date '+%Y%m%d')"

exec "${ROOT}/scripts/job_notify.sh" \
  "C18acc Extension overlay" "${1:-0}" "logs/intraday/launchd_c18acc-extension-overlay.log" RUN_C18ACC_EXTENSION_EMAIL \
  "reports/daily/${STAMP}_c18acc_extension_daily.md" \
  "reports/daily/c18acc_extension_daily.md" \
  "reports/order/intents/rrg-mono-swap-accel-extension_$(date '+%Y-%m-%d').json"
