#!/usr/bin/env python3
"""
Yahoo 研究資料 backfill：長歷史日線、除息拆股、adj close、universe 缺口、RRG 歷史、0050 PIT proxy。

用法：
  python src/backfill_yahoo_research.py --report
  python src/backfill_yahoo_research.py --sync --start 2019-01-01
  python src/backfill_yahoo_research.py --sync --only index-bars,us-bars,tw-gaps
  python src/backfill_yahoo_research.py --sync --only corp-actions,rrg-history,benchmark-pit
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from project_config import (
    BENCHMARK_ETF_WATCHLIST_CODES,
    DEFAULT_ETF_CODES,
)
from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    load_etf_constituent_watchlist,
    seed_benchmark_pit_quarterly_snapshots,
    upsert_daily_bars,
    upsert_stock_corporate_actions,
    upsert_stock_daily_bars,
    upsert_us_daily_bars,
)
from yahoo_chart_sync import (
    DEFAULT_US_RESEARCH_TICKERS,
    DEFAULT_YAHOO_BACKFILL_START,
    YAHOO_INDEX_CODES,
    corporate_action_rows,
    daily_bars_rows_from_yahoo,
    stock_daily_bars_rows_from_yahoo,
    us_daily_bars_rows_from_yahoo,
)

ALL_LAYERS = (
    "index-bars",
    "us-bars",
    "tw-gaps",
    "corp-actions",
    "rrg-history",
    "benchmark-pit",
)


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def _missing_constituent_stock_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT h.stock_id
        FROM etf_holdings h
        LEFT JOIN stock_daily_bars b ON b.stock_id = h.stock_id
        WHERE b.stock_id IS NULL
          AND h.stock_id GLOB '[0-9][0-9][0-9][0-9]'
        ORDER BY h.stock_id
        """
    ).fetchall()
    return [str(r["stock_id"]) for r in rows]


def _existing_us_tickers(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT DISTINCT ticker FROM us_daily_bars").fetchall()
    return {str(r["ticker"]).upper() for r in rows}


def _corp_action_universe(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(symbol_key, yahoo_symbol) for corp-actions sync."""
    items: list[tuple[str, str]] = []
    for code, sym in YAHOO_INDEX_CODES.items():
        items.append((code, sym))
    for ticker in sorted(_existing_us_tickers(conn) | set(DEFAULT_US_RESEARCH_TICKERS)):
        items.append((ticker, ticker))
    seen: set[str] = set()
    for w in load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES):
        sid = w["stock_id"]
        if sid in seen:
            continue
        seen.add(sid)
        items.append((f"TW:{sid}", f"{sid}.TW"))
    return items


def sync_index_bars(
    conn: sqlite3.Connection,
    db_path: Path,
    start: date,
    end: date,
    *,
    quiet: bool,
    delay: float,
) -> int:
    total = 0
    for code, yahoo_sym in YAHOO_INDEX_CODES.items():
        rows = daily_bars_rows_from_yahoo(code, yahoo_sym, start, end)
        if not rows:
            if not quiet:
                print(f"  WARN index-bars {code}: 無資料", file=sys.stderr)
            continue
        n = upsert_daily_bars(conn, rows)
        total += n
        if not quiet:
            print(f"  index-bars {code} ({yahoo_sym}): {n} rows")
        time.sleep(delay)
    if total > 0:
        from sync_tech_risk_context import sync_tech_risk

        history_days = max(90, (end - start).days + 1)
        try:
            sync_tech_risk(
                db_path,
                history_days,
                session_limit=min(1500, history_days),
                quiet=quiet,
            )
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"  WARN tech_risk rebuild: {exc}", file=sys.stderr)
    return total


def sync_us_bars(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    *,
    quiet: bool,
    delay: float,
) -> int:
    tickers = sorted(_existing_us_tickers(conn) | set(DEFAULT_US_RESEARCH_TICKERS))
    total = 0
    for ticker in tickers:
        rows = us_daily_bars_rows_from_yahoo(ticker, start, end)
        if not rows:
            if not quiet:
                print(f"  WARN us-bars {ticker}: 無資料", file=sys.stderr)
            continue
        n = upsert_us_daily_bars(conn, rows)
        total += n
        if not quiet:
            print(f"  us-bars {ticker}: {n} rows")
        time.sleep(delay)
    return total


def sync_tw_gaps(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    *,
    quiet: bool,
    delay: float,
) -> int:
    missing = _missing_constituent_stock_ids(conn)
    if not missing:
        if not quiet:
            print("  tw-gaps: 無缺口")
        return 0
    total = 0
    for stock_id in missing:
        try:
            rows, yahoo_sym = stock_daily_bars_rows_from_yahoo(stock_id, start, end)
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"  WARN tw-gaps {stock_id}: {exc}", file=sys.stderr)
            time.sleep(delay)
            continue
        if not rows:
            if not quiet:
                print(f"  WARN tw-gaps {stock_id}: 無資料", file=sys.stderr)
            time.sleep(delay)
            continue
        n = upsert_stock_daily_bars(conn, rows)
        total += n
        if not quiet:
            print(f"  tw-gaps {stock_id} ({yahoo_sym}): {n} rows")
        time.sleep(delay)
    return total


def sync_corp_actions(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    *,
    quiet: bool,
    delay: float,
) -> int:
    total = 0
    for symbol_key, yahoo_sym in _corp_action_universe(conn):
        rows = corporate_action_rows(symbol_key, yahoo_sym, start, end)
        if rows:
            total += upsert_stock_corporate_actions(conn, rows)
            if not quiet:
                print(f"  corp-actions {symbol_key}: {len(rows)} events")
        time.sleep(delay)
    return total


def sync_rrg_history(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    *,
    quiet: bool,
    min_warmup_days: int = 25,
    max_sessions: int = 0,
) -> int:
    from market_benchmark import load_benchmark_close
    from project_config import DEFAULT_ETF_CODES
    from research.backtest.finpilot_local_backtest import load_price_panels
    from rrg_universe_snapshot import build_universe_rows_from_panels, persist_universe_snapshot

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    dates = [d for d in close.index.astype(str).tolist() if start.isoformat() <= d <= end.isoformat()]
    if len(dates) <= min_warmup_days:
        if not quiet:
            print(f"  rrg-history: 跳過（panel 僅 {len(dates)} 日）")
        return 0

    eligible = dates[min_warmup_days:]
    if max_sessions > 0:
        eligible = eligible[-max_sessions:]

    existing = {
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT session_date FROM rrg_universe_scores
            WHERE screen_kind = 'close'
            """,
        ).fetchall()
    }

    total = 0
    for session_date in eligible:
        if session_date in existing:
            continue
        if session_date not in close.index.astype(str):
            continue
        rows = build_universe_rows_from_panels(
            conn,
            session_date,
            close,
            bench,
            etf_codes=DEFAULT_ETF_CODES,
            data_baseline_date=session_date,
            tick_ok_by_id=None,
        )
        for r in rows:
            r["tick_ok"] = 1
        n = persist_universe_snapshot(
            conn, session_date=session_date, screen_kind="close", rows=rows
        )
        total += n
        if not quiet and (total <= 3 or session_date == eligible[-1] or len(eligible) <= 10):
            print(f"  rrg-history {session_date}: {n} rows")
    return total


def sync_benchmark_pit(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    *,
    quiet: bool,
) -> int:
    total = 0
    for code in BENCHMARK_ETF_WATCHLIST_CODES:
        n = seed_benchmark_pit_quarterly_snapshots(
            conn,
            code,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
        total += n
        if not quiet:
            print(f"  benchmark-pit {code}: {n} constituent rows seeded")
    return total


def print_report(conn: sqlite3.Connection) -> None:
    def one(sql: str) -> sqlite3.Row | None:
        return conn.execute(sql).fetchone()

    print("=== Yahoo research backfill coverage ===")
    for label, sql in (
        ("daily_bars yahoo/index", "SELECT COUNT(*), MIN(date), MAX(date) FROM daily_bars WHERE source='yahoo'"),
        ("stock_daily_bars yfinance", "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM stock_daily_bars WHERE source='yfinance'"),
        ("us_daily_bars", "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM us_daily_bars"),
        ("corporate_actions", "SELECT COUNT(*), MIN(ex_date), MAX(ex_date) FROM stock_corporate_actions"),
        ("rrg_universe close days", "SELECT COUNT(DISTINCT session_date), MIN(session_date), MAX(session_date) FROM rrg_universe_scores WHERE screen_kind='close'"),
        ("benchmark_constituents snaps", "SELECT COUNT(DISTINCT snapshot_date), MIN(snapshot_date), MAX(snapshot_date) FROM benchmark_constituents_meta"),
        ("constituent K-line gaps", f"SELECT COUNT(DISTINCT h.stock_id) FROM etf_holdings h LEFT JOIN stock_daily_bars b ON b.stock_id=h.stock_id WHERE b.stock_id IS NULL"),
    ):
        row = one(sql)
        print(f"- {label}: {tuple(row) if row else ()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Yahoo research data backfill")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--start", default=DEFAULT_YAHOO_BACKFILL_START.isoformat())
    parser.add_argument("--end", default=None)
    parser.add_argument("--only", default=",".join(ALL_LAYERS))
    parser.add_argument("--delay", type=float, default=0.4, help="Yahoo 請求間隔（秒）")
    parser.add_argument(
        "--rrg-max-sessions",
        type=int,
        default=504,
        help="RRG 歷史最多回灌交易日（0=區間內全部）",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not args.sync and not args.report:
        parser.error("specify --sync and/or --report")

    start = _parse_date(args.start)
    end = _parse_date(args.end) if args.end else date.today()
    layers = tuple(x.strip() for x in args.only.split(",") if x.strip())
    bad = set(layers) - set(ALL_LAYERS)
    if bad:
        parser.error(f"unknown layers: {sorted(bad)}")

    conn = connect(args.db)
    try:
        if args.report:
            print_report(conn)
        if args.sync:
            totals: dict[str, int] = {}
            if "index-bars" in layers:
                totals["index-bars"] = sync_index_bars(
                    conn, args.db, start, end, quiet=args.quiet, delay=args.delay
                )
            if "us-bars" in layers:
                totals["us-bars"] = sync_us_bars(
                    conn, start, end, quiet=args.quiet, delay=args.delay
                )
            if "tw-gaps" in layers:
                totals["tw-gaps"] = sync_tw_gaps(
                    conn, start, end, quiet=args.quiet, delay=args.delay
                )
            if "corp-actions" in layers:
                totals["corp-actions"] = sync_corp_actions(
                    conn, start, end, quiet=args.quiet, delay=args.delay
                )
            if "benchmark-pit" in layers:
                totals["benchmark-pit"] = sync_benchmark_pit(
                    conn, start, end, quiet=args.quiet
                )
            if "rrg-history" in layers:
                totals["rrg-history"] = sync_rrg_history(
                    conn,
                    start,
                    end,
                    quiet=args.quiet,
                    max_sessions=args.rrg_max_sessions,
                )
            if not args.quiet:
                print("Done:", totals)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
