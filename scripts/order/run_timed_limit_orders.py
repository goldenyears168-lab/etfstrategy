#!/usr/bin/env python3
"""Timed limit orders from config/order.yaml · timed_limit_orders.

  PYTHONPATH=src .venv-fubon/bin/python scripts/order/run_timed_limit_orders.py
  ORDER_TIMED_LIMIT_DRY_RUN=0 ORDER_TIMED_LIMIT_IGNORE_CLOCK=1 \\
    PYTHONPATH=src .venv-fubon/bin/python scripts/order/run_timed_limit_orders.py --date 2026-07-23
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

_TZ = ZoneInfo("Asia/Taipei")


def main() -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--force-dry-run", action="store_true")
    ap.add_argument(
        "--ignore-clock",
        action="store_true",
        help="Run due jobs even if wall clock ≠ configured time",
    )
    args = ap.parse_args()
    if args.ignore_clock:
        import os

        os.environ["ORDER_TIMED_LIMIT_IGNORE_CLOCK"] = "1"
    day = args.date or datetime.now(tz=_TZ).strftime("%Y-%m-%d")

    from order.timed_limit_order import run_due_timed_jobs, run_timed_limit_job, load_timed_jobs

    if args.force_dry_run:
        out = {"session_date": day, "results": []}
        for job in load_timed_jobs():
            out["results"].append(
                run_timed_limit_job(job, session_date=day, dry_run=True)
            )
    else:
        out = run_due_timed_jobs(session_date=day)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print("TIMED_LIMIT_ORDERS=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
