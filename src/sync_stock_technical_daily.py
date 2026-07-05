#!/usr/bin/env python3
"""由 stock_daily_bars 批次計算技術指標 → stock_technical_daily。"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from market_sync_window import min_rows_required, resolve_sync_window
from screener_universe import resolve_sync_watchlist
from stock_db import (
    DEFAULT_DB_PATH,
    TechnicalCoverage,
    connect,
    load_technical_coverage_map,
    upsert_stock_technical_daily,
)
from technical_indicators import HIGH_LOOKBACK, compute_technical_rows

DEFAULT_LOOKBACK_DAYS = 60
INCREMENTAL_OVERLAP_DAYS = 7
BAR_BUFFER = HIGH_LOOKBACK + 20


def resolve_technical_fetch_window(
    coverage: TechnicalCoverage | None,
    start: date,
    end: date,
    lookback_days: int,
    *,
    force_refresh: bool,
) -> tuple[str, date | None, date | None]:
    window_days = max(1, (end - start).days + 1)
    min_rows = min_rows_required(lookback_days if lookback_days else window_days)
    if coverage is None:
        series: list[tuple[str | None, str | None, int]] = [(None, None, 0)]
    else:
        series = [(coverage.min_date, coverage.max_date, coverage.count_window)]
    return resolve_sync_window(
        start=start,
        end=end,
        min_rows=min_rows,
        series=series,
        force_refresh=force_refresh,
        overlap_days=INCREMENTAL_OVERLAP_DAYS,
    )


def _load_bars_for_technical(
    conn,
    stock_id: str,
    *,
    output_start: date,
    output_end: date,
) -> list[dict]:
    buffer_start = (output_start - timedelta(days=BAR_BUFFER * 2)).isoformat()
    rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind'
          AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC
        """,
        (stock_id, buffer_start, output_end.isoformat()),
    ).fetchall()
    return [dict(r) for r in rows]


def sync_stock_technical_daily(
    db_path: Path,
    lookback_days: int | None = None,
    *,
    window_start: date | None = None,
    window_end: date | None = None,
    stock_ids: list[str] | None = None,
    universe: str = "etf_watchlist",
    universe_as_of: date | None = None,
    dry_run: bool = False,
    quiet: bool = False,
    max_stocks: int = 0,
    force_refresh: bool = False,
) -> dict[str, int]:
    end = window_end or date.today()
    if window_start is not None:
        start = window_start
        effective_lookback = max(1, (end - start).days + 1)
    elif lookback_days is not None:
        start = end - timedelta(days=lookback_days)
        effective_lookback = lookback_days
    else:
        effective_lookback = DEFAULT_LOOKBACK_DAYS
        start = end - timedelta(days=effective_lookback)

    conn = connect(db_path)
    try:
        watchlist = resolve_sync_watchlist(
            conn,
            universe,
            universe_as_of=universe_as_of,
            stock_ids=stock_ids,
            end=end,
        )
        if not watchlist:
            raise RuntimeError(f"universe {universe} 為空")
        if max_stocks > 0:
            watchlist = watchlist[:max_stocks]
        coverage_map = load_technical_coverage_map(
            conn,
            [w["stock_id"] for w in watchlist],
            window_start=start.isoformat(),
            window_end=end.isoformat(),
        )
    finally:
        conn.close()

    stats = {"stocks": len(watchlist), "rows": 0, "ok": 0, "warn": 0, "skipped": 0}
    start_s = start.isoformat()
    end_s = end.isoformat()

    for item in watchlist:
        stock_id = item["stock_id"]
        action, fetch_start, fetch_end = resolve_technical_fetch_window(
            coverage_map.get(stock_id),
            start,
            end,
            effective_lookback,
            force_refresh=force_refresh,
        )
        if action == "skip":
            stats["skipped"] += 1
            continue
        assert fetch_start is not None and fetch_end is not None
        conn = connect(db_path)
        try:
            bars = _load_bars_for_technical(
                conn,
                stock_id,
                output_start=fetch_start,
                output_end=fetch_end,
            )
        finally:
            conn.close()
        if len(bars) < 20:
            stats["warn"] += 1
            continue
        all_rows = compute_technical_rows(stock_id, bars)
        rows = [r for r in all_rows if start_s <= r["trade_date"] <= end_s]
        if not rows:
            stats["warn"] += 1
            continue
        stats["ok"] += 1
        if dry_run:
            stats["rows"] += len(rows)
            continue
        conn = connect(db_path)
        try:
            stats["rows"] += upsert_stock_technical_daily(conn, rows)
        finally:
            conn.close()

    if not quiet and not dry_run:
        print(
            f"技術指標 sync：{stats['ok']}/{stats['stocks']} OK · "
            f"rows={stats['rows']} skipped={stats['skipped']} warn={stats['warn']}"
        )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="計算並寫入 stock_technical_daily")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--sync-db", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--universe", default="etf_watchlist")
    parser.add_argument("--max-stocks", type=int, default=0)
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()

    end = date.fromisoformat(args.end_date) if args.end_date else date.today()
    start = (
        date.fromisoformat(args.start_date)
        if args.start_date
        else end - timedelta(days=args.lookback_days)
    )
    dry_run = args.dry_run or not args.sync_db
    try:
        sync_stock_technical_daily(
            args.db,
            window_start=start,
            window_end=end,
            universe=args.universe,
            dry_run=dry_run,
            quiet=args.quiet,
            max_stocks=args.max_stocks,
            force_refresh=args.force_refresh,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
