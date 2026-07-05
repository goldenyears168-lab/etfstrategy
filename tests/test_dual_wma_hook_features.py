from __future__ import annotations

import unittest

from research.backtest.dual_wma_tactical_slot_backtest import (
    HookEntryParams,
    _matches_hook_buy,
)
from rrg_universe_intraday_panel import (
    DEFAULT_HOOK_RETRACE_EPS,
    annotate_hook_features,
    classify_dual_wma_trade_signal,
    is_hook_peak_frame,
)


def _day_points(
    spreads: list[tuple[str, float, float, float]],
    *,
    w20q: str = "improving",
    w5q: str = "improving",
    w20_rs: float = 99.0,
) -> list[dict]:
    """(spread_class, dist, spread_rs, spread_mom) per poll."""
    pts: list[dict] = []
    for i, (sc, dist, srs, sms) in enumerate(spreads):
        pts.append(
            {
                "date": "2026-06-01",
                "minute": f"09:{i:02d}",
                "frame_id": f"2026-06-01T09:{i:02d}",
                "fresh": True,
                "w20": {"quadrant": w20q, "rs_ratio": w20_rs},
                "w5": {"quadrant": w5q},
                "spread_class": sc,
                "spread_dist": dist,
                "spread_rs": srs,
                "spread_mom": sms,
            }
        )
    annotate_hook_features(pts, retrace_eps=DEFAULT_HOOK_RETRACE_EPS)
    return pts


class HookFeaturesTest(unittest.TestCase):
    def test_hook_complete_after_extension_peak(self) -> None:
        pts = _day_points(
            [
                ("aligned", 1.0, 0.2, 0.1),
                ("extension", 2.5, 1.5, 1.2),
                ("extension", 3.0, 1.8, 1.5),
                ("extension", 2.8, 1.6, 1.3),
                ("aligned", 1.2, 0.3, 0.2),
            ]
        )
        complete = [p for p in pts if p.get("hook_complete")]
        self.assertEqual(len(complete), 1)
        self.assertAlmostEqual(complete[0]["hook_peak_dist"], 3.0)
        self.assertGreaterEqual(float(complete[0]["hook_retrace_pct"]), 0.15)
        self.assertEqual(classify_dual_wma_trade_signal(complete[0]), "hook_buy")

    def test_no_hook_without_extension_peak(self) -> None:
        pts = _day_points(
            [
                ("aligned", 1.0, 0.2, 0.1),
                ("aligned", 0.8, 0.1, 0.1),
            ]
        )
        self.assertFalse(any(p.get("hook_complete") for p in pts))

    def test_mixed_peak_with_allow_mixed(self) -> None:
        p = {
            "spread_class": "mixed",
            "spread_dist": 1.8,
            "spread_rs": 1.0,
            "spread_mom": 0.8,
        }
        self.assertFalse(is_hook_peak_frame(p, peak_signal_min=1.5, allow_mixed_peak=False))
        self.assertTrue(is_hook_peak_frame(p, peak_signal_min=1.5, allow_mixed_peak=True))

    def test_mixed_peak_hook_complete(self) -> None:
        pts = _day_points(
            [
                ("aligned", 1.0, 0.2, 0.1),
                ("mixed", 1.8, 1.0, 0.8),
                ("mixed", 1.9, 1.1, 0.9),
                ("aligned", 1.1, 0.3, 0.2),
            ]
        )
        annotate_hook_features(pts, retrace_eps=0.10, peak_signal_min=1.5, allow_mixed_peak=True)
        complete = [p for p in pts if p.get("hook_complete")]
        self.assertEqual(len(complete), 1)

    def test_matches_hook_buy_quality_gate(self) -> None:
        p = {
            "fresh": True,
            "hook_complete": True,
            "w20": {"quadrant": "improving", "rs_ratio": 99.0},
            "w5": {"quadrant": "improving"},
            "spread_rs": 0.5,
            "spread_mom": 0.3,
            "hook_quality": 0.05,
        }
        self.assertTrue(
            _matches_hook_buy(p, HookEntryParams(pool_tier="strict", hook_quality_min=0.0))
        )
        self.assertFalse(
            _matches_hook_buy(p, HookEntryParams(pool_tier="strict", hook_quality_min=0.1))
        )

    def test_rejects_non_improving(self) -> None:
        p = {
            "fresh": True,
            "hook_complete": True,
            "w20": {"quadrant": "leading", "rs_ratio": 101.0},
            "w5": {"quadrant": "improving"},
            "spread_rs": 0.5,
            "spread_mom": 0.3,
            "hook_quality": 0.1,
        }
        self.assertFalse(_matches_hook_buy(p, HookEntryParams(pool_tier="strict")))

    def test_hook_candidate_wide_pool_arc(self) -> None:
        pts = _day_points(
            [
                ("aligned", 0.8, 0.2, 0.1),
                ("extension", 1.2, 0.8, 0.6),
                ("extension", 2.0, 1.0, 0.8),
                ("aligned", 1.85, 0.3, 0.2),
            ]
        )
        candidates = [p for p in pts if p.get("hook_candidate")]
        self.assertGreaterEqual(len(candidates), 1)
        self.assertFalse(any(p.get("hook_complete") for p in pts))
        pool_pt = candidates[-1]
        self.assertEqual(classify_dual_wma_trade_signal(pool_pt), "hook_pool")
        self.assertTrue(
            _matches_hook_buy(pool_pt, HookEntryParams(pool_tier="pool"))
        )

    def test_pool_tier_point_filters(self) -> None:
        p = {
            "fresh": True,
            "hook_candidate": True,
            "w20": {"quadrant": "improving", "rs_ratio": 97.0},
            "w5": {"quadrant": "improving"},
            "spread_rs": 0.5,
            "spread_mom": 0.3,
            "spread_dist": 1.6,
            "hook_retrace_pct": 0.12,
            "hook_pool_peak_dist": 1.3,
        }
        base = HookEntryParams(pool_tier="pool")
        self.assertTrue(_matches_hook_buy(p, base))
        self.assertFalse(
            _matches_hook_buy(
                p,
                HookEntryParams(pool_tier="pool", hook_retrace_pct_min=0.15),
            )
        )
        self.assertFalse(
            _matches_hook_buy(
                p,
                HookEntryParams(pool_tier="pool", spread_dist_max=1.5),
            )
        )
        self.assertTrue(
            _matches_hook_buy(
                p,
                HookEntryParams(pool_tier="pool", require_both_vectors=True),
            )
        )


if __name__ == "__main__":
    unittest.main()
