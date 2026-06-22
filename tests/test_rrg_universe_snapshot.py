"""Tests for RRG universe snapshot (per-stock SQLite persist)."""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from rrg_universe_snapshot import (
    _close_bars_ready,
    build_universe_rows_from_panels,
    run_close_universe_snapshot,
)
from stock_db.rrg import load_rrg_universe_scores, replace_rrg_universe_scores


_RRG_TABLE_SQL = """
CREATE TABLE rrg_universe_scores (
    session_date TEXT NOT NULL,
    screen_kind TEXT NOT NULL,
    data_baseline_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    rs_ratio REAL,
    rs_momentum REAL,
    quadrant TEXT,
    quadrants_json TEXT,
    trend TEXT,
    disp REAL,
    seg_last REAL,
    segs_json TEXT,
    tier2 INTEGER NOT NULL DEFAULT 0,
    mono_tier2 INTEGER NOT NULL DEFAULT 0,
    mono_fresh INTEGER NOT NULL DEFAULT 0,
    daily_pct REAL,
    tick_ok INTEGER,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (session_date, screen_kind, stock_id)
);
CREATE TABLE stock_daily_bars (
    stock_id TEXT,
    trade_date TEXT,
    source TEXT
);
"""


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_RRG_TABLE_SQL)
    return conn


def _mock_panels(stock_ids: list[str], dates: list[str]) -> tuple[pd.DataFrame, pd.Series]:
    idx = pd.Index(dates, name="date")
    close = pd.DataFrame(
        {sid: np.linspace(100, 110, len(dates)) for sid in stock_ids},
        index=idx,
    )
    bench = pd.Series(np.linspace(1000, 1050, len(dates)), index=idx, name="IX0001")
    return close, bench


class RrgUniverseSnapshotTests(unittest.TestCase):
    def test_build_universe_rows_count_matches_watchlist(self) -> None:
        conn = _memory_conn()
        stocks = ["2330", "2317", "2454"]
        close, bench = _mock_panels(stocks, [f"2026-06-{d:02d}" for d in range(1, 25)])
        as_of = "2026-06-22"

        with patch(
            "rrg_universe_snapshot.load_etf_constituent_watchlist",
            return_value=[
                {"stock_id": s, "stock_name": f"Name{s}"} for s in stocks
            ],
        ):
            rows = build_universe_rows_from_panels(
                conn,
                as_of,
                close,
                bench,
                data_baseline_date="2026-06-21",
                tick_ok_by_id={"2330": True},
            )

        self.assertEqual(len(rows), len(stocks))
        self.assertEqual({r["stock_id"] for r in rows}, set(stocks))
        self.assertEqual(rows[0]["tick_ok"], 1)
        self.assertEqual(rows[1]["tick_ok"], 0)
        conn.close()

    def test_sqlite_replace_roundtrip(self) -> None:
        conn = _memory_conn()
        rows = [
            {
                "data_baseline_date": "2026-06-22",
                "stock_id": "2330",
                "stock_name": "台積電",
                "rs_ratio": 102.0,
                "rs_momentum": 101.0,
                "quadrant": "Leading",
                "quadrants_json": '["Leading"]',
                "trend": "up",
                "disp": 1.5,
                "seg_last": 0.8,
                "segs_json": "[0.8]",
                "tier2": 1,
                "mono_tier2": 0,
                "mono_fresh": 0,
                "daily_pct": 1.2,
                "tick_ok": 1,
            }
        ]
        replace_rrg_universe_scores(
            conn, session_date="2026-06-22", screen_kind="close", rows=rows
        )
        loaded = load_rrg_universe_scores(conn, "2026-06-22", "close")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["stock_id"], "2330")

        replace_rrg_universe_scores(
            conn, session_date="2026-06-22", screen_kind="close", rows=[]
        )
        self.assertEqual(load_rrg_universe_scores(conn, "2026-06-22", "close"), [])
        conn.close()

    def test_close_bars_ready_requires_min_bars(self) -> None:
        conn = _memory_conn()
        for i in range(49):
            conn.execute(
                """
                INSERT INTO stock_daily_bars (stock_id, trade_date, source)
                VALUES (?, '2026-06-22', 'finmind')
                """,
                (f"{i:04d}",),
            )
        conn.commit()
        self.assertFalse(_close_bars_ready(conn, "2026-06-22", min_bars=50))
        conn.execute(
            "INSERT INTO stock_daily_bars (stock_id, trade_date, source) VALUES ('9999', '2026-06-22', 'finmind')"
        )
        conn.commit()
        self.assertTrue(_close_bars_ready(conn, "2026-06-22", min_bars=50))
        conn.close()

    @patch("rrg_universe_snapshot.load_price_panels")
    @patch("rrg_universe_snapshot.load_benchmark_close")
    @patch("rrg_universe_snapshot.latest_trading_date", return_value="2026-06-22")
    def test_run_close_skips_without_bars(
        self,
        _mock_latest,
        _mock_bench,
        _mock_panels,
    ) -> None:
        conn = _memory_conn()
        n, session = run_close_universe_snapshot(conn, session_date="2026-06-22")
        self.assertEqual(n, 0)
        self.assertIsNone(session)
        conn.close()


if __name__ == "__main__":
    unittest.main()
