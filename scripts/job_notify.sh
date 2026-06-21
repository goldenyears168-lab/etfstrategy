#!/usr/bin/env bash
# 排程完成通知（Gmail SMTP → Mail.app 收件匣）
#
# 用法：
#   job_notify.sh <主旨前綴> <exit_code> <log路徑> [<env開關名>] [報告相對路徑…]
#   JOB_NOTIFY_EXTRA=自由文字（選填，會併入郵件正文）

set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
SUBJECT="${1:?usage: job_notify.sh <subject> <exit_code> <log> [env_flag] [reports…]}"
EXIT_CODE="${2:?}"
LOG_PATH="${3:?}"
ENV_FLAG="${4:-}"
shift 4 2>/dev/null || shift 3 || true
REPORTS=("$@")

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "⚠ 郵件通知略過：venv 不存在"
  exit 0
fi

LOG_FILE="${LOG_PATH}"
if [[ "${LOG_FILE}" != /* ]]; then
  LOG_FILE="${ROOT}/${LOG_FILE}"
fi

EXTRA="${JOB_NOTIFY_EXTRA:-}"
for rel in "${REPORTS[@]}"; do
  [[ -n "${rel}" && -f "${ROOT}/${rel}" ]] && EXTRA+="${rel}"$'\n'
done

ARGS=(
  --subject-prefix="${SUBJECT}"
  --exit-code="${EXIT_CODE}"
  --log-path="${LOG_FILE}"
  --extra="${EXTRA}"
)
if [[ -n "${ENV_FLAG}" ]]; then
  ARGS+=(--env-flag="${ENV_FLAG}")
fi

set +e
"${PYTHON}" "${ROOT}/scripts/notify_job_result.py" "${ARGS[@]}"
MAIL_EXIT=$?
set -e

if [[ "${MAIL_EXIT}" -ne 0 ]]; then
  if [[ "${EXIT_CODE}" -eq 0 ]]; then
    STATUS="完成"
  else
    STATUS="失敗"
  fi
  /usr/bin/osascript -e \
    "display notification \"${SUBJECT} ${STATUS}；郵件未設定（見 .env GMAIL_*）\" with title \"ETF研究\"" \
    2>/dev/null || true
fi

exit 0
