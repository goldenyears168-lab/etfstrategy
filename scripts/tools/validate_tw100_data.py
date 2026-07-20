#!/usr/bin/env python3
"""TW100 data quality gate · run before Alpha158 / walk-forward."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from research.tw100_data_quality import audit_tw100_data_quality  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from universe_config import tw100_config  # noqa: E402


def main() -> int:
    cfg = tw100_config()
    p = argparse.ArgumentParser(description="TW100 research data quality audit")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--snapshot-date", default=cfg["research_snapshot_date"])
    p.add_argument("--start", default=cfg["research_backfill_start"])
    p.add_argument("--end", default=cfg["research_snapshot_date"])
    p.add_argument("--min-bars", type=int, default=611, help="WFA fold minimum (504+100+7)")
    p.add_argument("--json-out", type=Path, default=None)
    p.add_argument("--warn-only", action="store_true", help="Exit 0 even if warnings (errors still fail)")
    args = p.parse_args()

    conn = connect(args.db)
    try:
        report = audit_tw100_data_quality(
            conn,
            snapshot_date=args.snapshot_date,
            window_start=args.start,
            window_end=args.end,
            min_bars=args.min_bars,
        )
    finally:
        conn.close()

    payload = report.to_dict()
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if report.errors:
        for e in report.errors:
            print(f"ERROR: {e}", file=sys.stderr)
    for w in report.warnings:
        print(f"WARN: {w}", file=sys.stderr)

    if not report.passed:
        return 1
    if report.warnings and not args.warn_only:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
