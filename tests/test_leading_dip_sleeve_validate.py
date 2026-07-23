"""Unit tests for Leading Dip sleeve pure helpers (no DB)."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.leading_dip_sleeve_validate import (
    FROZEN_SLOT_POLICY,
    TOTAL_OPEN_CAP,
    SlotPolicy,
    apply_frozen_policy,
    apply_slot_policy,
    apply_slot_policy_t1_soft,
    apply_total_open_cap,
    build_path_frame,
    classify_busy_day,
    concurrent_open_peak,
    day_slot_cap,
    effective_sample_stats,
    first_hit_index,
    is_t2_minute,
    mild_down_flag,
    mild_down_stratum,
    passes_structure_filters,
    rank_deepest_excess,
    select_day_slots,
    spearman_rho,
    summarize_trades,
)


class TestDaySlotCap(unittest.TestCase):
    def test_default_cap_two(self) -> None:
        self.assertEqual(day_slot_cap(1, 0.0), 1)
        self.assertEqual(day_slot_cap(2, 0.0), 2)
        self.assertEqual(day_slot_cap(5, 0.0), 2)

    def test_busy_without_crash_stays_two(self) -> None:
        self.assertEqual(day_slot_cap(5, -1.0), 2)
        self.assertEqual(day_slot_cap(9, +3.0), 2)

    def test_busy_and_crash_expands_to_four(self) -> None:
        self.assertEqual(day_slot_cap(3, -1.5), 3)
        self.assertEqual(day_slot_cap(5, -2.0), 4)
        self.assertEqual(day_slot_cap(9, -1.51), 4)

    def test_hard_cap(self) -> None:
        pol = SlotPolicy(name="t", hard_cap=3, expand_cap=4)
        self.assertEqual(day_slot_cap(9, -2.0, policy=pol), 3)


class TestRankingAndSelect(unittest.TestCase):
    def test_rank_deepest_excess(self) -> None:
        rows = [
            {"sid": "A", "ex0": -4.1, "minute": "10:00"},
            {"sid": "B", "ex0": -6.0, "minute": "09:40"},
            {"sid": "C", "ex0": -5.0, "minute": "11:00"},
        ]
        ranked = rank_deepest_excess(rows)
        self.assertEqual([r["sid"] for r in ranked], ["B", "C", "A"])

    def test_select_day_slots_expand(self) -> None:
        rows = [
            {"sid": str(i), "ex0": -4.0 - i, "rB_min": -2.0, "minute": "10:00"}
            for i in range(5)
        ]
        sel = select_day_slots(rows, policy=FROZEN_SLOT_POLICY)
        self.assertEqual(len(sel), 4)
        self.assertEqual(sel[0]["sid"], "4")  # deepest

    def test_select_day_slots_no_expand_on_green_busy(self) -> None:
        rows = [
            {"sid": str(i), "ex0": -4.0 - i, "rB_min": +1.0, "minute": "10:00"}
            for i in range(5)
        ]
        sel = select_day_slots(rows)
        self.assertEqual(len(sel), 2)


class TestApplyPolicy(unittest.TestCase):
    def test_apply_requires_t2(self) -> None:
        df = pd.DataFrame(
            [
                {"date": "2026-01-02", "sid": "1", "ex0": -5.0, "ex3": 1.0, "T2": False, "rB_min": -2.0, "half": "OOS"},
                {"date": "2026-01-02", "sid": "2", "ex0": -6.0, "ex3": 2.0, "T2": True, "rB_min": -2.0, "half": "OOS"},
                {"date": "2026-01-02", "sid": "3", "ex0": -4.5, "ex3": 0.5, "T2": True, "rB_min": -2.0, "half": "OOS"},
            ]
        )
        out = apply_slot_policy(df, require_t2=True)
        self.assertEqual(set(out["sid"]), {"2", "3"})


class TestTaxonomyAndMisc(unittest.TestCase):
    def test_classify_busy_day(self) -> None:
        self.assertEqual(classify_busy_day(1, -3.0, -2.0), "quiet")
        self.assertIn("CRASH", classify_busy_day(4, -2.0, -1.0))
        self.assertIn("green_eod", classify_busy_day(4, +0.2, +1.0))

    def test_structure_and_t2(self) -> None:
        self.assertTrue(passes_structure_filters(not_ur=True, w5_weak=True))
        self.assertFalse(passes_structure_filters(not_ur=False, w5_weak=True))
        self.assertTrue(
            passes_structure_filters(not_ur=False, w5_weak=True, require_not_ur=False)
        )
        self.assertTrue(
            passes_structure_filters(not_ur=True, w5_weak=False, require_w5_weak=False)
        )
        self.assertTrue(is_t2_minute("09:35"))
        self.assertFalse(is_t2_minute("09:30"))
        self.assertFalse(is_t2_minute("09:25"))

    def test_t1_soft_slot_policy(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "date": "2026-01-02",
                    "sid": "T1",
                    "ex0": -5.0,
                    "ex3": 1.0,
                    "T2": False,
                    "rB_min": 0.0,
                    "half": "OOS",
                },
                {
                    "date": "2026-01-02",
                    "sid": "T2a",
                    "ex0": -4.5,
                    "ex3": 2.0,
                    "T2": True,
                    "rB_min": 0.0,
                    "half": "OOS",
                },
                {
                    "date": "2026-01-02",
                    "sid": "T2b",
                    "ex0": -4.2,
                    "ex3": 0.5,
                    "T2": True,
                    "rB_min": 0.0,
                    "half": "OOS",
                },
            ]
        )
        # T1 ex0=-5 with +1pp penalty → effective -4.0; fixed cap≤2 → T2a/T2b only
        out = apply_slot_policy_t1_soft(df, t1_ex_penalty_pp=1.0)
        self.assertEqual(set(out["sid"]), {"T2a", "T2b"})
        # Deep T1 still wins if deeper than penalty
        df2 = df.copy()
        df2.loc[df2["sid"] == "T1", "ex0"] = -6.5
        out2 = apply_slot_policy_t1_soft(df2, t1_ex_penalty_pp=1.0)
        self.assertIn("T1", set(out2["sid"]))
        self.assertEqual(len(out2), 2)
    def test_effective_sample_stats(self) -> None:
        df = pd.DataFrame(
            {
                "date": ["2026-01-02", "2026-01-02", "2026-02-03"],
                "sid": ["A", "B", "C"],
                "ex3": [1.0, 2.0, -0.5],
            }
        )
        s = effective_sample_stats(df)
        self.assertEqual(s["n_trades"], 3)
        self.assertEqual(s["n_day_clusters"], 2)
        self.assertEqual(s["max_same_day"], 2)
        self.assertEqual(s["per_year"], {"2026": 3})

    def test_mild_down(self) -> None:
        # Watch band: bret20 ∈ [−5%, 0)
        self.assertTrue(mild_down_flag(-2.0))
        self.assertTrue(mild_down_flag(-5.0))
        self.assertFalse(mild_down_flag(0.0))
        self.assertFalse(mild_down_flag(-6.0))

    def test_first_hit_index(self) -> None:
        s = pd.Series([False, False, True, True])
        self.assertEqual(first_hit_index(s), 2)
        self.assertIsNone(first_hit_index(pd.Series([False, False])))

    def test_build_path_frame_wma5_open_depth(self) -> None:
        # Stock dumps; bench flat → d_open deep; warmstart keeps Bw live at bar0.
        m = pd.DataFrame(
            {
                "minute": [f"09:{i:02d}" for i in range(0, 30, 5)],
                "S": [100.0, 99.0, 97.0, 95.0, 94.0, 93.0],
                "B": [1000.0, 1000.0, 1001.0, 1000.0, 999.0, 1000.0],
            }
        )
        warm = pd.Series([998.0, 999.0, 1000.0, 1000.0])
        f = build_path_frame(m, s0=100.0, b0=1000.0, bench_wma_len=5, b_warm=warm)
        self.assertTrue(f["Bw"].notna().all())
        self.assertLess(float(f["d_open"].iloc[-1]), -5.0)
        self.assertAlmostEqual(float(f["ex_open"].iloc[0]), 0.0, places=6)
        self.assertLess(float(f["ex_open"].iloc[-1]), -5.0)
        i = first_hit_index(f["d_open"] <= -3.0)
        self.assertIsNotNone(i)
        assert i is not None
        self.assertGreaterEqual(i, 2)

    def test_rank_by_d0(self) -> None:
        rows = [
            {"sid": "A", "ex0": -6.0, "d0": -2.0, "minute": "10:00"},
            {"sid": "B", "ex0": -4.0, "d0": -5.0, "minute": "09:40"},
        ]
        by_ex = rank_deepest_excess(rows, depth_key="ex0")
        by_d = rank_deepest_excess(rows, depth_key="d0")
        self.assertEqual(by_ex[0]["sid"], "A")
        self.assertEqual(by_d[0]["sid"], "B")

    def test_spearman_rho_anticorrelated(self) -> None:
        # Perfect anti-rank correlation
        rho = spearman_rho([1, 2, 3, 4], [4, 3, 2, 1])
        self.assertAlmostEqual(rho, -1.0, places=6)

    def test_concurrent_peak(self) -> None:
        picks = pd.DataFrame(
            [
                {"date": "2026-01-02", "exit_date": "2026-01-07"},
                {"date": "2026-01-03", "exit_date": "2026-01-08"},
                {"date": "2026-01-06", "exit_date": "2026-01-09"},
            ]
        )
        trading = [
            "2026-01-02",
            "2026-01-03",
            "2026-01-06",
            "2026-01-07",
            "2026-01-08",
            "2026-01-09",
        ]
        out = concurrent_open_peak(picks, trading)
        self.assertGreaterEqual(out["peak"], 2.0)

    def test_summarize_trades(self) -> None:
        df = pd.DataFrame(
            {
                "date": ["2025-09-01", "2025-11-01", "2025-12-01"],
                "half": ["IS", "OOS", "OOS"],
                "ex3": [1.0, 2.0, -0.5],
            }
        )
        s = summarize_trades(df, oos_cut="2025-10-01")
        self.assertEqual(s["n"], 3)
        self.assertEqual(s["oos_n"], 2)
        self.assertAlmostEqual(s["oos_hit"], 0.5)


class TestTotalOpenCap(unittest.TestCase):
    def test_skip_new_when_full(self) -> None:
        # max_open=2: D1 takes A+B; D2 still has 2 open through exit → skip C;
        # D3 (01-06) A/B still open (exit 01-07) → skip D.
        picks = pd.DataFrame(
            [
                {"date": "2026-01-02", "sid": "A", "ex0": -6.0, "exit_date": "2026-01-07", "ex3": 1.0},
                {"date": "2026-01-02", "sid": "B", "ex0": -5.0, "exit_date": "2026-01-07", "ex3": 1.0},
                {"date": "2026-01-03", "sid": "C", "ex0": -8.0, "exit_date": "2026-01-08", "ex3": 1.0},
                {"date": "2026-01-06", "sid": "D", "ex0": -7.0, "exit_date": "2026-01-09", "ex3": 1.0},
            ]
        )
        trading = [
            "2026-01-02",
            "2026-01-03",
            "2026-01-06",
            "2026-01-07",
            "2026-01-08",
            "2026-01-09",
        ]
        out = apply_total_open_cap(picks, trading, max_open=2)
        self.assertEqual(set(out["sid"]), {"A", "B"})
        peak = concurrent_open_peak(out, trading)
        self.assertLessEqual(peak["peak"], 2.0)

    def test_fills_remaining_room_deepest_first(self) -> None:
        picks = pd.DataFrame(
            [
                {"date": "2026-01-02", "sid": "A", "ex0": -5.0, "exit_date": "2026-01-03", "ex3": 1.0},
                {"date": "2026-01-03", "sid": "B", "ex0": -4.0, "exit_date": "2026-01-06", "ex3": 1.0},
                {"date": "2026-01-03", "sid": "C", "ex0": -9.0, "exit_date": "2026-01-06", "ex3": 1.0},
                {"date": "2026-01-03", "sid": "D", "ex0": -6.0, "exit_date": "2026-01-06", "ex3": 1.0},
            ]
        )
        trading = ["2026-01-02", "2026-01-03", "2026-01-06"]
        # max_open=2: day1 takes A. Day2: A still open (exit inclusive), room=1 → deepest C.
        out = apply_total_open_cap(picks, trading, max_open=2)
        self.assertEqual(list(out["sid"]), ["A", "C"])
        peak = concurrent_open_peak(out, trading)
        self.assertLessEqual(peak["peak"], 2.0)

    def test_apply_frozen_policy_chains(self) -> None:
        df = pd.DataFrame(
            [
                {"date": "2026-01-02", "sid": "1", "ex0": -5.0, "ex3": 1.0, "T2": True, "rB_min": 0.0, "exit_date": "2026-01-07"},
                {"date": "2026-01-02", "sid": "2", "ex0": -6.0, "ex3": 2.0, "T2": True, "rB_min": 0.0, "exit_date": "2026-01-07"},
                {"date": "2026-01-02", "sid": "3", "ex0": -4.5, "ex3": 0.5, "T2": False, "rB_min": 0.0, "exit_date": "2026-01-07"},
            ]
        )
        trading = ["2026-01-02", "2026-01-07"]
        out = apply_frozen_policy(df, trading, require_t2=True, max_open=6)
        self.assertEqual(set(out["sid"]), {"1", "2"})

    def test_mild_down_stratum(self) -> None:
        df = pd.DataFrame(
            {
                "date": ["2025-11-01", "2025-11-02"],
                "half": ["OOS", "OOS"],
                "ex3": [1.0, -1.0],
                "mild_down": [True, False],
            }
        )
        s = mild_down_stratum(df)
        self.assertEqual(s["policy"], "watch_only")
        self.assertEqual(s["mild_down"]["n"], 1)
        self.assertEqual(s["other"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
