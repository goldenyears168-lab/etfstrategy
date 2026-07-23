"""Tests for abc_v3_f1_entry_stock_pool_sweep."""

from __future__ import annotations

import unittest

from research.backtest.abc_v3_f1_entry_stock_pool_sweep import (
    StockPoolRule,
    _leg_passes_pool_rule,
    _tercile_labels,
    attach_stock_baseline_tiers,
)


class TestStockPoolTiers(unittest.TestCase):
    def test_tercile_split(self) -> None:
        vals = {f"s{i}": float(i) for i in range(9)}
        labels = _tercile_labels(vals)
        self.assertEqual(len(set(labels.values())), 3)

    def test_exclude_high(self) -> None:
        rule = StockPoolRule("x", "x", "gap_w5_w3_eod", "exclude_high")
        self.assertTrue(_leg_passes_pool_rule({"stock_tier_gap_w5_w3_eod": "low"}, rule))
        self.assertFalse(_leg_passes_pool_rule({"stock_tier_gap_w5_w3_eod": "high"}, rule))

    def test_attach_tiers(self) -> None:
        baseline = {
            "A": {"gap_w5_w3_eod": 0.5},
            "B": {"gap_w5_w3_eod": 2.0},
            "C": {"gap_w5_w3_eod": 5.0},
        }
        legs = [{"stock_id": "A"}, {"stock_id": "B"}, {"stock_id": "C"}]
        out, tiers = attach_stock_baseline_tiers(legs, baseline, keys=("gap_w5_w3_eod",))
        self.assertIn("stock_tier_gap_w5_w3_eod", out[0])
        self.assertEqual(len(tiers["gap_w5_w3_eod"]), 3)


if __name__ == "__main__":
    unittest.main()
