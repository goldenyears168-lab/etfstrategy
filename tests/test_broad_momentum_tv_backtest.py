"""Smoke tests for broad_momentum TV strategy backtest."""

from __future__ import annotations

import sqlite3
import unittest

import numpy as np
import pandas as pd

from market_breadth_impulse import zweig_thrust_flags
from research.backtest.broad_momentum_tv_backtest import (
    _compute_adx,
    _trend_template_pass_count,
)


class BroadMomentumTvBacktestTests(unittest.TestCase):
    def test_trend_template_pass_pct(self) -> None:
        idx = pd.date_range("2023-01-02", periods=260, freq="B").strftime("%Y-%m-%d")
        up = pd.DataFrame(
            {f"s{i}": np.linspace(100, 200, 260) + i for i in range(5)},
            index=idx,
        )
        pct = _trend_template_pass_count(up, min_pass=7)
        self.assertGreater(float(pct.iloc[-1]), 0.5)

    def test_zweig_thrust_detects_spike(self) -> None:
        vals = pd.Series([0.35] * 9 + [0.65], index=list(range(10)))
        flags = zweig_thrust_flags(vals, zweig_low=0.40, zweig_high=0.615)
        self.assertTrue(bool(flags.iloc[-1]))

    def test_saved_strategy_config(self) -> None:
        from research.backtest.broad_momentum_tv_backtest import (
            SAVED_STRATEGY_IDS,
            get_saved_strategy_spec,
            params_from_config,
        )

        params = params_from_config()
        self.assertEqual(params.minervini_pass, 7)
        self.assertEqual(SAVED_STRATEGY_IDS, ("minervini-sepa-basket",))
        spec = get_saved_strategy_spec("minervini-sepa-basket")
        self.assertIn("title", spec)
        self.assertEqual(spec["breadth_zone_200"], "strong")

    def test_adx_positive_on_trend(self) -> None:
        idx = pd.date_range("2023-01-02", periods=300, freq="B")
        close = pd.Series(np.linspace(100, 200, 300), index=idx)
        high = close + 1
        low = close - 1
        adx = _compute_adx(high, low, close)
        self.assertGreater(float(adx.iloc[-1]), 20.0)

    def test_run_on_sqlite(self) -> None:
        from research.backtest.broad_momentum_tv_backtest import run_all_broad_momentum_backtests

        dates = pd.date_range("2022-01-03", periods=900, freq="B").strftime("%Y-%m-%d").tolist()
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
        stocks = [f"{i:04d}" for i in range(1, 21)]
        for d in dates:
            for j, s in enumerate(stocks):
                px = 100 + dates.index(d) * 0.05 + j
                conn.execute(
                    "INSERT INTO stock_daily_bars VALUES (?,?,?,?,?,?)",
                    (s, d, px, px, 1000, "finmind"),
                )
            bx = 1000 + dates.index(d) * 0.5
            conn.execute(
                "INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("IX0001", d, bx, bx + 1, bx - 1, bx, 100, None, "tej", d),
            )
        conn.commit()
        summary, results, _ = run_all_broad_momentum_backtests(
            conn, start_date="2024-01-01", end_date="2025-06-01"
        )
        self.assertEqual(len(results), 5)
        self.assertIn("strategy", summary.columns)
        conn.close()


if __name__ == "__main__":
    unittest.main()
