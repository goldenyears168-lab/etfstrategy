"""Tests for c18acc_weekday_gate entry targets."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_weekday_gate import (
    WeekdayGateConfig,
    entry_target_date,
    is_weekday_gate_active,
    should_defer_max_hold_default_to_friday,
    swap_margin_for_day,
)


class TestWeekdayGate(unittest.TestCase):
    def test_w0_inactive(self) -> None:
        self.assertFalse(is_weekday_gate_active(WeekdayGateConfig(variant_id="W0")))

    def test_e1_defer_mon_to_fri(self) -> None:
        full = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
        gate = WeekdayGateConfig(variant_id="E1", entry_mode="E1")
        self.assertEqual(entry_target_date(gate, "2026-01-05", full), "2026-01-09")
        self.assertEqual(entry_target_date(gate, "2026-01-09", full), "2026-01-09")

    def test_e3_mon_to_tue(self) -> None:
        full = ["2026-01-05", "2026-01-06", "2026-01-07"]
        gate = WeekdayGateConfig(variant_id="E3", entry_mode="E3")
        self.assertEqual(entry_target_date(gate, "2026-01-05", full), "2026-01-06")
        self.assertEqual(entry_target_date(gate, "2026-01-06", full), "2026-01-06")

    def test_h1_defer_max_hold_until_friday(self) -> None:
        gate = WeekdayGateConfig(variant_id="H1", max_hold_exit_align="friday")
        self.assertTrue(is_weekday_gate_active(gate))
        self.assertTrue(should_defer_max_hold_default_to_friday(gate, "2026-01-05"))  # Mon
        self.assertTrue(should_defer_max_hold_default_to_friday(gate, "2026-01-08"))  # Thu
        self.assertFalse(should_defer_max_hold_default_to_friday(gate, "2026-01-09"))  # Fri
        w0 = WeekdayGateConfig(variant_id="W0")
        self.assertFalse(should_defer_max_hold_default_to_friday(w0, "2026-01-05"))

    def test_f1_friday_swap_margin(self) -> None:
        gate = WeekdayGateConfig(variant_id="F1", friday_swap_margin=0.03)
        self.assertEqual(swap_margin_for_day(gate, 0.05, "2026-01-09"), 0.03)
        self.assertEqual(swap_margin_for_day(gate, 0.05, "2026-01-08"), 0.05)
        self.assertEqual(swap_margin_for_day(None, 0.05, "2026-01-09"), 0.05)

    def test_friday_pool_and_max_swaps(self) -> None:
        from research.backtest.c18acc_weekday_gate import (
            friday_swap_pool_override,
            max_swaps_for_day,
        )

        gate = WeekdayGateConfig(
            variant_id="FB",
            friday_swap_pool="union_mono",
            friday_max_swaps=2,
        )
        self.assertEqual(
            friday_swap_pool_override(gate, "fresh_union_accel", "2026-01-09"),
            "fresh_union_mono",
        )
        self.assertEqual(
            friday_swap_pool_override(gate, "fresh_union_accel", "2026-01-08"),
            "fresh_union_accel",
        )
        self.assertEqual(max_swaps_for_day(gate, 1, "2026-01-09"), 2)
        self.assertEqual(max_swaps_for_day(gate, 1, "2026-01-08"), 1)


if __name__ == "__main__":
    unittest.main()
