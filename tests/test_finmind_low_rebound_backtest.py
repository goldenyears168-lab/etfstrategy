"""FinMind low-rebound screener · unit tests."""
from __future__ import annotations

import sqlite3
import unittest

import pandas as pd

from research.backtest.finmind_screener_common import (
    _parse_fiscal_year,
    has_consecutive_cash_dividend,
)


class FiscalYearParseTests(unittest.TestCase):
    def test_roc_year(self) -> None:
        self.assertEqual(_parse_fiscal_year("112年"), 2023)
        self.assertEqual(_parse_fiscal_year("112年第4季"), 2023)
        self.assertEqual(_parse_fiscal_year("103年"), 2014)

    def test_western_year(self) -> None:
        self.assertEqual(_parse_fiscal_year("2023"), 2023)


class DividendConsecutiveTests(unittest.TestCase):
    def test_five_consecutive_years_roc(self) -> None:
        div = pd.DataFrame(
            [
                {"stock_id": "2330", "fiscal_year": "108年", "ex_cash_date": "2020-03-01", "cash_dividend": 2.0},
                {"stock_id": "2330", "fiscal_year": "109年", "ex_cash_date": "2021-03-01", "cash_dividend": 2.5},
                {"stock_id": "2330", "fiscal_year": "110年", "ex_cash_date": "2022-03-01", "cash_dividend": 2.5},
                {"stock_id": "2330", "fiscal_year": "111年", "ex_cash_date": "2023-03-01", "cash_dividend": 3.0},
                {"stock_id": "2330", "fiscal_year": "112年", "ex_cash_date": "2024-03-01", "cash_dividend": 3.5},
            ]
        )
        self.assertTrue(
            has_consecutive_cash_dividend(div, "2330", "2024-06-01", min_years=5)
        )

    def test_gap_breaks_streak(self) -> None:
        div = pd.DataFrame(
            [
                {"stock_id": "2330", "fiscal_year": "2019", "ex_cash_date": "2020-03-01", "cash_dividend": 2.0},
                {"stock_id": "2330", "fiscal_year": "2021", "ex_cash_date": "2022-03-01", "cash_dividend": 2.5},
                {"stock_id": "2330", "fiscal_year": "2022", "ex_cash_date": "2023-03-01", "cash_dividend": 3.0},
                {"stock_id": "2330", "fiscal_year": "2023", "ex_cash_date": "2024-03-01", "cash_dividend": 3.5},
            ]
        )
        self.assertFalse(
            has_consecutive_cash_dividend(div, "2330", "2024-06-01", min_years=5)
        )


class ValidateOnlySmokeTests(unittest.TestCase):
    def test_validate_runs_on_real_db(self) -> None:
        from stock_db import DEFAULT_DB_PATH, connect
        from research.backtest.finmind_screener_common import count_condition_passes

        if not DEFAULT_DB_PATH.is_file():
            self.skipTest("no stocks.db")
        import os
        if not os.environ.get("RUN_SLOW_DB_TESTS"):
            self.skipTest("slow real-DB smoke (~50s on mini); set RUN_SLOW_DB_TESTS=1 to run")
        conn = connect(DEFAULT_DB_PATH)
        try:
            out = count_condition_passes(
                conn, "tw100", "2024-06-01", "2024-06-30"
            )
        finally:
            conn.close()
        self.assertIn("condition_hits", out)
        self.assertGreaterEqual(out["trading_days"], 1)


class ExitRuleTests(unittest.TestCase):
    def test_variant_id_sl_tp(self) -> None:
        from research.backtest.finmind_low_rebound_backtest import LowReboundExitRule

        rule = LowReboundExitRule(hold_days=20, stop_loss_pct=8.0, take_profit_pct=15.0)
        self.assertEqual(rule.variant_id, "h20_sl8_tp15")


if __name__ == "__main__":
    unittest.main()
