"""Tests for abc_v3_f1_entry_structure_sweep."""

from __future__ import annotations

import unittest

from research.backtest.abc_v3_f1_entry_structure_sweep import (
    GapSweetBand,
    Rv3TroughState,
    _rebound_ok,
    _split_dates_is_oos,
    verdict_abc_v3_f1_entry_structure_sweep,
)


def _point(mv20: float, mv5: float, mv3: float) -> dict:
    return {
        "w20": {"rs_momentum": mv20},
        "w5": {"rs_momentum": mv5},
        "w3": {"rs_momentum": mv3},
    }


class TestGapSweetBand(unittest.TestCase):
    def setUp(self) -> None:
        self.band = GapSweetBand(
            gap_w20_w5_min=2.0, gap_w20_w5_max=7.0, gap_w5_w3_min=-0.5, gap_w5_w3_max=1.5
        )

    def test_in_band(self) -> None:
        p = _point(mv20=105.0, mv5=101.0, mv3=100.5)  # gap20-5=4.0, gap5-3=0.5
        self.assertTrue(self.band.ok(p))

    def test_below_band(self) -> None:
        p = _point(mv20=102.0, mv5=101.0, mv3=100.5)  # gap20-5=1.0 < min 2.0
        self.assertFalse(self.band.ok(p))

    def test_above_band(self) -> None:
        p = _point(mv20=110.0, mv5=101.0, mv3=100.5)  # gap20-5=9.0 > max 7.0
        self.assertFalse(self.band.ok(p))

    def test_missing_leg_is_not_ok(self) -> None:
        p = {"w20": {"rs_momentum": 105.0}, "w5": {"rs_momentum": 101.0}}
        self.assertFalse(self.band.ok(p))


class TestRv3TroughState(unittest.TestCase):
    def test_trough_tracks_running_min_and_rebound(self) -> None:
        state = Rv3TroughState()
        series = [100.2, 99.6, 99.1, 99.4, 99.9]
        expected_rebound = [0.0, 0.0, 0.0, 0.3, 0.8]
        expected_poll_rise = [False, False, False, True, True]
        for rv3, exp_rebound, exp_rise in zip(series, expected_rebound, expected_poll_rise):
            rebound, poll_rise = state.update(rv3)
            self.assertAlmostEqual(rebound, exp_rebound, places=3)
            self.assertEqual(poll_rise, exp_rise)
        self.assertEqual(state.trough, 99.1)

    def test_reset_clears_state_at_day_boundary(self) -> None:
        state = Rv3TroughState()
        state.update(99.1)
        state.update(99.9)
        state.reset()
        rebound, poll_rise = state.update(101.0)
        self.assertEqual(rebound, 0.0)
        self.assertFalse(poll_rise)


class TestReboundOk(unittest.TestCase):
    def test_rebound_only_ignores_poll_rise(self) -> None:
        self.assertTrue(_rebound_ok(0.5, False, rebound_min=0.3, mode="rebound_only"))
        self.assertFalse(_rebound_ok(0.2, False, rebound_min=0.3, mode="rebound_only"))

    def test_rebound_and_poll_rise_requires_both(self) -> None:
        self.assertTrue(_rebound_ok(0.5, True, rebound_min=0.3, mode="rebound_and_poll_rise"))
        self.assertFalse(_rebound_ok(0.5, False, rebound_min=0.3, mode="rebound_and_poll_rise"))
        self.assertFalse(_rebound_ok(0.2, True, rebound_min=0.3, mode="rebound_and_poll_rise"))


class TestSplitDatesIsOos(unittest.TestCase):
    def test_ratio_split(self) -> None:
        dates = [f"2026-01-{d:02d}" for d in range(1, 11)]
        is_dates, oos_dates = _split_dates_is_oos(dates, ratio=0.7)
        self.assertEqual(len(is_dates), 7)
        self.assertEqual(len(oos_dates), 3)
        self.assertEqual(is_dates + oos_dates, dates)

    def test_empty_dates(self) -> None:
        self.assertEqual(_split_dates_is_oos([], ratio=0.7), ([], []))

    def test_single_date_keeps_it_in_is(self) -> None:
        is_dates, oos_dates = _split_dates_is_oos(["2026-01-01"], ratio=0.7)
        self.assertEqual(is_dates, ["2026-01-01"])
        self.assertEqual(oos_dates, [])

    def test_two_dates_keeps_at_least_one_oos(self) -> None:
        is_dates, oos_dates = _split_dates_is_oos(["2026-01-01", "2026-01-02"], ratio=0.99)
        self.assertEqual(is_dates, ["2026-01-01"])
        self.assertEqual(oos_dates, ["2026-01-02"])


class TestVerdict(unittest.TestCase):
    def test_picks_winning_cell(self) -> None:
        bt = {
            "grid": [
                {
                    "id": "cell_a",
                    "label": "cell a",
                    "gap_w20_w5_min": 2.0,
                    "gap_w20_w5_max": 7.0,
                    "gap_w5_w3_min": -0.5,
                    "gap_w5_w3_max": 1.5,
                    "rebound_min": 0.3,
                    "rebound_mode": "rebound_only",
                    "min_poll_idx": 1,
                },
                {
                    "id": "cell_b",
                    "label": "cell b",
                    "gap_w20_w5_min": 2.0,
                    "gap_w20_w5_max": 7.0,
                    "gap_w5_w3_min": -0.5,
                    "gap_w5_w3_max": 1.5,
                    "rebound_min": 0.5,
                    "rebound_mode": "rebound_and_poll_rise",
                    "min_poll_idx": 1,
                },
            ],
            "summaries": {
                "baseline_f1": {"n": 80, "mean_ret_3d_net_pct": 2.0},
                "cell_a": {"n": 30, "mean_ret_3d_net_pct": 2.1},
                "cell_b": {"n": 20, "mean_ret_3d_net_pct": 2.9},
            },
        }
        v = verdict_abc_v3_f1_entry_structure_sweep(bt, min_n=15)
        self.assertTrue(v["passed"])
        self.assertEqual(v["recommend"], "cell_b")

    def test_no_cell_reaches_min_n(self) -> None:
        bt = {
            "grid": [
                {
                    "id": "cell_a",
                    "label": "cell a",
                    "gap_w20_w5_min": 2.0,
                    "gap_w20_w5_max": 7.0,
                    "gap_w5_w3_min": -0.5,
                    "gap_w5_w3_max": 1.5,
                    "rebound_min": 0.3,
                    "rebound_mode": "rebound_only",
                    "min_poll_idx": 1,
                },
            ],
            "summaries": {
                "baseline_f1": {"n": 80, "mean_ret_3d_net_pct": 2.0},
                "cell_a": {"n": 5, "mean_ret_3d_net_pct": 4.0},
            },
        }
        v = verdict_abc_v3_f1_entry_structure_sweep(bt, min_n=15)
        self.assertFalse(v["passed"])
        self.assertEqual(v["recommend"], "baseline_f1")


if __name__ == "__main__":
    unittest.main()
