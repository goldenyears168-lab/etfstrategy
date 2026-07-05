"""Tests for C18acc rotation-exit · Track B force-exit logic."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from research.backtest.c18acc_rotation_force_exit_sweep import rotation_force_exit_sweep_configs
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    quad_force_exit_eligible,
    quad_force_exit_streak_days,
)


class TestQuadForceExitStreakDays(unittest.TestCase):
    def _panel(self) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
        rs_ratio = pd.DataFrame({"2330": [1.0] * 5, "2454": [1.0] * 5}, index=dates)
        rs_mom = pd.DataFrame({"2330": [1.0] * 5, "2454": [1.0] * 5}, index=dates)
        return rs_ratio, rs_mom, dates

    def _quad_map(self, mapping: dict[str, str]):
        def _fake(
            _rs_ratio: pd.DataFrame,
            _rs_mom: pd.DataFrame,
            _full_dates: list[str],
            as_of: str,
            stock_id: str,
        ) -> str | None:
            if stock_id != "2330":
                return "leading"
            return mapping.get(as_of)

        return patch("research.backtest.rrg_mono_score_swap_c._stock_end_q", side_effect=_fake)

    def test_weakening_streak(self) -> None:
        rs_ratio, rs_mom, dates = self._panel()
        quads = {
            "2024-01-02": "leading",
            "2024-01-03": "weakening",
            "2024-01-04": "lagging",
            "2024-01-05": "lagging",
            "2024-01-08": "lagging",
        }
        with self._quad_map(quads):
            streak = quad_force_exit_streak_days(
                rs_ratio,
                rs_mom,
                dates,
                as_of="2024-01-03",
                stock_id="2330",
                entry_date="2024-01-02",
                mode="weakening",
            )
        self.assertEqual(streak, 1)

    def test_lagging_streak_two_days(self) -> None:
        rs_ratio, rs_mom, dates = self._panel()
        quads = {
            "2024-01-02": "leading",
            "2024-01-03": "weakening",
            "2024-01-04": "lagging",
            "2024-01-05": "lagging",
            "2024-01-08": "lagging",
        }
        with self._quad_map(quads):
            streak = quad_force_exit_streak_days(
                rs_ratio,
                rs_mom,
                dates,
                as_of="2024-01-08",
                stock_id="2330",
                entry_date="2024-01-02",
                mode="lagging",
            )
        self.assertEqual(streak, 3)

    def test_weakening_or_lagging_streak(self) -> None:
        rs_ratio, rs_mom, dates = self._panel()
        quads = {
            "2024-01-02": "leading",
            "2024-01-03": "weakening",
            "2024-01-04": "lagging",
            "2024-01-05": "lagging",
            "2024-01-08": "lagging",
        }
        with self._quad_map(quads):
            streak = quad_force_exit_streak_days(
                rs_ratio,
                rs_mom,
                dates,
                as_of="2024-01-08",
                stock_id="2330",
                entry_date="2024-01-02",
                mode="weakening_or_lagging",
            )
        self.assertEqual(streak, 4)

    def test_streak_breaks_on_leading(self) -> None:
        rs_ratio, rs_mom, dates = self._panel()
        with self._quad_map({"2024-01-08": "leading"}):
            streak = quad_force_exit_streak_days(
                rs_ratio,
                rs_mom,
                dates,
                as_of="2024-01-08",
                stock_id="2454",
                entry_date="2024-01-02",
                mode="weakening_or_lagging",
            )
        self.assertEqual(streak, 0)


class TestQuadForceExitEligible(unittest.TestCase):
    def test_none_mode(self) -> None:
        cfg = ScoreSwapCConfig(quad_force_exit_mode="none")
        self.assertFalse(
            quad_force_exit_eligible(
                cfg, hold_days=10, effective_min_hold=5, streak_days=3
            )
        )

    def test_requires_min_hold(self) -> None:
        cfg = ScoreSwapCConfig(
            quad_force_exit_mode="lagging",
            quad_force_exit_min_days=1,
            quad_force_exit_requires_min_hold=True,
            min_hold_days=5,
        )
        self.assertFalse(
            quad_force_exit_eligible(
                cfg, hold_days=3, effective_min_hold=5, streak_days=2
            )
        )
        self.assertTrue(
            quad_force_exit_eligible(
                cfg, hold_days=5, effective_min_hold=5, streak_days=1
            )
        )

    def test_skips_min_hold_when_disabled(self) -> None:
        cfg = ScoreSwapCConfig(
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=2,
            quad_force_exit_requires_min_hold=False,
        )
        self.assertTrue(
            quad_force_exit_eligible(
                cfg, hold_days=1, effective_min_hold=5, streak_days=2
            )
        )

    def test_min_days_threshold(self) -> None:
        cfg = ScoreSwapCConfig(
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=2,
        )
        self.assertFalse(
            quad_force_exit_eligible(
                cfg, hold_days=6, effective_min_hold=5, streak_days=1
            )
        )
        self.assertTrue(
            quad_force_exit_eligible(
                cfg, hold_days=6, effective_min_hold=5, streak_days=2
            )
        )


class TestRotationForceExitSweepConfigs(unittest.TestCase):
    def test_variant_ids(self) -> None:
        ids = [c.variant_id for c in rotation_force_exit_sweep_configs()]
        self.assertEqual(ids, ["B0", "B1", "B2", "B3", "B4", "B5"])

    def test_b1_lagging_force_exit(self) -> None:
        b1 = next(c for c in rotation_force_exit_sweep_configs() if c.variant_id == "B1")
        self.assertEqual(b1.quad_force_exit_mode, "lagging")
        self.assertEqual(b1.quad_force_exit_min_days, 1)

    def test_b5_combo(self) -> None:
        b5 = next(c for c in rotation_force_exit_sweep_configs() if c.variant_id == "B5")
        self.assertFalse(b5.accel_sell_negative_only)
        self.assertEqual(b5.quad_force_exit_mode, "weakening")
        self.assertEqual(b5.quad_force_exit_min_days, 2)


if __name__ == "__main__":
    unittest.main()
