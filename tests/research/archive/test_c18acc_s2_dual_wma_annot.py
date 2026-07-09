"""Tests for C18acc+S2 dual-WMA annotation helpers."""

from __future__ import annotations

import unittest

from research.backtest.archive.c18acc_s2_dual_wma_annot import (
    W5Trigger,
    _compare_timing,
    _first_rotation_trigger,
    _w5_deep_pullback,
)


class TestC18accS2DualWmaAnnot(unittest.TestCase):
    def test_w5_deep_pullback(self) -> None:
        p = {
            "w20": {"quadrant": "leading"},
            "w5": {"quadrant": "lagging"},
            "spread_class": "pullback",
            "spread_dist": 4.0,
        }
        self.assertTrue(_w5_deep_pullback(p))

    def test_first_rotation_picks_earliest(self) -> None:
        triggers = [
            W5Trigger("exit_w20_weakening", "w", 5, "2026-02-10", "10:00", None, None, None, None),
            W5Trigger("w5_lagging", "l", 3, "2026-02-08", "10:00", None, None, None, None),
        ]
        first = _first_rotation_trigger(triggers)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.rule_id, "w5_lagging")

    def test_compare_timing_w5_earlier(self) -> None:
        full_dates = ["2026-02-01", "2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05"]
        w5 = W5Trigger("w5_lagging", "l", 1, "2026-02-02", "10:00", None, None, None, None)
        out = _compare_timing(
            full_dates=full_dates,
            entry_date="2026-02-01",
            s2_exit_date="2026-02-05",
            w5_trigger=w5,
        )
        self.assertTrue(out["w5_leads_s2"])
        self.assertEqual(out["timing_bucket"], "w5_earlier")


if __name__ == "__main__":
    unittest.main()
