#!/usr/bin/env python3
"""Songshan copytrade Order poll · T+1 09:25–09:40 · 1 張.

  PYTHONPATH=src .venv-fubon/bin/python scripts/order/run_songshan_copytrade_poll.py
  PYTHONPATH=src .venv-fubon/bin/python scripts/order/run_songshan_copytrade_poll.py \\
    --date 2026-07-23 --time 09:25 --force-dry-run
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

from project_dotenv import load_project_dotenv
from stock_db import DEFAULT_DB_PATH, connect

_TZ = ZoneInfo("Asia/Taipei")
SNAP_DIR = ROOT / "reports" / "order" / "snapshots"


def main() -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--time")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--force-dry-run", action="store_true")
    args = ap.parse_args()
    now = datetime.now(tz=_TZ)
    day = args.date or now.strftime("%Y-%m-%d")
    minute = args.time or now.strftime("%H:%M")

    from order.songshan_copytrade_config import load_songshan_copytrade_order_config
    from order.songshan_copytrade_order import process_songshan_copytrade_poll

    cfg = load_songshan_copytrade_order_config()
    dry = True if args.force_dry_run else cfg.dry_run
    conn = connect(args.db)
    try:
        payload = process_songshan_copytrade_poll(
            session_date=day,
            poll_minute=minute,
            conn=conn,
            dry_run=dry,
            cfg=cfg,
        )
    finally:
        conn.close()

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = day.replace("-", "")
    path = SNAP_DIR / f"songshan_copytrade_{stamp}.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    path.write_text(text, encoding="utf-8")
    (SNAP_DIR / "songshan_copytrade_latest.json").write_text(text, encoding="utf-8")
    print(text)
    print("SONGSHAN_COPYTRADE_POLL=1")
    if payload.get("status") in {"submitted", "dry_run", "queued_no_submit"}:
        print("SONGSHAN_COPYTRADE_ENTRY=1")
    if payload.get("status") == "skipped_fail":
        print("SONGSHAN_COPYTRADE_FAIL_SKIP=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
