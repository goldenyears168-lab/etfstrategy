#!/usr/bin/env python3
"""C18acc daily RRG Weakening stop-loss overlay sweep · C_bleed / C_pure_drip cohorts。

用法：
  PYTHONPATH=src python scripts/run_c18acc_rrg_weakening_stop_sweep.py
  PYTHONPATH=src python scripts/run_c18acc_rrg_weakening_stop_sweep.py \\
    --date-start 2024-01-01 --date-end 2026-06-30
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.c18acc_rrg_weakening_stop_sweep import (  # noqa: E402
    render_rrg_weakening_stop_md,
    run_rrg_weakening_stop_sweep,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc RRG Weakening stop sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-30")
    parser.add_argument("--is-end", default="2025-12-31")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        payload = run_rrg_weakening_stop_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_start=args.oos_start,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_rrg_weakening_stop_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out.with_suffix(".md")
    md_path.write_text(render_rrg_weakening_stop_md(payload), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Wrote {md_path}")

    full = payload.get("full") or {}
    print(f"C_bleed n={full.get('bleed_cohort_n')} · C_pure_drip n={full.get('pure_drip_cohort_n')}")
    best = payload.get("best_by_delta_i36") or {}
    print(
        f"Best Δ I36: {best.get('variant_id')} · delta={best.get('delta_vs_i36_pp')}pp "
        f"quad={best.get('quad_exits')} ext={best.get('ext_exits')}"
    )
    w6 = (
        (full.get("hypothesis_results") or {})
        .get("variants", {})
        .get("W6", {})
        .get("hypotheses", {})
    )
    print(f"W6 H-RRG-WK-D1: {w6.get('H-RRG-WK-D1')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
