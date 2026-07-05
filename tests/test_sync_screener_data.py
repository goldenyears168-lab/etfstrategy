"""Screener data parsers and technical indicator tests."""
from __future__ import annotations

import sqlite3
import unittest
from datetime import date, timedelta

from stock_db._schema import _migrate_schema
from sync_futures_institutional_daily import parse_futures_institutional_rows
from sync_fundamentals import parse_dividend_rows, parse_financial_rows
from sync_stock_shareholding_daily import parse_shareholding_rows
from technical_indicators import compute_kd_series, compute_technical_rows


class ScreenerParserTests(unittest.TestCase):
    def test_parse_futures_institutional(self) -> None:
        raw = [
            {
                "date": "2024-01-02",
                "institutional_investors": "Foreign_Investor",
                "long_open_interest_balance_volume": 1000,
                "short_open_interest_balance_volume": 400,
                "long_deal_volume": 50,
                "short_deal_volume": 30,
            }
        ]
        rows = parse_futures_institutional_rows("TX", raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["net_oi_vol"], 600.0)
        self.assertEqual(rows[0]["net_deal_vol"], 20.0)

    def test_parse_shareholding(self) -> None:
        raw = [
            {
                "date": "2024-01-02",
                "ForeignInvestmentRemainingShares": 1_000_000,
                "ForeignInvestmentRemainRatio": 12.5,
                "NumberOfSharesIssued": 2_000_000_000,
            }
        ]
        rows = parse_shareholding_rows("2330", raw)
        self.assertEqual(rows[0]["foreign_remaining_shares"], 1_000_000.0)
        self.assertEqual(rows[0]["shares_issued"], 2_000_000_000.0)

    def test_parse_dividend(self) -> None:
        raw = [
            {
                "year": "2023",
                "date": "2024-06-01",
                "CashEarningsDistribution": 2.0,
                "CashStatutorySurplus": 1.0,
                "CashExDividendTradingDate": "2024-07-15",
                "AnnouncementDate": "2024-05-20",
            }
        ]
        rows = parse_dividend_rows("2330", raw)
        self.assertEqual(rows[0]["cash_dividend"], 3.0)
        self.assertEqual(rows[0]["ex_cash_date"], "2024-07-15")

    def test_operating_margin_from_financials(self) -> None:
        raw = [
            {"date": "2024-03-31", "type": "Revenue", "value": 100.0, "origin_name": "營業收入"},
            {
                "date": "2024-03-31",
                "type": "OperatingIncome",
                "value": 10.0,
                "origin_name": "營業利益",
            },
        ]
        hist, _, _ = parse_financial_rows(raw)
        metrics = {h["metric"]: h["value"] for h in hist}
        self.assertEqual(metrics["revenue"], 100.0)
        self.assertEqual(metrics["operating_income"], 10.0)
        self.assertAlmostEqual(metrics["operating_margin_pct"], 10.0)


class KdCrossTests(unittest.TestCase):
    def test_kd_cross_above_20(self) -> None:
        n = 30
        highs = [100.0] * n
        lows = [90.0] * n
        closes = [90.0] * 20 + [99.0] * 10
        _, k_vals, cross = compute_kd_series(highs, lows, closes)
        self.assertTrue(any(c == 1 for c in cross if cross))
        last_k = [k for k in k_vals if k is not None][-1]
        self.assertGreater(last_k, 50.0)

    def test_compute_technical_rows(self) -> None:
        bars = []
        base = date(2024, 1, 1)
        for i in range(130):
            trade = base + timedelta(days=i)
            px = 100.0 + i * 0.2
            bars.append(
                {
                    "trade_date": trade.isoformat(),
                    "open": px,
                    "high": px + 1,
                    "low": px - 1,
                    "close": px,
                    "volume": 1000 + i,
                }
            )
        rows = compute_technical_rows("2330", bars)
        self.assertEqual(len(rows), 130)
        self.assertIsNotNone(rows[-1]["ma20"])
        self.assertIsNotNone(rows[-1]["high_120d"])


class SchemaMigrationTests(unittest.TestCase):
    def test_screener_tables_created(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS stock_financial_history ("
            "stock_id TEXT, period_date TEXT, period_type TEXT, metric TEXT,"
            "value REAL, source TEXT, synced_at TEXT,"
            "PRIMARY KEY (stock_id, period_date, period_type, metric, source));"
        )
        from stock_db._schema import _SCHEMA

        conn.executescript(_SCHEMA)
        _migrate_schema(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for name in (
            "futures_institutional_daily",
            "stock_shareholding_daily",
            "stock_dividend_history",
            "stock_market_value_daily",
            "stock_technical_daily",
        ):
            self.assertIn(name, tables)
        conn.close()


if __name__ == "__main__":
    unittest.main()
