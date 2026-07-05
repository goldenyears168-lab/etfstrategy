#!/usr/bin/env bash
# 11:00 盤中持倉脈動 · 寄信

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="${2:-$(date '+%Y%m%d')}"

exec "${ROOT}/scripts/job_notify.sh" \
  "11:00 盤中持倉脈動" "${1:-0}" "logs/intraday/intraday_midday_digest_${STAMP}.log" \
  RUN_INTRADAY_MIDDAY_DIGEST_EMAIL
