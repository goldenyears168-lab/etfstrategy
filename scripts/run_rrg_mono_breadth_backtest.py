#!/usr/bin/env python3
"""RRG mono hold7 backtest × 200MA breadth zones."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_backtest import render_comparison_markdown, run_breadth_zone_comparison  # noqa: E402
from research.backtest.slot_backtest_summary import (  # noqa: E402
    SlotBacktestConfig,
    build_summary_payload,
    write_slot_backtest_summary,
)
from stock_db import PROJECT_ROOT  # noqa: E402

from report_paths import RESEARCH_RRG

REPORTS = RESEARCH_RRG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG mono × breadth zone backtest")
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-12-31")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None, help="JSON 輸出（含 by_zone）")
    parser.add_argument(
        "--write-slot-summary",
        action="store_true",
        help="寫入 rrg_mono_hold7_slot_backtest_2026.json（strategy.yaml backtest.source_summary）",
    )
    args = parser.parse_args(argv)

    print(f"Running mono+seg_last+3slot+hold7 × 5 breadth zones ({args.date_start}..{args.date_end})...")
    results = run_breadth_zone_comparison(
        date_start=args.date_start,
        date_end=args.date_end,
    )
    md = render_comparison_markdown(results)
    stamp = date.today().strftime("%Y%m%d")
    out = args.output or REPORTS / f"{stamp}_rrg_mono_breadth_zones.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"Wrote {out}")
    print()
    print(md)

    if args.json:
        slim = {
            "date_start": results["date_start"],
            "date_end": results["date_end"],
            "by_zone": {
                z: {
                    "summary": results["by_zone"][z]["summary"],
                    "zh": results["by_zone"][z]["zh"],
                }
                for z in results["by_zone"]
            },
            "pooled_by_entry_zone": results["pooled_by_entry_zone"],
            "pooled_all": results["pooled_all"]["summary"],
        }
        args.json.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")

    if args.write_slot_summary or (
        args.date_start <= "2026-12-31" and args.date_end >= "2026-01-01"
    ):
        cfg = SlotBacktestConfig(
            date_start=max(args.date_start, "2026-01-01"),
            date_end=min(args.date_end, "2026-12-31"),
            n_slots=3,
            hold_days=7,
        )
        summary_path = REPORTS / "rrg_mono_hold7_slot_backtest_2026.json"
        payload = build_summary_payload(
            track_id="rrg-mono-hold7",
            config=cfg,
            summary=results["pooled_all"]["summary"],
            source_module="rrg_mono_backtest",
        )
        write_slot_backtest_summary(summary_path, payload)
        print(f"Wrote {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
