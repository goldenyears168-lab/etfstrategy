#!/usr/bin/env bash
# 手動同步研究成果至 Supabase（已自 launchd / notify 排程移除 · 僅按需執行）
#
# 用法：research_supabase_sync.sh <1300|1630> [<env開關名>]
# 預設需 RUN_SUPABASE_RESEARCH_SYNC=1 才會執行。

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
SLOT="${1:?usage: research_supabase_sync.sh <1300|1630> [env_flag]}"
ENV_FLAG="${2:-RUN_SUPABASE_RESEARCH_SYNC}"

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "⚠ Supabase sync 略過：venv 不存在"
  exit 0
fi

if [[ -n "${ENV_FLAG}" ]]; then
  if [[ -f "${ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    eval "$("${PYTHON}" -c "from project_dotenv import shell_export_dotenv; print(shell_export_dotenv())")"
    set +a
  fi
  val="${!ENV_FLAG:-0}"
  if [[ "${val}" == "0" || "${val}" == "false" || "${val}" == "False" || -z "${val}" ]]; then
    echo "Supabase sync 略過（${ENV_FLAG}=${val:-未設定}）"
    exit 0
  fi
fi

set +e
SYNC_OUT="$("${PYTHON}" "${ROOT}/scripts/sync_research_to_supabase.py" --slot "${SLOT}" 2>&1)"
SYNC_EXIT=$?
set -e

echo "${SYNC_OUT}"

if [[ "${SYNC_EXIT}" -ne 0 ]]; then
  /usr/bin/osascript -e \
    "display notification \"Supabase sync ${SLOT} 失敗（見 log）\" with title \"ETF研究\"" \
    2>/dev/null || true
  exit "${SYNC_EXIT}"
fi

exit 0
