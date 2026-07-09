"""Tests for C18acc × ABC v3+f1 Phase1b event-observe sleeve."""

from __future__ import annotations

import unittest

from research.backtest.archive.c18acc_abc_dual_sleeve_phase1b import (
    peak_concurrent_slots,
    strip_abc_candidate_periods,
)
from research.backtest.archive.c18acc_abc_dual_sleeve_phase1 import (
    combine_fixed_weight_nav,
    phase1_verdict,
)


class TestPeakConcurrentSlots(unittest.TestCase):
    def test_overlapping_holds(self) -> None:
        periods = [
            {"entry_date": "2024-01-02", "exit_date": "2024-01-04"},
            {"entry_date": "2024-01-03", "exit_date": "2024-01-05"},
            {"entry_date": "2024-01-04", "exit_date": "2024-01-06"},
        ]
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-06"]
        self.assertEqual(peak_concurrent_slots(periods, dates), 3)

    def test_empty_periods_defaults_hold_days(self) -> None:
        self.assertGreaterEqual(peak_concurrent_slots([], ["2024-01-02"]), 3)


class TestStripPeriods(unittest.TestCase):
    def test_minimal_fields(self) -> None:
        rows = strip_abc_candidate_periods(
            [
                {
                    "stock_id": "2330",
                    "entry_date": "2024-01-02",
                    "exit_date": "2024-01-05",
                    "entry_px": 100.0,
                    "rank_score": 9.9,
                    "net_pct": 2.5,
                }
            ]
        )
        self.assertEqual(rows[0]["stock_id"], "2330")
        self.assertNotIn("rank_score", rows[0])


class TestEventObserveCombo(unittest.TestCase):
    def test_higher_abc_daily_lifts_combo_verdict(self) -> None:
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-06"]
        c18 = [{"date": d, "equity_ntd": 100_000 * (1.002 ** i)} for i, d in enumerate(dates)]
        abc = [{"date": d, "equity_ntd": 100_000 * (1.01 ** i)} for i, d in enumerate(dates)]
        combo_nav, _ = combine_fixed_weight_nav(
            dates, c18, abc, total_capital=100_000, c18_weight=0.7, abc_weight=0.3
        )
        c18_only_nav, _ = combine_fixed_weight_nav(
            dates, c18, abc, total_capital=100_000, c18_weight=1.0, abc_weight=0.0
        )
        from research.backtest.archive.c18acc_abc_dual_sleeve_phase1 import _metrics_from_nav_series

        combo_m = _metrics_from_nav_series(dates, combo_nav, total_capital=100_000)
        c18_m = _metrics_from_nav_series(dates, c18_only_nav, total_capital=100_000)
        combo_block = {"windows": {"FULL": combo_m, "OOS_H1": {}}}
        c18_block = {"windows": {"FULL": c18_m, "OOS_H1": {}}}
        v = phase1_verdict(combo_block, c18_block)
        self.assertTrue(v["adopt_dual_sleeve"])


if __name__ == "__main__":
    unittest.main()
