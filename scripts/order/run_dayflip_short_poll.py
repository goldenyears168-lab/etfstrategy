#!/usr/bin/env python3
"""dayflip-futures-short v1 · order poll · 08:44-13:41 · 每次呼叫 reconcile 一次.

規格：reports/research/branch-footprint-screen/dayflip_gapup_short/FROZEN_SPEC_V1.json
G2 未過（前推 5/40 訊號日）；2026-08-07 使用者明確授權跳過 G2 直接建置真實下單，
且明確要求不預設 dry-run（見 src/order/dayflip_short_order.py 檔頭說明）。

⚠️ 2026-08-08：launchd 生產路徑已改用 scripts/order/run_dayflip_short_worker.py
（KeepAlive 常駐 worker，重用單一 Fubon session，取代這支腳本原本每次呼叫都全新
登入的 StartInterval 冷啟動模式）。這支腳本保留供手動測試 / 單次 debug 用，
不再是 launchd 的進入點——跟 scripts/order/run_tmf_channel_poll.py 相對於
run_tmf_channel_worker.py 的關係一樣。

  PYTHONPATH=src:scripts/research .venv/bin/python \\
    scripts/order/run_dayflip_short_poll.py
  # 手動測試（保證不送真單，覆蓋 sleeve 旗標）：
  PYTHONPATH=src:scripts/research .venv/bin/python \\
    scripts/order/run_dayflip_short_poll.py --force-dry-run
  # 主 .venv 已原生裝 fubon_neo；.venv-fubon 只在 host Python>3.13 時當備援（見
  # fubon_subprocess.py），launchd launcher 本身也是用 .venv/bin/python。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/research"))

from project_dotenv import load_project_dotenv  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
SNAP_DIR = ROOT / "reports" / "order" / "snapshots"


def in_trade_window(hm: str) -> bool:
    return "08:44" <= hm <= "13:41"


def main() -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-dry-run", action="store_true",
                    help="覆蓋 sleeve 旗標，保證不送真單（人工測試用）")
    ap.add_argument("--allow-outside-window", action="store_true",
                    help="測試用：忽略時間窗檢查")
    ap.add_argument("--override-hm",
                    help="測試用：模擬現在時刻(HH:MM，例如08:45)，走完整流程不用等真的開盤")
    ap.add_argument("--reset-ledger", action="store_true",
                    help="測試用：先刪除今日 ledger 再執行，避免被前一次測試的terminal狀態擋住")
    ap.add_argument("--force-reset-live-position", action="store_true",
                    help="⚠️ 危險：搭配 --reset-ledger，允許刪除一個 status=entered（真的有部位）的"
                         "ledger。沒加這個旗標時，--reset-ledger 對 entered 狀態會直接拒絕執行，"
                         "避免手滑在盤中對正在追蹤的真實部位重置成 idle 而重複進場。")
    args = ap.parse_args()

    if args.reset_ledger:
        from order.dayflip_short_order import _LEDGER_PATH, load_ledger
        current = load_ledger()
        if current.status == "entered" and not args.force_reset_live_position:
            print(json.dumps({
                "error": "refused_reset_live_position",
                "message": (
                    "今天的 ledger 顯示 status=entered（可能是真實部位），拒絕 --reset-ledger。"
                    "若確定沒有真實部位（例如已經人工用富邦看盤軟體核對過），加上 "
                    "--force-reset-live-position 才會真的刪除。"
                ),
                "current_ledger": current.__dict__,
            }, ensure_ascii=False, indent=1, default=str))
            return 1
        _LEDGER_PATH.unlink(missing_ok=True)
        print(f"reset ledger: {_LEDGER_PATH}")

    now = datetime.now(tz=_TZ)
    hm = args.override_hm or now.strftime("%H:%M")
    if not args.allow_outside_window and not args.override_hm and not in_trade_window(hm):
        print(json.dumps({"skip": "outside_window", "hm": hm}, ensure_ascii=False))
        return 0

    from order.dayflip_short_order import reconcile_once

    result = reconcile_once(dry_run=True if args.force_dry_run else None,
                            override_hm=args.override_hm)

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d")
    path = SNAP_DIR / f"dayflip_short_{stamp}.json"
    prior = []
    if path.exists():
        try:
            prior = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            prior = []
    prior.append({"ts": now.isoformat(), **result})
    path.write_text(json.dumps(prior, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    if result.get("action") == "entry_failed":
        print("DAYFLIP_SHORT_ENTRY_FAILED=1")
        return 1
    if result.get("action") == "poll_query_failed":
        print("DAYFLIP_SHORT_POLL_FAILED=1")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
