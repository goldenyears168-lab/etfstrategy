"""Tests for dual_wma_short_length_sweep."""

from __future__ import annotations

import unittest

from research.backtest.archive.dual_wma_short_length_sweep import (
    verdict_short_length_sweep,
)


class TestDualWmaShortLengthSweep(unittest.TestCase):
    def test_verdict_prefers_wma5_when_noisier_w3(self) -> None:
        daily = [
            {
                "short_length": 3,
                "quadrant_flip_rate_pct": 45.0,
                "boundary_stock_day_pct": 12.0,
                "false_reduction": {"false_reduction_pct": 18.0},
                "w20_weakening_lead": {"short_weak_prior_day_pct": 55.0},
                "spurious_short_weak": {"spurious_short_weak_pct": 8.0},
            },
            {
                "short_length": 5,
                "quadrant_flip_rate_pct": 28.0,
                "boundary_stock_day_pct": 8.0,
                "false_reduction": {"false_reduction_pct": 12.0},
                "w20_weakening_lead": {"short_weak_prior_day_pct": 50.0},
                "spurious_short_weak": {"spurious_short_weak_pct": 5.0},
            },
        ]
        v = verdict_short_length_sweep(daily)
        self.assertIn(v["recommend_fast_chart"], ("WMA5", "WMA5 (marginal)"))
        self.assertGreater(v["score_wma5"], v["score_wma3"])

    def test_verdict_can_pick_wma3_with_clear_edge(self) -> None:
        daily = [
            {
                "short_length": 3,
                "quadrant_flip_rate_pct": 25.0,
                "boundary_stock_day_pct": 6.0,
                "false_reduction": {"false_reduction_pct": 10.0},
                "w20_weakening_lead": {"short_weak_prior_day_pct": 70.0},
                "spurious_short_weak": {"spurious_short_weak_pct": 4.0},
            },
            {
                "short_length": 5,
                "quadrant_flip_rate_pct": 28.0,
                "boundary_stock_day_pct": 8.0,
                "false_reduction": {"false_reduction_pct": 12.0},
                "w20_weakening_lead": {"short_weak_prior_day_pct": 50.0},
                "spurious_short_weak": {"spurious_short_weak_pct": 5.0},
            },
        ]
        v = verdict_short_length_sweep(daily)
        self.assertEqual(v["recommend_fast_chart"], "WMA3")


if __name__ == "__main__":
    unittest.main()
