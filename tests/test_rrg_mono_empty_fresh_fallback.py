"""Tests for RRG mono fresh=0 fallback backtest."""

from __future__ import annotations

import unittest

from research.backtest.rrg_mono_backtest import (
    EmptyFreshPolicy,
    _last_nonempty_fresh,
    _resolve_entry_pool,
)
from rrg_mono_daily_brief import ScanRow


def _row(sid: str, seg: float) -> ScanRow:
    return ScanRow(
        stock_id=sid,
        stock_name=sid,
        fresh=True,
        mono=True,
        seg_last=seg,
        disp=1.2,
        segs=[0.1, 0.2, seg],
        quadrants=["leading"] * 4,
        rs_ratio=1.0,
        rs_momentum=1.0,
        daily_pct=None,
    )


class TestEmptyFreshFallback(unittest.TestCase):
    def test_last_nonempty_fresh(self) -> None:
        fresh = {
            "2024-01-02": [],
            "2024-01-03": [_row("2330", 0.5)],
            "2024-01-04": [],
        }
        dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
        prev = _last_nonempty_fresh(fresh, dates, "2024-01-04")
        self.assertEqual(len(prev), 1)
        self.assertEqual(prev[0].stock_id, "2330")

    def test_resolve_baseline_empty(self) -> None:
        pool, source = _resolve_entry_pool(
            "2024-01-04",
            fresh_mono=[],
            policy="baseline",
            fresh_by_date={},
            trade_dates=["2024-01-04"],
        )
        self.assertEqual(source, "empty")
        self.assertEqual(pool, [])

    def test_resolve_prev_day(self) -> None:
        fresh_by_date = {
            "2024-01-03": [_row("2330", 0.5)],
            "2024-01-04": [],
        }
        pool, source = _resolve_entry_pool(
            "2024-01-04",
            fresh_mono=[],
            policy="prev_day",
            fresh_by_date=fresh_by_date,
            trade_dates=["2024-01-03", "2024-01-04"],
        )
        self.assertEqual(source, "prev_day")
        self.assertEqual(pool[0].stock_id, "2330")

    def test_fresh_wins_over_fallback(self) -> None:
        today = [_row("2454", 0.8)]
        pool, source = _resolve_entry_pool(
            "2024-01-04",
            fresh_mono=today,
            policy="no_fresh",
            fresh_by_date={},
            trade_dates=["2024-01-04"],
            mono_by_date={"2024-01-04": [_row("2330", 0.9)]},
        )
        self.assertEqual(source, "fresh")
        self.assertEqual(pool[0].stock_id, "2454")


if __name__ == "__main__":
    unittest.main()
