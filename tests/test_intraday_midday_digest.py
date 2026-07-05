"""Tests for intraday_midday_digest."""

from __future__ import annotations

import unittest

from intraday_midday_digest import MiddayDigestInput, format_intraday_midday_digest


class IntradayMiddayDigestTests(unittest.TestCase):
    def test_format_with_holdings(self) -> None:
        inp = MiddayDigestInput(session_date="2026-07-03")
        holdings = "■ 持倉即時脈動（advisory · 非出清指令）\n  輪詢 11:00"
        text = format_intraday_midday_digest(inp, holdings_digest=holdings)
        self.assertIn("11:00 盤中持倉脈動", text)
        self.assertIn("1m K @ 輪詢分鐘", text)
        self.assertIn("持倉即時脈動", text)


if __name__ == "__main__":
    unittest.main()
