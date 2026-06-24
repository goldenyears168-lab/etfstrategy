#!/usr/bin/env python3
"""C18 動量排序變體 · M1 rs_momentum 水位 · M2 RRG 位移減數（seg_step_delta）。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_score_swap_c import (  # noqa: E402
    DEFAULT_C18_MOM_SWEEP,
    run_score_swap_c_sweep,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18 mom sort-key sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_score_swap_c_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            configs=DEFAULT_C18_MOM_SWEEP,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18_mom_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    ref = payload.get("reference_c0_hold7") or {}
    print(f"Ref C0 hold7 mean_excess={ref.get('mean_excess_pct')}%")
    for row in payload.get("summaries") or []:
        print(
            f"  {row.get('variant_id'):12} sort={row.get('sort_key'):14} "
            f"margin={row.get('effective_margin')} "
            f"mean_excess={row.get('mean_excess_pct')}% "
            f"swaps={row.get('swaps_total')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
