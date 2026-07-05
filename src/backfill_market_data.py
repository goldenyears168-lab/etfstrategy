#!/usr/bin/env python3
"""
歷史市場資料 backfill：補齊 K 線 / 法人 / 籌碼 / screener 回測資料（FinMind + TEJ）。


用法：
  python src/backfill_market_data.py --report
  python src/backfill_market_data.py --sync --calendar-days 730
  python src/backfill_market_data.py --sync --only shareholding,technical --universe tw100 --start-date 2015-01-01
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from market_sync_window import iter_calendar_chunks
from project_config import (
    BENCHMARK_CODES,
    DEFAULT_BACKFILL_CALENDAR_DAYS,
    DEFAULT_BACKFILL_CHUNK_DAYS,
    DEFAULT_ETF_CODES,
)
from query_stock_prices import sync_etf_daily_bars, sync_tej_benchmarks
from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    format_market_data_coverage,
    market_data_coverage_summary,
)
from sync_fundamentals import sync_dividend_history
from sync_futures_institutional_daily import sync_futures_institutional_daily
from sync_stock_chip_daily import sync_stock_chip_daily
from sync_stock_market_daily import sync_stock_market_daily
from sync_stock_market_value_daily import sync_stock_market_value_daily
from sync_stock_shareholding_daily import sync_stock_shareholding_daily
from sync_stock_technical_daily import sync_stock_technical_daily
from universe_config import tw100_config

LEGACY_LAYERS = ("etf-bars", "stock-market", "chip")
SCREENER_LAYERS = (
    "shareholding",
    "dividend",
    "futures-inst",
    "market-value",
    "technical",
)
ALL_LAYERS = LEGACY_LAYERS + SCREENER_LAYERS


def _resolve_window(
    calendar_days: int,
    *,
    end: date | None = None,
    start_date: date | None = None,
) -> tuple[date, date]:
    end_d = end or date.today()
    if start_date is not None:
        return start_date, end_d
    start_d = end_d - timedelta(days=calendar_days)
    return start_d, end_d


def print_coverage(
    db_path: Path,
    calendar_days: int,
    *,
    start_date: date | None = None,
) -> None:
    start, end = _resolve_window(calendar_days, start_date=start_date)
    conn = connect(db_path)
    try:
        summary = market_data_coverage_summary(
            conn,
            window_start=start.isoformat(),
            window_end=end.isoformat(),
        )
    finally:
        conn.close()
    print(format_market_data_coverage(summary))


def backfill_etf_bars(
    db_path: Path,
    calendar_days: int,
    *,
    quiet: bool,
    start_date: date | None = None,
) -> int:
    start, end = _resolve_window(calendar_days, start_date=start_date)
    if not quiet:
        print(f"=== ETF / 指數日線 TEJ（{start} ～ {end}）===")
    bench = sync_tej_benchmarks(
        db_path,
        calendar_days,
        BENCHMARK_CODES,
        quiet=quiet,
    )
    etf = sync_etf_daily_bars(
        DEFAULT_ETF_CODES,
        db_path,
        calendar_days,
        quiet=quiet,
    )
    if not quiet:
        print(f"  指數 upsert={bench} · ETF upsert={etf}")
    return bench + etf


def _universe_passes(universes: tuple[str, ...]) -> list[tuple[str, date, date]]:
    """Return list of (universe, start, end) windows."""
    tw100_start = date.fromisoformat(str(tw100_config().get("research_backfill_start", "2015-01-01")))
    end = date.today()
    etf_start = end - timedelta(days=DEFAULT_BACKFILL_CALENDAR_DAYS)
    passes: list[tuple[str, date, date]] = []
    for u in universes:
        if u == "tw100":
            passes.append(("tw100", tw100_start, end))
        elif u == "etf_watchlist":
            passes.append(("etf_watchlist", etf_start, end))
        elif u == "both":
            passes.append(("tw100", tw100_start, end))
            passes.append(("etf_watchlist", etf_start, end))
        else:
            raise RuntimeError(f"unknown universe: {u}")
    return passes


def backfill_stock_market_chunks(
    db_path: Path,
    calendar_days: int,
    chunk_days: int,
    *,
    quiet: bool,
    max_stocks: int,
    request_delay: float,
    universe: str = "etf_watchlist",
    start_date: date | None = None,
) -> dict[str, int]:
    start, end = _resolve_window(calendar_days, start_date=start_date)
    chunks = iter_calendar_chunks(start, end, chunk_days)
    totals = {"chunks": 0, "ok": 0, "bars": 0, "institutional": 0, "warn": 0, "skipped": 0}
    if not quiet:
        print(
            f"=== 成分股 K 線 + 法人 FinMind（{universe} · {start} ～ {end} · "
            f"{len(chunks)} chunks × {chunk_days}d）==="
        )
    for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
        if not quiet:
            print(f"--- chunk {i}/{len(chunks)}: {chunk_start} ～ {chunk_end} ---")
        stats = sync_stock_market_daily(
            db_path,
            window_start=chunk_start,
            window_end=chunk_end,
            universe=universe,
            quiet=quiet,
            max_stocks=max_stocks,
            request_delay=request_delay,
        )
        totals["chunks"] += 1
        totals["ok"] += stats.get("ok", 0)
        totals["bars"] += stats.get("bars", 0)
        totals["institutional"] += stats.get("institutional", 0)
        totals["warn"] += stats.get("warn", 0)
        totals["skipped"] += stats.get("skipped", 0)
    if not quiet:
        print(
            f"  成分股 sync 完成：chunks={totals['chunks']} "
            f"bars={totals['bars']} inst={totals['institutional']} "
            f"warn={totals['warn']} skipped={totals['skipped']}"
        )
    return totals


def backfill_chip_chunks(
    db_path: Path,
    calendar_days: int,
    chunk_days: int,
    *,
    quiet: bool,
    max_stocks: int,
    request_delay: float,
    start_date: date | None = None,
) -> dict[str, int]:
    start, end = _resolve_window(calendar_days, start_date=start_date)
    chunks = iter_calendar_chunks(start, end, chunk_days)
    totals = {
        "chunks": 0,
        "ok": 0,
        "margin": 0,
        "lending": 0,
        "daytrade": 0,
        "warn": 0,
        "skipped": 0,
    }
    if not quiet:
        print(
            f"=== 籌碼 FinMind（{start} ～ {end} · "
            f"{len(chunks)} chunks × {chunk_days}d）==="
        )
    for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
        if not quiet:
            print(f"--- chunk {i}/{len(chunks)}: {chunk_start} ～ {chunk_end} ---")
        stats = sync_stock_chip_daily(
            db_path,
            window_start=chunk_start,
            window_end=chunk_end,
            quiet=quiet,
            max_stocks=max_stocks,
            request_delay=request_delay,
        )
        totals["chunks"] += 1
        totals["ok"] += stats.get("ok", 0)
        totals["margin"] += stats.get("margin", 0)
        totals["lending"] += stats.get("lending", 0)
        totals["daytrade"] += stats.get("daytrade", 0)
        totals["warn"] += stats.get("warn", 0)
        totals["skipped"] += stats.get("skipped", 0)
    if not quiet:
        print(
            f"  籌碼 sync 完成：chunks={totals['chunks']} "
            f"margin={totals['margin']} lending={totals['lending']} "
            f"daytrade={totals['daytrade']} warn={totals['warn']}"
        )
    return totals


def _backfill_per_stock_chunks(
    label: str,
    sync_fn,
    db_path: Path,
    start: date,
    end: date,
    chunk_days: int,
    *,
    quiet: bool,
    max_stocks: int,
    request_delay: float,
    universe: str,
) -> None:
    chunks = iter_calendar_chunks(start, end, chunk_days)
    if not quiet:
        print(f"=== {label}（{universe} · {start} ～ {end} · {len(chunks)} chunks）===", flush=True)
    for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
        if not quiet:
            print(
                f"--- chunk {i}/{len(chunks)}: {chunk_start} ～ {chunk_end} ---",
                flush=True,
            )
        stats = sync_fn(
            db_path,
            window_start=chunk_start,
            window_end=chunk_end,
            universe=universe,
            quiet=quiet,
            max_stocks=max_stocks,
            request_delay=request_delay,
        )
        if not quiet:
            print(
                f"  chunk {i}/{len(chunks)} done: "
                f"ok={stats.get('ok', 0)}/{stats.get('stocks', '?')} "
                f"rows={stats.get('rows', 0)} skipped={stats.get('skipped', 0)} "
                f"warn={stats.get('warn', 0)}",
                flush=True,
            )
        sys.stdout.flush()
        sys.stderr.flush()


def backfill_screener_layers(
    db_path: Path,
    layers: tuple[str, ...],
    *,
    universes: tuple[str, ...],
    chunk_days: int,
    calendar_days: int,
    start_date: date | None,
    quiet: bool,
    max_stocks: int,
    request_delay: float,
) -> None:
    for uni, win_start, win_end in _universe_passes(universes):
        if start_date is not None and uni == "tw100":
            win_start = start_date
        if "shareholding" in layers:
            _backfill_per_stock_chunks(
                "外資持股 TaiwanStockShareholding",
                sync_stock_shareholding_daily,
                db_path,
                win_start,
                win_end,
                chunk_days,
                quiet=quiet,
                max_stocks=max_stocks,
                request_delay=request_delay,
                universe=uni,
            )
        if "dividend" in layers:
            _backfill_per_stock_chunks(
                "股利 TaiwanStockDividend",
                sync_dividend_history,
                db_path,
                win_start,
                win_end,
                chunk_days,
                quiet=quiet,
                max_stocks=max_stocks,
                request_delay=request_delay,
                universe=uni,
            )
        if "market-value" in layers:
            _backfill_per_stock_chunks(
                "市值 TaiwanStockMarketValue",
                sync_stock_market_value_daily,
                db_path,
                win_start,
                win_end,
                chunk_days,
                quiet=quiet,
                max_stocks=max_stocks,
                request_delay=request_delay,
                universe=uni,
            )
        if "technical" in layers:
            if not quiet:
                print(f"=== 技術指標（{uni} · {win_start} ～ {win_end}）===")
            sync_stock_technical_daily(
                db_path,
                window_start=win_start,
                window_end=win_end,
                universe=uni,
                quiet=quiet,
                max_stocks=max_stocks,
            )

    if "futures-inst" in layers:
        tw_start = start_date or date.fromisoformat(
            str(tw100_config().get("research_backfill_start", "2015-01-01"))
        )
        end = date.today()
        chunks = iter_calendar_chunks(tw_start, end, chunk_days)
        if not quiet:
            print(f"=== 期貨法人 TX（{tw_start} ～ {end} · {len(chunks)} chunks）===")
        for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
            if not quiet:
                print(f"--- chunk {i}/{len(chunks)}: {chunk_start} ～ {chunk_end} ---")
            sync_futures_institutional_daily(
                db_path,
                window_start=chunk_start,
                window_end=chunk_end,
                quiet=quiet,
                request_delay=request_delay,
            )


def run_backfill(
    db_path: Path,
    *,
    calendar_days: int,
    chunk_days: int,
    layers: tuple[str, ...],
    quiet: bool,
    max_stocks: int,
    request_delay: float,
    universe: str = "etf_watchlist",
    start_date: date | None = None,
) -> None:
    if "etf-bars" in layers:
        backfill_etf_bars(db_path, calendar_days, quiet=quiet, start_date=start_date)
    if "stock-market" in layers:
        for uni, _, _ in _universe_passes((universe,)):
            backfill_stock_market_chunks(
                db_path,
                calendar_days,
                chunk_days,
                quiet=quiet,
                max_stocks=max_stocks,
                request_delay=request_delay,
                universe=uni,
                start_date=start_date,
            )
    if "chip" in layers:
        backfill_chip_chunks(
            db_path,
            calendar_days,
            chunk_days,
            quiet=quiet,
            max_stocks=max_stocks,
            request_delay=request_delay,
            start_date=start_date,
        )

    screener_hits = [layer for layer in layers if layer in SCREENER_LAYERS]
    if screener_hits:
        backfill_screener_layers(
            db_path,
            tuple(screener_hits),
            universes=(universe,),
            chunk_days=chunk_days,
            calendar_days=calendar_days,
            start_date=start_date,
            quiet=quiet,
            max_stocks=max_stocks,
            request_delay=request_delay,
        )

    if not quiet:
        print("\n=== Backfill 後覆蓋 ===")
    print_coverage(db_path, calendar_days, start_date=start_date)


def main() -> int:
    parser = argparse.ArgumentParser(description="歷史市場資料 backfill（FinMind + TEJ）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--calendar-days",
        type=int,
        default=DEFAULT_BACKFILL_CALENDAR_DAYS,
        help=f"回溯曆日（預設 {DEFAULT_BACKFILL_CALENDAR_DAYS} ≈ 2 年）",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="YYYY-MM-DD（優先於 calendar-days · TW100 長窗）",
    )
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=DEFAULT_BACKFILL_CHUNK_DAYS,
        help=f"FinMind 分段天數（預設 {DEFAULT_BACKFILL_CHUNK_DAYS}）",
    )
    parser.add_argument(
        "--only",
        default="",
        help=f"只跑指定層，逗號分隔：{','.join(ALL_LAYERS)}",
    )
    parser.add_argument(
        "--universe",
        default="etf_watchlist",
        choices=("etf_watchlist", "tw100", "both"),
        help="成分股 / screener 同步 universe",
    )
    parser.add_argument("--sync", action="store_true", help="執行 backfill 寫入 DB")
    parser.add_argument("--report", action="store_true", help="只印覆蓋摘要")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--max-stocks", type=int, default=0)
    parser.add_argument("--request-delay", type=float, default=0.35)
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start_date) if args.start_date else None

    if args.only:
        layers = tuple(x.strip() for x in args.only.split(",") if x.strip())
        bad = [x for x in layers if x not in ALL_LAYERS]
        if bad:
            print(f"ERROR: 未知 layer {bad}；可用 {ALL_LAYERS}", file=sys.stderr)
            return 2
    else:
        layers = ALL_LAYERS

    if args.report and not args.sync:
        print_coverage(args.db, args.calendar_days, start_date=start_date)
        return 0

    if not args.sync:
        parser.error("請加上 --sync 或 --report")

    if start_date is None and args.calendar_days < 30:
        print("ERROR: calendar-days 至少 30（或使用 --start-date）", file=sys.stderr)
        return 2
    if args.chunk_days < 7 or args.chunk_days > 180:
        print("ERROR: chunk-days 建議 30～90（允許 7～180）", file=sys.stderr)
        return 2

    try:
        run_backfill(
            args.db,
            calendar_days=args.calendar_days,
            chunk_days=args.chunk_days,
            layers=layers,
            quiet=args.quiet,
            max_stocks=args.max_stocks,
            request_delay=args.request_delay,
            universe=args.universe,
            start_date=start_date,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
