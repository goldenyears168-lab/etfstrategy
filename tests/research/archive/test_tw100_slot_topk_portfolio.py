"""TW100 slot Top-K portfolio unit tests."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.archive.tw100_slot_topk_portfolio import (
    benchmark_periods_from_signals,
    periods_from_pred_panel,
)


class Tw100SlotTopkTests(unittest.TestCase):
    def test_periods_from_pred(self) -> None:
        cal = [f"2024-01-{d:02d}" for d in range(2, 12)]
        idx = pd.MultiIndex.from_product(
            [["2024-01-02"], ["2330", "2317", "2454", "2881"]],
            names=["datetime", "instrument"],
        )
        pred = pd.Series([0.4, 0.3, 0.2, 0.1], index=idx)
        periods = periods_from_pred_panel(pred, cal, top_k=2, min_cross_section=2)
        self.assertEqual(len(periods), 2)
        self.assertEqual(periods[0]["entry_date"], "2024-01-03")
        self.assertEqual(periods[0]["exit_date"], "2024-01-05")

    def test_benchmark_periods_dedupe(self) -> None:
        cal = [f"2024-01-{d:02d}" for d in range(2, 12)]
        out = benchmark_periods_from_signals(
            ["2024-01-02", "2024-01-02", "2024-01-03"], cal, "0050"
        )
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
