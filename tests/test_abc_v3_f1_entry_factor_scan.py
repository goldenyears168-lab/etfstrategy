"""Tests for abc_v3_f1_entry_factor_scan."""

from __future__ import annotations

import unittest

from research.backtest.abc_v3_f1_entry_factor_scan import (
    _percentile,
    _spearman,
    _trailing_window,
    _zscore,
    attach_normalized_gate_features,
    scan_factor_correlations,
)


class TestZscorePercentile(unittest.TestCase):
    def test_zscore_basic(self) -> None:
        trailing = [1.0, 2.0, 3.0, 4.0, 5.0]
        z = _zscore(3.0, trailing)
        self.assertAlmostEqual(z, 0.0, places=3)
        z_high = _zscore(5.0, trailing)
        self.assertGreater(z_high, 0)

    def test_zscore_needs_at_least_two_points(self) -> None:
        self.assertIsNone(_zscore(1.0, [1.0]))
        self.assertIsNone(_zscore(1.0, []))

    def test_zscore_zero_std_is_none(self) -> None:
        self.assertIsNone(_zscore(5.0, [3.0, 3.0, 3.0]))

    def test_percentile_basic(self) -> None:
        trailing = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(_percentile(5.0, trailing), 100.0)
        self.assertEqual(_percentile(0.0, trailing), 0.0)
        self.assertEqual(_percentile(3.0, trailing), 60.0)


class TestTrailingWindow(unittest.TestCase):
    def test_no_lookahead(self) -> None:
        series = [
            {"date": "2026-01-01", "gap_w20_w5_eod": 1.0},
            {"date": "2026-01-02", "gap_w20_w5_eod": 2.0},
            {"date": "2026-01-03", "gap_w20_w5_eod": 3.0},
        ]
        vals = _trailing_window(series, before_date="2026-01-02", window=60, key="gap_w20_w5_eod")
        self.assertEqual(vals, [1.0])

    def test_window_caps_length(self) -> None:
        series = [{"date": f"2026-01-{d:02d}", "gap_w20_w5_eod": float(d)} for d in range(1, 21)]
        vals = _trailing_window(series, before_date="2026-01-21", window=5, key="gap_w20_w5_eod")
        self.assertEqual(vals, [16.0, 17.0, 18.0, 19.0, 20.0])

    def test_missing_key_values_excluded(self) -> None:
        series = [
            {"date": "2026-01-01", "gap_w20_w5_eod": None},
            {"date": "2026-01-02", "gap_w20_w5_eod": 2.0},
        ]
        vals = _trailing_window(series, before_date="2026-01-03", window=60, key="gap_w20_w5_eod")
        self.assertEqual(vals, [2.0])


class TestSpearman(unittest.TestCase):
    def test_perfect_monotonic_is_one(self) -> None:
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertAlmostEqual(_spearman(xs, ys), 1.0, places=3)

    def test_inverse_monotonic_is_negative_one(self) -> None:
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [50.0, 40.0, 30.0, 20.0, 10.0]
        self.assertAlmostEqual(_spearman(xs, ys), -1.0, places=3)

    def test_ties_handled(self) -> None:
        xs = [1.0, 1.0, 2.0, 2.0]
        ys = [1.0, 2.0, 3.0, 4.0]
        rho = _spearman(xs, ys)
        self.assertIsNotNone(rho)


class TestAttachNormalizedGateFeatures(unittest.TestCase):
    def test_ratio_and_raw_variants_computed(self) -> None:
        legs = [
            {
                "stock_id": "1101",
                "entry_date": "2026-02-10",
                "gate_gap_w20_w5": 4.0,
                "gate_gap_w5_w3": 0.5,
                "gate_rv3_rebound": 0.6,
                "entry_t0": {"w20_mv": 104.0, "w5_mv": 100.0, "w3_mv": 99.5, "w3_rv": 99.4},
                "return_pct": 2.0,
            }
        ]
        baseline = {
            "1101": [
                {
                    "date": f"2026-01-{d:02d}",
                    "gap_w20_w5_eod": 3.0 + 0.1 * d,
                    "gap_w5_w3_eod": 0.2 + 0.01 * d,
                    "max_rv3_rebound_day": 0.3 + 0.02 * d,
                }
                for d in range(1, 16)
            ]
        }
        out = attach_normalized_gate_features(legs, baseline, window=60, min_trailing=10)
        leg = out[0]
        self.assertEqual(leg["gap_w20_w5_raw"], 4.0)
        self.assertIsNotNone(leg["gap_w20_w5_z"])
        self.assertIsNotNone(leg["gap_w20_w5_pct"])
        # ratio: (104/100 - 1) * 100 = 4.0
        self.assertAlmostEqual(leg["gap_w20_w5_ratio"], 4.0, places=3)
        # trough = 99.4 - 0.6 = 98.8; ratio = 0.6/98.8*100
        self.assertAlmostEqual(leg["rv3_rebound_ratio"], 0.6 / 98.8 * 100, places=3)

    def test_insufficient_trailing_history_yields_none(self) -> None:
        legs = [
            {
                "stock_id": "1101",
                "entry_date": "2026-02-10",
                "gate_gap_w20_w5": 4.0,
                "gate_gap_w5_w3": 0.5,
                "gate_rv3_rebound": 0.6,
                "entry_t0": {"w20_mv": 104.0, "w5_mv": 100.0, "w3_mv": 99.5, "w3_rv": 99.4},
                "return_pct": 2.0,
            }
        ]
        out = attach_normalized_gate_features(legs, {}, window=60, min_trailing=10)
        leg = out[0]
        self.assertIsNone(leg["gap_w20_w5_z"])
        self.assertIsNone(leg["gap_w20_w5_pct"])
        self.assertIsNotNone(leg["gap_w20_w5_ratio"])  # ratio doesn't need history


class TestScanFactorCorrelations(unittest.TestCase):
    def test_ranks_by_abs_spearman(self) -> None:
        legs = [
            {"strong_factor": float(i), "weak_factor": 0.001 * (i % 3), "return_pct": float(i) * 2}
            for i in range(1, 31)
        ]
        rows = scan_factor_correlations(
            legs, factor_cols=("strong_factor", "weak_factor"), min_n=5
        )
        self.assertEqual(rows[0]["factor"], "strong_factor")
        self.assertAlmostEqual(rows[0]["spearman_rho"], 1.0, places=2)

    def test_below_min_n_is_skipped(self) -> None:
        legs = [{"f": 1.0, "return_pct": 1.0}] * 3
        rows = scan_factor_correlations(legs, factor_cols=("f",), min_n=20)
        self.assertTrue(rows[0]["skipped"])


if __name__ == "__main__":
    unittest.main()
