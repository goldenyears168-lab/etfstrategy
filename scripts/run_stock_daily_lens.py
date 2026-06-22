#!/usr/bin/env python3
"""Build stock_daily_lens + lens_daily_alert → SQLite + optional Supabase."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from lens_alert_digest import maybe_send_lens_daily_email, persist_lens_daily_alert
from project_dotenv import load_project_dotenv
from stock_daily_lens import persist_stock_daily_lens, resolve_lens_trade_date
from stock_db import DEFAULT_DB_PATH, connect
from supabase_lens_sync import maybe_sync_lens_bundle_to_supabase


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="Cross-layer stock_daily_lens batch")
    parser.add_argument("--date", help="trade_date YYYY-MM-DD（預設：最近交易日）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--no-supabase",
        action="store_true",
        help="略過 Supabase sync（即使 RUN_SUPABASE_RESEARCH_SYNC=1）",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="略過 email（即使 RUN_LENS_DAILY_NOTIFY=1）",
    )
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        trade_date = resolve_lens_trade_date(conn, args.date)
        if not trade_date:
            print("stock_daily_lens: skipped（無台指交易日）")
            return 0

        n = persist_stock_daily_lens(conn, trade_date)
        alert = persist_lens_daily_alert(conn, trade_date)
        print(f"stock_daily_lens: trade_date={trade_date} rows={n}")
        print(f"lens_daily_alert: {alert['headline_zh']}")

        if not args.no_supabase:
            synced = maybe_sync_lens_bundle_to_supabase(
                conn, trade_date, scheduled=args.date is None
            )
            if synced is not None:
                lens_n, alert_n = synced
                print(f"Supabase lens sync: lens_rows={lens_n} alert_rows={alert_n}")

        if not args.no_email:
            sent = maybe_send_lens_daily_email(alert)
            if sent:
                print("lens_daily_alert: email sent")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
