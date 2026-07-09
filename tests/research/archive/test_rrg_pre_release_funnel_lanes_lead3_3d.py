"""Tests for RRG lead3 funnel lanes · 3-day hold."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.archive.rrg_pre_release_funnel_lanes_lead3_3d import (
    CONFIG_PATH,
    H2A_B1_COMBO,
    H4A_ST7P_COIL_COMBOS,
    H4B_HIGH_RET_COMBOS,
    analyze_case_lane_feedback,
    infer_archetype_tag,
    load_lead3_funnel_config,
    mine_case_signal_facts,
    sweep_hypothesis_lanes,
    _backtrack_case_signal_days,
    _passes_coverage_thresholds,
    _sweep_h4_hypotheses,
)


def _synthetic_lead3_panel(n: int = 40) -> pd.DataFrame:
    rows = []
    for i in range(n):
        d = f"2024-06-{i+1:02d}" if i < 30 else f"2024-07-{i-29:02d}"
        rows.append(
            {
                "date": d,
                "sid": "2330" if i % 2 == 0 else "2316",
                "sig_ret": 2.0 + (i % 4) * 0.5,
                "excess": -1.0,
                "end_q": "improving",
                "improve_streak": 4 + (i % 3),
                "rv": 98.5,
                "bench_today_ret": 2.5 if i % 3 == 0 else 0.5,
                "session_gap_days": 1,
                "base_setup": i % 5 == 0,
                "s2_setup_core": i % 7 == 0,
                "disp_contract": i % 4 == 0,
                "disp_d": -0.2 if i % 4 == 0 else 0.1,
                "prev_end_q": "lagging" if i % 6 == 0 else "improving",
                "sid_hist_ok": i % 2 == 0,
                "ret_1d": 1.0,
                "ret_2d": 2.0,
                "ret_3d": 4.0 if i % 3 else -1.0,
                "min_ret_3d": -2.0,
                "max_ret_3d": 5.0,
                "win_3d": i % 3 != 0,
                "loss5_3d": False,
                "loss8_3d": False,
                "flip_leading_3d": i % 4 == 0,
            }
        )
    return pd.DataFrame(rows)


class TestLead3FunnelLanes(unittest.TestCase):
    def test_config_has_archetype_tags(self) -> None:
        cfg = load_lead3_funnel_config(CONFIG_PATH)
        for ld in cfg["lanes"]["strict"]:
            self.assertIn("archetype_tag", ld)
            self.assertEqual(ld["archetype_tag"], infer_archetype_tag(ld["rule_atoms"]))

    def test_infer_archetype_tag(self) -> None:
        self.assertEqual(
            infer_archetype_tag(["lead3", "b2", "disp", "hist", "q35"]),
            "b2_burst",
        )
        self.assertEqual(
            infer_archetype_tag(["lead3", "base", "disp", "hist", "q35"]),
            "base_coil",
        )
        self.assertEqual(
            infer_archetype_tag(["lead3", "b2", "dip", "hist", "s2"]),
            "s2_dip",
        )
        self.assertEqual(
            infer_archetype_tag(["lead3", "plag", "disp", "hist"]),
            "plag_flip",
        )
        self.assertEqual(infer_archetype_tag(H2A_B1_COMBO), "b1_moderate")
        self.assertEqual(
            infer_archetype_tag(["lead3", "st7p", "disp", "flat", "hist"]),
            "st7p_coil",
        )
        self.assertEqual(
            infer_archetype_tag(["lead3", "st7p", "b2", "hist", "q35"]),
            "late_streak",
        )
        self.assertEqual(
            infer_archetype_tag(["lead3", "disp", "hist", "q35"]),
            "high_ret_lift",
        )

    def test_sweep_hypothesis_lanes_runs(self) -> None:
        panel = _synthetic_lead3_panel()
        cfg = load_lead3_funnel_config(CONFIG_PATH)
        strict = cfg["lanes"]["strict"]
        coverage = cfg["lanes"]["coverage"]
        out = sweep_hypothesis_lanes(panel, strict, coverage)
        self.assertIn("h1a_plag_sweep", out)
        self.assertIn("h2a_b1_moderate", out)
        self.assertIn("h3a_late_streak", out)
        self.assertIn("h4a_st7p_coil", out)
        self.assertIn("h4b_high_ret_lift", out)

    def test_h4_combo_constants(self) -> None:
        self.assertEqual(H4A_ST7P_COIL_COMBOS[0], ("lead3", "st7p", "disp", "flat"))
        self.assertEqual(H4B_HIGH_RET_COMBOS[0], ("lead3", "disp", "hist", "q35"))
        self.assertEqual(infer_archetype_tag(H4A_ST7P_COIL_COMBOS[0]), "st7p_coil")
        self.assertEqual(infer_archetype_tag(H4B_HIGH_RET_COMBOS[0]), "high_ret_lift")

    def test_sweep_h4_hypotheses_structure(self) -> None:
        panel = _synthetic_lead3_panel()
        cfg = load_lead3_funnel_config(CONFIG_PATH)
        h4 = _sweep_h4_hypotheses(
            panel,
            strict_defs=cfg["lanes"]["strict"],
            coverage_defs=cfg["lanes"]["coverage"],
            ref_day_sets=[],
            used_archetypes={"b2_burst", "base_coil", "s2_dip"},
        )
        self.assertIn("h4a_st7p_coil", h4)
        self.assertIn("candidates", h4["h4a_st7p_coil"])
        self.assertIn("h4b_high_ret_lift", h4)

    def test_passes_coverage_thresholds(self) -> None:
        row = {"mean_3d": 2.6, "win_3d": 0.56, "n": 16, "day_set": set()}
        ok, reasons = _passes_coverage_thresholds(row)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])
        row_fail = {"mean_3d": 2.4, "win_3d": 0.56, "n": 16, "day_set": set()}
        ok2, reasons2 = _passes_coverage_thresholds(row_fail)
        self.assertFalse(ok2)
        self.assertTrue(any("mean_3d" in r for r in reasons2))

    def test_backtrack_case_signal_days(self) -> None:
        panel = _synthetic_lead3_panel()
        strict = load_lead3_funnel_config(CONFIG_PATH)["lanes"]["strict"]
        bt = _backtrack_case_signal_days(
            panel, panel, strict, "2330", "2024-06-15", lookback_sessions=5,
        )
        self.assertIn("candidates", bt)
        self.assertIn("best", bt)
        self.assertLessEqual(len(bt["candidates"]), 6)

    def test_case_feedback_structure(self) -> None:
        panel = _synthetic_lead3_panel()
        strict = load_lead3_funnel_config(CONFIG_PATH)["lanes"]["strict"]
        fb = analyze_case_lane_feedback(panel, strict, conn=None, parent_panel=panel)
        self.assertIn("summary", fb)
        self.assertIn("missed_archetypes", fb["summary"])
        self.assertIn("hit_rate", fb["summary"])
        self.assertIn("backtrack_hit_rate", fb["summary"])

    def test_mine_case_signal_facts(self) -> None:
        panel = _synthetic_lead3_panel()
        fb = {"cases": []}
        facts = mine_case_signal_facts(panel, fb)
        self.assertIn("atom_hit_rates", facts)
        self.assertIn("top_atoms", facts)
        self.assertIn("high_ret_scan", facts)
        self.assertIn("suggested_hypotheses", facts)


if __name__ == "__main__":
    unittest.main()
