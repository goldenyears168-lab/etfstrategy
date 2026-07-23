"""Tests for TW↔NQ 30m rolling risk-gate (pulse + ≥60m co_sync rebuy)."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.us_tw_30m_rolling_risk_gate_study import (
    RiskGateSpec,
    _confirm_label,
    champion_l2_spec,
    champion_l2r_spec,
    enrich_poll_30m,
    pick_champion,
    pick_co_sync_rebuy,
    registered_grid,
    simulate_day,
)


class Test30mRiskGate(unittest.TestCase):
    def _poll(self) -> pd.DataFrame:
        n = 20
        tw = [0.0 - 0.15 * i for i in range(10)] + [-1.5, -1.4, -1.2, -1.0, -0.9, -0.85, -0.8, -0.78, -0.75, -0.7]
        nq = [0.0 - 0.03 * i for i in range(10)] + [-0.3, -0.28, -0.25, -0.2, -0.18, -0.15, -0.12, -0.1, -0.08, -0.05]
        s30 = [a - b - 0.5 for a, b in zip(tw, nq)]
        s30[5] = -1.2
        s30[6] = -1.3
        polls = []
        h, m = 9, 30
        for _ in range(n):
            polls.append(f"{h:02d}:{m:02d}")
            m += 5
            if m >= 60:
                h += 1
                m -= 60
        tw_w5 = [-0.2] * 12 + [0.35, 0.40, 0.45, 0.50, 0.40, 0.35, 0.30, 0.25]
        nq_w5 = [-0.05] * 12 + [0.10, 0.12, 0.15, 0.18, 0.15, 0.12, 0.10, 0.08]
        df = pd.DataFrame(
            {
                "poll": polls,
                "tw_from_open": tw,
                "nq_from_open": nq,
                "cum_spread": [a - b for a, b in zip(tw, nq)],
                "tw_win": s30,  # 30m placeholder
                "nq_win": [x * 0.2 for x in s30],
                "spread_win": s30,
                "tw_win_5": tw_w5,
                "nq_win_5": nq_w5,
                "corr": [0.5] * n,
                "same_sign": [0.5] * n,
            }
        )
        df.attrs.update(
            {
                "date": "2026-07-14",
                "close_from_open": -0.7,
                "trough_from_open": -1.5,
                "peak_to_trough": 3.5,
            }
        )
        return enrich_poll_30m(df)

    def test_grid_cooldown_ge_60m(self) -> None:
        g = registered_grid()
        cos = [s for s in g if s.repair_mode == "co_sync"]
        self.assertTrue(cos)
        self.assertTrue(all(s.repair_min_polls_after >= 12 for s in cos))
        self.assertTrue(all(s.repair_window_min in (5, 10) for s in cos))
        self.assertTrue(all(s.outperform_margin <= 0.15 for s in cos))

    def test_gap_pulse_present(self) -> None:
        poll = self._poll()
        self.assertIn("gap_pulse", poll.columns)
        self.assertIn("sync_pulse", poll.columns)
        self.assertIn("tw_bar_roc", poll.columns)

    def test_co_sync_short_window_rebuy(self) -> None:
        poll = self._poll()
        spec = RiskGateSpec(
            "co",
            "test",
            win30_thr=-1.0,
            confirm_polls=1,
            reenter=True,
            repair_mode="co_sync",
            repair_min_polls_after=12,
            outperform_margin=0.10,
            repair_window_min=5,
            require_nq_pos=True,
            block_reenter_new_low=True,
        )
        out = simulate_day(poll, spec)
        self.assertTrue(out["triggered"])
        self.assertTrue(out["reentered"])

    def test_pick_prefers_714_hit(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "strategy_id": "miss",
                    "repair_mode": "co_sync",
                    "reenter": True,
                    "win30_thr": -0.6,
                    "confirm_polls": 2,
                    "repair_min_polls_after": 12,
                    "reenter_before_trough_rate_on_events": 0.0,
                    "net_edge_ntd": 999,
                    "outperform_margin": 0.05,
                },
                {
                    "strategy_id": "hit",
                    "repair_mode": "co_sync",
                    "reenter": True,
                    "win30_thr": -0.6,
                    "confirm_polls": 2,
                    "repair_min_polls_after": 12,
                    "reenter_before_trough_rate_on_events": 0.1,
                    "net_edge_ntd": 100,
                    "outperform_margin": 0.1,
                },
            ]
        )
        sims = {
            "miss": {"2026-07-14": {"reenter_poll": "13:00"}},
            "hit": {"2026-07-14": {"reenter_poll": "11:45"}},
        }
        best = pick_co_sync_rebuy(df, champ_thr=-0.6, champ_confirm=2, day_sims=sims)
        self.assertEqual(best["strategy_id"], "hit")

    def test_pick_champion_flatten(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "strategy_id": "a",
                    "event_recall": 0.9,
                    "sell_near_trough_rate_on_events": 0.0,
                    "reenter_before_trough_rate_on_events": 0.0,
                    "reenter": True,
                    "net_edge_ntd": 500,
                },
                {
                    "strategy_id": "b",
                    "event_recall": 0.9,
                    "sell_near_trough_rate_on_events": 0.0,
                    "reenter_before_trough_rate_on_events": 0.0,
                    "reenter": False,
                    "net_edge_ntd": 100,
                },
            ]
        )
        best, _ = pick_champion(df)
        self.assertEqual(best["strategy_id"], "b")

    def test_confirm_label_matrix(self) -> None:
        self.assertEqual(_confirm_label({"is_event3": True, "triggered": True}), "OK_EVENT")
        self.assertEqual(_confirm_label({"is_event3": False, "triggered": True}), "FALSE_ALARM")
        self.assertEqual(_confirm_label({"is_event3": True, "triggered": False}), "MISS_EVENT")
        self.assertEqual(_confirm_label({"is_event3": False, "triggered": False}), "QUIET_OK")

    def test_champion_specs(self) -> None:
        l2 = champion_l2_spec()
        l2r = champion_l2r_spec()
        self.assertEqual(l2.strategy_id, "rg30_det-0.6_c2")
        self.assertFalse(l2.reenter)
        self.assertEqual(l2.win30_thr, -0.6)
        self.assertEqual(l2.confirm_polls, 2)
        self.assertEqual(l2r.strategy_id, "rg30_det-0.6_c2_co_w16_m0.1_r5")
        self.assertTrue(l2r.reenter)
        self.assertEqual(l2r.repair_mode, "co_sync")
        self.assertEqual(l2r.repair_min_polls_after, 16)
        self.assertEqual(l2r.outperform_margin, 0.1)
        self.assertEqual(l2r.repair_window_min, 5)


if __name__ == "__main__":
    unittest.main()
