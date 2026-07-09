#!/usr/bin/env python3
"""Track 5 · OR fade short sweep + C18acc overlay vs I36.

Topic: cz-track5-or-fade (config/research.yaml)
Outputs:
  reports/research/intraday/cz_track5_or_fade_standalone_2026.json
  reports/research/intraday/cz_track5_c18acc_or_overlay_2026.json
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.cz_track5_or_fade import (  # noqa: E402
    OrFadeParams,
    COST_RT_NEGOTIATED,
    DEFAULT_COST_RT,
    build_track5_verdict,
    default_or_fade_sweep_grid,
    run_c18acc_or_overlay_sweep,
    run_standalone_or_fade_sweep,
    write_track5_report,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

STANDALONE_REPORT = ROOT / "reports/research/intraday/cz_track5_or_fade_standalone_2026.json"
OVERLAY_REPORT = ROOT / "reports/research/intraday/cz_track5_c18acc_or_overlay_2026.json"
VERDICT_REPORT = ROOT / "reports/research/intraday/cz_track5_or_fade_verdict_2026.json"


def compact_or_fade_grid() -> list[OrFadeParams]:
    """Smaller grid for CI / quick iteration."""
    grid: list[OrFadeParams] = []
    for or_min in (5, 15):
        for stop_mult in (0.2, 0.3, 0.4, 0.5):
            for cost, tag in ((DEFAULT_COST_RT, "c0585"), (COST_RT_NEGOTIATED, "c0420")):
                grid.append(
                    OrFadeParams(
                        label=f"sf_or{or_min}_s{stop_mult:.1f}_{tag}",
                        or_minutes=or_min,
                        stop_mult=stop_mult,
                        cost_rt=cost,
                    )
                )
    for or_min in (5, 15):
        grid.append(
            OrFadeParams(
                label=f"lm_or{or_min}_c0585",
                or_minutes=or_min,
                direction="long_momentum",
            )
        )
        grid.append(
            OrFadeParams(
                label=f"sm_or{or_min}_c0585",
                or_minutes=or_min,
                direction="short_momentum",
            )
        )
    for stop_mult in (0.3, 0.4):
        grid.append(
            OrFadeParams(
                label=f"sf_or5_s{stop_mult:.1f}_ext3_c0585",
                or_minutes=5,
                stop_mult=stop_mult,
                min_ext_at_entry_pct=3.0,
            )
        )
    return grid


def main() -> None:
    parser = argparse.ArgumentParser(description="Track 5 OR fade sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--standalone-only", action="store_true")
    parser.add_argument("--overlay-only", action="store_true")
    parser.add_argument("--full-grid", action="store_true", help="Full preregistered grid (~80 variants)")
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    args = parser.parse_args()

    conn = connect(args.db)
    t0 = time.time()
    standalone_result: dict | None = None
    overlay_result: dict | None = None

    if not args.overlay_only:
        print("=== Standalone OR fade sweep ===")
        grid = default_or_fade_sweep_grid() if args.full_grid else compact_or_fade_grid()
        print(f"  variants: {len(grid)}")
        standalone_result = run_standalone_or_fade_sweep(
            conn,
            params_grid=grid,
            date_start_c18acc=args.date_start,
            date_end_c18acc=args.date_end,
        )
        write_track5_report(standalone_result, STANDALONE_REPORT)
        print(f"  pass_count: {standalone_result['summary']['pass_count']}")
        print(f"  best_oos_avg_net: {standalone_result['summary']['best_oos_avg_net_pct']}%")
        print(f"  → {STANDALONE_REPORT}")

    if not args.standalone_only:
        print("=== C18acc OR overlay vs I36 ===")
        overlay_result = run_c18acc_or_overlay_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
        )
        write_track5_report(overlay_result, OVERLAY_REPORT)
        i36 = overlay_result.get("baseline_i36", {})
        best = overlay_result.get("best", {})
        print(f"  I0 mean_excess: {overlay_result['baseline_i0'].get('mean_excess_pct')}%")
        print(f"  I36 mean_excess: {i36.get('mean_excess_pct')}% · Δ I0 {i36.get('delta_vs_i0_pp')}pp")
        print(f"  beats_i36: {overlay_result.get('beats_i36_count')} variants")
        print(f"  best: {best.get('variant_id')} · excess {best.get('mean_excess_pct')}%")
        print(f"  session_or_vs_i36: {overlay_result.get('session_or_vs_i36')}")
        print(f"  → {OVERLAY_REPORT}")

    if standalone_result and overlay_result:
        verdict = build_track5_verdict(standalone_result, overlay_result)
        write_track5_report(
            {"topic": "cz-track5-verdict", **verdict},
            VERDICT_REPORT,
        )
        print(f"=== Verdict ===")
        print(f"  H-ORF-1: {verdict['H-ORF-1_short_fade_oos_positive']}")
        print(f"  H-ORF-2: {verdict['H-ORF-2_overlay_beats_i36']}")
        print(f"  → {VERDICT_REPORT}")

    print(f"Done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
