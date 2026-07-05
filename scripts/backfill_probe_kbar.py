#!/usr/bin/env python3
"""Backfill 1m kbar for market-probe-radar · ETF constituent watchlist。

  PYTHONPATH=src .venv/bin/python scripts/backfill_probe_kbar.py --start 2025-01-01
  PYTHONPATH=src .venv/bin/python scripts/backfill_probe_kbar.py --recent-days 35
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from backfill_rrg_lens_backtest_data import (  # noqa: E402
    _parse_date,
    _trade_dates_in_range,
    backfill_stock_kbar_finmind,
    backfill_stock_kbar_yahoo,
)
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect, load_etf_constituent_watchlist  # noqa: E402

DEFAULT_START = "2025-01-01"
MIN_BARS = 30


def collect_probe_kbar_gaps(
    conn,
    *,
    stock_ids: list[str],
    start: str,
    end: str,
    min_bars: int = MIN_BARS,
) -> list[tuple[str, str]]:
    """(trade_date, stock_id) 缺 1m 或 bar 數不足。"""
    if not stock_ids:
        return []
    dates = _trade_dates_in_range(conn, start=start, end=end)
    if not dates:
        return []
    placeholders = ",".join("?" * len(stock_ids))
    rows = conn.execute(
        f"""
        SELECT trade_date, stock_id, COUNT(*) AS n
        FROM stock_kbar_1m
        WHERE trade_date BETWEEN ? AND ?
          AND stock_id IN ({placeholders})
          AND source IN ('finmind', 'yahoo')
        GROUP BY trade_date, stock_id
        """,
        (start, end, *stock_ids),
    ).fetchall()
    good = {
        (str(r["trade_date"]), str(r["stock_id"]))
        for r in rows
        if int(r["n"] or 0) >= min_bars
    }
    return [(d, sid) for d in dates for sid in stock_ids if (d, sid) not in good]


def probe_coverage_summary(
    conn,
    *,
    stock_ids: list[str],
    start: str,
    end: str,
    min_bars: int = MIN_BARS,
    min_stocks_per_day: int = 50,
) -> dict[str, int | float]:
    dates = _trade_dates_in_range(conn, start=start, end=end)
    placeholders = ",".join("?" * len(stock_ids))
    good_days = 0
    for d in dates:
        row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT stock_id) AS n
            FROM stock_kbar_1m
            WHERE trade_date = ? AND stock_id IN ({placeholders})
              AND source IN ('finmind', 'yahoo')
            GROUP BY trade_date
            HAVING COUNT(*) >= ?
            """,
            (d, *stock_ids, min_bars),
        ).fetchone()
        # count stocks meeting min bars per day
        row2 = conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM (
              SELECT stock_id FROM stock_kbar_1m
              WHERE trade_date = ? AND stock_id IN ({placeholders})
                AND source IN ('finmind', 'yahoo')
              GROUP BY stock_id HAVING COUNT(*) >= ?
            )
            """,
            (d, *stock_ids, min_bars),
        ).fetchone()
        n = int(row2[0] or 0) if row2 else 0
        if n >= min_stocks_per_day:
            good_days += 1
    return {
        "trade_days": len(dates),
        "days_ge_min_stocks": good_days,
        "pct_days_ge_min_stocks": round(good_days / len(dates) * 100, 1) if dates else 0,
        "watchlist_size": len(stock_ids),
        "min_bars": min_bars,
        "min_stocks_per_day": min_stocks_per_day,
    }


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="Backfill 1m kbar for probe radar universe")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--recent-days", type=int, default=None, help="override start = today-N+1")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--provider", choices=("finmind", "yahoo", "auto"), default="auto")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--min-bars", type=int, default=MIN_BARS)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    start_s = args.start
    end_s = args.end
    if args.recent_days:
        start_s = (date.today() - timedelta(days=args.recent_days - 1)).isoformat()

    conn = connect(args.db)
    try:
        stock_ids = [str(w["stock_id"]) for w in load_etf_constituent_watchlist(conn)]
        before = probe_coverage_summary(
            conn, stock_ids=stock_ids, start=start_s, end=end_s, min_bars=args.min_bars
        )
        print("=== probe kbar coverage (before) ===", flush=True)
        for k, v in before.items():
            print(f"  {k}: {v}")

        gaps = collect_probe_kbar_gaps(
            conn,
            stock_ids=stock_ids,
            start=start_s,
            end=end_s,
            min_bars=args.min_bars,
        )
        print(f"  gaps (stock-days): {len(gaps)}", flush=True)
        if args.report_only:
            return 0

        if not gaps and not args.force:
            print("No gaps · skip backfill")
            return 0

        start_d = _parse_date(start_s)
        end_d = _parse_date(end_s)
        provider = args.provider
        total = 0
        if provider in ("finmind", "auto"):
            try:
                total += backfill_stock_kbar_finmind(
                    conn,
                    start=start_d,
                    end=end_d,
                    stock_ids=stock_ids,
                    force=args.force,
                    delay=args.delay,
                    quiet=args.quiet,
                    pairs=gaps if not args.force else None,
                )
            except RuntimeError as exc:
                print(f"  finmind skip: {exc}")
                if provider == "finmind":
                    raise
        if provider in ("yahoo", "auto"):
            total += backfill_stock_kbar_yahoo(
                conn,
                start=start_d,
                end=end_d,
                stock_ids=stock_ids,
                force=args.force,
                delay=max(0.35, args.delay),
                quiet=args.quiet,
                kbar_1m_only=True,
                recent_days=max(30, (end_d - start_d).days + 1),
            )

        after = probe_coverage_summary(
            conn, stock_ids=stock_ids, start=start_s, end=end_s, min_bars=args.min_bars
        )
        print(f"  bars_upserted={total}")
        print("=== probe kbar coverage (after) ===")
        for k, v in after.items():
            print(f"  {k}: {v}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
