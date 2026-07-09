"""Tests for F1 Friday swap margin sweep helpers."""

from __future__ import annotations

import unittest

from research.backtest.archive.c18acc_friday_swap_margin_sweep import (
    FRIDAY_SWAP_MARGIN_GRID,
    f1_variant_id,
    friday_swap_margin_gates,
)


class TestFridaySwapMarginSweep(unittest.TestCase):
    def test_variant_id(self) -> None:
        self.assertEqual(f1_variant_id(0.03), "F1m030")
        self.assertEqual(f1_variant_id(0.05), "F1m050")

    def test_gates_include_w0(self) -> None:
        gates = friday_swap_margin_gates(margins=(0.03,), include_w0=True)
        self.assertEqual(gates[0].variant_id, "W0")
        self.assertEqual(gates[1].variant_id, "F1m030")
        self.assertEqual(gates[1].friday_swap_margin, 0.03)

    def test_grid_has_baseline(self) -> None:
        self.assertIn(0.05, FRIDAY_SWAP_MARGIN_GRID)
        self.assertIn(0.03, FRIDAY_SWAP_MARGIN_GRID)


if __name__ == "__main__":
    unittest.main()
