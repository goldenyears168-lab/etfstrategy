"""Smoke tests for pullback × momentum_correction backtest."""

from __future__ import annotations

import unittest

from research.backtest.pullback_regime_backtest import (
    PullbackRegimeBacktestConfig,
    _macd_cross_up,
    _rsi,
    run_pullback_regime_backtest,
)
from stage_analysis import minervini_pass_at_date
from stock_db import DEFAULT_DB_PATH, connect


class TestPullbackRegimeBacktest(unittest.TestCase):
    def test_rsi_macd_helpers(self) -> None:
        import pandas as pd

        close = pd.Series([100, 101, 99, 98, 97, 99, 102, 104, 103, 105], dtype=float)
        rsi = _rsi(close, 2)
        self.assertTrue(rsi.iloc[-1] > rsi.iloc[3])
        cross = _macd_cross_up(close)
        self.assertEqual(len(cross), len(close))

    def test_run_backtest_smoke(self) -> None:
        conn = connect(DEFAULT_DB_PATH)
        try:
            result = run_pullback_regime_backtest(
                conn,
                PullbackRegimeBacktestConfig(
                    date_start="2025-01-01",
                    date_end="2025-03-31",
                    horizons=(30,),
                    top_n=5,
                ),
            )
        finally:
            conn.close()
        self.assertIn("summaries", result)
        self.assertGreaterEqual(result["config"]["n_correction_days"], 0)
        l1c = [s for s in result["summaries"] if s["strategy_id"] == "l1c_mom20_baseline"]
        self.assertEqual(len(l1c), 1)


if __name__ == "__main__":
    unittest.main()
