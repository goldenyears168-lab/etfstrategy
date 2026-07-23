#!/usr/bin/env bash
# Buy signal radar · 有新 C0 買進訊號時寄信

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="$(date '+%Y%m%d')"

# /bin/bash 讀腳本（勿直接 exec · launchd 對 Documents/*.sh 可能 Operation not permitted）
exec /bin/bash "${ROOT}/scripts/job_notify.sh" \
  "Buy signal radar" "${1:-0}" "logs/intraday/launchd_buy-signal-radar.log" RUN_BUY_SIGNAL_EMAIL \
  "reports/daily/${STAMP}_buy_signal_radar.md" \
  "reports/daily/buy_signal_radar.md"
