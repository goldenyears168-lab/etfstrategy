"""Tests for ABC v3+f1 overnight regime annot helpers."""

from __future__ import annotations

import unittest

from research.backtest.archive.abc_v3_f1_overnight_regime_annot import _ledger_row_to_period


class TestAbcV3F1OvernightRegimeAnnot(unittest.TestCase):
    def test_ledger_row_to_period(self) -> None:
        row = {
            "stock_id": "2330",
            "stock_name": "台積電",
            "entry_date": "2026-01-02",
            "exit_date": "2026-01-07",
            "net_pct": 1.5,
            "excess_pct": 0.8,
        }
        period = _ledger_row_to_period(row)
        self.assertEqual(period["return_pct"], 1.5)
        self.assertEqual(period["excess_pct"], 0.8)
        self.assertEqual(period["stock_id"], "2330")


if __name__ == "__main__":
    unittest.main()
