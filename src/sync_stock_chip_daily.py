#!/usr/bin/env python3
"""
成分股融資融券 / 借券 / 當沖（FinMind）→ stock_margin_daily 等。

Universe：ETF 持股聯集；incremental 邏輯同 sync_stock_market_daily。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from chip_data import parse_daytrade_rows, parse_lending_rows, parse_margin_rows
from market_sync_window import min_rows_required, resolve_sync_window
from stock_db import (
    DEFAULT_DB_PATH,
    StockChipCoverage,
    connect,
    load_etf_constituent_watchlist,
    load_stock_chip_coverage_map,
    upsert_stock_daytrade_daily,
    upsert_stock_lending_daily,
    upsert_stock_margin_daily,
)
from sync_etf_signal import SOURCE, fetch_finmind

DEFAULT_LOOKBACK_DAYS = 14
REQUEST_DELAY_SEC = 0.35
INCREMENTAL_OVERLAP_DAYS = 7


def _min_rows_required(lookback_days: int) -> int:
    return min_rows_required(lookback_days)


def resolve_chip_fetch_window(
    coverage: StockChipCoverage | None,
    start: date,
    end: date,
    lookback_days: int,
    *,
    force_refresh: bool,
) -> tuple[str, date | None, date | None]:
    window_days = max(1, (end - start).days + 1)
    min_rows = _min_rows_required(lookback_days if lookback_days else window_days)
    if coverage is None:
        series: list[tuple[str | None, str | None, int]] = [
            (None, None, 0),
            (None, None, 0),
            (None, None, 0),
        ]
    else:
        series = [
            (coverage.margin_min, coverage.margin_max, coverage.margin_count_window),
            (coverage.lending_min, coverage.lending_max, coverage.lending_count_window),
            (coverage.daytrade_min, coverage.daytrade_max, coverage.daytrade_count_window),
        ]
    return resolve_sync_window(
        start=start,
        end=end,
        min_rows=min_rows,
        series=series,
        force_refresh=force_refresh,
        overlap_days=INCREMENTAL_OVERLAP_DAYS,
    )


def build_chip_rows(
    stock_id: str,
    start: date,
    end: date,
) -> tuple[list[dict], list[dict], list[dict]]:
    margin_raw = fetch_finmind(
        "TaiwanStockMarginPurchaseShortSale", stock_id, start, end
    )
    lending_raw = fetch_finmind("TaiwanStockSecuritiesLending", stock_id, start, end)
    daytrade_raw = fetch_finmind("TaiwanStockDayTrading", stock_id, start, end)
    return (
        parse_margin_rows(stock_id, margin_raw),
        parse_lending_rows(stock_id, lending_raw),
        parse_daytrade_rows(stock_id, daytrade_raw),
    )


def sync_stock_chip_daily(
    db_path: Path,
    lookback_days: int | None = None,
    *,
    window_start: date | None = None,
    window_end: date | None = None,
    stock_ids: list[str] | None = None,
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
        if stock_ids:
            name_rows = conn.execute(
                f"""
                SELECT stock_id, MAX(stock_name) AS stock_name
                FROM etf_holdings
                WHERE stock_id IN ({",".join("?" * len(stock_ids))})
                GROUP BY stock_id
                """,
                stock_ids,
            ).fetchall()
            name_by_id = {str(r["stock_id"]): r["stock_name"] or "" for r in name_rows}
            watchlist = [
                {
                    "stock_id": sid,
                    "stock_name": name_by_id.get(sid, ""),
                    "etf_hold_count": 0,
                }
                for sid in stock_ids
            ]
        else:
            watchlist = load_etf_constituent_watchlist(conn)
        coverage_stock_ids = [w["stock_id"] for w in watchlist]
        coverage_map = load_stock_chip_coverage_map(
            conn,
            coverage_stock_ids,
            window_start=start.isoformat(),
            window_end=end.isoformat(),
        )
    finally:
        conn.close()

    if not watchlist:
        raise RuntimeError("持股聯集為空：請先跑收盤持股同步")

    if max_stocks > 0:
        watchlist = watchlist[:max_stocks]

    stats = {
        "stocks": len(watchlist),
        "margin": 0,
        "lending": 0,
        "daytrade": 0,
        "ok": 0,
        "warn": 0,
        "skipped": 0,
        "incremental": 0,
        "full": 0,
    }

    for i, item in enumerate(watchlist):
        stock_id = item["stock_id"]
        action, fetch_start, fetch_end = resolve_chip_fetch_window(
            coverage_map.get(stock_id),
            start,
            end,
            effective_lookback,
            force_refresh=force_refresh,
        )
        if action == "skip":
            stats["skipped"] += 1
            if not quiet:
                cov = coverage_map[stock_id]
                print(
                    f"  SKIP {stock_id}: 融資至 {cov.margin_max} "
                    f"借券至 {cov.lending_max} 當沖至 {cov.daytrade_max}",
                    file=sys.stderr,
                )
            continue

        if i > 0 and request_delay > 0:
            time.sleep(request_delay)

        assert fetch_start is not None and fetch_end is not None
        if action == "incremental":
            stats["incremental"] += 1
        else:
            stats["full"] += 1

        try:
            margin, lending, daytrade = build_chip_rows(
                stock_id, fetch_start, fetch_end
            )
            if not margin and not lending and not daytrade:
                stats["warn"] += 1
                if not quiet:
                    print(f"  WARN {stock_id}: 無籌碼延伸資料", file=sys.stderr)
                continue
            stats["ok"] += 1
            if dry_run:
                if not quiet:
                    tag = "增量" if action == "incremental" else "全量"
                    print(
                        f"  DRY {stock_id} ({tag}): margin={len(margin)} "
                        f"lending={len(lending)} daytrade={len(daytrade)}"
                    )
                stats["margin"] += len(margin)
                stats["lending"] += len(lending)
                stats["daytrade"] += len(daytrade)
                continue
            conn = connect(db_path)
            try:
                stats["margin"] += upsert_stock_margin_daily(conn, margin)
                stats["lending"] += upsert_stock_lending_daily(conn, lending)
                stats["daytrade"] += upsert_stock_daytrade_daily(conn, daytrade)
            finally:
                conn.close()
            if quiet:
                tag = "Δ" if action == "incremental" else ""
                print(
                    f"  {stock_id}{tag}: m={len(margin)} l={len(lending)} "
                    f"d={len(daytrade)} ({fetch_start}～{fetch_end})"
                )
        except requests.HTTPError as exc:
            stats["warn"] += 1
            print(f"  WARN {stock_id}: FinMind HTTP {exc}", file=sys.stderr)
        except RuntimeError as exc:
            stats["warn"] += 1
            print(f"  WARN {stock_id}: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            stats["warn"] += 1
            print(f"  WARN {stock_id}: {exc}", file=sys.stderr)

    if not quiet and not dry_run:
        print(
            f"籌碼延伸 sync：{stats['ok']}/{stats['stocks']} 檔 OK，"
            f"跳過 {stats['skipped']} · 增量 {stats['incremental']} · 全量 {stats['full']}，"
            f"margin={stats['margin']} lending={stats['lending']} "
            f"daytrade={stats['daytrade']} warn={stats['warn']}"
        )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="同步融資融券/借券/當沖至 SQLite")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--sync-db", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--lookback-days", type=int, default=None)
    parser.add_argument("--max-stocks", type=int, default=0)
    parser.add_argument("--request-delay", type=float, default=REQUEST_DELAY_SEC)
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    if args.start_date and args.lookback_days is not None:
        print("ERROR: --start-date 與 --lookback-days 請擇一", file=sys.stderr)
        return 1
    lookback = args.lookback_days if args.lookback_days is not None else DEFAULT_LOOKBACK_DAYS
    window_start = date.fromisoformat(args.start_date) if args.start_date else None
    window_end = date.fromisoformat(args.end_date) if args.end_date else None
    dry_run = args.dry_run or not args.sync_db
    try:
        sync_stock_chip_daily(
            args.db,
            lookback if window_start is None else None,
            window_start=window_start,
            window_end=window_end,
            dry_run=dry_run,
            quiet=args.quiet,
            max_stocks=args.max_stocks,
            request_delay=args.request_delay,
            force_refresh=args.force_refresh,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
