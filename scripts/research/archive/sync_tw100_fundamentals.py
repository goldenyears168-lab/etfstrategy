#!/usr/bin/env python3
"""Backfill TW100 historical fundamentals (PER time series + financial history) for ML."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_db import DEFAULT_DB_PATH, connect, upsert_stock_financial_history, upsert_stock_fundamental
from stock_universe import TW100_UNIVERSE_ID, resolve_universe_watchlist
from sync_fundamentals import REQUEST_DELAY_SEC, SOURCE, build_stock_fundamental_history


def sync_tw100_fundamentals(
    db_path: Path,
    *,
    start: date,
    end: date,
    snapshot_date: str,
    dry_run: bool = False,
    quiet: bool = False,
    max_stocks: int = 0,
    request_delay: float = REQUEST_DELAY_SEC,
) -> dict[str, int]:
    conn = connect(db_path)
    try:
        watch = resolve_universe_watchlist(
            conn,
            TW100_UNIVERSE_ID,
            as_of=date.fromisoformat(snapshot_date),
        )
    finally:
        conn.close()
    if not watch:
        raise RuntimeError(f"TW100 universe empty @ {snapshot_date}")

    stock_ids = [w["stock_id"] for w in watch]
    if max_stocks > 0:
        stock_ids = stock_ids[:max_stocks]

    stats = {
        "stocks": len(stock_ids),
        "fundamental_rows": 0,
        "history_rows": 0,
        "ok": 0,
        "warn": 0,
    }

    for i, stock_id in enumerate(stock_ids):
        if i > 0 and request_delay > 0:
            time.sleep(request_delay)
        try:
            fund_rows, history, _ = build_stock_fundamental_history(stock_id, start, end)
            if not fund_rows and not history:
                stats["warn"] += 1
                if not quiet:
                    print(f"  WARN {stock_id}: no fundamental history", file=sys.stderr)
                continue
            stats["ok"] += 1
            if dry_run:
                stats["fundamental_rows"] += len(fund_rows)
                stats["history_rows"] += len(history)
                if not quiet:
                    print(f"  DRY {stock_id}: fund={len(fund_rows)} hist={len(history)}")
                continue
            conn = connect(db_path)
            try:
                if fund_rows:
                    stats["fundamental_rows"] += upsert_stock_fundamental(conn, fund_rows)
                if history:
                    stats["history_rows"] += upsert_stock_financial_history(conn, history)
            finally:
                conn.close()
            if not quiet:
                print(f"  {stock_id}: fund={len(fund_rows)} hist={len(history)}")
        except requests.HTTPError as exc:
            stats["warn"] += 1
            print(f"  WARN {stock_id}: HTTP {exc}", file=sys.stderr)
        except RuntimeError as exc:
            stats["warn"] += 1
            print(f"  WARN {stock_id}: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            stats["warn"] += 1
            print(f"  WARN {stock_id}: {exc}", file=sys.stderr)

    if not quiet:
        print(
            f"TW100 fundamentals: {stats['ok']}/{stats['stocks']} OK · "
            f"fund_rows={stats['fundamental_rows']} hist_rows={stats['history_rows']} "
            f"warn={stats['warn']}"
        )
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill TW100 historical fundamentals (FinMind)")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--sync-db", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--start-date", default="2015-01-01")
    p.add_argument("--end-date", default=None)
    p.add_argument("--snapshot-date", default="2026-06-26")
    p.add_argument("--max-stocks", type=int, default=0)
    p.add_argument("--request-delay", type=float, default=REQUEST_DELAY_SEC)
    args = p.parse_args()

    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date) if args.end_date else date.today()
    dry_run = args.dry_run or not args.sync_db
    try:
        sync_tw100_fundamentals(
            args.db,
            start=start,
            end=end,
            snapshot_date=args.snapshot_date,
            dry_run=dry_run,
            quiet=args.quiet,
            max_stocks=args.max_stocks,
            request_delay=args.request_delay,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
