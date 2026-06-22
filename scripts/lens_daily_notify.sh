#!/usr/bin/env bash
# Lens daily alert email · 收盤後（需 RUN_LENS_DAILY_NOTIFY=1）
set -euo pipefail
ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
exec "${ROOT}/scripts/job_notify.sh" \
  "今日亮點" \
  0 \
  "${ROOT}/log/program.log" \
  RUN_LENS_DAILY_NOTIFY \
  "（headline 見 lens_daily_alert · Supabase）"
