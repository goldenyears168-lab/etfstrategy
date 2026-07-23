#!/usr/bin/env bash
# 僅 Mac mini：安裝 Cursor My Machines worker（KeepAlive）
#
# 用法：
#   scripts/install-cursor-agent-worker-launchd.sh
#   scripts/install-cursor-agent-worker-launchd.sh --uninstall
#   scripts/install-cursor-agent-worker-launchd.sh --status
#
# 前置（mini .env）：
#   CURSOR_AGENT_WORKER_ENABLED=1
#   CURSOR_API_KEY=cursor_...   # Dashboard → Integrations
#   CURSOR_AGENT_WORKER_NAME=mac-mini   # 選填

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCHD_SRC="${PROJECT_ROOT}/launchd"
APP_SUPPORT="${HOME}/Library/Application Support/com.jackm4.etf"
AGENT_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"
LABEL="com.jackm4.etf.cursor-agent-worker"
LAUNCHER="${APP_SUPPORT}/cursor-agent-worker.sh"

usage() {
  cat <<EOF
用法: $(basename "$0") [--uninstall|--status]

  KeepAlive：agent worker start --name mac-mini（My Machines）
  閘門：.env CURSOR_AGENT_WORKER_ENABLED=1 + CURSOR_API_KEY
  僅裝在 Mac mini；Book 勿裝

  log：~/Library/Logs/com.jackm4.etf/cursor-agent-worker.log
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

status_agents() {
  echo "=== ${LABEL} ==="
  launchctl list | grep -E "cursor-agent-worker|LABEL" || echo "(not listed)"
  if [[ -f "${HOME}/Library/Logs/com.jackm4.etf/cursor-agent-worker.log" ]]; then
    echo "--- last log ---"
    tail -n 20 "${HOME}/Library/Logs/com.jackm4.etf/cursor-agent-worker.log" || true
  fi
}

uninstall_agents() {
  bootout_label
  rm -f "${AGENT_DIR}/${LABEL}.plist"
  echo "✓ unloaded ${LABEL}"
}

install_agents() {
  if [[ ! -x "${HOME}/.local/bin/agent" ]] && ! command -v agent >/dev/null 2>&1; then
    echo "✗ agent CLI missing；先：curl https://cursor.com/install -fsS | bash" >&2
    exit 1
  fi

  mkdir -p "${AGENT_DIR}" "${APP_SUPPORT}" "${PROJECT_ROOT}/logs" \
    "${HOME}/Library/Logs/com.jackm4.etf"

  local src="${LAUNCHD_SRC}/cursor-agent-worker-launcher.sh.template"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" "${src}" >"${LAUNCHER}"
  chmod +x "${LAUNCHER}"
  echo "✓ launcher ${LAUNCHER}"

  chmod +x "${PROJECT_ROOT}/scripts/launchd/cursor-agent-worker.command" 2>/dev/null || true

  local plist_src="${LAUNCHD_SRC}/com.jackm4.etf.cursor-agent-worker.plist.template"
  local dest="${AGENT_DIR}/${LABEL}.plist"
  bootout_label
  sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
      -e "s|{{CURSOR_AGENT_WORKER_LAUNCHER}}|${LAUNCHER}|g" \
      -e "s|{{HOME}}|${HOME}|g" \
      "${plist_src}" >"${dest}"
  bootstrap_label "${dest}"
  echo "✓ ${LABEL}"
  echo ""
  echo "確認 .env：CURSOR_AGENT_WORKER_ENABLED=1 與 CURSOR_API_KEY"
  echo "手機／Book Cloud Agent 選 worker=mac-mini（或 CURSOR_AGENT_WORKER_NAME）"
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --uninstall) uninstall_agents ;;
  --status) status_agents ;;
  "") install_agents ;;
  *) usage; exit 1 ;;
esac
