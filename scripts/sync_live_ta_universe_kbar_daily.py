#!/usr/bin/env python3
"""Daily incremental sync: stock_kbar_1m for today's actual Live TA universe.

Keeps ``compute_mom1_fade_threshold()`` (src/ops_live_ta.py) fed with a fresh
trailing-window so the 1-2min fade-mom field (raw_mom_1__pct_top5_D10, OOS
72.97%) doesn't silently degrade to "insufficient history" for whichever
stocks are in the live dynamic universe (holdings ∪ watchlist) today — that
universe changes daily and stock_kbar_1m was previously only populated by
ad-hoc research backfills, not kept fresh (see one-off gap-fill:
scripts/backfill_live_ta_universe_kbar.py, and
reports/research/intraday_direction_thermometer/LIVE_TA_FIELD_OPTIMIZATION_20260728.md).

Intended to run once daily after the cash session closes (13:30 TPE), via
launchd (see launchd/com.jackm4.goldenstocks.live-ta-kbar-sync.plist.template). Not an
Order job — read/write is local stock_kbar_1m only.

  PYTHONPATH=src python scripts/sync_live_ta_universe_kbar_daily.py --report-only
  PYTHONPATH=src python scripts/sync_live_ta_universe_kbar_daily.py
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
from ops_live_ta import resolve_live_ta_universe  # noqa: E402
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
    ap.add_argument("--trading-days", type=int, default=15)
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    load_project_dotenv()
    if not finmind_token():
        print("✗ FINMIND_TOKEN 未設定，跳過", flush=True)
        return 1

    _db_path_ref = Path(args.db)
    conn = connect(_db_path_ref)

    universe = resolve_live_ta_universe(conn, include_ops_holdings=True)
    stocks = [sid for sid, _name in universe]
    print(f"Live TA universe today: {stocks}", flush=True)
    if not stocks:
        print("✗ universe empty, nothing to sync", flush=True)
        return 0

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
    if not trade_dates:
        print("✗ no trading-date reference (stock_daily_bars empty for 2330)", flush=True)
        return 1
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
            if err and err != "quota_exceeded_skip":
                n_errors += 1
                print(f"  ! {sid} {td}: {err[:120]}", flush=True)
            elif not err:
                n_inserted += n_bars
        if _quota_exceeded.is_set():
            print("  Stopped early: FinMind quota/rate-limit hit.", flush=True)

    print(
        f"Done. {n_inserted} bars written, {n_errors} errors, "
        f"{completed}/{len(gaps)} attempted, {time.time()-t0:.0f}s total.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
