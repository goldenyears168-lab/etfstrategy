#!/usr/bin/env python3
"""C18acc · structural pyramid · 3-slot resim (H-C18-PYRAMID-2).

  PYTHONPATH=src python3 scripts/research/run_c18acc_structural_pyramid_slot_resim.py
  PYTHONPATH=src python3 scripts/research/run_c18acc_structural_pyramid_slot_resim.py \\
    --compare-slots 3,4 --date-end 2026-07-09
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.c18acc_structural_pyramid_slot_resim import (  # noqa: E402
    render_c18acc_slot_count_compare_md,
    render_c18acc_structural_pyramid_slot_resim_md,
    run_c18acc_slot_count_compare,
    run_c18acc_structural_pyramid_slot_resim,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc pyramid slot resim / slot-count compare")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-02")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--n-slots", type=int, default=3)
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument("--oos-start", default="2026-01-02")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--gate-cache",
        type=Path,
        default=RESEARCH_RRG / "20260711_c18acc_avoid_mixed_gate_cache.json",
    )
    ap.add_argument("--total-capital", type=float, default=100_000.0)
    ap.add_argument(
        "--compare-slots",
        default=None,
        help="Comma list e.g. 3,4 — champion-only slot-count compare (skips pyramid)",
    )
    ap.add_argument(
        "--skip-pyramid",
        action="store_true",
        help="Champion-only (no kinematic / pyramid overlay)",
    )
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    stamp = date.today().strftime("%Y%m%d")
    conn = connect(args.db)
    try:
        if args.compare_slots:
            counts = tuple(int(x.strip()) for x in args.compare_slots.split(",") if x.strip())
            payload = run_c18acc_slot_count_compare(
                conn,
                slot_counts=counts,
                date_start=args.date_start,
                date_end=args.date_end,
                is_end=args.is_end,
                oos_start=args.oos_start,
                gate_cache_path=str(args.gate_cache) if args.gate_cache else None,
                total_capital=args.total_capital,
            )
            out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_slot_count_compare.json"
            out_md = out_json.with_suffix(".md")
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            out_md.write_text(render_c18acc_slot_count_compare_md(payload), encoding="utf-8")
            d = payload.get("delta") or {}
            print(f"\nWrote {out_json}")
            print(f"Wrote {out_md}")
            print(
                f"  OOS Δexcess={d.get('delta_oos_excess_pp')}pp · "
                f"legs Δ={d.get('delta_oos_n')} · "
                f"FULL NAV Δ={d.get('delta_full_nav_total_pp')}pp"
            )
            return 0

        payload = run_c18acc_structural_pyramid_slot_resim(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            n_slots=args.n_slots,
            is_end=args.is_end,
            oos_start=args.oos_start,
            gate_cache_path=str(args.gate_cache) if args.gate_cache else None,
            total_capital=args.total_capital,
            skip_pyramid=bool(args.skip_pyramid),
        )
    finally:
        conn.close()

    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_structural_pyramid_slot_resim.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_structural_pyramid_slot_resim_md(payload), encoding="utf-8")

    v = payload.get("verdict") or {}
    print(f"\nWrote {out_json}")
    print(f"Wrote {out_md}")
    print(f"  {v.get('summary')}")
    if payload.get("skip_pyramid"):
        return 0
    return 0 if v.get("recommend_adopt") else 2


if __name__ == "__main__":
    raise SystemExit(main())
