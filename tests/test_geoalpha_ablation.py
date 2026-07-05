"""Tests for GeoAlpha Phase 2 ablation entry logic."""

from __future__ import annotations

import random
import unittest

from research.backtest.dual_wma_tactical_slot_backtest import HookEntryParams
from research.backtest.geoalpha_ablation_backtest import (
    _collect_geo_candidates,
    geo_hook_champion_entry,
)


def _frame(date: str, minute: str = "10:00") -> dict:
    return {"id": f"{date}T{minute}", "date": date, "minute": minute}


def _hook_imp_point(
    *,
    date: str = "2026-04-01",
    minute: str = "10:00",
    hook_retrace_pct: float = 0.18,
    spread_dist: float = 2.0,
    w20_rs: float = 98.0,
) -> dict:
    return {
        "frame_id": f"{date}T{minute}",
        "date": date,
        "minute": minute,
        "fresh": True,
        "hook_candidate": True,
        "hook_complete": False,
        "hook_retrace_pct": hook_retrace_pct,
        "spread_dist": spread_dist,
        "spread_rs": 1.0,
        "spread_mom": 0.5,
        "spread_class": "pullback",
        "w20": {"quadrant": "improving", "rs_ratio": w20_rs},
        "w5": {"quadrant": "improving", "rs_ratio": 101.0},
    }


def _ext_open_point(*, date: str = "2026-04-01", minute: str = "09:30") -> dict:
    return {
        "frame_id": f"{date}T{minute}",
        "date": date,
        "minute": minute,
        "fresh": True,
        "hook_candidate": False,
        "hook_complete": False,
        "spread_class": "extension",
        "spread_rs": 2.0,
        "spread_mom": 1.5,
        "spread_dist": 3.0,
        "dist_rising": True,
        "w20": {"quadrant": "improving", "rs_ratio": 99.0},
        "w5": {"quadrant": "leading", "rs_ratio": 102.0},
    }


class TestGeoAblationEntry(unittest.TestCase):
    def setUp(self) -> None:
        self.frames = [
            _frame("2026-04-01", "09:30"),
            _frame("2026-04-01", "10:00"),
            _frame("2026-04-01", "10:30"),
        ]
        self.trajectories = [
            {
                "stock_id": "2330",
                "points": [
                    _hook_imp_point(minute="09:30", hook_retrace_pct=0.16, spread_dist=1.6),
                    _hook_imp_point(minute="10:00", hook_retrace_pct=0.25, spread_dist=2.5),
                    _ext_open_point(minute="10:30"),
                ],
            }
        ]
        self.struct = {("2330", "2026-04-01"): 2.0}
        self.row = {("2330", "2026-04-01"): {"w5_drs_rsr_1d": 1.0, "bench_today_ret": 0.5}}
        self.thr = {"2026-04-01": 1.0}
        self.hook_ep = geo_hook_champion_entry()
        self.rng = random.Random(42)

    def test_m2_first_poll(self) -> None:
        events = _collect_geo_candidates(
            self.trajectories,
            self.frames,
            "M2",
            hook_ep=self.hook_ep,
            struct_by_key=self.struct,
            row_by_key=self.row,
            threshold_by_date=self.thr,
            rng=self.rng,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], ("2330", 0))

    def test_m1_highest_s_alpha(self) -> None:
        events = _collect_geo_candidates(
            self.trajectories,
            self.frames,
            "M1",
            hook_ep=self.hook_ep,
            struct_by_key=self.struct,
            row_by_key=self.row,
            threshold_by_date=self.thr,
            rng=self.rng,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1], 1)

    def test_m4_requires_struct_gate(self) -> None:
        low_thr = {"2026-04-01": 5.0}
        events = _collect_geo_candidates(
            self.trajectories,
            self.frames,
            "M4",
            hook_ep=self.hook_ep,
            struct_by_key=self.struct,
            row_by_key=self.row,
            threshold_by_date=low_thr,
            rng=self.rng,
        )
        self.assertEqual(events, [])

    def test_m5_ext_open_not_hook(self) -> None:
        traj = [
            {
                "stock_id": "2330",
                "points": [
                    _hook_imp_point(minute="09:30"),
                    _ext_open_point(minute="10:00"),
                ],
            }
        ]
        events = _collect_geo_candidates(
            traj,
            self.frames[:2],
            "M5",
            hook_ep=self.hook_ep,
            struct_by_key=self.struct,
            row_by_key=self.row,
            threshold_by_date=self.thr,
            rng=self.rng,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1], 1)

    def test_champion_entry_params(self) -> None:
        ep = geo_hook_champion_entry()
        self.assertEqual(ep.pool_tier, "improving")
        self.assertEqual(ep.hook_retrace_pct_min, 0.15)
        self.assertEqual(ep.spread_dist_min, 1.5)


if __name__ == "__main__":
    unittest.main()
