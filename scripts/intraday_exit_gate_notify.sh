#!/usr/bin/env bash
# 09:05 組合閘門 · dry-run notify

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="${2:-$(date '+%Y%m%d')}"

exec "${ROOT}/scripts/job_notify.sh" \
  "Intraday exit gate" "${1:-0}" "logs/intraday/intraday_exit_gate_${STAMP}.log" \
  RUN_INTRADAY_EXIT_GATE_EMAIL \
  "reports/order/snapshots/intraday_gate_${STAMP}.md"
