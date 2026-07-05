"""Tests for drs/extension struct force exit helpers."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    drs_neg_streak_days,
    spread_lost_streak_days,
    struct_force_exit_eligible,
)


class TestStructForceExit(unittest.TestCase):
    def test_drs_neg_streak(self) -> None:
        idx = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        rs = pd.DataFrame({"2330": [100.0, 100.5, 100.4, 100.2]}, index=idx)
        streak = drs_neg_streak_days(
            rs, idx, as_of="2024-01-05", stock_id="2330", entry_date="2024-01-02"
        )
        self.assertEqual(streak, 2)

    def test_spread_lost_streak(self) -> None:
        idx = ["2024-01-02", "2024-01-03", "2024-01-04"]
        spread = pd.DataFrame({"2330": [1.0, -0.5, -0.2]}, index=idx)
        streak = spread_lost_streak_days(
            spread, idx, as_of="2024-01-04", stock_id="2330", entry_date="2024-01-02"
        )
        self.assertEqual(streak, 2)

    def test_struct_eligible_respects_loss_gate(self) -> None:
        cfg = ScoreSwapCConfig(
            struct_force_exit_mode="drs_neg",
            struct_force_exit_min_days=2,
            struct_force_exit_loss_pct=-0.05,
        )
        self.assertTrue(
            struct_force_exit_eligible(
                cfg, hold_days=6, effective_min_hold=5, streak_days=2, unrealized_ret=-0.06
            )
        )
        self.assertFalse(
            struct_force_exit_eligible(
                cfg, hold_days=6, effective_min_hold=5, streak_days=2, unrealized_ret=-0.02
            )
        )


if __name__ == "__main__":
    unittest.main()
