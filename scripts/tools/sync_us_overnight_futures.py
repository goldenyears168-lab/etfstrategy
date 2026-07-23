#!/usr/bin/env python3
"""Sync US ES/NQ overnight futures snapshots for TW intraday polls.

  # Backfill 90 TW sessions (1h bars · Yahoo ES=F/NQ=F)
  PYTHONPATH=src python3 scripts/tools/sync_us_overnight_futures.py --tail-days 90

  # Live snapshot now
  PYTHONPATH=src python3 scripts/tools/sync_us_overnight_futures.py --live
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.finpilot_local_backtest import load_price_panels  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect, upsert_us_futures_overnight_snapshots  # noqa: E402
from us_futures_overnight import (  # noqa: E402
    DEFAULT_CAPTURE_LABELS,
    build_live_snapshot,
    build_snapshots_for_dates,
)


def _tw_trade_dates(conn, *, tail_days: int, date_end: str | None) -> list[str]:
    close, _, _ = load_price_panels(conn)
    full = close.index.astype(str).tolist()
    end = date_end or full[-1]
    dates = [d for d in full if d <= end]
    if tail_days > 0 and len(dates) > tail_days:
        dates = dates[-tail_days:]
    return dates


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="US ES/NQ overnight snapshot sync")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--tail-days", type=int, default=90)
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--interval", default="1h", choices=("1h", "5m"))
    ap.add_argument("--live", action="store_true", help="single live snapshot")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        if args.live:
            row = build_live_snapshot()
            if not row:
                print("BLOCKER: no live ES/NQ snapshot", file=sys.stderr)
                return 1
            rows = [row]
        else:
            tw_dates = _tw_trade_dates(conn, tail_days=args.tail_days, date_end=args.date_end)
            print(f"Building ES/NQ snapshots for {len(tw_dates)} TW days · labels={len(DEFAULT_CAPTURE_LABELS)}")
            rows = build_snapshots_for_dates(
                tw_dates,
                capture_labels=DEFAULT_CAPTURE_LABELS,
                interval=args.interval,
            )
        if not rows:
            print("BLOCKER: zero snapshot rows", file=sys.stderr)
            return 1
        if args.dry_run:
            print(f"dry-run: {len(rows)} rows · sample={rows[0]}")
            return 0
        n = upsert_us_futures_overnight_snapshots(conn, rows)
        cov = sum(1 for r in rows if r.get("es_overnight_pct") is not None)
        print(f"Wrote {n} rows · ES overnight coverage {cov}/{len(rows)}")
        stamp = date.today().isoformat()
        print(f"  range {rows[0]['tw_session_date']} → {rows[-1]['tw_session_date']} · synced {stamp}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
