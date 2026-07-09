"""Tests for GeoAlpha geometry layer."""

from __future__ import annotations

import unittest

from research.backtest.archive.geoalpha_panel import (
    classify_geo_phase,
    compute_s_alpha,
    compute_s_joint,
    extract_geo_features,
)


def _pt(
    *,
    spread_class: str = "extension",
    spread_rs: float = 2.0,
    spread_mom: float = 1.5,
    spread_dist: float = 3.0,
    w20q: str = "improving",
    w5q: str = "leading",
    hook_candidate: bool = False,
    hook_complete: bool = False,
    hook_retrace_pct: float | None = None,
) -> dict:
    return {
        "fresh": True,
        "spread_class": spread_class,
        "spread_rs": spread_rs,
        "spread_mom": spread_mom,
        "spread_dist": spread_dist,
        "w20": {"quadrant": w20q, "rs_ratio": 100.5},
        "w5": {"quadrant": w5q, "rs_ratio": 102.0},
        "hook_candidate": hook_candidate,
        "hook_complete": hook_complete,
        "hook_retrace_pct": hook_retrace_pct,
        "date": "2026-04-01",
        "minute": "10:00",
        "frame_id": "2026-04-01T10:00",
    }


class TestClassifyGeoPhase(unittest.TestCase):
    def test_extension_open(self) -> None:
        phi = classify_geo_phase(_pt(spread_class="extension", spread_rs=2.0, spread_mom=1.0))
        self.assertEqual(phi, "EXT_OPEN")

    def test_hook_pool(self) -> None:
        phi = classify_geo_phase(_pt(hook_candidate=True, hook_retrace_pct=0.03))
        self.assertEqual(phi, "HOOK_POOL")

    def test_hook_imp(self) -> None:
        phi = classify_geo_phase(
            _pt(
                hook_candidate=True,
                hook_retrace_pct=0.12,
                w20q="improving",
                w5q="improving",
                spread_rs=0.5,
            )
        )
        self.assertEqual(phi, "HOOK_IMP")

    def test_hook_strict(self) -> None:
        phi = classify_geo_phase(_pt(hook_complete=True))
        self.assertEqual(phi, "HOOK_STRICT")

    def test_pullback(self) -> None:
        phi = classify_geo_phase(
            _pt(spread_class="pullback", spread_rs=-2.0, spread_mom=-1.5)
        )
        self.assertEqual(phi, "PULLBACK")


class TestGeoAlphaScores(unittest.TestCase):
    def test_s_alpha_bounded(self) -> None:
        hi = compute_s_alpha(
            _pt(spread_class="extension", spread_rs=3.0, spread_mom=2.0),
            {
                "w5_drs_rsr_1d": 1.2,
                "bench_today_ret": 2.0,
                "roe_latest_q": 18.0,
                "revenue_yoy_pct": 20.0,
                "sig_ret": 1.0,
            },
        )
        lo = compute_s_alpha(
            _pt(spread_class="pullback", spread_rs=-2.0, spread_mom=-1.0),
            {"w5_drs_rsr_1d": -0.5, "bench_today_ret": -1.0, "sig_ret": 5.0},
        )
        self.assertGreaterEqual(hi, 0.0)
        self.assertLessEqual(hi, 1.0)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(lo, 1.0)
        self.assertGreater(hi, lo)

    def test_s_joint_gated(self) -> None:
        j = compute_s_joint(2.0, "EXT_OPEN", 0.8)
        self.assertGreater(j, 0.0)
        self.assertLessEqual(j, 1.0)
        low_geo = compute_s_joint(2.0, "OTHER", 0.8)
        self.assertLess(low_geo, j)

    def test_extract_geo_features_keys(self) -> None:
        geo = extract_geo_features(_pt())
        for key in ("g_dist", "g_retrace", "g_peak", "g_curv", "g_rs", "g_mom", "g_w20_rs"):
            self.assertIn(key, geo)
            self.assertGreaterEqual(geo[key], 0.0)
            self.assertLessEqual(geo[key], 1.0)


if __name__ == "__main__":
    unittest.main()
