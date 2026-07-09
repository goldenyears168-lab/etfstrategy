"""Tests for RRG drs_3d displacement scoring."""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research.backtest.archive.rrg_drs3d_score_backtest import (
    DEFAULT_KINEMATIC_WEIGHTS,
    DEFAULT_WEIGHTS,
    compute_cross_100_3d,
    compute_drs3d_score,
    compute_drs3d_score_kinematic,
    compute_go_verdict,
    compute_yearly_lane_stats,
    cross_sectional_percentile_rank,
    evaluate_bridge_stratification,
    evaluate_deep_dive,
    evaluate_lane_walk_forward,
    evaluate_score_ab_comparison,
    evaluate_score_deciles,
    evaluate_two_stage_funnels,
    pool_omega_kinematic_mask,
    pool_omega_mask,
    _lane_mask_drs3d,
    _ret_outcome_stats,
)


def _omega_row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "end_q": "improving",
        "sig_ret": 1.5,
        "improve_streak": 4,
        "disp_contract": True,
        "rsr_d": 98.5,
        "rv": 98.5,
        "drs_rsr_1d": 0.4,
        "drs_rsr_4d": 1.2,
        "disp": 0.8,
        "trend": "up_right",
        "mono_up": True,
        "heading_rsr": 0.75,
        "v20": 0.3,
        "a_step": 0.1,
    }
    base.update(overrides)
    return base


class TestDrs3dScore(unittest.TestCase):
    def test_compute_score_high_vs_low(self) -> None:
        high = compute_drs3d_score(_omega_row(mono_up=True, drs_rsr_1d=0.8, drs_rsr_4d=2.0))
        low = compute_drs3d_score(
            _omega_row(mono_up=False, drs_rsr_1d=-0.2, drs_rsr_4d=0.0, rsr_d=96.0, disp=1.3)
        )
        self.assertGreater(high, low)

    def test_chase_penalty(self) -> None:
        no_chase = compute_drs3d_score(_omega_row(sig_ret=2.0, disp_contract=True))
        chase = compute_drs3d_score(_omega_row(sig_ret=4.0, disp_contract=False))
        self.assertGreater(no_chase, chase)

    def test_proximity_higher_near_100(self) -> None:
        near = compute_drs3d_score(_omega_row(rsr_d=99.0))
        far = compute_drs3d_score(_omega_row(rsr_d=96.0))
        self.assertGreater(near, far)

    def test_pool_omega_mask(self) -> None:
        rows = [
            _omega_row(),
            _omega_row(trend="down_left", end_q="lagging", sig_ret=0.5, disp_contract=True),
            _omega_row(sig_ret=6.0),
            _omega_row(disp=0.1),
            _omega_row(rsr_d=97.0, rv=97.0),
        ]
        panel = pd.DataFrame(rows)
        mask = pool_omega_mask(panel)
        self.assertTrue(mask.iloc[0])
        self.assertEqual(int(mask.sum()), 1)

    def test_pool_omega_kinematic_includes_rsr96_excludes_down_left(self) -> None:
        rows = [
            _omega_row(rsr_d=96.0, rv=96.0),
            _omega_row(trend="down_left", end_q="lagging", sig_ret=0.5, disp_contract=True),
            _omega_row(rsr_d=101.0, rv=101.0, improve_streak=1),
            _omega_row(disp=0.1),
        ]
        panel = pd.DataFrame(rows)
        mask = pool_omega_kinematic_mask(panel)
        self.assertTrue(mask.iloc[0], "RSR=96 up_right should pass kinematic pool")
        self.assertFalse(mask.iloc[1], "down_left should be excluded")
        self.assertFalse(mask.iloc[2], "low improve_streak should be excluded")
        self.assertEqual(int(mask.sum()), 1)

    def test_score_kin_ranks_high_velocity_over_low(self) -> None:
        high_vel = compute_drs3d_score_kinematic(
            _omega_row(v20=1.0, heading_rsr=0.9, drs_rsr_1d=0.8, mono_up=True)
        )
        low_vel = compute_drs3d_score_kinematic(
            _omega_row(v20=0.05, heading_rsr=0.2, drs_rsr_1d=0.0, mono_up=False, a_step=-0.1)
        )
        self.assertGreater(high_vel, low_vel)

    def test_cross_100_3d_logic(self) -> None:
        rsr_d = pd.Series([98.0, 99.5, 100.2, 97.0])
        rsr_d3 = pd.Series([100.5, 100.0, 99.8, 96.5])
        cross = compute_cross_100_3d(rsr_d, rsr_d3)
        expected = [True, True, False, False]
        self.assertEqual(cross.tolist(), expected)

    def test_drs_3d_outcome_logic(self) -> None:
        rsr_d = np.array([98.0, 99.5, 100.2])
        rsr_d3 = np.array([100.5, 100.0, 99.8])
        drs = rsr_d3 - rsr_d
        np.testing.assert_allclose(drs, [2.5, 0.5, -0.4])

    def test_cross_sectional_percentile_rank(self) -> None:
        panel = pd.DataFrame(
            {
                "date": ["2026-01-01", "2026-01-01", "2026-01-01", "2026-01-02", "2026-01-02"],
                "raw": [1.0, 2.0, 3.0, 5.0, 10.0],
                "in_pool": [True, True, True, True, True],
            }
        )
        ranks = cross_sectional_percentile_rank(panel, "raw", mask_col="in_pool")
        self.assertAlmostEqual(ranks.iloc[0], 1 / 3, places=4)
        self.assertAlmostEqual(ranks.iloc[2], 1.0, places=4)
        self.assertAlmostEqual(ranks.iloc[4], 1.0, places=4)

    def test_evaluate_score_deciles_monotonic(self) -> None:
        n = 100
        rng = np.random.default_rng(42)
        scores = rng.uniform(0, 1, n)
        drs = scores * 2.0 + rng.normal(0, 0.1, n)
        panel = pd.DataFrame(
            {
                "drs3d_score": scores,
                "drs_3d": drs,
                "ret_3d": drs,
                "flip_leading_3d": drs > 1.0,
                "cross_100_3d": drs > 1.5,
                "in_pool_omega": True,
            }
        )
        ev = evaluate_score_deciles(panel, subset_col="in_pool_omega")
        self.assertGreater(ev.spearman_ic, 0.5)
        self.assertTrue(ev.h1_top_beats_pool)
        self.assertEqual(len(ev.deciles), 10)

    def test_evaluate_score_ab_comparison_smoke(self) -> None:
        n = 50
        rng = np.random.default_rng(7)
        scores = rng.uniform(0, 1, n)
        kin_scores = rng.uniform(0, 1, n)
        drs = scores * 1.5 + rng.normal(0, 0.2, n)
        panel = pd.DataFrame(
            {
                "drs3d_score": scores,
                "drs3d_score_kin": kin_scores,
                "drs3d_score_kin_prox": kin_scores * 0.9,
                "drs_3d": drs,
                "ret_3d": drs,
                "flip_leading_3d": drs > 0.5,
                "cross_100_3d": drs > 1.0,
                "in_pool_omega": True,
                "in_pool_omega_kinematic": rng.random(n) > 0.3,
            }
        )
        ab = evaluate_score_ab_comparison(panel)
        self.assertIsNotNone(ab.position)
        self.assertIsNotNone(ab.kinematic)
        self.assertIsNotNone(ab.kinematic_with_proximity)

    def test_default_weights_keys(self) -> None:
        for key in ("w_proximity", "w_velocity", "w_spread", "w_mono"):
            self.assertIn(key, DEFAULT_WEIGHTS)
        for key in ("w_velocity", "w_heading", "w_spread", "w_mono"):
            self.assertIn(key, DEFAULT_KINEMATIC_WEIGHTS)
        self.assertEqual(DEFAULT_KINEMATIC_WEIGHTS["w_proximity"], 0.0)

    def test_bridge_stratification_smoke(self) -> None:
        rows = []
        for i in range(60):
            rows.append(
                {
                    **_omega_row(
                        sig_ret=0.5 if i % 3 == 0 else 4.0,
                    ),
                    "end_q": "leading" if i % 2 == 0 else "improving",
                    "drs_3d": 0.5,
                    "ret_3d": 1.0 if i % 3 == 0 else -0.5,
                    "flip_leading_3d": False,
                    "drs3d_score": float(i) / 60,
                    "in_pool_omega": True,
                }
            )
        panel = pd.DataFrame(rows)
        bridge = evaluate_bridge_stratification(panel)
        self.assertGreater(bridge["n"], 0)
        self.assertGreater(len(bridge["cells"]), 0)

    def test_two_stage_funnel_smoke(self) -> None:
        rows = []
        for i in range(80):
            rows.append(
                {
                    **_omega_row(rsr_d=98.8, rv=98.8, sig_ret=0.2, improve_streak=5),
                    "drs_3d": 0.8,
                    "ret_3d": 2.0 if i >= 72 else 0.1,
                    "flip_leading_3d": i >= 72,
                    "drs3d_score": float(i) / 80,
                    "in_pool_omega": True,
                    "bench_today_ret": 1.2,
                    "disp_contract": True,
                    "base_setup": True,
                    "s2_setup_core": True,
                    "s4_rv_98_99": True,
                    "excess": -1.0,
                    "prev_end_q": "improving",
                    "s0_fresh_flip": False,
                    "s3_burst": False,
                    "sid_hist_ok": True,
                }
            )
        panel = pd.DataFrame(rows)
        funnel = evaluate_two_stage_funnels(panel)
        self.assertGreater(len(funnel["lanes"]), 0)
        d10 = next(ln for ln in funnel["lanes"] if ln["lane_id"] == "score_d10")
        self.assertGreaterEqual(d10["n"], 1)

    def _synthetic_panel(self, n: int = 120) -> pd.DataFrame:
        rows = []
        for i in range(n):
            yr = 2018 + (i // 30)
            rows.append(
                {
                    **_omega_row(rsr_d=98.8, rv=98.8, sig_ret=0.2 if i % 4 else 4.5, improve_streak=5),
                    "date": f"{yr}-{(i % 12) + 1:02d}-15",
                    "sid": f"{1000 + i % 5}",
                    "drs_3d": 0.8,
                    "ret_3d": 1.5 if i % 5 == 0 else 0.2,
                    "excess_3d": 1.0 if i % 5 == 0 else -0.1,
                    "bench_ret_3d": 0.5,
                    "flip_leading_3d": i % 5 == 0,
                    "drs3d_score": float(i) / n,
                    "in_pool_omega": True,
                    "bench_today_ret": 1.2,
                    "disp_contract": True,
                    "base_setup": True,
                    "s2_setup_core": True,
                    "s4_rv_98_99": True,
                    "excess": -1.0,
                    "prev_end_q": "improving",
                    "s0_fresh_flip": False,
                    "s3_burst": False,
                    "sid_hist_ok": True,
                }
            )
        return pd.DataFrame(rows)

    def test_ret_outcome_stats_includes_excess(self) -> None:
        sub = pd.DataFrame({"ret_3d": [1.0, 2.0, 0.5], "excess_3d": [0.5, 1.0, -0.2], "drs_3d": [0.1, 0.2, 0.3]})
        stats = _ret_outcome_stats(sub)
        self.assertIn("mean_excess_3d", stats)
        self.assertAlmostEqual(stats["mean_excess_3d"], 0.4333, places=3)

    def test_lane_mask_not_leading_and_bridge_atoms(self) -> None:
        panel = pd.DataFrame(
            [
                {**_omega_row(), "end_q": "improving", "sig_ret": 2.0, "score_decile": 5.0},
                {**_omega_row(), "end_q": "leading", "sig_ret": 2.0, "score_decile": 5.0},
                {**_omega_row(), "end_q": "improving", "sig_ret": 4.0, "score_decile": 6.0},
            ]
        )
        panel["atom_not_leading_d"] = panel["end_q"] != "leading"
        panel["atom_decile_d47"] = panel["score_decile"].between(4, 7)
        panel["atom_moderate"] = panel["sig_ret"].map(
            lambda x: -1.0 <= x <= 1.0 or (1.0 < x < 3.0)
        )
        panel["atom_chase"] = panel["sig_ret"] >= 3.0
        m = _lane_mask_drs3d(panel, ("decile_d47", "not_leading_d", "moderate"))
        self.assertTrue(m.iloc[0])
        self.assertFalse(m.iloc[1])
        self.assertFalse(m.iloc[2])

    def test_yearly_lane_stats(self) -> None:
        panel = self._synthetic_panel(60)
        yearly = compute_yearly_lane_stats(panel, ("flat", "b1", "disp"))
        self.assertGreater(len(yearly), 0)
        self.assertIn("year", yearly[0])
        self.assertIn("mean_excess_3d", yearly[0])

    def test_walk_forward_lane_smoke(self) -> None:
        panel = self._synthetic_panel(200)
        wf = evaluate_lane_walk_forward(panel, ("lead3", "flat", "b1", "disp"))
        self.assertIn("oos_pooled", wf)
        self.assertIn("folds", wf)

    def test_deep_dive_and_verdict_smoke(self) -> None:
        panel = self._synthetic_panel(200)
        deep = evaluate_deep_dive(panel, years_span=5.0)
        verdict = compute_go_verdict(deep)
        self.assertIn(verdict["verdict"], ("GO", "WEAK", "NO-GO"))
        self.assertIn("abandon_h_b1_d10_track", verdict)
        self.assertGreater(len(deep["lanes"]), 0)


if __name__ == "__main__":
    unittest.main()
