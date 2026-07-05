"""Alpha158 LGBM WFA helper tests (no qlib required)."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.tw100_alpha158_lgbm_wfa import (
    daily_rank_ic_from_panel,
    _train_valid_segments,
)
from research.backtest.tw100_walk_forward import WalkForwardFold


class Tw100Alpha158HelperTests(unittest.TestCase):
    def test_train_valid_segments(self) -> None:
        cal = [f"2020-{m:02d}-01" for m in range(1, 13)]  # 12 fake months
        cal = pd.bdate_range("2019-01-01", periods=600).strftime("%Y-%m-%d").tolist()
        fold = WalkForwardFold(
            fold_id=0,
            train_start=cal[0],
            train_end=cal[503],
            embargo_start=cal[504],
            embargo_end=cal[510],
            test_start=cal[511],
            test_end=cal[599],
        )
        train, valid, test = _train_valid_segments(fold, cal, valid_ratio=0.2)
        self.assertEqual(train[0], fold.train_start)
        self.assertLess(train[1], valid[0])
        self.assertEqual(test[0], fold.test_start)

    def test_daily_rank_ic_from_panel(self) -> None:
        idx = pd.MultiIndex.from_product(
            [["2024-01-02", "2024-01-03"], ["2330", "2317", "2454"]],
            names=["datetime", "instrument"],
        )
        pred = pd.Series([0.1, 0.2, 0.3, 0.3, 0.2, 0.1], index=idx)
        label = pd.Series([0.05, 0.15, 0.25, 0.25, 0.15, 0.05], index=idx)
        series = daily_rank_ic_from_panel(pred, label, min_cross_section=3)
        self.assertEqual(len(series), 2)


if __name__ == "__main__":
    unittest.main()
