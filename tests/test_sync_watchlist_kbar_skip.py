"""Tests · sync_watchlist_kbar skip-if-fresh."""

from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import patch

from rrg_mono_swap_accel_screen import sync_watchlist_kbar
from stock_db.kbar import upsert_stock_kbar_5m


class TestSyncWatchlistKbarSkip(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE stock_kbar_5m (
                stock_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                minute TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL NOT NULL,
                volume INTEGER,
                source TEXT NOT NULL DEFAULT 'yahoo',
                synced_at TEXT NOT NULL,
                PRIMARY KEY (stock_id, trade_date, minute, source)
            );
            """
        )
        upsert_stock_kbar_5m(
            self.conn,
            [
                {
                    "stock_id": "2330",
                    "trade_date": "2026-07-08",
                    "minute": "10:30:00",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 10,
                    "source": "yahoo",
                },
                {
                    "stock_id": "2317",
                    "trade_date": "2026-07-08",
                    "minute": "10:25:00",
                    "open": 200.0,
                    "high": 201.0,
                    "low": 199.0,
                    "close": 200.5,
                    "volume": 10,
                    "source": "yahoo",
                },
            ],
        )

    def tearDown(self) -> None:
        self.conn.close()

    @patch.dict(os.environ, {"C18ACC_KBAR_SYNC": "1", "KBAR_SYNC_SKIP_FRESH": "1"})
    @patch("rrg_mono_swap_accel_screen.fetch_tw_intraday_kbar_rows")
    def test_skips_fresh_ids_only_fetches_stale(
        self,
        mock_fetch,
    ) -> None:
        mock_fetch.return_value = (
            [
                {
                    "stock_id": "2317",
                    "trade_date": "2026-07-08",
                    "minute": "10:30:00",
                    "open": 200.0,
                    "high": 201.0,
                    "low": 199.0,
                    "close": 200.5,
                    "volume": 10,
                    "source": "yahoo",
                }
            ],
            None,
        )
        n = sync_watchlist_kbar(
            self.conn,
            ["2330", "2317"],
            "2026-07-08",
            sleep_sec=0,
            poll_minute="10:30",
        )
        self.assertEqual(mock_fetch.call_count, 1)
        self.assertEqual(mock_fetch.call_args.kwargs.get("interval"), "5m")
        self.assertEqual(mock_fetch.call_args[0][0], "2317")
        self.assertGreater(n, 0)

    @patch.dict(os.environ, {"C18ACC_KBAR_SYNC": "1", "KBAR_SYNC_SKIP_FRESH": "0"})
    @patch("rrg_mono_swap_accel_screen.fetch_tw_intraday_kbar_rows")
    def test_skip_fresh_disabled_fetches_all(
        self,
        mock_fetch,
    ) -> None:
        mock_fetch.return_value = ([], None)
        sync_watchlist_kbar(
            self.conn,
            ["2330", "2317"],
            "2026-07-08",
            sleep_sec=0,
            poll_minute="10:30",
        )
        self.assertEqual(mock_fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
