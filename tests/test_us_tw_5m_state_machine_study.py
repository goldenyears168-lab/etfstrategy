"""Tests for TW↔NQ 5m state-machine labeling and PnL path."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.us_tw_5m_state_machine_study import (
    StateStrategySpec,
    label_states,
    pick_champion,
    registered_grid,
    simulate_day_state,
)


class TestStateMachine(unittest.TestCase):
    def _poll(self) -> pd.DataFrame:
        # Bar path: detach at cum=-1.0 (09:25), trough cum, then repair with tw>nq & tw>0 & corr high & Δcum
        n = 10
        df = pd.DataFrame(
            {
                "poll": [f"09:{10 + i * 5:02d}" for i in range(n)],
                "tw_from_open": [0.0, -0.3, -0.7, -1.2, -1.5, -1.4, -1.0, -0.6, -0.4, -0.5],
                "nq_from_open": [0.0, -0.1, -0.1, -0.2, -0.2, -0.3, -0.2, -0.1, 0.0, 0.0],
                "cum_spread": [0.0, -0.2, -0.6, -1.0, -1.3, -1.1, -0.8, -0.5, -0.4, -0.5],
                "tw_win": [-0.1, -0.3, -0.4, -0.5, -0.3, -0.1, 0.35, 0.40, 0.25, 0.05],
                "nq_win": [-0.05, -0.1, -0.05, -0.1, 0.0, -0.05, 0.05, 0.10, 0.10, 0.0],
                "spread_win": [-0.05, -0.2, -0.35, -0.4, -0.3, -0.05, 0.30, 0.30, 0.15, 0.05],
                "corr": [0.8, 0.5, 0.2, -0.1, 0.1, 0.25, 0.45, 0.55, 0.6, 0.5],
                "same_sign": [0.9, 0.7, 0.5, 0.3, 0.4, 0.5, 0.7, 0.85, 0.8, 0.7],
            }
        )
        df.attrs.update(
            {
                "date": "2026-07-14",
                "close_from_open": -0.5,
                "trough_from_open": -1.5,
                "peak_to_trough": 3.5,
            }
        )
        return df

    def test_registered_grid_finite(self) -> None:
        g = registered_grid()
        self.assertGreater(len(g), 50)
        self.assertLess(len(g), 1200)  # constrained, not unbounded
        ids = [s.strategy_id for s in g]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("baseline_combo_cum1_win06_reenter_w10", ids)
        self.assertTrue(any(i.startswith("h2_") for i in ids))
        self.assertTrue(any(i.startswith("xw_") for i in ids))

    def test_label_detach_then_repair(self) -> None:
        poll = self._poll()
        spec = StateStrategySpec(
            "t",
            "test",
            cum_thr=-1.0,
            window_min=5,
            reenter_on_repair=True,
            repair_corr_min=0.4,
            repair_delta_cum=0.4,
            require_tw_pos=True,
            require_tw_gt_nq=True,
        )
        labeled = label_states(poll, spec)
        self.assertEqual(labeled.loc[labeled["poll"] == "09:25", "state"].iloc[0], "DETACH")
        # first REPAIRING after trough recovery
        repair_rows = labeled[labeled["state"] == "REPAIRING"]
        self.assertFalse(repair_rows.empty)
        self.assertGreaterEqual(str(repair_rows.iloc[0]["poll"]), "09:40")

    def test_simulate_reenter_pnl(self) -> None:
        poll = self._poll()
        spec = StateStrategySpec(
            "t",
            "test",
            cum_thr=-1.0,
            window_min=5,
            reenter_on_repair=True,
            repair_corr_min=0.4,
            repair_delta_cum=0.4,
        )
        out = simulate_day_state(poll, spec)
        self.assertTrue(out["triggered"])
        self.assertEqual(out["detach_poll"], "09:25")
        self.assertTrue(out["reentered"])
        self.assertIsNotNone(out["reenter_poll"])
        # exit at -1.2, reenter later, close -0.5 → strat pnl = -1.2 + (-0.5 - reenter)
        self.assertIsInstance(out["pnl_strat_pct"], float)

    def test_pick_champion_prefers_fpr_cap(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "strategy_id": "noisy",
                    "net_edge_ntd": 9999,
                    "false_trigger_rate_on_nonevent": 0.55,
                },
                {
                    "strategy_id": "tight",
                    "net_edge_ntd": 100,
                    "false_trigger_rate_on_nonevent": 0.20,
                },
            ]
        )
        best, rule = pick_champion(df)
        self.assertEqual(best["strategy_id"], "tight")
        self.assertTrue(rule.startswith("fpr_cap"))


    def test_dual_sync_and_requires_both(self) -> None:
        poll = self._poll()
        spec = StateStrategySpec(
            "and",
            "test",
            cum_thr=-1.0,
            window_min=5,
            reenter_on_repair=True,
            repair_corr_min=0.4,
            repair_same_sign_min=0.8,
            repair_sync_and=True,
            repair_delta_cum=0.4,
        )
        labeled = label_states(poll, spec)
        # at 09:40 corr=0.45 ok but same_sign=0.7 < 0.8 → not repair yet
        row40 = labeled[labeled["poll"] == "09:40"].iloc[0]
        self.assertEqual(row40["state"], "DETACH")
        # 09:45 ss=0.85 & corr=0.55 → REPAIRING
        row45 = labeled[labeled["poll"] == "09:45"].iloc[0]
        self.assertEqual(row45["state"], "REPAIRING")

    def test_attach_repair_window(self) -> None:
        from research.backtest.us_tw_5m_state_machine_study import attach_repair_window

        base = self._poll()
        alt = base.copy()
        alt["tw_win"] = alt["tw_win"] + 0.5
        merged = attach_repair_window(base, alt)
        self.assertIn("r_tw_win", merged.columns)
        self.assertAlmostEqual(float(merged.iloc[-1]["r_tw_win"]), float(alt.iloc[-1]["tw_win"]))


if __name__ == "__main__":
    unittest.main()
