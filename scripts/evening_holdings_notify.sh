#!/usr/bin/env bash
# 收盤持股雷達完成通知（手動 1630 / launchd evening-holdings）

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="$(date '+%Y%m%d')"

exec "${ROOT}/scripts/job_notify.sh" \
  "ETF 日報" "${1:?}" "logs/daily_sync_${STAMP}.log" RUN_EVENING_HOLDINGS_EMAIL \
  "reports/daily/${STAMP}_etf_daily.md" \
  "reports/daily/etf-daily/daily_brief.md" \
  "reports/daily/regime/daily_brief.md"
