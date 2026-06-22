"""copytrade_l1h9_daily · markdown structure."""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from copytrade.signals import CopytradeSignal
from copytrade_l1h9_daily import (
    build_copytrade_l1h9_markdown,
    signals_for_date,
)


class TestCopytradeL1h9Daily(unittest.TestCase):
    def test_signals_for_date_empty_when_no_pairs(self) -> None:
        conn = MagicMock(spec=sqlite3.Connection)
        with patch(
            "copytrade_l1h9_daily.list_etf_snapshot_dates",
            return_value=[],
        ):
            score, outcome, sigs = signals_for_date(conn, "2026-06-20")
        self.assertEqual((score, outcome, sigs), ("", "", []))

    def test_build_markdown_lists_signals(self) -> None:
        conn = MagicMock(spec=sqlite3.Connection)
        sig = CopytradeSignal(
            signal_date="2026-06-20",
            stock_id="2330",
            stock_name="台積電",
            action="加码",
            share_delta=1000.0,
            weight_delta=0.5,
        )
        with (
            patch(
                "copytrade_l1h9_daily.list_etf_snapshot_dates",
                return_value=["2026-06-20", "2026-06-18"],
            ),
            patch(
                "copytrade_l1h9_daily.signals_for_date",
                return_value=("2026-06-18", "2026-06-20", [sig]),
            ),
            patch(
                "copytrade_l1h9_daily.build_cross_etf_consensus",
                return_value=[],
            ),
        ):
            md, meta = build_copytrade_l1h9_markdown(conn, as_of="2026-06-20")
        self.assertIn("ETF00981A 跟單策略", md)
        self.assertIn("2330", md)
        self.assertIn("加碼", md)
        self.assertEqual(meta["signal_count"], 1)
        self.assertEqual(meta["strategy_id"], "00981a-l1h9")


if __name__ == "__main__":
    unittest.main()
