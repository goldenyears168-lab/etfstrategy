#!/usr/bin/env python3
"""Backfill historical OR+ext≥3% watch events from C18acc leg history.

Research topic: c18acc-extension-radar (config/research.yaml)
Verdict source: reports/research/intraday/cz_track5_or_fade_verdict_2026.json
  O3 ≡ I36 · 81 watch hits · 13 precede combo · does NOT trigger sell.

Output:
  reports/research/rrg/c18acc_or_ext_watch_log.csv

Usage:
  PYTHONPATH=src python scripts/run_c18acc_or_ext_watch_backfill.py
  PYTHONPATH=src python scripts/run_c18acc_or_ext_watch_backfill.py \\
    --date-start 2024-01-01 --date-end 2026-06-26
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.c18acc_intraday_1m_hold import _load_c18acc_intraday_context  # noqa: E402
from research.backtest.c18acc_extension_exit import _ma20, _prior_close  # noqa: E402
from research.backtest.rrg_mono_score_swap_c import _trading_days_between  # noqa: E402
from intraday_extension_radar import ExtensionScanConfig, scan_session, pick_exit_for_mode  # noqa: E402
from c18acc_extension_overlay import (  # noqa: E402
    OR_EXT_WATCH_CSV_PATH,
    DEFAULT_OR_WATCH_MIN_EXT_PCT,
    OR_WATCH_OPENING_END,
    OrExtWatchEvent,
    _detect_or_ext_watch,
    append_or_ext_watch_csv,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from stock_db.kbar import kbar_day_has_data, load_kbar_day_bars  # noqa: E402

DEFAULT_DATE_START = "2024-01-01"
DEFAULT_DATE_END = "2026-06-26"
DEFAULT_MIN_HOLD_DAYS = 5

_COMBO_CFG = ExtensionScanConfig(
    exit_mode="combo_spike",
    poll_interval_min=1,
    min_ext_prev_pct=4.0,
    min_fade_from_high_pct=1.0,
    require_opening_spike=False,
)


def _hold_dates(full_dates: list[str], entry: str, exit_date: str) -> list[str]:
    return [d for d in full_dates if entry < d <= exit_date]


def run_backfill(
    conn,
    *,
    date_start: str = DEFAULT_DATE_START,
    date_end: str = DEFAULT_DATE_END,
    min_hold_days: int = DEFAULT_MIN_HOLD_DAYS,
    min_or_ext_pct: float = DEFAULT_OR_WATCH_MIN_EXT_PCT,
    out_path: Path | None = None,
    overwrite: bool = False,
) -> list[OrExtWatchEvent]:
    out_path = out_path or OR_EXT_WATCH_CSV_PATH

    print(f"Loading C18acc base periods {date_start} … {date_end}")
    base_periods, _close, full_dates, i0_summary = _load_c18acc_intraday_context(
        conn, date_start=date_start, date_end=date_end
    )
    print(f"  {len(base_periods)} legs · {i0_summary.get('mean_excess_pct', '?')}% mean_excess")

    events: list[OrExtWatchEvent] = []
    kbar_cache: dict = {}

    for pos in base_periods:
        sid = str(pos["stock_id"])
        entry = str(pos["entry_date"])
        orig_exit = str(pos["exit_date"])
        stock_name = str(pos.get("stock_name") or "")

        for d in _hold_dates(full_dates, entry, orig_exit):
            if _trading_days_between(full_dates, entry, d) < min_hold_days:
                continue
            key = (sid, d)
            if key not in kbar_cache:
                if not kbar_day_has_data(conn, sid, d):
                    kbar_cache[key] = ()
                else:
                    kbar_cache[key] = load_kbar_day_bars(conn, sid, d)
            bars = kbar_cache[key]
            if not bars:
                continue
            prev_close = _prior_close(conn, sid, d, full_dates)
            if prev_close is None:
                continue
            ma20 = _ma20(conn, sid, d, full_dates)

            scan = scan_session(
                bars,
                trade_date=d,
                stock_id=sid,
                prev_close=prev_close,
                ma20=ma20,
                config=_COMBO_CFG,
            )
            or_hit = _detect_or_ext_watch(scan.bars, min_ext_pct=min_or_ext_pct)
            if or_hit is None:
                continue

            i36_sig = pick_exit_for_mode(scan, "combo_spike")
            events.append(
                OrExtWatchEvent(
                    stock_id=sid,
                    stock_name=stock_name,
                    session=d,
                    or_minute=or_hit[0],
                    or_ext_pct=or_hit[1],
                    i36_triggered=i36_sig is not None,
                    notes=f"entry={entry} orig_exit={orig_exit}",
                )
            )

    print(f"  OR+ext≥{min_or_ext_pct}% watch hits: {len(events)}")
    i36_count = sum(1 for e in events if e.i36_triggered)
    print(f"  I36 also fired: {i36_count} / {len(events)}")

    if overwrite and out_path.exists():
        out_path.unlink()
    append_or_ext_watch_csv(events, csv_path=out_path)
    print(f"  Written → {out_path}")
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill OR+ext watch log from C18acc leg history")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default=DEFAULT_DATE_START)
    parser.add_argument("--date-end", default=DEFAULT_DATE_END)
    parser.add_argument("--min-hold-days", type=int, default=DEFAULT_MIN_HOLD_DAYS)
    parser.add_argument("--min-or-ext-pct", type=float, default=DEFAULT_OR_WATCH_MIN_EXT_PCT)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Remove existing CSV before writing (default: append)",
    )
    parser.add_argument("--out", type=Path, default=OR_EXT_WATCH_CSV_PATH)
    args = parser.parse_args()

    with connect(args.db) as conn:
        run_backfill(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            min_hold_days=args.min_hold_days,
            min_or_ext_pct=args.min_or_ext_pct,
            out_path=args.out,
            overwrite=args.overwrite,
        )


if __name__ == "__main__":
    main()
