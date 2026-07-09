#!/usr/bin/env python3
"""C18acc+S2 · Phase W-stack earliest-exit analysis.

用法：
  PYTHONPATH=src python3 scripts/run_c18acc_s2_dual_wma_stack.py
  PYTHONPATH=src python3 scripts/run_c18acc_s2_dual_wma_stack.py \\
    --annot reports/research/rrg/20260704_c18acc_s2_dual_wma_annot.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_s2_dual_wma_exit_save import load_annot_payload  # noqa: E402
from research.backtest.archive.c18acc_s2_dual_wma_stack import (  # noqa: E402
    render_w_stack_md,
    run_w_stack_analysis,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc+S2 Phase W-stack")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--annot",
        type=Path,
        default=RESEARCH_RRG / "20260704_c18acc_s2_dual_wma_annot.json",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.annot.is_file():
        raise SystemExit(f"annot not found: {args.annot}")

    annot = load_annot_payload(args.annot)
    conn = connect(args.db)
    try:
        payload = run_w_stack_analysis(conn, annot)
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_s2_dual_wma_stack.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_w_stack_md(payload), encoding="utf-8")

    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    full = payload["summaries"]["W-S-FULL"]["all_legs"]
    cons = payload["summaries"]["W-S-FULL-CONS"]["all_legs"]
    print(
        f"  B0 mean_excess={payload['summaries']['B0']['all_legs'].get('mean_excess_pct')}%"
    )
    print(
        f"  W-S-FULL Δ={full.get('delta_mean_excess_pp')}pp "
        f"early={full.get('n_early_exit')} cons Δ={cons.get('delta_mean_excess_pp')}pp"
    )
    print(f"  best={payload.get('best_variant_by_excess')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
