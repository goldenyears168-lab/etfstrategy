#!/usr/bin/env bash
# 09:08 開盤解讀 · 寄信

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="${2:-$(date '+%Y%m%d')}"

exec "${ROOT}/scripts/job_notify.sh" \
  "開盤解讀" "${1:-0}" "logs/intraday/intraday_open_digest_${STAMP}.log" \
  RUN_INTRADAY_OPEN_DIGEST_EMAIL
