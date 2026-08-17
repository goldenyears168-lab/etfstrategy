#!/usr/bin/env bash
# 安全地把新的 Gmail app password 寫進 ${GOLDENSTOCKS_DATA_DIR}/.env。
#
# 為什麼要有這支：2026-08-14 起所有排程信件都被 Gmail 拒絕
#   (535 5.7.8 Username and Password not accepted)
# ——app password 被撤銷（Google 帳號密碼變更會自動撤銷所有 app password）。
# 整個觀察管道（morning-gate-brief / nightly-expert-digest / daily-sync 告警…）
# 在修好之前都是聾的。
#
# 設計原則：
#   * 用 `read -rs` 讀取，**畫面不回顯、不進 shell history、不寫任何 log**
#   * 自動去掉 Google 顯示時插入的空格（Google 會顯示成 "abcd efgh ijkl mnop"）
#   * 改之前先備份 .env
#   * 只動 GMAIL_APP_PASSWORD 這一行，其他行不碰
#   * 寫完可選擇立刻寄一封測試信驗證
#
# 先在手機瀏覽器產生新密碼：https://myaccount.google.com/apppasswords
# （需要帳號已開啟兩步驟驗證）
#
#   bash scripts/ops/set_gmail_app_password.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_ROOT="${GOLDENSTOCKS_DATA_DIR:-${HOME}/goldenstocks-data}"
ENV_FILE="${DATA_ROOT}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "✗ 找不到 ${ENV_FILE}" >&2
  exit 1
fi

echo "將寫入：${ENV_FILE}"
echo "先在 https://myaccount.google.com/apppasswords 產生新的 app password。"
echo ""

# -s = 不回顯；-r = 不解讀反斜線
read -rsp "貼上 Gmail app password（輸入不會顯示，貼完按 Enter）: " PW1
echo ""
read -rsp "再貼一次確認: " PW2
echo ""

# Google 顯示時會插空格，一律去掉所有空白
PW1="${PW1//[[:space:]]/}"
PW2="${PW2//[[:space:]]/}"

if [[ -z "${PW1}" ]]; then
  echo "✗ 空值，未做任何變更" >&2
  exit 1
fi
if [[ "${PW1}" != "${PW2}" ]]; then
  echo "✗ 兩次輸入不一致，未做任何變更" >&2
  exit 1
fi
if [[ "${#PW1}" -ne 16 ]]; then
  echo "⚠ 長度是 ${#PW1} 字元，Gmail app password 通常是 16 字元。"
  read -rp "還是要繼續嗎？(yes/no) " CONFIRM
  [[ "${CONFIRM}" == "yes" ]] || { echo "已取消"; exit 1; }
fi

BACKUP="${ENV_FILE}.bak-$(date '+%Y%m%d-%H%M%S')-gmail"
cp "${ENV_FILE}" "${BACKUP}"
chmod 600 "${BACKUP}"
echo "已備份 → ${BACKUP}"

# 用 python 改寫，避免 sed 對密碼裡的特殊字元（& / | \）誤解讀
PW="${PW1}" "${ROOT}/.venv/bin/python" - "${ENV_FILE}" <<'PYEOF'
import os, sys, pathlib
path = pathlib.Path(sys.argv[1])
pw = os.environ["PW"]
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
out, replaced = [], False
for line in lines:
    if line.startswith("GMAIL_APP_PASSWORD="):
        out.append(f"GMAIL_APP_PASSWORD={pw}\n")
        replaced = True
    else:
        out.append(line)
if not replaced:
    if out and not out[-1].endswith("\n"):
        out.append("\n")
    out.append(f"GMAIL_APP_PASSWORD={pw}\n")
path.write_text("".join(out), encoding="utf-8")
print("已更新 GMAIL_APP_PASSWORD（" + ("取代既有行" if replaced else "新增一行") + "）")
PYEOF

chmod 600 "${ENV_FILE}"
unset PW1 PW2

echo ""
read -rp "要立刻寄一封測試信驗證嗎？(yes/no) " SEND
if [[ "${SEND}" == "yes" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
  PYTHONPATH="${ROOT}/src" "${ROOT}/.venv/bin/python" - <<'PYEOF'
from notify_email import send_alert
try:
    send_alert("goldenstocks · Gmail app password 測試", "這封信寄到＝SMTP 憑證已修復。")
    print("✓ 測試信已送出，去收件匣確認")
except Exception as exc:
    print(f"✗ 仍然失敗：{exc}")
    print("  535 BadCredentials → app password 不對或已被撤銷，重新產生一組再跑一次")
    raise SystemExit(1)
PYEOF
fi
