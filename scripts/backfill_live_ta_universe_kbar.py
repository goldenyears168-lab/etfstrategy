#!/usr/bin/env python3
"""Backfill stock_kbar_1m for the current Live TA dynamic universe (FinMind).

One-off gap-fill so ``compute_mom1_fade_threshold()`` in ``src/ops_live_ta.py``
has a fresh trailing-10-trading-day window for the stocks the website is
actually displaying today, instead of degrading to "insufficient history".
Not a scheduled job — see reports/research/intraday_direction_thermometer/
LIVE_TA_FIELD_OPTIMIZATION_20260728.md for why a real daily sync is still
needed for this to stay fresh going forward.

  PYTHONPATH=src python scripts/backfill_live_ta_universe_kbar.py --report-only
  PYTHONPATH=src python scripts/backfill_live_ta_universe_kbar.py
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

from finmind_client import fetch_finmind, finmind_token  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from stock_db.kbar import finmind_kbar_rows_to_db, upsert_stock_kbar_1m  # noqa: E402

MIN_BARS = 100  # a full TW session has ~265 1m bars; treat <100 as a gap
_local = threading.local()
_db_path_ref: Path | None = None
_quota_exceeded = threading.Event()


def _get_local_conn():
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
    except Exception as exc:  # noqa: BLE001
        err_str = str(exc)
        if "403" in err_str or "Rate limit" in err_str.lower():
            _quota_exceeded.set()
        return sid, td, 0, err_str


def main() -> int:
    global _db_path_ref
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stocks", default="2327,2492,3189,3653,6147,6505,8046")
    ap.add_argument("--trading-days", type=int, default=15)
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    load_project_dotenv()
    if not finmind_token():
        raise RuntimeError("FINMIND_TOKEN 未設定")

    _db_path_ref = Path(args.db)
    conn = connect(_db_path_ref)
    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]

    trade_dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT trade_date FROM stock_daily_bars "
            "WHERE stock_id='2330' AND trade_date <= (SELECT MAX(trade_date) FROM stock_daily_bars) "
            "ORDER BY trade_date DESC LIMIT ?",
            (args.trading_days,),
        ).fetchall()
    ]
    trade_dates.sort()
    print(f"Trailing {len(trade_dates)} trading days: {trade_dates[0]}..{trade_dates[-1]}", flush=True)

    gaps: list[tuple[str, str]] = []
    for sid in stocks:
        for td in trade_dates:
            n = conn.execute(
                "SELECT COUNT(*) FROM stock_kbar_1m WHERE stock_id=? AND trade_date=?",
                (sid, td),
            ).fetchone()[0]
            if n < MIN_BARS:
                gaps.append((sid, td))

    print(f"Gaps: {len(gaps)} stock-days across {len(stocks)} stocks", flush=True)
    for sid in stocks:
        n_gap = sum(1 for g in gaps if g[0] == sid)
        print(f"  {sid}: {n_gap} missing days", flush=True)

    if args.report_only or not gaps:
        return 0

    t0 = time.time()
    n_inserted = 0
    n_errors = 0
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as exe:
        futures = {exe.submit(_fetch_one, g): g for g in gaps}
        for fut in as_completed(futures):
            sid, td, n_bars, err = fut.result()
            completed += 1
            if err == "quota_exceeded_skip":
                pass
            elif err:
                n_errors += 1
                print(f"  ! {sid} {td}: {err[:120]}", flush=True)
            else:
                n_inserted += n_bars
            if completed % 20 == 0 or completed == len(gaps):
                print(
                    f"  {completed}/{len(gaps)} done · {n_inserted} bars written · "
                    f"{n_errors} errors · {time.time()-t0:.0f}s",
                    flush=True,
                )
        if _quota_exceeded.is_set():
            print("\n  Stopped early: FinMind quota/rate-limit hit.", flush=True)

    print(f"\nDone. {n_inserted} bars written, {n_errors} errors, {time.time()-t0:.0f}s total.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
