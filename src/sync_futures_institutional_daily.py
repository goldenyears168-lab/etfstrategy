#!/usr/bin/env python3
"""期貨三大法人（FinMind TaiwanFuturesInstitutionalInvestors）→ futures_institutional_daily。"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from market_sync_window import min_rows_required, resolve_sync_window
from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    load_futures_institutional_coverage,
    upsert_futures_institutional_daily,
)
from sync_etf_signal import SOURCE, fetch_finmind

DEFAULT_LOOKBACK_DAYS = 60
REQUEST_DELAY_SEC = 0.35
INCREMENTAL_OVERLAP_DAYS = 7
DEFAULT_FUTURES_ID = "TX"


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def default_futures_id() -> str:
    return os.environ.get("SCREENER_FUTURES_ID", DEFAULT_FUTURES_ID).strip() or DEFAULT_FUTURES_ID


def parse_futures_institutional_rows(
    futures_id: str,
    raw: list[dict],
) -> list[dict]:
    rows: list[dict] = []
    for item in raw:
        trade_date = str(item.get("date") or item.get("Date") or "")[:10]
        if not trade_date:
            continue
        inst = str(
            item.get("institutional_investors")
            or item.get("name")
            or ""
        ).strip()
        if not inst:
            continue
        long_oi = _float_or_none(item.get("long_open_interest_balance_volume"))
        short_oi = _float_or_none(item.get("short_open_interest_balance_volume"))
        long_deal = _float_or_none(item.get("long_deal_volume"))
        short_deal = _float_or_none(item.get("short_deal_volume"))
        net_oi = None
        if long_oi is not None and short_oi is not None:
            net_oi = long_oi - short_oi
        net_deal = None
        if long_deal is not None and short_deal is not None:
            net_deal = long_deal - short_deal
        rows.append(
            {
                "futures_id": futures_id,
                "trade_date": trade_date,
                "inst_name": inst,
                "long_oi_vol": long_oi,
                "short_oi_vol": short_oi,
                "net_oi_vol": net_oi,
                "long_deal_vol": long_deal,
                "short_deal_vol": short_deal,
                "net_deal_vol": net_deal,
                "source": SOURCE,
            }
        )
    return rows


def resolve_futures_fetch_window(
    dmin: str | None,
    dmax: str | None,
    count: int,
    start: date,
    end: date,
    lookback_days: int,
    *,
    force_refresh: bool,
) -> tuple[str, date | None, date | None]:
    min_rows = min_rows_required(lookback_days)
    series: list[tuple[str | None, str | None, int]] = [(dmin, dmax, count)]
    return resolve_sync_window(
        start=start,
        end=end,
        min_rows=min_rows,
        series=series,
        force_refresh=force_refresh,
        overlap_days=INCREMENTAL_OVERLAP_DAYS,
    )


def sync_futures_institutional_daily(
    db_path: Path,
    lookback_days: int | None = None,
    *,
    window_start: date | None = None,
    window_end: date | None = None,
    futures_id: str | None = None,
    dry_run: bool = False,
    quiet: bool = False,
    request_delay: float = REQUEST_DELAY_SEC,
    force_refresh: bool = False,
) -> dict[str, int]:
    fid = futures_id or default_futures_id()
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
        dmin, dmax, count = load_futures_institutional_coverage(
            conn,
            fid,
            window_start=start.isoformat(),
            window_end=end.isoformat(),
        )
    finally:
        conn.close()

    action, fetch_start, fetch_end = resolve_futures_fetch_window(
        dmin,
        dmax,
        count,
        start,
        end,
        effective_lookback,
        force_refresh=force_refresh,
    )
    stats = {"rows": 0, "warn": 0, "skipped": 0, "action": action}

    if action == "skip":
        stats["skipped"] = 1
        if not quiet:
            print(f"  SKIP {fid}: 期貨法人已同步至 {dmax}", file=sys.stderr)
        return stats

    assert fetch_start is not None and fetch_end is not None
    if request_delay > 0:
        time.sleep(request_delay)

    try:
        raw = fetch_finmind(
            "TaiwanFuturesInstitutionalInvestors",
            fid,
            fetch_start,
            fetch_end,
        )
        rows = parse_futures_institutional_rows(fid, raw)
        if not rows:
            stats["warn"] = 1
            if not quiet:
                print(f"  WARN {fid}: 無期貨法人資料", file=sys.stderr)
            return stats
        stats["rows"] = len(rows)
        if dry_run:
            if not quiet:
                print(f"  DRY {fid}: rows={len(rows)}")
            return stats
        conn = connect(db_path)
        try:
            stats["rows"] = upsert_futures_institutional_daily(conn, rows)
        finally:
            conn.close()
        if not quiet:
            print(f"  {fid}: upsert={stats['rows']} ({action})")
    except requests.HTTPError as exc:
        stats["warn"] = 1
        print(f"  WARN {fid}: HTTP {exc}", file=sys.stderr)
    except RuntimeError as exc:
        stats["warn"] = 1
        print(f"  WARN {fid}: {exc}", file=sys.stderr)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="同步期貨三大法人至 SQLite")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--sync-db", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--futures-id", default=None)
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
        sync_futures_institutional_daily(
            args.db,
            window_start=start,
            window_end=end,
            futures_id=args.futures_id,
            dry_run=dry_run,
            quiet=args.quiet,
            force_refresh=args.force_refresh,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
