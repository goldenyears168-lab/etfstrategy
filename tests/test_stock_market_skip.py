"""sync_stock_market_daily：同日跳過 / 增量窗。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from stock_db import StockMarketCoverage
from sync_stock_market_daily import resolve_fetch_window


class TestResolveFetchWindow(unittest.TestCase):
    def setUp(self) -> None:
        self.end = date(2026, 6, 4)
        self.start = self.end - timedelta(days=60)

    def test_skip_when_fully_synced(self) -> None:
        cov = StockMarketCoverage(
            stock_id="2330",
            bar_min="2026-04-05",
            bar_max="2026-06-04",
            bar_count_window=40,
            inst_min="2026-04-05",
            inst_max="2026-06-04",
            inst_count_window=40,
        )
        action, fs, fe = resolve_fetch_window(
            cov, self.start, self.end, 60, force_refresh=False
        )
        self.assertEqual(action, "skip")
        self.assertIsNone(fs)

    def test_incremental_when_bar_behind(self) -> None:
        cov = StockMarketCoverage(
            stock_id="2454",
            bar_min="2026-04-05",
            bar_max="2026-06-01",
            bar_count_window=40,
            inst_min="2026-04-05",
            inst_max="2026-06-01",
            inst_count_window=40,
        )
        action, fs, fe = resolve_fetch_window(
            cov, self.start, self.end, 60, force_refresh=False
        )
        self.assertEqual(action, "incremental")
        assert fs is not None
        self.assertGreater(fs, self.start)
        self.assertEqual(fe, self.end)

    def test_backfill_when_missing_old_bars(self) -> None:
        cov = StockMarketCoverage(
            stock_id="2330",
            bar_min="2026-05-01",
            bar_max="2026-06-04",
            bar_count_window=25,
            inst_min="2026-05-01",
            inst_max="2026-06-04",
            inst_count_window=25,
        )
        action, fs, fe = resolve_fetch_window(
            cov, self.start, self.end, 60, force_refresh=False
        )
        self.assertEqual(action, "backfill")
        assert fs is not None and fe is not None
        self.assertEqual(fs, self.start)
        self.assertLess(fe, self.end)

    def test_force_refresh_full(self) -> None:
        cov = StockMarketCoverage(
            stock_id="2330",
            bar_min="2026-04-05",
            bar_max="2026-06-04",
            bar_count_window=40,
            inst_min="2026-04-05",
            inst_max="2026-06-04",
            inst_count_window=40,
        )
        action, fs, fe = resolve_fetch_window(
            cov, self.start, self.end, 60, force_refresh=True
        )
        self.assertEqual(action, "full")
        self.assertEqual(fs, self.start)
        self.assertEqual(fe, self.end)


if __name__ == "__main__":
    unittest.main()
