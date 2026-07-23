"""Unit tests for c18acc_open_timing_study helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from research.backtest.c18acc_open_timing_study import (
    PRIOR_FILL_MINUTE,
    apply_prior_day_1300_fills,
    prior_day_1300_fill,
    prior_day_1300_px,
)
from research.backtest.rrg_lens_score_swap import _prior_trading_date


class TestPriorDayResolve(unittest.TestCase):
    def test_prior_trading_date(self) -> None:
        dates = ["2026-01-02", "2026-01-05", "2026-01-06"]
        self.assertEqual(_prior_trading_date(dates, "2026-01-06"), "2026-01-05")
        self.assertIsNone(_prior_trading_date(dates, "2026-01-02"))


class TestPriorDay1300Fill(unittest.TestCase):
    def setUp(self) -> None:
        self.full_dates = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
        self.leg = {
            "stock_id": "2330",
            "stock_name": "TSMC",
            "signal_date": "2026-01-06",
            "entry_date": "2026-01-06",
            "exit_date": "2026-01-07",
            "entry_px": 100.0,
            "exit_px": 110.0,
            "entry_minute": "09:30",
            "return_pct": 10.0,
            "excess_pct": 8.0,
            "bench_return_pct": 2.0,
        }

    def test_missing_px_returns_none(self) -> None:
        conn = MagicMock()
        with patch(
            "research.backtest.c18acc_open_timing_study.prior_day_1300_px",
            return_value=None,
        ):
            self.assertIsNone(
                prior_day_1300_fill(conn, self.leg, full_dates=self.full_dates)
            )

    def test_fill_recomputes_return(self) -> None:
        conn = MagicMock()
        # prior 13:00 px=100 · exit=110 → +10%; bench=1% → excess=9%
        with patch(
            "research.backtest.c18acc_open_timing_study.prior_day_1300_px",
            return_value=100.0,
        ), patch(
            "research.backtest.c18acc_open_timing_study.bench_return_entry_to_exit",
            return_value=1.0,
        ):
            out = prior_day_1300_fill(conn, self.leg, full_dates=self.full_dates)
        assert out is not None
        self.assertEqual(out["entry_date"], "2026-01-05")
        self.assertEqual(out["entry_minute"], PRIOR_FILL_MINUTE)
        self.assertEqual(out["entry_px"], 100.0)
        self.assertEqual(out["exit_date"], "2026-01-07")
        self.assertEqual(out["return_pct"], 10.0)
        self.assertEqual(out["excess_pct"], 9.0)
        self.assertEqual(out["baseline_entry_date"], "2026-01-06")
        self.assertTrue(out["gross_win"])

    def test_bad_window_skipped(self) -> None:
        conn = MagicMock()
        leg = dict(self.leg)
        leg["exit_date"] = "2026-01-05"  # prior == exit
        with patch(
            "research.backtest.c18acc_open_timing_study.prior_day_1300_px",
            return_value=100.0,
        ):
            self.assertIsNone(prior_day_1300_fill(conn, leg, full_dates=self.full_dates))

    def test_apply_counts_skips(self) -> None:
        conn = MagicMock()
        legs = [self.leg, dict(self.leg, stock_id="2317")]
        with patch(
            "research.backtest.c18acc_open_timing_study.prior_day_1300_px",
            side_effect=[95.0, None],
        ), patch(
            "research.backtest.c18acc_open_timing_study.bench_return_entry_to_exit",
            return_value=0.5,
        ):
            filled, meta = apply_prior_day_1300_fills(
                conn, legs, full_dates=self.full_dates
            )
        self.assertEqual(meta["n_baseline"], 2)
        self.assertEqual(meta["n_filled"], 1)
        self.assertEqual(meta["n_skipped_no_px"], 1)
        self.assertEqual(len(filled), 1)
        # 110/95 - 1 = 15.789...%
        self.assertAlmostEqual(filled[0]["return_pct"], (110 / 95 - 1) * 100, places=3)


class TestPriorDay1300Px(unittest.TestCase):
    def test_uses_cache_and_price_helper(self) -> None:
        conn = MagicMock()
        bars = (("09:30:00", 90.0), ("13:00:00", 101.5), ("13:30:00", 102.0))
        cache: dict = {}
        with patch(
            "research.backtest.c18acc_open_timing_study.load_kbar_day_closes",
            return_value=bars,
        ) as load_mock:
            px = prior_day_1300_px(
                conn, stock_id="2330", prior_date="2026-01-05", kbar_cache=cache
            )
        self.assertEqual(px, 101.5)
        load_mock.assert_called_once()
        # second call hits cache
        with patch(
            "research.backtest.c18acc_open_timing_study.load_kbar_day_closes"
        ) as load2:
            px2 = prior_day_1300_px(
                conn, stock_id="2330", prior_date="2026-01-05", kbar_cache=cache
            )
        self.assertEqual(px2, 101.5)
        load2.assert_not_called()


if __name__ == "__main__":
    unittest.main()
