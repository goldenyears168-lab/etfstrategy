"""Smoke tests for tanish35 momentum backtest."""

from __future__ import annotations

import sqlite3
import unittest

import numpy as np
import pandas as pd

from research.backtest.tanish_momentum_backtest import (
    _combined_score_row,
    _precompute_indicators,
    simulate_tanish_momentum,
    TanishMomentumParams,
)


class TanishMomentumBacktestTests(unittest.TestCase):
    def test_combined_score_prefers_uptrend(self) -> None:
        idx = pd.date_range("2023-01-03", periods=300, freq="B").strftime("%Y-%m-%d")
        close = pd.DataFrame(
            {
                "1111": np.linspace(100, 200, 300),
                "2222": np.linspace(200, 100, 300),
            },
            index=idx,
        )
        ind = _precompute_indicators(close, TanishMomentumParams())
        ind["close"] = close
        day = idx[-1]
        scored = _combined_score_row(["1111", "2222"], day, ind, TanishMomentumParams())
        self.assertTrue(scored)
        self.assertEqual(scored[0][0], "1111")

    def test_run_on_sqlite(self) -> None:
        dates = pd.date_range("2023-01-03", periods=400, freq="B").strftime("%Y-%m-%d").tolist()
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE stock_daily_bars (
                stock_id TEXT, trade_date TEXT, open REAL, close REAL, volume REAL, source TEXT
            );
            CREATE TABLE daily_bars (
                code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
                volume REAL, spread REAL, source TEXT, synced_at TEXT
            );
            """
        )
        stocks = [f"{i:04d}" for i in range(1, 11)]
        for d in dates:
            for j, s in enumerate(stocks):
                px = 100 + dates.index(d) * 0.1 + j
                conn.execute(
                    "INSERT INTO stock_daily_bars VALUES (?,?,?,?,?,?)",
                    (s, d, px, px, 1000, "finmind"),
                )
            bx = 1000 + dates.index(d) * 0.3
            conn.execute(
                "INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("IX0001", d, bx, bx + 1, bx - 1, bx, 100, None, "tej", d),
            )
        conn.commit()
        from market_benchmark import load_benchmark_close
        from research.backtest.finpilot_local_backtest import load_price_panels

        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn)
        bt = [d for d in dates if d >= "2024-06-01"]
        r = simulate_tanish_momentum(
            close, bench, {}, variant="author", bt_dates=bt
        )
        self.assertGreater(r.stats["trading_days"], 10)
        conn.close()


if __name__ == "__main__":
    unittest.main()
