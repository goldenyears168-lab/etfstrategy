"""Tests for market_breadth_impulse."""

from __future__ import annotations

import unittest

import pandas as pd

from market_breadth_impulse import (
    BreadthImpulseParams,
    build_impulse_panel_from_close,
    classify_zweig_ema_tier,
    compute_impulse_panel,
    impulse_event_snapshot_at,
    impulse_snapshot_at,
    luxalgo_exposure,
    ma_zone_exposure,
    rhythm_snapshot_at,
    zweig_state_exposure,
)


class TestMarketBreadthImpulse(unittest.TestCase):
    def test_ma_zone_exposure_tiers(self) -> None:
        self.assertEqual(ma_zone_exposure(15.0), 0.0)
        self.assertEqual(ma_zone_exposure(85.0), 1.0)
        self.assertEqual(ma_zone_exposure(55.0), 0.5)

    def test_zweig_thrust_detected(self) -> None:
        idx = pd.date_range("2024-01-01", periods=30, freq="B").strftime("%Y-%m-%d")
        adv = pd.Series([40.0] * 10 + [80.0] * 20, index=idx)
        decl = pd.Series([60.0] * 10 + [20.0] * 20, index=idx)
        p = BreadthImpulseParams(zweig_low=0.40, zweig_high=0.60, zweig_ema_span=5, thrust_hold_days=5)
        panel = compute_impulse_panel(adv, decl, pd.Index(idx), p)
        self.assertTrue(panel["zweig_thrust_today"].any() or panel["deemer_bam_today"].any())

    def test_luxalgo_exposure_boosts_on_thrust(self) -> None:
        idx = pd.date_range("2024-01-01", periods=30, freq="B").strftime("%Y-%m-%d")
        adv = pd.Series([40.0] * 10 + [80.0] * 20, index=idx)
        decl = pd.Series([60.0] * 10 + [20.0] * 20, index=idx)
        p = BreadthImpulseParams(thrust_hold_days=10)
        panel = compute_impulse_panel(adv, decl, pd.Index(idx), p)
        full = luxalgo_exposure(panel, p)
        state = zweig_state_exposure(panel, p)
        self.assertGreaterEqual(float(full.max()), float(state.max()))

    def test_classify_zweig_ema_tier(self) -> None:
        self.assertEqual(classify_zweig_ema_tier(0.40), "off")
        self.assertEqual(classify_zweig_ema_tier(0.47), "low")
        self.assertEqual(classify_zweig_ema_tier(0.55), "mid")
        self.assertEqual(classify_zweig_ema_tier(0.62), "high")

    def test_rhythm_snapshot(self) -> None:
        idx = pd.date_range("2024-01-01", periods=40, freq="B").strftime("%Y-%m-%d")
        close = pd.DataFrame(
            {f"S{i}": range(100, 100 + len(idx)) for i in range(30)},
            index=idx,
        )
        panel = build_impulse_panel_from_close(close)
        snap = rhythm_snapshot_at(panel, idx[-1])
        self.assertTrue(snap.get("available"))
        self.assertIn("zweig_ema_tier", snap)

    def test_impulse_event_snapshot(self) -> None:
        idx = pd.date_range("2024-01-01", periods=40, freq="B").strftime("%Y-%m-%d")
        close = pd.DataFrame(
            {f"S{i}": range(100, 100 + len(idx)) for i in range(30)},
            index=idx,
        )
        panel = build_impulse_panel_from_close(close)
        snap = impulse_event_snapshot_at(panel, idx[-1])
        self.assertTrue(snap.get("available"))
        self.assertIn("thrust_active", snap)
        self.assertNotIn("zweig_ema_tier", snap)

    def test_impulse_snapshot(self) -> None:
        idx = pd.date_range("2024-01-01", periods=40, freq="B").strftime("%Y-%m-%d")
        close = pd.DataFrame(
            {f"S{i}": range(100, 100 + len(idx)) for i in range(30)},
            index=idx,
        )
        panel = build_impulse_panel_from_close(close)
        snap = impulse_snapshot_at(panel, idx[-1])
        self.assertTrue(snap.get("available"))
        self.assertIn("thrust_active", snap)
        self.assertIn("zweig_ema_tier", snap)


if __name__ == "__main__":
    unittest.main()
