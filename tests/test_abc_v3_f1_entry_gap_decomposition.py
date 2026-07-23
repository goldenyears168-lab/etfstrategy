"""Tests for abc_v3_f1_entry_gap_decomposition."""

from __future__ import annotations

import unittest

from research.backtest.abc_v3_f1_entry_gap_decomposition import (
    attach_within_between_components,
    gap_variance_decomposition,
    per_stock_within_correlations,
    _tercile_labels,
)


class TestWithinBetween(unittest.TestCase):
    def test_within_sums_to_raw(self) -> None:
        legs = [
            {
                "stock_id": "A",
                "gap_w20_w5_raw": 5.0,
                "gap_w5_w3_raw": 1.0,
                "return_pct": 3.0,
            },
            {
                "stock_id": "A",
                "gap_w20_w5_raw": 3.0,
                "gap_w5_w3_raw": -1.0,
                "return_pct": 1.0,
            },
            {
                "stock_id": "B",
                "gap_w20_w5_raw": 8.0,
                "gap_w5_w3_raw": 0.0,
                "return_pct": 5.0,
            },
        ]
        out = attach_within_between_components(legs)
        # A mean gap20 = 4.0
        self.assertAlmostEqual(out[0]["gap_w20_w5_within"], 1.0)
        self.assertAlmostEqual(out[1]["gap_w20_w5_within"], -1.0)
        self.assertAlmostEqual(out[0]["gap_w20_w5_between"], 4.0)
        # within + between reconstructs raw
        self.assertAlmostEqual(
            out[0]["gap_w20_w5_within"] + out[0]["gap_w20_w5_between"],
            out[0]["gap_w20_w5_raw"],
        )


class TestVarianceDecomposition(unittest.TestCase):
    def test_between_dominates_when_stocks_differ(self) -> None:
        legs = [
            {"stock_id": "A", "gap_w20_w5_raw": 2.0},
            {"stock_id": "A", "gap_w20_w5_raw": 2.5},
            {"stock_id": "B", "gap_w20_w5_raw": 8.0},
            {"stock_id": "B", "gap_w20_w5_raw": 8.5},
        ]
        v = gap_variance_decomposition(legs, col="gap_w20_w5_raw")
        self.assertFalse(v["skipped"])
        self.assertGreater(v["icc_between_share"], 0.9)

    def test_within_dominates_when_same_stock_varies(self) -> None:
        legs = [
            {"stock_id": "A", "gap_w20_w5_raw": 1.0},
            {"stock_id": "A", "gap_w20_w5_raw": 9.0},
            {"stock_id": "B", "gap_w20_w5_raw": 1.5},
            {"stock_id": "B", "gap_w20_w5_raw": 8.5},
        ]
        v = gap_variance_decomposition(legs, col="gap_w20_w5_raw")
        self.assertGreater(v["icc_within_share"], 0.5)


class TestPerStockWithinMeta(unittest.TestCase):
    def test_positive_within_stock_correlation(self) -> None:
        legs = []
        for i in range(5):
            legs.append(
                {
                    "stock_id": "A",
                    "gap_w20_w5_within": float(i),
                    "return_pct": float(i) * 2,
                }
            )
        m = per_stock_within_correlations(legs, min_events=3)
        self.assertFalse(m["skipped"])
        self.assertAlmostEqual(m["mean_rho"], 1.0, places=2)


class TestTercileLabels(unittest.TestCase):
    def test_splits_three_groups(self) -> None:
        vals = {f"s{i}": float(i) for i in range(9)}
        labels = _tercile_labels(vals)
        self.assertEqual(len(set(labels.values())), 3)


if __name__ == "__main__":
    unittest.main()
