"""Tests for Yahoo chart sync and research backfill helpers."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from stock_db.benchmark import seed_benchmark_pit_quarterly_snapshots, upsert_benchmark_constituents
from stock_db.benchmark import upsert_benchmark_constituents_meta
from stock_db.connection import connect
from stock_db.market import upsert_daily_bars, upsert_stock_daily_bars
from research.backtest.finpilot_local_backtest import load_price_panels
from yahoo_chart_sync import (
    YahooDailyBar,
    fetch_yahoo_daily_bars,
    stock_daily_bars_rows_from_yahoo,
)


class TestLoadPricePanelsMerge(unittest.TestCase):
    def test_prefers_finmind_over_yfinance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            upsert_stock_daily_bars(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "trade_date": "2026-06-20",
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.5,
                        "adj_close": 100.5,
                        "volume": 1000,
                        "source": "finmind",
                    },
                    {
                        "stock_id": "2330",
                        "trade_date": "2026-06-20",
                        "open": 90.0,
                        "high": 91.0,
                        "low": 89.0,
                        "close": 90.5,
                        "adj_close": 90.5,
                        "volume": 900,
                        "source": "yfinance",
                    },
                    {
                        "stock_id": "2330",
                        "trade_date": "2026-06-21",
                        "open": 1.0,
                        "high": 1.0,
                        "low": 1.0,
                        "close": 1.0,
                        "adj_close": 1.0,
                        "volume": 1,
                        "source": "yfinance",
                    },
                ],
            )
            close, _, _ = load_price_panels(conn)
            self.assertAlmostEqual(float(close.loc["2026-06-20", "2330"]), 100.5)
            self.assertAlmostEqual(float(close.loc["2026-06-21", "2330"]), 1.0)
            conn.close()


class TestBenchmarkPitSeed(unittest.TestCase):
    def test_seed_quarterly_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            upsert_daily_bars(
                conn,
                [
                    {"code": "IX0001", "date": "2020-03-31", "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "source": "tej"},
                    {"code": "IX0001", "date": "2020-06-30", "open": 101.0, "high": 101.0, "low": 101.0, "close": 101.0, "source": "tej"},
                    {"code": "IX0001", "date": "2020-09-30", "open": 102.0, "high": 102.0, "low": 102.0, "close": 102.0, "source": "tej"},
                ],
            )
            upsert_benchmark_constituents_meta(
                conn,
                {
                    "benchmark_code": "0050",
                    "snapshot_date": "2026-06-21",
                    "holding_count": 2,
                    "source": "yuanta_html",
                },
            )
            upsert_benchmark_constituents(
                conn,
                [
                    {
                        "benchmark_code": "0050",
                        "snapshot_date": "2026-06-21",
                        "stock_id": "2330",
                        "stock_name": "台積電",
                        "weight_pct": None,
                        "source": "yuanta_html",
                    },
                    {
                        "benchmark_code": "0050",
                        "snapshot_date": "2026-06-21",
                        "stock_id": "2317",
                        "stock_name": "鴻海",
                        "weight_pct": None,
                        "source": "yuanta_html",
                    },
                ],
            )
            n = seed_benchmark_pit_quarterly_snapshots(
                conn,
                "0050",
                start_date="2020-01-01",
                end_date="2020-12-31",
            )
            self.assertGreater(n, 0)
            snaps = conn.execute(
                "SELECT DISTINCT snapshot_date FROM benchmark_constituents_meta WHERE benchmark_code='0050'"
            ).fetchall()
            dates = {r[0] for r in snaps}
            self.assertIn("2020-03-31", dates)
            self.assertIn("2020-06-30", dates)
            conn.close()


class TestYahooDailyBarsParse(unittest.TestCase):
    @patch("yfinance.download")
    def test_fetch_parses_adj_close(self, mock_download) -> None:
        mock_download.return_value = pd.DataFrame(
            {
                "Open": [10.0],
                "High": [11.0],
                "Low": [9.5],
                "Close": [10.5],
                "Adj Close": [10.2],
                "Volume": [1000.0],
            },
            index=pd.DatetimeIndex(["2020-01-02"]),
        )
        bars = fetch_yahoo_daily_bars("TSM", date(2020, 1, 1), date(2020, 1, 5))
        self.assertEqual(len(bars), 1)
        self.assertIsInstance(bars[0], YahooDailyBar)
        self.assertEqual(bars[0].trade_date, "2020-01-02")
        self.assertAlmostEqual(bars[0].adj_close or 0, 10.2)

    @patch("yahoo_chart_sync.fetch_tw_daily_bars")
    def test_stock_rows_include_adj_close(self, mock_tw) -> None:
        mock_tw.return_value = (
            [
                YahooDailyBar(
                    trade_date="2020-01-02",
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    adj_close=0.9,
                    volume=10.0,
                )
            ],
            "2330.TW",
        )
        rows, sym = stock_daily_bars_rows_from_yahoo("2330", date(2020, 1, 1), date(2020, 1, 5))
        self.assertEqual(sym, "2330.TW")
        self.assertAlmostEqual(rows[0]["adj_close"], 0.9)


if __name__ == "__main__":
    unittest.main()
