"""stock_daily_bars / stock_institutional_daily schema 與 upsert。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from stock_db import (
    connect,
    count_stock_market_rows,
    load_etf_constituent_universe_gaps,
    load_etf_constituent_watchlist,
    load_etf_ever_held_constituents,
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
        watch = load_etf_constituent_watchlist(
            self.conn, ("00981A", "00403A"), fund_codes=()
        )
        ids = {w["stock_id"] for w in watch}
        self.assertEqual(ids, {"2330", "2317"})
        tsm = next(w for w in watch if w["stock_id"] == "2330")
        self.assertEqual(tsm["etf_hold_count"], 2)
        self.assertEqual(tsm["fund_hold_count"], 0)
        self.assertEqual(tsm["benchmark_hold_count"], 0)

    def test_constituent_watchlist_includes_benchmark(self) -> None:
        from stock_db.benchmark import upsert_benchmark_constituents, upsert_benchmark_constituents_meta

        synced = "2026-06-01T00:00:00+00:00"
        upsert_benchmark_constituents_meta(
            self.conn,
            {
                "benchmark_code": "0050",
                "snapshot_date": "2026-06-01",
                "holding_count": 2,
                "source": "test",
                "synced_at": synced,
            },
        )
        upsert_benchmark_constituents(
            self.conn,
            [
                {
                    "benchmark_code": "0050",
                    "snapshot_date": "2026-06-01",
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "weight_pct": 20.0,
                    "source": "test",
                    "synced_at": synced,
                },
                {
                    "benchmark_code": "0050",
                    "snapshot_date": "2026-06-01",
                    "stock_id": "2395",
                    "stock_name": "研華",
                    "weight_pct": 1.0,
                    "source": "test",
                    "synced_at": synced,
                },
            ],
        )
        watch = load_etf_constituent_watchlist(
            self.conn,
            (),
            fund_codes=(),
            benchmark_codes=("0050",),
        )
        ids = {w["stock_id"] for w in watch}
        self.assertEqual(ids, {"2330", "2395"})
        adv = next(w for w in watch if w["stock_id"] == "2395")
        self.assertEqual(adv["benchmark_hold_count"], 1)
        self.assertEqual(adv["etf_hold_count"], 0)

    def test_constituent_watchlist_includes_mutual_fund(self) -> None:
        synced = "2026-06-01T00:00:00+00:00"
        from stock_db import upsert_mutual_fund_holdings, upsert_mutual_fund_holdings_meta

        upsert_mutual_fund_holdings_meta(
            self.conn,
            {
                "fund_code": "ACDD04",
                "snapshot_date": "2026-05-31",
                "fund_name": "安聯台灣科技基金",
                "disclosure_type": "monthly_top10",
                "fund_size_billion": None,
                "holding_count": 2,
                "source": "test",
                "source_edit_at": None,
            },
        )
        upsert_mutual_fund_holdings(
            self.conn,
            [
                {
                    "fund_code": "ACDD04",
                    "snapshot_date": "2026-05-31",
                    "disclosure_type": "monthly_top10",
                    "stock_id": "2454",
                    "stock_name": "聯發科",
                    "rank_no": 1,
                    "shares": None,
                    "weight_pct": 10.0,
                    "amount": None,
                    "asset_type": "stock",
                    "source": "test",
                    "source_edit_at": None,
                    "synced_at": synced,
                },
                {
                    "fund_code": "ACDD04",
                    "snapshot_date": "2026-05-31",
                    "disclosure_type": "monthly_top10",
                    "stock_id": "6669",
                    "stock_name": "緯穎",
                    "rank_no": 2,
                    "shares": None,
                    "weight_pct": 8.0,
                    "amount": None,
                    "asset_type": "stock",
                    "source": "test",
                    "source_edit_at": None,
                    "synced_at": synced,
                },
            ],
        )
        watch = load_etf_constituent_watchlist(self.conn, (), fund_codes=("ACDD04",))
        ids = {w["stock_id"] for w in watch}
        self.assertEqual(ids, {"2454", "6669"})
        self.assertTrue(all(w["etf_hold_count"] == 0 for w in watch))
        self.assertTrue(all(w["fund_hold_count"] == 1 for w in watch))

    def test_constituent_watchlist_includes_supplemental(self) -> None:
        from project_config import SUPPLEMENTAL_WATCHLIST_STOCKS

        watch = load_etf_constituent_watchlist(
            self.conn,
            (),
            fund_codes=(),
            benchmark_codes=(),
        )
        ids = {w["stock_id"] for w in watch}
        self.assertTrue(ids.issuperset(SUPPLEMENTAL_WATCHLIST_STOCKS.keys()))
        innolux = next(w for w in watch if w["stock_id"] == "3481")
        self.assertEqual(innolux["stock_name"], "群創")
        self.assertEqual(innolux["supplemental_hold_count"], 1)
        self.assertEqual(innolux["etf_hold_count"], 0)

    def test_ever_held_and_universe_gaps(self) -> None:
        synced = "2026-06-01T00:00:00+00:00"
        rows = [
            ("00981A", "2026-06-01", "2330", 1000.0),
            ("00981A", "2026-06-01", "2317", 1000.0),
            ("00981A", "2026-05-01", "1319", 500.0),
            ("00981A", "2026-05-15", "1319", 0.0),
        ]
        for etf, snap, stock, shares in rows:
            upsert_etf_holdings_meta(
                self.conn,
                {
                    "etf_code": etf,
                    "snapshot_date": snap,
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
                        "snapshot_date": snap,
                        "stock_id": stock,
                        "stock_name": stock,
                        "shares": shares,
                        "weight_pct": 5.0,
                        "amount": None,
                        "source": "test",
                        "source_edit_at": None,
                        "synced_at": synced,
                    }
                ],
            )

        ever = load_etf_ever_held_constituents(self.conn, ("00981A",))
        ever_ids = {r["stock_id"] for r in ever}
        self.assertEqual(ever_ids, {"2330", "2317", "1319"})

        gaps = load_etf_constituent_universe_gaps(self.conn, ("00981A",))
        self.assertEqual([g["stock_id"] for g in gaps], ["1319"])
        self.assertEqual(gaps[0]["first_seen"], "2026-05-01")
        self.assertEqual(gaps[0]["last_seen"], "2026-05-01")


if __name__ == "__main__":
    unittest.main()
