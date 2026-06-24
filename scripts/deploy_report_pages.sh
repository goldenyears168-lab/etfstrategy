#!/usr/bin/env bash
# 將本地研究 HTML 發布到 Cloudflare Pages（靜態 · 不需 Readdy build）
#
# 前置：npx wrangler login（或設定 CLOUDFLARE_API_TOKEN）
#
# 用法：
#   scripts/deploy_report_pages.sh reports/research/rrg/20260615_rrg_universe.html
#   scripts/deploy_report_pages.sh reports/research/order/intraday_exit_research_memo.html
#   scripts/deploy_report_pages.sh path/to/report.html --project-name my-custom-name
#   scripts/deploy_report_pages.sh path/to/report.html --dry-run

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  sed -n '2,11p' "$0"
}

HTML_PATH=""
PROJECT_NAME=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-name)
      shift
      PROJECT_NAME="${1:?--project-name 需要名稱}"
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "未知參數: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -n "${HTML_PATH}" ]]; then
        echo "多餘參數: $1" >&2
        exit 2
      fi
      HTML_PATH="$1"
      ;;
  esac
  shift
done

if [[ -z "${HTML_PATH}" ]]; then
  echo "請指定 HTML 路徑" >&2
  usage >&2
  exit 2
fi

if [[ "${HTML_PATH}" != /* ]]; then
  HTML_PATH="${ROOT}/${HTML_PATH}"
fi

if [[ ! -f "${HTML_PATH}" ]]; then
  echo "找不到檔案: ${HTML_PATH}" >&2
  exit 1
fi

SOURCE_PATH="$(readlink -f "${HTML_PATH}" 2>/dev/null || realpath "${HTML_PATH}")"
BASENAME="$(basename "${SOURCE_PATH}" .html)"

if [[ -z "${PROJECT_NAME}" ]]; then
  PROJECT_NAME="etf-$(echo "${BASENAME}" | tr '[:upper:]' '[:lower:]' | tr '_' '-')"
fi

# 根目錄 symlink stub（<300B 且含 meta refresh）→ 優先用同路徑 .bak
if [[ "$(wc -c < "${SOURCE_PATH}" | tr -d ' ')" -lt 512 ]] \
  && grep -q 'http-equiv="refresh"' "${SOURCE_PATH}" 2>/dev/null; then
  BAK="${SOURCE_PATH}.bak"
  if [[ -f "${BAK}" ]]; then
    echo "注意：${SOURCE_PATH} 為轉址 stub，改用 ${BAK}"
    SOURCE_PATH="${BAK}"
  else
    echo "警告：${SOURCE_PATH} 看起來是轉址 stub，且無 .bak 備份" >&2
  fi
fi

DEPLOY_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cf-pages-deploy.XXXXXX")"
cleanup() { rm -rf "${DEPLOY_DIR}"; }
trap cleanup EXIT

cp "${SOURCE_PATH}" "${DEPLOY_DIR}/index.html"

echo "來源: ${SOURCE_PATH}"
echo "專案: ${PROJECT_NAME}"
echo "暫存: ${DEPLOY_DIR}/index.html ($(wc -c < "${DEPLOY_DIR}/index.html" | tr -d ' ') bytes)"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[dry-run] 略過 wrangler deploy"
  exit 0
fi

if ! npx --yes wrangler@latest whoami >/dev/null 2>&1; then
  echo "尚未登入 Cloudflare。請執行: npx wrangler login" >&2
  echo "或設定環境變數 CLOUDFLARE_API_TOKEN" >&2
  exit 1
fi

deploy_once() {
  npx --yes wrangler@latest pages deploy "${DEPLOY_DIR}" \
    --project-name="${PROJECT_NAME}" \
    --branch="main" \
    --commit-dirty=true
}

set +e
DEPLOY_OUT="$(deploy_once 2>&1)"
DEPLOY_RC=$?
set -e

if [[ "${DEPLOY_RC}" -ne 0 ]] && grep -q 'Project not found' <<<"${DEPLOY_OUT}"; then
  echo "專案不存在，建立 ${PROJECT_NAME} …"
  npx --yes wrangler@latest pages project create "${PROJECT_NAME}" --production-branch main
  DEPLOY_OUT="$(deploy_once 2>&1)"
  DEPLOY_RC=$?
fi

echo "${DEPLOY_OUT}"

if [[ "${DEPLOY_RC}" -ne 0 ]]; then
  exit "${DEPLOY_RC}"
fi

PREVIEW_URL="$(sed -n 's/.*\(https:\/\/[^ ]*\.pages\.dev\).*/\1/p' <<<"${DEPLOY_OUT}" | tail -1)"
PROD_URL="https://${PROJECT_NAME}.pages.dev/"

echo ""
echo "正式網址: ${PROD_URL}"
if [[ -n "${PREVIEW_URL}" && "${PREVIEW_URL}" != "${PROD_URL%/}" ]]; then
  echo "本次部署: ${PREVIEW_URL}"
fi
