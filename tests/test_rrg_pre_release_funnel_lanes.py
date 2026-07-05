"""Tests for RRG pre-release funnel lanes config and mask evaluation."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.rrg_pre_release_funnel_lanes import (
    CONFIG_PATH,
    _atom_mask,
    build_pre_release_funnel_panel,
    jaccard,
    lane_mask,
    load_funnel_lanes_config,
)


def _synthetic_lifecycle(n: int = 40) -> pd.DataFrame:
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


class TestFunnelLanes(unittest.TestCase):
    def test_config_loads_strict_lanes(self) -> None:
        cfg = load_funnel_lanes_config(CONFIG_PATH)
        self.assertEqual(cfg["model_id"], "rrg-pre-release-funnel")
        strict = cfg["lanes"]["strict"]
        self.assertEqual(len(strict), 30)
        self.assertEqual(strict[0]["id"], "R01")
        self.assertIn("b2", strict[0]["rule_atoms"])

    def test_atom_masks_on_synthetic(self) -> None:
        df = _synthetic_lifecycle()
        df["prev_end_q"] = ""
        df["sid_hist_ok"] = False
        df["sid_hist_n"] = 0
        self.assertTrue(_atom_mask(df, "b0").any())
        self.assertTrue(_atom_mask(df, "gap1").all())
        combo = ("b0", "gap1", "flat")
        hits = df[lane_mask(df, combo)]
        self.assertGreaterEqual(len(hits), 0)

    def test_pre_release_pool_filter(self) -> None:
        df = _synthetic_lifecycle()
        panel, _ = build_pre_release_funnel_panel(events=df)
        self.assertTrue((panel["sig_ret"] < 5.0).all())
        lag = panel[panel["end_q"] == "lagging"]
        if len(lag):
            ok = lag["disp_contract"] | (lag["sig_ret"] > 0.5)
            self.assertTrue(ok.all())

    def test_jaccard(self) -> None:
        self.assertEqual(jaccard({"a", "b"}, {"b", "c"}), 1 / 3)


if __name__ == "__main__":
    unittest.main()
