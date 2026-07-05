"""FinMind low P/S growth screener · unit tests."""
from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.finmind_ps_growth_common import (
    has_consecutive_quarter_margins,
    ps_ratio_from_stored,
)


class PsRatioTests(unittest.TestCase):
    def test_stored_to_ratio(self) -> None:
        self.assertAlmostEqual(ps_ratio_from_stored(500.0), 5.0)
        self.assertAlmostEqual(ps_ratio_from_stored(1027.38), 10.2738)


class MarginTests(unittest.TestCase):
    def test_two_quarter_margin_pass(self) -> None:
        fin = pd.DataFrame(
            [
                {"stock_id": "2330", "period_date": "2024-03-31", "period_type": "quarter", "metric": "revenue", "value": 100.0},
                {"stock_id": "2330", "period_date": "2024-03-31", "period_type": "quarter", "metric": "net_income", "value": 8.0},
                {"stock_id": "2330", "period_date": "2024-06-30", "period_type": "quarter", "metric": "revenue", "value": 110.0},
                {"stock_id": "2330", "period_date": "2024-06-30", "period_type": "quarter", "metric": "net_income", "value": 7.0},
            ]
        )
        self.assertTrue(
            has_consecutive_quarter_margins(
                fin, "2330", "2024-07-01", min_pct=5.0, n_quarters=2, allow_net_approx=True
            )
        )


class ValidateSmokeTests(unittest.TestCase):
    def test_validate_runs(self) -> None:
        from stock_db import DEFAULT_DB_PATH, connect
        from research.backtest.finmind_ps_growth_common import count_condition_passes

        if not DEFAULT_DB_PATH.is_file():
            self.skipTest("no stocks.db")
        conn = connect(DEFAULT_DB_PATH)
        try:
            out = count_condition_passes(conn, "tw100", "2024-06-01", "2024-06-30")
        finally:
            conn.close()
        self.assertIn("condition_hits", out)


if __name__ == "__main__":
    unittest.main()
