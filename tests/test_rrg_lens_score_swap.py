"""Tests for rrg_lens_score_swap backtest helpers."""

from __future__ import annotations

import unittest

from research.backtest.rrg_lens_score_swap import (
    SwapConfig,
    _combined_scores,
    _effective_gate,
    _passes_gate,
    _rebalance_minutes,
    _swap_threshold,
)


class TestRrgLensScoreSwapHelpers(unittest.TestCase):
    def test_rebalance_minutes(self) -> None:
        mins = _rebalance_minutes(interval_min=15, no_swap_before="09:30")
        self.assertEqual(mins[0], "09:30")
        self.assertIn("13:30", mins)

    def test_passes_gate_lens_only(self) -> None:
        self.assertTrue(
            _passes_gate(
                "lens_only",
                in_pool=True,
                prior_lens={"signal_convergence": 0},
                rrg_today={},
                rrg_yesterday=None,
            )
        )
        self.assertFalse(
            _passes_gate(
                "lens_only",
                in_pool=False,
                prior_lens=None,
                rrg_today={},
                rrg_yesterday=None,
            )
        )

    def test_combined_scores_alpha_extremes(self) -> None:
        daily = {"A": 10.0, "B": 20.0}
        intra = {"A": 100.0, "B": 0.0}
        all_daily = _combined_scores(["A", "B"], alpha=0.0, daily=daily, intraday=intra)
        all_intra = _combined_scores(["A", "B"], alpha=1.0, daily=daily, intraday=intra)
        self.assertGreater(all_intra["A"], all_intra["B"])
        self.assertGreater(all_daily["B"], all_daily["A"])

    def test_swap_threshold(self) -> None:
        held = {"a": 1.0, "b": 2.0, "c": 3.0}
        self.assertEqual(_swap_threshold(held, "beat_held_best"), 3.0)
        self.assertEqual(_swap_threshold(held, "beat_held_worst"), 1.0)

    def test_effective_gate_dual(self) -> None:
        cfg = SwapConfig(candidate_gate="tier2", entry_gate="tier2", swap_gate="lens_only")
        self.assertEqual(_effective_gate(cfg, role="entry"), "tier2")
        self.assertEqual(_effective_gate(cfg, role="swap"), "lens_only")
        cfg = SwapConfig(alpha=0.8, candidate_gate="mono_tier2", entry_gate="tier2", swap_gate="lens_only")
        d = cfg.to_dict()
        self.assertEqual(d["entry_gate"], "tier2")
        self.assertEqual(d["swap_gate"], "lens_only")


if __name__ == "__main__":
    unittest.main()
