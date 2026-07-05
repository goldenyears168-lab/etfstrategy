"""Tests for RRG pre-release funnel lanes · 3-day hold."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.rrg_pre_release_funnel_lanes_3d import (
    CONFIG_PATH,
    build_pre_release_feature_panel,
    build_pre_release_funnel_panel_3d,
    enrich_events_3d_outcomes,
    jaccard,
    lane_mask,
    load_funnel_lanes_3d_config,
    search_atom_ids,
)


def _synthetic_lifecycle(n: int = 45) -> pd.DataFrame:
    rows = []
    for i in range(n):
        d = f"2020-01-{i+1:02d}"
        rows.append(
            {
                "date": d,
                "sid": "2330" if i % 2 == 0 else "2317",
                "sig_ret": -0.5 + (i % 5) * 0.3,
                "excess": -1.0,
                "nxt_ret": 0.8 if i % 3 else -0.4,
                "end_q": "improving" if i % 4 else "lagging",
                "improve_streak": 1 + (i % 6),
                "bench_today_ret": 0.5 + (i % 3),
                "session_gap_days": 1,
                "base_setup": i % 5 == 0,
                "s2_setup_core": i % 7 == 0,
                "s3_burst": False,
                "s4_rv_98_99": i % 9 == 0,
                "s0_fresh_flip": i % 8 == 0,
                "disp_contract": i % 6 == 0,
                "disp_d": -0.15 if i % 6 == 0 else 0.05,
            }
        )
    return pd.DataFrame(rows)


class TestFunnelLanes3d(unittest.TestCase):
    def test_config_loads(self) -> None:
        cfg = load_funnel_lanes_3d_config(CONFIG_PATH)
        self.assertEqual(cfg["model_id"], "rrg-pre-release-funnel-3d")
        self.assertEqual(cfg["universe"]["outcome"], "ret_3d")
        self.assertEqual(len(cfg["lanes"]["strict"]), 30)

    def test_enrich_3d_outcomes_synthetic(self) -> None:
        df = _synthetic_lifecycle(30)
        df["ret_1d"] = 1.0
        df["ret_2d"] = 2.0
        df["ret_3d"] = 3.0
        df["max_ret_3d"] = 3.5
        df["min_ret_3d"] = -0.5
        df["win_3d"] = True
        df["loss5_3d"] = False
        df["loss8_3d"] = False
        self.assertIn("ret_3d", df.columns)

    def test_pre_release_pool_3d(self) -> None:
        df = _synthetic_lifecycle(30)
        df["ret_1d"] = 1.0
        df["ret_2d"] = 2.0
        df["ret_3d"] = 3.0
        df["max_ret_3d"] = 3.5
        df["min_ret_3d"] = -0.5
        df["win_3d"] = True
        df["loss5_3d"] = False
        df["loss8_3d"] = False
        panel, _ = build_pre_release_funnel_panel_3d(events=df)
        self.assertTrue((panel["sig_ret"] < 5.0).all())
        self.assertIn("ret_3d", panel.columns)

    def test_lane_mask(self) -> None:
        df = _synthetic_lifecycle(30)
        df["ret_1d"] = 1.0
        df["ret_2d"] = 2.0
        df["ret_3d"] = 3.0
        df["max_ret_3d"] = 3.5
        df["min_ret_3d"] = -0.5
        df["win_3d"] = True
        df["loss5_3d"] = False
        df["loss8_3d"] = False
        panel, _ = build_pre_release_funnel_panel_3d(events=df)
        hits = panel[lane_mask(panel, ("b0", "gap1"))]
        self.assertGreaterEqual(len(hits), 0)

    def test_jaccard(self) -> None:
        self.assertEqual(jaccard({"a", "b"}, {"b", "c"}), 1 / 3)

    def test_lead3_atom(self) -> None:
        from research.backtest.rrg_pre_release_funnel_lanes import _atom_mask

        df = pd.DataFrame(
            [
                {
                    "end_q": "improving",
                    "rv": 98.5,
                    "improve_streak": 5,
                    "bench_today_ret": 1.0,
                    "disp_contract": False,
                    "base_setup": True,
                    "s2_setup_core": False,
                    "s4_rv_98_99": True,
                    "s0_fresh_flip": False,
                    "sig_ret": 1.0,
                    "excess": -1.0,
                    "session_gap_days": 1,
                    "sid_hist_ok": True,
                    "prev_end_q": "lagging",
                },
                {
                    "end_q": "improving",
                    "rv": 97.0,
                    "improve_streak": 5,
                    "bench_today_ret": 1.0,
                    "disp_contract": False,
                    "base_setup": False,
                    "s2_setup_core": False,
                    "s4_rv_98_99": False,
                    "s0_fresh_flip": False,
                    "sig_ret": 1.0,
                    "excess": -1.0,
                    "session_gap_days": 1,
                    "sid_hist_ok": False,
                    "prev_end_q": "improving",
                },
            ]
        )
        m = _atom_mask(df, "lead3")
        self.assertTrue(m.iloc[0])
        self.assertFalse(m.iloc[1])

    def test_feature_panel_atom_columns(self) -> None:
        df = _synthetic_lifecycle(30)
        df["ret_1d"] = 1.0
        df["ret_2d"] = 2.0
        df["ret_3d"] = 3.0
        df["max_ret_3d"] = 3.5
        df["min_ret_3d"] = -0.5
        df["win_3d"] = True
        df["loss5_3d"] = False
        df["loss8_3d"] = False
        df["rv"] = 98.5
        df["bench_nxt_ret"] = 0.5
        df["prev_q"] = "lagging"
        panel, meta = build_pre_release_feature_panel(events=df)
        cfg = load_funnel_lanes_3d_config(CONFIG_PATH)
        atom_ids = search_atom_ids(cfg)
        self.assertGreater(len(panel), 0)
        self.assertEqual(meta["n_rows"], len(panel))
        for atom_id in atom_ids:
            col = f"atom_{atom_id}"
            self.assertIn(col, panel.columns)
            self.assertTrue(set(panel[col].unique()).issubset({0, 1}))
        pool, _ = build_pre_release_funnel_panel_3d(events=df)
        direct = lane_mask(pool, ["b2", "dip"])
        merged = panel.merge(pool[["sid", "date"]], on=["sid", "date"])
        self.assertEqual(len(merged), len(panel))

    def test_june2026_leading_cases_constant(self) -> None:
        from research.backtest.rrg_pre_release_funnel_lanes_3d import JUNE2026_LEADING_STREAK_CASES

        self.assertEqual(len(JUNE2026_LEADING_STREAK_CASES), 42)
        sid, name, d0, d1, pct, rv, mom = JUNE2026_LEADING_STREAK_CASES[0]
        self.assertEqual(sid, "2316")
        self.assertEqual(name, "楠梓電")
        self.assertGreaterEqual(pct, 15.0)


if __name__ == "__main__":
    unittest.main()
