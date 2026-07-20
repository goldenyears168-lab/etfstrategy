#!/usr/bin/env python3
"""Backfill dated daily brief MD/HTML from stocks.db (historical as-of dates)."""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PY = ROOT / ".venv" / "bin" / "python"
sys.path.insert(0, str(SRC))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def trading_dates(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT date FROM daily_bars
        WHERE code = 'IX0001' AND source = 'tej'
          AND date >= ? AND date <= ?
        ORDER BY date
        """,
        (date_start, date_end),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _run(cmd: list[str], *, quiet: bool) -> None:
    kwargs: dict = {"cwd": ROOT, "env": {**dict(__import__("os").environ), "PYTHONPATH": str(SRC)}}
    if quiet:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    subprocess.run(cmd, check=True, **kwargs)


def backfill_day(day: str, *, quiet: bool) -> None:
    _run([str(PY), str(SRC / "etf_daily_report.py"), "--as-of", day, "--write-reports", "--quiet"], quiet=quiet)
    _run(
        [str(PY), str(SRC / "regime_daily_brief.py"), "--as-of", day, "--write-reports", "--quiet"],
        quiet=quiet,
    )
    _run([str(PY), str(SRC / "vcp_funnel_specs_daily.py"), "--as-of", day], quiet=quiet)
    _run(
        [str(PY), str(SRC / "copytrade_l1h9_daily.py"), "--date", day, "--write-reports", "--quiet"],
        quiet=quiet,
    )
    _run(
        [str(PY), str(SRC / "rrg_mono_daily_brief.py"), "--date", day, "--no-apply"],
        quiet=quiet,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill reports/daily for trading-date range")
    parser.add_argument("--date-start", default="2026-01-01")
    parser.add_argument("--date-end", default="2026-06-07")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        days = trading_dates(conn, date_start=args.date_start, date_end=args.date_end)
    finally:
        conn.close()

    if not days:
        print(f"No IX0001 trading days in {args.date_start}..{args.date_end}", file=sys.stderr)
        return 1

    print(f"backfill {len(days)} days: {days[0]} .. {days[-1]}")
    for i, day in enumerate(days, 1):
        if not args.quiet:
            print(f"  [{i}/{len(days)}] {day}")
        backfill_day(day, quiet=args.quiet)
    print(f"done: {len(days)} days")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
