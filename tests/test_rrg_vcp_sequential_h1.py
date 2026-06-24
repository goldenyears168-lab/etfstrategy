"""Tests for H1 RRG → VCP sequential backtest helpers."""

from __future__ import annotations

import unittest

from research.backtest.rrg_vcp_sequential_h1 import _trading_deadline


class TestRrgVcpSequentialH1(unittest.TestCase):
    def test_trading_deadline(self) -> None:
        dates = ["2026-06-17", "2026-06-18", "2026-06-22", "2026-06-23"]
        self.assertEqual(_trading_deadline(dates, "2026-06-17", 0), "2026-06-17")
        self.assertEqual(_trading_deadline(dates, "2026-06-17", 2), "2026-06-22")
        self.assertIsNone(_trading_deadline(dates, "2026-06-99", 1))


if __name__ == "__main__":
    unittest.main()
