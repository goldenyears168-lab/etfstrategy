#!/usr/bin/env python3
"""Buy signal radar · C0 entry advisory · no slot simulation."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv
from stock_db import DEFAULT_DB_PATH, connect
from strategy_signal_radar import print_buy_radar_log, run_buy_signal_radar, write_radar_report

_TZ = ZoneInfo("Asia/Taipei")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Buy signal radar · C0 advisory")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="Session date (default: today)")
    p.add_argument("--time", metavar="HH:MM", help="Poll clock time (default: now)")
    p.add_argument("--no-dedup", action="store_true", help="Do not mark dedup / state writes")
    return p.parse_args()


def _resolve_now(date: str | None, time: str | None) -> datetime | None:
    if not date and not time:
        return None
    if date and not time:
        time = "09:00"
    if time and not date:
        from rrg_mono_intraday_watch import _session_date

        date = _session_date()
    hh, mm = time.split(":", 1)
    return datetime(int(date[:4]), int(date[5:7]), int(date[8:10]), int(hh), int(mm), tzinfo=_TZ)


def main() -> int:
    load_project_dotenv()
    args = _parse_args()
    now = _resolve_now(args.date, args.time)
    session_date = args.date or (now.strftime("%Y-%m-%d") if now else None)

    conn = connect(DEFAULT_DB_PATH)
    try:
        result = run_buy_signal_radar(
            conn,
            session_date=session_date,
            now=now,
            mark_dedup=not args.no_dedup,
        )
    finally:
        conn.close()

    if result.new_signals:
        path = write_radar_report(
            "buy", result.new_signals, session_date=result.session_date, poll_minute=result.poll_minute
        )
        print(f"報告：{path}")

    # ABC Order retired 2026-07-15 · radar is observe-only (no Order submission path).
    result.abc_v3_f1_orders = []

    print_buy_radar_log(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
