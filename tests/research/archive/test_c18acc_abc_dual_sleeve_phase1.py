"""Tests for C18acc × ABC v3+f1 Phase1 partitioned NAV."""

from __future__ import annotations

import unittest

from research.backtest.archive.c18acc_abc_dual_sleeve_phase1 import (
    combine_fixed_weight_nav,
    phase1_verdict,
    run_partitioned_combo_metrics,
)


def _nav(dates: list[str], rets: list[float], start: float = 100_000.0) -> list[dict]:
    out: list[dict] = []
    eq = start
    out.append({"date": dates[0], "equity_ntd": eq})
    for i, r in enumerate(rets, start=1):
        eq *= 1.0 + r / 100.0
        out.append({"date": dates[i], "equity_ntd": round(eq, 2)})
    return out


class TestCombineNav(unittest.TestCase):
    def test_70_30_combine(self) -> None:
        dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
        c18 = _nav(dates, [1.0, 1.0])
        abc = _nav(dates, [0.5, 0.5])
        combo_nav, eqs = combine_fixed_weight_nav(
            dates, c18, abc, total_capital=100_000, c18_weight=0.7, abc_weight=0.3
        )
        self.assertEqual(len(combo_nav), 3)
        self.assertAlmostEqual(eqs[0], 100_000.0, places=0)
        self.assertGreater(eqs[-1], 100_000.0)


class TestPartitionedMetrics(unittest.TestCase):
    def test_combo_beats_single_sleeve_metrics(self) -> None:
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        c18 = _nav(dates, [0.2, 0.2, 0.2])
        abc = _nav(dates, [0.5, 0.5, 0.5])
        combo = run_partitioned_combo_metrics(
            dates, c18, abc, total_capital=100_000, c18_weight=0.7, abc_weight=0.3
        )
        c18_only = run_partitioned_combo_metrics(
            dates, c18, abc, total_capital=100_000, c18_weight=1.0, abc_weight=0.0
        )
        c_full = combo["windows"]["FULL"]["mean_daily_return_pct"]
        b_full = c18_only["windows"]["FULL"]["mean_daily_return_pct"]
        self.assertGreater(c_full, b_full)


class TestVerdict(unittest.TestCase):
    def test_adopt_when_combo_beats_c18(self) -> None:
        combo = {
            "windows": {
                "FULL": {"mean_daily_return_pct": 0.25, "cagr_pct": 50.0},
                "OOS_H1": {"mean_daily_return_pct": 0.2},
            }
        }
        base = {
            "windows": {
                "FULL": {"mean_daily_return_pct": 0.20, "cagr_pct": 40.0},
                "OOS_H1": {"mean_daily_return_pct": 0.15},
            }
        }
        v = phase1_verdict(combo, base)
        self.assertTrue(v["adopt_dual_sleeve"])
        self.assertIn("ADOPT", v["summary"])

    def test_no_go_when_cagr_lags(self) -> None:
        combo = {
            "windows": {
                "FULL": {"mean_daily_return_pct": 0.25, "cagr_pct": 30.0},
                "OOS_H1": {},
            }
        }
        base = {
            "windows": {
                "FULL": {"mean_daily_return_pct": 0.20, "cagr_pct": 40.0},
                "OOS_H1": {},
            }
        }
        v = phase1_verdict(combo, base)
        self.assertFalse(v["adopt_dual_sleeve"])


if __name__ == "__main__":
    unittest.main()
