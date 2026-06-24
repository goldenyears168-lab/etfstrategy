#!/usr/bin/env python3
"""rrg-mono-swap-accel（C18acc）× Market breadth zone · graduation hold-out。

用法：
  PYTHONPATH=src python scripts/run_rrg_mono_swap_accel_breadth.py
  PYTHONPATH=src python scripts/run_rrg_mono_swap_accel_breadth.py \\
    --date-start 2024-01-01 --date-end 2026-06-22
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_score_swap_c import (  # noqa: E402
    render_swap_accel_breadth_markdown,
    run_swap_accel_breadth_zone_comparison,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc × breadth zone hold-out")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--out-md", type=Path, default=None)
    parser.add_argument("--out-json", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        results = run_swap_accel_breadth_zone_comparison(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_md = args.out_md or RESEARCH_RRG / f"{stamp}_rrg_mono_swap_accel_breadth_zones.md"
    out_json = args.out_json or RESEARCH_RRG / f"{stamp}_rrg_mono_swap_accel_breadth_zones.json"
    out_md.parent.mkdir(parents=True, exist_ok=True)

    md = render_swap_accel_breadth_markdown(results)
    out_md.write_text(md, encoding="utf-8")

    slim = {
        "slug": results["slug"],
        "short_name": results["short_name"],
        "variant_id": results["variant_id"],
        "date_start": results["date_start"],
        "date_end": results["date_end"],
        "pooled_all": results["pooled_all"],
        "pooled_by_entry_zone": results["pooled_by_entry_zone"],
        "by_zone": {
            z: {"summary": results["by_zone"][z]["summary"], "zh": results["by_zone"][z]["zh"]}
            for z in results["by_zone"]
        },
        "references": results.get("references"),
        "graduation_gate": results.get("graduation_gate"),
    }
    out_json.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")

    gate = results.get("graduation_gate") or {}
    print(f"Wrote {out_md}")
    print(f"Wrote {out_json}")
    print(f"Graduation gate: {'PASS' if gate.get('passed') else 'FAIL'}")
    print(md)
    return 0 if gate.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
