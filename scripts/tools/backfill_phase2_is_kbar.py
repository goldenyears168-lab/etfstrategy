#!/usr/bin/env python3
"""Backfill 1m kbar for Phase 2 spike+heat universe — IS period coverage.

Targets the 78-stock universe from cz_intraday_common._load_stock_ids()
for the IS period (2024-01-01 to 2025-12-14).

Usage:
  python scripts/tools/backfill_phase2_is_kbar.py              # 2025 only (~75 min)
  python scripts/tools/backfill_phase2_is_kbar.py --year 2024  # 2024 only (~75 min)
  python scripts/tools/backfill_phase2_is_kbar.py --year all   # full 2024+2025 (~150 min)
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv  # noqa: E402

load_project_dotenv()

from finmind_client import fetch_finmind, finmind_token  # noqa: E402
from research.backtest.archive.cz_intraday_common import _load_stock_ids  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from stock_db.kbar import finmind_kbar_rows_to_db, kbar_day_has_data, upsert_stock_kbar_1m  # noqa: E402

IS_YEAR_RANGES = {
    "2025": ("2025-01-01", "2025-12-14"),
    "2024": ("2024-01-01", "2024-12-31"),
    "all": ("2024-01-01", "2025-12-14"),
}

# Thread-local DB connections for concurrent access
_local = threading.local()
_db_path_ref: Path | None = None


def _get_local_conn() -> object:
    """Return thread-local SQLite connection."""
    import sqlite3
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(str(_db_path_ref))
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


_quota_exceeded = threading.Event()  # set when 403 is received → stop all workers


def _fetch_one(args: tuple[str, str]) -> tuple[str, str, int, str | None]:
    """Fetch one (stock_id, trade_date) pair. Returns (sid, td, n_bars, err)."""
    if _quota_exceeded.is_set():
        return args[0], args[1], 0, "quota_exceeded_skip"
    sid, td = args
    d = date.fromisoformat(td)
    try:
        raw = fetch_finmind("TaiwanStockKBar", sid, d, d)
        rows = finmind_kbar_rows_to_db(sid, raw)
        if rows:
            conn = _get_local_conn()
            upsert_stock_kbar_1m(conn, rows)
        return sid, td, len(rows), None
    except Exception as exc:
        err_str = str(exc)
        if "403" in err_str:
            _quota_exceeded.set()  # signal all workers to stop
        return sid, td, 0, err_str


def _trade_dates_in_range(conn, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_daily_bars
        WHERE source='finmind' AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (start, end),
    ).fetchall()
    return [r["trade_date"] for r in rows]


def run_backfill(
    conn,
    stock_ids: list[str],
    trade_dates: list[str],
    *,
    workers: int = 4,
    force: bool = False,
    max_calls: int = 4800,  # daily quota safety cap (FinMind free ≈ 5000/day)
) -> int:
    if not finmind_token():
        raise RuntimeError("FINMIND_TOKEN not set")

    _quota_exceeded.clear()

    # Build work list (skip already-complete days)
    work: list[tuple[str, str]] = []
    for sid in stock_ids:
        for td in trade_dates:
            if force or not kbar_day_has_data(conn, sid, td):
                work.append((sid, td))

    total = len(work)
    capped = min(total, max_calls)
    print(f"  Gaps to fill: {total:,} stock-days  (capped at {capped:,} this run)")
    if total == 0:
        print("  Already complete — nothing to do.")
        return 0

    work_this_run = work[:capped]
    print(f"  Workers: {workers}  Stocks: {len(stock_ids)}  Dates: {len(trade_dates)}")
    print(f"  Range: {trade_dates[0]} → {trade_dates[-1]}")
    print(f"  Estimated time: ~{capped / workers / 5 / 60:.0f} min", flush=True)
    print()

    t0 = time.time()
    n_inserted = 0
    n_errors = 0
    n_skipped = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(_fetch_one, item): item for item in work_this_run}
        for fut in as_completed(futures):
            sid, td, n_bars, err = fut.result()
            completed += 1
            if err == "quota_exceeded_skip":
                n_skipped += 1
            elif err:
                n_errors += 1
                if "403" in err:
                    print(f"\n  ⚠ 403 quota hit at call {completed} — stopping cleanly", flush=True)
            else:
                n_inserted += n_bars

            if completed % 200 == 0 or completed == capped:
                elapsed = time.time() - t0
                real_calls = completed - n_skipped
                rate = real_calls / elapsed if elapsed > 0 else 0
                eta = (capped - completed) / (completed / elapsed) if elapsed > 0 else 0
                print(
                    f"  [{completed:>6}/{capped}]  bars={n_inserted:>9,}  "
                    f"err={n_errors}  skip={n_skipped}  rate={rate:.1f}/s  ETA={eta/60:.0f}min",
                    flush=True,
                )

            if _quota_exceeded.is_set() and n_skipped > 50:
                break  # enough workers drained

    elapsed = time.time() - t0
    real_calls = completed - n_skipped
    print(f"\n  Done: {n_inserted:,} bars  {real_calls} API calls  {n_errors} errs  {elapsed/60:.1f} min")
    print(f"  Remaining after this run: {total - real_calls:,} stock-days")
    return n_inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Phase 2 IS 1m kbar")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--year", default="2025", choices=["2024", "2025", "all"])
    parser.add_argument("--workers", type=int, default=4, help="Concurrent API workers")
    parser.add_argument("--max-calls", type=int, default=4800, help="Max API calls (daily quota cap)")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if data exists")
    args = parser.parse_args()

    global _db_path_ref
    _db_path_ref = args.db

    conn = connect(args.db)
    conn.row_factory = __import__("sqlite3").Row

    stock_ids = _load_stock_ids(conn)
    print(f"Phase 2 universe: {len(stock_ids)} stocks")

    date_range = IS_YEAR_RANGES[args.year]
    trade_dates = _trade_dates_in_range(conn, date_range[0], date_range[1])
    print(f"IS range ({args.year}): {len(trade_dates)} trading days  [{date_range[0]}~{date_range[1]}]")

    run_backfill(conn, stock_ids, trade_dates, workers=args.workers,
                force=args.force, max_calls=args.max_calls)


if __name__ == "__main__":
    main()
