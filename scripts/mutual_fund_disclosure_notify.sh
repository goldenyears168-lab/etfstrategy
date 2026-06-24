#!/usr/bin/env bash
# 安聯台灣科技基金月報偵測失敗通知（launchd mutual-fund-disclosure-watch）

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="$(date '+%Y%m%d')"

exec env JOB_NOTIFY_EXTRA="${JOB_NOTIFY_EXTRA:-}" \
  "${ROOT}/scripts/job_notify.sh" \
  "安聯台灣科技基金月報偵測" "${1:?}" \
  "logs/mutual_fund_disclosure_watch_${STAMP}.log" \
  RUN_MUTUAL_FUND_DISCLOSURE_EMAIL
