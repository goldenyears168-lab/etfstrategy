"""TW100 chip feature panel tests."""

from __future__ import annotations

import sqlite3
import unittest

import pandas as pd

from research.backtest.tw100_chip_features import (
    CHIP_FEATURE_NAMES,
    build_chip_feature_panels,
    chip_coverage_report,
)


class Tw100ChipFeatureTests(unittest.TestCase):
    def test_build_chip_panels_on_fixture(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE stock_institutional_daily (
                stock_id TEXT, trade_date TEXT, close_price REAL,
                foreign_net REAL, investment_trust_net REAL, dealer_self_net REAL,
                three_institution_net REAL, source TEXT, synced_at TEXT,
                PRIMARY KEY (stock_id, trade_date, source)
            );
            CREATE TABLE stock_institutional_side_daily (
                stock_id TEXT, trade_date TEXT, inst_name TEXT,
                buy REAL, sell REAL, net REAL, source TEXT, synced_at TEXT,
                PRIMARY KEY (stock_id, trade_date, inst_name, source)
            );
            CREATE TABLE stock_margin_daily (
                stock_id TEXT, trade_date TEXT, margin_balance REAL, margin_change REAL,
                short_balance REAL, short_change REAL, source TEXT, synced_at TEXT,
                PRIMARY KEY (stock_id, trade_date, source)
            );
            CREATE TABLE stock_lending_daily (
                stock_id TEXT, trade_date TEXT, lending_balance REAL, lending_change REAL,
                fee_rate REAL, source TEXT, synced_at TEXT,
                PRIMARY KEY (stock_id, trade_date, source)
            );
            CREATE TABLE stock_daytrade_daily (
                stock_id TEXT, trade_date TEXT, daytrade_volume REAL, total_volume REAL,
                daytrade_ratio_pct REAL, source TEXT, synced_at TEXT,
                PRIMARY KEY (stock_id, trade_date, source)
            );
            """
        )
        dates = pd.bdate_range("2024-01-01", periods=30).strftime("%Y-%m-%d")
        for d in dates:
            conn.execute(
                "INSERT INTO stock_institutional_daily VALUES (?,?,?,?,?,?,?,?,?)",
                ("2330", d, 100.0, 1.0, 0.5, 0.1, 1.6, "finmind", "now"),
            )
            conn.execute(
                "INSERT INTO stock_institutional_side_daily VALUES (?,?,?,?,?,?,?,?)",
                ("2330", d, "Foreign_Investor", 10.0, 5.0, 5.0, "finmind", "now"),
            )
            conn.execute(
                "INSERT INTO stock_institutional_side_daily VALUES (?,?,?,?,?,?,?,?)",
                ("2330", d, "Investment_Trust", 8.0, 2.0, 6.0, "finmind", "now"),
            )
            conn.execute(
                "INSERT INTO stock_margin_daily VALUES (?,?,?,?,?,?,?,?)",
                ("2330", d, 1000.0, 10.0, 200.0, -2.0, "finmind", "now"),
            )
            conn.execute(
                "INSERT INTO stock_lending_daily VALUES (?,?,?,?,?,?,?)",
                ("2330", d, 500.0, 3.0, 0.1, "finmind", "now"),
            )
        month_ends = ["2024-01-31", "2024-02-29"]
        panels = build_chip_feature_panels(
            conn,
            ["2330"],
            month_ends,
            start="2024-01-01",
            end="2024-02-29",
        )
        self.assertEqual(set(panels.keys()), set(CHIP_FEATURE_NAMES))
        self.assertFalse(pd.isna(panels["foreign_net_20d"].at["2024-02-29", "2330"]))
        cov = chip_coverage_report(panels, month_ends, min_cross_section=1)
        self.assertGreaterEqual(cov["eligible_month_ends"], 1)
        conn.close()

    def test_resolve_mom_fund_chip_feature_count(self) -> None:
        from research.backtest.tw100_monthly_wfa import resolve_feature_names

        names = resolve_feature_names("mom_fund_chip")
        self.assertEqual(len(names), 4 + 5 + 9)


if __name__ == "__main__":
    unittest.main()
