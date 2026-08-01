#!/usr/bin/env python3
"""signal_outcomes forward-outcome ledger · daily labeling + backfill runner.

Grades daily RRG (close leading/improving) and VCP (entry_ready) signals with
realized forward return + EXCESS vs IX0001 over H5/H10/H20 and upserts them into
the signal_outcomes table (idempotent — promotes pending_* on re-run). Writes
ONLY signal_outcomes.

Usage:
  # daily (trailing window so pending_* promote as adjusted/price lands):
  PYTHONPATH=src python3 scripts/run_signal_outcomes_daily.py

  # coverage report only (no writes):
  PYTHONPATH=src python3 scripts/run_signal_outcomes_daily.py --report

  # explicit backfill window:
  PYTHONPATH=src python3 scripts/run_signal_outcomes_daily.py --sources rrg \
      --backfill-start 2015-02-09 --backfill-end 2026-07-27
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from build_signal_outcomes import build_outcomes
from stock_db import DEFAULT_DB_PATH, connect, upsert_signal_outcomes

DEFAULT_SOURCES = ("rrg", "vcp")
DAILY_TRAILING_CAL_DAYS = 40


def _latest_signal_date(conn, sources: list[str]) -> str | None:
    dates: list[str] = []
    if "rrg" in sources:
        row = conn.execute(
            "SELECT MAX(data_baseline_date) FROM rrg_universe_scores WHERE screen_kind='close'"
        ).fetchone()
        if row and row[0]:
            dates.append(str(row[0]))
    if "vcp" in sources:
        row = conn.execute(
            "SELECT MAX(as_of_date) FROM vcp_screen_scores_v2 WHERE entry_ready=1"
        ).fetchone()
        if row and row[0]:
            dates.append(str(row[0]))
    return max(dates) if dates else None


def _resolve_window(conn, args, sources: list[str]) -> tuple[str, str]:
    if args.date:
        return args.date, args.date
    end = args.backfill_end or _latest_signal_date(conn, sources)
    if not end:
        raise SystemExit("no signals found for the requested sources")
    if args.backfill_start:
        start = args.backfill_start
    else:
        end_dt = datetime.strptime(end, "%Y-%m-%d").date()
        start = (end_dt - timedelta(days=DAILY_TRAILING_CAL_DAYS)).isoformat()
    return start, end


def _print_breakdown(rows: list[dict]) -> None:
    per = Counter((r["run_id"], r["horizon"], r["status"]) for r in rows)
    print(f"\n  computed {len(rows)} outcome rows")
    print(f"  {'run_id':<24} {'H':>3} {'status':<15} {'count':>7}")
    for (run_id, h, status), n in sorted(per.items()):
        print(f"  {run_id:<24} {h:>3} {status:<15} {n:>7}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="grade a single signal date T (YYYY-MM-DD)")
    ap.add_argument("--backfill-start", help="signal date T lower bound (YYYY-MM-DD)")
    ap.add_argument("--backfill-end", help="signal date T upper bound (YYYY-MM-DD)")
    ap.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                    help="comma list: rrg,vcp (default both)")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path")
    ap.add_argument("--report", action="store_true",
                    help="compute + print status breakdown but DO NOT write")
    args = ap.parse_args()

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    conn = connect(Path(args.db))
    try:
        start, end = _resolve_window(conn, args, sources)
        print(f"signal_outcomes: sources={sources} window T in [{start} .. {end}]  db={args.db}")
        rows = build_outcomes(conn, sources, start, end)
        _print_breakdown(rows)
        if args.report:
            print("\n  --report: no rows written.")
            return 0
        n = upsert_signal_outcomes(conn, rows)
        print(f"\n  upserted {n} rows into signal_outcomes.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
