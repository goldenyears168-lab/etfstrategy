#!/usr/bin/env python3
"""RRG drs_5d displacement score · 5-day hold backtest runner."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.rrg_drs5d_score_backtest import (  # noqa: E402
    run_drs5d_score_backtest,
    write_drs5d_report,
)

REPORT_DIR = ROOT / "reports" / "research" / "rrg"


def main() -> int:
    ap = argparse.ArgumentParser(description="RRG drs_5d · 5-day structural displacement backtest")
    ap.add_argument("--out-dir", type=Path, default=REPORT_DIR)
    args = ap.parse_args()

    result = run_drs5d_score_backtest()
    json_path, md_path = write_drs5d_report(result, args.out_dir)

    ev = result["evaluation"]
    ab = result.get("ab_comparison", {})
    kin = ab.get("kinematic", {})
    tuned = result.get("tuned_kinematic_evaluation", {})
    bl = result.get("pool_omega_baseline", {})

    print(f"Pool Ω n={ev['omega_n']} · IC(drs_5d)={ev['spearman_ic']:+.4f} · lift={ev['top_decile_lift']:+.3f}")
    print(f"  mean drs_5d={ev['pool_mean_drs_3d']:+.3f} · mean ret_5d={bl.get('mean_ret_5d', 0):+.2f}%")
    if kin:
        print(
            f"Pool Ω′ n={kin.get('omega_n', 0)} · IC={kin.get('spearman_ic', 0):+.4f} · "
            f"lift={kin.get('top_decile_lift', 0):+.3f}"
        )
    if tuned:
        print(
            f"Tuned Ω′ IC={tuned.get('spearman_ic', 0):+.4f} · "
            f"lift={tuned.get('top_decile_lift', 0):+.3f}"
        )
    wf = result.get("walk_forward", {})
    if wf.get("summary"):
        sm = wf["summary"]
        print(
            f"WFA OOS: {sm.get('n_oos_ic_positive', 0)}/{sm.get('n_folds', 0)} folds IC>0 · "
            f"mean OOS IC={sm.get('mean_oos_ic_tuned', 0):+.4f} · "
            f"{'PASS' if wf.get('passed') else wf.get('verdict', 'fail')}"
        )
    feat = result.get("trajectory_feature_ic", {})
    if feat:
        print(
            f"Trajectory features: {feat.get('n_significant_p05', 0)}/"
            f"{feat.get('n_features_tested', 0)} sig · verdict={feat.get('verdict', '')}"
        )
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
