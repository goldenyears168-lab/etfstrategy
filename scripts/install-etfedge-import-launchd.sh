#!/usr/bin/env bash
# 安裝 00981A etfedge 一次性匯入排程（預設 2026-06-17 08:20）
#
# 用法：
#   scripts/install-etfedge-import-launchd.sh
#   scripts/install-etfedge-import-launchd.sh --uninstall
#   scripts/install-etfedge-import-launchd.sh --status

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCHD_SRC="${PROJECT_ROOT}/launchd"
AGENT_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"
LABEL="com.jackm4.etf.etfedge-import"
TEMPLATE="com.jackm4.etf.etfedge-import.plist.template"

usage() {
  cat <<EOF
用法: $(basename "$0") [--uninstall|--status]

  安裝一次性 launchd 排程：
    2026-06-17 08:20（本地時間）執行 etfedge 00981A 持股匯入
    成功或失敗皆寄信（需 .env 設定 GMAIL_* 或 NOTIFY_WEBHOOK_URL）

  log：${PROJECT_ROOT}/logs/launchd_etfedge-import.log
EOF
}

bootout_label() {
  launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
  launchctl unload "${AGENT_DIR}/${LABEL}.plist" 2>/dev/null || true
}

bootstrap_label() {
  local plist_path="$1"
  if launchctl bootstrap "${GUI_DOMAIN}" "${plist_path}" 2>/dev/null; then
    return 0
  fi
  launchctl load "${plist_path}"
}

install_agent() {
  if [[ ! -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
    echo "✗ 找不到 ${PROJECT_ROOT}/.venv/bin/python" >&2
    exit 1
  fi
  local cmd="${PROJECT_ROOT}/scripts/launchd/etfedge-import.command"
  if [[ ! -f "${cmd}" ]]; then
    echo "✗ 缺少 ${cmd}" >&2
    exit 1
  fi
  chmod +x "${cmd}"

  mkdir -p "${AGENT_DIR}" "${PROJECT_ROOT}/logs"
  local src="${LAUNCHD_SRC}/${TEMPLATE}"
  local dest="${AGENT_DIR}/${LABEL}.plist"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi

  bootout_label
  sed "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" "${src}" >"${dest}"
  bootstrap_label "${dest}"
  echo "✓ 已安裝 ${LABEL}"
  echo "  時間：2026-06-17 08:20（Mac 須已登入、未睡眠）"
  echo "  請確認 .env 已設定 GMAIL_USER + GMAIL_APP_PASSWORD + GMAIL_NOTIFY_TO"
}

uninstall_agent() {
  bootout_label
  rm -f "${AGENT_DIR}/${LABEL}.plist"
  echo "✓ 已卸載 ${LABEL}"
}

show_status() {
  launchctl list 2>/dev/null | grep -E "${LABEL}" || echo "  （未載入）"
  if [[ -f "${AGENT_DIR}/${LABEL}.plist" ]]; then
    echo "  plist: ${AGENT_DIR}/${LABEL}.plist"
  fi
}

ACTION=install
case "${1:-}" in
  --uninstall) ACTION=uninstall ;;
  --status) ACTION=status ;;
  -h|--help) usage; exit 0 ;;
  "") ACTION=install ;;
  *)
    echo "未知參數：$1" >&2
    usage
    exit 2
    ;;
esac

case "${ACTION}" in
  install) install_agent ;;
  uninstall) uninstall_agent ;;
  status) show_status ;;
esac
