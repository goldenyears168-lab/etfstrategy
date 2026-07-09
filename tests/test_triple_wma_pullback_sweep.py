"""Tests for triple_wma_pullback_sweep pattern matchers."""

from __future__ import annotations

import unittest

from research.backtest.triple_wma_pullback_sweep import (
    matches_structural_w20_w5,
    matches_triple_w3_dip,
    matches_triple_w3_rv_high_low,
    matches_w2_improving,
    matches_w2_rv_mv_band,
    matches_w2_rv_threshold,
    matches_w2_sweet_spot,
    matches_w3_rv_hl_w2_sweet,
    matches_w3_rv_hl_abc_v2,
    matches_w3_rv_hl_abc_v3,
    matches_w3_rv_hl_abc_v3_f1,
    matches_w5_mv_lte_w3_mv,
    W3_ABC_V3_W20_RV_MAX,
)


class TestTripleWmaPullbackPatterns(unittest.TestCase):
    def test_triple_w3_dip(self) -> None:
        p = {
            "fresh": True,
            "w20": {"quadrant": "leading", "rs_ratio": 102.0},
            "w5": {"quadrant": "leading", "rs_ratio": 101.0},
            "w3": {"quadrant": "lagging", "rs_ratio": 99.0},
        }
        self.assertTrue(matches_triple_w3_dip(p))
        self.assertTrue(matches_triple_w3_dip(p, leading_only=True))

    def test_triple_rejects_w5_weak(self) -> None:
        p = {
            "fresh": True,
            "w20": {"quadrant": "leading"},
            "w5": {"quadrant": "lagging"},
            "w3": {"quadrant": "lagging"},
        }
        self.assertFalse(matches_triple_w3_dip(p))

    def test_rv_high_low(self) -> None:
        p = {
            "fresh": True,
            "w20": {"rs_ratio": 101.0},
            "w5": {"rs_ratio": 100.5},
            "w3": {"rs_ratio": 99.5},
        }
        self.assertTrue(matches_triple_w3_rv_high_low(p))

    def test_structural_micro_w3(self) -> None:
        from research.backtest.triple_wma_pullback_sweep import (
            matches_micro_trigger,
            matches_structural_w20_w5,
        )

        p = {
            "fresh": True,
            "w20": {"quadrant": "leading", "rs_ratio": 102.0},
            "w5": {"quadrant": "improving", "rs_ratio": 101.0},
            "w3": {"quadrant": "lagging", "rs_ratio": 99.0},
            "w2": {"quadrant": "leading", "rs_ratio": 101.0},
        }
        self.assertTrue(matches_structural_w20_w5(p))
        self.assertTrue(matches_micro_trigger(p, micro_length=3))
        self.assertFalse(matches_micro_trigger(p, micro_length=2))

    def test_w2_sweet_and_w3_combo(self) -> None:
        p = {
            "fresh": True,
            "w20": {"rs_ratio": 102.0, "quadrant": "leading"},
            "w5": {"rs_ratio": 101.0, "quadrant": "improving"},
            "w3": {"rs_ratio": 99.0, "quadrant": "lagging"},
            "w2": {"rs_ratio": 99.8, "rs_momentum": 100.5, "quadrant": "improving"},
        }
        self.assertTrue(matches_w2_sweet_spot(p))
        self.assertTrue(matches_triple_w3_rv_high_low(p))
        self.assertTrue(matches_w3_rv_hl_w2_sweet(p))
        p_bad = {**p, "w2": {"rs_ratio": 98.0, "rs_momentum": 100.5, "quadrant": "improving"}}
        self.assertFalse(matches_w3_rv_hl_w2_sweet(p_bad))

    def test_w3_abc_v2_matcher(self) -> None:
        p = {
            "fresh": True,
            "w20": {"quadrant": "leading", "rs_ratio": 104.0},
            "w5": {"quadrant": "weakening", "rs_ratio": 100.8},
            "w3": {"rs_ratio": 99.0, "quadrant": "lagging"},
        }
        self.assertTrue(matches_w3_rv_hl_abc_v2(p))
        self.assertFalse(
            matches_w3_rv_hl_abc_v2({**p, "w5": {"quadrant": "leading", "rs_ratio": 100.8}})
        )
        self.assertFalse(
            matches_w3_rv_hl_abc_v2({**p, "w20": {"quadrant": "leading", "rs_ratio": 106.0}})
        )

    def test_w3_abc_v3_matcher(self) -> None:
        p = {
            "fresh": True,
            "spread_class": "pullback",
            "spread_dist": 2.0,
            "spread_rs": -1.0,
            "spread_mom": -1.0,
            "trade_signal": "lead_pullback_buy",
            "w20": {"quadrant": "leading", "rs_ratio": 103.0},
            "w5": {"quadrant": "weakening", "rs_ratio": 100.8},
            "w3": {"rs_ratio": 99.0, "quadrant": "lagging"},
        }
        self.assertTrue(matches_w3_rv_hl_abc_v3(p))
        self.assertFalse(
            matches_w3_rv_hl_abc_v3({**p, "w20": {"quadrant": "leading", "rs_ratio": 105.0}})
        )
        self.assertFalse(matches_w3_rv_hl_abc_v3({**p, "trade_signal": None}))
        self.assertTrue(
            matches_w3_rv_hl_abc_v3({**p, "trade_signal": None}, require_lead_pullback=False)
        )
        self.assertFalse(
            matches_w3_rv_hl_abc_v3(
                {**p, "w20": {"quadrant": "leading", "rs_ratio": W3_ABC_V3_W20_RV_MAX + 0.1}}
            )
        )

    def test_w3_abc_v3_f1_matcher(self) -> None:
        p = {
            "fresh": True,
            "spread_class": "pullback",
            "spread_dist": 2.0,
            "spread_rs": -1.0,
            "spread_mom": -1.0,
            "trade_signal": "lead_pullback_buy",
            "w20": {"quadrant": "leading", "rs_ratio": 103.0, "rs_momentum": 102.0},
            "w5": {"quadrant": "weakening", "rs_ratio": 100.8, "rs_momentum": 99.5},
            "w3": {"rs_ratio": 99.0, "quadrant": "lagging", "rs_momentum": 100.0},
        }
        self.assertTrue(matches_w3_rv_hl_abc_v3_f1(p))
        self.assertFalse(
            matches_w3_rv_hl_abc_v3_f1(
                {
                    **p,
                    "w5": {"quadrant": "weakening", "rs_ratio": 100.8, "rs_momentum": 101.0},
                }
            )
        )
        self.assertTrue(matches_w5_mv_lte_w3_mv(p))
        self.assertFalse(
            matches_w5_mv_lte_w3_mv(
                {
                    **p,
                    "w5": {"quadrant": "weakening", "rs_ratio": 100.8, "rs_momentum": 100.5},
                    "w3": {"rs_ratio": 99.0, "quadrant": "lagging", "rs_momentum": 100.0},
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
