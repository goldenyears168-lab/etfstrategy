#!/usr/bin/env bash
# 00981A etfedge 持股匯入完成通知（launchd etfedge-import）

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

exec env JOB_NOTIFY_EXTRA="${JOB_NOTIFY_EXTRA:-}" \
  "${ROOT}/scripts/job_notify.sh" \
  "00981A etfedge 持股匯入" "${1:?}" "logs/launchd_etfedge-import.log" RUN_ETFEDGE_IMPORT_EMAIL
