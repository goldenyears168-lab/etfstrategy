"""Tests for c18acc_entry_momentum_gate_study."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_entry_momentum_gate_study import _point_momentum


class TestEntryMomentum(unittest.TestCase):
    def _pt(self, w3_mv: float, w3_rv: float, w3_q: str = "improving") -> dict:
        leg = lambda mv, rv, q: {"rs_momentum": mv, "rs_ratio": rv, "quadrant": q}
        return {
            "minute": "09:35",
            "w3": leg(w3_mv, w3_rv, w3_q),
            "w5": leg(99.0, 99.5, "improving"),
            "w20": leg(98.0, 99.0, "improving"),
            "w2": leg(100.0, 99.8, "improving"),
            "spread_class": "mixed",
            "spread_dist": 1.0,
        }

    def test_momentum_ok_when_mv_high_and_rv_rising(self) -> None:
        prev = self._pt(100.5, 99.0)
        entry = self._pt(101.2, 99.6)
        m = _point_momentum(entry, prev)
        self.assertTrue(m["w3_mv_ge_100"])
        self.assertTrue(m["w3_rv_rising_poll"])
        self.assertTrue(m["w3_momentum_ok"])

    def test_falling_when_both_flat_or_down(self) -> None:
        prev = self._pt(101.0, 100.0)
        entry = self._pt(100.5, 99.8)
        m = _point_momentum(entry, prev)
        self.assertTrue(m["w3_both_falling_poll"])
        self.assertFalse(m["w3_momentum_ok"])


if __name__ == "__main__":
    unittest.main()
