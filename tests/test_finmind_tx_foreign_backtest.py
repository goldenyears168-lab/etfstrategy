"""FinMind TX foreign net long 3d · unit tests."""
from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.finmind_tx_foreign_common import streak_signals


class StreakSignalTests(unittest.TestCase):
    def test_three_day_streak(self) -> None:
        net = pd.Series([1.0, 2.0, 3.0, -1.0, 4.0, 5.0, 6.0], index=[f"2024-01-0{i}" for i in range(1, 8)])
        sig = streak_signals(net, streak_days=3)
        self.assertTrue(bool(sig.iloc[2]))
        self.assertTrue(bool(sig.iloc[6]))
        self.assertFalse(bool(sig.iloc[3]))


class ValidateSmokeTests(unittest.TestCase):
    def test_validate_runs(self) -> None:
        from stock_db import DEFAULT_DB_PATH
        from research.backtest.finmind_tx_foreign_common import validate_tx_foreign_data

        if not DEFAULT_DB_PATH.is_file():
            self.skipTest("no stocks.db")
        out = validate_tx_foreign_data(date_start="2024-01-01", date_end="2024-12-31")
        self.assertGreater(out.get("e1_streak_3d_signals", 0), 0)


if __name__ == "__main__":
    unittest.main()
