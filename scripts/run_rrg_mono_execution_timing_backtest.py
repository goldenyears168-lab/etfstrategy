#!/usr/bin/env python3
"""RRG mono hold7 · signal-day close vs next-day open entry comparison."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_backtest import (  # noqa: E402
    render_execution_timing_markdown,
    run_execution_timing_comparison,
)
from report_paths import RESEARCH_RRG  # noqa: E402

REPORTS = RESEARCH_RRG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RRG mono hold7 · close vs next_open execution timing"
    )
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-12-31")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    print(
        f"Running RRG mono hold7 execution timing ({args.date_start}..{args.date_end})..."
    )
    results = run_execution_timing_comparison(
        date_start=args.date_start,
        date_end=args.date_end,
    )
    md = render_execution_timing_markdown(results)
    stamp = date.today().strftime("%Y%m%d")
    out = args.output or REPORTS / f"{stamp}_rrg_mono_execution_timing.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"Wrote {out}")
    print()
    print(md)

    if args.json:
        slim = {
            "date_start": results["date_start"],
            "date_end": results["date_end"],
            "by_mode": {
                k: {
                    "label": v["label"],
                    "summary": v["summary"],
                    "by_weekday": v["by_weekday"],
                    "friday_signals": v["friday_signals"],
                    "friday_n": v["friday_n"],
                }
                for k, v in results["by_mode"].items()
            },
        }
        args.json.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
