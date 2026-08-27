#!/usr/bin/env python3
"""Backfill stock_kbar_1m for the exact (stock, T0) pairs used by
dayflip_short_t0_intraday_pattern_study.py — one-off gap-fill, not scheduled.

Only 76/221 (34%) of dayflip-short's all_trades.csv rows had stock_kbar_1m
coverage for their T0 (signal_date); this fetches the missing pairs from
FinMind's TaiwanStockKBar dataset so the pattern study can run on a larger,
less selection-biased sample.

  PYTHONPATH=src .venv/bin/python scripts/research/backfill_dayflip_t0_kbar.py
  PYTHONPATH=src .venv/bin/python scripts/research/backfill_dayflip_t0_kbar.py --report-only
"""

from __future__ import annotations

import argparse
import csv
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind, finmind_token  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from stock_db.kbar import finmind_kbar_rows_to_db, upsert_stock_kbar_1m  # noqa: E402

TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
MIN_BARS = 50
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


def _missing_pairs(conn, *, date_field: str = "signal_date") -> list[tuple[str, str]]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    pairs = {(r["stock"], r[date_field]) for r in rows}
    missing = []
    for sid, sd in sorted(pairs):
        n = conn.execute(
            "SELECT COUNT(*) FROM stock_kbar_1m WHERE stock_id=? AND trade_date=?", (sid, sd)
        ).fetchone()[0]
        if n < MIN_BARS:
            missing.append((sid, sd))
    return missing


def _fetch_one(args: tuple[str, str]) -> tuple[str, str, int, str | None]:
    if _quota_exceeded.is_set():
        return args[0], args[1], 0, "skipped_quota"
    sid, td = args
    try:
        d = date.fromisoformat(td)
        raw = fetch_finmind("TaiwanStockKBar", sid, d, d)
        rows = finmind_kbar_rows_to_db(sid, raw)
        if rows:
            conn = _get_local_conn()
            upsert_stock_kbar_1m(conn, rows)
        return sid, td, len(rows), None
    except Exception as exc:  # noqa: BLE001
        err_str = str(exc)
        if "403" in err_str or "rate limit" in err_str.lower() or "402" in err_str:
            _quota_exceeded.set()
        return sid, td, 0, err_str


def main() -> int:
    global _db_path_ref
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument(
        "--date-field",
        default="signal_date",
        choices=["signal_date", "trade_date"],
        help="signal_date=T0（訊號日）· trade_date=T0+1（進場/倒貨日）",
    )
    args = ap.parse_args()

    load_project_dotenv()
    if not finmind_token():
        raise RuntimeError("FINMIND_TOKEN 未設定")

    _db_path_ref = Path(args.db)
    conn = connect(_db_path_ref)
    missing = _missing_pairs(conn, date_field=args.date_field)
    print(f"missing (stock, T0) pairs needing backfill: {len(missing)}")
    if args.report_only or not missing:
        return 0

    t0 = time.monotonic()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_fetch_one, pair): pair for pair in missing}
        for i, fut in enumerate(as_completed(futs), 1):
            sid, td, n, err = fut.result()
            if err:
                fail += 1
                if err != "skipped_quota":
                    print(f"  [{i}/{len(missing)}] {sid} {td}: FAIL {err[:120]}")
            else:
                ok += 1
            if _quota_exceeded.is_set() and err == "skipped_quota" and fail == 1:
                print("  quota exceeded — remaining requests skipped")

    elapsed = round(time.monotonic() - t0, 1)
    print(f"done: ok={ok} fail={fail} elapsed={elapsed}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
