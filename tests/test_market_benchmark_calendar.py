"""market_benchmark · 交易日對齊。"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import date

from market_benchmark import (
    is_trading_date,
    is_trading_session_day,
    latest_trading_date,
    resolve_brief_trade_date,
)


class TestTradingCalendar(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        # 交易日曆錨點來源 2026-08 改綁 stock_daily_bars（原 daily_bars/tej 會腐化）。
        self.conn.execute(
            """
            CREATE TABLE stock_daily_bars (
                stock_id TEXT, trade_date TEXT, open REAL, close REAL, volume REAL, source TEXT
            )
            """
        )
        for d in ("2026-06-17", "2026-06-18", "2026-06-19"):
            self.conn.execute(
                "INSERT INTO stock_daily_bars (stock_id, trade_date, close) "
                "VALUES ('2330', ?, 1.0)",
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

    def test_session_day_before_tej_close(self) -> None:
        self.conn.execute(
            "INSERT INTO stock_daily_bars (stock_id, trade_date, close) "
            "VALUES ('2330', '2026-06-24', 1.0)"
        )
        wed = date(2026, 6, 25)
        self.assertFalse(is_trading_date(self.conn, wed))
        self.assertTrue(is_trading_session_day(self.conn, wed))


if __name__ == "__main__":
    unittest.main()
