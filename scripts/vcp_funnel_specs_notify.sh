#!/usr/bin/env bash
# VCP Pivot Gate / Coil Close 盤中 brief 完成通知（launchd vcp-funnel-specs）

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STAMP="$(date '+%Y%m%d')"

exec "${ROOT}/scripts/job_notify.sh" \
  "VCP Pivot Gate / Coil Close" "${1:?}" "logs/launchd_vcp-funnel-specs.log" RUN_VCP_FUNNEL_SPECS_EMAIL \
  "reports/daily/${STAMP}_vcp_funnel_specs_daily_brief.md" \
  "reports/daily/vcp_funnel_specs_daily_brief.md" \
  "reports/daily/${STAMP}_vcp_pivot_gate_daily_brief.md" \
  "reports/daily/vcp_pivot_gate_daily_brief.md" \
  "reports/daily/${STAMP}_vcp_coil_close_daily_brief.md" \
  "reports/daily/vcp_coil_close_daily_brief.md"
