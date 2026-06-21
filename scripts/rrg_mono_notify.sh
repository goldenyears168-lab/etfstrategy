#!/usr/bin/env bash
# RRG mono 每日掃描完成通知（Gmail SMTP → Mail.app 收件匣）

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="$(date '+%Y%m%d')"

exec "${ROOT}/scripts/job_notify.sh" \
  "RRG mono 掃描" "${1:?}" "logs/launchd_rrg-mono-scan.log" RUN_RRG_MONO_EMAIL \
  "reports/${STAMP}_rrg_mono_daily.md" \
  "reports/rrg_mono_daily.md" \
  "data/rrg_mono_slots.json"
