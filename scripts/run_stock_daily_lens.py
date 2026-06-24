#!/usr/bin/env python3
"""Build stock_daily_highlight + daily_highlight_alert → Supabase (+ local alert cache)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from lens_alert_digest import maybe_send_lens_daily_email
from project_dotenv import load_project_dotenv
from stock_daily_lens import publish_stock_daily_highlight, resolve_lens_trade_date
from stock_db import DEFAULT_DB_PATH, connect
from supabase_lens_sync import maybe_sync_lens_bundle_to_supabase


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="Cross-layer stock_daily_highlight batch")
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
            print("stock_daily_highlight: skipped（無台指交易日）")
            return 0

        rows, alert = publish_stock_daily_highlight(conn, trade_date)
        row_dicts = [r.to_db_dict() for r in rows]
        print(f"stock_daily_highlight: trade_date={trade_date} rows={len(rows)}")
        print(f"daily_highlight_alert: {alert['headline_zh']}")

        if not args.no_supabase:
            synced = maybe_sync_lens_bundle_to_supabase(
                conn,
                trade_date,
                row_dicts,
                alert,
                scheduled=args.date is None,
            )
            if synced is not None:
                highlight_n, alert_n = synced
                print(f"Supabase highlight sync: rows={highlight_n} alert_rows={alert_n}")

        if not args.no_email:
            sent = maybe_send_lens_daily_email(alert)
            if sent:
                print("daily_highlight_alert: email sent")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
