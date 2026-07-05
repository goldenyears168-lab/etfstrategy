#!/usr/bin/env python3
"""
外資持股 + 發行股數（FinMind TaiwanStockShareholding）→ stock_shareholding_daily。

注意：FinMind 此 dataset 為外資持股表，非投信持股餘額。
"""

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
    ShareholdingCoverage,
    connect,
    load_shareholding_coverage_map,
    upsert_stock_shareholding_daily,
)
from sync_etf_signal import SOURCE, fetch_finmind

DEFAULT_LOOKBACK_DAYS = 60
REQUEST_DELAY_SEC = 0.35
INCREMENTAL_OVERLAP_DAYS = 7


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def parse_shareholding_rows(stock_id: str, raw: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for item in raw:
        trade_date = str(item.get("date") or item.get("Date") or "")[:10]
        if not trade_date:
            continue
        foreign_sh = _float_or_none(
            item.get("ForeignInvestmentRemainingShares")
            or item.get("foreign_investment_remaining_shares")
        )
        foreign_ratio = _float_or_none(
            item.get("ForeignInvestmentRemainRatio")
            or item.get("foreign_investment_remain_ratio")
        )
        shares_issued = _float_or_none(
            item.get("NumberOfSharesIssued") or item.get("number_of_shares_issued")
        )
        rows.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "foreign_remaining_shares": foreign_sh,
                "foreign_remaining_ratio": foreign_ratio,
                "shares_issued": shares_issued,
                "capital_ntd": None,
                "source": SOURCE,
            }
        )
    return rows


def enrich_capital_ntd(
    conn,
    rows: list[dict],
) -> list[dict]:
    if not rows:
        return rows
    out: list[dict] = []
    for row in rows:
        cap = row.get("capital_ntd")
        shares = row.get("shares_issued")
        if cap is None and shares is not None:
            close_row = conn.execute(
                """
                SELECT close FROM stock_daily_bars
                WHERE stock_id = ? AND trade_date = ? AND source = 'finmind'
                LIMIT 1
                """,
                (row["stock_id"], row["trade_date"]),
            ).fetchone()
            if close_row and close_row[0] is not None:
                cap = float(shares) * float(close_row[0])
        out.append({**row, "capital_ntd": cap})
    return out


def resolve_shareholding_fetch_window(
    coverage: ShareholdingCoverage | None,
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


def sync_stock_shareholding_daily(
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
        coverage_map = load_shareholding_coverage_map(
            conn,
            [w["stock_id"] for w in watchlist],
            window_start=start.isoformat(),
            window_end=end.isoformat(),
        )
    finally:
        conn.close()

    stats = {
        "stocks": len(watchlist),
        "rows": 0,
        "ok": 0,
        "warn": 0,
        "skipped": 0,
    }

    for i, item in enumerate(watchlist):
        stock_id = item["stock_id"]
        action, fetch_start, fetch_end = resolve_shareholding_fetch_window(
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
            raw = fetch_finmind("TaiwanStockShareholding", stock_id, fetch_start, fetch_end)
            rows = parse_shareholding_rows(stock_id, raw)
            if not rows:
                stats["warn"] += 1
                continue
            stats["ok"] += 1
            if dry_run:
                stats["rows"] += len(rows)
                continue
            conn = connect(db_path)
            try:
                rows = enrich_capital_ntd(conn, rows)
                stats["rows"] += upsert_stock_shareholding_daily(conn, rows)
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
                f"  ... shareholding {i + 1}/{stats['stocks']} "
                f"ok={stats['ok']} rows={stats['rows']}",
                flush=True,
            )

    if not quiet and not dry_run:
        print(
            f"外資持股 sync：{stats['ok']}/{stats['stocks']} OK · "
            f"rows={stats['rows']} skipped={stats['skipped']} warn={stats['warn']}"
        )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="同步外資持股至 SQLite")
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
        sync_stock_shareholding_daily(
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
