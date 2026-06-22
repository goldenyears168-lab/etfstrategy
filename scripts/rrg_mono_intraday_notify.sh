#!/usr/bin/env bash
# RRG mono 收盤前預警完成通知（launchd rrg-mono-intraday-watch）

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="$(date '+%Y%m%d')"

exec "${ROOT}/scripts/job_notify.sh" \
  "RRG mono 收盤前預警" "${1:?}" "logs/launchd_rrg-mono-intraday-watch.log" RUN_RRG_MONO_INTRADAY_EMAIL \
  "reports/daily/${STAMP}_rrg_mono_intraday_watch.md" \
  "reports/daily/rrg_mono_intraday_watch.md"
