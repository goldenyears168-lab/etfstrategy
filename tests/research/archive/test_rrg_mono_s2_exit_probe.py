"""Tests for RRG mono hold7 S2 exit probe helpers."""

from __future__ import annotations

import unittest

from research.backtest.archive.rrg_mono_s2_exit_probe import (
    M2_QUAD_EXIT,
    _delta_pp,
    _eval_hypotheses,
    _package_variant,
    render_rrg_mono_s2_exit_probe_md,
)


class TestRrgMonoS2ExitProbe(unittest.TestCase):
    def test_m2_spec_matches_s2_mirror(self) -> None:
        self.assertEqual(M2_QUAD_EXIT.variant_id, "M2")
        self.assertEqual(M2_QUAD_EXIT.exit_quadrants, ("weakening",))
        self.assertEqual(M2_QUAD_EXIT.consecutive_days, 2)
        self.assertEqual(M2_QUAD_EXIT.mtm_threshold_pct, -5.0)

    def test_eval_hypotheses_pass(self) -> None:
        m0 = {"mean_excess_pct": 4.0}
        m1 = {"mean_excess_pct": 4.2}
        m2 = {
            "mean_excess_pct": 4.5,
            "quad_exits": 8,
            "median_save_vs_orig_pct": 1.2,
        }
        h = _eval_hypotheses(m0, m1, m2)
        self.assertTrue(all(v["pass"] for v in h.values()))

    def test_eval_hypotheses_fail_loss_gate(self) -> None:
        m0 = {"mean_excess_pct": 4.0}
        m1 = {"mean_excess_pct": 4.5}
        m2 = {
            "mean_excess_pct": 4.3,
            "quad_exits": 10,
            "median_save_vs_orig_pct": 0.5,
        }
        h = _eval_hypotheses(m0, m1, m2)
        self.assertFalse(h["H-MONO-S2-4"]["pass"])

    def test_delta_pp(self) -> None:
        self.assertEqual(_delta_pp({"mean_excess_pct": 5.1}, {"mean_excess_pct": 4.0}), 1.1)

    def test_render_md_contains_variants(self) -> None:
        payload = {
            "date_start": "2025-01-02",
            "date_end": "2026-06-30",
            "n_slots": 3,
            "n_trade_dates": 100,
            "ssg_note": "note",
            "m2_spec": M2_QUAD_EXIT.to_dict(),
            "variants": {
                "M0": _package_variant("M0", "base", [], {"mean_excess_pct": 1.0}, overlay_kind="baseline"),
                "M1": _package_variant("M1", "e2", [], {"mean_excess_pct": 1.1}, overlay_kind="e2"),
                "M2": _package_variant(
                    "M2",
                    "s2",
                    [],
                    {"mean_excess_pct": 1.2, "quad_exits": 6, "median_save_vs_orig_pct": 0.3},
                    overlay_kind="s2",
                ),
            },
            "deltas_pp": {"M1_vs_M0": 0.1, "M2_vs_M0": 0.2, "M2_vs_M1": 0.1},
            "hypothesis_statements": {"H-MONO-S2-1": "test"},
            "hypotheses": {"H-MONO-S2-1": {"pass": True, "value": 0.2}},
            "recommend_phase2": True,
            "timeline_anchors": {},
        }
        md = render_rrg_mono_s2_exit_probe_md(payload)
        self.assertIn("M2", md)
        self.assertIn("Phase 1", md)


if __name__ == "__main__":
    unittest.main()
