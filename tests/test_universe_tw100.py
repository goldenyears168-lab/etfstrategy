"""TW100 universe + extended stock market schema tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from stock_db import (
    connect,
    upsert_stock_daily_bars,
    upsert_stock_institutional_side_daily,
)
from stock_universe import (
    TW100_UNIVERSE_ID,
    UniverseSnapshotError,
    fetch_tw100_constituents,
    load_universe_constituents,
    upsert_universe_snapshot,
)
from sync_stock_market_daily import build_institutional_side_rows, build_stock_rows


class TestTw100Universe(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_universe_snapshot_roundtrip(self) -> None:
        constituents = [
            {"stock_id": "2330", "stock_name": "台積電", "rank_no": 1, "market_value": 1e12},
            {"stock_id": "2317", "stock_name": "鴻海", "rank_no": 2, "market_value": 5e11},
        ]
        n = upsert_universe_snapshot(
            self.conn, TW100_UNIVERSE_ID, "2026-06-26", constituents
        )
        self.assertEqual(n, 2)
        loaded = load_universe_constituents(self.conn, TW100_UNIVERSE_ID, "2026-06-26")
        self.assertEqual([w["stock_id"] for w in loaded], ["2330", "2317"])

    @patch("stock_universe.fetch_finmind_dataset")
    def test_fetch_tw100_constituents_rank_and_extra(self, mock_fetch) -> None:
        mock_fetch.return_value = [
            {"date": "2026-06-26", "stock_id": "2330", "market_value": 100},
            {"date": "2026-06-26", "stock_id": "2317", "market_value": 90},
            {"date": "2026-06-26", "stock_id": "2834", "market_value": 80},
            {"date": "2026-06-26", "stock_id": "00400A", "market_value": 999},
        ]
        rows = fetch_tw100_constituents(
            date(2026, 6, 26),
            rank_limit=2,
            extra_stock_ids=["2834"],
        )
        ids = [r["stock_id"] for r in rows]
        self.assertEqual(ids, ["2330", "2317", "2834"])
        self.assertEqual(rows[2]["rank_no"], 3)

    @patch("stock_universe.fetch_finmind_dataset")
    def test_fetch_tw100_excludes_etfs(self, mock_fetch) -> None:
        mock_fetch.return_value = [
            {"date": "2026-06-26", "stock_id": "0050", "market_value": 200},
            {"date": "2026-06-26", "stock_id": "2330", "market_value": 100},
            {"date": "2026-06-26", "stock_id": "2317", "market_value": 90},
            {"date": "2026-06-26", "stock_id": "2454", "market_value": 80},
        ]
        rows = fetch_tw100_constituents(
            date(2026, 6, 26),
            rank_limit=2,
            exclude_stock_ids=["0050", "0056"],
        )
        self.assertEqual([r["stock_id"] for r in rows], ["2330", "2317"])

    def test_hardened_snapshot_rejects_small(self) -> None:
        with self.assertRaises(UniverseSnapshotError):
            upsert_universe_snapshot(
                self.conn,
                TW100_UNIVERSE_ID,
                "2026-06-29",
                [{"stock_id": "2330", "rank_no": 1, "market_value": 1.0}],
                min_constituent_count=95,
            )

    def test_load_latest_qualified_snapshot(self) -> None:
        good = [{"stock_id": f"{i:04d}", "rank_no": i, "market_value": 1.0} for i in range(100)]
        upsert_universe_snapshot(self.conn, TW100_UNIVERSE_ID, "2026-06-26", good)
        upsert_universe_snapshot(
            self.conn,
            TW100_UNIVERSE_ID,
            "2026-06-29",
            [{"stock_id": "2330", "rank_no": 1, "market_value": 1.0}],
        )
        loaded = load_universe_constituents(
            self.conn, TW100_UNIVERSE_ID, min_constituent_count=95
        )
        self.assertEqual(len(loaded), 100)


class TestExtendedStockMarket(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_amount_and_institutional_side_upsert(self) -> None:
        bars = [
            {
                "stock_id": "2330",
                "trade_date": "2026-06-01",
                "open": 900.0,
                "high": 910.0,
                "low": 895.0,
                "close": 905.0,
                "adj_close": 900.0,
                "volume": 10000,
                "amount": 9_050_000.0,
                "source": "finmind",
            }
        ]
        side = [
            {
                "stock_id": "2330",
                "trade_date": "2026-06-01",
                "inst_name": "Foreign_Investor",
                "buy": 1000.0,
                "sell": 500.0,
                "net": 500.0,
                "source": "finmind",
            }
        ]
        self.assertEqual(upsert_stock_daily_bars(self.conn, bars), 1)
        self.assertEqual(upsert_stock_institutional_side_daily(self.conn, side), 1)
        row = self.conn.execute(
            "SELECT amount, adj_close FROM stock_daily_bars WHERE stock_id = ?",
            ("2330",),
        ).fetchone()
        self.assertEqual(row["amount"], 9_050_000.0)
        self.assertEqual(row["adj_close"], 900.0)

    def test_build_institutional_side_rows(self) -> None:
        raw = [
            {
                "date": "2026-06-01",
                "name": "Foreign_Investor",
                "buy": 100,
                "sell": 40,
            },
            {"date": "2026-06-01", "name": "Dealer_Hedging", "buy": 0, "sell": 10},
        ]
        rows = build_institutional_side_rows("2330", raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["net"], 60.0)

    @patch("sync_stock_market_daily.fetch_finmind")
    def test_build_stock_rows_includes_adj_and_amount(self, mock_fetch) -> None:
        def _fake(dataset, stock_id, start, end):  # noqa: ARG001
            if dataset == "TaiwanStockPrice":
                return [
                    {
                        "date": "2026-06-01",
                        "open": 100,
                        "max": 101,
                        "min": 99,
                        "close": 100.5,
                        "Trading_Volume": 1000,
                        "Trading_money": 100500.0,
                    }
                ]
            if dataset == "TaiwanStockPriceAdj":
                return [{"date": "2026-06-01", "close": 99.0}]
            if dataset == "TaiwanStockInstitutionalInvestorsBuySell":
                return [
                    {
                        "date": "2026-06-01",
                        "name": "Foreign_Investor",
                        "buy": 10,
                        "sell": 5,
                    },
                    {
                        "date": "2026-06-01",
                        "name": "Investment_Trust",
                        "buy": 0,
                        "sell": 0,
                    },
                    {
                        "date": "2026-06-01",
                        "name": "Dealer_self",
                        "buy": 1,
                        "sell": 1,
                    },
                ]
            return []

        mock_fetch.side_effect = _fake
        bars, inst, side = build_stock_rows("2330", date(2026, 6, 1), date(2026, 6, 1))
        self.assertEqual(bars[0]["adj_close"], 99.0)
        self.assertEqual(bars[0]["amount"], 100500.0)
        self.assertEqual(inst[0]["foreign_net"], 5.0)
        self.assertGreaterEqual(len(side), 3)


if __name__ == "__main__":
    unittest.main()
