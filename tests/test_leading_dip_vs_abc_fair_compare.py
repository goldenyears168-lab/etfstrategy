"""Unit tests for fair-compare pure helpers (no DB)."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.leading_dip_vs_abc_fair_compare import (
    apply_cost_haircut,
    decide_fair_verdict,
    deep_enough_proxy,
    passes_arm_a_layer,
    summarize_fair_arm,
)


class TestArmAFilters(unittest.TestCase):
    def test_a0_passthrough(self) -> None:
        self.assertTrue(
            passes_arm_a_layer("A0", is_leading=False, not_ur=False, w5_weak=False)
        )

    def test_layers(self) -> None:
        self.assertFalse(
            passes_arm_a_layer("A1", is_leading=False, not_ur=True, w5_weak=True)
        )
        self.assertTrue(
            passes_arm_a_layer("A1", is_leading=True, not_ur=False, w5_weak=False)
        )
        self.assertFalse(
            passes_arm_a_layer("A2", is_leading=True, not_ur=False, w5_weak=True)
        )
        self.assertTrue(
            passes_arm_a_layer("A2", is_leading=True, not_ur=True, w5_weak=False)
        )
        self.assertFalse(
            passes_arm_a_layer("A3", is_leading=True, not_ur=True, w5_weak=False)
        )
        self.assertTrue(
            passes_arm_a_layer("A3", is_leading=True, not_ur=True, w5_weak=True)
        )

    def test_a4_needs_depth(self) -> None:
        self.assertFalse(
            passes_arm_a_layer(
                "A4", is_leading=True, not_ur=True, w5_weak=True, deep_enough=None
            )
        )
        self.assertTrue(
            passes_arm_a_layer(
                "A4", is_leading=True, not_ur=True, w5_weak=True, deep_enough=True
            )
        )


class TestCostAndDepth(unittest.TestCase):
    def test_haircut(self) -> None:
        px, ex = apply_cost_haircut(5.0, 3.0, haircut_pp=0.6)
        self.assertAlmostEqual(px or 0, 4.4)
        self.assertAlmostEqual(ex or 0, 2.4)

    def test_deep_enough(self) -> None:
        self.assertTrue(deep_enough_proxy(excess0=-4.1))
        self.assertTrue(deep_enough_proxy(gap3=-0.9))
        self.assertFalse(deep_enough_proxy(excess0=-3.0, gap3=-0.5))
        self.assertIsNone(deep_enough_proxy())


class TestSummarizeAndVerdict(unittest.TestCase):
    def test_summarize(self) -> None:
        df = pd.DataFrame(
            {
                "date": ["2025-06-01", "2025-11-01", "2025-11-02"],
                "px_net": [1.0, -0.5, 2.0],
                "ex_net": [2.0, -1.0, 3.0],
            }
        )
        s = summarize_fair_arm(df, oos_cut="2025-10-01", n_trading_days=100)
        self.assertEqual(s["n"], 3)
        self.assertEqual(s["oos_n"], 2)
        self.assertAlmostEqual(s["hit_ex"], 2 / 3)
        self.assertEqual(s["per_year"]["2025"], 3)

    def test_verdict_tighten(self) -> None:
        arm_a = {
            "A0": {"n": 400, "hit_ex": 0.5, "med_ex": 0.0},
            "A3": {"n": 200, "hit_ex": 0.78, "med_ex": 5.5},
        }
        arm_b = {
            "B0": {"n": 80, "hit_ex": 0.76, "med_ex": 6.0, "oos_hit_ex": 0.77, "oos_med_ex": 6.0},
            "B0_slots": {"n": 80, "hit_ex": 0.76, "med_ex": 6.0},
        }
        v = decide_fair_verdict(arm_a, arm_b)
        self.assertEqual(v["choice"], "tighten_abc")

    def test_verdict_dual_when_a_starves(self) -> None:
        arm_a = {
            "A0": {"n": 400, "hit_ex": 0.5, "med_ex": 0.0},
            "A3": {"n": 5, "hit_ex": 0.4, "med_ex": 0.5},
        }
        arm_b = {
            "B0": {
                "n": 80,
                "hit_ex": 0.76,
                "med_ex": 6.0,
                "oos_hit_ex": 0.77,
                "oos_med_ex": 6.0,
            },
            "B1": {
                "n": 90,
                "hit_ex": 0.70,
                "med_ex": 4.0,
                "oos_hit_ex": 0.65,
                "oos_med_ex": 3.0,
            },
            "B2": {
                "n": 100,
                "hit_ex": 0.68,
                "med_ex": 3.5,
                "oos_hit_ex": 0.60,
                "oos_med_ex": 2.0,
            },
        }
        v = decide_fair_verdict(arm_a, arm_b)
        self.assertIn(v["choice"], {"dual_track", "hold_observe", "widen_dip_mild"})
        self.assertTrue(v["reject_bare_600"])


if __name__ == "__main__":
    unittest.main()
