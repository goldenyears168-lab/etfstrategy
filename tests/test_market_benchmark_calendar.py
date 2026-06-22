"""market_benchmark · 交易日對齊。"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import date

from market_benchmark import is_trading_date, latest_trading_date, resolve_brief_trade_date


class TestTradingCalendar(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE daily_bars (
                code TEXT, source TEXT, date TEXT, close REAL
            )
            """
        )
        for d in ("2026-06-17", "2026-06-18", "2026-06-19"):
            self.conn.execute(
                "INSERT INTO daily_bars VALUES ('IX0001', 'tej', ?, 1.0)",
                (d,),
            )

    def test_sunday_resolves_to_friday(self) -> None:
        sunday = date(2026, 6, 21)
        self.assertFalse(is_trading_date(self.conn, sunday))
        self.assertEqual(
            resolve_brief_trade_date(self.conn, sunday),
            date(2026, 6, 19),
        )

    def test_latest_trading_date(self) -> None:
        self.assertEqual(
            latest_trading_date(self.conn, on_or_before="2026-06-20"),
            "2026-06-19",
        )


if __name__ == "__main__":
    unittest.main()
