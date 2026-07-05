"""Tests for C18acc rotation-exit · Track B2 selective force-exit logic."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_rotation_selective_exit_sweep import (
    _pick_best_selective,
    rotation_selective_exit_sweep_configs,
)
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    quad_force_exit_eligible,
)


class TestQuadForceExitLossGate(unittest.TestCase):
    def _cfg(self, **kwargs) -> ScoreSwapCConfig:
        return ScoreSwapCConfig(
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=2,
            quad_force_exit_requires_min_hold=False,
            **kwargs,
        )

    def test_no_loss_gate_when_none(self) -> None:
        cfg = self._cfg(quad_force_exit_loss_pct=None)
        self.assertTrue(
            quad_force_exit_eligible(
                cfg,
                hold_days=5,
                effective_min_hold=5,
                streak_days=2,
                unrealized_ret=0.02,
            )
        )

    def test_loss_gate_blocks_small_loss(self) -> None:
        cfg = self._cfg(quad_force_exit_loss_pct=-0.03)
        self.assertFalse(
            quad_force_exit_eligible(
                cfg,
                hold_days=5,
                effective_min_hold=5,
                streak_days=2,
                unrealized_ret=-0.02,
            )
        )

    def test_loss_gate_allows_deep_loss(self) -> None:
        cfg = self._cfg(quad_force_exit_loss_pct=-0.03)
        self.assertTrue(
            quad_force_exit_eligible(
                cfg,
                hold_days=5,
                effective_min_hold=5,
                streak_days=2,
                unrealized_ret=-0.05,
            )
        )

    def test_loss_gate_exact_threshold(self) -> None:
        cfg = self._cfg(quad_force_exit_loss_pct=-0.03)
        self.assertTrue(
            quad_force_exit_eligible(
                cfg,
                hold_days=5,
                effective_min_hold=5,
                streak_days=2,
                unrealized_ret=-0.03,
            )
        )

    def test_loss_gate_requires_unrealized_ret(self) -> None:
        cfg = self._cfg(quad_force_exit_loss_pct=-0.03)
        self.assertFalse(
            quad_force_exit_eligible(
                cfg,
                hold_days=5,
                effective_min_hold=5,
                streak_days=2,
                unrealized_ret=None,
            )
        )


class TestRotationSelectiveExitSweepConfigs(unittest.TestCase):
    def test_variant_ids(self) -> None:
        ids = [c.variant_id for c in rotation_selective_exit_sweep_configs()]
        self.assertEqual(ids, ["S0", "S1", "S2", "S3", "S4", "S5", "S7"])

    def test_s1_weakening_2d_loss_3pct(self) -> None:
        s1 = next(c for c in rotation_selective_exit_sweep_configs() if c.variant_id == "S1")
        self.assertEqual(s1.quad_force_exit_mode, "weakening")
        self.assertEqual(s1.quad_force_exit_min_days, 2)
        self.assertEqual(s1.quad_force_exit_loss_pct, -0.03)

    def test_s7_lagging_1d_loss_3pct(self) -> None:
        s7 = next(c for c in rotation_selective_exit_sweep_configs() if c.variant_id == "S7")
        self.assertEqual(s7.quad_force_exit_mode, "lagging")
        self.assertEqual(s7.quad_force_exit_min_days, 1)
        self.assertEqual(s7.quad_force_exit_loss_pct, -0.03)

    def test_s0_no_force_exit(self) -> None:
        s0 = next(c for c in rotation_selective_exit_sweep_configs() if c.variant_id == "S0")
        self.assertEqual(s0.quad_force_exit_mode, "none")
        self.assertIsNone(s0.quad_force_exit_loss_pct)


class TestPickBestSelective(unittest.TestCase):
    def test_picks_highest_rotation_delta(self) -> None:
        rows = [
            {"variant_id": "S0", "rotation_cohort_delta_mean_return_pp": 0.0},
            {"variant_id": "S1", "rotation_cohort_delta_mean_return_pp": 0.5, "mean_excess_pct": 4.0},
            {"variant_id": "S3", "rotation_cohort_delta_mean_return_pp": 1.2, "mean_excess_pct": 3.8},
            {"variant_id": "S7", "rotation_cohort_delta_mean_return_pp": 0.8, "mean_excess_pct": 4.5},
        ]
        best = _pick_best_selective(rows)
        self.assertIsNotNone(best)
        assert best is not None
        self.assertEqual(best["variant_id"], "S3")


if __name__ == "__main__":
    unittest.main()
