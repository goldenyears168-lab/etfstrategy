"""Tests for RRG drs_5d displacement scoring."""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research.backtest.rrg_drs3d_score_backtest import compute_cross_100_nd
from research.backtest.rrg_drs5d_score_backtest import (
    DEFAULT_KINEMATIC_WEIGHTS_5D,
    DEFAULT_WEIGHTS_5D,
    HOLD_DAYS,
    TUNED_KINEMATIC_WEIGHTS_5D,
    analyze_trajectory_feature_ic,
    compute_drs5d_score,
    compute_drs5d_score_kinematic,
    evaluate_score_ab_comparison_5d,
    run_walk_forward_validation,
)


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "end_q": "improving",
        "sig_ret": 1.5,
        "improve_streak": 4,
        "disp_contract": True,
        "rsr_d": 98.5,
        "drs_rsr_1d": 0.4,
        "drs_rsr_4d": 1.2,
        "drs_rsr_5d": 1.8,
        "disp": 0.8,
        "trend": "up_right",
        "mono_up": True,
        "heading_rsr": 0.75,
        "v20": 0.3,
        "a_step": 0.1,
    }
    base.update(overrides)
    return base


def _synthetic_panel(n_days: int = 40, per_day: int = 12) -> pd.DataFrame:
    rng = np.random.default_rng(99)
    rows: list[dict[str, object]] = []
    for y in range(2015, 2015 + n_days // 10 + 2):
        for m in range(1, 13):
            d = f"{y}-{m:02d}-15"
            for i in range(per_day):
                feat = rng.uniform(0, 2)
                rows.append(
                    {
                        **_row(),
                        "date": d,
                        "sid": f"{i:04d}",
                        "drs_rsr_5d": feat,
                        "heading_rsr": feat / 2,
                        "drs_5d": feat * 0.8 + rng.normal(0, 0.1),
                        "ret_5d": rng.normal(0.2, 0.5),
                        "flip_leading_5d": feat > 1.0,
                        "cross_100_5d": feat > 1.2,
                        "in_pool_omega": True,
                        "in_pool_omega_kinematic": True,
                    }
                )
    return pd.DataFrame(rows)


class TestDrs5dScore(unittest.TestCase):
    def test_hold_days_constant(self) -> None:
        self.assertEqual(HOLD_DAYS, 5)

    def test_tuned_weights_match_default(self) -> None:
        self.assertEqual(TUNED_KINEMATIC_WEIGHTS_5D["w_velocity"], 0.95)

    def test_compute_drs5d_score_high_vs_low(self) -> None:
        high = compute_drs5d_score(_row(drs_rsr_5d=2.5, mono_up=True))
        low = compute_drs5d_score(_row(drs_rsr_5d=0.0, mono_up=False, rsr_d=96.0, disp=1.3))
        self.assertGreater(high, low)

    def test_kinematic_5d_ranks_velocity(self) -> None:
        high = compute_drs5d_score_kinematic(_row(drs_rsr_5d=2.0, heading_rsr=0.9, mono_up=True))
        low = compute_drs5d_score_kinematic(_row(drs_rsr_5d=0.1, heading_rsr=0.1, mono_up=False))
        self.assertGreater(high, low)

    def test_cross_100_5d_logic(self) -> None:
        rsr_d = pd.Series([98.0, 100.5])
        rsr_d5 = pd.Series([101.0, 99.0])
        cross = compute_cross_100_nd(rsr_d, rsr_d5)
        self.assertEqual(cross.tolist(), [True, False])

    def test_evaluate_ab_5d_smoke(self) -> None:
        n = 60
        panel = pd.DataFrame(
            {
                "drs5d_score": [i / n for i in range(n)],
                "drs5d_score_kin": [i / n for i in range(n)],
                "drs_5d": [i / n * 2 for i in range(n)],
                "ret_5d": [i / n for i in range(n)],
                "flip_leading_5d": [i > 30 for i in range(n)],
                "cross_100_5d": [i > 40 for i in range(n)],
                "in_pool_omega": True,
                "in_pool_omega_kinematic": True,
            }
        )
        ab = evaluate_score_ab_comparison_5d(panel)
        self.assertGreater(ab.position.spearman_ic, 0.5)
        self.assertGreater(ab.kinematic.spearman_ic, 0.0)

    def test_trajectory_feature_ic_smoke(self) -> None:
        panel = _synthetic_panel()
        out = analyze_trajectory_feature_ic(panel)
        self.assertGreater(out["n_features_tested"], 0)
        self.assertIn(out["verdict"], ("yes_multiple_features", "yes_top_feature_only", "weak_some_features", "not_significant_pooled"))

    def test_walk_forward_smoke(self) -> None:
        panel = _synthetic_panel(n_days=80, per_day=15)
        wf = run_walk_forward_validation(panel, train_years=2, test_years=1)
        self.assertGreater(wf["summary"]["n_folds"], 0)
        self.assertGreater(len(wf["folds"]), 0)

    def test_default_5d_weight_keys(self) -> None:
        self.assertIn("vel_scale", DEFAULT_WEIGHTS_5D)
        self.assertEqual(DEFAULT_KINEMATIC_WEIGHTS_5D["w_proximity"], 0.0)


if __name__ == "__main__":
    unittest.main()
