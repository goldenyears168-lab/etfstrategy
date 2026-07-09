"""TW100 monthly WFA unit tests."""

from __future__ import annotations

import sqlite3
import unittest

import pandas as pd

from research.backtest.archive.tw100_monthly_wfa import (
    build_walk_forward_monthly_folds,
    forward_month_end_return,
    month_end_trading_dates,
    monthly_excess_label,
    momentum_features,
)
from research_config import load_research_splits


class Tw100MonthlyWfaTests(unittest.TestCase):
    def test_month_end_trading_dates(self) -> None:
        dates = ["2024-01-02", "2024-01-15", "2024-01-31", "2024-02-01", "2024-02-29"]
        me = month_end_trading_dates(dates)
        self.assertEqual(me, ["2024-01-31", "2024-02-29"])

    def test_forward_month_end_return(self) -> None:
        idx = ["2024-01-31", "2024-02-29", "2024-03-29"]
        panel = pd.DataFrame({"A": [100.0, 110.0, 121.0]}, index=idx)
        ret = forward_month_end_return(panel, idx)
        self.assertAlmostEqual(float(ret.loc["2024-01-31", "A"]), 0.1, places=6)
        self.assertAlmostEqual(float(ret.loc["2024-02-29", "A"]), 0.1, places=6)

    def test_monthly_excess_label(self) -> None:
        idx = ["2024-01-31", "2024-02-29", "2024-03-29"]
        panel = pd.DataFrame({"A": [100.0, 120.0, 132.0]}, index=idx)
        bench = pd.Series([1000.0, 1050.0, 1102.5], index=idx)
        excess = monthly_excess_label(panel, bench, idx)
        # stock +20%, bench +5% -> excess ~15%
        self.assertAlmostEqual(float(excess.loc["2024-01-31", "A"]), 0.15, places=4)

    def test_build_monthly_folds(self) -> None:
        month_ends = [f"2020-{m:02d}-28" for m in range(1, 13)]
        month_ends += [f"2021-{m:02d}-28" for m in range(1, 13)]
        month_ends += [f"2022-{m:02d}-28" for m in range(1, 13)]
        month_ends += [f"2023-{m:02d}-28" for m in range(1, 13)]
        month_ends += [f"2024-{m:02d}-28" for m in range(1, 7)]
        folds = build_walk_forward_monthly_folds(
            month_ends, train_months=12, test_months=3, embargo_months=1, max_folds=2
        )
        self.assertEqual(len(folds), 2)
        self.assertEqual(folds[0].fold_id, 0)

    def test_split_spec_registered(self) -> None:
        spec = load_research_splits().get("tw100_monthly_wfa_36_6")
        assert spec is not None
        self.assertEqual(spec.method, "walk_forward_monthly")
        self.assertEqual(spec.train_months, 36)

    def test_momentum_features_shape(self) -> None:
        idx = pd.date_range("2024-01-01", periods=300, freq="B").astype(str)
        panel = pd.DataFrame({"A": range(300)}, index=idx, dtype=float)
        feats = momentum_features(panel)
        self.assertIn("mom_21d", feats)
        self.assertEqual(feats["mom_21d"].shape, panel.shape)


if __name__ == "__main__":
    unittest.main()
