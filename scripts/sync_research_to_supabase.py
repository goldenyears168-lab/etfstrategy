#!/usr/bin/env python3
"""Upload 13:00 / 16:30 research briefs to Supabase (好時官網預約 · stock_research)."""

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
    SLOT_BRIEF_TYPES,
    dashboard_url,
    supabase_configured,
    sync_all,
    sync_slot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync research briefs to Supabase")
    parser.add_argument(
        "--slot",
        choices=sorted(SLOT_BRIEF_TYPES),
        help="1300 or 1630; default sync both",
    )
    parser.add_argument("--date", type=date.fromisoformat, help="YYYY-MM-DD override")
    args = parser.parse_args(argv)

    load_project_dotenv()
    if not supabase_configured():
        print(
            "Supabase 未設定：請在 .env 加入\n"
            "  VITE_PUBLIC_SUPABASE_URL（或 SUPABASE_URL）\n"
            "  SUPABASE_SERVICE_ROLE_KEY（Python upsert 用）",
            file=sys.stderr,
        )
        return 2

    result = sync_slot(args.slot, args.date) if args.slot else sync_all(args.date)
    print(f"uploaded: {', '.join(result.uploaded) or '—'}")
    print(f"skipped:  {', '.join(result.skipped) or '—'}")
    if result.errors:
        for err in result.errors:
            print(f"error:    {err}", file=sys.stderr)
    print(f"dashboard: {dashboard_url()}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
