"""query_stock_prices：ETF 日線 TEJ → FinMind fallback。"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

_HAS_YFINANCE = importlib.util.find_spec("yfinance") is not None
if not _HAS_YFINANCE:
    sys.modules.setdefault("yfinance", types.ModuleType("yfinance"))

from query_stock_prices import _sync_one_etf_daily_bars, sync_etf_daily_bars
from stock_db import connect


@unittest.skipUnless(_HAS_YFINANCE, "yfinance not installed")
class TestEtfDailyFallback(unittest.TestCase):
    def test_tej_success_no_finmind(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "t.db")
        tej_bars = [
            {
                "code": "00981A",
                "date": "2026-06-02",
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "volume": 1000,
                "spread": None,
                "source": "tej",
            }
        ]
        try:
            with (
                patch("query_stock_prices.fetch_tej_etf_bars", return_value=tej_bars),
                patch("query_stock_prices.fetch_finmind_daily") as fm,
            ):
                n = _sync_one_etf_daily_bars(
                    conn,
                    "00981A",
                    date(2026, 6, 1),
                    date(2026, 6, 3),
                    quiet=True,
                )
            self.assertEqual(n, 1)
            fm.assert_not_called()
            row = conn.execute(
                "SELECT source, close FROM daily_bars WHERE code='00981A'"
            ).fetchone()
            self.assertEqual(row["source"], "tej")
            self.assertEqual(row["close"], 10.2)
        finally:
            conn.close()
            tmp.cleanup()

    def test_tej_fail_fallback_finmind(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "t.db")
        fm_bars = [
            {
                "code": "00407A",
                "date": "2026-06-02",
                "open": 15.0,
                "high": 15.2,
                "low": 14.8,
                "close": 15.1,
                "volume": 500,
                "spread": 0.5,
                "source": "finmind",
            }
        ]
        try:
            with (
                patch(
                    "query_stock_prices.fetch_tej_etf_bars",
                    side_effect=RuntimeError("TEJ PDB003"),
                ),
                patch("query_stock_prices.fetch_finmind_daily", return_value=fm_bars),
            ):
                n = _sync_one_etf_daily_bars(
                    conn,
                    "00407A",
                    date(2026, 6, 1),
                    date(2026, 6, 3),
                    quiet=True,
                )
            self.assertEqual(n, 1)
            row = conn.execute(
                "SELECT source, close FROM daily_bars WHERE code='00407A'"
            ).fetchone()
            self.assertEqual(row["source"], "finmind")
            self.assertEqual(row["close"], 15.1)
        finally:
            conn.close()
            tmp.cleanup()

    def test_sync_etf_daily_bars_skips_total_failure(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "t.db"
        with (
            patch(
                "query_stock_prices._sync_one_etf_daily_bars",
                side_effect=[2, RuntimeError("both failed")],
            ),
        ):
            total = sync_etf_daily_bars(("A", "B"), db, 30, quiet=True)
        self.assertEqual(total, 2)


if __name__ == "__main__":
    unittest.main()
