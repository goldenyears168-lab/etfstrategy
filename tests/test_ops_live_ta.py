"""Unit tests · disposition auction clock (no network)."""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from ops_live_ta import classify_auction_phase, next_auction, parse_stock_list

_TPE = ZoneInfo("Asia/Taipei")


class TestOpsLiveTa(unittest.TestCase):
    def test_parse_default(self) -> None:
        rows = parse_stock_list("")
        self.assertEqual(rows[0][0], "2492")

    def test_parse_named(self) -> None:
        rows = parse_stock_list("2492:華新科,2330")
        self.assertEqual(rows[0], ("2492", "華新科"))
        self.assertEqual(rows[1][0], "2330")

    def test_between_session(self) -> None:
        now = datetime(2026, 7, 23, 10, 5, tzinfo=_TPE)  # Thu
        phase, action, note, nxt = classify_auction_phase(now)
        self.assertEqual(phase, "between")
        self.assertEqual(action, "觀望")
        self.assertIsNotNone(nxt)
        assert nxt is not None
        self.assertEqual(nxt.strftime("%H:%M"), "10:20")
        self.assertIn("10:20", note)

    def test_pre_match(self) -> None:
        now = datetime(2026, 7, 23, 10, 19, 30, tzinfo=_TPE)
        phase, action, _, nxt = classify_auction_phase(now)
        self.assertEqual(phase, "pre_match")
        self.assertEqual(action, "注意撮合")
        assert nxt is not None
        self.assertEqual(nxt.strftime("%H:%M"), "10:20")

    def test_weekend(self) -> None:
        now = datetime(2026, 7, 25, 10, 0, tzinfo=_TPE)  # Sat
        phase, action, _, nxt = classify_auction_phase(now)
        self.assertEqual(phase, "weekend")
        self.assertEqual(action, "盤後")
        self.assertIsNone(nxt)
        self.assertIsNone(next_auction(now))


if __name__ == "__main__":
    unittest.main()
