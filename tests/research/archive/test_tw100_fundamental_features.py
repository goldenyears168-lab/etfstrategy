"""TW100 fundamental feature panel tests."""

from __future__ import annotations

import sqlite3
import unittest

import pandas as pd

from research.backtest.archive.tw100_fundamental_features import (
    build_fundamental_feature_panels,
    earnings_yield,
    book_to_price,
)


class Tw100FundamentalFeatureTests(unittest.TestCase):
    def test_earnings_yield_clips(self) -> None:
        self.assertAlmostEqual(earnings_yield(10.0), 0.1)
        self.assertIsNone(earnings_yield(-5.0))
        self.assertAlmostEqual(earnings_yield(500.0), 1.0 / 200.0)

    def test_book_to_price(self) -> None:
        self.assertAlmostEqual(book_to_price(2.0), 0.5)

    def test_pit_panels_on_fixture(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE stock_fundamental (
                stock_id TEXT, as_of_date TEXT, pe REAL, pb REAL, dividend_yield REAL,
                roe_ttm REAL, eps_ttm REAL, eps_latest_q REAL, roe_latest_q REAL,
                revenue_yoy_pct REAL, revenue_mom_accel_pp REAL, source TEXT, synced_at TEXT,
                PRIMARY KEY (stock_id, as_of_date, source)
            );
            CREATE TABLE stock_financial_history (
                stock_id TEXT, period_date TEXT, period_type TEXT, metric TEXT,
                value REAL, source TEXT, synced_at TEXT,
                PRIMARY KEY (stock_id, period_date, period_type, metric, source)
            );
            """
        )
        conn.execute(
            "INSERT INTO stock_fundamental VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2330", "2024-01-15", 15.0, 5.0, 2.5, None, None, None, None, None, None, "finmind", "now"),
        )
        conn.execute(
            "INSERT INTO stock_fundamental VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2330", "2024-06-15", 12.0, 4.0, 3.0, None, None, None, None, None, None, "finmind", "now"),
        )
        conn.execute(
            "INSERT INTO stock_financial_history VALUES (?,?,?,?,?,?,?)",
            ("2330", "2024-03-31", "quarter", "net_income", 100.0, "finmind", "now"),
        )
        conn.execute(
            "INSERT INTO stock_financial_history VALUES (?,?,?,?,?,?,?)",
            ("2330", "2024-03-31", "quarter", "equity", 500.0, "finmind", "now"),
        )
        month_ends = ["2024-01-31", "2024-06-30"]
        panels = build_fundamental_feature_panels(
            conn,
            ["2330"],
            month_ends,
            start="2024-01-01",
            end="2024-06-30",
        )
        self.assertAlmostEqual(float(panels["ep_ttm"].at["2024-01-31", "2330"]), 1 / 15.0, places=6)
        self.assertAlmostEqual(float(panels["ep_ttm"].at["2024-06-30", "2330"]), 1 / 12.0, places=6)
        self.assertAlmostEqual(float(panels["roe_ttm"].at["2024-06-30", "2330"]), 20.0, places=4)
        conn.close()


if __name__ == "__main__":
    unittest.main()
