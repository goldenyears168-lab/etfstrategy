"""Tests for RRG × Regime four-axis Phase 3 · RH50 entry gate helpers."""

from __future__ import annotations

import unittest

from research.backtest.archive.rrg_regime_four_axis_phase3 import (
    _delta_pp,
    _evaluate_variant_pass,
    build_rh50_entry_gate_lookup,
    build_rh50_session_flags,
    phase3_variant_specs,
)


class TestRrgRegimeFourAxisPhase3(unittest.TestCase):
    def test_delta_pp(self) -> None:
        self.assertEqual(_delta_pp(1.2, 0.7), 0.5)
        self.assertIsNone(_delta_pp(None, 1.0))

    def test_session_flags_pit_t1(self) -> None:
        full_dates = ["2026-01-02", "2026-01-03", "2026-01-06"]
        rh = {"2026-01-02": 55.0, "2026-01-03": 40.0}
        flags = build_rh50_session_flags(["2026-01-03", "2026-01-06"], full_dates, rh)
        self.assertTrue(flags["2026-01-03"]["rh50_pass"])
        self.assertEqual(flags["2026-01-03"]["regime_t1_as_of"], "2026-01-02")
        self.assertFalse(flags["2026-01-06"]["rh50_pass"])
        self.assertEqual(flags["2026-01-06"]["regime_t1_as_of"], "2026-01-03")

    def test_entry_gate_blocks_unhealthy_days(self) -> None:
        full_dates = ["2026-01-02", "2026-01-03"]
        rh = {"2026-01-02": 30.0}
        allow = {"A", "B", "C"}
        gate, stats = build_rh50_entry_gate_lookup(
            ["2026-01-03"], full_dates, rh, allow, threshold_pct=50.0
        )
        self.assertEqual(gate["2026-01-03"], set())
        self.assertEqual(stats["blocked_session_days"], 1)
        self.assertEqual(stats["allowed_session_days"], 0)

    def test_entry_gate_allows_healthy_days(self) -> None:
        full_dates = ["2026-01-02", "2026-01-03"]
        rh = {"2026-01-02": 60.0}
        allow = {"A", "B"}
        gate, stats = build_rh50_entry_gate_lookup(
            ["2026-01-03"], full_dates, rh, allow, threshold_pct=50.0
        )
        self.assertEqual(gate["2026-01-03"], allow)
        self.assertEqual(stats["allowed_session_days"], 1)

    def test_evaluate_variant_pass(self) -> None:
        base = {
            "FULL": {"n_periods": 100, "mean_excess_pct": 0.5},
            "OOS": {"n_periods": 30, "mean_excess_pct": 0.4},
        }
        gated = {
            "FULL": {"n_periods": 55, "mean_excess_pct": 0.6},
            "OOS": {"n_periods": 25, "mean_excess_pct": 0.55},
        }
        ev = _evaluate_variant_pass(base, gated)
        self.assertTrue(ev["pass"])
        self.assertEqual(ev["capture_rate_full_pct"], 55.0)
        self.assertEqual(ev["OOS_delta_excess_pp"], 0.15)

    def test_phase3_specs(self) -> None:
        ids = {s["variant_id"] for s in phase3_variant_specs()}
        self.assertEqual(ids, {"R0", "P3-H-RH50"})


if __name__ == "__main__":
    unittest.main()
