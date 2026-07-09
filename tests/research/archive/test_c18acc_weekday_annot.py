"""Tests for c18acc_weekday_annot helpers."""

from __future__ import annotations

import unittest

from research.backtest.archive.c18acc_weekday_annot import (
    _compute_weekend_gap_pct,
    _friday_vs_non_friday,
    _iso_weekday,
)


class TestWeekdayAnnot(unittest.TestCase):
    def test_iso_weekday_monday(self) -> None:
        self.assertEqual(_iso_weekday("2026-01-05"), 0)
        self.assertEqual(_iso_weekday("2026-01-09"), 4)

    def test_friday_vs_non_friday_delta(self) -> None:
        legs = [
            {"entry_weekday": 4, "excess_pct": 2.0, "return_pct": 3.0, "beat_bench": True, "gross_win": True},
            {"entry_weekday": 4, "excess_pct": 1.0, "return_pct": 2.0, "beat_bench": True, "gross_win": True},
            {"entry_weekday": 0, "excess_pct": -1.0, "return_pct": 0.0, "beat_bench": False, "gross_win": False},
        ]
        out = _friday_vs_non_friday(legs, "entry_weekday")
        self.assertEqual(out["friday_n"], 2)
        self.assertEqual(out["non_friday_n"], 1)
        self.assertEqual(out["delta_excess_pp"], 2.5)
        self.assertFalse(out["h_wd_0_pass"])

    def test_weekend_gap_single_week(self) -> None:
        import pandas as pd

        full_dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
        close = pd.DataFrame({"2330": [100.0, 101.0, 102.0, 103.0, 104.0]}, index=full_dates)
        opn = pd.DataFrame({"2330": [100.0, 101.0, 102.0, 103.0, 105.0]}, index=full_dates)
        held, gap, n = _compute_weekend_gap_pct(
            stock_id="2330",
            entry_date="2026-01-05",
            exit_date="2026-01-09",
            full_dates=full_dates,
            close=close,
            opn=opn,
        )
        self.assertFalse(held)
        self.assertIsNone(gap)
        self.assertEqual(n, 0)

        held2, gap2, n2 = _compute_weekend_gap_pct(
            stock_id="2330",
            entry_date="2026-01-05",
            exit_date="2026-01-12",
            full_dates=["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12"],
            close=pd.concat(
                [
                    close,
                    pd.DataFrame({"2330": [106.0]}, index=["2026-01-12"]),
                ]
            ),
            opn=pd.concat(
                [
                    opn,
                    pd.DataFrame({"2330": [107.0]}, index=["2026-01-12"]),
                ]
            ),
        )
        self.assertTrue(held2)
        self.assertAlmostEqual(gap2, 2.8846, places=2)
        self.assertEqual(n2, 1)


if __name__ == "__main__":
    unittest.main()
