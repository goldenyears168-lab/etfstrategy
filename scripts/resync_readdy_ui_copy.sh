#!/usr/bin/env bash
# 文案／SSOT 變更後：site_content + stock_daily_highlight 一鍵重推 Supabase
#
# 步驟：
#   1. supabase/site/*.md → stock_research.site_content
#   2. 重建 stock_daily_highlight + daily_highlight_alert（含 narrative_zh）→ Supabase
#
# 用法：
#   scripts/resync_readdy_ui_copy.sh
#   scripts/resync_readdy_ui_copy.sh --site-only
#   scripts/resync_readdy_ui_copy.sh --highlight-only
#   scripts/resync_readdy_ui_copy.sh --days 30
#   scripts/resync_readdy_ui_copy.sh --latest   # 僅最近 1 個交易日（快 · 略過排程閘門）

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
SRC="${ROOT}/src"
export PYTHONPATH="${SRC}${PYTHONPATH:+:${PYTHONPATH}}"

SITE_ONLY=0
HIGHLIGHT_ONLY=0
LATEST_ONLY=0
DAYS=20

while [[ $# -gt 0 ]]; do
  case "$1" in
    --site-only) SITE_ONLY=1 ;;
    --highlight-only) HIGHLIGHT_ONLY=1 ;;
    --latest) LATEST_ONLY=1 ;;
    --days)
      shift
      DAYS="${1:?--days 需要數字}"
      ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      echo "未知參數: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ ! -x "${PYTHON}" ]]; then
  echo "錯誤：${PYTHON} 不存在，請先建立 .venv" >&2
  exit 2
fi

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  eval "$("${PYTHON}" -c "from project_dotenv import shell_export_dotenv; print(shell_export_dotenv())")"
  set +a
fi

# 手動重推：強制開啟 highlight sync（不依賴 RUN_SUPABASE_RESEARCH_SYNC）
export RUN_SUPABASE_LENS_SYNC=1

if ! "${PYTHON}" -c "from supabase_research_sync import supabase_configured; import sys; sys.exit(0 if supabase_configured() else 1)"; then
  echo "Supabase 未設定：請在 .env 設定 SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY" >&2
  exit 2
fi

run_site() {
  echo "=== [1/2] site_content ← supabase/site/*.md ==="
  if [[ -d "${ROOT}/supabase/site" ]]; then
    "${PYTHON}" "${ROOT}/scripts/sync_site_content_to_supabase.py"
  else
    echo "本機無 supabase/site/ · 改從 git HEAD 推送"
    "${PYTHON}" "${ROOT}/scripts/push_site_content_md.py"
  fi
}

run_highlight() {
  echo "=== [2/2] stock_daily_highlight + daily_highlight_alert ==="
  local days="${DAYS}"
  if [[ "${LATEST_ONLY}" -eq 1 ]]; then
    days=1
  fi
  "${PYTHON}" "${ROOT}/scripts/backfill_stock_daily_lens.py" --days "${days}"
}

FAILED=0

echo "=== [0] uiCopy.generated.ts ← home_ui_copy.py + daily_ui_copy.py ==="
"${PYTHON}" "${ROOT}/scripts/generate_readdy_ui_copy.py" || FAILED=1

if [[ "${HIGHLIGHT_ONLY}" -eq 0 ]]; then
  run_site || FAILED=1
fi

if [[ "${SITE_ONLY}" -eq 0 ]]; then
  run_highlight || FAILED=1
fi

if [[ "${FAILED}" -ne 0 ]]; then
  echo "resync_readdy_ui_copy: 部分步驟失敗 (exit=${FAILED})" >&2
  exit 1
fi

echo "resync_readdy_ui_copy: 完成"
