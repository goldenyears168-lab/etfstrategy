"""Tests for w3_rv_hl_winner_profile."""

from __future__ import annotations

import unittest

from research.backtest.w3_rv_hl_winner_profile import (
    ABC_V3_ENTRY_GATE_SPECS,
    _poll_idx_for_frame,
    extract_entry_features,
    profile_winners_losers,
    verdict_abc_v3_entry_gate_sweep,
)


class TestW3WinnerProfile(unittest.TestCase):
    def test_extract_features(self) -> None:
        p = {
            "minute": "11:00",
            "spread_class": "pullback",
            "spread_dist": 2.5,
            "spread_rs": -1.0,
            "spread_mom": -0.5,
            "w20": {"rs_ratio": 102.0, "rs_momentum": 101.0, "quadrant": "leading"},
            "w5": {"rs_ratio": 101.0, "rs_momentum": 100.5, "quadrant": "leading"},
            "w3": {"rs_ratio": 99.2, "rs_momentum": 100.8, "quadrant": "improving"},
            "w2": {"rs_ratio": 99.6, "rs_momentum": 101.0, "quadrant": "improving"},
        }
        f = extract_entry_features(p)
        self.assertEqual(f["w3_dip_depth"], 0.8)
        self.assertTrue(f["w3_mv_above_100"])
        self.assertFalse(f["w3_rv_shallow"])

    def test_profile_split(self) -> None:
        trades = [
            {"outcome": "win", "w3_mv": 101.0, "w3_rv": 99.6, "w3_mv_above_100": True, "w3_rv_shallow": True, "net_pct": 2.0},
            {"outcome": "loss", "w3_mv": 99.0, "w3_rv": 98.5, "w3_mv_above_100": False, "w3_rv_shallow": False, "net_pct": -1.0},
        ]
        prof = profile_winners_losers(trades)
        self.assertEqual(prof["n_win"], 1)
        self.assertEqual(prof["n_loss"], 1)

    def test_poll_idx_for_frame(self) -> None:
        frames = [{"minute": "11:00"}, {"minute": "09:00"}]
        self.assertEqual(_poll_idx_for_frame(frames, 0), 4)
        self.assertEqual(_poll_idx_for_frame(frames, 1), 0)

    def test_entry_gate_verdict_picks_winner(self) -> None:
        bt = {
            "baseline_first_trigger": {"n": 80, "mean_ret_3d_net_pct": 2.0},
            "gates": [
                {"id": "first_trigger", "label": "first", "n": 80, "mean_ret_3d_net_pct": 2.0, "fill_rate_pct": 100.0},
                {"id": "gte_1100", "label": "≥11:00", "n": 30, "mean_ret_3d_net_pct": 2.8, "fill_rate_pct": 37.5},
            ],
            "post_hoc_slices": {"entry_mid_session": {"n": 20, "mean_ret_3d_net_pct": 3.5}},
        }
        v = verdict_abc_v3_entry_gate_sweep(bt, min_n=15)
        self.assertTrue(v["passed"])
        self.assertEqual(v["recommend"], "gte_1100")
        self.assertEqual(len(ABC_V3_ENTRY_GATE_SPECS), 5)


if __name__ == "__main__":
    unittest.main()
