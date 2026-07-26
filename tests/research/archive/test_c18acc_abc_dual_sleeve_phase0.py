"""Tests for C18acc × ABC v3+f1 dual-sleeve Phase0 diagnostics."""

from __future__ import annotations

import unittest

from research.backtest.archive.c18acc_abc_dual_sleeve_phase0 import (
    compute_daily_return_correlation,
    compute_entry_overlap,
    compute_leg_return_correlation,
    phase0_verdict,
    render_c18acc_abc_dual_sleeve_phase0_md,
)


def _row(d: str, sid: str, ret: float) -> dict:
    return {"entry_date": d, "stock_id": sid, "return_pct": ret, "net_pct": ret}


class TestEntryOverlap(unittest.TestCase):
    def test_low_overlap_disjoint(self) -> None:
        c18 = [_row("2024-01-02", "2330", 1.0)]
        abc = [_row("2024-01-03", "2454", 2.0)]
        ov = compute_entry_overlap(c18, abc)
        self.assertEqual(ov["both_executed_pairs"], 0)
        self.assertEqual(ov["entry_jaccard_executed"], 0.0)
        self.assertEqual(ov["same_day_stock_rate_executed"], 0.0)

    def test_conflict_same_day_stock(self) -> None:
        c18 = [_row("2024-01-02", "2330", 1.0), _row("2024-01-03", "2454", 2.0)]
        abc = [_row("2024-01-02", "2330", 3.0), _row("2024-01-04", "2317", 1.0)]
        ov = compute_entry_overlap(c18, abc)
        self.assertEqual(ov["both_executed_pairs"], 1)
        self.assertAlmostEqual(ov["entry_jaccard_executed"], 1 / 3, places=4)
        self.assertAlmostEqual(ov["same_day_stock_rate_executed"], 1 / 3, places=4)

    def test_underfill_days(self) -> None:
        c18 = [_row("2024-01-02", "2330", 1.0)]  # 1 < 3 slots
        abc = [_row("2024-01-02", "2454", 2.0), _row("2024-01-03", "2317", 1.0)]
        cand = [_row("2024-01-02", "2454", 2.0), _row("2024-01-02", "3008", 1.0)]
        ov = compute_entry_overlap(c18, abc, abc_candidate_rows=cand)
        # 2024-01-02 (1 entry) and 2024-01-03 (0 entries) both underfill
        self.assertEqual(ov["c18_underfill_days"], 2)
        self.assertEqual(ov["abc_executed_on_c18_underfill_days"], 2)
        self.assertEqual(ov["abc_candidates_on_c18_underfill_days"], 1)


class TestCorrelation(unittest.TestCase):
    def test_daily_return_corr(self) -> None:
        c18_nav = [
            {"date": "2024-01-02", "equity_ntd": 100_000},
            {"date": "2024-01-03", "equity_ntd": 101_000},
            {"date": "2024-01-04", "equity_ntd": 102_000},
            {"date": "2024-01-05", "equity_ntd": 103_000},
        ]
        abc_nav = [
            {"date": "2024-01-02", "equity_ntd": 100_000},
            {"date": "2024-01-03", "equity_ntd": 100_500},
            {"date": "2024-01-04", "equity_ntd": 101_000},
            {"date": "2024-01-05", "equity_ntd": 101_500},
        ]
        corr = compute_daily_return_correlation(c18_nav, abc_nav)
        self.assertEqual(corr["n_aligned_days"], 3)
        self.assertIsNotNone(corr["daily_return_corr"])
        self.assertGreater(corr["daily_return_corr"], 0.9)

    def test_leg_corr_overlap(self) -> None:
        c18 = [_row("2024-01-02", "2330", 2.0), _row("2024-01-03", "2454", -1.0)]
        abc = [_row("2024-01-02", "2330", 1.5), _row("2024-01-04", "2317", 3.0)]
        leg = compute_leg_return_correlation(c18, abc)
        self.assertEqual(leg["n_overlap_legs"], 1)


class TestVerdict(unittest.TestCase):
    def test_go_when_all_pass(self) -> None:
        v = phase0_verdict(
            {"entry_jaccard_executed": 0.1, "same_day_stock_rate_executed": 0.05},
            {"daily_return_corr": 0.2},
        )
        self.assertTrue(v["go_phase1_dual_sleeve"])
        self.assertIn("GO_PHASE1", v["summary"])

    def test_no_go_high_jaccard(self) -> None:
        v = phase0_verdict(
            {"entry_jaccard_executed": 0.5, "same_day_stock_rate_executed": 0.05},
            {"daily_return_corr": 0.2},
        )
        self.assertFalse(v["go_phase1_dual_sleeve"])
        self.assertIn("NO_GO", v["summary"])


class TestRenderMd(unittest.TestCase):
    def test_render_contains_verdict(self) -> None:
        overlap = {
            "c18_executed_pairs": 10,
            "abc_executed_pairs": 20,
            "entry_jaccard_executed": 0.05,
            "same_day_stock_rate_executed": 0.02,
            "c18_underfill_days": 50,
            "abc_executed_on_c18_underfill_days": 10,
            "abc_candidates_on_c18_underfill_days": 15,
        }
        correlation = {"daily_return_corr": 0.1, "n_aligned_days": 100}
        # Use the real verdict builder so `summary` reflects actual go/no-go
        # wording instead of an arbitrary hand-written string.
        verdict = phase0_verdict(overlap, correlation)
        md = render_c18acc_abc_dual_sleeve_phase0_md(
            {
                "date_start": "2024-01-01",
                "date_end": "2024-06-30",
                "n_trade_dates": 120,
                "source": "test",
                "overlap": overlap,
                "correlation": correlation,
                "verdict": verdict,
            }
        )
        self.assertTrue(verdict["go_phase1_dual_sleeve"])
        self.assertIn("GO_PHASE1_DUAL_SLEEVE", md)
        self.assertIn("Jaccard", md)


if __name__ == "__main__":
    unittest.main()
