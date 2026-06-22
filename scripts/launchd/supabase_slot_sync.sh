#!/usr/bin/env bash
# launchd 尾段：依 RUN_SUPABASE_RESEARCH_SYNC 推送 daily_briefs（1300 / 1630）
# 用法：supabase_slot_sync.sh <1300|1630>

set -euo pipefail

SLOT="${1:?usage: supabase_slot_sync.sh <1300|1630>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PYTHON="${ROOT}/.venv/bin/python"

if [[ -f "${ROOT}/.env" && -x "${PYTHON}" ]]; then
  set -a
  # shellcheck disable=SC1091
  eval "$("${PYTHON}" -c "from project_dotenv import shell_export_dotenv; print(shell_export_dotenv())")"
  set +a
fi

"${ROOT}/scripts/research_supabase_sync.sh" "${SLOT}" || true
