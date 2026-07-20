#!/usr/bin/env python3
"""
補齊 ETF 歷史曾持有、但已不在最新 universe 的成分股市場／籌碼資料。

用法：
  python scripts/backfill_historical_constituents.py --etf-code 00981A --report
  python scripts/backfill_historical_constituents.py --etf-code 00981A --sync --calendar-days 730
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_sync_window import iter_calendar_chunks
from project_config import parse_etf_codes
from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    load_etf_constituent_universe_gaps,
)
from sync_stock_chip_daily import sync_stock_chip_daily
from sync_stock_market_daily import sync_stock_market_daily


def print_gaps(db_path: Path, etf_codes: tuple[str, ...]) -> list[dict]:
    conn = connect(db_path)
    try:
        gaps = load_etf_constituent_universe_gaps(conn, etf_codes)
    finally:
        conn.close()

    label = ",".join(etf_codes)
    print(f"=== Universe 缺口（曾持有 · 不在最新 snapshot）· {label} ===")
    if not gaps:
        print("  （無缺口）")
        return gaps

    for row in gaps:
        print(
            f"  {row['stock_id']} {row['stock_name']}  "
            f"{row['first_seen']}..{row['last_seen']}  "
            f"ETF數={row['etf_hold_count']}"
        )
    print(f"共 {len(gaps)} 檔")
    return gaps


def run_backfill(
    db_path: Path,
    etf_codes: tuple[str, ...],
    *,
    calendar_days: int,
    chunk_days: int,
    layers: tuple[str, ...],
    quiet: bool,
    request_delay: float,
) -> None:
    gaps = print_gaps(db_path, etf_codes)
    if not gaps:
        return

    stock_ids = [row["stock_id"] for row in gaps]
    end = date.today()
    start = end - timedelta(days=calendar_days)
    chunks = iter_calendar_chunks(start, end, chunk_days)

    if "stock-market" in layers:
        print(f"\n=== K 線 + 法人（{start} ～ {end} · {len(chunks)} chunks）===")
        for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
            if not quiet:
                print(f"--- market chunk {i}/{len(chunks)}: {chunk_start} ～ {chunk_end} ---")
            sync_stock_market_daily(
                db_path,
                window_start=chunk_start,
                window_end=chunk_end,
                stock_ids=stock_ids,
                quiet=quiet,
                request_delay=request_delay,
            )

    if "chip" in layers:
        print(f"\n=== 籌碼（{start} ～ {end} · {len(chunks)} chunks）===")
        for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
            if not quiet:
                print(f"--- chip chunk {i}/{len(chunks)}: {chunk_start} ～ {chunk_end} ---")
            sync_stock_chip_daily(
                db_path,
                window_start=chunk_start,
                window_end=chunk_end,
                stock_ids=stock_ids,
                quiet=quiet,
                request_delay=request_delay,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="補齊歷史成分股 universe 缺口")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--etf-code",
        default="00981A",
        help="單一 ETF 或逗號分隔（預設 00981A）",
    )
    parser.add_argument("--report", action="store_true", help="僅列出缺口")
    parser.add_argument("--sync", action="store_true", help="補齊 K 線／法人／籌碼")
    parser.add_argument("--calendar-days", type=int, default=730)
    parser.add_argument("--chunk-days", type=int, default=90)
    parser.add_argument(
        "--only",
        default="stock-market,chip",
        help="stock-market,chip（逗號分隔）",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--request-delay", type=float, default=0.35)
    args = parser.parse_args()

    etf_codes = parse_etf_codes(args.etf_code)
    layers = tuple(x.strip() for x in args.only.split(",") if x.strip())

    if args.report or not args.sync:
        print_gaps(args.db, etf_codes)
        if not args.sync:
            return 0

    run_backfill(
        args.db,
        etf_codes,
        calendar_days=args.calendar_days,
        chunk_days=args.chunk_days,
        layers=layers,
        quiet=args.quiet,
        request_delay=args.request_delay,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
