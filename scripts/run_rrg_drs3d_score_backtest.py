#!/usr/bin/env python3
"""RRG drs_3d displacement score · 3-day hold backtest runner."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_drs3d_score_backtest import (  # noqa: E402
    run_drs3d_score_backtest,
    write_drs3d_report,
)

REPORT_DIR = ROOT / "reports" / "research" / "rrg"


def main() -> int:
    ap = argparse.ArgumentParser(description="RRG drs_3d displacement score backtest")
    ap.add_argument("--out-dir", type=Path, default=REPORT_DIR)
    args = ap.parse_args()

    result = run_drs3d_score_backtest()
    json_path, md_path = write_drs3d_report(result, args.out_dir)

    ev = result["evaluation"]
    ab = result.get("ab_comparison", {})
    kin = ab.get("kinematic", {})
    bridge = result.get("bridge_stratification", {})
    funnel = result.get("two_stage_funnel", {})
    verdict = result.get("verdict", {})
    deep = result.get("deep_dive", {})
    print(
        f"Pool Ω n={ev['omega_n']} · IC={ev['spearman_ic']:+.4f} · lift={ev['top_decile_lift']:+.3f}"
    )
    if kin:
        print(
            f"Pool Ω′ n={kin.get('omega_n', 0)} · IC={kin.get('spearman_ic', 0):+.4f} · "
            f"lift={kin.get('top_decile_lift', 0):+.3f}"
        )
    n_sig = len(bridge.get("significant_pools", []))
    print(f"Bridge sig ret_3d>0 pools: {n_sig}")
    if funnel.get("lanes"):
        top_lane = funnel["lanes"][0]
        print(
            f"Top funnel lane: {top_lane['lane_id']} n={top_lane['n']} "
            f"mean_ret_3d={top_lane['mean_ret_3d']:+.2f}%"
        )
    print(f"H-B1 beats D10: {funnel.get('h_b1_beats_score_d10')}")
    if verdict:
        top = verdict.get("top_lane") or {}
        print(f"\n§I Verdict: {verdict.get('verdict', '—')} · top={top.get('lane_id', '—')}")
        print(f"H-B1 D10 track abandon: {verdict.get('abandon_h_b1_d10_track')}")
    if deep.get("lanes"):
        for ln in deep["lanes"][:3]:
            if ln["lane_id"] in (
                "score_top_flat_b1_disp",
                "lead3_flat_b1_disp",
                "bridge_d47_nl_chase",
            ):
                wf = ln.get("walk_forward", {}).get("oos_pooled", {})
                print(
                    f"  {ln['lane_id']}: n={ln['n']} ret={ln['mean_ret_3d']:+.2f}% "
                    f"excess={ln.get('mean_excess_3d', 0):+.2f}% OOS n={wf.get('n', 0)}"
                )
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
