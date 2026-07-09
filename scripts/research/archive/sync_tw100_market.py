#!/usr/bin/env python3
"""Backfill/sync TW100 universe daily bars (FinMind) — ML path only."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_db import DEFAULT_DB_PATH, connect
from stock_universe import TW100_UNIVERSE_ID, resolve_universe_watchlist, sync_tw100_snapshot
from sync_stock_market_daily import sync_stock_market_daily
from universe_config import tw100_config

MISSING_TW100_BACKFILL = (
    "1101,1301,1326,1802,2207,2395,2492,2609,2615,2801,2834,2912,5871,6446,6770,8069"
)


def main() -> int:
    cfg = tw100_config()
    parser = argparse.ArgumentParser(description="Sync TW100 universe → stock_daily_bars")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--sync-db", action="store_true", help="寫入 DB（預設 dry-run）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help=f"回溯天數（預設 {cfg['default_lookback_days']}；與 --start-date 互斥）",
    )
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD（與 lookback 互斥）")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD（預設今天）")
    parser.add_argument(
        "--universe-as-of",
        default=None,
        help="TW100 成分基準日（預設 end-date 或今天）",
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="只 backfill 16 檔已知缺 K 線標的",
    )
    parser.add_argument(
        "--stock-ids",
        default=None,
        help=f"逗號分隔代號（預設全 TW100；missing-only 時為 {MISSING_TW100_BACKFILL}）",
    )
    parser.add_argument("--refresh-universe", action="store_true", help="重抓 TaiwanStockMarketValue")
    parser.add_argument("--force-refresh", action="store_true", help="忽略 DB 覆蓋強制重抓")
    parser.add_argument("--request-delay", type=float, default=0.35)
    parser.add_argument(
        "--sync-chip",
        action="store_true",
        help="Phase 2：同步 TW100 籌碼（margin/lending/daytrade）",
    )
    parser.add_argument(
        "--chip-lookback-days",
        type=int,
        default=400,
        help="--sync-chip 回溯天數（預設 400）",
    )
    args = parser.parse_args()

    if args.start_date and args.lookback_days is not None:
        print("ERROR: --start-date 與 --lookback-days 請擇一", file=sys.stderr)
        return 1

    lookback_days = args.lookback_days if args.lookback_days is not None else cfg["default_lookback_days"]

    window_end = date.fromisoformat(args.end_date) if args.end_date else date.today()
    universe_as_of = (
        date.fromisoformat(args.universe_as_of) if args.universe_as_of else window_end
    )
    window_start = date.fromisoformat(args.start_date) if args.start_date else None

    if args.refresh_universe:
        sync_tw100_snapshot(args.db, as_of=universe_as_of)

    stock_ids: list[str] | None
    if args.stock_ids:
        stock_ids = [s.strip() for s in args.stock_ids.split(",") if s.strip()]
    elif args.missing_only:
        stock_ids = [s.strip() for s in MISSING_TW100_BACKFILL.split(",") if s.strip()]
    else:
        stock_ids = None

    dry_run = args.dry_run or not args.sync_db
    sync_stock_market_daily(
        args.db,
        lookback_days if window_start is None else None,
        window_start=window_start,
        window_end=window_end,
        stock_ids=stock_ids,
        universe=TW100_UNIVERSE_ID,
        universe_as_of=universe_as_of,
        dry_run=dry_run,
        quiet=args.quiet,
        request_delay=args.request_delay,
        force_refresh=args.force_refresh,
    )

    if args.sync_chip:
        from sync_stock_chip_daily import sync_stock_chip_daily  # noqa: WPS433

        chip_ids = stock_ids
        if chip_ids is None:
            conn = connect(args.db)
            try:
                watch = resolve_universe_watchlist(conn, TW100_UNIVERSE_ID, as_of=universe_as_of)
            finally:
                conn.close()
            chip_ids = [w["stock_id"] for w in watch]
        sync_stock_chip_daily(
            args.db,
            args.chip_lookback_days if window_start is None else None,
            window_start=window_start,
            window_end=window_end,
            stock_ids=chip_ids,
            dry_run=dry_run,
            quiet=args.quiet,
            request_delay=args.request_delay,
            force_refresh=args.force_refresh,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
