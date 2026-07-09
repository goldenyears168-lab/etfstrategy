"""Tests for C18acc rotation-exit · swap block classification & sweep configs."""

from __future__ import annotations

import unittest

from research.backtest.archive.c18acc_rotation_exit_sweep import rotation_exit_sweep_configs
from research.backtest.c18acc_swap_block_audit import classify_daily_swap_block


class TestClassifyDailySwapBlock(unittest.TestCase):
    def test_min_hold_lock(self) -> None:
        reason = classify_daily_swap_block(
            hold_days=3,
            effective_min_hold=5,
            avg_accel=-0.1,
            accel_sell_negative_only=True,
            sort_key="avg_accel_decel",
            quad_bypass=False,
            sell_eligible=True,
            is_worst_held=True,
            has_challenger=True,
            swaps_today=0,
            max_swaps_per_day=1,
            swap_zone_ok=True,
        )
        self.assertEqual(reason, "min_hold_lock")

    def test_accel_sell_negative_only(self) -> None:
        reason = classify_daily_swap_block(
            hold_days=6,
            effective_min_hold=5,
            avg_accel=0.05,
            accel_sell_negative_only=True,
            sort_key="avg_accel_decel",
            quad_bypass=False,
            sell_eligible=False,
            is_worst_held=False,
            has_challenger=False,
            swaps_today=0,
            max_swaps_per_day=1,
            swap_zone_ok=True,
        )
        self.assertEqual(reason, "accel_sell_negative_only")

    def test_quad_bypass_allows_sell_eligible(self) -> None:
        reason = classify_daily_swap_block(
            hold_days=6,
            effective_min_hold=5,
            avg_accel=0.05,
            accel_sell_negative_only=True,
            sort_key="avg_accel_decel",
            quad_bypass=True,
            sell_eligible=True,
            is_worst_held=True,
            has_challenger=False,
            swaps_today=0,
            max_swaps_per_day=1,
            swap_zone_ok=True,
        )
        self.assertEqual(reason, "no_challenger")

    def test_would_swap(self) -> None:
        reason = classify_daily_swap_block(
            hold_days=6,
            effective_min_hold=5,
            avg_accel=-0.2,
            accel_sell_negative_only=True,
            sort_key="avg_accel_decel",
            quad_bypass=False,
            sell_eligible=True,
            is_worst_held=True,
            has_challenger=True,
            swaps_today=0,
            max_swaps_per_day=1,
            swap_zone_ok=True,
        )
        self.assertEqual(reason, "would_swap")

    def test_no_challenger(self) -> None:
        reason = classify_daily_swap_block(
            hold_days=6,
            effective_min_hold=5,
            avg_accel=-0.2,
            accel_sell_negative_only=True,
            sort_key="avg_accel_decel",
            quad_bypass=False,
            sell_eligible=True,
            is_worst_held=True,
            has_challenger=False,
            swaps_today=0,
            max_swaps_per_day=1,
            swap_zone_ok=True,
        )
        self.assertEqual(reason, "no_challenger")


class TestRotationExitSweepConfigs(unittest.TestCase):
    def test_variant_ids(self) -> None:
        ids = [c.variant_id for c in rotation_exit_sweep_configs()]
        self.assertEqual(ids, ["A0", "A1", "A2", "A3", "A4", "A5"])

    def test_a3_quad_weakening(self) -> None:
        a3 = next(c for c in rotation_exit_sweep_configs() if c.variant_id == "A3")
        self.assertEqual(a3.quad_weakening_sell_days, 2)

    def test_a5_loss_waiver(self) -> None:
        a5 = next(c for c in rotation_exit_sweep_configs() if c.variant_id == "A5")
        self.assertEqual(a5.loss_waiver_threshold, -0.05)
        self.assertEqual(a5.loss_waiver_min_hold, 2)


if __name__ == "__main__":
    unittest.main()
