#!/usr/bin/env python3
"""RRG mono top10 × T-1 成交量分層 · hold7 統計研究。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_volume_tier import (  # noqa: E402
    analyze_paired_extreme_legs,
    analyze_volume_tier_legs,
    collect_paired_extreme_legs,
    collect_volume_tier_legs,
    render_volume_tier_markdown,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG mono volume tier study")
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-12-31")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    legs, meta = collect_volume_tier_legs(
        conn, date_start=args.date_start, date_end=args.date_end
    )
    paired_legs, paired_meta = collect_paired_extreme_legs(
        conn, date_start=args.date_start, date_end=args.date_end, min_pool=3
    )
    analysis = analyze_volume_tier_legs(legs)
    paired_analysis = analyze_paired_extreme_legs(paired_legs)
    md = render_volume_tier_markdown(
        legs,
        meta,
        analysis,
        paired_legs=paired_legs,
        paired_analysis=paired_analysis,
        paired_meta=paired_meta,
    )

    stamp = date.today().strftime("%Y%m%d")
    out = args.output or RESEARCH_RRG / f"{stamp}_rrg_mono_volume_tier_hold7.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"Wrote {out}")
    print()
    print(md)

    if args.json:
        payload = {
            "meta": meta,
            "analysis": analysis,
            "legs": [asdict(lg) for lg in legs],
            "paired_meta": paired_meta,
            "paired_analysis": paired_analysis,
            "paired_legs": [asdict(lg) for lg in paired_legs],
        }
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
