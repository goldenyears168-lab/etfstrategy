#!/usr/bin/env bash
# 安裝下單層 launchd：08:55 防睡眠 · 09:00–09:04 每分鐘追價
#
# 用法：
#   scripts/install-order-launchd.sh
#   scripts/install-order-launchd.sh --uninstall
#   scripts/install-order-launchd.sh --status

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCHD_SRC="${PROJECT_ROOT}/launchd"
AGENT_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"

LABELS=(
  com.jackm4.etf.order-wake
  com.jackm4.etf.order-chase-open
)
TEMPLATES=(
  com.jackm4.etf.order-wake.plist.template
  com.jackm4.etf.order-chase-open.plist.template
)
COMMANDS=(
  order-wake
  order-chase-open
)
# 舊版單次送單 label（升級時卸載）
LEGACY_LABELS=(
  com.jackm4.etf.order-5347-open
)

usage() {
  cat <<EOF
用法: $(basename "$0") [--uninstall|--status]

  08:55 order-wake（caffeinate 10 分鐘）
  09:00–09:04 每分鐘 order-chase-open（限價追賣一 · 最多 5 輪）

  閘門：.env ORDER_LAUNCHD_ENABLED=1
  僅撤 user_def=chase_open 的程式單，不動人工掛單

  log：${PROJECT_ROOT}/logs/launchd_order-chase-open.log
EOF
}

bootout_label() {
  local label="$1"
  launchctl bootout "${GUI_DOMAIN}/${label}" 2>/dev/null || true
  launchctl unload "${AGENT_DIR}/${label}.plist" 2>/dev/null || true
}

bootstrap_label() {
  local plist_path="$1"
  if launchctl bootstrap "${GUI_DOMAIN}" "${plist_path}" 2>/dev/null; then
    return 0
  fi
  launchctl load "${plist_path}"
}

install_agents() {
  if [[ ! -x "${PROJECT_ROOT}/.venv-fubon/bin/python" ]]; then
    echo "✗ 找不到 ${PROJECT_ROOT}/.venv-fubon/bin/python" >&2
    exit 1
  fi

  local name path label
  for name in "${COMMANDS[@]}"; do
    path="${PROJECT_ROOT}/scripts/launchd/${name}.command"
    if [[ ! -f "${path}" ]]; then
      echo "✗ 缺少 ${path}" >&2
      exit 1
    fi
    chmod +x "${path}"
  done

  for label in "${LEGACY_LABELS[@]}"; do
    bootout_label "${label}"
    rm -f "${AGENT_DIR}/${label}.plist"
    echo "✓ 已卸載舊版 ${label}"
  done

  mkdir -p "${AGENT_DIR}" "${PROJECT_ROOT}/logs"

  local i template src dest
  for i in "${!TEMPLATES[@]}"; do
    template="${TEMPLATES[$i]}"
    label="${LABELS[$i]}"
    src="${LAUNCHD_SRC}/${template}"
    dest="${AGENT_DIR}/${label}.plist"
    if [[ ! -f "${src}" ]]; then
      echo "✗ 缺少 ${src}" >&2
      exit 1
    fi
    bootout_label "${label}"
    sed "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" "${src}" >"${dest}"
    bootstrap_label "${dest}"
    echo "✓ ${label}"
  done

  echo ""
  echo "spec：reports/order/intents/scheduled/open_market_10000.json"
  if grep -q '^ORDER_LAUNCHD_ENABLED=1' "${PROJECT_ROOT}/.env" 2>/dev/null; then
    echo "✓ .env ORDER_LAUNCHD_ENABLED=1"
  else
    echo "⚠ 請在 .env 加入 ORDER_LAUNCHD_ENABLED=1"
  fi
}

uninstall_agents() {
  local label
  for label in "${LABELS[@]}" "${LEGACY_LABELS[@]}"; do
    bootout_label "${label}"
    rm -f "${AGENT_DIR}/${label}.plist"
    echo "✓ 已移除 ${label}"
  done
}

show_status() {
  launchctl list 2>/dev/null | grep -E 'jackm4\.etf\.order-' || echo "  （未載入）"
  local label
  for label in "${LABELS[@]}" "${LEGACY_LABELS[@]}"; do
    if [[ -f "${AGENT_DIR}/${label}.plist" ]]; then
      echo "  plist: ${AGENT_DIR}/${label}.plist"
    fi
  done
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
  install) install_agents ;;
  uninstall) uninstall_agents ;;
  status) show_status ;;
esac
