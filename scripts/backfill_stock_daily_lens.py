#!/usr/bin/env python3
"""Backfill stock_daily_lens for delta history (≥20 trading days recommended)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from lens_alert_digest import persist_lens_daily_alert
from market_benchmark import list_trading_dates
from project_dotenv import load_project_dotenv
from stock_daily_lens import persist_stock_daily_lens, resolve_lens_trade_date
from stock_db import DEFAULT_DB_PATH, connect
from supabase_lens_sync import maybe_sync_lens_bundle_to_supabase


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="Backfill stock_daily_lens")
    parser.add_argument("--days", type=int, default=20, help="往回幾個交易日")
    parser.add_argument("--end", help="結束日 YYYY-MM-DD（預設：最近交易日）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--no-supabase",
        action="store_true",
        help="略過 Supabase sync",
    )
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        end = resolve_lens_trade_date(conn, args.end)
        if not end:
            print("backfill: no trading dates")
            return 1
        dates = list_trading_dates(conn, end=end, limit=max(1, args.days))
        for trade_date in dates:
            n = persist_stock_daily_lens(conn, trade_date)
            persist_lens_daily_alert(conn, trade_date)
            print(f"  {trade_date}: lens_rows={n}")
            if not args.no_supabase:
                synced = maybe_sync_lens_bundle_to_supabase(
                    conn, trade_date, scheduled=False
                )
                if synced is not None:
                    print(f"    supabase lens={synced[0]} alert={synced[1]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
