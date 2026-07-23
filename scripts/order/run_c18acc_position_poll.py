#!/usr/bin/env python3
"""C18acc position poll · underwater_rebound pyramid add (Order layer O3-7).

  PYTHONPATH=src python3 scripts/order/run_c18acc_position_poll.py \
    --date 2026-07-09 --time 10:30

Default dry-run. Enable:
  C18ACC_PYRAMID_ADD_ENABLED=1 ...
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv
from rrg_mono_swap_accel_screen import load_slot_state
from stock_db import DEFAULT_DB_PATH, connect

_TZ = ZoneInfo("Asia/Taipei")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="C18acc position monitor poll (pyramid add)")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="Session date (default: today)")
    p.add_argument("--time", metavar="HH:MM", help="Poll clock (default: now)")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    return p.parse_args()


def _resolve_now(date: str | None, time: str | None) -> tuple[str, str]:
    if date and time:
        return date, time
    now = datetime.now(tz=_TZ)
    return (date or now.strftime("%Y-%m-%d"), time or now.strftime("%H:%M"))


def main() -> int:
    load_project_dotenv()
    args = _parse_args()
    session_date, poll_minute = _resolve_now(args.date, args.time)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    from order.c18acc_position_monitor import process_c18acc_position_monitor

    state = load_slot_state()
    conn = connect(args.db)
    try:
        payload = process_c18acc_position_monitor(
            state=state,
            session_date=session_date,
            poll_minute=poll_minute,
            conn=conn,
            dry_run=True,
        )
    finally:
        conn.close()

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(
        f"\nopen={payload.get('open_n')} hits={payload.get('hits_n')} "
        f"skipped={payload.get('skipped_reason') or '—'}"
    )
    for act in payload.get("actions") or []:
        print(
            f"  [{act.get('kind')}] slot={act.get('slot')} {act.get('symbol')} · "
            f"{act.get('reason')} · ret={act.get('ret_from_entry_pct')}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
