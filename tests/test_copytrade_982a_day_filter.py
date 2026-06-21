"""copytrade_982a_day_filter · 982A 重疊調倉日 filter。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from research.backtest.copytrade_982a_day_filter import (
    build_982a_overlap_day_set,
    filter_grouped_by_overlap_days,
    run_982a_day_filter_study,
)
from research.backtest.copytrade_backtest import group_signals_by_date, iter_copytrade_signals
from stock_db import connect, upsert_etf_holdings, upsert_etf_holdings_meta


def _seed_etf(
    conn: sqlite3.Connection,
    etf_code: str,
    snap: str,
    stocks: list[tuple[str, float]],
) -> None:
    synced = "2026-06-01T00:00:00+00:00"
    upsert_etf_holdings_meta(
        conn,
        {
            "etf_code": etf_code,
            "snapshot_date": snap,
            "nav": 100.0,
            "holding_count": len(stocks),
            "source": "test",
            "source_edit_at": None,
            "synced_at": synced,
        },
    )
    upsert_etf_holdings(
        conn,
        [
            {
                "etf_code": etf_code,
                "snapshot_date": snap,
                "stock_id": sid,
                "stock_name": sid,
                "shares": shares,
                "weight_pct": 5.0,
                "amount": None,
                "source": "test",
                "source_edit_at": None,
                "synced_at": synced,
            }
            for sid, shares in stocks
        ],
    )


class TestCopytrade982aDayFilter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "t.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_overlap_day_set_same_stock_only(self) -> None:
        # 981A adds 2330 on 2026-06-03; 982A adds 2330 same day + 2454 only on 982A
        _seed_etf(self.conn, "00981A", "2026-06-02", [("2330", 100.0)])
        _seed_etf(self.conn, "00981A", "2026-06-03", [("2330", 120.0), ("2317", 50.0)])
        _seed_etf(self.conn, "00982A", "2026-06-02", [("2330", 80.0)])
        _seed_etf(self.conn, "00982A", "2026-06-03", [("2330", 90.0), ("2454", 10.0)])

        overlap = build_982a_overlap_day_set(self.conn, "00981A", "00982A")
        self.assertEqual(overlap, {"2026-06-03"})

        grouped = group_signals_by_date(iter_copytrade_signals(self.conn, "00981A"))
        filtered = filter_grouped_by_overlap_days(grouped, overlap, "overlap")
        self.assertEqual(set(filtered), {"2026-06-03"})
        self.assertEqual(len(filtered["2026-06-03"]), 2)

    def test_run_study_smoke(self) -> None:
        _seed_etf(self.conn, "00981A", "2026-06-02", [("2330", 100.0)])
        _seed_etf(self.conn, "00981A", "2026-06-03", [("2330", 120.0)])
        _seed_etf(self.conn, "00982A", "2026-06-02", [("2330", 80.0)])
        _seed_etf(self.conn, "00982A", "2026-06-03", [("2330", 90.0)])

        out = run_982a_day_filter_study(
            self.conn,
            "00981A",
            strategy_id="L1H9",
            persist=False,
        )
        self.assertIn("batch_id", out)
        self.assertGreaterEqual(out["n_overlap_days"], 1)
        self.assertIn("day_982a_overlap", out["filters"])


if __name__ == "__main__":
    unittest.main()
