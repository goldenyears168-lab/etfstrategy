#!/usr/bin/env python3
"""Backfill stock_research.daily_briefs from local report MD/HTML files (website payload)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv
from supabase_research_sync import (
    backfill,
    dashboard_url,
    discover_report_dates,
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

    days = discover_report_dates(args.days)
    print(f"report dates ({args.days}d window): {', '.join(d.isoformat() for d in days) or '—'}")

    result = backfill(args.days)
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
