#!/usr/bin/env python3
"""Backfill stock_research.daily_briefs from local report MD/HTML files (website payload)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv
from supabase_research_sync import (
    backfill,
    backfill_dates,
    dashboard_url,
    discover_report_dates,
    discover_report_dates_between,
    supabase_configured,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill research briefs to Supabase from on-disk report files"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Look back N calendar days for dated report files (default: 14)",
    )
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument(
        "--signal-hits",
        action="store_true",
        help="Also sync stock_signal_hits per day (slow; daily_sync uses RUN_SUPABASE_SIGNAL_SYNC)",
    )
    args = parser.parse_args(argv)

    load_project_dotenv()
    if not supabase_configured():
        print(
            "Supabase 未設定：請在 .env 加入\n"
            "  VITE_PUBLIC_SUPABASE_URL（或 SUPABASE_URL）\n"
            "  SUPABASE_SERVICE_ROLE_KEY（Python upsert 用；Readdy 讀取用 anon key）",
            file=sys.stderr,
        )
        return 2

    sync_signal_hits = args.signal_hits
    if args.from_date or args.to_date:
        start = args.from_date or args.to_date
        end = args.to_date or args.from_date
        if start is None or end is None or start > end:
            print("error: --from 與 --to 需同時指定且 from ≤ to", file=sys.stderr)
            return 2
        days = discover_report_dates_between(start, end)
        print(
            f"report dates ({start.isoformat()}..{end.isoformat()}): "
            f"{', '.join(d.isoformat() for d in days) or '—'}"
        )
        result = backfill_dates(days, sync_signal_hits=sync_signal_hits)
    else:
        days = discover_report_dates(args.days)
        print(f"report dates ({args.days}d window): {', '.join(d.isoformat() for d in days) or '—'}")
        result = backfill(args.days, sync_signal_hits=sync_signal_hits)
    print(f"uploaded: {len(result.uploaded)}")
    for key in result.uploaded:
        print(f"  + {key}")
    print(f"skipped:  {len(result.skipped)}")
    for key in result.skipped:
        print(f"  - {key}")
    if result.errors:
        for err in result.errors:
            print(f"error:    {err}", file=sys.stderr)
    print(f"dashboard: {dashboard_url()}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
