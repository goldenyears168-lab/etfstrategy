#!/usr/bin/env bash
# 安裝下單層 launchd：08:55 防睡眠（order-wake）
#
# 用法：
#   scripts/install-order-launchd.sh
#   scripts/install-order-launchd.sh --uninstall
#   scripts/install-order-launchd.sh --status

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCHD_SRC="${PROJECT_ROOT}/launchd"
APP_SUPPORT="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
AGENT_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"
ORDER_WAKE_LAUNCHER="${APP_SUPPORT}/order-wake.sh"

LABELS=(
  com.jackm4.goldenstocks.order-wake
)
TEMPLATES=(
  com.jackm4.goldenstocks.order-wake.plist.template
)
COMMANDS=(
  order-wake
)
# 已退役／不裝：升級時自動卸載（見 deploy/mac-mini/MIGRATION_PLAN.md §4.6「不裝開盤追價」）
LEGACY_LABELS=(
  com.jackm4.goldenstocks.order-5347-open
  com.jackm4.goldenstocks.order-chase-open
)

usage() {
  cat <<EOF
用法: $(basename "$0") [--uninstall|--status]

  order-wake：鐘面每 5 分 · Mon–Fri 08:50–13:40 caffeinate（盤中防休眠）

  開盤窗追價（order-chase-open）已退役、不裝：每次執行本腳本會自動
  bootout + 移除其 plist（見 LEGACY_LABELS）。程式碼仍在
  scripts/order/chase_scheduled.py／src/order/chase.py，需要時可手動
  bootstrap launchd/com.jackm4.goldenstocks.order-chase-open.plist.template。
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

  mkdir -p "${AGENT_DIR}" "${APP_SUPPORT}" "${PROJECT_ROOT}/logs"

  # order-wake → Application Support（避開 Documents TCC）
  local wake_src="${LAUNCHD_SRC}/order-wake-launcher.sh.template"
  if [[ ! -f "${wake_src}" ]]; then
    echo "✗ 缺少 ${wake_src}" >&2
    exit 1
  fi
  sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" "${wake_src}" >"${ORDER_WAKE_LAUNCHER}"
  chmod +x "${ORDER_WAKE_LAUNCHER}"
  echo "✓ launcher ${ORDER_WAKE_LAUNCHER}"

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
    mkdir -p "${HOME}/Library/Logs/com.jackm4.goldenstocks"
    sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
        -e "s|{{ORDER_WAKE_LAUNCHER}}|${ORDER_WAKE_LAUNCHER}|g" \
        -e "s|{{HOME}}|${HOME}|g" \
        "${src}" >"${dest}"
    bootstrap_label "${dest}"
    echo "✓ ${label}"
  done

  echo ""
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
