"""market_sync_window 與 backfill 覆蓋摘要測試。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from market_sync_window import iter_calendar_chunks, resolve_sync_window
from stock_db import connect, format_market_data_coverage, market_data_coverage_summary


class MarketSyncWindowTests(unittest.TestCase):
    def test_skip_when_window_fully_covered(self) -> None:
        action, start, end = resolve_sync_window(
            start=date(2024, 1, 1),
            end=date(2024, 6, 30),
            min_rows=20,
            series=[
                ("2024-01-01", "2024-06-30", 120),
                ("2024-01-01", "2024-06-30", 120),
            ],
            force_refresh=False,
        )
        self.assertEqual(action, "skip")
        self.assertIsNone(start)
        self.assertIsNone(end)

    def test_backfill_when_missing_old_data(self) -> None:
        action, start, end = resolve_sync_window(
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            min_rows=50,
            series=[
                ("2024-06-01", "2024-12-31", 150),
                ("2024-06-01", "2024-12-31", 150),
            ],
            force_refresh=False,
        )
        self.assertEqual(action, "backfill")
        self.assertEqual(start, date(2024, 1, 1))
        self.assertEqual(end, date(2024, 6, 8))

    def test_incremental_when_missing_recent_data(self) -> None:
        action, start, end = resolve_sync_window(
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            min_rows=50,
            series=[
                ("2024-01-01", "2024-06-30", 130),
                ("2024-01-01", "2024-06-30", 130),
            ],
            force_refresh=False,
        )
        self.assertEqual(action, "incremental")
        self.assertEqual(start, date(2024, 6, 23))
        self.assertEqual(end, date(2024, 12, 31))

    def test_iter_calendar_chunks(self) -> None:
        chunks = iter_calendar_chunks(date(2024, 1, 1), date(2024, 3, 1), 30)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], date(2024, 1, 1))
        self.assertEqual(chunks[-1][1], date(2024, 3, 1))


class MarketCoverageSummaryTests(unittest.TestCase):
    def test_empty_db_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            try:
                summary = market_data_coverage_summary(
                    conn,
                    window_start="2024-01-01",
                    window_end="2024-12-31",
                )
                text = format_market_data_coverage(summary)
            finally:
                conn.close()
        self.assertIn("2024-01-01", text)


if __name__ == "__main__":
    unittest.main()
