"""price_adapter：FinMind tick + manual fallback。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from price_adapter import (
    fetch_finmind_tick_rows,
    prices_from_tick_rows,
    resolve_snapshot_prices,
    yahoo_suffix_order,
)


class TestPriceAdapter(unittest.TestCase):
    def test_prices_from_tick_rows(self) -> None:
        rows = [
            {"stock_id": "2330", "close": 1010.0},
            {"stock_id": "6223", "close": 0},
            {"stock_id": "1303", "close": 105.5},
        ]
        self.assertEqual(prices_from_tick_rows(rows), {"2330": 1010.0, "1303": 105.5})

    def test_manual_only(self) -> None:
        prices, label, warnings = resolve_snapshot_prices(
            ["2330", "6223"],
            manual={"2330": 1000.0},
            source="manual",
        )
        self.assertEqual(prices, {"2330": 1000.0})
        self.assertIsNone(label)
        self.assertIn("6223", warnings[0])

    @patch("price_adapter.fetch_finmind_tick_rows")
    def test_finmind_with_manual_override(self, mock_fetch) -> None:
        mock_fetch.return_value = (
            [
                {"stock_id": "2330", "close": 1010.0},
                {"stock_id": "6223", "close": 5800.0},
            ],
            None,
        )
        prices, label, warnings = resolve_snapshot_prices(
            ["2330", "6223"],
            manual={"2330": 1005.0},
            source="finmind",
        )
        self.assertEqual(prices["2330"], 1005.0)
        self.assertEqual(prices["6223"], 5800.0)
        self.assertEqual(label, "finmind_tick")
        self.assertEqual(warnings, [])

    @patch("price_adapter.fetch_finmind_tick_rows")
    def test_finmind_failure_fallback_manual(self, mock_fetch) -> None:
        mock_fetch.return_value = ([], "API error")
        prices, label, warnings = resolve_snapshot_prices(
            ["2330", "6223"],
            manual={"6223": 5800.0},
            source="finmind",
        )
        self.assertEqual(prices, {"6223": 5800.0})
        self.assertIsNone(label)
        self.assertTrue(any("FinMind" in w for w in warnings))
        self.assertTrue(any("2330" in w for w in warnings))

    @patch("price_adapter.finmind_token", return_value="tok")
    @patch("price_adapter.fetch_finmind_tick_rows")
    def test_auto_uses_finmind_when_token(self, mock_fetch, _token) -> None:
        mock_fetch.return_value = ([{"stock_id": "2330", "close": 1000.0}], None)
        with patch.dict(os.environ, {"FINMIND_TOKEN": "tok"}):
            prices, label, _ = resolve_snapshot_prices(["2330"], source="auto")
        self.assertEqual(prices["2330"], 1000.0)
        self.assertEqual(label, "finmind_tick")

    @patch("price_adapter.fetch_yahoo_last_prices")
    def test_yahoo_source(self, mock_yahoo) -> None:
        mock_yahoo.return_value = {"2330": 2380.0, "6223": 5760.0}
        prices, label, warnings = resolve_snapshot_prices(
            ["2330", "6223"],
            source="yahoo",
        )
        self.assertEqual(prices, {"2330": 2380.0, "6223": 5760.0})
        self.assertEqual(label, "yahoo_1m")
        self.assertTrue(any("Yahoo 1m 報價" in w for w in warnings))

    def test_yahoo_suffix_order_otc_first(self) -> None:
        self.assertEqual(yahoo_suffix_order("6223", {"6223": "OTC"}), ("TWO", "TW"))
        self.assertEqual(yahoo_suffix_order("2330", {"2330": "TSE"}), ("TW", "TWO"))

    @patch("price_adapter.fetch_yahoo_last_prices")
    @patch("price_adapter.fetch_finmind_tick_rows")
    def test_auto_yahoo_fallback_when_finmind_fails(self, mock_fetch, mock_yahoo) -> None:
        mock_fetch.return_value = ([], "Your level is register")
        mock_yahoo.return_value = {"2330": 2380.0, "6223": 5835.0, "1303": 105.0}
        prices, label, warnings = resolve_snapshot_prices(
            ["2330", "6223", "1303"],
            source="auto",
        )
        self.assertEqual(prices, {"2330": 2380.0, "6223": 5835.0, "1303": 105.0})
        self.assertEqual(label, "yahoo_1m")
        self.assertTrue(any("Yahoo" in w for w in warnings))
        self.assertFalse(any(w.startswith("缺少報價") for w in warnings))

    @patch("price_adapter.fetch_tick_snapshots")
    def test_fetch_finmind_tick_rows_batches(self, mock_snap) -> None:
        mock_snap.return_value = ([{"stock_id": "2330"}], None)
        rows, err = fetch_finmind_tick_rows(["2330", "2454"], batch_size=1)
        self.assertIsNone(err)
        self.assertEqual(len(rows), 2)
        self.assertEqual(mock_snap.call_count, 2)


if __name__ == "__main__":
    unittest.main()
