#!/usr/bin/env bash
# 13:00 盤中研究合併摘要 · 寄信

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="${2:-$(date '+%Y%m%d')}"

exec "${ROOT}/scripts/job_notify.sh" \
  "13:00 盤中研究摘要" "${1:-0}" "logs/intraday/intraday_1300_digest_${STAMP}.log" \
  RUN_INTRADAY_1300_DIGEST_EMAIL
