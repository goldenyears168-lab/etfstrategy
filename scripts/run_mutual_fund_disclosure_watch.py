#!/usr/bin/env python3
"""Daily probe: ACDD04 monthly disclosure published? Email only when new."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mutual_fund_disclosure_watch import watch_fund
from project_dotenv import load_project_dotenv
from stock_db import DEFAULT_DB_PATH
from sync_mutual_fund_holdings import ALLIANZ_TW_TECH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe mutual fund monthly disclosure; email when a new snapshot appears",
    )
    parser.add_argument("--fund", default=ALLIANZ_TW_TECH.fund_code)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--dry-run", action="store_true", help="Probe only; do not sync or email")
    args = parser.parse_args(argv)

    load_project_dotenv()

    result = watch_fund(
        args.fund,
        db_path=args.db,
        sync_on_new=not args.dry_run,
        notify=not args.dry_run,
    )

    if result.status == "error":
        print(f"ERROR: {result.error}", file=sys.stderr)
        if result.db_latest:
            print(f"DB latest: {result.db_latest}")
        return 1

    if result.status == "new":
        print(
            f"NEW {result.fund_code} {result.remote_latest} "
            f"(was {result.db_latest or 'none'}) written={result.holdings_written}"
        )
        return 0

    print(
        f"unchanged {result.fund_code}: remote={result.remote_latest} db={result.db_latest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
