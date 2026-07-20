#!/usr/bin/env python3
"""Move stray research HTML into category subdirs and restore redirect stubs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import (  # noqa: E402
    organize_research_html,
    write_research_html_redirects,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Organize research HTML under reports/research/{breadth,rrg,...}/"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned moves/redirects without writing",
    )
    parser.add_argument(
        "--redirects-only",
        action="store_true",
        help="Only write redirect stubs at reports/research/ root",
    )
    args = parser.parse_args()

    if not args.redirects_only:
        moves = organize_research_html(dry_run=args.dry_run)
        if moves:
            print(f"{'Would move' if args.dry_run else 'Moved'} {len(moves)} file(s):")
            for src, dest in moves:
                print(f"  {src.relative_to(ROOT)} → {dest.relative_to(ROOT)}")
        else:
            print("No stray HTML to move.")

    redirects = write_research_html_redirects(dry_run=args.dry_run)
    if redirects:
        print(f"{'Would write' if args.dry_run else 'Wrote'} {len(redirects)} symlink alias(es):")
        for p in redirects:
            print(f"  {p.relative_to(ROOT)}")
    else:
        print("Symlink aliases up to date.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
