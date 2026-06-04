"""stock_daily_bars / stock_institutional_daily schema 與 upsert。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from stock_db import (
    connect,
    count_stock_market_rows,
    load_etf_constituent_watchlist,
    upsert_etf_holdings,
    upsert_etf_holdings_meta,
    upsert_stock_daily_bars,
    upsert_stock_institutional_daily,
)


class TestStockMarketTables(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.conn = connect(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_schema_and_upsert(self) -> None:
        bars = [
            {
                "stock_id": "2330",
                "trade_date": "2026-05-01",
                "open": 900.0,
                "high": 910.0,
                "low": 895.0,
                "close": 905.0,
                "volume": 10000,
                "source": "finmind",
            }
        ]
        inst = [
            {
                "stock_id": "2330",
                "trade_date": "2026-05-01",
                "close_price": 905.0,
                "foreign_net": 100.0,
                "investment_trust_net": 50.0,
                "dealer_self_net": -10.0,
                "three_institution_net": 140.0,
                "source": "finmind",
            }
        ]
        self.assertEqual(upsert_stock_daily_bars(self.conn, bars), 1)
        self.assertEqual(upsert_stock_institutional_daily(self.conn, inst), 1)
        bar_n, inst_n, bar_max, inst_max = count_stock_market_rows(self.conn)
        self.assertEqual(bar_n, 1)
        self.assertEqual(inst_n, 1)
        self.assertEqual(bar_max, "2026-05-01")
        self.assertEqual(inst_max, "2026-05-01")

        bars[0]["close"] = 908.0
        upsert_stock_daily_bars(self.conn, bars)
        row = self.conn.execute(
            "SELECT close FROM stock_daily_bars WHERE stock_id = ? AND trade_date = ?",
            ("2330", "2026-05-01"),
        ).fetchone()
        self.assertEqual(row["close"], 908.0)

    def test_constituent_watchlist_union(self) -> None:
        synced = "2026-06-01T00:00:00+00:00"
        for etf, stock in (("00981A", "2330"), ("00981A", "2317"), ("00403A", "2330")):
            upsert_etf_holdings_meta(
                self.conn,
                {
                    "etf_code": etf,
                    "snapshot_date": "2026-06-01",
                    "nav": 100.0,
                    "holding_count": 2,
                    "source": "test",
                    "source_edit_at": None,
                    "synced_at": synced,
                },
            )
            upsert_etf_holdings(
                self.conn,
                [
                    {
                        "etf_code": etf,
                        "snapshot_date": "2026-06-01",
                        "stock_id": stock,
                        "stock_name": stock,
                        "shares": 1000.0,
                        "weight_pct": 5.0,
                        "amount": None,
                        "source": "test",
                        "source_edit_at": None,
                        "synced_at": synced,
                    }
                ],
            )
        watch = load_etf_constituent_watchlist(self.conn, ("00981A", "00403A"))
        ids = {w["stock_id"] for w in watch}
        self.assertEqual(ids, {"2330", "2317"})
        tsm = next(w for w in watch if w["stock_id"] == "2330")
        self.assertEqual(tsm["etf_hold_count"], 2)


if __name__ == "__main__":
    unittest.main()
