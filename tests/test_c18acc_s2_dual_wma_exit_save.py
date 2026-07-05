"""Tests for W-EXIT save analysis."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_s2_dual_wma_exit_save import _summarize_save


class TestWExitSave(unittest.TestCase):
    def test_summarize_save(self) -> None:
        rows = [
            {"save_vs_orig_pct": 2.0, "excess_delta_pp": 1.0, "hold_days_saved": 3,
             "orig_return_pct": 5.0, "w5_return_pct": 3.0},
            {"save_vs_orig_pct": -1.0, "excess_delta_pp": -0.5, "hold_days_saved": 1,
             "orig_return_pct": 4.0, "w5_return_pct": 5.0},
        ]
        s = _summarize_save(rows)
        self.assertEqual(s["n"], 2)
        self.assertEqual(s["median_save_vs_orig_pct"], 0.5)
        self.assertEqual(s["pct_save_positive"], 50.0)


if __name__ == "__main__":
    unittest.main()
