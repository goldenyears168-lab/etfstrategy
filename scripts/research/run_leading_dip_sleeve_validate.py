#!/usr/bin/env python3
"""Leading Dip sleeve · Research self-validator (frozen ``leading_dip``).

  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_sleeve_validate.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_sleeve_validate.py --write-artifact
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_sleeve_validate.py --observe-day 2026-04-08
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.leading_dip_sleeve_validate import (  # noqa: E402
    FROZEN_SPEC_ID,
    OOS_CUT_DEFAULT,
    START_DEFAULT,
    format_ablation_table,
    format_observe_text,
    format_report_text,
    observe_day,
    run_n_expansion_ablations,
    run_validation,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate Leading Dip frozen research spec")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--oos-cut", default=OOS_CUT_DEFAULT)
    ap.add_argument(
        "--write-artifact",
        action="store_true",
        help="Write JSON summary under reports/research/rrg/",
    )
    ap.add_argument(
        "--write-events",
        action="store_true",
        help="Write frozen-policy events CSV (paper-observe)",
    )
    ap.add_argument(
        "--ablate-n",
        action="store_true",
        help="Run controlled n-expansion ablations (same cleaning) and print table",
    )
    ap.add_argument(
        "--write-ablation",
        action="store_true",
        help="With --ablate-n, also write JSON artifact",
    )
    ap.add_argument(
        "--observe-day",
        metavar="YYYY-MM-DD",
        help="Paper-observe eligible triggers + slot/cap decisions for one day (no orders)",
    )
    ap.add_argument(
        "--observe-json",
        action="store_true",
        help="With --observe-day, also print JSON payload",
    )
    ap.add_argument(
        "--coverage",
        action="store_true",
        help="With --observe-day, use coverage track (drop T2 filter)",
    )
    args = ap.parse_args()

    con = connect(args.db)
    try:
        if args.observe_day:
            obs = observe_day(
                con,
                args.observe_day,
                start=args.start,
                oos_cut=args.oos_cut,
                require_t2=not args.coverage,
            )
            print(format_observe_text(obs))
            if args.observe_json:
                print(json.dumps(obs, indent=2, ensure_ascii=False, default=str))
            return 0 if obs.get("ok") else 2

        if args.ablate_n:
            rows = run_n_expansion_ablations(con, start=args.start, oos_cut=args.oos_cut)
            print("=== Leading Dip · n-expansion ablations ===")
            print(format_ablation_table(rows))
            if args.write_ablation:
                out_dir = ROOT / "reports" / "research" / "rrg"
                out_dir.mkdir(parents=True, exist_ok=True)
                art = out_dir / "20260715_leading_dip_n_ablation.json"
                art.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
                print(f"\nWrote {art}")
            return 0

        report, events = run_validation(con, start=args.start, oos_cut=args.oos_cut)
    finally:
        con.close()

    text = format_report_text(report)
    print(text)

    out_dir = ROOT / "reports" / "research" / "rrg"
    if args.write_artifact:
        out_dir.mkdir(parents=True, exist_ok=True)
        art = out_dir / "20260715_leading_dip_validate.json"
        art.write_text(json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n")
        print(f"\nWrote {art}")

    if args.write_events and len(events):
        out_dir.mkdir(parents=True, exist_ok=True)
        # Structure events (all Spec structure hits); useful for paper observe
        csv_path = out_dir / "20260715_leading_dip_events.csv"
        events.to_csv(csv_path, index=False)
        print(f"Wrote {csv_path} n={len(events)}")

    if not report.passed:
        print(
            f"\nABORT: {FROZEN_SPEC_ID} self-validation FAILED — "
            "see FAIL gates above (Research regression).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
