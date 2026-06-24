#!/usr/bin/env bash
# C18acc 盤中 screen · 有 entry/swap/exit 動作時寄信（launchd rrg-c18acc-poll）

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="$(date '+%Y%m%d')"

exec "${ROOT}/scripts/job_notify.sh" \
  "C18acc 盤中訊號" "${1:-0}" "logs/launchd_rrg-c18acc-poll.log" RUN_RRG_C18ACC_EMAIL \
  "reports/daily/${STAMP}_rrg_c18acc_screen.md" \
  "reports/daily/rrg_c18acc_screen.md" \
  "logs/rrg_c18acc_poll_tick.log"
