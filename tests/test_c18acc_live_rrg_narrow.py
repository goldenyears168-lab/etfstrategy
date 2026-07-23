"""C18acc live RRG narrowing · terminal values match full-history compute."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.backtest.rrg_mono_score_swap_c import build_daily_spread_rs_panel
from rrg_mono_swap_accel_screen import (
    RRG_LIVE_LOOKBACK_BARS,
    _signal_rrg_panels,
    load_close_bench_for_screen,
)
from rrg_rotation import compute_rrg_panel


def _synth_panels(
    *,
    n_days: int = 400,
    n_stocks: int = 8,
) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2024-01-02", periods=n_days).strftime("%Y-%m-%d")
    close = pd.DataFrame(
        {
            f"{1000 + i}": 100.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.015, n_days))
            for i in range(n_stocks)
        },
        index=dates,
    )
    bench = pd.Series(
        100.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.01, n_days)),
        index=dates,
        name="bench",
    )
    return close, bench


class TestSignalRrgPanelsNarrow(unittest.TestCase):
    def test_lookback_and_cols_match_full_at_as_of(self) -> None:
        close, bench = _synth_panels()
        as_of = str(close.index[-5])
        ids = list(close.columns[:3])
        _, _, rs_full, mom_full, _ = _signal_rrg_panels(
            close, bench, as_of, stock_ids=None, lookback_bars=None
        )
        _, _, rs_n, mom_n, dates_n = _signal_rrg_panels(
            close,
            bench,
            as_of,
            stock_ids=ids,
            lookback_bars=RRG_LIVE_LOOKBACK_BARS,
        )
        self.assertLessEqual(len(dates_n), RRG_LIVE_LOOKBACK_BARS)
        self.assertEqual(list(rs_n.columns), ids)
        for sid in ids:
            self.assertTrue(
                np.isclose(rs_full.at[as_of, sid], rs_n.at[as_of, sid], rtol=0, atol=1e-9)
            )
            self.assertTrue(
                np.isclose(mom_full.at[as_of, sid], mom_n.at[as_of, sid], rtol=0, atol=1e-9)
            )


class TestSpreadRsPanelNarrow(unittest.TestCase):
    def test_slot_subset_matches_full(self) -> None:
        close, bench = _synth_panels()
        as_of = str(close.index[-1])
        ids = list(close.columns[:2])
        full = build_daily_spread_rs_panel(close, bench)
        narrow = build_daily_spread_rs_panel(
            close,
            bench,
            stock_ids=ids,
            lookback_bars=RRG_LIVE_LOOKBACK_BARS,
        )
        self.assertEqual(list(narrow.columns), ids)
        for sid in ids:
            self.assertTrue(
                np.isclose(full.at[as_of, sid], narrow.at[as_of, sid], rtol=0, atol=1e-9)
            )

    def test_empty_stock_ids_returns_empty(self) -> None:
        close, bench = _synth_panels()
        empty = build_daily_spread_rs_panel(close, bench, stock_ids=[])
        self.assertEqual(list(empty.columns), [])


class TestSpreadClassLookbackEquiv(unittest.TestCase):
    def test_column_trim_matches_full_rrg_terminal(self) -> None:
        close, bench = _synth_panels()
        as_of = str(close.index[-1])
        sid = str(close.columns[0])
        panel = close.loc[close.index.astype(str) <= as_of]
        rs_full, mom_full, _ = compute_rrg_panel(panel, bench.reindex(panel.index), length=20)
        trimmed = panel[[sid]].iloc[-80:]
        rs_n, mom_n, _ = compute_rrg_panel(
            trimmed, bench.reindex(trimmed.index), length=20
        )
        self.assertTrue(
            np.isclose(rs_full.at[as_of, sid], rs_n.at[as_of, sid], rtol=0, atol=1e-9)
        )
        self.assertTrue(
            np.isclose(mom_full.at[as_of, sid], mom_n.at[as_of, sid], rtol=0, atol=1e-9)
        )


class TestCloseBenchCache(unittest.TestCase):
    def test_second_load_hits_disk_cache(self) -> None:
        close, bench = _synth_panels(n_days=60, n_stocks=3)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "panel.pkl"
            conn = object()  # unused; fingerprint + load mocked

            with (
                patch(
                    "rrg_mono_swap_accel_screen.CLOSE_PANEL_CACHE_PATH",
                    cache_path,
                ),
                patch(
                    "rrg_mono_swap_accel_screen._daily_bars_fingerprint",
                    return_value=("2026-07-09", 100),
                ),
                patch(
                    "rrg_mono_swap_accel_screen.load_price_panels",
                    return_value=(close, close, close),
                ) as mock_load,
                patch(
                    "rrg_mono_swap_accel_screen.load_benchmark_close",
                    return_value=bench,
                ),
            ):
                c1, b1 = load_close_bench_for_screen(conn, use_cache=True)  # type: ignore[arg-type]
                c2, b2 = load_close_bench_for_screen(conn, use_cache=True)  # type: ignore[arg-type]
            self.assertEqual(mock_load.call_count, 1)
            self.assertTrue(cache_path.is_file())
            pd.testing.assert_frame_equal(c1, c2)
            pd.testing.assert_series_equal(b1, b2)


if __name__ == "__main__":
    unittest.main()
