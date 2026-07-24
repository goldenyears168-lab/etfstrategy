#!/usr/bin/env bash
# Book → mini：写入 CURSOR_API_KEY 并安装 My Machines worker（不把 key 印到 stdout）
#
# 用法（三选一）：
#   1) 把 key 单行写入仓库根 .cursor_api_key_tmp，再：
#        bash scripts/setup_mini_cursor_worker_key.sh
#   2) bash scripts/setup_mini_cursor_worker_key.sh --from-env
#        （读 Book 本机 .env 的 CURSOR_API_KEY，scp 到 mini）
#   3) CURSOR_API_KEY='cursor_...' bash scripts/setup_mini_cursor_worker_key.sh
#
# 建立 key：https://cursor.com/dashboard/integrations

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="${ROOT}/.cursor_api_key_tmp"
HOST="${CURSOR_WORKER_SSH_HOST:-mac-mini}"
REMOTE_ROOT='Documents/ETF/股票研究'

KEY="${CURSOR_API_KEY:-}"
if [[ "${1:-}" == "--from-env" ]]; then
  if [[ -f "${ROOT}/.env" ]]; then
    KEY="$(grep -E '^CURSOR_API_KEY=' "${ROOT}/.env" | head -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//;s/["'\'']$//')"
  fi
elif [[ -z "${KEY}" && -f "${TMP}" ]]; then
  KEY="$(tr -d ' \t\r\n' <"${TMP}")"
fi

if [[ -z "${KEY}" ]]; then
  echo "缺少 CURSOR_API_KEY。" >&2
  echo "请到 https://cursor.com/dashboard/integrations 建立个人 API key，" >&2
  echo "写入 ${TMP}（单行），再重跑本脚本。" >&2
  exit 1
fi

if [[ "${KEY}" != cursor_* && "${KEY}" != key_* ]]; then
  echo "警告：key 前缀不像 Cursor API key；仍继续。" >&2
fi

echo "→ upsert mini .env + install worker launchd（key 不回显）"
ssh -o BatchMode=yes "${HOST}" "bash -s" <<REMOTE
set -euo pipefail
ENV="\$HOME/${REMOTE_ROOT}/.env"
touch "\$ENV"
upsert() {
  local k="\$1" v="\$2"
  if grep -q "^\${k}=" "\$ENV" 2>/dev/null; then
    # use | delimiter; value may contain /
    sed -i '' "s|^\${k}=.*|\${k}=\${v}|" "\$ENV"
  else
    printf '\\n%s=%s\\n' "\$k" "\$v" >> "\$ENV"
  fi
}
upsert CURSOR_AGENT_WORKER_ENABLED 1
upsert CURSOR_AGENT_WORKER_NAME mac-mini
upsert CURSOR_API_KEY '${KEY}'
cd "\$HOME/${REMOTE_ROOT}"
chmod +x scripts/install-cursor-agent-worker-launchd.sh
bash scripts/install-cursor-agent-worker-launchd.sh
sleep 2
bash scripts/install-cursor-agent-worker-launchd.sh --status
# health: process up?
if pgrep -f 'agent worker' >/dev/null 2>&1; then
  echo WORKER_PROCESS=up
else
  echo WORKER_PROCESS=down
  echo '--- launcher log tail ---'
  tail -n 40 "\$HOME/Library/Logs/com.jackm4.etf/cursor-agent-worker.log" 2>/dev/null || true
  exit 2
fi
REMOTE

rm -f "${TMP}"
echo "✓ mini worker 已安装；临时 key 文件已删除（若有）"
echo "手机 Cursor App：Add Workspace → 本 repo → Agent 环境选 mac-mini"
