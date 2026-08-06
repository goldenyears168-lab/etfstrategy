"""Unit tests for morning_gate gap_bias mapping (hang asymmetry only)."""

from __future__ import annotations

import unittest

from morning_gate_brief import compute_gap_bias


class GapBiasTest(unittest.TestCase):
    def test_flat_near_zero(self):
        self.assertEqual(compute_gap_bias(-0.03, 0.77), "flat")
        self.assertEqual(compute_gap_bias(0.10, 0.77), "flat")

    def test_fire_long_short(self):
        self.assertEqual(compute_gap_bias(0.50, 0.77), "L")  # z≈0.65
        self.assertEqual(compute_gap_bias(-0.50, 0.77), "S")

    def test_asia_conflict_damps(self):
        # z≈0.65 but NQ+ vs NK− → flat
        self.assertEqual(compute_gap_bias(0.50, 0.77, nq=1.0, asia_nk=-0.2), "flat")
        # strong z≥1 still fires
        self.assertEqual(compute_gap_bias(1.0, 0.77, nq=1.0, asia_nk=-0.2), "L")

    def test_missing_pred(self):
        self.assertEqual(compute_gap_bias(None, 0.77), "flat")


if __name__ == "__main__":
    unittest.main()
