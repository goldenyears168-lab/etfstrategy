#!/usr/bin/env bash
# Sell signal radar · 有新 extension 賣出訊號時寄信

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="$(date '+%Y%m%d')"

exec "${ROOT}/scripts/job_notify.sh" \
  "Sell signal radar" "${1:-0}" "logs/intraday/launchd_sell-signal-radar.log" RUN_SELL_SIGNAL_EMAIL \
  "reports/daily/${STAMP}_sell_signal_radar.md" \
  "reports/daily/sell_signal_radar.md"
