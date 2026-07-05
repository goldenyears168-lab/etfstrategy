"""Tests for market_sync_window backfill resolution."""

from __future__ import annotations

import unittest
from datetime import date

from market_sync_window import resolve_sync_window


class MarketSyncWindowTests(unittest.TestCase):
    def test_need_old_backfills_full_gap_not_overlap_only(self) -> None:
        start = date(2015, 1, 1)
        end = date(2026, 6, 26)
        series = [
            ("2024-06-01", "2026-06-26", 500),
            ("2024-06-01", "2026-06-26", 500),
        ]
        action, fetch_start, fetch_end = resolve_sync_window(
            start=start,
            end=end,
            min_rows=800,
            series=series,
            force_refresh=False,
        )
        self.assertEqual(action, "full")

    def test_need_old_only_fetches_until_day_before_earliest(self) -> None:
        start = date(2015, 1, 1)
        end = date(2026, 6, 26)
        series = [
            ("2015-01-05", "2026-06-26", 2796),
            ("2015-01-05", "2026-06-26", 2065),
        ]
        action, fetch_start, fetch_end = resolve_sync_window(
            start=start,
            end=end,
            min_rows=800,
            series=series,
            force_refresh=False,
        )
        self.assertEqual(action, "backfill")
        self.assertEqual(fetch_start, start)
        self.assertEqual(fetch_end, date(2015, 1, 4))


if __name__ == "__main__":
    unittest.main()
