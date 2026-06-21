"""VCP-TM calculator unit tests (ported from tradermonty execution_state / pivot logic)."""

from __future__ import annotations

import unittest

from vcp_tm.calculators.execution_state import apply_state_cap, compute_execution_state
from vcp_tm.calculators.pivot_proximity_calculator import calculate_pivot_proximity
from vcp_tm.scorer import calculate_composite_score


class VcpTmCalculatorTests(unittest.TestCase):
    def test_execution_state_pre_breakout_below_pivot(self):
        r = compute_execution_state(
            distance_from_pivot_pct=-3.0,
            price=97.0,
            sma50=90.0,
            sma200=85.0,
            sma200_distance_pct=14.0,
            last_contraction_low=92.0,
            breakout_volume=False,
        )
        self.assertEqual(r["state"], "Pre-breakout")

    def test_execution_state_breakout_with_volume(self):
        r = compute_execution_state(
            distance_from_pivot_pct=1.5,
            price=101.5,
            sma50=95.0,
            sma200=88.0,
            sma200_distance_pct=15.0,
            last_contraction_low=96.0,
            breakout_volume=True,
        )
        self.assertEqual(r["state"], "Breakout")

    def test_execution_state_damaged_below_stop(self):
        r = compute_execution_state(
            distance_from_pivot_pct=-2.0,
            price=90.0,
            sma50=95.0,
            sma200=88.0,
            sma200_distance_pct=2.0,
            last_contraction_low=92.0,
            breakout_volume=False,
        )
        self.assertEqual(r["state"], "Damaged")

    def test_state_cap_extended_limits_rating(self):
        capped, applied = apply_state_cap("Textbook VCP", "Extended")
        self.assertTrue(applied)
        self.assertEqual(capped, "Developing VCP")

    def test_pivot_proximity_breakout_confirmed(self):
        r = calculate_pivot_proximity(
            102.0, 100.0, last_contraction_low=95.0, breakout_volume=True
        )
        self.assertEqual(r["trade_status"], "BREAKOUT CONFIRMED")
        self.assertGreaterEqual(r["score"], 100)

    def test_composite_score_with_state_cap(self):
        r = calculate_composite_score(
            trend_score=95.0,
            contraction_score=90.0,
            volume_score=85.0,
            pivot_score=80.0,
            rs_score=75.0,
            valid_vcp=True,
            execution_state="Extended",
            pattern_type="Extended Leader",
        )
        self.assertGreater(r["composite_score"], 80)
        self.assertTrue(r["state_cap_applied"])
        self.assertEqual(r["rating"], "Developing VCP")


if __name__ == "__main__":
    unittest.main()
