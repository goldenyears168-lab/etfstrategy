"""Tests for ABC v3 × RH50 entry gate helpers."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from research.backtest.archive.abc_v3_rh50_gate import (
    _delta_pp,
    _evaluate,
    _leg_stats,
)
from research.backtest.archive.rrg_regime_four_axis_phase3 import build_rotation_health_by_date


class TestAbcV3Rh50Gate(unittest.TestCase):
    def test_delta_pp(self) -> None:
        self.assertEqual(_delta_pp(2.0, 1.5), 0.5)
        self.assertIsNone(_delta_pp(1.0, None))

    def test_leg_stats(self) -> None:
        legs = [
            {"net_pct": 1.0, "excess_pct": 0.8},
            {"net_pct": -0.5, "excess_pct": -0.3},
            {"net_pct": 2.0, "excess_pct": 1.2},
        ]
        ls = _leg_stats(legs)
        self.assertEqual(ls["n_legs"], 3)
        self.assertAlmostEqual(ls["mean_net_pct"], 0.8333, places=3)
        self.assertAlmostEqual(ls["mean_excess_pct"], 0.5667, places=3)
        self.assertAlmostEqual(ls["win_rate_pct"], 66.67, places=1)

    def test_leg_stats_empty(self) -> None:
        ls = _leg_stats([])
        self.assertEqual(ls["n_legs"], 0)
        self.assertIsNone(ls["mean_excess_pct"])

    def test_evaluate_pass(self) -> None:
        base = {
            "FULL": {"leg_stats": {"n_legs": 100, "mean_excess_pct": 0.5}},
            "OOS": {
                "leg_stats": {"n_legs": 30, "mean_excess_pct": 0.4},
                "portfolio": {"mean_daily_return_pct": 0.1},
            },
        }
        gated = {
            "FULL": {"leg_stats": {"n_legs": 55, "mean_excess_pct": 0.7}},
            "OOS": {
                "leg_stats": {"n_legs": 25, "mean_excess_pct": 0.55},
                "portfolio": {"mean_daily_return_pct": 0.15},
            },
        }
        ev = _evaluate(base, gated)
        self.assertTrue(ev["pass"])
        self.assertEqual(ev["capture_rate_full_pct"], 55.0)
        self.assertEqual(ev["OOS_delta_excess_pp"], 0.15)
        self.assertEqual(ev["OOS_delta_daily_pp"], 0.05)

    def test_evaluate_fail_on_oos_drop(self) -> None:
        base = {
            "FULL": {"leg_stats": {"n_legs": 100, "mean_excess_pct": 0.5}},
            "OOS": {
                "leg_stats": {"n_legs": 30, "mean_excess_pct": 1.0},
                "portfolio": {"mean_daily_return_pct": 0.2},
            },
        }
        gated = {
            "FULL": {"leg_stats": {"n_legs": 60, "mean_excess_pct": 0.3}},
            "OOS": {
                "leg_stats": {"n_legs": 22, "mean_excess_pct": 0.2},
                "portfolio": {"mean_daily_return_pct": 0.05},
            },
        }
        ev = _evaluate(base, gated)
        self.assertFalse(ev["pass"])
        self.assertLess(ev["OOS_delta_excess_pp"], -0.2)

    def test_build_rotation_health_passes_length(self) -> None:
        import pandas as pd

        close = pd.DataFrame(
            {"A": [100.0, 101.0, 102.0]},
            index=["2026-01-01", "2026-01-02", "2026-01-03"],
        )
        bench = pd.Series(
            [100.0, 100.5, 101.0],
            index=["2026-01-01", "2026-01-02", "2026-01-03"],
        )
        captured: dict[str, Any] = {}

        def _fake_panel(_close, _bench, *, length=20, mom_length=None):
            captured["length"] = length
            empty = pd.DataFrame()
            return empty, empty, empty

        with (
            patch(
                "research.backtest.archive.rrg_regime_four_axis_phase3.load_price_panels",
                return_value=(close, None, None),
            ),
            patch(
                "research.backtest.archive.rrg_regime_four_axis_phase3.load_benchmark_close",
                return_value=bench,
            ),
            patch(
                "research.backtest.archive.rrg_regime_four_axis_phase3.compute_rrg_panel",
                side_effect=_fake_panel,
            ),
            patch(
                "research.backtest.archive.rrg_regime_four_axis_phase3._universe_rrg_stats_from_quad",
                return_value={},
            ),
        ):
            build_rotation_health_by_date(None, length=5)  # type: ignore[arg-type]
        self.assertEqual(captured["length"], 5)


if __name__ == "__main__":
    unittest.main()
