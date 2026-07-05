"""Tests for Phase W-stack."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_s2_dual_wma_stack import _trigger_sort_key


class TestWStack(unittest.TestCase):
    def test_trigger_sort_key(self) -> None:
        a = {"trade_date": "2026-01-02", "minute": "10:00"}
        b = {"trade_date": "2026-01-02", "minute": "09:30"}
        self.assertLess(_trigger_sort_key(b), _trigger_sort_key(a))


if __name__ == "__main__":
    unittest.main()
