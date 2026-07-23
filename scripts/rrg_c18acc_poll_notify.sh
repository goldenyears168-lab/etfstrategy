#!/usr/bin/env bash
# C18acc · fallback notify when fill report was not sent from Python

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

# /bin/bash 讀腳本（勿直接 exec · launchd 對 Documents/*.sh 可能 Operation not permitted）
exec /bin/bash "${ROOT}/scripts/job_notify.sh" \
  "C18acc 動作（無成交報告）" "${1:-0}" "logs/intraday/launchd_rrg-c18acc-poll.log" RUN_RRG_C18ACC_EMAIL
