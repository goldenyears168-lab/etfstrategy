#!/usr/bin/env python3
"""Leading Dip · trigger / TOD expand research compares.

  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_bench_wma5_study.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_bench_wma5_study.py --with-excess
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_bench_wma5_study.py --excess-open
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_bench_wma5_study.py --time-sleeves
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.leading_dip_sleeve_validate import (  # noqa: E402
    BENCH_WMA5_DEPTH_SWEEP,
    BENCH_WMA5_EXCESS_DEPTH_SWEEP,
    OOS_CUT_DEFAULT,
    START_DEFAULT,
    format_bench_wma5_excess_table,
    format_bench_wma5_table,
    format_gap_excess_open_table,
    format_time_sleeve_table,
    render_bench_wma5_excess_md,
    render_bench_wma5_md,
    render_gap_excess_open_md,
    render_time_sleeve_md,
    run_bench_wma5_compare,
    run_bench_wma5_excess_compare,
    run_gap_excess_open_compare,
    run_time_sleeve_expand,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_STEM = RESEARCH_RRG / "20260715_leading_dip_bench_wma5_compare"
DEFAULT_STEM_EX = RESEARCH_RRG / "20260715_leading_dip_bench_wma5_excess_compare"
DEFAULT_STEM_OPEN = RESEARCH_RRG / "20260715_leading_dip_gap_excess_open_anchor"
DEFAULT_STEM_TOD = RESEARCH_RRG / "20260715_leading_dip_time_sleeve_expand"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare Leading Dip trigger variants")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--oos-cut", default=OOS_CUT_DEFAULT)
    ap.add_argument("--wma-len", type=int, default=5)
    ap.add_argument("--with-excess", action="store_true")
    ap.add_argument("--excess-open", action="store_true")
    ap.add_argument(
        "--time-sleeves",
        action="store_true",
        help="TOD density + complementary time-window sleeves",
    )
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    modes = sum(bool(x) for x in (args.with_excess, args.excess_open, args.time_sleeves))
    if modes > 1:
        print("BLOCKER: choose one mode flag", file=sys.stderr)
        return 2

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    if args.time_sleeves:
        out = args.out or DEFAULT_STEM_TOD
    elif args.excess_open:
        out = args.out or DEFAULT_STEM_OPEN
    elif args.with_excess:
        out = args.out or DEFAULT_STEM_EX
    else:
        out = args.out or DEFAULT_STEM

    con = connect(args.db)
    try:
        if args.time_sleeves:
            payload = run_time_sleeve_expand(con, start=args.start, oos_cut=args.oos_cut)
            text = format_time_sleeve_table(payload)
            md = render_time_sleeve_md(payload)
        elif args.excess_open:
            payload = run_gap_excess_open_compare(
                con, start=args.start, oos_cut=args.oos_cut
            )
            text = format_gap_excess_open_table(payload)
            md = render_gap_excess_open_md(payload)
        elif args.with_excess:
            payload = run_bench_wma5_excess_compare(
                con,
                start=args.start,
                oos_cut=args.oos_cut,
                depth_sweep=BENCH_WMA5_EXCESS_DEPTH_SWEEP,
                bench_wma_len=args.wma_len,
            )
            text = format_bench_wma5_excess_table(payload)
            md = render_bench_wma5_excess_md(payload)
        else:
            payload = run_bench_wma5_compare(
                con,
                start=args.start,
                oos_cut=args.oos_cut,
                depth_sweep=BENCH_WMA5_DEPTH_SWEEP,
                bench_wma_len=args.wma_len,
            )
            text = format_bench_wma5_table(payload)
            md = render_bench_wma5_md(payload)
    finally:
        con.close()

    print(text)
    print()

    if not args.no_write:
        stem = out.with_suffix("") if out.suffix in (".md", ".json") else out
        out_json = Path(str(stem) + ".json")
        out_md = Path(str(stem) + ".md")
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")
        out_md.write_text(md + "\n")
        print(f"Wrote {out_json}")
        print(f"Wrote {out_md}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
