#!/usr/bin/env bash
# VCP 盤中 watch 完成通知（launchd vcp-intraday-watch）

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="$(date '+%Y%m%d')"

exec "${ROOT}/scripts/job_notify.sh" \
  "VCP 盤中 watch" "${1:?}" "logs/launchd_vcp-intraday-watch.log" RUN_VCP_INTRADAY_EMAIL \
  "reports/${STAMP}_vcp_intraday_watch.md" \
  "reports/vcp_intraday_watch.md"
