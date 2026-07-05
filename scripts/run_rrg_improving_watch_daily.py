#!/usr/bin/env python3
"""RRG Improving lifecycle watch · daily close brief.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_improving_watch_daily.py
  PYTHONPATH=src .venv/bin/python scripts/run_rrg_improving_watch_daily.py --date 2026-06-26
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv
from rrg_improving_watch import run_improving_watch_daily
from stock_db import DEFAULT_DB_PATH, connect


def main() -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="RRG Improving watch daily brief")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Signal day (default: latest bar)")
    args = parser.parse_args()

    conn = connect(DEFAULT_DB_PATH)
    try:
        result = run_improving_watch_daily(conn, session=args.date)
    finally:
        conn.close()

    print(result["markdown"])
    print(f"\nWrote {result['report_dated']}")
    print(f"Wrote {result['report_latest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
