"""C18acc full_rrg unconstrained study · unit tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from research.backtest.c18acc_full_rrg_unconstrained_study import (
    _apply_gate,
    _build_verdict_gates,
    _entry_event_set,
    _leg_stats,
    _strict_kbar_close,
    compute_unconstrained_leg,
    derive_set_variant_events,
    render_c18acc_full_rrg_unconstrained_md,
)


class FullRrgUnconstrainedStudyTests(unittest.TestCase):
    def test_derive_set_variant_events(self) -> None:
        v0 = [
            {"stock_id": "2330", "entry_date": "2025-06-02", "variant": "V0"},
            {"stock_id": "3711", "entry_date": "2025-06-02", "variant": "V0"},
        ]
        v1 = [
            {"stock_id": "2330", "entry_date": "2025-06-02", "variant": "V1"},
            {"stock_id": "6488", "entry_date": "2025-06-02", "variant": "V1"},
        ]
        derived = derive_set_variant_events(v0, v1)
        self.assertEqual(_entry_event_set(derived["V2"]), {("2330", "2025-06-02")})
        self.assertEqual(_entry_event_set(derived["V3"]), {("6488", "2025-06-02")})
        self.assertEqual(_entry_event_set(derived["V4"]), {("3711", "2025-06-02")})
        self.assertEqual(derived["V3"][0]["variant"], "V3")

    def test_apply_gate_whitelist(self) -> None:
        gate = {"2025-06-02": {"2330", "3711"}}
        ids = _apply_gate({"2330", "9999"}, "2025-06-02", gate)
        self.assertEqual(ids, {"2330"})

    def test_leg_stats_mean_excess(self) -> None:
        legs = [
            {"net_pct": 2.0, "excess_pct": 1.0},
            {"net_pct": -1.0, "excess_pct": -0.5},
        ]
        st = _leg_stats(legs, hold_days=5)
        self.assertEqual(st["n"], 2)
        self.assertEqual(st["mean_ret_pct"], 0.5)
        self.assertEqual(st["mean_excess_pct"], 0.25)
        self.assertEqual(st["win_rate_pct"], 50.0)

    def test_compute_leg_skips_missing_entry_kbar(self) -> None:
        conn = MagicMock()
        kbar_cache: dict = {}
        with patch(
            "research.backtest.c18acc_full_rrg_unconstrained_study.exit_close_date_from_entry",
            return_value="2025-06-09",
        ), patch(
            "research.backtest.c18acc_full_rrg_unconstrained_study._strict_kbar_close",
            return_value=None,
        ):
            leg, reason = compute_unconstrained_leg(
                conn,
                stock_id="2330",
                entry_date="2025-06-02",
                entry_minute="09:35",
                hold_days=5,
                kbar_cache=kbar_cache,
            )
        self.assertIsNone(leg)
        self.assertEqual(reason, "missing_entry_kbar")

    def test_strict_kbar_no_daily_fallback(self) -> None:
        conn = MagicMock()
        kbar_cache: dict = {}
        with patch(
            "research.backtest.c18acc_full_rrg_unconstrained_study.load_kbar_day_5m_closes",
            return_value=(),
        ):
            px = _strict_kbar_close(conn, "2330", "2025-06-02", "09:35", kbar_cache)
        self.assertIsNone(px)

    def test_verdict_gates_structure(self) -> None:
        payload = {
            "event_counts": {"V0": 100},
            "set_ops": {"shared_n": 80, "shared_stats": {"mean_excess_pct": 1.5}},
            "variants": {
                "V0": {
                    "n_legs": 90,
                    "slices": {
                        "full": {"n": 90, "mean_excess_pct": 1.0},
                        "is": {"n": 60, "mean_excess_pct": 1.1},
                        "oos": {"n": 30, "mean_excess_pct": 0.8},
                    },
                },
                "V1": {
                    "n_legs": 85,
                    "slices": {
                        "full": {"n": 85, "mean_excess_pct": 1.4},
                        "is": {"n": 55, "mean_excess_pct": 1.0},
                        "oos": {"n": 30, "mean_excess_pct": 1.2},
                    },
                },
                "V3": {"slices": {"full": {"n": 5, "mean_excess_pct": 0.5}}},
                "V4": {"slices": {"full": {"n": 10, "mean_excess_pct": 0.2}}},
            },
        }
        gates = _build_verdict_gates(payload)
        self.assertIn("G2_oos", gates)
        self.assertIn("G3_is", gates)
        self.assertIn("G4_boundary", gates)
        self.assertIn("G5_coverage", gates)
        self.assertIn("G6_live_feasible", gates)
        self.assertEqual(gates["G2_oos"]["delta_pp"], 0.4)
        self.assertTrue(gates["G2_oos"]["pass"])
        self.assertTrue(gates["G3_is"]["pass"])

    def test_render_md_contains_hypothesis_gates(self) -> None:
        payload = {
            "date_start": "2025-01-02",
            "date_end": "2026-07-09",
            "is_end": "2025-12-31",
            "oos_start": "2026-01-02",
            "hold_days": 5,
            "entry_minute": "09:35",
            "confirm_bars": 2,
            "confirm_anchor_minute": "09:30",
            "set_ops": {"shared_n": 1, "v0_only_n": 0, "v1_only_n": 0},
            "comparison": {"v1_vs_v0_full_delta_excess_pp": 0.1},
            "variants": {
                vid: {
                    "label": vid,
                    "n_events": 1,
                    "n_legs": 1,
                    "slices": {
                        "full": {"n": 1, "mean_excess_pct": 1.0},
                        "is": {"n": 1, "mean_excess_pct": 1.0},
                        "oos": {"n": 0, "mean_excess_pct": None},
                    },
                }
                for vid in ("V0", "V1", "V2", "V3", "V4")
            },
            "verdict_gates": _build_verdict_gates(
                {
                    "event_counts": {"V0": 1},
                    "set_ops": {"shared_n": 1, "shared_stats": {"mean_excess_pct": 1.0}},
                    "variants": {
                        "V0": {
                            "n_legs": 1,
                            "slices": {
                                "full": {"n": 1, "mean_excess_pct": 1.0},
                                "is": {"n": 1, "mean_excess_pct": 1.0},
                                "oos": {"n": 0},
                            },
                        },
                        "V1": {
                            "n_legs": 1,
                            "slices": {
                                "full": {"n": 1, "mean_excess_pct": 1.1},
                                "is": {"n": 1, "mean_excess_pct": 1.0},
                                "oos": {"n": 0},
                            },
                        },
                        "V3": {"slices": {"full": {"n": 0}}},
                        "V4": {"slices": {"full": {"n": 0}}},
                    },
                }
            ),
        }
        md = render_c18acc_full_rrg_unconstrained_md(payload)
        self.assertIn("full_rrg vs PIT fresh", md)
        self.assertIn("G2 OOS", md)
        self.assertIn("V0 vs V1", md)


if __name__ == "__main__":
    unittest.main()
