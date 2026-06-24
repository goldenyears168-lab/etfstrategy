"""Tests for rrg-lens-score-swap SQLite backfill helpers."""

from __future__ import annotations

import json
import sqlite3
import unittest

from stock_db.kbar import (
    finmind_kbar_rows_to_db,
    load_kbar_day_closes,
    price_at_or_before_minute,
    upsert_stock_kbar_1m,
)
from stock_db.lens import (
    load_lens_daily_highlight,
    load_lens_daily_highlight_stock_ids,
    upsert_lens_daily_highlight,
)


class TestLensDailyHighlightLocal(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE lens_daily_highlight (
                trade_date TEXT NOT NULL,
                stock_id TEXT NOT NULL,
                row_json TEXT NOT NULL,
                lens_score REAL NOT NULL DEFAULT 0,
                highlight_tier TEXT NOT NULL DEFAULT 'none',
                rrg_quadrant TEXT,
                rrg_mono_fresh INTEGER NOT NULL DEFAULT 0,
                rrg_tier2 INTEGER NOT NULL DEFAULT 0,
                synced_at TEXT NOT NULL,
                PRIMARY KEY (trade_date, stock_id)
            );
            """
        )

    def test_roundtrip(self) -> None:
        row = {
            "trade_date": "2026-06-20",
            "stock_id": "2330",
            "stock_name": "台積電",
            "lens_score": 42.5,
            "highlight_tier": "watch",
            "rrg_quadrant": "leading",
            "rrg_mono_fresh": True,
            "rrg_tier2": True,
            "etf_add_codes": ["00981A"],
            "sources_json": {"rrg": {"screen_kind": "close"}},
        }
        n = upsert_lens_daily_highlight(self.conn, [row])
        self.assertEqual(n, 1)
        loaded = load_lens_daily_highlight(self.conn, "2026-06-20")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["stock_id"], "2330")
        self.assertEqual(loaded[0]["etf_add_codes"], ["00981A"])
        self.assertEqual(load_lens_daily_highlight_stock_ids(self.conn, "2026-06-20"), {"2330"})


class TestStockKbar1m(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE stock_kbar_1m (
                stock_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                minute TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL NOT NULL,
                volume INTEGER,
                source TEXT NOT NULL DEFAULT 'finmind',
                synced_at TEXT NOT NULL,
                PRIMARY KEY (stock_id, trade_date, minute, source)
            );
            """
        )

    def test_finmind_rows_and_price_lookup(self) -> None:
        raw = [
            {"date": "2026-06-20", "minute": "09:01:00", "close": 100.0},
            {"date": "2026-06-20", "minute": "10:00:00", "close": 101.5},
            {"date": "2026-06-20", "minute": "11:00:00", "close": 99.0},
        ]
        rows = finmind_kbar_rows_to_db("2330", raw)
        upsert_stock_kbar_1m(self.conn, rows)
        bars = load_kbar_day_closes(self.conn, "2330", "2026-06-20")
        self.assertEqual(len(bars), 3)
        self.assertEqual(price_at_or_before_minute(bars, "10:00"), 101.5)
        self.assertEqual(price_at_or_before_minute(bars, "09:30"), 100.0)


if __name__ == "__main__":
    unittest.main()
