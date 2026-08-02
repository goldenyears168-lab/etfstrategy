"""FinMind MA20 breakout volume · unit tests."""
from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.finmind_ma20_breakout_common import (
    Ma20BreakoutParams,
    build_ma20_breakout_screen_matrices,
)


class BreakoutMatrixTests(unittest.TestCase):
    def test_strict_ma20_breakout(self) -> None:
        cal = ["2024-01-02", "2024-01-03", "2024-01-04"]
        close = pd.DataFrame(
            {"2330": [95.0, 98.0, 102.0]},
            index=cal,
        )
        ma20 = pd.DataFrame(
            {"2330": [100.0, 99.0, 100.0]},
            index=cal,
        )
        tech = pd.DataFrame(
            [
                {"stock_id": "2330", "trade_date": "2024-01-02", "return_1d_pct": 1.0, "ma20": 100.0, "vol_ratio_5d": 1.0},
                {"stock_id": "2330", "trade_date": "2024-01-03", "return_1d_pct": 3.0, "ma20": 99.0, "vol_ratio_5d": 1.5},
                {"stock_id": "2330", "trade_date": "2024-01-04", "return_1d_pct": 6.0, "ma20": 100.0, "vol_ratio_5d": 2.5},
            ]
        )
        matrices = build_ma20_breakout_screen_matrices(
            stock_ids=["2330"],
            cal=cal,
            close=close,
            tech=tech,
            params=Ma20BreakoutParams(),
        )
        self.assertFalse(bool(matrices.ma20_breakout.loc["2024-01-02", "2330"]))
        self.assertTrue(bool(matrices.ma20_breakout.loc["2024-01-04", "2330"]))
        full = (
            matrices.ma20_breakout.loc["2024-01-04", "2330"]
            and matrices.return_1d.loc["2024-01-04", "2330"]
            and matrices.vol_ratio.loc["2024-01-04", "2330"]
        )
        self.assertTrue(full)


class ValidateSmokeTests(unittest.TestCase):
    def test_validate_runs(self) -> None:
        from stock_db import DEFAULT_DB_PATH, connect
        from research.backtest.finmind_ma20_breakout_common import count_condition_passes

        if not DEFAULT_DB_PATH.is_file():
            self.skipTest("no stocks.db")
        import os
        if not os.environ.get("RUN_SLOW_DB_TESTS"):
            self.skipTest("slow real-DB smoke (~16s on mini); set RUN_SLOW_DB_TESTS=1 to run")
        conn = connect(DEFAULT_DB_PATH)
        try:
            out = count_condition_passes(conn, "tw100", "2024-06-01", "2024-06-30")
        finally:
            conn.close()
        self.assertIn("condition_hits", out)


if __name__ == "__main__":
    unittest.main()
