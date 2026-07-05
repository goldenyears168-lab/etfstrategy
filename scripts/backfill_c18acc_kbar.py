#!/usr/bin/env python3
"""P0 · C18acc mono shortlist 精準 1m kbar backfill（FinMind · quota-aware）。

只補 RRG mono fresh shortlist 上缺 K 的 (trade_date, stock_id)，不掃全 watchlist。

  PYTHONPATH=src python scripts/backfill_c18acc_kbar.py --report-only
  PYTHONPATH=src python scripts/backfill_c18acc_kbar.py
  PYTHONPATH=src python scripts/backfill_c18acc_kbar.py --tier i36 --report-only
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from backfill_rrg_lens_backtest_data import (  # noqa: E402
    _trade_dates_in_range,
    collect_mono_shortlist_kbar_gaps,
)
from finmind_client import fetch_finmind, finmind_token  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar  # noqa: E402
from research.backtest.rrg_mono_intraday_ab import close_shortlist  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from stock_db.kbar import finmind_kbar_rows_to_db, upsert_stock_kbar_1m  # noqa: E402

DEFAULT_START = "2024-01-01"
TIER_MIN_BARS = {"poll": 30, "i36": 200}

_local = threading.local()
_db_path_ref: Path | None = None
_quota_exceeded = threading.Event()


def mono_coverage_summary(
    conn,
    *,
    start: str,
    end: str,
    min_bars: int,
) -> dict[str, int | float]:
    trade_dates = _trade_dates_in_range(conn, start=start, end=end)
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    shortlist_days = 0
    full_coverage_days = 0
    total_pairs = 0
    ok_pairs = 0
    unique_stocks: set[str] = set()

    for trade_date in trade_dates:
        shortlist = close_shortlist(fresh_by_date.get(trade_date, []))
        if not shortlist:
            continue
        shortlist_days += 1
        day_ok = True
        for row in shortlist:
            unique_stocks.add(row.stock_id)
            total_pairs += 1
            n = conn.execute(
                """
                SELECT COUNT(*) AS n FROM stock_kbar_1m
                WHERE stock_id = ? AND trade_date = ? AND source IN ('finmind', 'yahoo')
                """,
                (row.stock_id, trade_date),
            ).fetchone()
            if int(n[0] or 0) >= min_bars:
                ok_pairs += 1
            else:
                day_ok = False
        if day_ok:
            full_coverage_days += 1

    return {
        "trade_days": len(trade_dates),
        "shortlist_days": shortlist_days,
        "full_coverage_days": full_coverage_days,
        "pct_full_coverage_days": round(full_coverage_days / shortlist_days * 100, 1)
        if shortlist_days
        else 0.0,
        "pair_coverage": ok_pairs,
        "pair_total": total_pairs,
        "pct_pair_coverage": round(ok_pairs / total_pairs * 100, 1) if total_pairs else 0.0,
        "unique_shortlist_stocks": len(unique_stocks),
        "min_bars": min_bars,
    }


def _get_local_conn() -> object:
    import sqlite3

    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(str(_db_path_ref))
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


def _fetch_one(args: tuple[str, str]) -> tuple[str, str, int, str | None]:
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
            _quota_exceeded.set()
        return sid, td, 0, err_str


def run_mono_backfill(
    conn,
    gaps: list[tuple[str, str]],
    *,
    workers: int = 4,
    max_calls: int = 4800,
) -> int:
    if not finmind_token():
        raise RuntimeError("FINMIND_TOKEN 未設定，無法拉 TaiwanStockKBar")

    _quota_exceeded.clear()
    work = [(sid, td) for td, sid in gaps]
    total = len(work)
    capped = min(total, max_calls)
    print(f"  Mono gaps: {total:,} stock-days  (capped at {capped:,} this run)", flush=True)
    if total == 0:
        print("  Already complete — nothing to do.", flush=True)
        return 0

    work_this_run = work[:capped]
    print(f"  Workers: {workers}  ETA: ~{capped / workers / 5 / 60:.0f} min", flush=True)
    print(flush=True)

    t0 = time.time()
    n_inserted = 0
    n_errors = 0
    n_skipped = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(_fetch_one, item): item for item in work_this_run}
        for fut in as_completed(futures):
            _sid, _td, n_bars, err = fut.result()
            completed += 1
            if err == "quota_exceeded_skip":
                n_skipped += 1
            elif err:
                n_errors += 1
                if "403" in err:
                    print(f"\n  ⚠ 403 quota hit at call {completed} — stopping cleanly", flush=True)
            else:
                n_inserted += n_bars

            if completed % 50 == 0 or completed == capped:
                elapsed = time.time() - t0
                real_calls = completed - n_skipped
                rate = real_calls / elapsed if elapsed > 0 else 0
                eta = (capped - completed) / (completed / elapsed) if elapsed > 0 else 0
                print(
                    f"  [{completed:>5}/{capped}]  bars={n_inserted:>8,}  "
                    f"err={n_errors}  skip={n_skipped}  rate={rate:.1f}/s  ETA={eta/60:.0f}min",
                    flush=True,
                )

            if _quota_exceeded.is_set() and n_skipped > 20:
                break

    elapsed = time.time() - t0
    real_calls = completed - n_skipped
    print(
        f"\n  Done: {n_inserted:,} bars  {real_calls} API calls  "
        f"{n_errors} errs  {elapsed/60:.1f} min",
        flush=True,
    )
    print(f"  Remaining after this run: {total - real_calls:,} stock-days", flush=True)
    return n_inserted


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="C18acc mono shortlist 1m kbar backfill (P0)")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--tier",
        choices=tuple(TIER_MIN_BARS),
        default="poll",
        help="poll=30 bars (C0 取價) · i36=200 bars (combo_spike)",
    )
    parser.add_argument("--min-bars", type=int, default=None, help="覆寫 tier 門檻")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-calls", type=int, default=4800)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args(argv)

    min_bars = args.min_bars if args.min_bars is not None else TIER_MIN_BARS[args.tier]

    global _db_path_ref
    _db_path_ref = args.db

    conn = connect(args.db)
    try:
        print(f"=== C18acc mono kbar · {args.start}..{args.end} · min_bars={min_bars} ===", flush=True)
        before = mono_coverage_summary(conn, start=args.start, end=args.end, min_bars=min_bars)
        print("--- coverage (before) ---", flush=True)
        for k, v in before.items():
            print(f"  {k}: {v}", flush=True)

        gaps = collect_mono_shortlist_kbar_gaps(
            conn, start=args.start, end=args.end, min_bars=min_bars
        )
        print(f"  gaps (stock-days): {len(gaps)}", flush=True)
        if args.report_only:
            return 0

        if not gaps:
            print("No gaps · skip backfill", flush=True)
            return 0

        run_mono_backfill(conn, gaps, workers=args.workers, max_calls=args.max_calls)

        after = mono_coverage_summary(conn, start=args.start, end=args.end, min_bars=min_bars)
        print("--- coverage (after) ---", flush=True)
        for k, v in after.items():
            print(f"  {k}: {v}", flush=True)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
