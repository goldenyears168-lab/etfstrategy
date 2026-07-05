#!/usr/bin/env python3
"""C18acc · POOL1 + S2 · hard stop −8% / −10% sweep."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.c18acc_hard_stop_sweep import (  # noqa: E402
    render_c18acc_hard_stop_sweep_md,
    run_c18acc_hard_stop_sweep,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc POOL1 hard stop sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-07-03")
    parser.add_argument("--is-end", default="2025-12-31")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_c18acc_hard_stop_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_start=args.oos_start,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_hard_stop_sweep.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_hard_stop_sweep_md(payload), encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")

    for vid in (
        "S2-POOL1",
        "S2L8",
        "HS8",
        "HS10",
        "IHS8",
        "IHS10",
        "PLC10",
    ):
        full = next(
            (r for r in payload["rows"] if r.get("variant_id") == vid and r.get("label") == "FULL"),
            None,
        )
        if not full or full.get("error"):
            continue
        port = full.get("portfolio") or {}
        legs10 = (full.get("legs_lte_minus_10pct") or {}).get("count")
        print(
            f"{vid} FULL: excess={full.get('mean_excess_pct')}% "
            f"total_ret={port.get('total_return_pct')}% "
            f"hard_stop={full.get('hard_stop_exits')} legs≤−10%={legs10} "
            f"Δ={full.get('delta_mean_excess_pp', 0)}pp"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
