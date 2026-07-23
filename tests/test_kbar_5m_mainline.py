"""Tests · stock_kbar_5m mainline storage."""

from __future__ import annotations

import sqlite3
import unittest

from stock_db.kbar import (
    KbarBar,
    aggregate_bars_to_5m,
    aggregate_taiex_5s_rows_to_5m,
    kbar_5m_close_at_minute,
    kbar_5m_fresh_for_poll,
    load_kbar_day_5m_closes,
    price_at_or_before_minute,
    upsert_stock_kbar_5m,
    upsert_yahoo_5m_rows,
)


class TestKbar5mMainline(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.conn.close()

    def test_aggregate_and_pit_price_matches_1m_poll(self) -> None:
        bars_1m = tuple(
            KbarBar(f"09:{m:02d}:00", 100.0 + m, 100.5 + m, 99.5 + m, 100.0 + m, 1000)
            for m in range(6)
        )
        closes_1m = tuple((b.minute, b.close) for b in bars_1m)
        px_1m = price_at_or_before_minute(closes_1m, "09:05")
        self.assertEqual(px_1m, 105.0)
        bars_5m = aggregate_bars_to_5m(bars_1m)
        self.assertEqual(bars_5m[-1].minute, "09:05:00")
        self.assertEqual(bars_5m[-1].close, 105.0)
        rows = [
            {
                "stock_id": "2330",
                "trade_date": "2026-07-08",
                "minute": b.minute,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "source": "yahoo",
            }
            for b in bars_5m
        ]
        upsert_stock_kbar_5m(self.conn, rows)
        px_5m = kbar_5m_close_at_minute(self.conn, "2330", "2026-07-08", "09:05")
        self.assertEqual(px_5m, px_1m)

    def test_fresh_for_poll_skips_when_latest_covers_grid(self) -> None:
        upsert_stock_kbar_5m(
            self.conn,
            [
                {
                    "stock_id": "2330",
                    "trade_date": "2026-07-08",
                    "minute": "09:30:00",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 10,
                    "source": "yahoo",
                }
            ],
        )
        self.assertTrue(
            kbar_5m_fresh_for_poll(self.conn, "2330", "2026-07-08", "09:30")
        )
        self.assertFalse(
            kbar_5m_fresh_for_poll(self.conn, "2330", "2026-07-08", "09:35")
        )
        self.assertFalse(
            kbar_5m_fresh_for_poll(self.conn, "2317", "2026-07-08", "09:30")
        )

    def test_backfill_finmind_fut_source(self) -> None:
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
                source TEXT NOT NULL,
                synced_at TEXT NOT NULL,
                PRIMARY KEY (stock_id, trade_date, minute, source)
            );
            """
        )
        from stock_db.kbar import backfill_stock_kbar_5m_from_1m, kbar_5m_backfill_complete

        rows = []
        for m in range(20):
            rows.append(
                {
                    "stock_id": "TX1!",
                    "trade_date": "2026-05-15",
                    "minute": f"09:{m:02d}:00",
                    "open": 18000.0 + m,
                    "high": 18001.0 + m,
                    "low": 17999.0 + m,
                    "close": 18000.0 + m,
                    "volume": 1,
                    "source": "finmind_fut",
                    "synced_at": "t",
                }
            )
        self.conn.executemany(
            """
            INSERT INTO stock_kbar_1m (
                stock_id, trade_date, minute, open, high, low, close, volume, source, synced_at
            ) VALUES (
                :stock_id, :trade_date, :minute, :open, :high, :low, :close, :volume, :source, :synced_at
            )
            """,
            rows,
        )
        self.conn.commit()
        n = backfill_stock_kbar_5m_from_1m(self.conn, "TX1!", "2026-05-15")
        self.assertGreater(n, 0)
        self.assertTrue(kbar_5m_backfill_complete(self.conn, "TX1!", "2026-05-15"))
        fut_n = self.conn.execute(
            "SELECT COUNT(*) FROM stock_kbar_5m WHERE stock_id='TX1!' AND source='finmind_fut'"
        ).fetchone()[0]
        self.assertGreaterEqual(fut_n, 2)

    def test_aggregate_taiex_5s_rows_to_5m(self) -> None:
        rows = [
            {"date": "2026-07-14 09:00:00", "TAIEX": 100.0},
            {"date": "2026-07-14 09:01:00", "TAIEX": 101.0},
            {"date": "2026-07-14 09:04:55", "TAIEX": 99.5},
            {"date": "2026-07-14 09:05:00", "TAIEX": 102.0},
            {"date": "2026-07-14 09:09:00", "TAIEX": 103.0},
            {"date": "2026-07-14 13:30:00", "TAIEX": 104.0},
        ]
        td, bars = aggregate_taiex_5s_rows_to_5m(rows)
        self.assertEqual(td, "2026-07-14")
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[0].minute, "09:00:00")
        self.assertEqual(bars[0].open, 100.0)
        self.assertEqual(bars[0].high, 101.0)
        self.assertEqual(bars[0].low, 99.5)
        self.assertEqual(bars[0].close, 99.5)
        self.assertEqual(bars[1].minute, "09:05:00")
        self.assertEqual(bars[1].close, 103.0)
        self.assertEqual(bars[2].minute, "13:30:00")
        self.assertEqual(bars[2].close, 104.0)

    def test_upsert_yahoo_5m_rows_filters_session(self) -> None:
        n = upsert_yahoo_5m_rows(
            self.conn,
            "2330",
            [
                {
                    "stock_id": "2330",
                    "trade_date": "2026-07-08",
                    "minute": "09:05:00",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 10,
                    "source": "yahoo",
                },
                {
                    "stock_id": "2330",
                    "trade_date": "2026-07-08",
                    "minute": "08:30:00",
                    "open": 99.0,
                    "high": 99.5,
                    "low": 98.5,
                    "close": 99.0,
                    "volume": 5,
                    "source": "yahoo",
                },
            ],
        )
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT minute, close FROM stock_kbar_5m WHERE stock_id='2330'"
        ).fetchone()
        self.assertEqual(row[0], "09:05:00")
        self.assertEqual(row[1], 100.5)


if __name__ == "__main__":
    unittest.main()
