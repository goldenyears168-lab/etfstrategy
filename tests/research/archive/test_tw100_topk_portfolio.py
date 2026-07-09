"""TW100 Top-K portfolio unit tests."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.archive.tw100_topk_portfolio import (
    daily_topk_portfolio_from_panels,
    summarize_portfolio_daily,
)


class Tw100TopkPortfolioTests(unittest.TestCase):
    def test_daily_topk_equal_weight(self) -> None:
        idx = pd.MultiIndex.from_product(
            [["2024-01-02"], ["2330", "2317", "2454", "2881"]],
            names=["datetime", "instrument"],
        )
        pred = pd.Series([0.4, 0.3, 0.2, 0.1], index=idx)
        label = pd.Series([0.10, 0.08, 0.06, 0.04], index=idx)
        rows = daily_topk_portfolio_from_panels(pred, label, k=2, min_cross_section=2)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["ret"], 0.09, places=6)

    def test_summarize_with_benchmark(self) -> None:
        daily = [
            {"date": "2024-01-02", "ret": 0.01, "turnover": 0.5},
            {"date": "2024-01-03", "ret": 0.02, "turnover": 0.2},
        ]
        bench = {"2024-01-02": 0.005, "2024-01-03": 0.01}
        s = summarize_portfolio_daily(daily, bench, cost_bps=10.0)
        self.assertEqual(s["n_days"], 2)
        self.assertIsNotNone(s["sharpe"])
        self.assertIsNotNone(s["mean_excess_vs_benchmark"])


if __name__ == "__main__":
    unittest.main()
