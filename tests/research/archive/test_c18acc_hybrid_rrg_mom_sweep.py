"""Tests for hybrid RRG (W20 ratio + WMA5 momentum)."""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research.backtest.archive.c18acc_hybrid_rrg_mom_sweep import (
    HYBRID_RRG_VARIANTS,
    build_rrg_panels_for_spec,
    daily_chart_metrics,
    hybrid_signal_quality,
)
from rrg_rotation import compute_rrg_panel, rs_momentum_from_ratio, rs_ratio_momentum


class TestHybridRrgMom(unittest.TestCase):
    def test_hybrid_mom_differs_from_w20_symmetric(self) -> None:
        idx = pd.date_range("2024-01-01", periods=80, freq="B")
        asset = pd.Series(np.linspace(100, 130, len(idx)), index=idx)
        bench = pd.Series(np.linspace(100, 115, len(idx)), index=idx)
        _, mom20 = rs_ratio_momentum(asset, bench, length=20)
        _, mom_hybrid = rs_ratio_momentum(asset, bench, length=20, mom_length=5)
        tail20 = float(mom20.dropna().iloc[-1])
        tail_h = float(mom_hybrid.dropna().iloc[-1])
        self.assertTrue(np.isfinite(tail20))
        self.assertNotAlmostEqual(tail20, tail_h, places=2)

    def test_rs_momentum_from_ratio_matches_hybrid(self) -> None:
        idx = pd.date_range("2024-01-01", periods=60, freq="B")
        close = pd.DataFrame({"2330": np.linspace(500, 620, len(idx))}, index=idx)
        bench = pd.Series(np.linspace(18000, 19000, len(idx)), index=idx)
        rs20, mom_panel, _ = compute_rrg_panel(close, bench, length=20, mom_length=5)
        mom_direct = rs_momentum_from_ratio(rs20["2330"], mom_length=5)
        self.assertFalse(mom_panel["2330"].dropna().empty)
        self.assertAlmostEqual(
            float(mom_panel["2330"].dropna().iloc[-1]),
            float(mom_direct.dropna().iloc[-1]),
            places=4,
        )

    def test_w20_m5_spec_keeps_ratio_length(self) -> None:
        spec = next(v for v in HYBRID_RRG_VARIANTS if v.spec_id == "W20-M5")
        idx = [f"2024-01-{d:02d}" for d in range(1, 41)]
        close = pd.DataFrame(
            {"2330": np.linspace(500, 600, 40), "2317": np.linspace(100, 90, 40)},
            index=idx,
        )
        bench = pd.Series(np.linspace(18000, 18500, 40), index=idx)
        rs_h, _, _ = build_rrg_panels_for_spec(close, bench, spec)
        rs20, _, _ = compute_rrg_panel(close, bench, length=20)
        self.assertAlmostEqual(
            float(rs_h["2330"].dropna().iloc[-1]),
            float(rs20["2330"].dropna().iloc[-1]),
            places=6,
        )

    def test_signal_quality_structure(self) -> None:
        idx = pd.date_range("2024-01-01", periods=90, freq="B")
        close = pd.DataFrame(
            {
                "2330": np.linspace(500, 640, len(idx)),
                "2317": np.linspace(100, 88, len(idx)),
            },
            index=[d.strftime("%Y-%m-%d") for d in idx],
        )
        bench = pd.Series(
            np.linspace(18000, 18600, len(idx)),
            index=[d.strftime("%Y-%m-%d") for d in idx],
        )
        tds = close.index.astype(str).tolist()
        out = hybrid_signal_quality(close, bench, tds, horizons=(1, 3), confirm_within=2)
        self.assertIn("exit_warning", out)
        self.assertIn("entry_early", out)
        self.assertIn("forward_excess_when_hybrid_weak", out["exit_warning"])
        self.assertIn("h1", out["exit_warning"]["forward_excess_when_hybrid_weak"])

    def test_daily_metrics_shape(self) -> None:
        idx = [f"2024-01-{d:02d}" for d in range(1, 45)]
        close = pd.DataFrame({"2330": np.linspace(500, 600, 44)}, index=idx)
        bench = pd.Series(np.linspace(18000, 18500, 44), index=idx)
        spec = HYBRID_RRG_VARIANTS[2]
        _, _, w20_quad = build_rrg_panels_for_spec(close, bench, HYBRID_RRG_VARIANTS[0])
        out = daily_chart_metrics(
            close, bench, idx[-20:], spec=spec, w20_quad=w20_quad
        )
        self.assertEqual(out["spec_id"], "W20-M5")
        self.assertIsNotNone(out.get("quadrant_flip_rate_pct"))


if __name__ == "__main__":
    unittest.main()
