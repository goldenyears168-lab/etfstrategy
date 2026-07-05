#!/usr/bin/env bash
# Morning holdings brief · 開盤前持倉 brief 寄信

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="${2:-$(date '+%Y%m%d')}"

exec "${ROOT}/scripts/job_notify.sh" \
  "Morning holdings brief" "${1:-0}" "logs/intraday/morning_holdings_brief_${STAMP}.log" \
  RUN_MORNING_HOLDINGS_EMAIL \
  "reports/order/snapshots/morning_brief_${STAMP}.md"
