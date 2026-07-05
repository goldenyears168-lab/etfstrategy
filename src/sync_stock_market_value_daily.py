#!/usr/bin/env python3
"""台股市值（FinMind TaiwanStockMarketValue）→ stock_market_value_daily + mcap/revenue TTM。"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from market_sync_window import min_rows_required, resolve_sync_window
from screener_universe import resolve_sync_watchlist
from stock_db import (
    DEFAULT_DB_PATH,
    MarketValueCoverage,
    connect,
    load_market_value_coverage_map,
    upsert_stock_market_value_daily,
)
from sync_etf_signal import SOURCE, fetch_finmind

DEFAULT_LOOKBACK_DAYS = 60
REQUEST_DELAY_SEC = 0.35
INCREMENTAL_OVERLAP_DAYS = 7


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def parse_market_value_rows(stock_id: str, raw: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for item in raw:
        trade_date = str(item.get("date") or item.get("Date") or "")[:10]
        if not trade_date:
            continue
        mv = _float_or_none(item.get("market_value"))
        if mv is None:
            continue
        rows.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "market_value_ntd": mv,
                "mcap_to_revenue_ttm": None,
                "source": SOURCE,
            }
        )
    return rows


def _revenue_ttm(conn, stock_id: str, as_of: str) -> float | None:
    rows = conn.execute(
        """
        SELECT period_date, value
        FROM stock_financial_history
        WHERE stock_id = ? AND metric = 'revenue' AND period_type = 'month'
          AND period_date <= ?
        ORDER BY period_date DESC
        LIMIT 12
        """,
        (stock_id, as_of),
    ).fetchall()
    if len(rows) < 12:
        return None
    return sum(float(r["value"]) for r in rows)


def enrich_mcap_to_revenue(conn, rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        ratio = row.get("mcap_to_revenue_ttm")
        mv = row.get("market_value_ntd")
        if ratio is None and mv is not None:
            rev_ttm = _revenue_ttm(conn, row["stock_id"], row["trade_date"])
            if rev_ttm and rev_ttm > 0:
                ratio = round(float(mv) / rev_ttm * 100.0, 4)
        out.append({**row, "mcap_to_revenue_ttm": ratio})
    return out


def resolve_market_value_fetch_window(
    coverage: MarketValueCoverage | None,
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


def sync_stock_market_value_daily(
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
    request_delay: float = REQUEST_DELAY_SEC,
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
        coverage_map = load_market_value_coverage_map(
            conn,
            [w["stock_id"] for w in watchlist],
            window_start=start.isoformat(),
            window_end=end.isoformat(),
        )
    finally:
        conn.close()

    stats = {"stocks": len(watchlist), "rows": 0, "ok": 0, "warn": 0, "skipped": 0}

    for i, item in enumerate(watchlist):
        stock_id = item["stock_id"]
        action, fetch_start, fetch_end = resolve_market_value_fetch_window(
            coverage_map.get(stock_id),
            start,
            end,
            effective_lookback,
            force_refresh=force_refresh,
        )
        if action == "skip":
            stats["skipped"] += 1
            continue
        if i > 0 and request_delay > 0:
            time.sleep(request_delay)
        assert fetch_start is not None and fetch_end is not None
        try:
            raw = fetch_finmind("TaiwanStockMarketValue", stock_id, fetch_start, fetch_end)
            rows = parse_market_value_rows(stock_id, raw)
            if not rows:
                stats["warn"] += 1
                continue
            stats["ok"] += 1
            if dry_run:
                stats["rows"] += len(rows)
                continue
            conn = connect(db_path)
            try:
                rows = enrich_mcap_to_revenue(conn, rows)
                stats["rows"] += upsert_stock_market_value_daily(conn, rows)
            finally:
                conn.close()
        except requests.HTTPError as exc:
            stats["warn"] += 1
            print(f"  WARN {stock_id}: HTTP {exc}", file=sys.stderr)
        except RuntimeError as exc:
            stats["warn"] += 1
            print(f"  WARN {stock_id}: {exc}", file=sys.stderr)

        if not quiet and stats["stocks"] >= 20 and (i + 1) % 10 == 0:
            print(
                f"  ... market-value {i + 1}/{stats['stocks']} "
                f"ok={stats['ok']} rows={stats['rows']}",
                flush=True,
            )

    if not quiet and not dry_run:
        print(
            f"市值 sync：{stats['ok']}/{stats['stocks']} OK · "
            f"rows={stats['rows']} skipped={stats['skipped']} warn={stats['warn']}"
        )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="同步台股市值至 SQLite")
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
        sync_stock_market_value_daily(
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
