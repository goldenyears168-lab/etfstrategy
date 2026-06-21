#!/usr/bin/env bash
# 週日深度補庫完成通知（手動 2000 / launchd weekly-deep）

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="$(date '+%Y%m%d')"

exec "${ROOT}/scripts/job_notify.sh" \
  "週日深度補庫" "${1:?}" "logs/weekly_sync_${STAMP}.log" RUN_WEEKLY_DEEP_EMAIL \
  "reports/factor_validation/summary.md" \
  "reports/qlib_tw_factor_walkforward.md"
