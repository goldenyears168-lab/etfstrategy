"""Minimal helpers for branch-follow all-events scan."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "run_branch_follow_all_events_scan",
    ROOT / "scripts/research/run_branch_follow_all_events_scan.py",
)
assert _SPEC and _SPEC.loader
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)


class EpisodeFirstTest(unittest.TestCase):
    def test_keeps_first_within_gap(self) -> None:
        dates = ["2025-01-02", "2025-01-05", "2025-01-10", "2025-01-20"]
        # prev=last kept: 01-05 within 5d of 01-02 → skip; 01-10 is 8d → keep
        got = m.episode_first_dates(dates, gap_days=5)
        self.assertEqual(got, ["2025-01-02", "2025-01-10", "2025-01-20"])

    def test_empty(self) -> None:
        self.assertEqual(m.episode_first_dates([]), [])


class SignificantBuysTest(unittest.TestCase):
    def test_abs_and_share_or_rank(self) -> None:
        df = pd.DataFrame(
            {
                "securities_trader_id": ["B1"] * 4,
                "trade_date": ["2025-01-02"] * 4,
                "stock_id": ["1111", "2222", "3333", "4444"],
                "buy_amt": [4e7, 3.5e7, 2e7, 1e7],
            }
        )
        sig = m.significant_buys(df, abs_floor=3e7, share_floor=0.10, top_k=3)
        self.assertEqual(set(sig["stock_id"]), {"1111", "2222"})


class SpearmanTest(unittest.TestCase):
    def test_perfect(self) -> None:
        rho = m.spearman_rho([1, 2, 3, 4], [1, 2, 3, 4])
        self.assertIsNotNone(rho)
        self.assertAlmostEqual(rho or 0.0, 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
